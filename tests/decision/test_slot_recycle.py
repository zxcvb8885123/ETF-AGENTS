"""position_sizing.slot_recycle v1（名額回收，版本閘門）。

排名在前的新股因預算為零或目標金額不足一張而算不出整張（LOT_SIZE）時，原本仍占掉一個新增名額，
後面的候選被記成 MAX_POSITIONS。閘門開啟時把它移出名額計數；省略閘門的舊 Policy 逐位元維持原重建行為。
"""

import copy
import unittest

from etf_agent.decision import AllocationOrderEngine, DecisionPolicyValidator, ProposalValidator, decision_policy_sha256
from etf_agent.decision.sizing import SLOT_RECYCLE, slot_recycle_enabled, validate_position_sizing

from test_exposure_rotation import LIMITS, NARROW
from test_gate_prerequisites import anchored_sizing, decision_item, sized, stance, three_symbol_bundle, three_symbol_intent
from test_portfolio_risk_decision import policy as base_policy
from test_risk_limit_gates import guard

POLICY_FIELDS = dict(liquidity_fill_rate="1", max_turnover_rate="1", **LIMITS)


def items(c1101="high", c2317="medium", c2330="low"):
    return [
        decision_item("1101.TW", "buy", c1101, adopt=["bull-1101-1"]),
        decision_item("2317.TW", "buy", c2317, adopt=["bull-2317-1"]),
        decision_item("2330.TW", "buy", c2330, adopt=["bull-2330-1"], reject=["bear-2330-1"]),
    ]


def world(gate=False, price_1101="5000", sizing_changes=None, policy_fields=None, level="neutral"):
    """1101 一張要 500 萬（遠超過任何權重上限）：排名第一卻永遠 LOT_SIZE；2317／2330 買得起。"""
    bundle = three_symbol_bundle("1000000", {}, price_1101=price_1101, **LIMITS)
    intent = three_symbol_intent(bundle, items())
    sizing = anchored_sizing(bundle, **NARROW)
    if gate:
        sizing["slot_recycle"] = copy.deepcopy(SLOT_RECYCLE)
    sizing.update(sizing_changes or {})
    base = base_policy()
    fields = {**POLICY_FIELDS, **(policy_fields or {})}
    policy, body = sized(bundle, intent, stance(level, "middle", "concentrated"), sizing, base, **fields)
    return bundle, intent, policy, body


def flags(body):
    return {(flag["symbol"], flag["code"]) for flag in body["constraint_flags"]}


def bought(body):
    return {o["symbol"] for o in body["order_proposal"]["orders"] if o["side"] == "buy"}


class SlotRecycleTests(unittest.TestCase):
    def test_without_the_gate_a_lot_size_candidate_keeps_its_slot(self):
        # 與描述相符的現況：1101 排名第一、LOT_SIZE，仍占掉一個名額，2330 被記 MAX_POSITIONS。
        _, _, policy, body = world(gate=False)
        self.assertNotIn("slot_recycle", policy["position_sizing"])
        self.assertEqual(bought(body), {"2317.TW"})
        found = flags(body)
        self.assertIn(("1101.TW", "LOT_SIZE"), found)
        self.assertIn(("2330.TW", "MAX_POSITIONS"), found)

    def test_with_the_gate_the_slot_goes_to_the_next_candidate(self):
        bundle, _, policy, body = world(gate=True)
        self.assertTrue(slot_recycle_enabled(policy["position_sizing"]))
        self.assertEqual(bought(body), {"2317.TW", "2330.TW"})
        found = flags(body)
        self.assertIn(("1101.TW", "LOT_SIZE"), found)
        self.assertNotIn(("2330.TW", "MAX_POSITIONS"), found)
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_total_positions_never_exceed_the_limit(self):
        for gate in (False, True):
            with self.subTest(gate=gate):
                _, _, _, body = world(gate=gate)
                positions = body["allocation_proposal"]["positions"]
                self.assertLessEqual(len(positions), int(LIMITS["max_positions"]))
        # 兩檔都買不起時不會因回收而多配：名額仍只有 2 個。
        _, _, _, body = world(gate=True)
        self.assertEqual(len(body["allocation_proposal"]["positions"]), 2)

    def test_everything_affordable_is_identical_with_or_without_the_gate(self):
        # 沒有任何 LOT_SIZE 候選時，開閘門的結果與沒開逐欄相同（除了 policy 本身）。
        off = world(gate=False, price_1101="100")[3]
        on = world(gate=True, price_1101="100")[3]
        for key in ("allocation_proposal", "order_proposal", "constraint_flags"):
            self.assertEqual(off[key], on[key], key)

    def test_all_candidates_unaffordable_still_records_lot_size_without_overflow(self):
        _, _, _, body = world(gate=True, price_1101="500000")
        self.assertLessEqual(len(body["allocation_proposal"]["positions"]), 2)
        self.assertIn(("1101.TW", "LOT_SIZE"), flags(body))

    def test_removing_the_gate_after_the_fact_fails_the_rebuild(self):
        bundle, intent, policy, body = world(gate=True)
        altered = copy.deepcopy(policy)
        del altered["position_sizing"]["slot_recycle"]
        altered["content_sha256"] = decision_policy_sha256(altered)
        rebuilt = ProposalValidator(bundle, altered, intent).validate(body)
        self.assertTrue(rebuilt)


class SlotRecycleShapeTests(unittest.TestCase):
    def errors(self, value):
        sizing = anchored_sizing(three_symbol_bundle("1000000", {}, **LIMITS), **NARROW)
        sizing["slot_recycle"] = value
        policy = base_policy()
        policy["position_sizing"] = sizing
        errors = []
        validate_position_sizing(policy, ["1101.TW", "2317.TW", "2330.TW"], errors)
        return errors

    def test_only_the_exact_version_object_is_accepted(self):
        self.assertEqual(self.errors({"version": "v1"}), [])
        for bad in ("v1", True, None, {}, {"version": "v2"}, {"version": "v1", "extra": 1}, ["v1"], 1):
            with self.subTest(bad=bad):
                self.assertTrue(any("slot_recycle" in error for error in self.errors(bad)), bad)

    def test_strategy_reflection_cannot_touch_it(self):
        from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES
        self.assertIn("slot_recycle", FORBIDDEN_PREFIXES)


if __name__ == "__main__":
    unittest.main()
