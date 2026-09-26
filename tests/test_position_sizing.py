import copy
import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionFinalizer,
    DecisionPolicyValidator,
    DecisionResultValidator,
    DecisionToolError,
    ProposalValidator,
    RevisionHistoryBuilder,
    ScenarioEngine,
    SizingPlanValidator,
    apply_sizing_plan,
    artifact_content_sha256,
    build_decision_policy,
    decision_policy_sha256,
)
from etf_agent.decision.sizing import conviction_weights

from test_portfolio_decision_agent import refresh_artifact_hash
from test_portfolio_risk_decision import policy as base_policy, risk_review, valid_inputs


SIZING = {
    "method": "conviction_volatility_v1",
    "conviction_multipliers": {"high": "1.5", "medium": "1", "low": "0.5"},
    "volatility_floor": "0.01",
}


def sized_inputs(tier_2317="high", tier_2330="low"):
    bundle, momentum, debate, intent = valid_inputs()
    intent = copy.deepcopy(intent)
    for item in intent["items"]:
        if item["symbol"] == "2330.TW":
            item["intent"] = "add"
            item["adopted_claim_ids"] = ["buy-2330-momentum"]
            item["rejected_claim_ids"] = ["sell-2330-hold"]
    refresh_artifact_hash(intent)
    plan = {
        "schema_version": "1.0",
        "plan_id": "sizing-1",
        "bundle_id": bundle["bundle_id"],
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "bundle_hash": bundle["bundle_sha256"],
        "trade_intent_result_id": intent["result_id"],
        "trade_intent_sha256": intent["content_sha256"],
        "items": [
            {"symbol": "2317.TW", "conviction": tier_2317, "rationale": "動能與流動性較佳。", "evidence_ids": ["price-2317"]},
            {"symbol": "2330.TW", "conviction": tier_2330, "rationale": "已持有，加碼幅度保守。", "evidence_ids": ["price-2330"]},
        ],
        "errors": [],
    }
    plan["content_sha256"] = artifact_content_sha256(plan)
    policy = base_policy()
    policy["position_sizing"] = copy.deepcopy(SIZING)
    policy["content_sha256"] = decision_policy_sha256(policy)
    return bundle, intent, plan, policy


def sized_policy(policy, plan):
    result = apply_sizing_plan(policy, plan)
    result["content_sha256"] = decision_policy_sha256(result)
    return result


class SizingPlanValidatorTests(unittest.TestCase):
    def test_valid_plan_covers_exactly_buy_and_add_candidates(self):
        bundle, intent, plan, _ = sized_inputs()
        self.assertEqual(SizingPlanValidator(bundle, intent).validate(plan), [])

    def test_rejects_missing_candidate_bad_tier_numbers_and_foreign_evidence(self):
        bundle, intent, plan, _ = sized_inputs()
        cases = []
        missing = copy.deepcopy(plan)
        missing["items"] = missing["items"][:1]
        cases.append((missing, "未分級全部"))
        tier = copy.deepcopy(plan)
        tier["items"][0]["conviction"] = "max"
        cases.append((tier, "conviction"))
        weight = copy.deepcopy(plan)
        weight["items"][0]["target_weight"] = "0.3"
        cases.append((weight, "不得包含配置"))
        foreign = copy.deepcopy(plan)
        foreign["items"][0]["evidence_ids"] = ["price-2330"]
        cases.append((foreign, "不屬於"))
        for case, message in cases:
            refresh_artifact_hash(case)
            errors = SizingPlanValidator(bundle, intent).validate(case)
            self.assertTrue(any(message in error for error in errors), (message, errors))

    def test_tampered_plan_hash_is_rejected(self):
        bundle, intent, plan, _ = sized_inputs()
        plan["items"][0]["conviction"] = "low"
        errors = SizingPlanValidator(bundle, intent).validate(plan)
        self.assertTrue(any("content_sha256" in error for error in errors))


class ConvictionAllocationTests(unittest.TestCase):
    def test_water_filling_caps_and_redistributes_budget(self):
        weights = conviction_weights(
            ["A", "B", "C"],
            {"A": "high", "B": "medium", "C": "low"},
            {"high": Decimal("1.5"), "medium": Decimal("1"), "low": Decimal("0.5")},
            {"A": Decimal("0.01"), "B": Decimal("0.02"), "C": Decimal("0.02")},
            Decimal("0.01"),
            {"A": Decimal("0.3"), "B": Decimal("0.3"), "C": Decimal("0.3")},
            Decimal("0.8"),
        )
        self.assertEqual(weights["A"], Decimal("0.3"))
        self.assertEqual(weights["B"], Decimal("0.3"))
        self.assertEqual(weights["C"], Decimal("0.2"))

    def test_sized_proposal_follows_tiers_and_rebuilds(self):
        bundle, intent, plan, policy = sized_inputs("high", "low")
        policy = sized_policy(policy, plan)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        proposal = AllocationOrderEngine(bundle, policy).run(intent)

        targets = {key: Decimal(value) for key, value in proposal["position_sizing"]["target_weights"].items()}
        self.assertGreater(targets["2317.TW"], targets["2330.TW"])
        self.assertLessEqual(sum(targets.values()), Decimal("0.95"))
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(proposal), [])

        _, _, flipped_plan, flipped_policy = sized_inputs("low", "high")
        flipped = AllocationOrderEngine(bundle, sized_policy(flipped_policy, flipped_plan)).run(intent)
        flipped_targets = {key: Decimal(value) for key, value in flipped["position_sizing"]["target_weights"].items()}
        self.assertLess(flipped_targets["2317.TW"], targets["2317.TW"])

    def test_sized_policy_replays_through_guard_history_and_finalizer(self):
        bundle, momentum, debate, intent = valid_inputs()
        _, _, plan, settings = sized_inputs()
        plan["trade_intent_result_id"] = intent["result_id"]
        plan["trade_intent_sha256"] = intent["content_sha256"]
        plan["items"] = plan["items"][:1]
        refresh_artifact_hash(plan)
        self.assertEqual(SizingPlanValidator(bundle, intent).validate(plan), [])
        settings = sized_policy(settings, plan)

        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        self.assertEqual(list(proposal["position_sizing"]["target_weights"]), ["2317.TW"])
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        self.assertTrue(guard["passed"])
        review = risk_review(bundle, proposal, scenario, guard)
        history = RevisionHistoryBuilder(bundle, settings, intent).create(proposal, scenario, guard, review)
        result = DecisionFinalizer(bundle, settings, momentum, debate, intent).run(
            proposal, scenario, guard, review, history
        )
        self.assertEqual(result["status"], "approved")
        self.assertEqual(
            DecisionResultValidator(bundle, settings, momentum, debate, intent).validate(
                proposal, scenario, guard, review, history, result
            ),
            [],
        )

    def test_sizing_enabled_without_plan_fails_closed(self):
        bundle, intent, _, policy = sized_inputs()
        with self.assertRaisesRegex(DecisionToolError, "尚未套用 SizingPlan"):
            AllocationOrderEngine(bundle, policy).run(intent)

    def test_policy_rejects_invalid_sizing_config(self):
        bundle, _, plan, policy = sized_inputs()
        policy["position_sizing"]["conviction_multipliers"]["low"] = "2"
        policy["content_sha256"] = decision_policy_sha256(policy)
        errors = DecisionPolicyValidator(bundle).validate(policy)
        self.assertTrue(any("high ≥ medium ≥ low" in error for error in errors))
        with self.assertRaisesRegex(DecisionToolError, "已套用"):
            apply_sizing_plan(sized_policy(sized_inputs()[3], plan), plan)


class DecisionPolicyBuilderTests(unittest.TestCase):
    def template(self):
        policy = base_policy()
        for field in (
            "lot_size", "commission_rate", "sell_tax_rate", "minimum_commission",
            "max_stock_weight", "special_weight_limits", "max_sector_weight",
            "min_positions", "max_positions", "cash_weight_ceiling",
            "minimum_active_share", "reuse_sell_proceeds", "sector_classification_version",
            "sector_by_symbol", "content_sha256",
        ):
            policy.pop(field)
        return policy

    def sectors(self, available_at="2026-09-19T00:00:00+00:00"):
        return {
            "version": "sector:fixture",
            "available_at": available_at,
            "sector_by_symbol": {"2330.TW": "industry:24", "2317.TW": "industry:31"},
        }

    def test_copies_hard_rules_and_sector_map(self):
        bundle = valid_inputs()[0]
        policy = build_decision_policy(self.template(), bundle, self.sectors())
        self.assertEqual(policy["max_stock_weight"], bundle["rules"]["max_stock_weight"])
        self.assertEqual(policy["cash_weight_ceiling"], bundle["rules"]["cash_weight_must_be_below"])
        self.assertEqual(policy["sector_by_symbol"]["2330.TW"], "industry:24")
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])

    def test_rejects_template_hard_fields_future_or_incomplete_sectors(self):
        bundle = valid_inputs()[0]
        template = self.template()
        template["max_stock_weight"] = "0.9"
        with self.assertRaisesRegex(DecisionToolError, "硬性規則"):
            build_decision_policy(template, bundle, self.sectors())
        with self.assertRaisesRegex(DecisionToolError, "晚於 decision_cutoff"):
            build_decision_policy(self.template(), bundle, self.sectors("2026-09-30T00:00:00+00:00"))
        partial = self.sectors()
        partial["sector_by_symbol"].pop("2317.TW")
        with self.assertRaisesRegex(DecisionToolError, "未覆蓋"):
            build_decision_policy(self.template(), bundle, partial)


class PolicySizingCliTests(unittest.TestCase):
    def test_cli_build_policy_validate_and_apply_sizing(self):
        bundle, intent, plan, _ = sized_inputs()
        root = Path(__file__).resolve().parents[1]
        template = json.loads((root / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        # 內附樣板的時間晚於 fixture cutoff；只替換時間以驗證其餘欄位可直接使用。
        template["available_at"] = "2026-09-19T00:00:00+00:00"
        sectors = DecisionPolicyBuilderTests().sectors()
        with tempfile.TemporaryDirectory() as directory:
            paths = {name: Path(directory) / ("%s.json" % name) for name in ("bundle", "intent", "plan", "template", "sectors")}
            for name, payload in (("bundle", bundle), ("intent", intent), ("plan", plan), ("template", template), ("sectors", sectors)):
                paths[name].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            def run(*args):
                completed = subprocess.run(
                    [sys.executable, str(root / "cli" / "portfolio_decision.py"), "--bundle", str(paths["bundle"]), *args],
                    capture_output=True, text=True, check=False,
                )
                return completed.returncode, json.loads(completed.stdout)

            base = Path(directory) / "policy.json"
            sized = Path(directory) / "policy_sized.json"
            code, _ = run("build-policy", "--template", str(paths["template"]), "--sector", str(paths["sectors"]), "--output", str(base))
            self.assertEqual(code, 0)
            code, result = run("validate-sizing", "--intent", str(paths["intent"]), "--input", str(paths["plan"]))
            self.assertEqual((code, result["valid"]), (0, True))
            code, result = run("apply-sizing", "--policy", str(base), "--intent", str(paths["intent"]), "--sizing", str(paths["plan"]), "--output", str(sized))
            self.assertEqual(code, 0, result)
            policy = json.loads(sized.read_text(encoding="utf-8"))
        self.assertEqual(policy["position_sizing"]["sizing_plan_sha256"], plan["content_sha256"])
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])


if __name__ == "__main__":
    unittest.main()
