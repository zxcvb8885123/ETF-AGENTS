"""名額脈絡（``decision/rotation.py``）與 team_inputs 2.3 的確定性與 fail-closed 測試。"""

import copy
import unittest

from etf_agent.decision import (
    DecisionFinalizer,
    apply_trade_decision,
    build_portfolio_slots,
    build_prior_decision_context,
    build_team_inputs,
    decision_policy_sha256,
    validate_portfolio_slots,
)
from etf_agent.decision.fields import DecisionToolError
from etf_agent.decision.finalization import ROTATION_TEAM_FIELDS, ROTATION_TEAM_VERSION
from etf_agent.decision.rotation import PORTFOLIO_SLOTS_FIELDS, PORTFOLIO_SLOTS_VERSION
from etf_agent.decision.sizing import HELD_ADD_PRIORITY, ROTATION_VERSION

from test_team_chain import team_world

ROTATION = {"version": ROTATION_VERSION, "max_swaps_per_run": 10, "min_new_conviction": "low"}


def world(rotation=True):
    """(bundle, momentum, debate, trade, base_policy, final_policy, reports)。

    ``team_world`` 會用 SIZING 覆蓋 position_sizing，所以 rotation 必須在這裡自行綁進 policy。
    """
    from test_portfolio_risk_decision import policy as base_policy
    from test_position_sizing import SIZING
    from test_team_chain import STANCE

    bundle, momentum, debate, trade, _, _, reports, _ = team_world()
    base = base_policy()
    base["position_sizing"] = copy.deepcopy(SIZING)
    base["position_sizing"]["held_add_priority"] = HELD_ADD_PRIORITY
    if rotation:
        base["position_sizing"]["rotation"] = dict(ROTATION)
    base["content_sha256"] = decision_policy_sha256(base)
    final = apply_trade_decision(copy.deepcopy(base), trade, STANCE, bundle)
    final["content_sha256"] = decision_policy_sha256(final)
    return bundle, momentum, debate, trade, base, final, reports


class BuildPortfolioSlotsTests(unittest.TestCase):
    def setUp(self):
        (self.bundle, self.momentum, self.debate, self.trade,
         self.base, self.policy, self.reports) = world()
        self.slots = build_portfolio_slots(self.bundle, self.policy, self.debate)

    def test_fields_are_exactly_the_whitelist(self):
        self.assertEqual(set(self.slots), PORTFOLIO_SLOTS_FIELDS)
        self.assertEqual(self.slots["schema_version"], PORTFOLIO_SLOTS_VERSION)

    def test_counts_come_from_account_and_rules(self):
        held = len(self.bundle["account_snapshot"]["positions"])
        self.assertEqual(self.slots["held_positions"], held)
        self.assertEqual(self.slots["max_positions"], int(self.policy["max_positions"]))
        self.assertEqual(
            self.slots["free_slots"], max(0, int(self.policy["max_positions"]) - held)
        )

    def test_rotation_block_mirrors_policy(self):
        self.assertEqual(
            self.slots["rotation"],
            {"enabled": True, "max_swaps_per_run": 10, "min_new_conviction": "low"},
        )

    def test_rotation_disabled_when_policy_omits_it(self):
        bundle, _, debate, _, _, policy, _ = world(rotation=False)
        slots = build_portfolio_slots(bundle, policy, debate)
        self.assertEqual(slots["rotation"], {"enabled": False})

    def test_strength_counts_split_held_and_unheld(self):
        held = {str(item["symbol"]).upper() for item in self.bundle["account_snapshot"]["positions"]}
        universe = {str(row["symbol"]).upper() for row in self.bundle["snapshot"]["latest_prices"]}
        self.assertEqual(sum(self.slots["held_by_bear_strength"].values()), len(held))
        self.assertEqual(
            sum(self.slots["unheld_by_bull_strength"].values()), len(universe - held)
        )

    def test_base_and_final_policy_build_identical_slots(self):
        """Finalizer 用最終 policy 重算，交易階段用基礎 policy 建立；兩者必須完全相同。"""
        self.assertNotEqual(self.policy["position_sizing"], self.base["position_sizing"])
        self.assertEqual(
            build_portfolio_slots(self.bundle, self.base, self.debate),
            build_portfolio_slots(self.bundle, self.policy, self.debate),
        )

    def test_missing_stance_packet_fails_closed(self):
        broken = dict(self.debate, packets=[self.debate["packets"][0]])
        with self.assertRaises(DecisionToolError):
            build_portfolio_slots(self.bundle, self.policy, broken)


class ValidatePortfolioSlotsTests(unittest.TestCase):
    def setUp(self):
        self.bundle, _, self.debate, _, _, self.policy, _ = world()
        self.slots = build_portfolio_slots(self.bundle, self.policy, self.debate)

    def errors_for(self, slots):
        errors: list = []
        validate_portfolio_slots(self.bundle, self.policy, self.debate, slots, errors)
        return errors

    def test_rebuilt_slots_pass(self):
        self.assertEqual(self.errors_for(self.slots), [])

    def test_tampered_count_is_rejected(self):
        errors = self.errors_for(dict(self.slots, free_slots=99))
        self.assertTrue(any("與重算結果不同" in error for error in errors), errors)

    def test_tampered_note_is_rejected(self):
        errors = self.errors_for(dict(self.slots, note="可以為了騰名額出場。"))
        self.assertTrue(any("與重算結果不同" in error for error in errors), errors)

    def test_unknown_field_is_rejected(self):
        errors = self.errors_for(dict(self.slots, suggested_exit="2330.TW"))
        self.assertTrue(any("未允許欄位" in error or "suggested_exit" in error for error in errors), errors)

    def test_missing_field_is_rejected(self):
        partial = {key: value for key, value in self.slots.items() if key != "free_slots"}
        self.assertNotEqual(self.errors_for(partial), [])

    def test_non_mapping_is_rejected(self):
        errors = self.errors_for("30")
        self.assertTrue(any("必須是物件" in error for error in errors), errors)


class TeamInputsRotationVersionTests(unittest.TestCase):
    def setUp(self):
        (self.bundle, self.momentum, self.debate, self.trade,
         _, self.policy, self.reports) = world()
        self.slots = build_portfolio_slots(self.bundle, self.policy, self.debate)
        from test_team_chain import STANCE

        self.stance = STANCE

    def test_slots_promote_team_inputs_to_2_3(self):
        prior = {"status": "unavailable", "reason": "沒有前次決策。"}
        team = build_team_inputs(self.reports, None, self.stance, prior, self.slots)
        self.assertEqual(team["schema_version"], ROTATION_TEAM_VERSION)
        self.assertEqual(set(team), ROTATION_TEAM_FIELDS)

    def test_slots_without_prior_context_are_rejected(self):
        with self.assertRaises(DecisionToolError):
            build_team_inputs(self.reports, None, self.stance, None, self.slots)

    def test_omitting_slots_keeps_2_2(self):
        prior = {"status": "unavailable", "reason": "沒有前次決策。"}
        team = build_team_inputs(self.reports, None, self.stance, prior)
        self.assertEqual(team["schema_version"], "2.2")
        self.assertNotIn("portfolio_slots", team)


class FinalizerRotationChainTests(unittest.TestCase):
    """Finalizer 以最終 policy 重算 2.3 的名額脈絡；被改寫一律拒絕。"""

    def setUp(self):
        (self.bundle, self.momentum, self.debate, self.trade,
         _, self.policy, self.reports) = world()
        from test_team_chain import STANCE

        self.slots = build_portfolio_slots(self.bundle, self.policy, self.debate)
        self.prior = build_prior_decision_context(self.bundle, None)
        self.stance = STANCE

    def finalize(self, team):
        from test_team_chain import finalize

        return finalize(self.bundle, self.momentum, self.debate, self.trade, self.policy, team)

    def slot_errors(self, slots):
        """只取與名額脈絡有關的錯誤：本 fixture 的多空鏈是 2.0 形狀，其餘錯誤與本測試無關。"""
        team = build_team_inputs(self.reports, None, self.stance, self.prior, slots)
        _, result = self.finalize(team)
        return [error for error in result["errors"] if "portfolio_slots" in error]

    def test_rebuilt_slots_produce_no_slot_errors_in_the_chain(self):
        self.assertEqual(self.slot_errors(self.slots), [])

    def test_tampered_slots_are_rejected_by_the_finalizer(self):
        errors = self.slot_errors(dict(self.slots, free_slots=99))
        self.assertTrue(any("與重算結果不同" in error for error in errors), errors)

    def test_slots_are_not_checked_for_2_2(self):
        """2.2 沒有名額脈絡欄位，Finalizer 不得因此報錯（舊 run 重建不變）。"""
        team = build_team_inputs(self.reports, None, self.stance, self.prior)
        _, result = self.finalize(team)
        self.assertEqual([error for error in result["errors"] if "portfolio_slots" in error], [])


if __name__ == "__main__":
    unittest.main()
