"""換股提名端與配置端對齊。

1. ``agent_swap.victim_bear_claim``（已開）：提名端也要求換出者有本次已採納的該股 bear- claim（三種 purpose），沒有時列
   ``no_adopted_bear_claim``、``needs_nomination`` 不為注定落空的組呼叫 Agent；沒有該閘門時事實與簡報逐位元不變。
2. ``agent_swap.swap_fit`` v1（版本閘門，只由人設定）：成交上限兩端共用 ``agent_swap_fill_cap``（取 agent_swap 與
   rotation 上限減第一輪出場數的較小者）；可行性試算前段某組放不進時逐組跳過，不連帶取消後面可行的組。
3. 旗標文字：earmark 啟用時 entry_reserve 旗標寫「留作現金」，否則維持「回到一般預算」。
"""

import copy
import unittest
from unittest import mock

from etf_agent.decision import AllocationOrderEngine, ProposalValidator, decision_policy_sha256
from etf_agent.decision import swap_nomination as sn
from etf_agent.decision.allocation import AllocationOrderEngine as Engine
from etf_agent.decision.sizing import (
    AGENT_SWAP_FIELDS, GATE_PREREQUISITES, SWAP_FIT, agent_swap_fill_cap, swap_fit_enabled,
)
from etf_agent.decision.swap_nomination import needs_nomination

from test_agent_swap_allocation import four_symbol_bundle, four_symbol_items, v2_world
from test_agent_swap_policy import errors_for
from test_entry_reserve import reserve_sizing
from test_exposure_swap import ExposureWorld
from test_exposure_swap_allocation import LIMITS, TWO_PAIRS
from test_gate_prerequisites import decision_item
from test_swap_earmark import traded, flags
from test_swap_nomination import World, first_round_items, good_output

BEAR = {"version": "v1"}


def bear_gate(policy):
    policy["position_sizing"]["agent_swap"]["victim_bear_claim"] = dict(BEAR)


def review_only_items():
    """2330 第一輪 hold／low，只採納持股事實（沒有 bear- claim）。"""
    return first_round_items(**{
        "2330.TW": decision_item("2330.TW", "hold", "low", adopt=["review:2330.TW:position"], reject=["bear-2330-1", "bull-2330-1"])})


def review_output():
    raw = copy.deepcopy(good_output())
    raw["nominations"][0]["sell_basis_ids"] = ["review:2330.TW:position"]
    return raw


class NominationBearClaimTests(unittest.TestCase):
    def victim(self, world):
        return {row["symbol"]: row for row in world.facts()["victims"]}["2330.TW"]

    def test_without_the_gate_a_position_fact_alone_still_qualifies(self):
        world = World(review_only_items())
        self.assertTrue(self.victim(world)["nominable"])
        self.assertEqual(world.validate(review_output()), [])
        self.assertNotIn("no_adopted_bear_claim", str(world.brief()))

    def test_with_the_gate_a_position_fact_alone_is_blocked_and_the_agent_is_not_called(self):
        world = World(review_only_items(), mutate_policy=bear_gate)
        row = self.victim(world)
        self.assertFalse(row["nominable"])
        self.assertIn("no_adopted_bear_claim", row["reasons"])
        self.assertFalse(needs_nomination(world.facts()))
        errors = world.validate(good_output())
        self.assertTrue(any("no_adopted_bear_claim" in e for e in errors), errors)
        self.assertIn("no_adopted_bear_claim", world.brief()["note"])

    def test_with_the_gate_an_adopted_bear_claim_keeps_the_victim_nominable(self):
        items = first_round_items(**{
            "2330.TW": decision_item("2330.TW", "hold", "low", adopt=["bear-2330-1", "review:2330.TW:position"], reject=["bull-2330-1"])})
        world = World(items, mutate_policy=bear_gate)
        self.assertTrue(self.victim(world)["nominable"])
        self.assertEqual(world.validate(good_output()), [])

    def test_exposure_purpose_also_needs_the_bear_claim_only_with_the_gate(self):
        plain = ExposureWorld(review_only_items())
        gated = ExposureWorld(review_only_items(), mutate_policy=bear_gate)
        row = lambda world: {r["symbol"]: r for r in world.facts()["victims"]}["2330.TW"]
        self.assertTrue(row(plain)["exposure_nominable"])
        self.assertFalse(row(gated)["exposure_nominable"])
        self.assertIn("no_adopted_bear_claim", row(gated)["exposure_reasons"])
        self.assertFalse(needs_nomination(gated.facts()))


class FillCapTests(unittest.TestCase):
    def test_shared_function(self):
        sizing = {"agent_swap": {"max_swaps_per_run": 10}, "rotation": {"max_swaps_per_run": 7}}
        self.assertEqual(agent_swap_fill_cap(sizing, 2), 5)
        self.assertEqual(agent_swap_fill_cap(sizing, 9), 0)
        self.assertEqual(agent_swap_fill_cap(sizing, 2, rotation_bound=False), 10)
        sizing["rotation"]["max_swaps_per_run"] = 20
        self.assertEqual(agent_swap_fill_cap(sizing, 2), 10)

    def test_policy_validation_registration_and_prerequisites(self):
        self.assertIn("swap_fit", AGENT_SWAP_FIELDS)
        rule = next(rule for rule in GATE_PREREQUISITES if rule.field == "agent_swap.swap_fit")
        self.assertTrue(rule.human_only)
        sizing = reserve_sizing()
        self.assertFalse([e for e in errors_for(sizing) if "swap_fit" in e])
        sizing["agent_swap"]["swap_fit"] = dict(SWAP_FIT)
        self.assertTrue(swap_fit_enabled(sizing))
        self.assertFalse([e for e in errors_for(sizing) if "swap_fit" in e])
        sizing["agent_swap"]["swap_fit"] = {"version": "v2"}
        self.assertTrue(any("swap_fit 必須是" in e for e in errors_for(sizing)))
        sizing["agent_swap"]["swap_fit"] = dict(SWAP_FIT)
        sizing["agent_swap"]["version"] = "v1"
        self.assertTrue(any("swap_fit 需要 agent_swap v2" in e for e in errors_for(sizing)))
        self.assertFalse(swap_fit_enabled({}))


def core(body):
    return {key: body[key] for key in ("constraint_flags", "allocation_proposal", "order_proposal")}


class AllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="336000", positions={"2330.TW": 1000, "1101.TW": 1000}, **LIMITS)
        cls.pairs = list(reversed(TWO_PAIRS))
        cls.intent, cls.policy, cls.plain, _ = v2_world(
            cls.bundle, four_symbol_items(), pairs=cls.pairs, policy_fields=LIMITS, level="neutral", grade="lower")

    def run_with(self, policy, gate, intent=None):
        bound = copy.deepcopy(policy)
        if gate:
            bound["position_sizing"]["agent_swap"]["swap_fit"] = dict(SWAP_FIT)
        bound["content_sha256"] = decision_policy_sha256(bound)
        intent = intent or self.intent
        body = AllocationOrderEngine(self.bundle, bound).run(intent)
        self.assertEqual(ProposalValidator(self.bundle, bound, intent).validate(body), [])
        return body

    def buys(self, body):
        return {key[0] for key in traded(body) if key[1] == "buy"} & {"2412.TW", "2317.TW"}

    def test_without_the_gate_nothing_changes(self):
        self.assertEqual(self.run_with(self.policy, False), self.plain)

    def test_gate_with_roomy_rotation_cap_is_the_same_allocation(self):
        self.assertEqual(core(self.run_with(self.policy, True)), core(self.plain))

    def test_rotation_cap_is_already_enforced_by_the_slot_budget_so_the_shared_cap_changes_nothing_here(self):
        # 配置端的名額計算（swap_left＝min(釋出名額, rotation 上限)，釋出名額含第一輪出場）本來就把換入者壓在 rotation 上限內，
        # 所以對齊成交上限只是讓兩端同一算式、結果與舊版一致（取消原因同為 swap_cap）。
        _, tight_policy, _, _ = v2_world(
            self.bundle, four_symbol_items(), pairs=self.pairs, policy_fields=LIMITS, level="neutral", grade="lower", rotation_max=1)
        old, new = self.run_with(tight_policy, False), self.run_with(tight_policy, True)
        self.assertEqual(len(self.buys(old)), 1)
        self.assertEqual(core(old), core(new))

    def test_an_infeasible_earlier_pair_no_longer_cancels_the_feasible_later_pair(self):
        original = Engine._conviction_targets
        first_buyer, second_buyer = self.pairs[0][0], self.pairs[1][0]

        def blocked(engine, *args, **kwargs):
            result = original(engine, *args, **kwargs)
            if first_buyer in (kwargs.get("allowed_new") or set()) and kwargs.get("allowed_new") is not None:
                ordered, weights, skipped, budget, held = result
                return ordered, {symbol: weight for symbol, weight in weights.items() if symbol != first_buyer}, skipped, budget, held
            return result

        with mock.patch.object(Engine, "_conviction_targets", blocked):
            old = self.run_with(self.policy, False)
            new = self.run_with(self.policy, True)
        self.assertNotIn((second_buyer, "buy"), traded(old))  # 舊行為：前段放不進，連帶取消後面可行的組
        self.assertIn((second_buyer, "buy"), traded(new))
        self.assertNotIn((first_buyer, "buy"), traded(new))
        self.assertIn((first_buyer, "AGENT_SWAP_CANCELLED:swap_cap"), flags(new))


if __name__ == "__main__":
    unittest.main()
