import json
import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from etf_agent.dashboard import build_performance_summary
from etf_agent.virtual_account import VirtualAccountRepository, VirtualAccountService
from etf_agent.virtual_account.daily import DailyAccountRunner, OfficialCloseMarket, add_business_days
from test_virtual_account import prepare_and_decide


def create_prices(path, rows):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS raw_payloads (id INTEGER PRIMARY KEY, source TEXT, fetched_at TEXT, sha256 TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_prices (
            symbol TEXT, trade_date TEXT, close_price TEXT, volume_shares INTEGER,
            source TEXT, fetched_at TEXT, raw_payload_id INTEGER
        );
        INSERT OR IGNORE INTO raw_payloads (id, sha256) VALUES (1, 'raw-sha');
        """
    )
    connection.executemany(
        "INSERT INTO daily_prices VALUES (?, ?, ?, ?, ?, ?, 1)", rows,
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

    def test_prepare_without_decision_carries_forward(self):
        snapshot = {
            "snapshot_id": "snapshot-1", "decision_cutoff": "2026-09-21T12:00:00+00:00",
            "usable": True, "latest_trade_date": "2026-09-21",
            "latest_prices": [{"symbol": "2330.TW", "analysis_close_price": "100"}],
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

    def test_close_market_requires_published_market_and_omits_missing_symbols(self):
        market = OfficialCloseMarket(self.database)
        self.assertIsNone(market.build("2026-09-21", ["2330.TW"]))
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 1000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        execution, close = market.build("2026-09-21", ["2330.TW"])
        self.assertNotIn("2330.TW", close["quotes"])
        self.assertEqual(execution["execution_at"], "2026-09-21T13:30:00+08:00")


if __name__ == "__main__":
    unittest.main()
