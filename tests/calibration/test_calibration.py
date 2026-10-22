import json
import re
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from etf_agent.calibration import CalibrationError, CalibrationRepository, build_observation, evaluate, execution_gap_report, record_all, record_day
from etf_agent.calibration.tools import average_ranks, rank_ic
from etf_agent.core.artifact_store import ImmutableRunStore

ROOT = Path(__file__).resolve().parents[2]
SYMBOLS = ["1101.TW", "1102.TW", "1103.TW", "1104.TW", "1105.TW"]
CONVICTION = {"1101.TW": "high", "1102.TW": "medium", "1103.TW": "low", "1104.TW": "low", "1105.TW": None}
INTENT = {"1101.TW": "buy", "1102.TW": "buy", "1103.TW": "buy", "1104.TW": "buy", "1105.TW": "no_trade"}
DAILY_GROWTH = {"1101.TW": Decimal("1.02"), "1102.TW": Decimal("1.01"), "1103.TW": Decimal("1.00"), "1104.TW": Decimal("0.99"), "1105.TW": Decimal("1.00")}


def make_database(path, symbols=SYMBOLS):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE raw_payloads (id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL);
        CREATE TABLE instruments (symbol TEXT PRIMARY KEY, in_competition_universe INTEGER);
        CREATE TABLE daily_prices (
            symbol TEXT, trade_date TEXT, open_price TEXT, high_price TEXT, low_price TEXT, close_price TEXT,
            volume_shares INTEGER, trade_value INTEGER, source TEXT, fetched_at TEXT, raw_payload_id INTEGER, ex_rights_marker INTEGER
        );
        INSERT INTO raw_payloads VALUES (1, 'raw-sha');
        """
    )
    connection.executemany("INSERT INTO instruments VALUES (?, 1)", [(symbol,) for symbol in symbols])
    connection.commit()
    connection.close()


def add_price(path, symbol, trade_date, price, *, volume=1000, source="TWSE_STOCK_DAY", fetched="2026-12-31T00:00:00+00:00", ex_rights=0, trade_value=None,
              open_price=None, high=None, low=None):
    value = int(Decimal(price) * volume) if trade_value is None else trade_value
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO daily_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (symbol, trade_date, open_price or str(price), high or str(price), low or str(price), str(price), volume, value, source, fetched, ex_rights),
    )
    connection.commit()
    connection.close()


def weekdays(start, count):
    day, result = start, []
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day)
        day += timedelta(days=1)
    return result


class RankIcTests(unittest.TestCase):
    def test_perfect_and_reverse_order(self):
        pairs = [(Decimal(i), Decimal(i) * 2) for i in range(1, 6)]
        self.assertEqual(round(rank_ic(pairs), 9), 1)
        self.assertEqual(round(rank_ic([(a, -b) for a, b in pairs]), 9), -1)

    def test_ties_use_average_rank(self):
        self.assertEqual(average_ranks([Decimal(3), Decimal(1), Decimal(3), Decimal(2)]), [Decimal("3.5"), Decimal(1), Decimal("3.5"), Decimal(2)])

    def test_undefined_cases_return_none(self):
        self.assertIsNone(rank_ic([(Decimal(1), Decimal(1)), (Decimal(2), Decimal(2))]))
        self.assertIsNone(rank_ic([(Decimal(1), Decimal(1)), (Decimal(1), Decimal(2)), (Decimal(1), Decimal(3))]))
        self.assertIsNone(rank_ic([(Decimal(1), Decimal(5)), (Decimal(2), Decimal(5)), (Decimal(3), Decimal(5))]))


class DecisionRunFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.decisions = self.tmp / "decisions"

    def save_decision(self, run_id="decision-20261022T113043Z", cutoff="2026-10-22T09:58:38+00:00", status="completed", drop_bear=False):
        intent = {
            "decision_id": "trade-decision:" + run_id, "snapshot_id": "snap", "decision_cutoff": cutoff, "status": status,
            "items": [{"symbol": symbol, "intent": INTENT[symbol], "conviction": CONVICTION[symbol]} for symbol in SYMBOLS],
        }
        packets = [
            {"role": "bull", "items": [{"symbol": symbol, "strength": "moderate"} for symbol in SYMBOLS]},
            {"role": "bear", "items": [{"symbol": symbol, "strength": "weak"} for symbol in SYMBOLS[:-1 if drop_bear else None]]},
        ]
        store = ImmutableRunStore(self.decisions / "2026-10-22", schema_version="1.0")
        return store.save(run_id, {"intent": intent, "debate": {"packets": packets}})


class ObservationTests(DecisionRunFixture):
    def test_builds_observation_with_both_strengths(self):
        observation = build_observation(self.save_decision())
        first = observation["items"][0]
        self.assertEqual((first["symbol"], first["conviction"], first["bull_strength"], first["bear_strength"]), ("1101.TW", "high", "moderate", "weak"))
        self.assertEqual(len(observation["items"]), 5)

    def test_tampered_archive_is_rejected(self):
        run = self.save_decision()
        intent = json.loads((run / "intent.json").read_text(encoding="utf-8"))
        intent["items"][0]["conviction"] = "low"
        (run / "intent.json").write_text(json.dumps(intent), encoding="utf-8")
        with self.assertRaises(CalibrationError):
            build_observation(run)

    def test_missing_strength_incomplete_intent_are_rejected(self):
        with self.assertRaises(CalibrationError):
            build_observation(self.save_decision(drop_bear=True))
        with self.assertRaises(CalibrationError):
            build_observation(self.save_decision(run_id="decision-20261023T113043Z", status="failed"))

    def test_recording_never_touches_the_decision_directory_and_is_idempotent(self):
        run = self.save_decision()
        before = {path.name: path.read_bytes() for path in run.iterdir()}
        repository = CalibrationRepository(self.tmp / "calibration")
        first = record_day(run, repository)
        second = record_day(run, repository)
        self.assertEqual(first, second)
        self.assertEqual(before, {path.name: path.read_bytes() for path in run.iterdir()})
        self.assertEqual(len(repository.observations()), 1)

    def test_record_all_reports_skipped_runs_without_stopping(self):
        self.save_decision()
        self.save_decision(run_id="decision-20261023T113043Z", status="failed")
        result = record_all(self.decisions, CalibrationRepository(self.tmp / "calibration"))
        self.assertEqual(result["recorded"], ["decision-20261022T113043Z"])
        self.assertEqual(len(result["skipped"]), 1)

    def test_tampered_observation_is_rejected(self):
        repository = CalibrationRepository(self.tmp / "calibration")
        run_dir = record_day(self.save_decision(), repository)
        path = run_dir / "observation.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["items"][0]["conviction"] = "low"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CalibrationError):
            repository.observations()


class EvaluateTests(DecisionRunFixture):
    def setUp(self):
        super().setUp()
        self.db = self.tmp / "prices.db"
        make_database(self.db)
        self.repository = CalibrationRepository(self.tmp / "calibration")

    def build_world(self, decision_days, total_days=45):
        days = weekdays(date(2026, 6, 1), total_days)
        for index, day in enumerate(days):
            for symbol in SYMBOLS:
                add_price(self.db, symbol, day.isoformat(), DAILY_GROWTH[symbol] ** index * 100)
        for index in range(decision_days):
            run_id = "decision-%sT100000Z" % days[index].strftime("%Y%m%d")
            run = ImmutableRunStore(self.decisions / days[index].isoformat(), schema_version="1.0").save(run_id, {
                "intent": {
                    "decision_id": "d" + run_id, "snapshot_id": "s", "status": "completed", "decision_cutoff": "%sT10:00:00+00:00" % days[index],
                    "items": [{"symbol": symbol, "intent": INTENT[symbol], "conviction": CONVICTION[symbol]} for symbol in SYMBOLS],
                },
                "debate": {"packets": [
                    {"role": "bull", "items": [{"symbol": symbol, "strength": "moderate"} for symbol in SYMBOLS]},
                    {"role": "bear", "items": [{"symbol": symbol, "strength": "weak"} for symbol in SYMBOLS]},
                ]},
            })
            record_day(run, self.repository)
        return days

    def test_enough_days_report_means_and_rank_ic(self):
        self.build_world(20)
        report = evaluate(self.repository, self.db, "2026-12-31T00:00:00+00:00")
        for horizon in ("1", "5", "20"):
            self.assertEqual(report["horizons"][horizon]["status"], "ok")
            self.assertEqual(report["horizons"][horizon]["complete_days"], 20)
        by = report["horizons"]["5"]["by_conviction"]
        self.assertGreater(Decimal(by["high"]["mean_excess_return"]), Decimal(by["low"]["mean_excess_return"]))
        # 兩檔 low 信心同分（名次取平均），所以略低於 1。
        self.assertGreater(Decimal(report["horizons"]["5"]["rank_ic"]["conviction"]["mean"]), Decimal("0.9"))
        self.assertEqual(report["horizons"]["5"]["rank_ic"]["conviction"]["n_days"], 20)

    def test_fewer_than_twenty_days_gives_counts_but_no_conclusion(self):
        self.build_world(19)
        entry = evaluate(self.repository, self.db, "2026-12-31T00:00:00+00:00")["horizons"]["5"]
        self.assertEqual(entry["status"], "insufficient_sample")
        self.assertEqual(entry["complete_days"], 19)
        self.assertGreater(entry["n_obs"], 0)
        for key in ("by_conviction", "by_intent", "rank_ic"):
            self.assertNotIn(key, entry)

    def test_prices_fetched_after_as_of_are_not_used(self):
        self.build_world(3, total_days=30)
        report = evaluate(self.repository, self.db, "2026-06-01T00:00:00+00:00")
        entry = report["horizons"]["1"]
        self.assertEqual(entry["complete_days"], 0)
        self.assertEqual(entry["n_obs"], 0)
        self.assertEqual(set(entry["pending_days"].values()), {"entry_price_pending"})

    def test_exit_price_not_yet_published_is_pending_not_filled(self):
        self.build_world(2, total_days=3)
        entry = evaluate(self.repository, self.db, "2026-12-31T00:00:00+00:00")["horizons"]["5"]
        self.assertEqual(entry["complete_days"], 0)
        self.assertEqual(set(entry["pending_days"].values()), {"exit_price_pending"})

    def test_yahoo_rows_and_zero_volume_never_give_an_average_price(self):
        db = self.tmp / "yahoo.db"
        make_database(db)
        add_price(db, "1101.TW", "2026-10-23", 100, source="YAHOO_FINANCE", trade_value=0)
        add_price(db, "1101.TW", "2026-10-26", 100, volume=0, trade_value=0)
        from etf_agent.calibration.prices import OfficialPriceBook
        book = OfficialPriceBook(db, "2026-12-31T00:00:00+00:00")
        self.assertEqual(book.trading_days("2026-10-23"), ["2026-10-26"])
        self.assertIsNone(book.vwap("2026-10-23").get("1101.TW"))
        self.assertIsNone(book.vwap("2026-10-26")["1101.TW"])

    def test_ex_rights_window_is_excluded(self):
        self.build_world(1, total_days=8)
        days = weekdays(date(2026, 6, 1), 8)
        connection = sqlite3.connect(self.db)
        connection.execute("UPDATE daily_prices SET ex_rights_marker = 1 WHERE symbol = '1101.TW' AND trade_date = ?", (days[3].isoformat(),))
        connection.commit()
        connection.close()
        report = evaluate(self.repository, self.db, "2026-12-31T00:00:00+00:00")
        self.assertEqual(report["horizons"]["5"]["excluded_obs"]["ex_rights"], 1)

    def test_naive_as_of_is_rejected(self):
        with self.assertRaises(CalibrationError):
            evaluate(self.repository, self.db, "2026-12-31T00:00:00")

    def test_superseded_runs_on_the_same_execution_day_count_once(self):
        days = self.build_world(1, total_days=8)
        later = ImmutableRunStore(self.decisions / days[0].isoformat(), schema_version="1.0").save("decision-%sT110000Z" % days[0].strftime("%Y%m%d"), {
            "intent": {"decision_id": "later", "snapshot_id": "s", "status": "completed", "decision_cutoff": "%sT11:00:00+00:00" % days[0],
                       "items": [{"symbol": symbol, "intent": INTENT[symbol], "conviction": CONVICTION[symbol]} for symbol in SYMBOLS]},
            "debate": {"packets": [
                {"role": "bull", "items": [{"symbol": symbol, "strength": "moderate"} for symbol in SYMBOLS]},
                {"role": "bear", "items": [{"symbol": symbol, "strength": "weak"} for symbol in SYMBOLS]},
            ]},
        })
        record_day(later, self.repository)
        report = evaluate(self.repository, self.db, "2026-12-31T00:00:00+00:00")
        self.assertEqual((report["observation_count"], report["decision_days"]), (2, 1))


class GapAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "gap.db"
        make_database(self.db, symbols=["2330.TW", "2317.TW"])

    def test_measures_gap_between_previous_close_and_average_price(self):
        add_price(self.db, "2330.TW", "2026-10-19", 100)
        add_price(self.db, "2330.TW", "2026-10-20", 100, trade_value=104000, open_price="110", high="110", low="110")  # 均價 104、開盤漲停
        add_price(self.db, "2317.TW", "2026-10-19", 50)
        add_price(self.db, "2317.TW", "2026-10-20", 50, volume=0, trade_value=0)  # 沒有成交，沒有均價
        report = execution_gap_report(self.db, "2026-12-31T00:00:00+00:00")
        self.assertEqual(report["pairs"], 1)
        self.assertEqual(report["vwap_vs_prev_close"]["p50"], "0.040000")
        self.assertEqual(report["open_limit_pairs"], {"up": 1, "down": 0})
        self.assertEqual(report["missing_vwap"], [{"symbol": "2317.TW", "trade_date": "2026-10-20"}])

    def test_ex_rights_and_calendar_gaps_are_skipped(self):
        add_price(self.db, "2330.TW", "2026-10-19", 100)
        add_price(self.db, "2330.TW", "2026-10-20", 90, ex_rights=1)
        add_price(self.db, "2330.TW", "2026-10-30", 95)
        report = execution_gap_report(self.db, "2026-12-31T00:00:00+00:00")
        self.assertEqual((report["pairs"], report["skipped_ex_rights_pairs"], report["skipped_calendar_gap_pairs"]), (0, 1, 1))

    def test_naive_as_of_is_rejected(self):
        with self.assertRaises(CalibrationError):
            execution_gap_report(self.db, "2026-12-31T00:00:00")


class DailyScriptHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("run_daily_pipeline_calibration", ROOT / "scripts" / "run_daily_pipeline.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_success_is_recorded_and_runs_the_cli_without_importing_the_package(self):
        from unittest.mock import MagicMock, patch
        with patch.object(self.module.subprocess, "run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as run:
            self.assertEqual(self.module.record_calibration(), {"status": "recorded"})
        command = run.call_args[0][0]
        self.assertTrue(command[1].endswith("cli/calibration.py"))
        self.assertEqual(command[2], "snapshot")

    def test_failures_are_reported_but_never_raised(self):
        from unittest.mock import MagicMock, patch
        with patch.object(self.module.subprocess, "run", return_value=MagicMock(returncode=1, stdout="", stderr="boom")):
            result = self.module.record_calibration()
        self.assertEqual((result["status"], result["exit_code"], result["error"]), ("error", 1, "boom"))
        with patch.object(self.module.subprocess, "run", side_effect=OSError("no python")):
            self.assertEqual(self.module.record_calibration()["status"], "error")


class IsolationTests(unittest.TestCase):
    def test_decision_chain_never_imports_calibration(self):
        pattern = re.compile(r"(import|from)\s+(etf_agent\.|\.)*calibration|from etf_agent import .*calibration")
        paths = [*(ROOT / "src" / "etf_agent" / "decision").rglob("*.py"), *(ROOT / "src" / "etf_agent" / "automation").rglob("*.py"),
                 ROOT / "scripts" / "run_daily_pipeline.py"]
        self.assertGreater(len(paths), 5)
        for path in paths:
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), str(path))


if __name__ == "__main__":
    unittest.main()
