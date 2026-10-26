"""持股廣度：風險 Agent 只選集中／平衡／分散，程式依 Policy 檔數與信心順序挑股，舊 policy 行為不變。"""

import copy
import unittest

from etf_agent.decision import (
    AllocationOrderEngine,
    DecisionPolicyValidator,
    DecisionToolError,
    apply_trade_decision,
    build_team_inputs,
    decision_policy_sha256,
)
from etf_agent.decision.contracts import DecisionContext
from etf_agent.decision.sizing import validate_cash_stance

from test_position_sizing import SIZING, sized_inputs
from test_team_chain import finalize, team_world


def with_targets(policy, concentrated=1, balanced=2, diversified=2):
    policy = copy.deepcopy(policy)
    policy["position_sizing"]["positions_by_breadth"] = {
        "concentrated": concentrated, "balanced": balanced, "diversified": diversified,
    }
    policy["content_sha256"] = decision_policy_sha256(policy)
    return policy


def bind(policy, intent, stance, bundle):
    result = apply_trade_decision(policy, intent, stance, bundle)
    result["content_sha256"] = decision_policy_sha256(result)
    return result


class PositionBreadthTests(unittest.TestCase):
    def setUp(self):
        # 2317 新買、2330 持有加碼，兩檔同為 medium 且沒有支持數，只能靠 ATR 分先後。
        self.bundle, self.intent, stance, policy = sized_inputs("medium", "medium")
        self.base = with_targets(policy)
        self.stance = dict(stance, breadth="concentrated")

    def test_breadth_target_limits_positions_and_breaks_ties_by_atr(self):
        policy = bind(self.base, self.intent, self.stance, self.bundle)
        self.assertEqual(policy["position_sizing"]["position_breadth"], "concentrated")
        self.assertEqual(DecisionPolicyValidator(self.bundle).validate(policy), [])
        engine = AllocationOrderEngine(self.bundle, policy)
        atr = engine._atr_pct()
        calmer = min(("2317.TW", "2330.TW"), key=lambda symbol: (atr[symbol], symbol))
        proposal = engine.run(self.intent)
        self.assertEqual(list(proposal["position_sizing"]["target_weights"]), [calmer])
        flags = [flag for flag in proposal["constraint_flags"] if flag["code"] == "MAX_POSITIONS"]
        self.assertTrue(flags and all("持股廣度" in flag["detail"] for flag in flags), flags)

        wide = bind(self.base, self.intent, dict(self.stance, breadth="diversified"), self.bundle)
        targets = AllocationOrderEngine(self.bundle, wide).run(self.intent)["position_sizing"]["target_weights"]
        self.assertEqual(sorted(targets), ["2317.TW", "2330.TW"])

    def test_legacy_policy_without_targets_still_fills_max_positions(self):
        _, _, stance, policy = sized_inputs("medium", "medium")
        legacy = bind(policy, self.intent, stance, self.bundle)
        self.assertNotIn("position_breadth", legacy["position_sizing"])
        targets = AllocationOrderEngine(self.bundle, legacy).run(self.intent)["position_sizing"]["target_weights"]
        self.assertEqual(sorted(targets), ["2317.TW", "2330.TW"])

    def test_targets_require_breadth_from_risk_agent(self):
        plain = {key: value for key, value in self.stance.items() if key != "breadth"}
        with self.assertRaisesRegex(DecisionToolError, "breadth"):
            apply_trade_decision(self.base, self.intent, plain, self.bundle)
        errors = []
        validate_cash_stance(DecisionContext(self.bundle), dict(self.stance, breadth="all_in"), "cash_stance", errors)
        self.assertTrue(any("breadth" in error for error in errors), errors)

    def test_policy_rejects_tampered_or_invalid_breadth(self):
        bound = bind(self.base, self.intent, self.stance, self.bundle)
        cases = (
            ({"position_breadth": "all_in"}, "position_breadth"),
            ({"positions_by_breadth": {"concentrated": 3, "balanced": 2, "diversified": 2}}, "concentrated ≤ balanced"),
            ({"positions_by_breadth": {"concentrated": 1, "balanced": 2}}, "剛好包含"),
            ({"positions_by_breadth": {"concentrated": "1", "balanced": 2, "diversified": 2}}, "整數"),
        )
        for change, message in cases:
            policy = copy.deepcopy(bound)
            policy["position_sizing"].update(change)
            policy["content_sha256"] = decision_policy_sha256(policy)
            errors = DecisionPolicyValidator(self.bundle).validate(policy)
            self.assertTrue(any(message in error for error in errors), (message, errors))

    def test_finalizer_rebuilds_breadth_from_team_inputs(self):
        bundle, momentum, debate, trade, _, _, reports, research = team_world()
        from test_portfolio_risk_decision import policy as base_policy

        base = base_policy()
        base["position_sizing"] = copy.deepcopy(SIZING)
        base = with_targets(base, concentrated=2, balanced=2, diversified=2)
        stance = {"level": "neutral", "breadth": "concentrated", "rationale": "市場中性。", "evidence_ids": ["price-2330"]}
        policy = bind(base, trade, stance, bundle)
        _, result = finalize(bundle, momentum, debate, trade, policy, build_team_inputs(reports, research, stance))
        self.assertEqual(result["status"], "approved", result["errors"])

        tampered = build_team_inputs(reports, research, dict(stance, breadth="diversified"))
        _, rejected = finalize(bundle, momentum, debate, trade, policy, tampered)
        self.assertNotEqual(rejected["status"], "approved")
        self.assertTrue(any("position_sizing.position_breadth" in error for error in rejected["errors"]), rejected["errors"])


if __name__ == "__main__":
    unittest.main()
