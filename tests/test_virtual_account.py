import json
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

from etf_agent.decision.contracts import canonical_sha256
from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository, VirtualAccountService
from etf_agent.decision import DecisionRepository, artifact_content_sha256, decision_bundle_sha256
from test_portfolio_risk_decision import policy


def prepare_and_decide(service, root):
    """封存 prepare-day 並建立一個以該 AccountSnapshot 核准買入 2317 的 Decision run（分析團隊鏈）。"""
    # 延後匯入：test_team_chain 反向匯入 test_portfolio_risk_decision 等測試模組。
    from test_team_chain import finalize, team_world, tradable_bundle
    from test_trader import items as trade_items

    bundle = tradable_bundle()
    snapshot = bundle["snapshot"]
    snapshot["decision_cutoff"] = "2026-09-20T00:55:00+00:00"
    bundle["decision_cutoff"] = snapshot["decision_cutoff"]
    bundle["snapshot_sha256"] = canonical_sha256(snapshot)
    snapshot_path = root / "decision-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    prepared = service.prepare_day(snapshot_path, "prepare-with-orders")
    bundle["account_snapshot"] = prepared["account_snapshot"]
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)

    # 空倉：2317 買進，2330 未持有只能 no_trade。
    decisions = trade_items()
    decisions[1] = dict(decisions[1], intent="no_trade")
    settings = policy()
    settings["liquidity_fill_rate"] = "1"
    bundle, momentum, debate, intent, bound, team, _, _ = team_world(bundle, decisions, settings)
    artifacts, decision = finalize(bundle, momentum, debate, intent, bound, team)
    assert decision["status"] == "approved", decision["errors"]
    decision_root = root / "decisions"
    DecisionRepository(decision_root).save("decision-1", {
        "decision_input": bundle, "policy": bound, "momentum": momentum,
        "debate": debate, "intent": intent, "decision": decision, "team_inputs": team,
        **artifacts,
    })

    return snapshot, decision_root


class VirtualAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.rules = self.root / "rules.json"
        self.rules.write_text(json.dumps({"initial_capital_twd": 1_000_000_000, "version": "fixture-v1"}), encoding="utf-8")
        self.repository = VirtualAccountRepository(self.root / "accounts", "cup-test")
        self.service = VirtualAccountService(self.repository)

    def tearDown(self):
        self.temp.cleanup()

    def test_raw_price_is_required_and_adjusted_price_is_not_used(self):
        snapshot = {"latest_prices": [{"symbol": "2330.TW", "close_price": "100", "analysis_close_price": "70"}]}
        self.assertEqual(self.service._snapshot_prices(snapshot), {"2330.TW": "100"})
        del snapshot["latest_prices"][0]["close_price"]
        with self.assertRaises(ValueError):
            self.service._snapshot_prices(snapshot)

    def test_morning_prepare_releases_cash_due_today(self):
        self.service.initialize(self.rules, "cup-test", "2026-09-18T17:00:00+08:00")
        previous = self.repository.latest()["state"]
        state = self.service._state(
            account_id="cup-test", sequence=1, parent_state_id=previous["state_id"],
            as_of="2026-09-21T17:00:00+08:00", settled_cash=Decimal("999999900"),
            unsettled_cash=Decimal("100"), pending_settlements=[{"settlement_date": "2026-09-22", "amount": "100"}],
            positions=[], nav=Decimal("1000000000"), provenance={"type": "close"})
        self.repository.save("pending", {"state": state}, state)
        path = self.root / "morning.json"
        path.write_text(json.dumps({"snapshot_id": "morning", "usable": True,
            "decision_cutoff": "2026-09-22T08:55:00+08:00", "latest_trade_date": "2026-09-21",
            "latest_prices": [{"symbol": "2330.TW", "close_price": "100"}]}), encoding="utf-8")
        result = self.service.prepare_day(path, "morning")
        self.assertEqual(Decimal(result["state"]["settled_cash"]), Decimal("1000000000"))
        self.assertEqual(result["state"]["pending_settlements"], [])

    def test_genesis_uses_ten_billion_once_and_is_idempotent(self):
        first = self.service.initialize(self.rules, "cup-test", "2026-09-18T17:00:00+08:00")
        self.assertFalse(first["reused"])
        self.assertEqual(first["state"]["settled_cash"], "1000000000")
        self.assertEqual(first["state"]["nav"], "1000000000")
        second = self.service.initialize(self.rules, "cup-test", "2026-09-19T17:00:00+08:00")
        self.assertTrue(second["reused"])
        self.assertEqual(second["state"]["state_id"], first["state"]["state_id"])
        self.assertEqual(self.repository.latest()["state"]["sequence"], 0)

    def test_prepare_day_creates_cutoff_account_snapshot_and_advances_parent(self):
        self.service.initialize(self.rules, "cup-test", "2026-09-18T17:00:00+08:00")
        snapshot = {
            "snapshot_id": "snapshot-20260921", "decision_cutoff": "2026-09-21T00:55:00+00:00",
            "usable": True, "latest_trade_date": "2026-09-18",
            "latest_prices": [{"symbol": "2330.TW", "analysis_close_price": "100", "close_price": "100"}],
        }
        path = self.root / "snapshot.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        result = self.service.prepare_day(path, "prepare-20260921")
        account = result["account_snapshot"]
        self.assertEqual(account["cash"], "1000000000")
        self.assertEqual(account["settled_cash"], "1000000000")
        self.assertEqual(account["nav"], "1000000000")
        self.assertEqual(account["positions"], [])
        self.assertEqual(account["valuation_at"], snapshot["decision_cutoff"])
        self.assertEqual(self.repository.latest()["state"]["sequence"], 1)
        self.assertTrue(self.repository.verify("prepare-20260921").exists())

    def test_cutoff_must_advance_and_state_tampering_is_detected(self):
        self.service.initialize(self.rules, "cup-test", "2026-09-18T17:00:00+08:00")
        snapshot = {"snapshot_id": "snapshot-1", "decision_cutoff": "2026-09-21T00:55:00+00:00", "usable": True, "latest_trade_date": "2026-09-18", "latest_prices": []}
        path = self.root / "snapshot.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        with self.assertRaisesRegex(VirtualAccountError, "latest_prices"):
            self.service.prepare_day(path, "prepare-bad")
        latest_state = self.repository.latest()["state"]
        self.assertEqual(latest_state["sequence"], 0)
        state_file = self.repository.runs / "genesis" / "state.json"
        state_file.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(VirtualAccountError, "雜湊"):
            self.repository.latest()

    def test_repository_rejects_a_branch_from_stale_parent(self):
        first = self.service.initialize(self.rules, "cup-test", "2026-09-18T17:00:00+08:00")
        stale = dict(first["state"])
        stale.update({"sequence": 1, "parent_state_id": "wrong-parent", "as_of": "2026-09-19T00:00:00+00:00"})
        stale.pop("state_id")
        stale.pop("content_sha256")
        stale["state_id"] = "virtual-state:" + canonical_sha256(stale)[:20]
        stale["content_sha256"] = artifact_content_sha256(stale)
        with self.assertRaisesRegex(VirtualAccountError, "直接續接"):
            self.repository.save("orphan", {"state": stale}, stale)

    def test_validated_decision_is_simulated_and_rolls_into_next_account_state(self):
        self.service.initialize(self.rules, "cup-test", "2026-09-18T17:00:00+08:00")
        snapshot, decision_root = prepare_and_decide(self.service, self.root)
        execution_quotes = {}
        close_quotes = {}
        for row in snapshot["latest_prices"]:
            symbol = row["symbol"]
            price = row["analysis_close_price"]
            execution_quotes[symbol] = {"tradable": True, "available_lots": 10000, "execution_price": price}
            close_quotes[symbol] = {"close_price": price}
        execution_path = self.root / "execution.json"
        execution_path.write_text(json.dumps({"price_basis": "unadjusted", "available_at": "2026-09-20T01:30:00+00:00", "execution_at": "2026-09-20T01:31:00+00:00", "quotes": execution_quotes}), encoding="utf-8")
        close_path = self.root / "close.json"
        close_path.write_text(json.dumps({"trade_date": "2026-09-20", "price_basis": "unadjusted", "available_at": "2026-09-20T08:00:00+00:00", "quotes": close_quotes}), encoding="utf-8")
        result = self.service.apply_decision(decision_root, "decision-1", execution_path, close_path, "2026-09-22", "close-day-1")
        self.assertTrue(result["transition"]["execution"]["fills"])
        self.assertEqual(result["state"]["sequence"], 2)
        self.assertTrue(result["state"]["positions"])
        self.assertEqual(self.repository.latest()["state"]["state_id"], result["state"]["state_id"])


if __name__ == "__main__":
    unittest.main()
