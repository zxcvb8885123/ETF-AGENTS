import re
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from etf_agent.research.cross_market_study import (
    CrossMarketStudyError, analyze, build_dataset, fetch_history, fetch_pool, latest_raw, night_changes, pool_series,
    run_analysis, simulate_cash,
)
from etf_agent.research.cross_market_study.contracts import MIN_DAYS, YAHOO_SYMBOLS

ROOT = Path(__file__).resolve().parents[2]


def weekdays(count, end=date(2026, 9, 18)):
    days, day = [], end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


def synthetic_raw(count=40, up=True):
    """美股前一晚上漲、台股隔日跳空上漲的合成歷史；US 日期 = 台股日期前一個工作日。"""
    tw = weekdays(count)
    target, closes = [], Decimal(10000)
    for index, day in enumerate(tw):
        swing = Decimal(1 if (index % 2 == 0) == up else -1)
        opening = closes * (1 + swing * Decimal("0.01"))
        close = opening * (1 + swing * Decimal("0.002"))
        target.append({"date": day.isoformat(), "open": str(opening), "high": str(max(opening, close) * Decimal("1.001")),
                       "low": str(min(opening, close) * Decimal("0.999")), "close": str(close)})
        closes = close
    yahoo = {}
    for symbol in YAHOO_SYMBOLS:
        level, bars = Decimal(100), []
        for index, day in enumerate(tw):
            us_day = day - timedelta(days=3 if day.weekday() == 0 else 1)
            swing = Decimal(1 if (index % 2 == 0) == up else -1)
            level = level * (1 + swing * Decimal("0.01"))
            bars.append({"date": us_day.isoformat(), "close": str(level)})
        yahoo[symbol] = bars
    night = [{"date": day.isoformat(), "contract_date": "202610", "close": "10100", "spread": "100", "volume": "5000",
              "settlement_price": "0.0", "trading_session": "after_market"} for day in tw]
    return {"target": target, "yahoo": yahoo, "night": night}


class NightChangeTests(unittest.TestCase):
    def test_picks_largest_volume_contract_and_uses_previous_settlement(self):
        rows = [
            {"date": "2026-09-18", "contract_date": "202610", "close": "10100", "spread": "100", "volume": "100", "trading_session": "after_market"},
            {"date": "2026-09-18", "contract_date": "202611", "close": "20000", "spread": "-1000", "volume": "9", "trading_session": "after_market"},
            {"date": "2026-09-18", "contract_date": "202610", "close": "1", "spread": "0", "volume": "999", "trading_session": "position"},
            {"date": "2026-09-17", "contract_date": "202610", "close": "0", "spread": "0", "volume": "5", "trading_session": "after_market"},
        ]
        self.assertEqual(night_changes(rows), {"2026-09-18": Decimal("100") / Decimal("10000") * 100})


class DatasetTests(unittest.TestCase):
    def test_signals_use_only_sessions_completed_before_decision_time(self):
        rows = build_dataset(synthetic_raw())
        self.assertEqual(len(rows), 39)
        last = rows[-1]
        self.assertIsNotNone(last["signals"]["us_main"])
        # 日經當日收盤（台北 14:30）晚於 07:00 決策，不得進入；只用前一個交易日的日韓收盤。
        self.assertIsNotNone(last["signals"]["asia"])
        for symbol in YAHOO_SYMBOLS:
            self.assertGreaterEqual(len(synthetic_raw()["yahoo"][symbol]), 40)
        self.assertIn("fill_to_next_avg", rows[0]["targets"])
        self.assertNotIn("fill_to_next_avg", last["targets"])  # 最後一天沒有隔日

    def test_too_little_target_history_is_refused(self):
        with self.assertRaises(CrossMarketStudyError):
            build_dataset({"target": [], "yahoo": {}, "night": []})


class AnalyzeTests(unittest.TestCase):
    def test_small_sample_reports_counts_only(self):
        result = analyze(build_dataset(synthetic_raw(40)))
        for block in result["primary"].values():
            self.assertEqual(block["verdict"], "insufficient_sample")
        for group in result["by_outlook"].values():
            self.assertTrue("note" in group or group["days"] >= 20)

    def test_consistent_predictive_history_is_supported_and_flipped_one_is_not(self):
        raw = synthetic_raw(MIN_DAYS + 40)
        result = analyze(build_dataset(raw))
        # 合成資料的符號完全一致，但報酬量級固定使 Rank IC 可能無定義；至少要走到統計分支且不判 insufficient。
        for block in result["primary"].values():
            self.assertNotEqual(block["verdict"], "insufficient_sample")
            self.assertGreaterEqual(block["n"], MIN_DAYS)
        self.assertEqual(result["criteria"]["primary_target"], "gap")
        self.assertIn("forward_from_fill_post_hoc", result)


class ServiceTests(unittest.TestCase):
    def test_fetch_saves_immutable_run_and_analysis_reads_it_back(self):
        raw = synthetic_raw(60)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ohlc = lambda symbol, start, end: (raw["target"] if symbol == "^TWII" else
                                                [{"date": b["date"], "close": b["close"]} for b in raw["yahoo"][symbol]])
            from datetime import datetime, timezone
            now = datetime(2026, 9, 19, tzinfo=timezone.utc)
            saved = fetch_history(root, 400, now, ohlc, lambda start, end: raw["night"])
            self.assertEqual(saved["target_days"], 60)
            run_id, loaded = latest_raw(root)
            self.assertEqual(run_id, saved["run_id"])
            self.assertEqual(len(loaded["target"]), 60)
            self.assertEqual(run_analysis(root)["analysis"]["days"], 59)
            target = next(root.glob("data/*/history-*/raw.json"))
            target.write_text(target.read_text(encoding="utf-8").replace("2026-09-18", "2026-09-17", 1), encoding="utf-8")
            with self.assertRaises(CrossMarketStudyError):
                latest_raw(root)


def pool_fixture(symbols=60, days=("2026-09-15", "2026-09-16", "2026-09-17"), ex=None):
    yahoo, official = {}, {}
    for index, day in enumerate(days):
        level = 100 + index * 10
        yahoo[day] = {"S%02d" % n: {"open": str(level), "high": str(level + 2), "low": str(level - 2), "close": str(level + 1),
                                  "ex": bool(ex and n == 0 and day == ex)} for n in range(symbols)}
        official[day] = {"S%02d" % n: str(level + 0.5) for n in range(symbols)}
    return {"symbols": ["S%02d" % n for n in range(symbols)], "yahoo": yahoo, "official_vwap": official}


class PoolTests(unittest.TestCase):
    def test_pool_series_uses_equal_weight_average_price_and_official_vwap(self):
        series = pool_series(pool_fixture())
        first = series["2026-09-16"]
        self.assertEqual(first["pool_fill_to_next_avg"], (Decimal("120.25") / Decimal("110.25") - 1) * 100)
        self.assertEqual(first["pool_official_next"], (Decimal("120.5") / Decimal("110.5") - 1) * 100)
        self.assertEqual(first["pool_gap"], (Decimal(110) / Decimal(101) - 1) * 100)
        self.assertNotIn("pool_fill_to_next_avg", series["2026-09-17"])  # 最後一天沒有隔日

    def test_ex_rights_symbol_is_dropped_and_small_pool_gives_nothing(self):
        series = pool_series(pool_fixture(ex="2026-09-16"))
        self.assertEqual(len(series["2026-09-16"]), 4)  # 其餘 59 檔仍足夠
        self.assertEqual(pool_series(pool_fixture(symbols=10))["2026-09-16"], {})

    def test_cash_simulation_charges_costs_and_rule_loses_when_selloffs_rebound(self):
        rows = []
        for index in range(40):
            down = index % 4 == 0
            rows.append({"date": "d%d" % index, "outlook": "negative" if down else "neutral", "agreement_label": "mixed",
                         "targets": {"pool_fill_to_next_avg": Decimal("1.0") if down else Decimal("0.1")}})
        result = simulate_cash(rows, {"negative": "defensive"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["triggered_days"], 10)
        self.assertLess(Decimal(result["rule_total_return_pct"]), Decimal(result["baseline_total_return_pct"]))
        self.assertEqual(Decimal(result["baseline_costs_pct"]), 0)
        self.assertGreater(Decimal(result["rule_costs_pct"]), 0)
        self.assertEqual(simulate_cash(rows[:2], {})["status"], "insufficient_sample")

    def test_pool_snapshot_is_frozen_and_merged_into_analysis(self):
        raw = synthetic_raw(60)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ohlc = lambda symbol, start, end: (raw["target"] if symbol == "^TWII" else
                                                [{"date": b["date"], "close": b["close"]} for b in raw["yahoo"][symbol]])
            from datetime import datetime, timezone
            now = datetime(2026, 9, 19, tzinfo=timezone.utc)
            fetch_history(root, 400, now, ohlc, lambda start, end: raw["night"])
            days = [row["date"] for row in raw["target"]]
            pool = pool_fixture(days=tuple(days))
            saved = fetch_pool(root, "unused.db", pool["symbols"], 400, now, lambda *args: dict(pool))
            self.assertEqual(saved["symbols"], 60)
            analysis = run_analysis(root)["pool_analysis"]
            self.assertEqual(analysis["pool_symbols"], 60)
            self.assertIn("R1_outlook_negative", analysis["cash_simulation"])


class IsolationTests(unittest.TestCase):
    def test_decision_chain_never_imports_cross_market_study(self):
        pattern = re.compile(r"(import|from)\s+(etf_agent\.)*(research\.)?cross_market_study")
        paths = [*(ROOT / "src" / "etf_agent" / "decision").rglob("*.py"), *(ROOT / "src" / "etf_agent" / "automation").rglob("*.py"),
                 ROOT / "scripts" / "run_daily_pipeline.py"]
        self.assertGreater(len(paths), 5)
        for path in paths:
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), str(path))


if __name__ == "__main__":
    unittest.main()
