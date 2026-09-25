import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.decision.contracts import artifact_content_sha256, canonical_sha256
from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository, VirtualAccountService
from etf_agent.decision import (
    AllocationOrderEngine, CompetitionGuardV2, DecisionFinalizer, DecisionRepository,
    MomentumEngine, RevisionHistoryBuilder, ScenarioEngine, artifact_content_sha256,
    decision_policy_sha256,
)
from test_portfolio_decision_agent import debate_bundle, decision_bundle, trade_intent_result
from test_portfolio_risk_decision import policy, risk_review


def prepare_and_decide(service, root):
    """封存 prepare-day 並建立一個以該 AccountSnapshot 核准買入 2317 的 Decision run。"""
    bundle = decision_bundle()
    snapshot = bundle["snapshot"]
    snapshot["tradable_symbols"] = ["2330.TW", "2317.TW"]
    snapshot["not_tradable_symbols"] = []
    snapshot["decision_cutoff"] = "2026-09-20T00:55:00+00:00"
    bundle["decision_cutoff"] = snapshot["decision_cutoff"]
    bundle["snapshot_sha256"] = canonical_sha256(snapshot)
    bundle["snapshot_sha256"] = canonical_sha256(snapshot)
    snapshot_path = root / "decision-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    prepared = service.prepare_day(snapshot_path, "prepare-with-orders")
    bundle["account_snapshot"] = prepared["account_snapshot"]
    from etf_agent.decision import decision_bundle_sha256
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)

    momentum = MomentumEngine(bundle).run()
    debate = debate_bundle(bundle, momentum)
    debate["packets"][0]["items"] = [item for item in debate["packets"][0]["items"] if item["symbol"] == "2317.TW"]
    debate["packets"][1]["items"] = []
    for packet in debate["packets"]:
        packet["content_sha256"] = artifact_content_sha256(packet)
    debate["content_sha256"] = artifact_content_sha256(debate)
    intent = trade_intent_result(bundle, momentum, debate)
    intent["items"] = [item for item in intent["items"] if item["symbol"] == "2317.TW"]
    intent["content_sha256"] = artifact_content_sha256(intent)
    settings = policy()
    settings["liquidity_fill_rate"] = "1"
    settings["content_sha256"] = decision_policy_sha256(settings)
    proposal = AllocationOrderEngine(bundle, settings).run(intent)
    scenario = ScenarioEngine(bundle, settings).run(proposal)
    guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
    review = risk_review(bundle, proposal, scenario, guard)
    history = RevisionHistoryBuilder(bundle, settings, intent).create(proposal, scenario, guard, review)
    decision = DecisionFinalizer(bundle, settings, momentum, debate, intent).run(proposal, scenario, guard, review, history)
    assert decision["status"] == "approved"
    decision_root = root / "decisions"
    DecisionRepository(decision_root).save("decision-1", {
        "decision_input": bundle, "policy": settings, "momentum": momentum,
        "debate": debate, "intent": intent, "proposal": proposal,
        "scenario": scenario, "guard": guard, "risk_review": review,
        "revision_history": history, "decision": decision,
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
            "latest_prices": [{"symbol": "2330.TW", "analysis_close_price": "100"}],
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
        close_path.write_text(json.dumps({"price_basis": "unadjusted", "available_at": "2026-09-20T08:00:00+00:00", "quotes": close_quotes}), encoding="utf-8")
        result = self.service.apply_decision(decision_root, "decision-1", execution_path, close_path, "2026-09-22", "close-day-1")
        self.assertTrue(result["transition"]["execution"]["fills"])
        self.assertEqual(result["state"]["sequence"], 2)
        self.assertTrue(result["state"]["positions"])
        self.assertEqual(self.repository.latest()["state"]["state_id"], result["state"]["state_id"])


if __name__ == "__main__":
    unittest.main()
