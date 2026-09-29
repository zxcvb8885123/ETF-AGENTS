import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from etf_agent.backtest.replay import ReplayError, ReplayRequest, build_assumed_status, prepare_backtest_database
from etf_agent.backtest.replay.contracts import available_at_for, cutoff_for
from etf_agent.backtest.replay.report import max_drawdown, nav_series
from etf_agent.data.trading_status import TradingStatusBundleValidator


def make_source(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE instruments (symbol TEXT, in_competition_universe INTEGER);
        CREATE TABLE raw_payloads (id INTEGER PRIMARY KEY, fetched_at TEXT);
        CREATE TABLE daily_prices (symbol TEXT, trade_date TEXT, fetched_at TEXT, raw_payload_id INTEGER);
        INSERT INTO instruments VALUES ('2330.TW', 1), ('9999.TW', 1);
        INSERT INTO raw_payloads VALUES (1, '2026-09-13T00:00:00+00:00');
        INSERT INTO daily_prices VALUES
          ('2330.TW', '2026-08-03', '2026-09-13T00:00:00+00:00', 1),
          ('2330.TW', '2026-08-04', '2026-09-13T00:00:00+00:00', 1),
          ('9999.TW', '2026-09-03', '2026-09-13T00:00:00+00:00', 1);
        """
    )
    connection.commit()
    connection.close()


class ReplayAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.db"
        make_source(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_rewrites_only_the_copy_and_keeps_original_times(self):
        target = self.root / "backtest.db"
        result = prepare_backtest_database(self.source, target, "2026-08-20")
        self.assertEqual(result["excluded_symbols_not_yet_trading"], ["9999.TW"])
        copy = sqlite3.connect(target)
        times = dict(copy.execute("SELECT trade_date, fetched_at FROM daily_prices WHERE symbol='2330.TW'"))
        self.assertEqual(times["2026-08-03"], available_at_for("2026-08-03"))
        originals = {row[0] for row in copy.execute("SELECT original_fetched_at FROM backtest_reconstruction")}
        self.assertIn("2026-09-13T00:00:00+00:00", originals)
        payload_time = copy.execute("SELECT fetched_at FROM raw_payloads WHERE id=1").fetchone()[0]
        self.assertEqual(payload_time, available_at_for("2026-08-03"))
        copy.close()
        source = sqlite3.connect(self.source)
        self.assertEqual(
            source.execute("SELECT DISTINCT fetched_at FROM daily_prices").fetchall(),
            [("2026-09-13T00:00:00+00:00",)],
        )
        self.assertEqual(source.execute("SELECT COUNT(*) FROM instruments WHERE in_competition_universe=1").fetchone()[0], 2)
        source.close()

    def test_price_is_not_available_before_reconstructed_time(self):
        self.assertGreater(available_at_for("2026-08-03"), "2026-08-03T08:55:00+00:00")
        self.assertEqual(cutoff_for(date(2026, 8, 3)), "2026-08-03T08:55:00+08:00")

    def test_refuses_same_path_and_existing_target(self):
        with self.assertRaises(ReplayError):
            prepare_backtest_database(self.source, self.source, "2026-08-20")
        target = self.root / "backtest.db"
        prepare_backtest_database(self.source, target, "2026-08-20")
        with self.assertRaises(ReplayError):
            prepare_backtest_database(self.source, target, "2026-08-20")


class ReplayStatusTests(unittest.TestCase):
    def test_assumed_status_validates_and_targets_decision_day(self):
        snapshot = {
            "snapshot_id": "s1", "universe_version": "u1", "decision_cutoff": "2026-08-03T08:55:00+08:00",
            "latest_prices": [{"symbol": "2330.TW"}, {"symbol": "6488.TWO"}],
        }
        bundle, assessment = build_assumed_status(snapshot, date(2026, 8, 3))
        self.assertEqual(bundle["target_session"]["end"], "2026-08-03T13:30:00+08:00")
        self.assertEqual({item["state"] for item in assessment["symbols"]}, {"allowed"})
        self.assertTrue(all(item["source_id"] == "BACKTEST_ASSUMED_NO_HISTORICAL_STATUS" for item in bundle["source_coverage"]))
        self.assertEqual(TradingStatusBundleValidator().validate(bundle, assessment), [])

    def test_request_rejects_reversed_or_bad_dates(self):
        with self.assertRaises(ReplayError):
            ReplayRequest.parse("2026-08-20", "2026-08-03")
        with self.assertRaises(ReplayError):
            ReplayRequest.parse("8/3", "8/20")


class ReplayReportTests(unittest.TestCase):
    def test_nav_series_prefers_close_states_and_drawdown(self):
        states = [
            {"type": "genesis", "as_of": "2026-07-31T17:00:00+08:00", "trade_date": None, "nav": "1000", "cash_weight": "1", "position_count": 0},
            {"type": "prepared", "as_of": "x", "trade_date": "2026-07-31", "nav": "1000", "cash_weight": "1", "position_count": 0},
            {"type": "close", "as_of": "x", "trade_date": "2026-08-03", "nav": "1100", "cash_weight": "0.1", "position_count": 25},
            {"type": "prepared", "as_of": "x", "trade_date": "2026-08-03", "nav": "999", "cash_weight": "0.1", "position_count": 25},
            {"type": "close", "as_of": "x", "trade_date": "2026-08-04", "nav": "990", "cash_weight": "0.1", "position_count": 25},
        ]
        series = nav_series(states)
        self.assertEqual([point["date"] for point in series], ["2026-07-31", "2026-08-03", "2026-08-04"])
        self.assertNotIn("genesis", {point["source"] for point in series})
        by_date = {point["date"]: point["nav"] for point in series}
        self.assertEqual(by_date["2026-08-03"], "1100")
        from decimal import Decimal
        self.assertEqual(max_drawdown(series), Decimal("990") / Decimal("1100") - Decimal("1"))


if __name__ == "__main__":
    unittest.main()
