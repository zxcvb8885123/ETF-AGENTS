"""基準標的收集：只保存行情、不污染 150 檔交易池；未核准來源不收集。"""

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from etf_agent.data import DailyPrice, HistoricalPriceProvider, HistoricalResponse, Instrument, MarketDataDatabase, SnapshotRepository
from etf_agent.data.benchmarks import (
    BenchmarkConfigError,
    benchmark_instruments,
    collect_benchmark_history,
    load_benchmarks,
    parse_benchmarks,
)
from etf_agent.data.historical_collector import HistoricalPriceCollector

TSMC = Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14")
CONFIG = {
    "version": "test",
    "benchmarks": [
        {"benchmark_id": "0050_buy_and_hold", "label": "0050", "kind": "single_instrument", "symbol": "0050",
         "name": "元大台灣50", "market": "TWSE", "source": "TWSE_STOCK_DAY", "license_status": "approved"},
        {"benchmark_id": "universe_equal_weight", "label": "等權", "kind": "universe_equal_weight",
         "universe": "data/official_universe.csv", "license_status": "approved"},
        {"benchmark_id": "0056_pending", "label": "0056", "kind": "single_instrument", "symbol": "0056",
         "name": "元大高股息", "market": "TWSE", "source": "TWSE_STOCK_DAY", "license_status": "unverified"},
    ],
}


class FakeProvider(HistoricalPriceProvider):
    """不連網：回傳固定一個月的日線。"""

    def __init__(self):
        super().__init__("https://example.invalid/STOCK_DAY")
        self.requested = []

    def fetch(self, instrument, month):
        self.requested.append((instrument.symbol, month))
        prices = [
            DailyPrice(symbol=instrument.symbol, code=instrument.code, name=instrument.name, trade_date="2026-10-02",
                       open_price=None, high_price=None, low_price=None, close_price=Decimal("100"), change=None,
                       volume_shares=10_000_000, trade_value=1_005_000_000, transactions=1),
        ]
        return HistoricalResponse(prices=tuple(prices), payload="{}", endpoint="fixture://" + instrument.symbol,
                                  fetched_at="2026-10-02T17:00:00+08:00", source=self.twse_source, warnings=())


class BenchmarkConfigTests(unittest.TestCase):
    def test_parse_and_filter_approved_single_instruments(self):
        benchmarks = parse_benchmarks(CONFIG)
        self.assertEqual([item.benchmark_id for item in benchmarks], ["0050_buy_and_hold", "universe_equal_weight", "0056_pending"])
        self.assertEqual(benchmarks[0].instrument.symbol, "0050.TW")
        self.assertEqual(benchmarks[0].instrument.code, "0050")
        instruments, skipped = benchmark_instruments(benchmarks)
        self.assertEqual([item.symbol for item in instruments], ["0050.TW"])
        self.assertEqual(len(skipped), 1)
        self.assertIn("unverified", skipped[0])

    def test_invalid_config_fails_closed(self):
        with self.assertRaises(BenchmarkConfigError):
            parse_benchmarks({"benchmarks": []})
        with self.assertRaises(BenchmarkConfigError):
            parse_benchmarks({"benchmarks": [{"benchmark_id": "x", "label": "x", "kind": "weird", "license_status": "approved"}]})
        with self.assertRaises(BenchmarkConfigError):
            parse_benchmarks({"benchmarks": [{"benchmark_id": "x", "label": "x", "kind": "single_instrument", "license_status": "approved", "symbol": "0050", "market": "TWSE"}]})
        duplicated = {"benchmarks": [CONFIG["benchmarks"][1], CONFIG["benchmarks"][1]]}
        with self.assertRaises(BenchmarkConfigError):
            parse_benchmarks(duplicated)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(BenchmarkConfigError):
                load_benchmarks(Path(temp) / "missing.json")

    def test_project_config_is_valid_and_official_only(self):
        root = Path(__file__).resolve().parents[2]
        benchmarks = load_benchmarks(root / "config" / "benchmarks.json")
        single = [item for item in benchmarks if item.kind == "single_instrument"]
        self.assertTrue(single)
        for item in single:
            self.assertNotEqual(item.source, "YAHOO_FINANCE")
            self.assertEqual(item.license_status, "approved")
            self.assertTrue(item.evidence)


class BenchmarkCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = MarketDataDatabase(Path(self.temp.name) / "m.db")
        self.database.initialize()
        with self.database.connect() as connection:
            self.database.upsert_instruments(connection, [TSMC], True)
        self.benchmarks = parse_benchmarks(CONFIG)

    def universe(self):
        with self.database.connect() as connection:
            return [str(row["symbol"]) for row in SnapshotRepository.load_universe(connection)]

    def test_collection_stores_prices_without_joining_the_universe(self):
        provider = FakeProvider()
        result, skipped = collect_benchmark_history(self.database, provider, self.benchmarks, date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(result.stored_rows, 1)
        self.assertEqual([item[0] for item in provider.requested], ["0050.TW"])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(self.universe(), ["2330.TW"])
        with self.database.connect() as connection:
            row = connection.execute("SELECT in_competition_universe, name FROM instruments WHERE symbol = '0050.TW'").fetchone()
            self.assertEqual(int(row["in_competition_universe"]), 0)
            self.assertEqual(row["name"], "元大台灣50")
            stored = connection.execute("SELECT source, close_price FROM daily_prices WHERE symbol = '0050.TW'").fetchone()
            self.assertEqual(stored["source"], "TWSE_STOCK_DAY")
            # Snapshot 的全池覆蓋判斷不受基準列影響：交易池 2330 沒有 10-02 行情，所以沒有完整交易日。
            self.assertIsNone(SnapshotRepository.find_latest_trade_date(connection, "2026-10-31", "2026-11-01T00:00:00+00:00"))
            self.assertEqual(len(connection.execute("SELECT * FROM analysis_daily_prices").fetchall()), 0)

    def test_existing_universe_member_is_not_demoted(self):
        # 若基準標的恰好也在交易池，in_competition_universe 以 MAX 保留為 1。
        collector = HistoricalPriceCollector(self.database, FakeProvider(), max_workers=1, in_competition_universe=False)
        collector.collect([TSMC], date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(self.universe(), ["2330.TW"])

    def test_default_collector_still_marks_universe_members(self):
        collector = HistoricalPriceCollector(self.database, FakeProvider(), max_workers=1)
        etf = Instrument("0050.TW", "0050", "元大台灣50", "TWSE", "2026-09-11")
        collector.collect([etf], date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(self.universe(), ["0050.TW", "2330.TW"])

    def test_nothing_to_collect_when_no_benchmark_is_approved(self):
        pending = parse_benchmarks({"benchmarks": [CONFIG["benchmarks"][2]]})
        result, skipped = collect_benchmark_history(self.database, FakeProvider(), pending, date(2026, 10, 1), date(2026, 10, 31))
        self.assertIsNone(result)
        self.assertEqual(len(skipped), 1)
        with self.database.connect() as connection:
            self.assertIsNone(connection.execute("SELECT symbol FROM instruments WHERE symbol = '0056.TW'").fetchone())

    def test_source_mismatch_is_rejected_before_fetching(self):
        mismatched = parse_benchmarks({"benchmarks": [{**CONFIG["benchmarks"][0], "source": "TWSE_STOCK_DAY_ALL"}]})
        provider = FakeProvider()
        with self.assertRaises(BenchmarkConfigError):
            collect_benchmark_history(self.database, provider, mismatched, date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(provider.requested, [])


class CliTests(unittest.TestCase):
    def test_compare_cli_prints_table_and_writes_json(self):
        import importlib.util
        import io
        import sys
        from contextlib import redirect_stdout

        from etf_agent.virtual_account import VirtualAccountRepository, VirtualAccountService

        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location("cli_benchmark", root / "cli" / "benchmark.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            database = MarketDataDatabase(base / "m.db")
            database.initialize()
            rules = base / "rules.json"
            rules.write_text(json.dumps({"initial_capital_twd": 1_000_000_000, "version": "fixture"}), encoding="utf-8")
            VirtualAccountService(VirtualAccountRepository(base / "ledger", "cli-test")).initialize(rules, "cli-test", "2026-10-01T17:00:00+08:00")
            output = base / "out.json"
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = module.main([
                    "--repository", str(base / "ledger"), "--account-id", "cli-test", "--database", str(base / "m.db"),
                    "compare", "--rules", str(root / "config" / "decision_rules.json"), "--output", str(output),
                ])
            self.assertEqual(code, 0)
            body = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(body["status"], "ok")
            self.assertIsNone(body["start_date"])
            self.assertIn("unavailable", buffer.getvalue())
            self.assertTrue(all(item["status"] == "unavailable" for item in body["benchmarks"]))
            sys.modules.pop("cli_benchmark", None)
