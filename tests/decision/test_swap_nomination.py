"""換股提名（5c）的契約、事實、Validator 與論點提示綁定：純單元，不碰 pipeline。

世界：三檔交易池（1101／2317／2330），持有 2317 與 2330 各一張，現金 100 萬；交易 Agent 第一輪：
1101 buy（medium，採納多頭 claim）、2317 hold（只採納多頭，沒有空頭依據）、2330 hold（採納空頭 claim 與持股事實）。
名額刻意設為 2 檔上限，廣度最窄 1 檔，所以 1101 被名額卡住、需要提名 2330 讓位。
"""

import copy
import json
import unittest
from decimal import Decimal

from etf_agent.automation.daily_pipeline import degraded_research_result
from etf_agent.decision import (
    DecisionToolError,
    MomentumEngine,
    build_research_debate,
    build_stance_brief,
    seal_stance_packet,
    seal_trade_decision,
    sentiment_unavailable_report,
    trade_decision_envelope,
)
from etf_agent.decision.position_review import build_position_review
from etf_agent.decision.selection_ranking import ranking_envelope, seal_selection_ranking
from etf_agent.decision import swap_nomination as sn
from etf_agent.decision.swap_nomination import (
    SwapEstimator, SwapNominationValidator, apply_swap_nomination, bind_thesis_hints, nomination_envelope, nomination_facts, nominations_of,
    needs_nomination, program_nomination, seal_swap_nomination, swap_nomination_brief, validate_team_swap_nomination, validate_thesis_hint_binding,
)

from test_analysts import event_items, finding, report, technical_items
from test_gate_prerequisites import decision_item, three_symbol_bundle
from test_portfolio_risk_decision import policy as base_policy

HELD = {"2317.TW": 1000, "2330.TW": 1000}
REVIEW_POLICY = {"version": "v1", "loss_alert": "0.15", "retrace_alert_20d": None, "gain_alert": None}
VERDICTS = {
    "verdicts_id": "tv-1", "as_of": "2026-09-14T10:00:00+08:00", "sha256": "b" * 64,
    "items": [
        {"symbol": "2330.TW", "verdict": "broken", "summary": "當初的成長理由已不成立。"},
        {"symbol": "2317.TW", "verdict": "intact", "summary": "理由仍成立。"},
    ],
}


def claim(claim_id, evidence):
    return {"claim_id": claim_id, "text": "依價格證據：%s。" % claim_id, "evidence_ids": [evidence], "finding_ids": []}


def build_debate(bundle):
    momentum = MomentumEngine(bundle).run()
    technical = [
        {"symbol": "1101.TW", "outlook": "positive", "findings": [finding("tech-1101-1", "price-1101")], "data_gaps": []},
        *technical_items(),
    ]
    unknown = [
        {"symbol": symbol, "outlook": "unknown", "findings": [], "data_gaps": ["NO_FINANCIAL_STATEMENTS"]}
        for symbol in ("1101.TW", "2317.TW", "2330.TW")
    ]
    events = [
        {"symbol": "1101.TW", "outlook": "unknown", "findings": [], "data_gaps": ["NO_MATERIAL_EVENT_IN_WINDOW"], "events": []},
        *event_items(),
    ]
    reports = {
        "technical": report(bundle, "technical", technical),
        "fundamental": report(bundle, "fundamental", unknown),
        "event": report(bundle, "event", events),
        "sentiment": sentiment_unavailable_report(bundle),
    }
    research = degraded_research_result(bundle["snapshot"], "research-1")
    bull_items = [
        {"symbol": "1101.TW", "strength": "moderate", "claims": [claim("bull-1101-1", "price-1101")], "invalidation_conditions": []},
        {"symbol": "2317.TW", "strength": "moderate", "claims": [claim("bull-2317-1", "price-2317")], "invalidation_conditions": []},
        {"symbol": "2330.TW", "strength": "weak", "claims": [claim("bull-2330-1", "price-2330")], "invalidation_conditions": []},
    ]
    bear_items = [
        {"symbol": symbol, "strength": "none", "claims": [], "invalidation_conditions": []} for symbol in ("1101.TW", "2317.TW")
    ] + [{"symbol": "2330.TW", "strength": "moderate", "claims": [claim("bear-2330-1", "price-2330")], "invalidation_conditions": []}]
    bull = seal_stance_packet(build_stance_brief(bundle, momentum, reports, research, "bull")["packet_envelope"], bull_items)
    bear = seal_stance_packet(build_stance_brief(bundle, momentum, reports, research, "bear")["packet_envelope"], bear_items)
    return build_research_debate(bundle, bull, bear, "debate-3")


def first_round_items(sell_adopted=("bear-2330-1", "review:2330.TW:position"), sell_rejected=("bull-2330-1",), **overrides):
    items = {
        "1101.TW": decision_item("1101.TW", "buy", "medium", adopt=["bull-1101-1"]),
        "2317.TW": decision_item("2317.TW", "hold", "low", adopt=["bull-2317-1"]),
        "2330.TW": decision_item("2330.TW", "hold", "medium", adopt=sell_adopted, reject=sell_rejected),
    }
    items.update(overrides)
    return list(items.values())


def policy_for(bundle, held_since="2026-09-01T09:00:00+08:00", **gate_changes):
    policy = copy.deepcopy(base_policy())
    policy.update(max_positions=2, min_positions=1, position_review=dict(REVIEW_POLICY))
    policy["sector_by_symbol"] = {symbol: "x" for symbol in ("1101.TW", "2317.TW", "2330.TW")}
    policy["position_sizing"] = {
        "rotation": {"version": "v1", "max_swaps_per_run": 5, "min_new_conviction": "medium"},
        "positions_by_breadth": {"concentrated": 1, "balanced": 2, "diversified": 2},
        "reentry_cooldown": {"version": "v1", "days": 3, "blocked_symbols": [], "source_run_ids": []},
        "exposure_rotation": {"held_since_by_symbol": {symbol: held_since for symbol in sorted(HELD)}},
        "cash_buffer_by_stance": {"aggressive": "0.03", "neutral": "0.10", "defensive": "0.20"},
        "agent_swap": dict({"version": "v1", "max_swaps_per_run": 2, "min_holding_days": 5}, **gate_changes),
    }
    return policy


class World:
    """一組互相一致的輸入。``mutate`` 讓個別測試改 policy／bundle／第一輪決策後重建。"""

    def __init__(self, items=None, mutate_policy=None, mutate_bundle=None, cash=1_000_000, **rules):
        self.bundle = three_symbol_bundle(cash, HELD, **rules)
        self.debate = build_debate(self.bundle)  # 先用合法 bundle 建立辯論；之後的改動只影響事實計算，不重算雜湊
        if mutate_bundle:
            mutate_bundle(self.bundle)
        self.policy = policy_for(self.bundle)
        if mutate_policy:
            mutate_policy(self.policy)
        self.trade = seal_trade_decision(trade_decision_envelope(self.bundle, self.debate, "trade-3"), items or first_round_items())
        tiers = [{"conviction": "medium", "ordered": [{"symbol": "1101.TW", "reason": "唯一候選"}]}]
        self.ranking = seal_selection_ranking(ranking_envelope(self.bundle, self.trade, "rank-1"), tiers)
        self.review = build_position_review(self.bundle, self.policy)
        self.exposure = {"items": [
            {"symbol": symbol, "weight": "0.100000", "beta": "1.100000", "up_capture": "1.000000", "down_capture": "0.900000"}
            for symbol in sorted(HELD)
        ]}
        self.envelope = nomination_envelope(self.bundle, self.trade, self.ranking, "swap-1")

    def inputs(self):
        return (self.bundle, self.policy, self.trade, self.ranking, self.debate, self.review, self.exposure)

    def facts(self):
        return nomination_facts(*self.inputs())

    def validator(self, prior_context=None):
        return SwapNominationValidator(*self.inputs(), prior_context=prior_context)

    def brief(self, verdicts=None):
        return swap_nomination_brief(*self.inputs(), verdicts=verdicts)

    def validate(self, raw, prior_context=None):
        return self.validator(prior_context).validate(seal_swap_nomination(self.envelope, raw))


def good_output(**changes):
    output = {
        "nominations": [{
            "buy_symbol": "1101.TW", "sell_symbol": "2330.TW", "buy_basis_ids": ["bull-1101-1"],
            "sell_basis_ids": ["bear-2330-1", "review:2330.TW:position"], "reason": "新股的多頭論點比這檔持股的剩餘論點更完整。",
        }],
        "thesis_hint_responses": [],
    }
    output.update(changes)
    return output


def with_pair(**changes):
    pair = dict(good_output()["nominations"][0], **changes)
    return good_output(nominations=[pair])


class FactsTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def test_candidates_victims_and_cap(self):
        facts = self.world.facts()
        self.assertEqual([(row["symbol"], row["nominable"], row["basis_ids"]) for row in facts["candidates"]],
                         [("1101.TW", True, ["bull-1101-1"])])
        victims = {row["symbol"]: row for row in facts["victims"]}
        self.assertEqual(victims["2330.TW"]["basis_ids"], ["bear-2330-1", "review:2330.TW:position"])
        self.assertTrue(victims["2330.TW"]["nominable"])
        self.assertEqual((victims["2317.TW"]["nominable"], victims["2317.TW"]["reasons"]), (False, ["no_bear_basis"]))
        self.assertEqual((facts["pair_cap"], facts["conservative_free_slots"], facts["first_round_exits"]), (2, 0, []))
        self.assertEqual(victims["2330.TW"]["holding_days"], 19)
        self.assertTrue(needs_nomination(facts))

    def test_rejected_claims_are_not_basis(self):
        world = World(first_round_items(sell_adopted=("review:2330.TW:position",), sell_rejected=("bear-2330-1", "bull-2330-1")))
        victims = {row["symbol"]: row for row in world.facts()["victims"]}
        self.assertEqual(victims["2330.TW"]["basis_ids"], ["review:2330.TW:position"])
        world = World(first_round_items(sell_adopted=("bull-2330-1",), sell_rejected=("bear-2330-1",)))
        self.assertEqual(
            {row["symbol"]: row["reasons"] for row in world.facts()["victims"]}["2330.TW"], ["no_bear_basis"])
        self.assertFalse(needs_nomination(world.facts()))

    def test_candidate_blocking_reasons(self):
        def tradability(bundle):
            bundle["trading_status_bundle"] = {"bundle_id": "t"}
            bundle["tradability_assessment"] = {"symbols": [{"symbol": "1101.TW", "state": "suspended"}]}

        def cooled(policy):
            policy["position_sizing"]["reentry_cooldown"]["blocked_symbols"] = ["1101.TW"]

        def strict(policy):
            policy["position_sizing"]["rotation"]["min_new_conviction"] = "high"

        for label, kwargs, expected in (
            ("tradable", dict(mutate_bundle=tradability), "not_tradable"),
            ("cooldown", dict(mutate_policy=cooled), "reentry_cooldown"),
            ("conviction", dict(mutate_policy=strict), "conviction_below_threshold"),
        ):
            row = World(**kwargs).facts()["candidates"][0]
            self.assertEqual((row["nominable"], row["reasons"]), (False, [expected]), label)

        def no_price(bundle):
            bundle["snapshot"]["latest_prices"] = [r for r in bundle["snapshot"]["latest_prices"] if r["symbol"] != "1101.TW"]

        self.assertEqual(World(mutate_bundle=no_price).facts()["candidates"][0]["reasons"], ["no_price"])

    def test_victim_blocking_reasons(self):
        def untradable(bundle):
            bundle["trading_status_bundle"] = {"bundle_id": "t"}
            bundle["tradability_assessment"] = {"symbols": [{"symbol": "2330.TW", "state": "restricted"}]}

        victims = {row["symbol"]: row for row in World(mutate_bundle=untradable).facts()["victims"]}
        self.assertEqual(victims["2330.TW"]["reasons"], ["not_tradable"])
        young = World(mutate_policy=lambda policy: policy["position_sizing"]["agent_swap"].update(min_holding_days=30))
        self.assertEqual({r["symbol"]: r["reasons"] for r in young.facts()["victims"]}["2330.TW"], ["holding_days_short"])
        recent = World(mutate_policy=lambda policy: policy["position_sizing"]["exposure_rotation"]["held_since_by_symbol"].update(
            {"2330.TW": "2026-09-18T09:00:00+08:00"}))
        self.assertEqual({r["symbol"]: r["holding_days"] for r in recent.facts()["victims"]}["2330.TW"], 2)

    def test_not_needed_rules(self):
        def breadth(narrowest, maximum):
            def mutate(policy):
                policy["max_positions"] = maximum
                policy["position_sizing"]["positions_by_breadth"] = {"concentrated": narrowest, "balanced": maximum, "diversified": maximum}
            return mutate

        # 持股 2 檔、最窄廣度 2：沒有空名額，需要提名；最窄廣度 3：還有 1 個空名額，可提名新股 1 檔不多於空名額，不需要。
        self.assertTrue(needs_nomination(World(mutate_policy=breadth(2, 3)).facts()))
        self.assertEqual(World(mutate_policy=breadth(2, 3)).facts()["conservative_free_slots"], 0)
        self.assertFalse(needs_nomination(World(mutate_policy=breadth(3, 3)).facts()))
        # 廣度最窄 1（低於持股數）仍會呼叫：空名額夾在 0。
        self.assertTrue(needs_nomination(World(mutate_policy=breadth(1, 3)).facts()))

    def test_pair_cap_subtracts_first_round_exits_and_zero_means_not_needed(self):
        items = first_round_items(**{"2317.TW": decision_item("2317.TW", "exit", None, adopt=["review:2317.TW:position"])})
        world = World(items, mutate_policy=lambda policy: policy["position_sizing"]["rotation"].update(max_swaps_per_run=1))
        facts = world.facts()
        self.assertEqual((facts["first_round_exits"], facts["pair_cap"]), (["2317.TW"], 0))
        self.assertFalse(needs_nomination(facts))
        wider = World(items, mutate_policy=lambda policy: policy["position_sizing"]["rotation"].update(max_swaps_per_run=2))
        self.assertEqual(wider.facts()["pair_cap"], 1)
        capped = World(mutate_policy=lambda policy: policy["position_sizing"]["agent_swap"].update(max_swaps_per_run=1))
        self.assertEqual(capped.facts()["pair_cap"], 1)

    def test_untradable_exit_does_not_release_a_slot(self):
        items = first_round_items(**{"2317.TW": decision_item("2317.TW", "exit", None, adopt=["review:2317.TW:position"])})

        def untradable(bundle):
            bundle["trading_status_bundle"] = {"bundle_id": "t"}
            bundle["tradability_assessment"] = {"symbols": [{"symbol": "2317.TW", "state": "restricted"}]}

        self.assertEqual(World(items, mutate_bundle=untradable).facts()["first_round_exits"], [])

    def test_required_hint_responses_are_bound_held_and_hold_only(self):
        world = World(mutate_policy=lambda policy: policy["position_sizing"]["agent_swap"].update(
            thesis_hint_symbols=["1101.TW", "2317.TW", "2330.TW", "9999.TW"], verdicts_ref={"verdicts_id": "x", "as_of": "y", "sha256": "z"}))
        self.assertEqual(world.facts()["required_hint_responses"], ["2317.TW", "2330.TW"])

    def test_missing_gate_or_inputs_fail_closed(self):
        world = World()
        policy = copy.deepcopy(world.policy)
        del policy["position_sizing"]["agent_swap"]
        with self.assertRaises(DecisionToolError):
            nomination_facts(world.bundle, policy, world.trade, world.ranking, world.debate, world.review, world.exposure)
        with self.assertRaises(DecisionToolError):
            nomination_facts(world.bundle, world.policy, world.trade, world.ranking, world.debate, None, world.exposure)
        policy = copy.deepcopy(world.policy)
        del policy["position_sizing"]["exposure_rotation"]["held_since_by_symbol"]
        with self.assertRaises(DecisionToolError):
            nomination_facts(world.bundle, policy, world.trade, world.ranking, world.debate, world.review, world.exposure)


class BriefTests(unittest.TestCase):
    def test_brief_shape_and_citable_background(self):
        brief = World().brief()
        self.assertEqual(brief["pair_limit"], 2)
        self.assertEqual([row["symbol"] for row in brief["candidates"]], ["1101.TW"])
        self.assertEqual(brief["candidates"][0]["buy_basis"][0]["id"], "bull-1101-1")
        self.assertIn("1101.TW", json.dumps(brief, ensure_ascii=False))
        holding = {row["symbol"]: row for row in brief["holdings"]}["2330.TW"]
        self.assertEqual({row["id"]: row["kind"] for row in holding["sell_basis"]},
                         {"bear-2330-1": "bear", "review:2330.TW:position": "position"})
        self.assertFalse(holding["background"]["citable"])
        self.assertFalse(brief["background"]["citable"])
        self.assertIn("稍後決定", brief["background"]["breadth_note"])
        self.assertGreaterEqual(brief["background"]["affordable_lots_estimate"], 1)
        self.assertEqual(brief["candidates"][0]["one_lot_cost"], "100243")

    def test_shadow_and_formal_versions_get_a_byte_identical_brief(self):
        v1 = World().brief()
        v2 = World(mutate_policy=lambda policy: policy["position_sizing"]["agent_swap"].update(version="v2")).brief()
        self.assertEqual(json.dumps(v1, sort_keys=True), json.dumps(v2, sort_keys=True))
        text = json.dumps(v1, ensure_ascii=False)
        for forbidden in ("shadow", "影子", "v1", "v2"):
            self.assertNotIn(forbidden, text.replace("one_lot_cost", ""), forbidden)

    def test_hint_summary_only_when_verdicts_match_the_bound_reference(self):
        ref = {"verdicts_id": "tv-1", "as_of": VERDICTS["as_of"], "sha256": VERDICTS["sha256"]}
        world = World(mutate_policy=lambda policy: policy["position_sizing"]["agent_swap"].update(
            thesis_hint_symbols=["2330.TW"], verdicts_ref=ref))
        hint = world.brief(VERDICTS)["thesis_hints"][0]
        self.assertEqual((hint["symbol"], hint["citable"]), ("2330.TW", False))
        self.assertIn("weekly_review_summary", hint)
        self.assertNotIn("weekly_review_summary", world.brief(dict(VERDICTS, sha256="c" * 64))["thesis_hints"][0])
        self.assertNotIn("weekly_review_summary", world.brief()["thesis_hints"][0])

    def test_blocked_candidates_are_listed_with_reasons_but_not_nominable(self):
        brief = World(mutate_policy=lambda policy: policy["position_sizing"]["rotation"].update(min_new_conviction="high")).brief()
        row = brief["candidates"][0]
        self.assertEqual((row["nominable"], row["blocked_reasons"]), (False, ["conviction_below_threshold"]))
        self.assertNotIn("one_lot_cost", row)


class EstimatorTests(unittest.TestCase):
    def test_known_numbers(self):
        world = World()
        estimator = SwapEstimator(world.bundle, world.policy, world.trade)
        # 100 元、滑價 10 bps → 100.10；一張 100,100，手續費 0.1425% = 142.64 進位 143。
        self.assertEqual(estimator.buy_one_lot_cost("1101.TW"), Decimal("100243"))
        # 2330 以 164 元、滑價 -10 bps → 163.84；賣一張 163,840 − 手續費 234 − 證交稅 492。
        self.assertEqual(estimator.sell_net("2330.TW", 1000), Decimal("163114"))
        self.assertEqual(estimator.first_round_sells, {})
        self.assertTrue(estimator.reuse)

    def test_gap_reserve_and_cash_buffer(self):
        def mutate(policy):
            policy["position_sizing"]["buy_gap_reserve"] = "v1"
            policy["position_sizing"]["buy_gap_reserve_rate"] = "0.03"

        world = World(mutate_policy=mutate)
        estimator = SwapEstimator(world.bundle, world.policy, world.trade)
        self.assertEqual(estimator.buy_one_lot_cost("1101.TW"), Decimal("100243") * Decimal("1.03"))
        self.assertEqual(estimator.cash_ratio, Decimal("0.20"))  # 沒有 cash_ratio_range：各姿態緩衝的最大值
        self.assertLess(estimator.usable_cash(), estimator.settled_cash)

    def test_cash_check_follows_the_list_order_and_reuse_flag(self):
        poor = World(cash=60_000, mutate_policy=lambda policy: policy.update(reuse_sell_proceeds=False))
        pairs = [("1101.TW", "2330.TW")]
        errors = SwapEstimator(poor.bundle, poor.policy, poor.trade).check_pairs(pairs)
        self.assertTrue(any("買不起" in error for error in errors), errors)
        reused = World(cash=300_000)
        self.assertEqual(SwapEstimator(reused.bundle, reused.policy, reused.trade).check_pairs(pairs), [])

    def test_turnover_lower_bound(self):
        world = World(mutate_policy=lambda policy: policy.update(max_turnover_rate="0.05"))
        errors = SwapEstimator(world.bundle, world.policy, world.trade).check_pairs([("1101.TW", "2330.TW")])
        self.assertTrue(any("換手上限" in error for error in errors), errors)


class ProductionCashPathTests(unittest.TestCase):
    GRID = {
        "version": "v1",
        "grid": {
            "aggressive": {"lower": "0.03", "middle": "0.05", "upper": "0.08"},
            "neutral": {"lower": "0.08", "middle": "0.10", "upper": "0.13"},
            "defensive": {"lower": "0.15", "middle": "0.18", "upper": "0.21"},
        },
        "max_daily_raise": "0.03",
    }

    def world(self, anchor=True):
        def mutate(policy):
            gate = copy.deepcopy(self.GRID)
            if anchor:
                gate["anchor"] = {"status": "account_cash", "ratio": "0.10000000", "source": None}
            policy["position_sizing"]["cash_ratio_range"] = gate

        return World(mutate_policy=mutate)

    def test_highest_selectable_grid_point_today_is_used_as_the_buffer(self):
        world = self.world()
        estimator = SwapEstimator(world.bundle, world.policy, world.trade)
        # anchor 0.10 + 每日 0.03 = 0.13：neutral.upper 0.13 可選；aggressive 全可選；defensive 只有 lower 0.15（切換姿態永遠可選 lower）。
        self.assertEqual(estimator.cash_ratio, Decimal("0.15"))
        self.assertEqual(world.validate(good_output()), [])

    def test_missing_anchor_is_reported_not_raised(self):
        world = self.world(anchor=False)
        errors = world.validate(good_output())
        self.assertTrue(any("無法以共同輸入重算事實" in error or "無法估計提名的保守現金與換手" in error for error in errors), errors)


class FinalPolicyInvarianceTests(unittest.TestCase):
    def test_bindings_written_after_the_risk_agent_do_not_change_facts_brief_or_verdict(self):
        world = World()
        final = copy.deepcopy(world.policy)
        sizing = final["position_sizing"]
        sizing.update(
            cash_stance="neutral", sizing_plan_id="trade-3", sizing_plan_sha256="a" * 64,
            conviction_by_symbol={"1101.TW": "medium"}, hold_conviction_by_symbol={"2317.TW": "low", "2330.TW": "medium"},
            position_breadth="balanced", rank_by_symbol={"1101.TW": 1}, selection_ranking_id="rank-1",
            selection_ranking_sha256="b" * 64, support_by_symbol={"1101.TW": 1})
        final["policy_id"] = "policy-1+trade-3"
        final["cash_buffer_rate"] = "0.12"
        self.assertNotEqual(final, world.policy)
        args = (world.bundle, final, world.trade, world.ranking, world.debate, world.review, world.exposure)
        self.assertEqual(nomination_facts(*args), world.facts())
        self.assertEqual(json.dumps(swap_nomination_brief(*args), sort_keys=True), json.dumps(world.brief(), sort_keys=True))
        sealed = seal_swap_nomination(world.envelope, good_output())
        self.assertEqual(SwapNominationValidator(*args).validate(sealed), [])
        self.assertEqual(world.validator().validate(sealed), [])


class HeldSinceTimezoneTests(unittest.TestCase):
    def test_naive_held_since_is_rejected_not_raised(self):
        world = World(mutate_policy=lambda p: p["position_sizing"]["exposure_rotation"]["held_since_by_symbol"].update({"2330.TW": "2026-09-01T09:00:00"}))
        with self.assertRaises(DecisionToolError):
            world.facts()
        errors = world.validate(good_output())
        self.assertTrue(any("無法以共同輸入重算事實" in error for error in errors), errors)


class SealingTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def test_completed_nomination_validates_and_is_sealed_with_hash(self):
        nomination = seal_swap_nomination(self.world.envelope, good_output())
        self.assertEqual(nomination["status"], "completed")
        self.assertEqual(self.world.validator().validate(nomination), [])
        self.assertEqual([item["buy_symbol"] for item in nominations_of(nomination)], ["1101.TW"])

    def test_empty_nomination_needs_a_reason_and_only_then(self):
        self.assertEqual(self.world.validate({"nominations": [], "thesis_hint_responses": [], "no_nomination_reason": "沒有足夠依據換股。"}), [])
        self.assertTrue(any("no_nomination_reason" in error for error in self.world.validate({"nominations": [], "thesis_hint_responses": []})))
        self.assertTrue(any("no_nomination_reason" in error for error in self.world.validate(good_output(no_nomination_reason="x"))))

    def test_program_statuses_in_a_world_that_needs_nomination(self):
        needs = self.world
        validator = needs.validator()
        for status in ("exhausted", "unavailable"):
            sealed = program_nomination(needs.envelope, status, ["最後一次驗證錯誤"])
            self.assertEqual(validator.validate(sealed), [], status)
            self.assertEqual(nominations_of(sealed), [])
        errors = validator.validate(program_nomination(needs.envelope, "not_needed"))
        self.assertTrue(any("不得封存 not_needed" in error for error in errors), errors)

    def test_program_statuses_in_a_world_that_does_not_need_nomination(self):
        world = World(mutate_policy=lambda policy: policy["position_sizing"]["agent_swap"].update(max_swaps_per_run=1) or policy["position_sizing"]["rotation"].update(max_swaps_per_run=0))
        self.assertFalse(needs_nomination(world.facts()))
        validator = world.validator()
        self.assertEqual(validator.validate(program_nomination(world.envelope, "not_needed")), [])
        for status in ("exhausted", "unavailable"):
            errors = validator.validate(program_nomination(world.envelope, status, ["x"]))
            self.assertTrue(any("不需要提名" in error for error in errors), (status, errors))
        errors = validator.validate(seal_swap_nomination(world.envelope, good_output()))
        self.assertTrue(any("不需要提名" in error for error in errors), errors)

    def test_degraded_shapes_are_fixed(self):
        validator = self.world.validator()
        sealed = program_nomination(self.world.envelope, "exhausted", ["x"])
        for mutate in (
            lambda s: s.update(nominations=good_output()["nominations"]),
            lambda s: s.update(thesis_hint_responses=[{"symbol": "2330.TW", "response": "kept", "reason": "r"}]),
            lambda s: s.update(no_nomination_reason="自己寫的理由"),
            lambda s: s.update(errors=[]),
            lambda s: s.update(errors=[""]),
            lambda s: s.update(errors=["x"] * 11),
        ):
            tampered = copy.deepcopy(sealed)
            mutate(tampered)
            tampered["content_sha256"] = sn.artifact_content_sha256(tampered)
            self.assertTrue(validator.validate(tampered), tampered)
        with self.assertRaises(DecisionToolError):
            program_nomination(self.world.envelope, "completed")

    def test_envelope_and_hash_are_bound(self):
        sealed = seal_swap_nomination(self.world.envelope, good_output())
        validator = self.world.validator()
        cases = {
            "hash": lambda s: s.update(content_sha256="0" * 64),
            "trade hash": lambda s: s.update(trade_decision_sha256="0" * 64),
            "ranking hash": lambda s: s.update(selection_ranking_sha256="0" * 64),
            "cutoff": lambda s: s.update(decision_cutoff="2030-01-01T00:00:00+00:00"),
            "bundle": lambda s: s.update(bundle_hash="0" * 64),
            "status": lambda s: s.update(status="done"),
            "unknown field": lambda s: s.update(extra="x"),
            "schema": lambda s: s.update(schema_version="9.9"),
        }
        for label, mutate in cases.items():
            tampered = copy.deepcopy(sealed)
            mutate(tampered)
            if label != "hash":
                tampered["content_sha256"] = sn.artifact_content_sha256(tampered)
            self.assertTrue(validator.validate(tampered), label)
        self.assertEqual(validator.validate("not a mapping"), ["SwapNomination 必須是物件"])

    def test_build_is_total_and_garbage_is_rejected_not_raised(self):
        for raw in (None, [], "x", 5, {}, {"nominations": "x"}, {"nominations": [1], "thesis_hint_responses": {}}):
            sealed = seal_swap_nomination(self.world.envelope, raw)
            self.assertTrue(self.world.validator().validate(sealed), raw)
        sealed = seal_swap_nomination(self.world.envelope, dict(good_output(), status="not_needed"))
        self.assertEqual(sealed["status"], "completed")
        self.assertIn("x_status", sealed)
        self.assertTrue(any("x_status" in error for error in self.world.validator().validate(sealed)))

    def test_nominations_of_ignores_non_completed(self):
        self.assertEqual(nominations_of(None), [])
        self.assertEqual(nominations_of(program_nomination(self.world.envelope, "unavailable", ["x"])), [])


class FailClosedTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def rejects(self, raw, text, world=None, **kwargs):
        errors = (world or self.world).validate(raw, **kwargs)
        self.assertTrue(any(text in error for error in errors), (text, errors))

    def test_buy_symbol_rules(self):
        self.rejects(with_pair(buy_symbol="2317.TW"), "第一輪不是 buy 或已持有")  # 已持有
        self.rejects(with_pair(buy_symbol="9999.TW"), "不在交易池")
        self.rejects(with_pair(buy_symbol="1101.tw"), "大寫代號")
        self.rejects(with_pair(buy_symbol=""), "大寫代號")
        self.rejects(with_pair(buy_symbol=5), "大寫代號")
        weak = World(first_round_items(**{"1101.TW": decision_item("1101.TW", "buy", "low", adopt=["bull-1101-1"])}))
        self.rejects(good_output(), "conviction_below_threshold", world=weak)
        add = World(first_round_items(**{"1101.TW": decision_item("1101.TW", "no_trade")}))
        self.rejects(good_output(), "第一輪不是 buy", world=add)
        cooled = World(mutate_policy=lambda p: p["position_sizing"]["reentry_cooldown"].update(blocked_symbols=["1101.TW"]))
        self.rejects(good_output(), "reentry_cooldown", world=cooled)

    def test_sell_symbol_rules(self):
        self.rejects(with_pair(sell_symbol="1101.TW"), "未持有或第一輪不是 hold")  # 未持有
        self.rejects(with_pair(sell_symbol="2317.TW"), "no_bear_basis")  # 沒有空頭依據
        self.rejects(with_pair(sell_symbol="9999.TW"), "不在交易池")
        trimmed = World(first_round_items(**{"2330.TW": decision_item("2330.TW", "trim", None, adopt=["bear-2330-1"], reject=["bull-2330-1"])}))
        self.rejects(good_output(), "第一輪不是 hold", world=trimmed)
        young = World(mutate_policy=lambda p: p["position_sizing"]["agent_swap"].update(min_holding_days=30))
        self.rejects(good_output(), "holding_days_short", world=young)

        def untradable(bundle):
            bundle["trading_status_bundle"] = {"bundle_id": "t"}
            bundle["tradability_assessment"] = {"symbols": [{"symbol": "2330.TW", "state": "restricted"}]}

        self.rejects(good_output(), "not_tradable", world=World(mutate_bundle=untradable))

    def test_same_symbol_duplicates_and_pair_cap(self):
        self.rejects(with_pair(sell_symbol="1101.TW", buy_symbol="1101.TW"), "不得是同一檔")
        pair = good_output()["nominations"][0]
        self.rejects(good_output(nominations=[pair, dict(pair)]), "只能出現一次")
        capped = World(mutate_policy=lambda p: p["position_sizing"]["agent_swap"].update(max_swaps_per_run=1))
        extra = dict(pair, buy_symbol="9999.TW", sell_symbol="2317.TW")
        self.rejects(good_output(nominations=[pair, extra]), "超過本次上限", world=capped)

    def test_buy_basis_must_be_the_buy_stocks_adopted_bull_claims(self):
        self.rejects(with_pair(buy_basis_ids=["bull-2330-1"]), "屬於其他股票")
        self.rejects(with_pair(buy_basis_ids=["bull-9999-1"]), "不存在")
        self.rejects(with_pair(buy_basis_ids=[]), "非空的字串清單")
        self.rejects(with_pair(buy_basis_ids=["bull-1101-1", "bull-1101-1"]), "不得重複")
        self.rejects(with_pair(buy_basis_ids=[1]), "非空的字串清單")
        self.rejects(with_pair(buy_basis_ids="bull-1101-1"), "非空的字串清單")
        unadopted = World(first_round_items(**{"1101.TW": decision_item("1101.TW", "buy", "medium", adopt=[], reject=["bull-1101-1"])}))
        self.rejects(good_output(), "第一輪已否決", world=unadopted)

    def test_sell_basis_must_be_the_sold_stocks_adopted_bear_or_position_facts(self):
        self.rejects(with_pair(sell_basis_ids=["bull-2330-1"]), "第一輪已否決")  # 被否決的多頭 claim
        self.rejects(with_pair(sell_basis_ids=["bull-1101-1"]), "屬於其他股票")
        self.rejects(with_pair(sell_basis_ids=["review:2317.TW:position"]), "屬於其他股票")
        self.rejects(with_pair(sell_basis_ids=["bear-2330-9"]), "不存在")
        self.rejects(with_pair(sell_basis_ids=[]), "非空的字串清單")
        only_position = World(first_round_items(sell_adopted=("review:2330.TW:position",), sell_rejected=("bear-2330-1", "bull-2330-1")))
        self.assertEqual(only_position.validate(with_pair(sell_basis_ids=["review:2330.TW:position"])), [])
        self.rejects(with_pair(sell_basis_ids=["bear-2330-1"]), "第一輪已否決", world=only_position)
        bull_adopted = World(first_round_items(sell_adopted=("bull-2330-1", "review:2330.TW:position"), sell_rejected=("bear-2330-1",)))
        self.rejects(with_pair(sell_basis_ids=["bull-2330-1"]), "不是可引用的空頭或持股依據", world=bull_adopted)

    def test_extra_fields_numbers_and_wrong_types(self):
        self.rejects(good_output(extra="x"), "含未允許欄位")
        self.rejects(with_pair(weight="0.1"), "不得包含配置")
        self.rejects(with_pair(shares=1000), "不得包含配置")
        self.rejects(with_pair(note="x"), "含未允許欄位")
        self.rejects(with_pair(reason=5), "reason 必須是非空字串")
        self.rejects(with_pair(reason="  "), "reason 必須是非空字串")
        self.rejects(with_pair(reason=0.5), "reason 必須是非空字串")
        self.rejects(with_pair(buy_symbol=True), "大寫代號")
        self.rejects(good_output(nominations={"buy_symbol": "1101.TW"}), "nominations 必須是陣列")
        self.rejects(good_output(nominations=["x"]), "必須是物件")
        self.rejects(good_output(thesis_hint_responses="x"), "thesis_hint_responses 必須是陣列")
        sealed = seal_swap_nomination(self.world.envelope, good_output())
        sealed["lots"] = 1
        sealed["content_sha256"] = sn.artifact_content_sha256(sealed)
        self.assertTrue(any("不得包含配置" in error for error in self.world.validator().validate(sealed)))

    def test_previous_run_identifiers_are_rejected_in_every_text_field(self):
        prior = {"context_id": "ctx-prev-9", "source": {"run_id": "run-prev-9", "decision_id": "decision:abc", "intent_id": "intent-prev-9"}}
        self.rejects(with_pair(reason="承接 run-prev-9 的結論"), "不得引用前次決策背景", prior_context=prior)
        self.rejects(good_output(nominations=[], no_nomination_reason="因為 ctx-prev-9"), "不得引用前次決策背景", prior_context=prior)
        self.assertEqual(self.world.validate(good_output(), prior_context=prior), [])
        hinted = World(mutate_policy=lambda p: p["position_sizing"]["agent_swap"].update(
            thesis_hint_symbols=["2317.TW"], verdicts_ref={"verdicts_id": "x", "as_of": "y", "sha256": "z"}))
        raw = good_output(thesis_hint_responses=[{"symbol": "2317.TW", "response": "kept", "reason": "見 decision:abc"}])
        self.rejects(raw, "不得引用前次決策背景", world=hinted, prior_context=prior)

    def test_thesis_hint_responses(self):
        ref = {"verdicts_id": "x", "as_of": "y", "sha256": "z"}
        hinted = World(mutate_policy=lambda p: p["position_sizing"]["agent_swap"].update(
            thesis_hint_symbols=["2317.TW", "2330.TW"], verdicts_ref=ref))
        kept = {"symbol": "2317.TW", "response": "kept", "reason": "仍有多頭論點。"}
        nominated = {"symbol": "2330.TW", "response": "nominated", "reason": "論點被推翻且有空頭依據。"}
        self.assertEqual(hinted.validate(good_output(thesis_hint_responses=[kept, nominated])), [])
        self.rejects(good_output(thesis_hint_responses=[kept]), "漏回應論點提示", world=hinted)
        self.rejects(good_output(thesis_hint_responses=[]), "漏回應論點提示", world=hinted)
        self.rejects(good_output(thesis_hint_responses=[kept, kept, nominated]), "只能回應一次", world=hinted)
        self.rejects(good_output(thesis_hint_responses=[kept, dict(nominated, symbol="1101.TW")]), "含不需回應的股票", world=hinted)
        self.rejects(good_output(thesis_hint_responses=[dict(kept, response="nominated"), nominated]), "必須是某一對的 sell_symbol", world=hinted)
        self.rejects(good_output(thesis_hint_responses=[kept, dict(nominated, response="kept")]), "不得出現在 sell_symbol", world=hinted)
        self.rejects(good_output(thesis_hint_responses=[kept, dict(nominated, response="maybe")]), "response 必須是", world=hinted)
        self.rejects(good_output(thesis_hint_responses=[kept, dict(nominated, reason="")]), "reason 必須是非空字串", world=hinted)
        self.rejects(good_output(thesis_hint_responses=[kept, dict(nominated, extra=1)]), "含未允許欄位", world=hinted)
        self.rejects(good_output(thesis_hint_responses=[kept, dict(nominated, response=True)]), "response 必須是", world=hinted)
        # 沒有提示時不得憑空回應。
        self.rejects(good_output(thesis_hint_responses=[kept]), "含不需回應的股票")

    def test_cash_and_turnover_lower_bounds(self):
        poor = World(cash=60_000, mutate_policy=lambda p: p.update(reuse_sell_proceeds=False))
        self.rejects(good_output(), "買不起新股一張", world=poor)
        busy = World(mutate_policy=lambda p: p.update(max_turnover_rate="0.05"))
        self.rejects(good_output(), "換手上限", world=busy)

    def test_inputs_that_cannot_be_recomputed_are_rejected_not_raised(self):
        world = World()
        broken = copy.deepcopy(world.policy)
        del broken["position_sizing"]["exposure_rotation"]["held_since_by_symbol"]
        validator = SwapNominationValidator(world.bundle, broken, world.trade, world.ranking, world.debate, world.review, world.exposure)
        errors = validator.validate(seal_swap_nomination(world.envelope, good_output()))
        self.assertTrue(any("無法以共同輸入重算事實" in error for error in errors), errors)


class ThesisHintBindingTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def test_binds_only_broken_symbols_with_reference(self):
        bound = bind_thesis_hints(self.world.policy, VERDICTS)
        gate = bound["position_sizing"]["agent_swap"]
        self.assertEqual(gate["thesis_hint_symbols"], ["2330.TW"])
        self.assertEqual(gate["verdicts_ref"], {"verdicts_id": "tv-1", "as_of": VERDICTS["as_of"], "sha256": VERDICTS["sha256"]})
        self.assertNotIn("thesis_hint_symbols", self.world.policy["position_sizing"]["agent_swap"])  # 不改原物件
        self.assertEqual(validate_thesis_hint_binding(bound, VERDICTS), [])

    def test_no_verdicts_or_no_broken_binds_nothing(self):
        intact = dict(VERDICTS, items=[VERDICTS["items"][1]])
        for verdicts in (None, intact):
            bound = bind_thesis_hints(self.world.policy, verdicts)
            self.assertEqual(bound, self.world.policy)
            self.assertEqual(validate_thesis_hint_binding(bound, verdicts), [])

    def test_template_prefill_is_cleared_when_binding_and_rejected_when_validating(self):
        prefilled = copy.deepcopy(self.world.policy)
        prefilled["position_sizing"]["agent_swap"].update(
            thesis_hint_symbols=["1101.TW"], verdicts_ref={"verdicts_id": "old", "as_of": "old", "sha256": "old"})
        self.assertTrue(validate_thesis_hint_binding(prefilled, None))
        self.assertTrue(validate_thesis_hint_binding(prefilled, VERDICTS))
        self.assertEqual(bind_thesis_hints(prefilled, None), self.world.policy)
        self.assertEqual(validate_thesis_hint_binding(bind_thesis_hints(prefilled, VERDICTS), VERDICTS), [])

    def test_mismatch_with_reread_verdicts_is_rejected(self):
        bound = bind_thesis_hints(self.world.policy, VERDICTS)
        self.assertTrue(validate_thesis_hint_binding(self.world.policy, VERDICTS))  # 缺綁定
        self.assertTrue(validate_thesis_hint_binding(bound, dict(VERDICTS, sha256="c" * 64)))
        self.assertTrue(validate_thesis_hint_binding(bound, dict(VERDICTS, items=[dict(VERDICTS["items"][0], symbol="2317.TW")])))
        tampered = copy.deepcopy(bound)
        tampered["position_sizing"]["agent_swap"]["thesis_hint_symbols"] = ["2317.TW", "2330.TW"]
        self.assertTrue(validate_thesis_hint_binding(tampered, VERDICTS))

    def test_policies_without_agent_swap_are_untouched(self):
        old = copy.deepcopy(self.world.policy)
        del old["position_sizing"]["agent_swap"]
        self.assertEqual(bind_thesis_hints(old, VERDICTS), old)
        self.assertEqual(validate_thesis_hint_binding(old, VERDICTS), [])


class AgentSwapV2BindingTests(unittest.TestCase):
    """v2（正式）：提名配對由 pipeline 綁進 Policy（apply_swap_nomination），Finalizer 以提名封存重算比對；v1 影子原樣不動。"""

    def setUp(self):
        self.world = World()
        self.nomination = seal_swap_nomination(self.world.envelope, good_output())

    def v2_policy(self):
        policy = copy.deepcopy(self.world.policy)
        policy["position_sizing"]["agent_swap"]["version"] = "v2"
        return policy

    def team(self):
        world = self.world
        return {"swap_nomination": self.nomination, "selection_ranking": world.ranking, "position_review": world.review,
                "exposure_review": world.exposure}

    def check(self, policy, team=None):
        errors: list = []
        validate_team_swap_nomination(self.world.bundle, policy, self.world.trade, self.world.debate, team or self.team(), errors)
        return errors

    def test_apply_binds_pairs_in_the_agents_order_with_the_sealed_reference(self):
        bound = apply_swap_nomination(self.v2_policy(), self.nomination)
        gate = bound["position_sizing"]["agent_swap"]
        self.assertEqual(gate["nominated_pairs"], [{"buy_symbol": "1101.TW", "sell_symbol": "2330.TW"}])
        self.assertEqual(gate["nomination_ref"], {
            "nomination_id": "swap-1", "sha256": self.nomination["content_sha256"], "status": "completed"})
        self.assertNotIn("content_sha256", bound)  # 呼叫端重算
        self.assertNotIn("nominated_pairs", self.world.policy["position_sizing"]["agent_swap"])  # 不改原物件
        self.assertEqual(self.check(bound), [])

    def test_non_completed_statuses_bind_an_empty_list_with_their_status(self):
        for status in ("not_needed", "exhausted", "unavailable"):
            program = program_nomination(self.world.envelope, status, ["x"] if status != "not_needed" else [])
            gate = apply_swap_nomination(self.v2_policy(), program)["position_sizing"]["agent_swap"]
            self.assertEqual((gate["nominated_pairs"], gate["nomination_ref"]["status"]), ([], status))

    def test_a_prefilled_template_is_overwritten_and_shadow_or_gateless_policies_are_untouched(self):
        prefilled = self.v2_policy()
        prefilled["position_sizing"]["agent_swap"].update(
            nominated_pairs=[{"buy_symbol": "2317.TW", "sell_symbol": "1101.TW"}],
            nomination_ref={"nomination_id": "old", "sha256": "0" * 64, "status": "completed"})
        bound = apply_swap_nomination(prefilled, self.nomination)
        self.assertEqual(bound["position_sizing"]["agent_swap"]["nominated_pairs"], [{"buy_symbol": "1101.TW", "sell_symbol": "2330.TW"}])
        self.assertEqual(apply_swap_nomination(self.world.policy, self.nomination), self.world.policy)  # v1 影子
        gateless = copy.deepcopy(self.world.policy)
        del gateless["position_sizing"]["agent_swap"]
        self.assertEqual(apply_swap_nomination(gateless, self.nomination), gateless)

    def test_finalizer_rejects_pairs_that_differ_from_the_sealed_nomination(self):
        bound = apply_swap_nomination(self.v2_policy(), self.nomination)
        cases = {
            "配對被改寫": lambda gate: gate.update(nominated_pairs=[{"buy_symbol": "2317.TW", "sell_symbol": "2330.TW"}]),
            "配對被清空": lambda gate: gate.update(nominated_pairs=[]),
            "狀態被改寫": lambda gate: gate["nomination_ref"].update(status="exhausted"),
            "雜湊不符": lambda gate: gate["nomination_ref"].update(sha256="f" * 64),
            "缺綁定": lambda gate: (gate.pop("nominated_pairs"), gate.pop("nomination_ref")),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                tampered = copy.deepcopy(bound)
                mutate(tampered["position_sizing"]["agent_swap"])
                self.assertTrue(any("agent_swap" in error for error in self.check(tampered)), label)

    def test_v1_shadow_needs_no_binding_and_has_no_version_gate_any_more(self):
        self.assertEqual(self.check(self.world.policy), [])
        self.assertFalse(hasattr(sn, "SUPPORTED_GATE_VERSIONS"))
        self.assertFalse(hasattr(sn, "unsupported_gate_version"))

    def test_the_empty_reason_string_means_not_applicable_when_there_are_nominations_for_codex_strict_schema(self):
        # Codex strict schema 要求欄位一律輸出：有提名時 no_nomination_reason 為空字串，封存時省略，與沒有輸出等價。
        sealed = seal_swap_nomination(self.world.envelope, good_output(no_nomination_reason=""))
        self.assertNotIn("no_nomination_reason", sealed)
        self.assertEqual(sealed["content_sha256"], self.nomination["content_sha256"])
        self.assertEqual(self.world.validate(good_output(no_nomination_reason="")), [])
        # 空清單時空字串仍被退回；有提名卻寫了理由也仍被退回。
        self.assertTrue(any("no_nomination_reason" in e for e in self.world.validate(
            {"nominations": [], "thesis_hint_responses": [], "no_nomination_reason": ""})))
        self.assertTrue(any("no_nomination_reason" in e for e in self.world.validate(good_output(no_nomination_reason="x"))))

    def test_the_agent_schema_requires_every_declared_property_for_codex(self):
        from etf_agent.automation.agent_schemas import SWAP_NOMINATION_SCHEMA

        self.assertEqual(set(SWAP_NOMINATION_SCHEMA["properties"]), set(SWAP_NOMINATION_SCHEMA["required"]))
        for node in (SWAP_NOMINATION_SCHEMA["properties"]["nominations"]["items"], SWAP_NOMINATION_SCHEMA["properties"]["thesis_hint_responses"]["items"]):
            self.assertEqual(set(node["properties"]), set(node["required"]))


if __name__ == "__main__":
    unittest.main()
