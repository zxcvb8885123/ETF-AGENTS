"""持股檢視（``decision/position_review.py``）與 team_inputs 2.4 的確定性與 fail-closed 測試。"""

import copy
import unittest
from decimal import Decimal

from etf_agent.core import canonical_sha256
from etf_agent.decision import (
    TradeDecisionValidator,
    build_position_review,
    build_team_inputs,
    seal_trade_decision,
    trade_decision_envelope,
    validate_position_review,
)
from etf_agent.decision.fields import DecisionToolError, decision_bundle_sha256
from etf_agent.decision.finalization import REVIEW_TEAM_FIELDS, REVIEW_TEAM_VERSION
from etf_agent.decision.position_review import (
    ITEM_FIELDS,
    POSITION_REVIEW_VERSION,
    review_id,
    validate_position_review_policy,
)

from test_trader import STANCE, items, world

SETTINGS = {"version": "v1", "loss_alert": "0.10", "retrace_alert_20d": "0.12", "gain_alert": None}
LOSS_ID = review_id("2330.TW", "loss_alert")
RETRACE_ID = review_id("2330.TW", "retrace_alert")


def policy(**overrides):
    return {"position_review": {**SETTINGS, **overrides}}


def losing_bundle(bundle, average_cost="200"):
    """2330.TW 成本 200、現價 164，帳面 -18%。"""
    changed = copy.deepcopy(bundle)
    changed["account_snapshot"]["positions"][0]["average_cost"] = average_cost
    changed["bundle_sha256"] = decision_bundle_sha256(changed)
    return changed


def with_retrace(bundle, high="200.0"):
    """把近 20 根 K 棒中的一根高點拉高，製造從高點回落。"""
    changed = copy.deepcopy(bundle)
    series = next(row for row in changed["price_series"] if row["symbol"] == "2330.TW")
    series["bars"][-5]["high"] = high
    series["series_sha256"] = canonical_sha256(series["bars"])
    changed["bundle_sha256"] = decision_bundle_sha256(changed)
    return changed


def decide(bundle, debate, values):
    return seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), values)


class BuildPositionReviewTests(unittest.TestCase):
    def setUp(self):
        self.bundle, self.momentum, _, self.debate = world()

    def test_fields_and_numbers_are_recomputable(self):
        review = build_position_review(self.bundle, policy())
        self.assertEqual(review["schema_version"], POSITION_REVIEW_VERSION)
        (item,) = review["items"]
        self.assertEqual(set(item), ITEM_FIELDS)
        self.assertEqual(item["symbol"], "2330.TW")
        self.assertEqual(item["unrealized_return"], str((Decimal("164") / Decimal("120") - 1).quantize(Decimal("0.000001"))))
        self.assertEqual(item["evidence_ids"], ["account-evidence-1", "price-2330"])
        self.assertEqual(item["flags"], [])

    def test_loss_alert_triggers_below_threshold(self):
        review = build_position_review(losing_bundle(self.bundle), policy())
        self.assertEqual(review["items"][0]["flags"], [{"review_id": LOSS_ID, "flag": "loss_alert"}])

    def test_retrace_alert_uses_recent_high(self):
        review = build_position_review(with_retrace(self.bundle), policy())
        item = review["items"][0]
        self.assertEqual(item["flags"], [{"review_id": RETRACE_ID, "flag": "retrace_alert"}])
        self.assertLess(Decimal(item["retrace_from_high_20d"]), Decimal("-0.12"))

    def test_gain_alert_is_off_when_null_and_on_when_set(self):
        self.assertEqual(build_position_review(self.bundle, policy())["items"][0]["flags"], [])
        flagged = build_position_review(self.bundle, policy(gain_alert="0.30"))["items"][0]["flags"]
        self.assertEqual([flag["flag"] for flag in flagged], ["gain_alert"])

    def test_missing_latest_price_fails_closed(self):
        broken = copy.deepcopy(self.bundle)
        broken["snapshot"]["latest_prices"] = [row for row in broken["snapshot"]["latest_prices"] if row["symbol"] != "2330.TW"]
        with self.assertRaises(DecisionToolError):
            build_position_review(broken, policy())

    def test_insufficient_bars_give_no_retrace_value(self):
        short = copy.deepcopy(self.bundle)
        for row in short["price_series"]:
            row["bars"] = row["bars"][-5:]
        item = build_position_review(short, policy())["items"][0]
        self.assertIsNone(item["retrace_from_high_20d"])

    def test_short_window_retrace_and_position_id_are_always_given(self):
        item = build_position_review(with_retrace(self.bundle), policy())["items"][0]
        self.assertEqual(item["position_id"], "review:2330.TW:position")
        self.assertIsNotNone(item["retrace_from_high_5d"])
        short = copy.deepcopy(self.bundle)
        for row in short["price_series"]:
            row["bars"] = row["bars"][-3:]
        self.assertIsNone(build_position_review(short, policy())["items"][0]["retrace_from_high_5d"])

    def test_all_thresholds_off_gives_facts_without_flags(self):
        off = policy(loss_alert=None, retrace_alert_20d=None, gain_alert=None)
        item = build_position_review(losing_bundle(self.bundle), off)["items"][0]
        self.assertEqual(item["flags"], [])
        self.assertLess(Decimal(item["unrealized_return"]), 0)

    def test_invalid_policy_is_rejected(self):
        for bad in (policy(version="v2"), policy(loss_alert="1.5"), policy(loss_alert="0")):
            errors = []
            validate_position_review_policy(bad["position_review"], errors)
            self.assertTrue(errors, bad)
        errors = []
        validate_position_review_policy({"version": "v1", "loss_alert": "0.1"}, errors)
        self.assertTrue(errors)


class ValidatePositionReviewTests(unittest.TestCase):
    def setUp(self):
        self.bundle, _, _, _ = world()
        self.policy = policy()
        self.review = build_position_review(self.bundle, self.policy)

    def errors_for(self, review):
        errors = []
        validate_position_review(self.bundle, self.policy, review, errors)
        return errors

    def test_rebuilt_review_passes(self):
        self.assertEqual(self.errors_for(self.review), [])

    def test_tampered_value_is_rejected(self):
        changed = copy.deepcopy(self.review)
        changed["items"][0]["unrealized_return"] = "0.000000"
        self.assertTrue(any("與重算結果不同" in error for error in self.errors_for(changed)))

    def test_tampered_flags_are_rejected(self):
        changed = copy.deepcopy(self.review)
        changed["items"][0]["flags"] = [{"review_id": LOSS_ID, "flag": "loss_alert"}]
        self.assertTrue(self.errors_for(changed))

    def test_unknown_field_and_non_mapping_are_rejected(self):
        self.assertTrue(self.errors_for(dict(self.review, suggested_exit="2330.TW")))
        self.assertTrue(any("必須是物件" in error for error in self.errors_for("x")))


class TradeValidatorReviewTests(unittest.TestCase):
    def setUp(self):
        bundle, self.momentum, _, self.debate = world()
        self.bundle = losing_bundle(bundle)
        self.review = build_position_review(self.bundle, policy())

    def validate(self, values, review="default"):
        review = self.review if review == "default" else review
        decision = decide(self.bundle, self.debate, values)
        return TradeDecisionValidator(self.bundle, self.momentum, self.debate, position_review=review).validate(decision)

    def values(self, **changes):
        values = items()
        values[1].update(changes)
        return values

    def test_hold_with_bull_claim_and_review_passes(self):
        values = self.values(adopted_claim_ids=["bull-2330-1", LOSS_ID], rejected_claim_ids=["bear-2330-1"])
        self.assertEqual(self.validate(values), [])

    def test_unhandled_review_is_rejected(self):
        self.assertTrue(any("未處理全部 claim" in error for error in self.validate(items())))

    def test_review_cannot_be_rejected(self):
        values = self.values(adopted_claim_ids=["bull-2330-1"], rejected_claim_ids=["bear-2330-1", LOSS_ID])
        self.assertTrue(any("只能採納不可否決" in error for error in self.validate(values)))

    def test_loss_alert_hold_without_bull_claim_is_rejected(self):
        values = self.values(adopted_claim_ids=["bear-2330-1", LOSS_ID], rejected_claim_ids=["bull-2330-1"])
        self.assertTrue(any("虧損警示" in error for error in self.validate(values)))

    POSITION_ID = "review:2330.TW:position"

    def test_position_fact_is_optional_for_holding(self):
        values = self.values(adopted_claim_ids=["bull-2330-1", LOSS_ID], rejected_claim_ids=["bear-2330-1"])
        self.assertEqual(self.validate(values), [])  # 不處理 position 編號也通過

    def test_exit_may_rest_on_the_agents_own_price_judgment(self):
        values = self.values(intent="exit", adopted_claim_ids=[self.POSITION_ID, LOSS_ID], rejected_claim_ids=["bull-2330-1", "bear-2330-1"])
        self.assertEqual(self.validate(values), [])

    def test_other_symbols_position_id_is_rejected(self):
        values = self.values(adopted_claim_ids=["bull-2330-1", LOSS_ID, "review:2317.TW:position"], rejected_claim_ids=["bear-2330-1"])
        self.assertTrue(any("引用不屬於該股票" in error for error in self.validate(values)))

    def test_duplicate_review_id_is_rejected(self):
        values = self.values(adopted_claim_ids=["bull-2330-1", LOSS_ID, LOSS_ID], rejected_claim_ids=["bear-2330-1"])
        self.assertTrue(any("剛好處理一次" in error for error in self.validate(values)))

    def test_exit_may_use_review_instead_of_bear_claim(self):
        values = self.values(
            intent="exit", adopted_claim_ids=[LOSS_ID], rejected_claim_ids=["bull-2330-1", "bear-2330-1"],
        )
        self.assertEqual(self.validate(values), [])

    def test_exit_without_bear_or_review_is_rejected(self):
        values = self.values(
            intent="exit", adopted_claim_ids=["bull-2330-1"], rejected_claim_ids=["bear-2330-1", LOSS_ID],
        )
        errors = self.validate(values)
        self.assertTrue(any("空頭 claim、持股檢視警示或持股事實" in error for error in errors), errors)

    def test_other_symbols_review_id_is_rejected(self):
        values = self.values(adopted_claim_ids=["bull-2330-1", LOSS_ID], rejected_claim_ids=["bear-2330-1"])
        values[0]["adopted_claim_ids"].append(LOSS_ID)
        self.assertTrue(any("不屬於該股票" in error for error in self.validate(values)))

    def test_without_review_old_behaviour_is_unchanged(self):
        self.assertEqual(self.validate(items(), review=None), [])
        values = self.values(adopted_claim_ids=["bull-2330-1", LOSS_ID], rejected_claim_ids=["bear-2330-1"])
        self.assertTrue(any("不屬於該股票" in error for error in self.validate(values, review=None)))

    def test_retrace_alert_alone_does_not_force_bull_claim(self):
        bundle, momentum, _, debate = world()
        bundle = with_retrace(bundle)
        review = build_position_review(bundle, policy())
        values = items()
        values[1].update(intent="hold", adopted_claim_ids=["bear-2330-1", RETRACE_ID], rejected_claim_ids=["bull-2330-1"])
        decision = decide(bundle, debate, values)
        errors = TradeDecisionValidator(bundle, momentum, debate, position_review=review).validate(decision)
        self.assertEqual(errors, [])


class TeamInputsReviewVersionTests(unittest.TestCase):
    def setUp(self):
        self.bundle, _, self.reports, _ = world()
        self.review = build_position_review(self.bundle, policy())

    def test_review_requires_slots(self):
        with self.assertRaises(DecisionToolError):
            build_team_inputs(self.reports, None, STANCE, {"status": "unavailable"}, None, self.review)

    def test_review_promotes_team_inputs_to_2_4(self):
        team = build_team_inputs(self.reports, None, STANCE, {"status": "unavailable"}, {"free_slots": 1}, self.review)
        self.assertEqual(team["schema_version"], REVIEW_TEAM_VERSION)
        self.assertEqual(set(team), REVIEW_TEAM_FIELDS)

    def test_without_review_stays_2_3(self):
        team = build_team_inputs(self.reports, None, STANCE, {"status": "unavailable"}, {"free_slots": 1})
        self.assertEqual(team["schema_version"], "2.3")
        self.assertNotIn("position_review", team)


class FinalizerReviewChainTests(unittest.TestCase):
    """Finalizer 以 policy 重算 2.4 的持股檢視；被改寫、policy 與版本不一致一律拒絕。"""

    def setUp(self):
        from test_rotation_slots import world as rotation_world
        from etf_agent.decision import build_portfolio_slots, build_prior_decision_context, decision_policy_sha256

        (self.bundle, self.momentum, self.debate, self.trade,
         _, policy_, self.reports) = rotation_world()
        self.policy = dict(policy_, position_review=dict(SETTINGS))
        self.policy["content_sha256"] = decision_policy_sha256(self.policy)
        self.slots = build_portfolio_slots(self.bundle, self.policy, self.debate)
        self.prior = build_prior_decision_context(self.bundle, None)
        self.review = build_position_review(self.bundle, self.policy)

    def review_errors(self, team, policy=None):
        from test_team_chain import finalize

        _, result = finalize(self.bundle, self.momentum, self.debate, self.trade, policy or self.policy, team)
        return [error for error in result["errors"] if "position_review" in error]

    def test_rebuilt_review_produces_no_review_errors(self):
        team = build_team_inputs(self.reports, None, STANCE, self.prior, self.slots, self.review)
        self.assertEqual(self.review_errors(team), [])

    def test_tampered_review_is_rejected(self):
        changed = copy.deepcopy(self.review)
        changed["items"][0]["unrealized_return"] = "0.500000"
        team = build_team_inputs(self.reports, None, STANCE, self.prior, self.slots, changed)
        self.assertTrue(any("與重算結果不同" in error for error in self.review_errors(team)))

    def test_policy_with_review_but_team_2_3_is_rejected(self):
        team = build_team_inputs(self.reports, None, STANCE, self.prior, self.slots)
        self.assertTrue(any("必須與 team_inputs 2.4 同時存在" in error for error in self.review_errors(team)))

    def test_team_2_4_without_policy_review_is_rejected(self):
        from etf_agent.decision import decision_policy_sha256

        bare = {key: value for key, value in self.policy.items() if key != "position_review"}
        bare["content_sha256"] = decision_policy_sha256(bare)
        team = build_team_inputs(self.reports, None, STANCE, self.prior, self.slots, self.review)
        self.assertTrue(self.review_errors(team, bare))


if __name__ == "__main__":
    unittest.main()
