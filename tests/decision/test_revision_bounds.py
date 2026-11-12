"""風險修正動作的數值界線（position_sizing.revision_bounds v1）：docs/plans/audit_fix_plan.md 項目 B。

1. Policy 驗證：形狀、下限不得高於 Policy 上限、缺欄位、多餘欄位、版本一律 fail-closed。
2. RiskReviewValidator：閘門開時 value 不得低於下限且須為 step 整數倍；沒有閘門時行為逐位元不變（0.01 仍通過）。
3. 登記：GATE_PREREQUISITES、POSITION_SIZING_FIELDS、策略反思禁區。
"""

import copy
import unittest

from etf_agent.automation.risk_stages import _revision_bounds_text
from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionPolicyValidator,
    RiskReviewValidator,
    ScenarioEngine,
    decision_policy_sha256,
    revision_effects,
)
from etf_agent.decision.sizing import GATE_FIELDS, GATE_PREREQUISITES, POSITION_SIZING_FIELDS, revision_bounds_gate
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_cash_fit_and_limits import gated
from test_portfolio_risk_decision import chain_inputs, risk_review
from test_position_sizing import sized_inputs, sized_policy

# 測試用數字（基礎 Policy 的 max_stock_weight 0.50、max_turnover_rate 0.50）；正式數字由人填，程式不設預設。
BOUNDS = {"version": "v1", "min_max_stock_weight": "0.10", "min_max_turnover_rate": "0.20", "step": "0.05"}


def action(kind, value):
    return {"type": kind, "symbol": "", "value": value, "reason": "降低風險。"}


class PolicyGateTests(unittest.TestCase):
    def setUp(self):
        self.bundle, intent, stance, policy = sized_inputs()
        self.policy = sized_policy(policy, intent, stance, self.bundle)

    def errors(self, gate):
        return DecisionPolicyValidator(self.bundle).validate(gated(self.policy, revision_bounds=gate))

    def test_valid_gate_passes_and_absent_gate_is_unchanged(self):
        self.assertEqual(DecisionPolicyValidator(self.bundle).validate(self.policy), [])
        self.assertEqual(self.errors(copy.deepcopy(BOUNDS)), [])

    def test_bad_shapes_fail_closed(self):
        cases = {
            "version": dict(BOUNDS, version="v2"),
            "floor above policy limit": dict(BOUNDS, min_max_stock_weight="0.60"),
            "turnover floor above policy limit": dict(BOUNDS, min_max_turnover_rate="0.60"),
            "zero floor": dict(BOUNDS, min_max_stock_weight="0"),
            "zero step": dict(BOUNDS, step="0"),
            "step above one": dict(BOUNDS, step="1.5"),
            "non decimal": dict(BOUNDS, step="abc"),
            "extra field": dict(BOUNDS, default_floor="0.1"),
            "not an object": "v1",
        }
        for label, gate in cases.items():
            self.assertTrue(self.errors(gate), label)
        for missing in ("min_max_stock_weight", "min_max_turnover_rate", "step", "version"):
            gate = {key: value for key, value in BOUNDS.items() if key != missing}
            self.assertTrue(self.errors(gate), missing)

    def test_registration_and_reflection_forbidden(self):
        self.assertIn("revision_bounds", GATE_FIELDS)
        self.assertIn("revision_bounds", POSITION_SIZING_FIELDS)
        rule = next(rule for rule in GATE_PREREQUISITES if rule.field == "revision_bounds")
        self.assertTrue(rule.human_only)
        self.assertIn("revision_bounds", FORBIDDEN_PREFIXES)
        self.assertIsNone(revision_bounds_gate({}))
        self.assertIsNone(revision_bounds_gate(None))
        self.assertEqual(revision_bounds_gate({"revision_bounds": BOUNDS}), BOUNDS)


class ReviewBoundsTests(unittest.TestCase):
    def setUp(self):
        self.bundle, _, _, intent, self.policy, _, _, _ = chain_inputs()
        self.proposal = AllocationOrderEngine(self.bundle, self.policy).run(intent)
        self.scenario = ScenarioEngine(self.bundle, self.policy).run(self.proposal)
        self.guard = CompetitionGuardV2(self.bundle, self.policy).run(self.proposal, self.scenario)
        self.gated = copy.deepcopy(self.policy)
        self.gated["position_sizing"] = {"revision_bounds": copy.deepcopy(BOUNDS)}

    def review(self, actions):
        return risk_review(self.bundle, self.proposal, self.scenario, self.guard, "revise", 1, actions)

    def validate(self, policy, actions):
        return RiskReviewValidator(self.bundle, policy).validate(
            self.proposal, self.scenario, self.guard, self.review(actions)
        )

    def test_without_gate_behaviour_is_unchanged(self):
        for kind in ("reduce_max_stock_weight", "reduce_turnover_limit"):
            self.assertEqual(self.validate(self.policy, [action(kind, "0.01")]), [], kind)
            self.assertEqual(self.validate(self.policy, [action(kind, "0.333")]), [], kind)
        # 不含 revision_bounds 的 position_sizing 也一樣。
        plain = copy.deepcopy(self.policy)
        plain["position_sizing"] = {}
        self.assertEqual(self.validate(plain, [action("reduce_max_stock_weight", "0.01")]), [])
        self.assertEqual(_revision_bounds_text(self.policy), "")

    def test_gate_accepts_values_on_grid_and_effects_follow(self):
        actions = [action("reduce_max_stock_weight", "0.30"), action("reduce_turnover_limit", "0.25")]
        self.assertEqual(self.validate(self.gated, actions), [])
        overrides = revision_effects(self.review(actions), self.proposal)["overrides"]
        self.assertEqual(overrides["max_stock_weight"], "0.30")
        self.assertEqual(overrides["max_turnover_rate"], "0.25")
        self.assertEqual(self.validate(self.gated, [action("reduce_max_stock_weight", "0.10")]), [])  # 剛好等於下限

    def test_gate_rejects_below_floor(self):
        errors = self.validate(self.gated, [action("reduce_max_stock_weight", "0.05")])
        self.assertTrue(any("不得低於 0.10" in e for e in errors), errors)
        errors = self.validate(self.gated, [action("reduce_turnover_limit", "0.15")])
        self.assertTrue(any("不得低於 0.20" in e for e in errors), errors)

    def test_gate_rejects_off_step(self):
        errors = self.validate(self.gated, [action("reduce_max_stock_weight", "0.33")])
        self.assertTrue(any("整數倍" in e for e in errors), errors)
        errors = self.validate(self.gated, [action("reduce_turnover_limit", "0.22")])
        self.assertTrue(any("整數倍" in e for e in errors), errors)

    def test_gate_does_not_relax_existing_checks(self):
        errors = self.validate(self.gated, [action("reduce_max_stock_weight", "0.60")])
        self.assertTrue(any("不得提高風險上限" in e for e in errors), errors)

    def test_increase_cash_buffer_checks_identical_with_and_without_gate(self):
        for value in ("0.12", "0.125", "0.95", "0.01"):
            self.assertEqual(
                self.validate(self.policy, [action("increase_cash_buffer", value)]),
                self.validate(self.gated, [action("increase_cash_buffer", value)]),
                value,
            )

    def test_brief_text_only_with_gate(self):
        text = _revision_bounds_text(self.gated)
        for needle in ("0.10", "0.20", "0.05", "remove_candidate"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
