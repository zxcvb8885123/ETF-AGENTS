import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.competition import (
    DPlanError, DPlanExporter, DPlanValidator, load_decision_run, verify_account_binding,
)
from etf_agent.decision import (
    AllocationOrderEngine, CompetitionGuardV2, DecisionFinalizer,
    RevisionHistoryBuilder, ScenarioEngine,
)
from etf_agent.virtual_account import VirtualAccountRepository, VirtualAccountService
from test_portfolio_risk_decision import policy, risk_review, valid_inputs
from test_virtual_account import prepare_and_decide


def valid_context():
    return {
        "sources": [{
            "source_id": "S1", "authority": "vendor", "url": "https://data.example.test/price",
            "content_as_of": "2026-09-19T18:00:00+08:00",
        }],
        "observations": [{
            "obs_id": "O1", "source_ref": ["S1"], "statement": "測試用事實資料，不代表真實市場行情。",
            "values": {"fixture_value": 1.0},
        }],
        "market_view": {
            "basis_refs": ["O1"], "logic": "此為測試內容，不構成任何真實市場方向判斷。",
            "regime": "neutral", "stance": "neutral",
            "posture": {"net_exposure_intent": "hold", "target_cash_pct_range": [0.0, 0.25]},
            "counter_evidence": None,
        },
        "inferences": [{
            "inf_id": "I1", "premise_refs": ["O1"],
            "logic": "此為測試用推論內容，僅用於驗證引用鏈和欄位契約。",
            "counter_evidence": None,
        }],
        "inference_refs_by_ticker": {"2330": ["I1"], "2317": ["I1"]},
        "agent_metadata": {
            "model_provider": "openai", "model_version": "codex-test-1",
            "run_started_at": "2026-09-19T20:01:00+08:00",
            "run_completed_at": "2026-09-19T20:02:00+08:00", "code_version": "test-fixture",
        },
    }


def approved_artifacts():
    bundle, momentum, debate, intent = valid_inputs()
    settings = policy()
    proposal = AllocationOrderEngine(bundle, settings).run(intent)
    scenario = ScenarioEngine(bundle, settings).run(proposal)
    guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
    review = risk_review(bundle, proposal, scenario, guard)
    history = RevisionHistoryBuilder(bundle, settings, intent).create(proposal, scenario, guard, review)
    result = DecisionFinalizer(bundle, settings, momentum, debate, intent).run(proposal, scenario, guard, review, history)
    return {
        "decision_input": bundle, "momentum": momentum, "debate": debate, "intent": intent,
        "policy": settings, "proposal": proposal, "scenario": scenario, "guard": guard,
        "risk_review": review, "revision_history": history, "decision": result,
    }


def valid_plan():
    context = valid_context()
    return {
        "schema_version": "4.0", "doc_type": "D-Plan", "team_id": "TEAM_042", "trade_date": "2026-09-22",
        "sources": context["sources"], "observations": context["observations"], "market_view": context["market_view"],
        "inferences": context["inferences"], "decisions": [], "no_trade_decisions": [], "orders": [],
        "agent_metadata": context["agent_metadata"],
    }


class DPlanContractTests(unittest.TestCase):
    def test_detects_orphan_references_unknown_fields_and_noncontiguous_ids(self):
        plan = valid_plan()
        self.assertEqual(DPlanValidator().validate(plan), [])
        changed = copy.deepcopy(plan)
        changed["observations"][0]["source_ref"] = ["S9"]
        changed["_unofficial"] = "not allowed"
        errors = DPlanValidator().validate(changed)
        self.assertTrue(any("引用不存在" in error for error in errors))
        self.assertTrue(any("未定義欄位" in error for error in errors))

    def test_exporter_requires_explicit_reasoning_context_and_rejects_mismatch(self):
        artifacts = approved_artifacts()
        with self.assertRaisesRegex(DPlanError, "context 缺少"):
            DPlanExporter().build("TEAM_042", "2026-09-22", {}, artifacts)
        with self.assertRaisesRegex(DPlanError, "eligible_tickers"):
            DPlanExporter().build("TEAM_042", "2026-09-22", valid_context(), artifacts)

    def test_validator_rejects_trade_orphan_and_bad_lot(self):
        plan = valid_plan()
        plan["decisions"] = [{"decision_id": "D1", "ticker": "2330", "action": "BUY", "target_weight": 0.1, "inference_refs": ["I1"]}]
        plan["orders"] = [{"ticker": "2330", "side": "BUY", "shares": 500, "decision_ref": "D999"}]
        errors = DPlanValidator().validate(plan)
        self.assertTrue(any("引用不存在" in error for error in errors))
        self.assertTrue(any("1,000 股整數倍" in error for error in errors))


class DPlanAccountBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        rules = self.root / "rules.json"
        rules.write_text(json.dumps({"initial_capital_twd": 1_000_000_000, "version": "fixture-v1"}), encoding="utf-8")
        self.accounts = self.root / "accounts"
        self.repository = VirtualAccountRepository(self.accounts, "cup-test")
        self.service = VirtualAccountService(self.repository)
        self.service.initialize(rules, "cup-test", "2026-09-18T17:00:00+08:00")
        self.genesis_run = self.repository.latest()["run_id"]
        _, decisions = prepare_and_decide(self.service, self.root)
        self.artifacts = load_decision_run(str(decisions), "decision-1")

    def tearDown(self):
        self.temp.cleanup()

    def bind(self, artifacts=None, run_id="prepare-with-orders"):
        return verify_account_binding(artifacts or self.artifacts, str(self.accounts), "cup-test", run_id)

    def test_accepts_latest_prepare_day_run_used_by_decision(self):
        binding = self.bind()
        self.assertEqual(binding["run_id"], "prepare-with-orders")
        self.assertTrue(binding["account_snapshot_sha256"])

    def test_rejects_run_that_is_not_latest(self):
        with self.assertRaisesRegex(DPlanError, "latest"):
            self.bind(run_id=self.genesis_run)

    def test_rejects_changed_account_snapshot(self):
        changed = copy.deepcopy(self.artifacts)
        changed["decision_input"]["account_snapshot"]["cash"] = "1"
        with self.assertRaisesRegex(DPlanError, "AccountSnapshot"):
            self.bind(changed)

    def test_rejects_snapshot_or_cutoff_mismatch(self):
        changed = copy.deepcopy(self.artifacts)
        changed["decision_input"]["decision_cutoff"] = "2026-09-21T00:55:00+00:00"
        with self.assertRaisesRegex(DPlanError, "Snapshot／cutoff"):
            self.bind(changed)
        changed = copy.deepcopy(self.artifacts)
        changed["decision_input"]["snapshot"]["snapshot_id"] = "other-snapshot"
        with self.assertRaisesRegex(DPlanError, "Snapshot／cutoff"):
            self.bind(changed)

    def test_rejects_missing_account_run(self):
        with self.assertRaisesRegex(DPlanError, "VirtualAccount run 驗證失敗"):
            self.bind(run_id="prepare-missing")


if __name__ == "__main__":
    unittest.main()
