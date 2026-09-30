import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionFinalizer,
    DecisionPolicyValidator,
    DecisionRepository,
    DecisionResultValidator,
    GuardValidator,
    ProposalValidator,
    RiskReviewValidator,
    RevisionHistoryBuilder,
    RevisionHistoryValidator,
    ScenarioEngine,
    ScenarioValidator,
    artifact_content_sha256,
    decision_bundle_sha256,
    decision_policy_sha256,
    decision_rules_sha256,
    revision_effects,
)

from test_analysts import bundle_with_event
from test_portfolio_decision_agent import refresh_artifact_hash


def policy():
    result = {
        "schema_version": "1.0",
        "policy_id": "policy-1",
        "available_at": "2026-09-19T18:00:00+00:00",
        "currency": "TWD",
        "cash_buffer_rate": "0.05",
        "default_target_weight": "0.20",
        "trim_fraction": "0.50",
        "commission_rate": "0.001425",
        "sell_tax_rate": "0.003",
        "minimum_commission": "20",
        "lot_size": 1000,
        "slippage_bps": "10",
        "max_turnover_rate": "0.50",
        "max_stock_weight": "0.50",
        "special_weight_limits": {"2330.TW": "0.50"},
        "max_sector_weight": "0.60",
        "sector_classification_version": "fixture-sector-1",
        "sector_by_symbol": {"2330.TW": "半導體", "2317.TW": "電子製造"},
        "min_positions": 1,
        "max_positions": 5,
        "cash_weight_ceiling": "0.90",
        "minimum_active_share": "0.05",
        "stress_price_decline_rate": "0.10",
        "stress_slippage_multiplier": "2",
        "liquidity_fill_rate": "0.50",
        "reuse_sell_proceeds": True,
        "max_revisions": 3,
    }
    result["content_sha256"] = decision_policy_sha256(result)
    return result


def chain_inputs(bundle=None, decisions=None, settings=None):
    """完整分析團隊鏈 fixture（bundle、動能、多空辯論、交易決策、綁定 policy、team_inputs…）。

    延後匯入 test_team_chain，避免它反向匯入本模組的 policy／risk_review 時循環。
    """
    from test_team_chain import team_world

    return team_world(bundle, decisions, settings)


def valid_inputs():
    """2317.TW 買進、2330.TW 續抱的 (bundle, momentum, debate, intent)。"""
    bundle, momentum, debate, intent, _, _, _, _ = chain_inputs()
    return bundle, momentum, debate, intent


def risk_review(bundle, proposal, scenario, guard, decision="approve", revision=0, actions=None):
    result = {
        "schema_version": "1.0",
        "review_id": "risk-review-%d" % revision,
        "bundle_id": bundle["bundle_id"],
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "bundle_hash": bundle["bundle_sha256"],
        "proposal_id": proposal["proposal_id"],
        "scenario_id": scenario["scenario_id"],
        "guard_id": guard["guard_id"],
        "revision_index": revision,
        "decision": decision,
        "rationale": "已檢查配置、現金、情境與硬性規則。",
        "evidence_ids": ["price-2317"],
        "risk_flags": [],
        "revision_actions": actions or [],
        "unresolved_questions": [],
        "status": "completed",
        "errors": [],
    }
    result["content_sha256"] = artifact_content_sha256(result)
    return result


class PortfolioRiskDecisionTests(unittest.TestCase):
    def test_no_official_etf_benchmark_uses_competition_limits_only(self):
        bundle, _, _, _ = valid_inputs()
        bundle["rules"]["required_benchmark_ids"] = []
        bundle["rules"]["minimum_active_share"] = "0"
        bundle["benchmarks"] = []
        bundle["rules"]["config_sha256"] = decision_rules_sha256(bundle["rules"])
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        settings = policy()
        settings["minimum_active_share"] = "0"
        settings["content_sha256"] = decision_policy_sha256(settings)
        _, momentum, debate, intent, *_ = chain_inputs(bundle)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(settings), [])
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        self.assertTrue(guard["passed"])
        checks = {item["rule_id"]: item for item in guard["checks"]}
        self.assertTrue(checks["STOCK_WEIGHT"]["passed"])
        self.assertTrue(checks["POSITION_COUNT"]["passed"])
        self.assertTrue(checks["ACTIVE_SHARE_NOT_APPLICABLE"]["passed"])
        self.assertEqual(guard["benchmark_results"], [])
        self.assertEqual(GuardValidator(bundle, settings).validate(proposal, scenario, guard), [])

        bundle["rules"]["minimum_active_share"] = "0.20"
        bundle["rules"]["config_sha256"] = decision_rules_sha256(bundle["rules"])
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        from etf_agent.decision import DecisionInputValidator
        self.assertTrue(any("沒有必備 ETF 基準" in error for error in DecisionInputValidator(bundle).validate()))

    def test_stress_cash_above_ceiling_is_warning_but_base_is_hard_rule(self):
        bundle, _, _, _ = valid_inputs()
        bundle["rules"]["required_benchmark_ids"] = []
        bundle["rules"]["minimum_active_share"] = "0"
        bundle["benchmarks"] = []
        bundle["rules"]["config_sha256"] = decision_rules_sha256(bundle["rules"])
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        settings = policy()
        settings["minimum_active_share"] = "0"
        settings["content_sha256"] = decision_policy_sha256(settings)
        _, momentum, _, intent, *_ = chain_inputs(bundle)
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)

        def with_cash(name, cash):
            changed = json.loads(json.dumps(scenario))
            item = next(row for row in changed["scenarios"] if row["name"] == name)
            item["cash"] = cash
            item["nav"] = str(int(cash) + sum(int(row["assumed_value"]) for row in item.get("positions", [])))
            return changed

        # 全現金建倉部分成交：壓力情境現金超過上限只是警告。
        stressed = with_cash("liquidity_stress", "100000000")
        guard = CompetitionGuardV2(bundle, settings).run(proposal, stressed)
        check = next(item for item in guard["checks"] if item["rule_id"] == "SCENARIOS")
        stress = next(item for item in check["details"] if item["name"] == "liquidity_stress")
        self.assertFalse(stress["cash_weight_ok"])
        self.assertTrue(stress["passed"])
        self.assertTrue(check["passed"])

        # base 情境代表目標配置，現金超過上限仍 fail-closed。
        guard = CompetitionGuardV2(bundle, settings).run(proposal, with_cash("base", "100000000"))
        check = next(item for item in guard["checks"] if item["rule_id"] == "SCENARIOS")
        self.assertFalse(check["passed"])
        self.assertFalse(guard["passed"])

    def test_policy_proposal_scenario_guard_and_final_decision(self):
        bundle, momentum, debate, intent, settings, team, _, _ = chain_inputs()
        self.assertEqual(DecisionPolicyValidator(bundle).validate(settings), [])
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        self.assertTrue(proposal["order_proposal"]["orders"])
        self.assertTrue(
            all(
                item["shares"] == item["lots"] * 1000
                for item in proposal["order_proposal"]["orders"]
            )
        )
        self.assertEqual(ProposalValidator(bundle, settings, intent).validate(proposal), [])
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        self.assertEqual(ScenarioValidator(bundle, settings).validate(proposal, scenario), [])
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        self.assertTrue(guard["passed"])
        self.assertEqual(GuardValidator(bundle, settings).validate(proposal, scenario, guard), [])
        review = risk_review(bundle, proposal, scenario, guard)
        self.assertEqual(
            RiskReviewValidator(bundle, settings).validate(proposal, scenario, guard, review),
            [],
        )
        history = RevisionHistoryBuilder(bundle, settings, intent).create(
            proposal, scenario, guard, review
        )
        self.assertEqual(
            RevisionHistoryValidator(bundle, settings, intent).validate(
                history, require_terminal=True
            ), []
        )
        result = DecisionFinalizer(bundle, settings, momentum, debate, intent, team).run(
            proposal, scenario, guard, review, history
        )
        self.assertEqual(result["status"], "approved", result["errors"])
        self.assertTrue(result["orders"])
        self.assertEqual(
            DecisionResultValidator(bundle, settings, momentum, debate, intent, team).validate(
                proposal, scenario, guard, review, history, result
            ),
            [],
        )

    def test_policy_requires_one_lot_of_one_thousand_shares(self):
        bundle, momentum, debate, intent = valid_inputs()
        settings = policy()
        settings["lot_size"] = 100
        settings["content_sha256"] = decision_policy_sha256(settings)
        errors = DecisionPolicyValidator(bundle).validate(settings)
        self.assertTrue(any("1000 股（一張）" in error for error in errors))

    def test_odd_lot_account_fails_closed(self):
        bundle, momentum, debate, intent = valid_inputs()
        bundle["account_snapshot"]["positions"][0]["shares"] = 500
        bundle["account_snapshot"]["nav"] = "582000"
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        with self.assertRaisesRegex(Exception, "第一版禁止產生零股交易"):
            AllocationOrderEngine(bundle, policy()).run(intent)

    def test_proposal_tampering_is_rejected(self):
        bundle, momentum, debate, intent = valid_inputs()
        settings = policy()
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        proposal["allocation_proposal"]["cash"] = "999999"
        proposal["content_sha256"] = artifact_content_sha256(proposal)
        errors = ProposalValidator(bundle, settings, intent).validate(proposal)
        self.assertIn("ProposalBundle 與確定性重算結果不一致", errors)

    def test_base_scenario_matches_hand_calculated_cash_and_nav(self):
        bundle, momentum, debate, intent = valid_inputs()
        settings = policy()
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        base = next(item for item in scenario["scenarios"] if item["name"] == "base")
        execution = base["executions"][0]
        self.assertEqual(execution["filled_lots"], 1)
        self.assertEqual(execution["execution_price"], "112.11")
        self.assertEqual(execution["gross_amount"], "112110")
        self.assertEqual(execution["commission"], "160")
        self.assertEqual(base["cash"], "387730")
        self.assertEqual(base["nav"], "663730")

    def test_liquidity_scenario_fills_only_whole_lots(self):
        bundle, momentum, debate, intent = valid_inputs()
        settings = policy()
        settings["default_target_weight"] = "0.40"
        settings["content_sha256"] = decision_policy_sha256(settings)
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        stress = next(item for item in scenario["scenarios"] if item["name"] == "liquidity_stress")
        self.assertEqual(stress["executions"][0]["filled_lots"], 1)
        self.assertEqual(stress["executions"][0]["filled_shares"], 1000)
        self.assertEqual(stress["unfilled_orders"][0]["unfilled_lots"], 1)

    def test_sell_not_filled_buy_filled_exposes_cash_gap(self):
        bundle, momentum, debate, intent = valid_inputs()
        bundle["account_snapshot"]["settled_cash"] = "80000"
        bundle["account_snapshot"]["unsettled_cash"] = "420000"
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        _, momentum, debate, intent, *_ = chain_inputs(bundle)
        intent["items"][1]["intent"] = "exit"
        refresh_artifact_hash(intent)
        settings = policy()
        settings["default_target_weight"] = "0.40"
        settings["cash_buffer_rate"] = "0"
        settings["max_turnover_rate"] = "1"
        settings["content_sha256"] = decision_policy_sha256(settings)
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        stress = next(item for item in scenario["scenarios"] if item["name"] == "liquidity_stress")
        self.assertGreater(int(stress["buying_power_shortfall"]), 0)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        self.assertFalse(guard["passed"])

    def test_cash_buffer_clips_buy_and_records_constraint(self):
        bundle, momentum, debate, intent = valid_inputs()
        settings = policy()
        settings["cash_buffer_rate"] = "0.99"
        settings["content_sha256"] = decision_policy_sha256(settings)
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        codes = {item["code"] for item in proposal["constraint_flags"]}
        self.assertIn("CASH_BUFFER", codes)
        self.assertFalse(
            any(item["side"] == "buy" for item in proposal["order_proposal"]["orders"])
        )

    def test_guard_fails_closed_without_explicit_tradability(self):
        bundle, momentum, debate, intent, *_ = chain_inputs(bundle_with_event())
        settings = policy()
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        self.assertFalse(guard["passed"])
        failed = {item["rule_id"] for item in guard["checks"] if not item["passed"]}
        self.assertIn("TRADABILITY_AVAILABLE", failed)

    def test_risk_cannot_approve_failed_guard(self):
        bundle, _, _, _ = valid_inputs()
        settings = policy()
        settings["min_positions"] = 3
        bundle["rules"]["min_positions"] = 3
        bundle["rules"]["config_sha256"] = decision_rules_sha256(bundle["rules"])
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        settings["content_sha256"] = decision_policy_sha256(settings)
        _, momentum, debate, intent, settings, team, _, _ = chain_inputs(bundle, None, settings)
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        self.assertFalse(guard["passed"])
        review = risk_review(bundle, proposal, scenario, guard)
        errors = RiskReviewValidator(bundle, settings).validate(
            proposal, scenario, guard, review
        )
        self.assertTrue(any("只能 reject" in error for error in errors))
        rejecting_review = risk_review(bundle, proposal, scenario, guard, "reject")
        history = RevisionHistoryBuilder(bundle, settings, intent).create(
            proposal, scenario, guard, rejecting_review
        )
        result = DecisionFinalizer(bundle, settings, momentum, debate, intent, team).run(
            proposal, scenario, guard, review, history
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["orders"], [])

    def test_no_trade_still_requires_guard_and_risk_review(self):
        bundle, _, _, _ = valid_inputs()
        settings = policy()
        settings["cash_weight_ceiling"] = "0.99"
        settings["minimum_active_share"] = "0.01"
        settings["content_sha256"] = decision_policy_sha256(settings)
        bundle["rules"]["cash_weight_must_be_below"] = "0.99"
        bundle["rules"]["minimum_active_share"] = "0.01"
        bundle["rules"]["config_sha256"] = decision_rules_sha256(bundle["rules"])
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        # 2317.TW 不買（採否決多頭 claim），2330.TW 續抱：沒有任何訂單。
        from test_trader import items as trade_items  # 延後匯入避免循環

        decisions = trade_items()
        decisions[0] = dict(
            decisions[0], intent="no_trade", conviction=None,
            adopted_claim_ids=[], rejected_claim_ids=["bull-2317-1"],
        )
        _, momentum, debate, intent, settings, team, _, _ = chain_inputs(bundle, decisions, settings)
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        self.assertEqual(proposal["order_proposal"]["orders"], [])
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        self.assertTrue(guard["passed"])
        review = risk_review(bundle, proposal, scenario, guard)
        history = RevisionHistoryBuilder(bundle, settings, intent).create(
            proposal, scenario, guard, review
        )
        result = DecisionFinalizer(
            bundle, settings, momentum, debate, intent, team
        ).run(proposal, scenario, guard, review, history)
        self.assertEqual(result["status"], "no_trade", result["errors"])
        self.assertIsNotNone(result["portfolio"])

    def test_finalizer_rejects_tampered_upstream_intent(self):
        bundle, momentum, debate, intent, settings, team, _, _ = chain_inputs()
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        review = risk_review(bundle, proposal, scenario, guard)
        history = RevisionHistoryBuilder(bundle, settings, intent).create(
            proposal, scenario, guard, review
        )
        intent["items"][0]["rationale"] = "遭竄改"
        result = DecisionFinalizer(
            bundle, settings, momentum, debate, intent, team
        ).run(proposal, scenario, guard, review, history)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("content_sha256" in error for error in result["errors"]))

    def blocked_holding_inputs(self):
        """2330.TW 已持有但被限制交易，2317.TW 仍可交易並買進。"""
        from etf_agent.decision import canonical_sha256

        bundle, momentum, debate, intent = valid_inputs()
        bundle["snapshot"]["tradable_symbols"] = ["2317.TW"]
        bundle["snapshot"]["not_tradable_symbols"] = ["2330.TW"]
        bundle["snapshot_sha256"] = canonical_sha256(bundle["snapshot"])
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        _, _, _, intent, *_ = chain_inputs(bundle)
        return bundle, intent

    def run_guard(self, bundle, intent):
        settings = policy()
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        self.assertEqual(GuardValidator(bundle, settings).validate(proposal, scenario, guard), [])
        return proposal, guard, {item["rule_id"]: item for item in guard["checks"]}

    def test_held_position_that_became_blocked_is_carried_with_warning(self):
        bundle, intent = self.blocked_holding_inputs()
        self.assertEqual(intent["items"][1]["intent"], "hold")
        proposal, guard, checks = self.run_guard(bundle, intent)
        self.assertTrue(guard["passed"])
        self.assertEqual(checks["HELD_NOT_TRADABLE"]["details"], ["2330.TW"])
        self.assertTrue(checks["HELD_NOT_TRADABLE"]["passed"])
        self.assertTrue(all(row["tradability_ok"] for row in checks["SCENARIOS"]["details"]))
        self.assertTrue(all(row["held_not_tradable"] == ["2330.TW"] for row in checks["SCENARIOS"]["details"]))
        self.assertFalse(any(order["symbol"] == "2330.TW" for order in proposal["order_proposal"]["orders"]))

    def test_blocked_holding_still_cannot_be_bought_or_sold(self):
        bundle, intent = self.blocked_holding_inputs()
        settings = policy()
        base = AllocationOrderEngine(bundle, settings).run(intent)
        template = next(order for order in base["order_proposal"]["orders"] if order["symbol"] == "2317.TW")
        for side in ("buy", "sell"):
            proposal = copy.deepcopy(base)
            proposal["order_proposal"]["orders"].append(dict(template, symbol="2330.TW", side=side))
            scenario = ScenarioEngine(bundle, settings).run(proposal)
            guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
            checks = {item["rule_id"]: item for item in guard["checks"]}
            self.assertFalse(guard["passed"], side)
            self.assertFalse(checks["TRADABLE"]["passed"], side)
            self.assertEqual(checks["TRADABLE"]["details"], ["2330.TW"], side)
            if side == "buy":
                # 買進受限持股時不適用續抱例外：情境也不通過，且不記 HELD_NOT_TRADABLE。
                self.assertFalse(checks["SCENARIOS"]["passed"])
                self.assertNotIn("HELD_NOT_TRADABLE", checks)

    def test_unheld_blocked_symbol_and_unknown_coverage_still_fail(self):
        from etf_agent.decision import canonical_sha256

        bundle, intent = self.blocked_holding_inputs()
        bundle["snapshot"]["tradable_symbols"] = ["2330.TW"]
        bundle["snapshot"]["not_tradable_symbols"] = ["2317.TW"]
        bundle["snapshot_sha256"] = canonical_sha256(bundle["snapshot"])
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        _, _, _, intent, *_ = chain_inputs(bundle)
        _, guard, checks = self.run_guard(bundle, intent)
        self.assertFalse(guard["passed"])
        self.assertFalse(checks["TRADABLE"]["passed"])
        self.assertNotIn("HELD_NOT_TRADABLE", checks)

    def test_guard_output_is_unchanged_when_nothing_held_is_blocked(self):
        bundle, momentum, debate, intent = valid_inputs()
        _, guard, checks = self.run_guard(bundle, intent)
        self.assertTrue(guard["passed"])
        self.assertNotIn("HELD_NOT_TRADABLE", checks)
        self.assertTrue(all("held_not_tradable" not in row for row in checks["SCENARIOS"]["details"]))

    def test_unfilled_forced_exit_fails_guard(self):
        bundle, momentum, debate, intent = valid_inputs()
        intent["items"][1]["intent"] = "forced_exit"
        settings = policy()
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        failed = {item["rule_id"] for item in guard["checks"] if not item["passed"]}
        self.assertIn("FORCED_EXIT_LIQUIDITY", failed)

    def test_revision_can_only_reduce_risk(self):
        bundle, momentum, debate, intent = valid_inputs()
        settings = policy()
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        actions = [
            {
                "type": "remove_candidate",
                "symbol": "2317.TW",
                "reason": "降低新增曝險。",
            },
            {
                "type": "increase_cash_buffer",
                "value": "0.10",
                "reason": "增加現金緩衝。",
            },
        ]
        review = risk_review(bundle, proposal, scenario, guard, "revise", 1, actions)
        self.assertEqual(
            RiskReviewValidator(bundle, settings).validate(proposal, scenario, guard, review),
            [],
        )
        effects = revision_effects(review, proposal)
        revised = AllocationOrderEngine(bundle, settings).run(
            intent,
            excluded_symbols=effects["excluded_symbols"],
            overrides=effects["overrides"],
            revision_count=1,
            parent_proposal_id=proposal["proposal_id"],
        )
        self.assertIn("2317.TW", revised["excluded_symbols"])
        self.assertFalse(
            any(
                item["symbol"] == "2317.TW" and item["side"] == "buy"
                for item in revised["order_proposal"]["orders"]
            )
        )

    def test_revision_history_replays_parent_chain_and_rejects_tampering(self):
        bundle, momentum, debate, intent = valid_inputs()
        settings = policy()
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        first_review = risk_review(
            bundle, proposal, scenario, guard, "revise", 1,
            [{"type": "increase_cash_buffer", "value": "0.10", "reason": "提高緩衝。"}],
        )
        history = RevisionHistoryBuilder(bundle, settings, intent).create(
            proposal, scenario, guard, first_review
        )
        effects = revision_effects(first_review, proposal)
        revised = AllocationOrderEngine(bundle, settings).run(
            intent,
            excluded_symbols=effects["excluded_symbols"],
            overrides=effects["overrides"],
            revision_count=1,
            parent_proposal_id=proposal["proposal_id"],
        )
        revised_scenario = ScenarioEngine(bundle, settings).run(revised)
        revised_guard = CompetitionGuardV2(bundle, settings).run(revised, revised_scenario)
        final_review = risk_review(bundle, revised, revised_scenario, revised_guard, "approve", 1)
        history = RevisionHistoryBuilder(bundle, settings, intent).append(
            history, revised, revised_scenario, revised_guard, final_review
        )
        self.assertEqual(
            RevisionHistoryValidator(bundle, settings, intent).validate(
                history, require_terminal=True
            ), []
        )
        tampered = copy.deepcopy(history)
        tampered["entries"][1]["proposal"]["parent_proposal_id"] = "proposal:wrong"
        tampered["content_sha256"] = artifact_content_sha256(tampered)
        errors = RevisionHistoryValidator(bundle, settings, intent).validate(tampered)
        self.assertTrue(any("Proposal" in error or "history_id" in error for error in errors))

    def test_turnover_revision_reduces_buy_order(self):
        bundle, momentum, debate, intent = valid_inputs()
        settings = policy()
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        review = risk_review(
            bundle,
            proposal,
            scenario,
            guard,
            "revise",
            1,
            [
                {
                    "type": "reduce_turnover_limit",
                    "value": "0.10",
                    "reason": "降低新增交易金額。",
                }
            ],
        )
        effects = revision_effects(review, proposal)
        revised = AllocationOrderEngine(bundle, settings).run(
            intent,
            excluded_symbols=effects["excluded_symbols"],
            overrides=effects["overrides"],
            revision_count=1,
            parent_proposal_id=proposal["proposal_id"],
        )
        self.assertLess(
            float(revised["order_proposal"]["turnover_rate"]),
            float(proposal["order_proposal"]["turnover_rate"]),
        )
        self.assertIn(
            "TURNOVER_LIMIT", {item["code"] for item in revised["constraint_flags"]}
        )

    def test_illegal_revision_and_fourth_revision_fail(self):
        bundle, momentum, debate, intent = valid_inputs()
        settings = policy()
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        review = risk_review(
            bundle,
            proposal,
            scenario,
            guard,
            "revise",
            4,
            [{"type": "reduce_max_stock_weight", "value": "0.60", "reason": "非法提高。"}],
        )
        errors = RiskReviewValidator(bundle, settings).validate(
            proposal, scenario, guard, review
        )
        self.assertTrue(any("超過允許範圍" in error for error in errors))
        with self.assertRaisesRegex(Exception, "不得提高上限"):
            revision_effects(review, proposal)

    def test_repository_is_idempotent_and_rejects_conflict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = DecisionRepository(Path(temp_dir))
            first = {"decision": {"status": "approved"}}
            path = repository.save("run-1", first)
            self.assertTrue((path / "manifest.json").exists())
            self.assertEqual(repository.save("run-1", first), path)
            with self.assertRaisesRegex(Exception, "不同內容"):
                repository.save("run-1", {"decision": {"status": "rejected"}})

    def test_repository_rejects_path_traversal_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = DecisionRepository(Path(temp_dir))
            with self.assertRaisesRegex(Exception, "run_id"):
                repository.save("../escape", {"decision": {"status": "approved"}})
            path = repository.save("safe-run", {"decision": {"status": "approved"}})
            (path / "decision.json").write_text('{"status":"tampered"}\n', encoding="utf-8")
            with self.assertRaisesRegex(Exception, "manifest 不一致"):
                repository.verify("safe-run")

    def test_repository_cleans_temporary_directory_after_interrupted_replace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = DecisionRepository(Path(temp_dir))
            with patch("etf_agent.core.artifact_store.os.replace", side_effect=OSError("stop")):
                with self.assertRaisesRegex(OSError, "stop"):
                    repository.save("interrupted", {"decision": {"status": "approved"}})
            self.assertFalse((Path(temp_dir) / "interrupted").exists())
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_cli_runs_validated_decision_pipeline_end_to_end(self):
        bundle, momentum, debate, intent, settings, team, _, _ = chain_inputs()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = {
                "bundle": root / "bundle.json",
                "momentum": root / "momentum.json",
                "debate": root / "debate.json",
                "intent": root / "intent.json",
                "policy": root / "policy.json",
                "proposal": root / "proposal.json",
                "scenario": root / "scenario.json",
                "guard": root / "guard.json",
                "review": root / "review.json",
                "history": root / "history.json",
                "decision": root / "decision.json",
                "team_inputs": root / "team_inputs.json",
            }
            for name, payload in (
                ("bundle", bundle), ("momentum", momentum), ("debate", debate),
                ("intent", intent), ("policy", settings), ("team_inputs", team),
            ):
                paths[name].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            script = Path(__file__).parents[1] / "cli" / "portfolio_decision.py"
            base = [sys.executable, str(script), "--bundle", str(paths["bundle"])]

            def run(*arguments):
                completed = subprocess.run(
                    base + list(arguments), capture_output=True, text=True, check=False
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            upstream = [
                "--momentum", str(paths["momentum"]), "--debate", str(paths["debate"]),
                "--intent", str(paths["intent"]), "--policy", str(paths["policy"]),
            ]
            run("compute-proposal", *upstream, "--output", str(paths["proposal"]))
            run("compute-scenarios", *upstream, "--proposal", str(paths["proposal"]), "--output", str(paths["scenario"]))
            run("compute-guard", *upstream, "--proposal", str(paths["proposal"]), "--scenario", str(paths["scenario"]), "--output", str(paths["guard"]))
            proposal = json.loads(paths["proposal"].read_text(encoding="utf-8"))
            scenario = json.loads(paths["scenario"].read_text(encoding="utf-8"))
            guard = json.loads(paths["guard"].read_text(encoding="utf-8"))
            review = risk_review(bundle, proposal, scenario, guard)
            paths["review"].write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            run("validate-risk", *upstream, "--proposal", str(paths["proposal"]), "--scenario", str(paths["scenario"]), "--guard", str(paths["guard"]), "--input", str(paths["review"]))
            run("build-history", *upstream, "--proposal", str(paths["proposal"]), "--scenario", str(paths["scenario"]), "--guard", str(paths["guard"]), "--review", str(paths["review"]), "--output", str(paths["history"]))
            risk_inputs = [
                *upstream, "--proposal", str(paths["proposal"]),
                "--scenario", str(paths["scenario"]), "--guard", str(paths["guard"]),
                "--review", str(paths["review"]), "--history", str(paths["history"]),
                "--team-inputs", str(paths["team_inputs"]),
            ]
            run("finalize", *risk_inputs, "--output", str(paths["decision"]))
            run("validate-decision", *risk_inputs, "--input", str(paths["decision"]))
            run(
                "save-run", *risk_inputs, "--decision", str(paths["decision"]),
                "--run-id", "cli-run", "--repository", str(root / "runs"),
            )
            self.assertTrue((root / "runs" / "cli-run" / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
