import json
import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from etf_agent.core import content_sha256
from etf_agent.dashboard import build_performance_summary
from etf_agent.data import TradingCalendar, TradingCalendarError
from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository, VirtualAccountService
from etf_agent.ledger import ExecutionSimulator, LedgerError
from etf_agent.virtual_account.daily import DailyAccountRunner, OfficialCloseMarket, add_business_days, average_price
from test_virtual_account import prepare_and_decide


def create_prices(path, rows):
    """rows 為 (symbol, date, close, volume, source, fetched[, trade_value])；省略成交金額時均價等於收盤價。"""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS raw_payloads (id INTEGER PRIMARY KEY, source TEXT, fetched_at TEXT, sha256 TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_prices (
            symbol TEXT, trade_date TEXT, close_price TEXT, volume_shares INTEGER,
            source TEXT, fetched_at TEXT, raw_payload_id INTEGER, trade_value INTEGER
        );
        INSERT OR IGNORE INTO raw_payloads (id, sha256) VALUES (1, 'raw-sha');
        """
    )
    connection.executemany(
        "INSERT INTO daily_prices VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        [tuple(row[:6]) + ((row[6] if len(row) > 6 else int(Decimal(row[2]) * row[3])),) for row in rows],
    )
    connection.commit()
    connection.close()


class DailyAccountRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        rules = self.root / "rules.json"
        rules.write_text(json.dumps({"initial_capital_twd": 1_000_000_000, "version": "fixture-v1"}), encoding="utf-8")
        self.repository = VirtualAccountRepository(self.root / "accounts", "cup-test")
        self.service = VirtualAccountService(self.repository)
        self.service.initialize(rules, "cup-test", "2026-09-18T17:00:00+08:00")
        self.database = self.root / "prices.db"
        create_prices(self.database, [])

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, decisions=None):
        return DailyAccountRunner(self.repository, self.database, decisions or self.root / "decisions")

    def test_business_day_settlement_skips_weekend(self):
        self.assertEqual(add_business_days(date(2026, 9, 24), 2), date(2026, 9, 28))

    def test_decision_waits_for_official_close_and_blocks_new_prepare(self):
        snapshot, decisions = prepare_and_decide(self.service, self.root)
        # 只有決策 cutoff 前的收盤價，或只有 Yahoo 資料時都不能結算。
        create_prices(self.database, [
            ("2317.TW", "2026-09-18", "100", 5_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-18T14:00:00+00:00"),
            ("2317.TW", "2026-09-21", "101", 5_000_000, "YAHOO_FINANCE", "2026-09-21T10:00:00+00:00"),
        ])
        runner = self.runner(decisions)
        self.assertEqual(runner.settle()["status"], "waiting_for_close_data")
        later = dict(snapshot, decision_cutoff="2026-09-21T12:00:00+00:00")
        later_path = self.root / "later.json"
        later_path.write_text(json.dumps(later), encoding="utf-8")
        result = runner.run(later_path, "prepare-next", self.root / "account.json")
        self.assertEqual(result["prepare"]["status"], "blocked_by_pending_decision")
        self.assertEqual(self.repository.latest()["run_id"], "prepare-with-orders")

    def test_settles_at_next_trading_day_official_close(self):
        snapshot, decisions = prepare_and_decide(self.service, self.root)
        decision = json.loads((decisions / "decision-1" / "decision.json").read_text(encoding="utf-8"))
        order = decision["orders"][0]
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
            ("2317.TW", "2026-09-21", "999", 50_000_000, "YAHOO_FINANCE", "2026-09-21T12:00:00+00:00"),
            ("2330.TW", "2026-09-21", "500", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        result = self.runner(decisions).settle()
        self.assertEqual(result["status"], "settled")
        self.assertEqual(result["trade_date"], "2026-09-21")
        self.assertEqual(result["settlement_date"], "2026-09-23")
        state = self.repository.latest()["state"]
        self.assertEqual(state["provenance"]["type"], "close")
        self.assertEqual(state["provenance"]["trade_date"], "2026-09-21")
        position = next(row for row in state["positions"] if row["symbol"] == "2317.TW")
        self.assertEqual(position["shares"], order["shares"])
        self.assertEqual(position["close_price"], "120")
        # 以收盤價 120 成交：成本＝成交金額＋手續費（0.1425%，無條件進位到元）。
        gross = Decimal(120) * order["shares"]
        commission = (gross * Decimal("0.001425")).to_integral_value(rounding="ROUND_CEILING")
        self.assertEqual(Decimal(state["settled_cash"]), Decimal(1_000_000_000) - gross - commission)
        summary = build_performance_summary(self.repository)
        self.assertEqual(summary["today"]["trade_date"], "2026-09-21")
        self.assertEqual(summary["fees"]["commission"], "%s.00" % commission)
        self.assertEqual(self.runner(decisions).settle()["status"], "nothing_pending")

    def settle_with_calendar(self, rows):
        snapshot, decisions = prepare_and_decide(self.service, self.root)
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
            ("2330.TW", "2026-09-21", "500", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        calendar = TradingCalendar.from_ogd_facts(
            [{"kind": "calendar_event", "event_date": day, "name": name} for day, name in rows], "fixture-sha"
        )
        runner = DailyAccountRunner(self.repository, self.database, decisions, calendar=calendar)
        return runner.settle()

    def test_official_calendar_settlement_skips_holiday_and_counts_settlement_only_day(self):
        result = self.settle_with_calendar([("2026-09-22", "國定假日"), ("2026-09-23", "市場無交易，僅辦理結算交割作業")])
        self.assertEqual(result["status"], "settled")
        self.assertEqual(result["settlement_date"], "2026-09-24")
        self.assertEqual(result["settlement_calendar_basis"], "twse_ogd_calendar")
        self.assertEqual(result["settlement_calendar_source_sha256"], "fixture-sha")

    def test_official_calendar_rejects_trade_on_closed_day(self):
        with self.assertRaisesRegex(VirtualAccountError, "不是交易日"):
            self.settle_with_calendar([("2026-09-21", "國定假日")])
        self.assertEqual(self.repository.latest()["run_id"], "prepare-with-orders")
        self.assertFalse((self.repository.root / "inputs" / "close-2026-09-21").exists())

    def test_official_calendar_rejects_uncovered_year(self):
        with self.assertRaisesRegex(TradingCalendarError, "未涵蓋 2026"):
            self.settle_with_calendar([("2027-01-01", "開國紀念日")])

    def test_fills_in_full_at_official_average_price_and_values_at_close(self):
        snapshot, decisions = prepare_and_decide(self.service, self.root)
        order = json.loads((decisions / "decision-1" / "decision.json").read_text(encoding="utf-8"))["orders"][0]
        # 當日只成交 1 張，仍全部成交；成交均價 605,000 ÷ 5,000 = 121，收盤 120。
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 5_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00", 605_000),
            ("2330.TW", "2026-09-21", "500", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        self.assertLess(5_000, order["shares"])
        result = self.runner(decisions).settle()
        self.assertEqual((result["fills"], result["unfilled"]), (1, 0))
        market = json.loads((self.repository.root / "inputs" / "close-2026-09-21" / "execution_market.json").read_text(encoding="utf-8"))
        self.assertEqual(market["price_rule"], "official_average_price")
        self.assertEqual(market["liquidity_cap"], "none")
        self.assertEqual(market["quotes"]["2317.TW"]["execution_price"], "121.00")
        self.assertIsNone(market["quotes"]["2317.TW"]["available_lots"])
        state = self.repository.latest()["state"]
        position = next(row for row in state["positions"] if row["symbol"] == "2317.TW")
        self.assertEqual(position["shares"], order["shares"])
        self.assertEqual(position["close_price"], "120")
        gross = Decimal(121) * order["shares"]
        commission = (gross * Decimal("0.001425")).to_integral_value(rounding="ROUND_CEILING")
        self.assertEqual(Decimal(state["settled_cash"]), Decimal(1_000_000_000) - gross - commission)

    def test_symbol_without_trades_has_no_average_price_and_is_not_filled(self):
        snapshot, decisions = prepare_and_decide(self.service, self.root)
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 0, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00", 0),
            ("2330.TW", "2026-09-21", "500", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        result = self.runner(decisions).settle()
        self.assertEqual((result["fills"], result["unfilled"]), (0, 1))
        transition = json.loads((self.repository.root / "runs" / result["run_id"] / "transition.json").read_text(encoding="utf-8"))
        self.assertEqual(transition["execution"]["unfilled_orders"][0]["reason"], "NOT_TRADABLE")

    def test_average_price_rounds_half_up_to_cent(self):
        self.assertEqual(average_price(1_000_001, 3_000), Decimal("333.33"))
        self.assertEqual(average_price(1_000_015, 1_000), Decimal("1000.02"))
        self.assertIsNone(average_price(0, 1_000))
        self.assertIsNone(average_price(1_000, 0))

    def test_simulator_liquidity_cap_is_explicit(self):
        simulator = ExecutionSimulator({"lot_size": 1000, "commission_rate": "0.001425", "minimum_commission": "0",
                                        "sell_tax_rate": "0.003", "reuse_sell_proceeds": True})
        decision = {"status": "approved", "orders": [{"symbol": "2330.TW", "side": "buy", "shares": 3000}]}
        decision["content_sha256"] = content_sha256(decision)
        def market(lots):
            return {"price_basis": "unadjusted", "available_at": "2026-09-21T13:30:00+08:00",
                    "quotes": {"2330.TW": {"tradable": True, "available_lots": lots, "execution_price": "100"}}}
        uncapped = simulator.run(decision, market(None), Decimal(10_000_000), "2026-09-21T13:30:00+08:00")
        self.assertEqual(uncapped["fills"][0]["shares"], 3000)
        capped = simulator.run(decision, market(1), Decimal(10_000_000), "2026-09-21T13:30:00+08:00")
        self.assertEqual(capped["unfilled_orders"][0]["unfilled_shares"], 2000)
        # 無量能上限時仍受買力限制。
        cash_limited = simulator.run(decision, market(None), Decimal(250_000), "2026-09-21T13:30:00+08:00")
        self.assertEqual(cash_limited["fills"][0]["shares"], 2000)
        for invalid in (-1, "3", True):
            with self.assertRaises(LedgerError):
                simulator.run(decision, market(invalid), Decimal(10_000_000), "2026-09-21T13:30:00+08:00")

    def test_prepare_without_decision_carries_forward(self):
        snapshot = {
            "snapshot_id": "snapshot-1", "decision_cutoff": "2026-09-21T12:00:00+00:00",
            "usable": True, "latest_trade_date": "2026-09-21",
            "latest_prices": [{"symbol": "2330.TW", "analysis_close_price": "100", "close_price": "100"}],
        }
        path = self.root / "snapshot.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        first = self.runner().run(path, "prepare-1", self.root / "account.json")
        self.assertEqual(first["settle"]["status"], "nothing_pending")
        self.assertEqual(first["prepare"]["status"], "prepared")
        snapshot.update(snapshot_id="snapshot-2", decision_cutoff="2026-09-22T12:00:00+00:00", latest_trade_date="2026-09-22")
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        second = self.runner().run(path, "prepare-2", self.root / "account.json")
        self.assertEqual(second["settle"]["status"], "no_decision")
        self.assertEqual(self.repository.latest()["run_id"], "prepare-2")
        history = build_performance_summary(self.repository)["history"]
        self.assertEqual([row["trade_date"] for row in history], ["2026-09-18", "2026-09-21", "2026-09-22"])

    def test_same_snapshot_reuses_state_and_restores_output(self):
        snapshot = {"snapshot_id": "same", "decision_cutoff": "2026-09-21T00:55:00+00:00",
                    "usable": True, "latest_trade_date": "2026-09-18",
                    "latest_prices": [{"symbol": "2330.TW", "close_price": "100"}]}
        path, output = self.root / "snapshot.json", self.root / "account.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        self.runner().run(path, "prepare-first", output)
        expected = output.read_bytes()
        output.unlink()
        repeated = self.runner().run(path, "prepare-second", output)
        self.assertEqual(repeated["prepare"]["status"], "reused")
        self.assertEqual(output.read_bytes(), expected)
        self.assertEqual(self.repository.latest()["run_id"], "prepare-first")

    def test_prices_captured_after_observation_are_not_used(self):
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 1000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        market = OfficialCloseMarket(self.database)
        self.assertIsNone(market.build("2026-09-21", ["2317.TW"], "2026-09-21T08:00:00+00:00"))
        self.assertIsNotNone(market.build("2026-09-21", ["2317.TW"], "2026-09-21T12:00:00+00:00"))

    def test_latest_available_price_version_is_independent_of_insert_order(self):
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "121", 1000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
            ("2317.TW", "2026-09-21", "120", 1000, "TWSE_STOCK_DAY_ALL", "2026-09-21T11:00:00+00:00"),
        ])
        market = OfficialCloseMarket(self.database)
        self.assertEqual(market.quotes("2026-09-21")["2317.TW"]["close_price"], "121")
        self.assertEqual(market.quotes("2026-09-21", "2026-09-21T11:30:00+00:00")["2317.TW"]["close_price"], "120")

    def test_invalid_next_snapshot_does_not_settle_pending_decision(self):
        snapshot, decisions = prepare_and_decide(self.service, self.root)
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 1000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        snapshot.update(decision_cutoff="2026-09-22T00:55:00+00:00", latest_prices=[])
        path = self.root / "invalid.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.runner(decisions).run(path, "bad-prepare", self.root / "output.json")
        self.assertEqual(self.repository.latest()["run_id"], "prepare-with-orders")

    def test_close_market_waits_for_all_required_symbols(self):
        market = OfficialCloseMarket(self.database)
        self.assertIsNone(market.build("2026-09-21", ["2330.TW"]))
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 1000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        self.assertIsNone(market.build("2026-09-21", ["2330.TW"]))
        execution, close = market.build("2026-09-21", ["2317.TW"])
        self.assertEqual(execution["execution_at"], "2026-09-21T13:30:00+08:00")


if __name__ == "__main__":
    unittest.main()
