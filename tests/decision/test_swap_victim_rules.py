"""換股換出者規則（position_sizing.agent_swap.victim_rules）：git 歷史中的 docs/plans/swap_victim_rules_plan.md 第 5、6 節。

覆蓋：Policy 驗證（形狀、值域、前提、人專用）、事實與簡報（thesis 資格改用 hold 信心上限與交易日、缺值 fail-closed、
exposure／quality_dip 資格不變、沒有閘門時逐鍵不變）、Validator 回饋（中性、並列兩種修法）、Finalizer 走同一個 Validator、
風險審查可引用清單含委託標的。合成世界沿用 test_swap_nomination 的三檔夾具。
"""

import copy
import json
import unittest
from pathlib import Path

from etf_agent.automation.risk_stages import review_citable_evidence_ids
from etf_agent.decision import swap_nomination as sn
from etf_agent.decision.sizing import (
    AGENT_SWAP_FIELDS, GATE_PREREQUISITES, validate_position_sizing, victim_rules_gate,
)
from etf_agent.decision.swap_nomination import SwapNominationValidator, needs_nomination, nomination_facts, seal_swap_nomination
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_agent_swap_policy import UNIVERSE, sizing_with
from test_exposure_swap import ExposureWorld, exposure_pair, output as exposure_output
from test_gate_prerequisites import decision_item
from test_portfolio_risk_decision import policy as base_policy
from test_swap_nomination import World, first_round_items, good_output

RULES = {"version": "v1", "thesis_max_hold_conviction": "low", "holding_days_basis": "trading_days"}
REVIEW_V2 = {
    "version": "v2", "loss_alert": "0.15", "retrace_alert_20d": None, "gain_alert": None,
    "trail_alert": None, "relative_loss_alert": None, "stale_alert": None, "context": "v1",
}


def rules(**changes):
    return dict(RULES, **changes)


def gated(mutate=None, victim_rules=None):
    """position_review v2（才有交易日數）加 victim_rules；``mutate`` 再改 policy。"""
    def apply(policy):
        policy["position_review"] = dict(REVIEW_V2)
        if victim_rules is not False:
            policy["position_sizing"]["agent_swap"]["victim_rules"] = victim_rules or rules()
        if mutate:
            mutate(policy)
    return apply


def low_items(conviction="low", **overrides):
    """2330 第一輪 hold，信心可調；2317 沒有空頭依據。"""
    items = {"2330.TW": decision_item("2330.TW", "hold", conviction, adopt=["bear-2330-1", "review:2330.TW:position"], reject=["bull-2330-1"])}
    items.update(overrides)
    return first_round_items(**items)


def policy_errors(sizing, review=None, version="v2"):
    policy = base_policy()
    policy["position_review"] = dict(REVIEW_V2) if review is None else review
    policy["position_sizing"] = sizing
    errors = []
    validate_position_sizing(policy, UNIVERSE, errors)
    return errors


def victim_errors(sizing, **kwargs):
    return [error for error in policy_errors(sizing, **kwargs) if "victim_rules" in error]


class PolicyGateTests(unittest.TestCase):
    def test_valid_shape_with_both_caps(self):
        for cap in ("low", "medium"):
            sizing = sizing_with("v2", victim_rules=rules(thesis_max_hold_conviction=cap))
            self.assertEqual(policy_errors(sizing), [], cap)

    def test_declared_human_only_and_forbidden_to_reflection(self):
        self.assertIn("victim_rules", AGENT_SWAP_FIELDS)
        rule = next(rule for rule in GATE_PREREQUISITES if rule.field == "agent_swap.victim_rules")
        self.assertTrue(rule.human_only)
        self.assertIn("agent_swap", rule.requires)
        self.assertIn("agent_swap", FORBIDDEN_PREFIXES)  # 整個 agent_swap（含子閘門）都在策略反思禁區

    def test_value_domain_and_shape_are_fail_closed(self):
        bad = {
            "high cap": rules(thesis_max_hold_conviction="high"),
            "unknown cap": rules(thesis_max_hold_conviction="none"),
            "calendar basis": rules(holding_days_basis="calendar_days"),
            "version": rules(version="v2"),
            "missing field": {"version": "v1", "thesis_max_hold_conviction": "low"},
            "extra field": dict(RULES, extra=1),
            "not an object": "v1",
            "null": None,
        }
        for label, value in bad.items():
            self.assertTrue(victim_errors(sizing_with("v2", victim_rules=value)), label)

    def test_requires_agent_swap_v2_and_position_review_v2(self):
        self.assertTrue(any("agent_swap v2" in e for e in victim_errors(sizing_with("v1", victim_rules=rules()))))
        v1_review = {"version": "v1", "loss_alert": "0.15", "retrace_alert_20d": None, "gain_alert": None}
        self.assertTrue(any("position_review.version=v2" in e for e in victim_errors(sizing_with("v2", victim_rules=rules()), review=v1_review)))

    def test_template_prefill_of_other_fields_still_rejected_and_absent_gate_is_valid(self):
        self.assertEqual(policy_errors(sizing_with("v2")), [])
        self.assertIsNone(victim_rules_gate(sizing_with("v2")))
        self.assertIsNone(victim_rules_gate(None))
        self.assertEqual(victim_rules_gate(sizing_with("v2", victim_rules=rules())), RULES)


class FactsAndBriefTests(unittest.TestCase):
    def victims(self, world):
        return {row["symbol"]: row for row in world.facts()["victims"]}

    def test_without_the_gate_facts_and_brief_are_unchanged_keys(self):
        world = World(low_items("medium"))
        row = self.victims(world)["2330.TW"]
        self.assertTrue(row["nominable"])  # 沒有閘門：medium 的 hold 仍可 thesis 提名（原行為）
        self.assertEqual(
            set(row), {"symbol", "hold_conviction", "weight", "holding_days", "rationale", "basis_ids", "reasons", "nominable"})
        brief = world.brief()
        self.assertNotIn("victim_rules", brief)
        for holding in brief["holdings"]:
            self.assertNotIn("holding_trading_days", holding)
            self.assertNotIn("victim_rules", json.dumps(holding))
        self.assertEqual(world.validate(good_output()), [])

    def test_gate_on_but_other_policy_unchanged_equals_old_facts_apart_from_added_keys(self):
        old = World(low_items("low"))
        new = World(low_items("low"), mutate_policy=gated())
        old_facts, new_facts = old.facts(), new.facts()
        for row in new_facts["victims"]:
            for key in ("holding_trading_days", "thesis_reasons", "thesis_nominable"):
                row.pop(key)
        self.assertEqual(old_facts, new_facts)

    def test_medium_and_high_hold_are_blocked_for_thesis_low_is_allowed(self):
        for conviction, blocked in (("high", True), ("medium", True), ("low", False)):
            world = World(low_items(conviction), mutate_policy=gated())
            row = self.victims(world)["2330.TW"]
            self.assertEqual(row["thesis_reasons"], ["hold_conviction_above_cap"] if blocked else [], conviction)
            self.assertEqual(row["thesis_nominable"], not blocked, conviction)
            self.assertEqual(needs_nomination(world.facts()), not blocked, conviction)

    def test_medium_cap_allows_medium_but_not_high(self):
        medium = gated(victim_rules=rules(thesis_max_hold_conviction="medium"))
        self.assertTrue(self.victims(World(low_items("medium"), mutate_policy=medium))["2330.TW"]["thesis_nominable"])
        self.assertFalse(self.victims(World(low_items("high"), mutate_policy=medium))["2330.TW"]["thesis_nominable"])

    def test_trading_days_replace_calendar_days_for_the_floor(self):
        base = World(low_items(), mutate_policy=gated())
        row = self.victims(base)["2330.TW"]
        self.assertEqual((row["holding_days"], row["holding_trading_days"]), (19, 18))  # 日曆 19 天、交易日 18 天
        # 下限 19：日曆天算夠、交易日不足，只在閘門開啟時被擋。
        floor = lambda policy: policy["position_sizing"]["agent_swap"].update(min_holding_days=19)
        self.assertEqual(self.victims(World(low_items(), mutate_policy=floor))["2330.TW"]["reasons"], [])
        gated_world = World(low_items(), mutate_policy=gated(floor))
        row = self.victims(gated_world)["2330.TW"]
        self.assertEqual(row["thesis_reasons"], ["holding_days_short"])
        self.assertEqual(row["reasons"], [])  # 舊口徑的欄位不變（quality_dip 資格不變）

    def test_missing_or_malformed_trading_days_fail_closed_as_short(self):
        for broken in (None, "18", True, 1.5):
            world = World(low_items(), mutate_policy=gated())
            for item in world.review["items"]:
                if broken is None:
                    item.pop("holding_trading_days", None)
                else:
                    item["holding_trading_days"] = broken
            row = {r["symbol"]: r for r in nomination_facts(*world.inputs())["victims"]}["2330.TW"]
            self.assertIn("holding_days_short", row["thesis_reasons"], broken)
            self.assertFalse(row["thesis_nominable"], broken)
            self.assertIsNone(row["holding_trading_days"], broken)

    def test_brief_adds_fields_only_with_the_gate(self):
        brief = World(low_items(), mutate_policy=gated()).brief()
        self.assertEqual(brief["victim_rules"]["thesis_max_hold_conviction"], "low")
        self.assertEqual(brief["victim_rules"]["holding_days_basis"], "trading_days")
        self.assertIn("purpose=exposure", brief["victim_rules"]["note"])
        holding = {row["symbol"]: row for row in brief["holdings"]}["2330.TW"]
        self.assertEqual(holding["holding_trading_days"], 18)
        self.assertTrue(holding["nominable"])
        blocked = World(low_items("medium"), mutate_policy=gated()).brief()
        holding = {row["symbol"]: row for row in blocked["holdings"]}["2330.TW"]
        self.assertFalse(holding["nominable"])
        self.assertEqual(holding["blocked_reasons"], ["hold_conviction_above_cap"])


class ValidatorTests(unittest.TestCase):
    def rejects(self, world, raw, *texts):
        errors = world.validate(raw)
        for text in texts:
            self.assertTrue(any(text in error for error in errors), (text, errors))
        return errors

    def test_medium_hold_thesis_pair_is_rejected_with_neutral_two_way_feedback(self):
        world = World(low_items("medium"), mutate_policy=gated())
        errors = self.rejects(world, good_output(), "hold_conviction_above_cap", "刪除該對")
        text = "".join(errors)
        self.assertNotIn("purpose=exposure", text)  # 沒有 exposure 閘門時不指向不存在的通道
        # 沒有閘門時同一份提名照舊合格（逐位元不變）。
        self.assertEqual(World(low_items("medium")).validate(good_output()), [])

    def test_feedback_lists_both_fixes_when_exposure_channel_exists(self):
        world = ExposureWorld(low_items("medium"), mutate_policy=gated())
        pair = dict(good_output()["nominations"][0], purpose="thesis")
        errors = self.rejects(world, exposure_output([pair]), "hold_conviction_above_cap", "purpose=exposure", "刪除該對")
        self.assertFalse(any("一定" in error or "必須改" in error for error in errors))

    def test_same_stock_with_exposure_purpose_passes(self):
        world = ExposureWorld(low_items("medium"), mutate_policy=gated())
        self.assertEqual(world.validate(exposure_output([exposure_pair("2330.TW")])), [])

    def test_low_hold_with_bear_basis_passes_and_high_is_rejected(self):
        self.assertEqual(World(low_items("low"), mutate_policy=gated()).validate(good_output()), [])
        self.rejects(World(low_items("high"), mutate_policy=gated()), good_output(), "hold_conviction_above_cap")

    def test_calendar_days_enough_but_trading_days_short_is_rejected(self):
        floor = lambda policy: policy["position_sizing"]["agent_swap"].update(min_holding_days=19)
        self.assertEqual(World(low_items(), mutate_policy=floor).validate(good_output()), [])
        self.rejects(World(low_items(), mutate_policy=gated(floor)), good_output(), "holding_days_short")

    def test_missing_trading_days_is_rejected_not_raised(self):
        world = World(low_items(), mutate_policy=gated())
        for item in world.review["items"]:
            item.pop("holding_trading_days", None)
        errors = world.validate(good_output())
        self.assertTrue(any("holding_days_short" in error for error in errors), errors)

    def test_exposure_purpose_eligibility_is_unchanged_by_the_gate(self):
        # 日曆 19 天、交易日 18 天：下限 19 時 exposure 仍照日曆天通過（資格不變）。
        floor = lambda policy: policy["position_sizing"]["agent_swap"].update(min_holding_days=19)
        world = ExposureWorld(low_items("high"), mutate_policy=gated(floor))
        row = {r["symbol"]: r for r in world.facts()["victims"]}["2330.TW"]
        self.assertTrue(row["exposure_nominable"])
        self.assertEqual(row["exposure_reasons"], [])

    def test_finalizer_rebuilds_with_the_same_validator(self):
        world = World(low_items("low"), mutate_policy=gated())
        nomination = seal_swap_nomination(world.envelope, good_output())
        self.assertEqual(world.validator().validate(nomination), [])
        self.assertEqual(sn.SwapNominationValidator(*world.inputs()).validate(nomination), [])
        # 同一份提名換成 medium 的第一輪（重建時的事實不同）一律拒絕：重建不信封存的字面。
        other = World(low_items("medium"), mutate_policy=gated())
        other_nomination = seal_swap_nomination(other.envelope, good_output())
        self.assertTrue(any("hold_conviction_above_cap" in e for e in other.validator().validate(other_nomination)))


class ReviewCitableEvidenceTests(unittest.TestCase):
    def test_sell_order_symbols_are_citable_even_when_not_held_after_allocation(self):
        world = World()
        rows = world.bundle["snapshot"]["latest_prices"]
        ids = {str(row["symbol"]).upper(): str(row["source_evidence_id"]) for row in rows}
        proposal = {
            "allocation_proposal": {"positions": [{"symbol": "2317.TW"}]},
            "order_proposal": {"orders": [{"symbol": "2330.TW", "side": "sell"}, {"symbol": "1101.TW", "side": "buy"}]},
        }
        citable = review_citable_evidence_ids(world.bundle, proposal)
        self.assertEqual(citable, sorted([ids["2317.TW"], ids["2330.TW"], ids["1101.TW"]]))

    def test_without_orders_it_is_the_old_held_only_list(self):
        world = World()
        ids = {str(row["symbol"]).upper(): str(row["source_evidence_id"]) for row in world.bundle["snapshot"]["latest_prices"]}
        proposal = {"allocation_proposal": {"positions": [{"symbol": "2317.TW"}]}}
        self.assertEqual(review_citable_evidence_ids(world.bundle, proposal), [ids["2317.TW"]])
        proposal["order_proposal"] = {"orders": []}
        self.assertEqual(review_citable_evidence_ids(world.bundle, proposal), [ids["2317.TW"]])

    def test_unknown_or_malformed_orders_add_nothing(self):
        world = World()
        proposal = {
            "allocation_proposal": {"positions": []},
            "order_proposal": {"orders": [{"symbol": "9999.TW", "side": "sell"}, "x", {"side": "sell"}]},
        }
        self.assertEqual(review_citable_evidence_ids(world.bundle, proposal), [])


class LiveConfigTests(unittest.TestCase):
    def test_the_live_policy_pins_victim_rules_v1_and_passes_the_policy_validation(self):
        # 2026-11-16：開啟 agent_swap.victim_rules（policy_id strategy-2026-11-16-victim-rules-1；回滾樣板 2026-11-16-before-victim-rules.json）。
        root = Path(__file__).resolve().parents[2]
        live = json.loads((root / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        # 2026-11-24 11:30（strategy-2026-11-24-agent-free-1）：thesis 換出者的 hold 信心上限 low→medium（使用者決定換股交給 Agent）。
        self.assertEqual(victim_rules_gate(live["position_sizing"]), dict(RULES, thesis_max_hold_conviction="medium"))
        self.assertEqual(live["position_review"]["version"], "v2")
        policy = copy.deepcopy(live)
        policy.setdefault("cash_weight_ceiling", "0.5")  # 正式檔沒有這個欄位（由 pipeline 依 rules 綁定），驗證需要一個數值
        errors = []
        validate_position_sizing(policy, UNIVERSE, errors)  # 整份正式 Policy（含真實的 position_review）逐項驗證
        self.assertEqual(errors, [])
        rollback = json.loads((root / "config" / "policy_versions" / "2026-11-16-before-victim-rules.json").read_text(encoding="utf-8"))
        self.assertIsNone(victim_rules_gate(rollback["position_sizing"]))


if __name__ == "__main__":
    unittest.main()
