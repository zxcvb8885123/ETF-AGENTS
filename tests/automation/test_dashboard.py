import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from etf_agent.dashboard import PerformanceError, build_performance_summary
from etf_agent.dashboard.app import create_app
from etf_agent.virtual_account import VirtualAccountRepository, VirtualAccountService


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.rules = self.root / "rules.json"
        self.rules.write_text(json.dumps({"initial_capital_twd": 1_000_000_000, "version": "fixture-v1"}), encoding="utf-8")
        self.repository = VirtualAccountRepository(self.root / "accounts", "cup-test")
        self.service = VirtualAccountService(self.repository)

    def tearDown(self):
        self.temp.cleanup()

    def _close(self, run_id, as_of, settled, price, commission, fills=None):
        parent = self.repository.latest()
        shares = 10_000
        cost = Decimal("100.1425")
        value = Decimal(price) * shares
        state = VirtualAccountService._state(
            account_id="cup-test", sequence=int(parent["state"]["sequence"]) + 1,
            parent_state_id=parent["state"]["state_id"], as_of=as_of,
            settled_cash=Decimal(settled), unsettled_cash=Decimal(0), pending_settlements=[],
            positions=[{"symbol": "2330.TW", "shares": shares, "cost_basis": str(cost), "close_price": price, "market_value": str(value)}],
            nav=Decimal(settled) + value,
            provenance={"type": "close", "parent_run_id": parent["run_id"], "trade_date": as_of[:10]},
        )
        if fills is None:
            fills = [{"symbol": "2330.TW", "side": "buy", "shares": shares, "commission": commission, "tax": "0"}] if commission else []
        transition = {"execution": {"fills": fills}}
        self.repository.save(run_id, {"state": state, "transition": transition}, state)

    def test_uninitialized_account_reports_status(self):
        self.assertEqual(build_performance_summary(self.repository)["status"], "uninitialized")

    def test_genesis_only_has_capital_and_no_daily_return(self):
        self.service.initialize(self.rules, "cup-test", "2026-09-18T17:00:00+08:00")
        summary = build_performance_summary(self.repository)
        self.assertEqual(summary["initial_capital"], "1000000000.00")
        self.assertEqual(summary["nav"], "1000000000.00")
        self.assertEqual(summary["total_return"], "0.000000")
        self.assertIsNone(summary["today"])
        self.assertEqual(len(summary["history"]), 1)

    def test_daily_and_total_returns_follow_close_states(self):
        self.service.initialize(self.rules, "cup-test", "2026-09-18T17:00:00+08:00")
        # 第一日：以 100 元買 10 張，收盤 110。
        self._close("close-1", "2026-09-21T16:00:00+08:00", "998998575", "110", "1425")
        # 第二日：收盤跌至 105。
        self._close("close-2", "2026-09-22T16:00:00+08:00", "998998575", "105", None)
        summary = build_performance_summary(self.repository)
        self.assertEqual(summary["nav"], "1000048575.00")
        self.assertEqual(summary["total_pnl"], "48575.00")
        self.assertEqual(summary["total_return"], "0.000049")
        self.assertEqual(summary["today"]["pnl"], "-50000.00")
        self.assertEqual(summary["today"]["trade_date"], "2026-09-22")
        self.assertEqual(summary["history"][1]["daily_pnl"], "98575.00")
        self.assertEqual(summary["fees"], {"commission": "1425.00", "tax": "0.00", "fill_count": 1})
        position = summary["positions"][0]
        self.assertEqual(position["lots"], 10)
        self.assertEqual(position["unrealized_pnl"], "48575.00")

    def test_trades_are_grouped_by_day_and_sells_carry_realized_pnl(self):
        self.service.initialize(self.rules, "cup-test", "2026-09-18T17:00:00+08:00")
        self._close("close-1", "2026-09-21T16:00:00+08:00", "998998575", "110", "1425")
        sell = {"symbol": "2330.TW", "side": "sell", "shares": 10_000, "execution_price": "110.00",
                "gross_amount": "1100000", "commission": "1568", "tax": "3300"}
        self._close("close-2", "2026-09-22T16:00:00+08:00", "998998575", "105", None, fills=[sell])
        summary = build_performance_summary(self.repository)
        self.assertEqual([day["trade_date"] for day in summary["trades"]], ["2026-09-21", "2026-09-22"])
        self.assertEqual(summary["trades"][0]["fills"][0]["side"], "buy")
        self.assertIsNone(summary["trades"][0]["fills"][0]["realized_pnl"])
        # 1,100,000 − 1,568 − 3,300 − 10,000 × 100.1425
        self.assertEqual(summary["trades"][1]["fills"][0]["realized_pnl"], "93707.00")
        self.assertEqual(summary["realized_pnl"], "93707.00")
        self.assertIsNone(summary["pending_orders"])

    def test_pending_orders_mark_final_version_time_and_count(self):
        from etf_agent.dashboard.performance import _pending_orders

        decisions = self.root / "decisions"
        run = decisions / "2026-10-26" / "decision-20261026T230000Z"
        run.mkdir(parents=True)
        (run / "decision_input.json").write_text(json.dumps({"account_snapshot": {"id": 2}, "trading_status_bundle": {
            "target_session": {"end": "2026-10-27T13:30:00+08:00"}}}), encoding="utf-8")
        (run / "decision.json").write_text(json.dumps({"status": "approved", "orders": []}), encoding="utf-8")
        versions = self.root / "competition_d_plan" / "versions" / "2026-10-27"
        versions.mkdir(parents=True)
        for stamp in ("20261026-210000", "20261027-070000"):
            (versions / ("D-Plan_TEAM_2026-10-27_%s.json" % stamp)).write_text("{}", encoding="utf-8")
        from unittest import mock
        with mock.patch("etf_agent.virtual_account.daily.DecisionRepository"):
            pending = _pending_orders(decisions, {"provenance": {"type": "prepared", "account_snapshot": {"id": 2}}})
        self.assertEqual(pending["decided_at"], "2026-10-27T07:00:00+08:00")
        self.assertEqual(pending["version_count"], 2)
        self.assertEqual(pending["execution_date"], "2026-10-27")

    def test_pending_orders_come_from_the_decision_of_the_prepared_snapshot(self):
        from etf_agent.dashboard.performance import _pending_orders

        decisions = self.root / "decisions"
        run = decisions / "2026-10-01" / "decision-1"
        run.mkdir(parents=True)
        (run / "decision_input.json").write_text(json.dumps({"account_snapshot": {"id": 1}}), encoding="utf-8")
        (run / "decision.json").write_text(json.dumps({"status": "approved", "portfolio": {"positions": [{"symbol": "2412.TW", "weight": "0.0798"}]}, "orders": [
            {"symbol": "2412.TW", "side": "sell", "intent": "trim", "lots": 30, "shares": 30000},
            {"symbol": "2330.TW", "side": "sell", "intent": "exit", "lots": 5, "shares": 5000}]}), encoding="utf-8")
        latest = {"provenance": {"type": "prepared", "account_snapshot": {"id": 1}}}
        from unittest import mock
        with mock.patch("etf_agent.virtual_account.daily.DecisionRepository"):
            pending = _pending_orders(decisions, latest)
        self.assertEqual(pending["orders"][0]["symbol"], "2412.TW")
        self.assertEqual(pending["orders"][0]["weight"], "0.0798")
        self.assertIsNone(pending["orders"][1]["weight"])
        weights = [item["weight"] for item in pending["orders"]]
        self.assertEqual(weights, ["0.0798", None])
        self.assertEqual(pending["decision_run_id"], "decision-1")
        self.assertIsNone(pending["decided_at"])
        self.assertIsNone(pending["version_count"])
        self.assertIsNone(_pending_orders(decisions, {"provenance": {"type": "close"}}))
        self.assertIsNone(_pending_orders(None, latest))

    def test_tampered_ledger_fails_closed(self):
        self.service.initialize(self.rules, "cup-test", "2026-09-18T17:00:00+08:00")
        self._close("close-1", "2026-09-21T16:00:00+08:00", "998998575", "110", "1425")
        (self.repository.runs / "close-1" / "transition.json").write_text('{"execution": {"fills": []}}', encoding="utf-8")
        with self.assertRaises(PerformanceError):
            build_performance_summary(self.repository)
        client = TestClient(create_app(self.root / "accounts", "cup-test"))
        self.assertEqual(client.get("/api/performance").status_code, 409)

    def test_api_and_page_are_served(self):
        self.service.initialize(self.rules, "cup-test", "2026-09-18T17:00:00+08:00")
        client = TestClient(create_app(self.root / "accounts", "cup-test"))
        response = client.get("/api/performance")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["nav"], "1000000000.00")
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("今日報酬率", page.text)


if __name__ == "__main__":
    unittest.main()
