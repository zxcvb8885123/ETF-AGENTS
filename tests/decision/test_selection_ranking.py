"""入選排序（``decision/selection_ranking.py``）與 team_inputs 2.5 的確定性與 fail-closed 測試。"""

import copy
import unittest
from decimal import Decimal

from etf_agent.decision import (
    AllocationOrderEngine,
    SelectionRankingValidator,
    apply_selection_ranking,
    candidates_by_conviction,
    decision_policy_sha256,
    program_ranking,
    ranking_envelope,
    ranking_needs_agent,
    seal_selection_ranking,
    selection_ranking_brief,
)
from etf_agent.decision.fields import DecisionToolError
from etf_agent.decision.selection_ranking import rank_by_symbol
from etf_agent.decision.sizing import HELD_ADD_PRIORITY, SELECTION_RANKING, validate_position_sizing

from test_position_sizing import sized_inputs, sized_policy


def two_in_one_tier():
    """2317（買進）與 2330（加碼）同為 medium 信心。"""
    bundle, intent, stance, policy = sized_inputs("medium", "medium")
    return bundle, intent, stance, policy


def tiers(*symbols, level="medium"):
    return [{"conviction": level, "ordered": [{"symbol": symbol, "reason": "理由。"} for symbol in symbols]}]


class RankingBuildTests(unittest.TestCase):
    def test_candidates_group_by_conviction(self):
        _, intent, _, _ = sized_inputs("high", "low")
        self.assertEqual(candidates_by_conviction(intent), {"high": ["2317.TW"], "low": ["2330.TW"]})

    def test_agent_only_needed_when_a_tier_has_two_candidates(self):
        self.assertFalse(ranking_needs_agent(sized_inputs("high", "low")[1]))
        self.assertTrue(ranking_needs_agent(two_in_one_tier()[1]))

    def test_program_ranking_validates_without_agent(self):
        bundle, intent, _, _ = sized_inputs("high", "low")
        ranking = program_ranking(ranking_envelope(bundle, intent, "r-1"), intent)
        self.assertEqual(SelectionRankingValidator(bundle, intent).validate(ranking), [])
        self.assertEqual(rank_by_symbol(ranking), {"2317.TW": 1, "2330.TW": 1})

    def test_brief_lists_every_candidate_with_program_numbers(self):
        bundle, intent, _, _ = two_in_one_tier()
        from test_trader import world

        _, momentum, reports, debate = world()
        rows = selection_ranking_brief(intent, debate, reports, momentum)
        self.assertEqual({row["symbol"] for row in rows}, {"2317.TW", "2330.TW"})
        self.assertTrue(all("trader_rationale" in row and "atr_14_pct" in row for row in rows))


class RankingValidatorTests(unittest.TestCase):
    def setUp(self):
        self.bundle, self.intent, _, _ = two_in_one_tier()
        self.envelope = ranking_envelope(self.bundle, self.intent, "r-1")
        self.validator = SelectionRankingValidator(self.bundle, self.intent)

    def seal(self, tier_list):
        return seal_selection_ranking(self.envelope, tier_list)

    def test_full_ranking_passes(self):
        self.assertEqual(self.validator.validate(self.seal(tiers("2330.TW", "2317.TW"))), [])

    def test_missing_candidate_is_rejected(self):
        errors = self.validator.validate(self.seal(tiers("2317.TW")))
        self.assertTrue(any("缺少該信心等級的候選" in error for error in errors), errors)

    def test_extra_or_misplaced_symbol_is_rejected(self):
        errors = self.validator.validate(self.seal(tiers("2317.TW", "2330.TW", "2454.TW")))
        self.assertTrue(any("不屬於該信心等級" in error for error in errors), errors)

    def test_symbol_in_wrong_tier_is_rejected(self):
        errors = self.validator.validate(self.seal(tiers("2317.TW", "2330.TW", level="high")))
        self.assertTrue(errors)

    def test_duplicate_symbol_is_rejected(self):
        errors = self.validator.validate(self.seal(tiers("2317.TW", "2317.TW", "2330.TW")))
        self.assertTrue(any("不得重複" in error for error in errors), errors)

    def test_empty_reason_is_rejected(self):
        bad = tiers("2317.TW", "2330.TW")
        bad[0]["ordered"][0]["reason"] = " "
        self.assertTrue(self.validator.validate(self.seal(bad)))

    def test_tampered_content_is_rejected(self):
        ranking = self.seal(tiers("2317.TW", "2330.TW"))
        ranking["tiers"][0]["ordered"].reverse()
        self.assertTrue(any("content_sha256" in error for error in self.validator.validate(ranking)))

    def test_ranking_bound_to_other_trade_decision_is_rejected(self):
        _, other, _, _ = sized_inputs("high", "low")
        errors = SelectionRankingValidator(self.bundle, other).validate(self.seal(tiers("2317.TW", "2330.TW")))
        self.assertTrue(errors)

    def test_unknown_field_and_forbidden_key_are_rejected(self):
        ranking = self.seal(tiers("2317.TW", "2330.TW"))
        self.assertTrue(self.validator.validate(dict(ranking, weight="0.1")))
        self.assertTrue(self.validator.validate(dict(ranking, hint="買2317")))


class ApplyRankingTests(unittest.TestCase):
    def setUp(self):
        self.bundle, self.intent, self.stance, policy = two_in_one_tier()
        policy["position_sizing"]["selection_ranking"] = SELECTION_RANKING
        policy["position_sizing"]["held_add_priority"] = HELD_ADD_PRIORITY
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.policy = sized_policy(policy, self.intent, self.stance, self.bundle)
        self.ranking = seal_selection_ranking(
            ranking_envelope(self.bundle, self.intent, "r-1"), tiers("2330.TW", "2317.TW")
        )

    def test_apply_binds_ranks_id_and_hash(self):
        bound = apply_selection_ranking(self.policy, self.ranking)
        sizing = bound["position_sizing"]
        self.assertEqual(sizing["rank_by_symbol"], {"2330.TW": 1, "2317.TW": 2})
        self.assertEqual(sizing["selection_ranking_id"], "r-1")
        self.assertEqual(sizing["selection_ranking_sha256"], self.ranking["content_sha256"])
        self.assertNotIn("content_sha256", bound)

    def test_policy_without_selection_ranking_is_refused(self):
        plain = copy.deepcopy(self.policy)
        plain["position_sizing"].pop("selection_ranking")
        with self.assertRaises(DecisionToolError):
            apply_selection_ranking(plain, self.ranking)

    def test_ranking_before_trade_binding_is_refused(self):
        _, _, _, unbound = two_in_one_tier()
        unbound["position_sizing"]["selection_ranking"] = SELECTION_RANKING
        with self.assertRaises(DecisionToolError):
            apply_selection_ranking(unbound, self.ranking)

    def test_allocation_follows_agent_rank_over_default_rules(self):
        bound = apply_selection_ranking(self.policy, self.ranking)
        bound["content_sha256"] = decision_policy_sha256(bound)
        engine = AllocationOrderEngine(self.bundle, bound)
        engine.policy["position_sizing"].update(
            positions_by_breadth={"concentrated": 2}, position_breadth="concentrated",
            support_by_symbol={"2317.TW": 5, "2330.TW": 0},  # 舊規則會讓 2317 先；名次要壓過它
        )
        order, _, _ = engine._conviction_targets(
            ["2317.TW", "2330.TW"], set(), {}, Decimal("1000000"), int(engine.policy["max_positions"]),
            {"cash_buffer_rate": Decimal("0.1"), "max_stock_weight": Decimal("0.5")}, 0,
        )
        self.assertEqual(order, ["2330.TW", "2317.TW"])

    def test_old_rules_apply_when_no_ranking(self):
        engine = AllocationOrderEngine(self.bundle, self.policy)
        engine.policy["position_sizing"].update(
            positions_by_breadth={"concentrated": 2}, position_breadth="concentrated",
            support_by_symbol={"2317.TW": 5, "2330.TW": 0},
        )
        order, _, _ = engine._conviction_targets(
            ["2317.TW", "2330.TW"], set(), {}, Decimal("1000000"), int(engine.policy["max_positions"]),
            {"cash_buffer_rate": Decimal("0.1"), "max_stock_weight": Decimal("0.5")}, 0,
        )
        self.assertEqual(order, ["2317.TW", "2330.TW"])

    def test_sizing_validator_rejects_partial_or_inconsistent_rank_fields(self):
        bound = apply_selection_ranking(self.policy, self.ranking)
        universe = {"2317.TW", "2330.TW"}
        errors = []
        validate_position_sizing(bound, universe, errors)
        self.assertEqual(errors, [])
        for mutate in (
            lambda s: s.pop("selection_ranking_sha256"),
            lambda s: s.update(rank_by_symbol={"2317.TW": 1}),
            lambda s: s.update(rank_by_symbol={"2317.TW": 0, "2330.TW": 1}),
            lambda s: s.update(selection_ranking="v2"),
        ):
            broken = copy.deepcopy(bound)
            mutate(broken["position_sizing"])
            errors = []
            validate_position_sizing(broken, universe, errors)
            self.assertTrue(errors)


class FinalizerRankingChainTests(unittest.TestCase):
    """Finalizer 重建驗證 2.5 的入選排序；被改寫、與 policy 或版本不一致一律拒絕。"""

    def setUp(self):
        from test_position_review import SETTINGS
        from test_rotation_slots import world as rotation_world
        from test_team_chain import STANCE
        from etf_agent.decision import (
            apply_trade_decision, build_portfolio_slots, build_position_review, build_prior_decision_context,
        )

        (self.bundle, self.momentum, self.debate, self.trade,
         base, _, self.reports) = rotation_world()
        base = copy.deepcopy(base)
        base["position_review"] = dict(SETTINGS)
        base["position_sizing"]["selection_ranking"] = SELECTION_RANKING
        base["content_sha256"] = decision_policy_sha256(base)
        self.stance = STANCE
        self.base = base
        self.ranking = program_ranking(ranking_envelope(self.bundle, self.trade, "r-1"), self.trade)
        policy = apply_trade_decision(copy.deepcopy(base), self.trade, STANCE, self.bundle, self.debate)
        self.policy = apply_selection_ranking(policy, self.ranking)
        self.policy["content_sha256"] = decision_policy_sha256(self.policy)
        self.slots = build_portfolio_slots(self.bundle, self.policy, self.debate)
        self.prior = build_prior_decision_context(self.bundle, None)
        self.review = build_position_review(self.bundle, self.policy)

    def ranking_errors(self, team, policy=None):
        from test_team_chain import finalize

        _, result = finalize(self.bundle, self.momentum, self.debate, self.trade, policy or self.policy, team)
        return [error for error in result["errors"] if "selection_ranking" in error or "rank_by_symbol" in error]

    def team(self, ranking):
        from etf_agent.decision import build_team_inputs

        return build_team_inputs(self.reports, None, self.stance, self.prior, self.slots, self.review, ranking)

    def test_rebuilt_ranking_produces_no_ranking_errors(self):
        self.assertEqual(self.ranking_errors(self.team(self.ranking)), [])

    def test_ranking_requires_position_review(self):
        from etf_agent.decision import build_team_inputs

        with self.assertRaises(DecisionToolError):
            build_team_inputs(self.reports, None, self.stance, self.prior, self.slots, None, self.ranking)

    def test_tampered_ranking_is_rejected(self):
        changed = copy.deepcopy(self.ranking)
        changed["tiers"][0]["ordered"][0]["reason"] = "改過的理由。"
        self.assertTrue(self.ranking_errors(self.team(changed)))

    def test_policy_ranks_must_match_the_ranking(self):
        forged = copy.deepcopy(self.policy)
        forged["position_sizing"]["rank_by_symbol"] = {symbol: 9 for symbol in forged["position_sizing"]["rank_by_symbol"]}
        forged["content_sha256"] = decision_policy_sha256(forged)
        errors = self.ranking_errors(self.team(self.ranking), forged)
        self.assertTrue(any("rank_by_symbol" in error for error in errors), errors)

    def test_policy_with_ranking_but_team_2_4_is_rejected(self):
        self.assertTrue(self.ranking_errors(self.team(None)))


class RankerStageTests(unittest.TestCase):
    """排序 Agent 步驟：驗證失敗會回饋重試；沒有同等級兩檔時不呼叫 Agent。"""

    def trade(self, bundle, debate, tier_2330="medium"):
        from etf_agent.decision import seal_trade_decision, trade_decision_envelope
        from test_trader import items

        values = items()
        values[1] = dict(
            values[1], intent="add", conviction=tier_2330,
            adopted_claim_ids=["bull-2330-1"], rejected_claim_ids=["bear-2330-1"],
        )
        return seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), values)

    def run_stage(self, runner, tier_2330):
        import tempfile
        from pathlib import Path

        from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
        from test_trader import ROOT, world

        bundle, momentum, reports, debate = world()
        trade = self.trade(bundle, debate, tier_2330)
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            return pipeline.run_selection_ranking(bundle, momentum, reports, debate, trade, "run-1"), runner

    def test_agent_ranks_same_tier_and_retries_on_invalid_output(self):
        from test_daily_pipeline import FakeRunner

        wrong = {"tiers": tiers("2317.TW")}
        right = {"tiers": tiers("2330.TW", "2317.TW")}
        ranking, runner = self.run_stage(FakeRunner({"ranker": [wrong, right]}), "medium")
        self.assertEqual(rank_by_symbol(ranking), {"2330.TW": 1, "2317.TW": 2})
        self.assertEqual(len(runner.prompts), 2)
        self.assertIn("缺少該信心等級的候選", runner.prompts[1][1])

    def test_no_agent_call_when_every_tier_has_one_candidate(self):
        from test_daily_pipeline import FakeRunner

        ranking, runner = self.run_stage(FakeRunner({}), "low")
        self.assertEqual(runner.prompts, [])
        self.assertEqual(rank_by_symbol(ranking), {"2317.TW": 1, "2330.TW": 1})


if __name__ == "__main__":
    unittest.main()
