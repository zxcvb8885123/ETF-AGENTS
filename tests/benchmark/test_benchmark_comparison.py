"""帳戶對 0050／交易池等權買入持有的基準比較：正常流程、前視拒絕、缺價 unavailable、費稅與授權。"""

import json
import tempfile
import unittest
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from etf_agent.benchmark import (
    BenchmarkError,
    BenchmarkPriceRepository,
    FeeSchedule,
    build_benchmark_comparison,
    max_drawdown,
    relative_drawdown,
    whole_lot_buy,
)
from etf_agent.dashboard.app import create_app
from etf_agent.data import DailyPrice, Instrument, MarketDataDatabase
from etf_agent.ledger import ExecutionSimulator
from etf_agent.virtual_account import VirtualAccountRepository, VirtualAccountService

CAPITAL = Decimal("1000000000")
RULES = {
    "version": "rules-test", "lot_size": 1000, "commission_rate": "0.001425",
    "sell_tax_rate": "0.003", "minimum_commission": "20",
}
UNIVERSE_CSV = "symbol,name,market,source_date\n2330,台積電,TWSE,2026-09-14\n6488,環球晶,TPEX,2026-09-14\n"
TSMC = Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14")
GW = Instrument("6488.TWO", "6488", "環球晶", "TPEX", "2026-09-14")
ETF = Instrument("0050.TW", "0050", "元大台灣50", "TWSE", "2026-09-11")
OPENED_AT = "2026-10-01T17:00:00+08:00"  # 週三收盤後開帳 → 10-02 才可成交


def _benchmarks_config(approved="approved"):
    return {
        "version": "test",
        "benchmarks": [
            {"benchmark_id": "0050_buy_and_hold", "label": "0050 買入持有", "kind": "single_instrument",
             "symbol": "0050", "code": "0050", "name": "元大台灣50", "market": "TWSE", "source": "TWSE_STOCK_DAY",
             "license_status": approved, "evidence": "test"},
            {"benchmark_id": "universe_equal_weight", "label": "等權", "kind": "universe_equal_weight",
             "universe": "universe.csv", "license_status": "approved"},
            {"benchmark_id": "yahoo_0050", "label": "Yahoo 0050", "kind": "single_instrument",
             "symbol": "0050", "name": "元大台灣50", "market": "TWSE", "source": "YAHOO_FINANCE",
             "license_status": "research_only"},
        ],
    }


def _price(instrument, trade_date, close, trade_value, volume):
    return DailyPrice(
        symbol=instrument.symbol, code=instrument.code, name=instrument.name, trade_date=trade_date,
        open_price=None, high_price=None, low_price=None, close_price=Decimal(close), change=None,
        volume_shares=volume, trade_value=trade_value, transactions=1,
    )


def _commission(gross):
    return max(gross * Decimal("0.001425"), Decimal("20")).quantize(Decimal("1"), rounding=ROUND_CEILING)


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "universe.csv").write_text(UNIVERSE_CSV, encoding="utf-8")
        self.rules = self.root / "decision_rules.json"
        self.rules.write_text(json.dumps(RULES), encoding="utf-8")
        self.benchmarks = self.root / "benchmarks.json"
        self.benchmarks.write_text(json.dumps(_benchmarks_config(), ensure_ascii=False), encoding="utf-8")
        self.db_path = self.root / "market.db"
        self.database = MarketDataDatabase(self.db_path)
        self.database.initialize()
        with self.database.connect() as connection:
            self.database.upsert_instruments(connection, [TSMC, GW], True)
            self.database.upsert_instruments(connection, [ETF], False)
        self.repository = VirtualAccountRepository(self.root / "ledger", "cup-test")
        self.service = VirtualAccountService(self.repository)
        competition = self.root / "competition_rules.json"
        competition.write_text(json.dumps({"initial_capital_twd": 1_000_000_000, "version": "fixture"}), encoding="utf-8")
        self.service.initialize(competition, "cup-test", OPENED_AT)

    def store(self, prices, source, fetched_at):
        run_id = "run-%s-%s" % (source, fetched_at)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO collection_runs(run_id, source, started_at, status) VALUES (?, ?, ?, 'success')",
                (run_id, source, fetched_at),
            )
            payload_id = self.database.insert_raw_payload(connection, run_id, source, "fixture://%s" % source, fetched_at, "{}")
            self.database.store_prices(connection, prices, source, fetched_at, run_id, payload_id)

    def store_day_one(self):
        # 10-02：2330 均價 110、6488 均價 200、0050 均價 100.50；收盤 110／200／100。
        self.store([_price(TSMC, "2026-10-02", "110", 1_100_000_000, 10_000_000)], "TWSE_STOCK_DAY", "2026-10-02T17:00:00+08:00")
        self.store([_price(GW, "2026-10-02", "200", 2_000_000_000, 10_000_000)], "TPEX_TRADING_STOCK", "2026-10-02T17:00:00+08:00")
        self.store([_price(ETF, "2026-10-02", "100", 1_005_000_000, 10_000_000)], "TWSE_STOCK_DAY", "2026-10-02T17:00:00+08:00")

    def store_day_two(self):
        self.store([_price(TSMC, "2026-10-03", "105", 1_050_000_000, 10_000_000)], "TWSE_STOCK_DAY", "2026-10-03T17:00:00+08:00")
        self.store([_price(GW, "2026-10-03", "220", 2_200_000_000, 10_000_000)], "TPEX_TRADING_STOCK", "2026-10-03T17:00:00+08:00")
        self.store([_price(ETF, "2026-10-03", "102", 1_020_000_000, 10_000_000)], "TWSE_STOCK_DAY", "2026-10-03T17:00:00+08:00")

    def close_state(self, run_id, as_of, settled, price):
        parent = self.repository.latest()
        shares = 10_000
        value = Decimal(price) * shares
        state = VirtualAccountService._state(
            account_id="cup-test", sequence=int(parent["state"]["sequence"]) + 1,
            parent_state_id=parent["state"]["state_id"], as_of=as_of,
            settled_cash=Decimal(settled), unsettled_cash=Decimal(0), pending_settlements=[],
            positions=[{"symbol": "2330.TW", "shares": shares, "cost_basis": "100.1425", "close_price": price, "market_value": str(value)}],
            nav=Decimal(settled) + value,
            provenance={"type": "close", "parent_run_id": parent["run_id"], "trade_date": as_of[:10]},
        )
        self.repository.save(run_id, {"state": state, "transition": {"execution": {"fills": []}}}, state)

    def build(self, as_of=None):
        return build_benchmark_comparison(self.repository, self.db_path, self.rules, self.benchmarks, self.root, as_of)


class ComparisonTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.store_day_one()
        self.store_day_two()
        # 帳戶：10-02 以 100 元買 10 張 2330（收盤 110），10-03 收盤 105。
        # 帳本 close 狀態的 as_of 是日終行情可得時間，晚於官方行情抓取時間。
        self.close_state("close-1", "2026-10-02T17:30:00+08:00", "998998575", "110")
        self.close_state("close-2", "2026-10-03T17:30:00+08:00", "998998575", "105")

    def test_buy_and_hold_benchmarks_follow_official_prices_and_fees(self):
        result = self.build()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["start_date"], "2026-10-02")
        self.assertEqual(result["trading_days"], 2)
        self.assertEqual(result["fees"]["commission_rate"], "0.001425")
        by_id = {item["benchmark_id"]: item for item in result["benchmarks"]}

        etf = by_id["0050_buy_and_hold"]
        self.assertEqual(etf["status"], "ok")
        self.assertEqual(etf["execution"]["price"], "100.50")
        # 9936 張：998,568,000 + 手續費 1,422,960 = 999,990,960 ≤ 10 億；9937 張會超過本金。
        self.assertEqual(etf["positions"], {"0050.TW": 9_936_000})
        self.assertEqual(etf["commission"], "1422960.00")
        self.assertEqual(_commission(Decimal("998568000")), Decimal("1422960"))
        cash = CAPITAL - Decimal("998568000") - Decimal("1422960")
        self.assertEqual(etf["cash"], "9040.00")
        self.assertEqual([point["trade_date"] for point in etf["series"]], ["2026-10-01", "2026-10-02", "2026-10-03"])
        self.assertEqual(etf["series"][1]["nav"], "993609040.00")
        self.assertEqual(etf["latest"]["nav"], "1013481040.00")
        self.assertEqual(etf["latest"]["cumulative_return"], "0.013481")
        self.assertEqual(etf["max_drawdown"], "0.006391")
        comparison = etf["comparison"]
        self.assertEqual(comparison["status"], "ok")
        self.assertEqual(comparison["trade_date"], "2026-10-03")
        self.assertEqual(comparison["aligned_days"], 3)
        self.assertEqual(comparison["account_cumulative_return"], "0.000049")
        self.assertEqual(comparison["excess_return"], "-0.013432")
        expected_relative = relative_drawdown(
            [CAPITAL, Decimal("1000098575"), Decimal("1000048575")],
            [CAPITAL, cash + 9_936_000 * 100, cash + 9_936_000 * 102],
        ).quantize(Decimal("0.000001"))
        self.assertEqual(comparison["relative_drawdown"], str(expected_relative))

        equal = by_id["universe_equal_weight"]
        self.assertEqual(equal["status"], "ok")
        self.assertEqual(equal["budget_per_symbol"], "500000000.00")
        self.assertEqual(equal["positions"], {"2330.TW": 4_538_000, "6488.TWO": 2_496_000})
        equal_cash = CAPITAL - Decimal("499180000") - _commission(Decimal("499180000")) - Decimal("499200000") - _commission(Decimal("499200000"))
        self.assertEqual(equal["cash"], "%s.00" % equal_cash)
        self.assertEqual(equal["latest"]["nav"], "%s.00" % (equal_cash + 4_538_000 * 105 + 2_496_000 * 220))
        self.assertEqual(result["account"]["cumulative_return"], "0.000049")
        self.assertEqual(result["account"]["max_drawdown"], "0.000050")

        yahoo = by_id["yahoo_0050"]
        self.assertEqual(yahoo["status"], "unavailable")
        self.assertIn("license_status=research_only", yahoo["reason"])

    def test_rows_fetched_after_as_of_are_invisible(self):
        # 10-03 的行情在 10-03 17:00 才抓到；as-of 10-03 16:00 只能看到 10-02。
        result = self.build("2026-10-03T16:00:00+08:00")
        etf = next(item for item in result["benchmarks"] if item["benchmark_id"] == "0050_buy_and_hold")
        self.assertEqual([point["trade_date"] for point in etf["series"]], ["2026-10-01", "2026-10-02"])
        self.assertEqual(etf["latest"]["trade_date"], "2026-10-02")
        self.assertEqual(etf["comparison"]["trade_date"], "2026-10-02")
        # as-of 早於起始日資料可得時間：沒有任何可用交易日。
        earlier = self.build("2026-10-02T16:00:00+08:00")
        self.assertIsNone(earlier["start_date"])
        self.assertTrue(all(item["status"] == "unavailable" for item in earlier["benchmarks"]))
        for item in (item for item in earlier["benchmarks"] if item["license_status"] == "approved"):
            self.assertIn("基準從之後第一個可下單的交易日起算", item["reason"])
            self.assertIn("截至 2026-10-02 16:00（台北）", item["reason"])

    def test_later_version_is_not_used_before_it_was_fetched(self):
        # 10-02 的 0050 收盤價在 10-04 被來源更正為 101；as-of 10-03 仍用當時的 100。
        self.store([_price(ETF, "2026-10-02", "101", 1_005_000_000, 10_000_000)], "TWSE_STOCK_DAY", "2026-10-04T10:00:00+08:00")
        before = self.build("2026-10-03T16:00:00+08:00")
        etf = next(item for item in before["benchmarks"] if item["benchmark_id"] == "0050_buy_and_hold")
        self.assertEqual(Decimal(etf["series"][1]["nav"]), Decimal(etf["cash"]) + 9_936_000 * 100)
        after = self.build("2026-10-04T12:00:00+08:00")
        etf_after = next(item for item in after["benchmarks"] if item["benchmark_id"] == "0050_buy_and_hold")
        self.assertEqual(Decimal(etf_after["series"][1]["nav"]), Decimal(etf_after["cash"]) + 9_936_000 * 101)

    def test_as_of_before_account_open_is_rejected(self):
        with self.assertRaises(BenchmarkError):
            self.build("2026-09-30T10:00:00+08:00")
        with self.assertRaises(BenchmarkError):
            self.build("2026-10-03T16:00:00")

    def test_dashboard_endpoint_serves_comparison_and_degrades_without_database(self):
        client = TestClient(create_app(self.root / "ledger", "cup-test", database=self.db_path, rules_path=self.rules, benchmarks_path=self.benchmarks, root=self.root))
        body = client.get("/api/benchmarks").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(len(body["benchmarks"]), 3)
        bare = TestClient(create_app(self.root / "ledger", "cup-test")).get("/api/benchmarks").json()
        self.assertEqual(bare["status"], "unavailable")
        missing_db = TestClient(create_app(self.root / "ledger", "cup-test", database=self.root / "none.db", rules_path=self.rules, benchmarks_path=self.benchmarks, root=self.root))
        self.assertEqual(missing_db.get("/api/benchmarks").json()["status"], "unavailable")


class MissingDataTests(_Fixture):
    def test_missing_universe_symbol_on_start_day_makes_equal_weight_unavailable(self):
        self.store([_price(TSMC, "2026-10-02", "110", 1_100_000_000, 10_000_000)], "TWSE_STOCK_DAY", "2026-10-02T17:00:00+08:00")
        self.store([_price(ETF, "2026-10-02", "100", 1_005_000_000, 10_000_000)], "TWSE_STOCK_DAY", "2026-10-02T17:00:00+08:00")
        self.close_state("close-1", "2026-10-02T17:30:00+08:00", "998998575", "110")
        result = self.build()
        by_id = {item["benchmark_id"]: item for item in result["benchmarks"]}
        self.assertEqual(by_id["universe_equal_weight"]["status"], "unavailable")
        self.assertIn("6488.TWO", by_id["universe_equal_weight"]["reason"])
        self.assertEqual(by_id["0050_buy_and_hold"]["status"], "ok")

    def test_missing_benchmark_price_on_start_day_is_unavailable(self):
        self.store([_price(TSMC, "2026-10-02", "110", 1_100_000_000, 10_000_000)], "TWSE_STOCK_DAY", "2026-10-02T17:00:00+08:00")
        self.store([_price(GW, "2026-10-02", "200", 2_000_000_000, 10_000_000)], "TPEX_TRADING_STOCK", "2026-10-02T17:00:00+08:00")
        self.close_state("close-1", "2026-10-02T17:30:00+08:00", "998998575", "110")
        result = self.build()
        etf = next(item for item in result["benchmarks"] if item["benchmark_id"] == "0050_buy_and_hold")
        self.assertEqual(etf["status"], "unavailable")
        self.assertIn("0050.TW", etf["reason"])

    def test_zero_volume_start_day_has_no_average_price(self):
        self.store([_price(TSMC, "2026-10-02", "110", 1_100_000_000, 10_000_000)], "TWSE_STOCK_DAY", "2026-10-02T17:00:00+08:00")
        self.store([_price(GW, "2026-10-02", "200", 2_000_000_000, 10_000_000)], "TPEX_TRADING_STOCK", "2026-10-02T17:00:00+08:00")
        self.store([_price(ETF, "2026-10-02", "100", 0, 0)], "TWSE_STOCK_DAY", "2026-10-02T17:00:00+08:00")
        self.close_state("close-1", "2026-10-02T17:30:00+08:00", "998998575", "110")
        etf = next(item for item in self.build()["benchmarks"] if item["benchmark_id"] == "0050_buy_and_hold")
        self.assertEqual(etf["status"], "unavailable")
        self.assertIn("成交均價", etf["reason"])

    def test_missing_close_on_a_later_day_leaves_that_day_unvalued(self):
        self.store_day_one()
        self.store([_price(TSMC, "2026-10-03", "105", 1_050_000_000, 10_000_000)], "TWSE_STOCK_DAY", "2026-10-03T17:00:00+08:00")
        self.store([_price(GW, "2026-10-03", "220", 2_200_000_000, 10_000_000)], "TPEX_TRADING_STOCK", "2026-10-03T17:00:00+08:00")
        self.close_state("close-1", "2026-10-02T17:30:00+08:00", "998998575", "110")
        self.close_state("close-2", "2026-10-03T17:30:00+08:00", "998998575", "105")
        etf = next(item for item in self.build()["benchmarks"] if item["benchmark_id"] == "0050_buy_and_hold")
        self.assertEqual(etf["status"], "ok")
        self.assertIsNone(etf["series"][2]["nav"])
        self.assertIn("0050.TW", etf["series"][2]["reason"])
        self.assertEqual(etf["latest"]["trade_date"], "2026-10-02")
        self.assertEqual(etf["unvalued_days"], 1)
        self.assertEqual(etf["comparison"]["trade_date"], "2026-10-02")

    def test_genesis_only_account_has_no_comparison(self):
        self.store_day_one()
        result = self.build("2026-10-02T18:00:00+08:00")
        etf = next(item for item in result["benchmarks"] if item["benchmark_id"] == "0050_buy_and_hold")
        self.assertEqual(etf["status"], "ok")
        self.assertEqual(etf["latest"]["trade_date"], "2026-10-02")
        self.assertEqual(etf["comparison"]["status"], "unavailable")
        self.assertEqual(etf["comparison"]["aligned_days"], 1)

    def test_no_official_prices_after_open_is_unavailable(self):
        result = self.build()
        self.assertIsNone(result["start_date"])
        self.assertTrue(all(item["status"] == "unavailable" for item in result["benchmarks"]))
        self.assertIn("開帳於 2026-10-01 17:00（台北，已過當日 08:55 交件期限）", result["benchmarks"][0]["reason"])

    def test_uninitialized_account(self):
        empty = VirtualAccountRepository(self.root / "ledger", "nobody")
        result = build_benchmark_comparison(empty, self.db_path, self.rules, self.benchmarks, self.root)
        self.assertEqual(result["status"], "uninitialized")

    def test_yahoo_rows_are_never_used(self):
        self.store([_price(TSMC, "2026-10-02", "110", 1_100_000_000, 10_000_000)], "TWSE_STOCK_DAY", "2026-10-02T17:00:00+08:00")
        self.store([_price(GW, "2026-10-02", "200", 2_000_000_000, 10_000_000)], "TPEX_TRADING_STOCK", "2026-10-02T17:00:00+08:00")
        self.store([_price(ETF, "2026-10-02", "100", 1_005_000_000, 10_000_000)], "YAHOO_FINANCE", "2026-10-02T17:00:00+08:00")
        self.close_state("close-1", "2026-10-02T17:30:00+08:00", "998998575", "110")
        etf = next(item for item in self.build()["benchmarks"] if item["benchmark_id"] == "0050_buy_and_hold")
        self.assertEqual(etf["status"], "unavailable")


class FeeAndMetricTests(unittest.TestCase):
    def setUp(self):
        self.simulator = ExecutionSimulator({"lot_size": 1000, "commission_rate": "0.001425", "sell_tax_rate": "0.003", "minimum_commission": "20", "reuse_sell_proceeds": False})

    def test_whole_lot_buy_keeps_commission_within_budget(self):
        fill = whole_lot_buy(self.simulator, "0050.TW", CAPITAL, Decimal("100.50"))
        self.assertEqual(fill["lots"], 9936)
        self.assertEqual(fill["commission"], "1422960")  # ceil(998,568,000 × 0.001425)
        self.assertEqual(fill["net_cash_impact"], "-999990960")
        self.assertLessEqual(-Decimal(fill["net_cash_impact"]), CAPITAL)
        # 多一張就超過本金。
        more = self.simulator.fill("0050.TW", "buy", 9_937_000, Decimal("100.50"))
        self.assertGreater(-Decimal(more["net_cash_impact"]), CAPITAL)

    def test_minimum_commission_and_unaffordable_cases(self):
        fill = whole_lot_buy(self.simulator, "X", Decimal("30000"), Decimal("10"))
        self.assertEqual(fill["lots"], 2)
        self.assertEqual(fill["commission"], "29")
        tiny = whole_lot_buy(self.simulator, "X", Decimal("10020"), Decimal("10"))
        self.assertEqual(tiny["lots"], 1)
        self.assertEqual(tiny["commission"], "20")  # 14.25 → 最低 20
        self.assertEqual(tiny["net_cash_impact"], "-10020")
        # 預算剛好等於一張股價但付不起最低手續費：一張都不買。
        self.assertIsNone(whole_lot_buy(self.simulator, "X", Decimal("10000"), Decimal("10")))
        self.assertIsNone(whole_lot_buy(self.simulator, "X", Decimal("100000"), Decimal("100.5")))

    def test_drawdowns(self):
        self.assertIsNone(max_drawdown([Decimal(100)]))
        self.assertEqual(max_drawdown([Decimal(100), Decimal(120), Decimal(90), Decimal(130)]), Decimal("0.25"))
        # 比值 1 → 1.1 → 0.99：相對基準自峰值回落 10%。
        self.assertEqual(relative_drawdown([Decimal(100), Decimal(110), Decimal(99)], [Decimal(100), Decimal(100), Decimal(100)]), Decimal("0.1"))
        with self.assertRaises(BenchmarkError):
            relative_drawdown([Decimal(1)], [Decimal(1), Decimal(2)])

    def test_fee_schedule_rejects_bad_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rules.json"
            path.write_text(json.dumps({"lot_size": 0, "commission_rate": "0.001425", "sell_tax_rate": "0.003", "minimum_commission": "20"}), encoding="utf-8")
            with self.assertRaises(BenchmarkError):
                FeeSchedule.from_rules(path)
            path.write_text(json.dumps({"lot_size": 1000, "commission_rate": "NaN", "sell_tax_rate": "0.003", "minimum_commission": "20"}), encoding="utf-8")
            with self.assertRaises(BenchmarkError):
                FeeSchedule.from_rules(path)
            path.write_text(json.dumps(RULES), encoding="utf-8")
            self.assertEqual(FeeSchedule.from_rules(path).assumptions()["minimum_commission"], "20")

    def test_repository_requires_aware_as_of(self):
        with tempfile.TemporaryDirectory() as temp:
            database = MarketDataDatabase(Path(temp) / "m.db")
            database.initialize()
            from datetime import datetime

            with self.assertRaises(BenchmarkError):
                BenchmarkPriceRepository(Path(temp) / "m.db").official_prices(["0050.TW"], "2026-10-01", "2026-10-02", datetime(2026, 10, 2))
            with self.assertRaises(BenchmarkError):
                BenchmarkPriceRepository(Path(temp) / "missing.db").official_prices(["0050.TW"], "2026-10-01", "2026-10-02", datetime(2026, 10, 2).astimezone())
