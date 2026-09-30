import copy
import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionFinalizer,
    DecisionPolicyValidator,
    DecisionResultValidator,
    DecisionToolError,
    ProposalValidator,
    RevisionHistoryBuilder,
    ScenarioEngine,
    apply_trade_decision,
    build_decision_policy,
    decision_policy_sha256,
    seal_trade_decision,
    trade_decision_envelope,
)
from etf_agent.decision.contracts import DecisionContext
from etf_agent.decision.sizing import conviction_weights, validate_cash_stance

from test_portfolio_risk_decision import chain_inputs, policy as base_policy, risk_review, valid_inputs


SIZING = {
    "method": "conviction_volatility_v1",
    "conviction_multipliers": {"high": "1.5", "medium": "1", "low": "0.5"},
    "volatility_floor": "0.01",
    "cash_buffer_by_stance": {"aggressive": "0.03", "neutral": "0.10", "defensive": "0.20"},
}


def sized_inputs(tier_2317="high", tier_2330="low", stance="neutral"):
    """(bundle, intent, cash_stance, policy)：2317 買進、2330 加碼的交易決策，policy 為尚未綁定的基礎 policy。"""
    from test_trader import items as trade_items  # 延後匯入：test_trader 也匯入本模組的 SIZING

    bundle, _, debate, _, _, _, _, _ = chain_inputs()
    decisions = trade_items()
    decisions[0] = dict(decisions[0], conviction=tier_2317)
    decisions[1] = dict(
        decisions[1], intent="add", conviction=tier_2330,
        adopted_claim_ids=["bull-2330-1"], rejected_claim_ids=["bear-2330-1"],
    )
    intent = seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), decisions)
    cash_stance = {"level": stance, "rationale": "市場廣度中性。", "evidence_ids": ["price-2330"]}
    policy = base_policy()
    policy["position_sizing"] = copy.deepcopy(SIZING)
    policy["content_sha256"] = decision_policy_sha256(policy)
    return bundle, intent, cash_stance, policy


def sized_policy(policy, intent, cash_stance, bundle):
    result = apply_trade_decision(policy, intent, cash_stance, bundle)
    result["content_sha256"] = decision_policy_sha256(result)
    return result


class CashStanceValidatorTests(unittest.TestCase):
    def test_cash_stance_requires_known_level_and_existing_evidence(self):
        bundle, _, stance, _ = sized_inputs()
        context = DecisionContext(bundle)
        ok = []
        validate_cash_stance(context, stance, "cash_stance", ok)
        self.assertEqual(ok, [])
        cases = (
            (dict(stance, level="all_in"), "cash_stance.level"),
            (dict(stance, evidence_ids=["price-9999"]), "引用不存在"),
            (dict(stance, evidence_ids=[]), "evidence_ids 不得為空"),
            (dict(stance, weight="0.3"), "未允許欄位"),
            (None, "cash_stance 必須是物件"),
        )
        for case, message in cases:
            errors = []
            validate_cash_stance(context, case, "cash_stance", errors)
            self.assertTrue(any(message in error for error in errors), (message, errors))


class ConvictionAllocationTests(unittest.TestCase):
    def test_water_filling_caps_and_redistributes_budget(self):
        weights = conviction_weights(
            ["A", "B", "C"],
            {"A": "high", "B": "medium", "C": "low"},
            {"high": Decimal("1.5"), "medium": Decimal("1"), "low": Decimal("0.5")},
            {"A": Decimal("0.01"), "B": Decimal("0.02"), "C": Decimal("0.02")},
            Decimal("0.01"),
            {"A": Decimal("0.3"), "B": Decimal("0.3"), "C": Decimal("0.3")},
            Decimal("0.8"),
        )
        self.assertEqual(weights["A"], Decimal("0.3"))
        self.assertEqual(weights["B"], Decimal("0.3"))
        self.assertEqual(weights["C"], Decimal("0.2"))

    def test_sized_proposal_follows_tiers_and_rebuilds(self):
        bundle, intent, stance, policy = sized_inputs("high", "low")
        policy = sized_policy(policy, intent, stance, bundle)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        proposal = AllocationOrderEngine(bundle, policy).run(intent)

        targets = {key: Decimal(value) for key, value in proposal["position_sizing"]["target_weights"].items()}
        self.assertGreater(targets["2317.TW"], targets["2330.TW"])
        self.assertLessEqual(sum(targets.values()), Decimal("0.95"))
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(proposal), [])

        _, flipped_intent, flipped_stance, flipped_policy = sized_inputs("low", "high")
        flipped = AllocationOrderEngine(
            bundle, sized_policy(flipped_policy, flipped_intent, flipped_stance, bundle)
        ).run(flipped_intent)
        flipped_targets = {key: Decimal(value) for key, value in flipped["position_sizing"]["target_weights"].items()}
        self.assertLess(flipped_targets["2317.TW"], targets["2317.TW"])

    def test_sized_policy_replays_through_guard_history_and_finalizer(self):
        bundle, momentum, debate, intent, settings, team, _, _ = chain_inputs()
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        self.assertEqual(list(proposal["position_sizing"]["target_weights"]), ["2317.TW"])
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        self.assertTrue(guard["passed"])
        review = risk_review(bundle, proposal, scenario, guard)
        history = RevisionHistoryBuilder(bundle, settings, intent).create(proposal, scenario, guard, review)
        result = DecisionFinalizer(bundle, settings, momentum, debate, intent, team).run(
            proposal, scenario, guard, review, history
        )
        self.assertEqual(result["status"], "approved", result["errors"])
        self.assertEqual(
            DecisionResultValidator(bundle, settings, momentum, debate, intent, team).validate(
                proposal, scenario, guard, review, history, result
            ),
            [],
        )

    def test_agent_cash_stance_sets_buffer_and_invested_budget(self):
        bundle, intent, stance, policy = sized_inputs(stance="aggressive")
        aggressive = sized_policy(policy, intent, stance, bundle)
        self.assertEqual(aggressive["cash_buffer_rate"], "0.03")
        self.assertEqual(aggressive["position_sizing"]["cash_stance"], "aggressive")
        _, _, defensive_stance, defensive_base = sized_inputs(stance="defensive")
        defensive = sized_policy(defensive_base, intent, defensive_stance, bundle)
        self.assertEqual(defensive["cash_buffer_rate"], "0.20")

        def invested(settings):
            proposal = AllocationOrderEngine(bundle, settings).run(intent)
            return sum(Decimal(value) for value in proposal["position_sizing"]["target_weights"].values())

        self.assertGreater(invested(aggressive), invested(defensive))
        self.assertEqual(DecisionPolicyValidator(bundle).validate(defensive), [])

    def test_policy_rejects_cash_buffer_not_matching_stance_or_above_ceiling(self):
        bundle, intent, stance, policy = sized_inputs()
        tampered = sized_policy(policy, intent, stance, bundle)
        tampered["cash_buffer_rate"] = "0.01"
        tampered["content_sha256"] = decision_policy_sha256(tampered)
        errors = DecisionPolicyValidator(bundle).validate(tampered)
        self.assertTrue(any("cash_stance 對應" in error for error in errors), errors)

        policy["position_sizing"]["cash_buffer_by_stance"]["defensive"] = policy["cash_weight_ceiling"]
        policy["content_sha256"] = decision_policy_sha256(policy)
        errors = DecisionPolicyValidator(bundle).validate(policy)
        self.assertTrue(any("低於 cash_weight_ceiling" in error for error in errors), errors)

    def test_sizing_enabled_without_trade_decision_binding_fails_closed(self):
        bundle, intent, _, policy = sized_inputs()
        with self.assertRaisesRegex(DecisionToolError, "尚未綁定交易決策"):
            AllocationOrderEngine(bundle, policy).run(intent)

    def test_policy_rejects_invalid_sizing_config(self):
        bundle, intent, stance, policy = sized_inputs()
        policy["position_sizing"]["conviction_multipliers"]["low"] = "2"
        policy["content_sha256"] = decision_policy_sha256(policy)
        errors = DecisionPolicyValidator(bundle).validate(policy)
        self.assertTrue(any("high ≥ medium ≥ low" in error for error in errors))
        with self.assertRaisesRegex(DecisionToolError, "尚未綁定的基礎 policy"):
            apply_trade_decision(sized_policy(sized_inputs()[3], intent, stance, bundle), intent, stance, bundle)


class DecisionPolicyBuilderTests(unittest.TestCase):
    def template(self):
        policy = base_policy()
        for field in (
            "lot_size", "commission_rate", "sell_tax_rate", "minimum_commission",
            "max_stock_weight", "special_weight_limits", "max_sector_weight",
            "min_positions", "max_positions", "cash_weight_ceiling",
            "minimum_active_share", "reuse_sell_proceeds", "sector_classification_version",
            "sector_by_symbol", "content_sha256",
        ):
            policy.pop(field)
        return policy

    def sectors(self, available_at="2026-09-19T00:00:00+00:00"):
        return {
            "version": "sector:fixture",
            "available_at": available_at,
            "sector_by_symbol": {"2330.TW": "industry:24", "2317.TW": "industry:31"},
        }

    def test_copies_hard_rules_and_sector_map(self):
        bundle = valid_inputs()[0]
        policy = build_decision_policy(self.template(), bundle, self.sectors())
        self.assertEqual(policy["max_stock_weight"], bundle["rules"]["max_stock_weight"])
        self.assertEqual(policy["cash_weight_ceiling"], bundle["rules"]["cash_weight_must_be_below"])
        self.assertEqual(policy["sector_by_symbol"]["2330.TW"], "industry:24")
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])

    def test_rejects_template_hard_fields_future_or_incomplete_sectors(self):
        bundle = valid_inputs()[0]
        template = self.template()
        template["max_stock_weight"] = "0.9"
        with self.assertRaisesRegex(DecisionToolError, "硬性規則"):
            build_decision_policy(template, bundle, self.sectors())
        with self.assertRaisesRegex(DecisionToolError, "晚於 decision_cutoff"):
            build_decision_policy(self.template(), bundle, self.sectors("2026-09-30T00:00:00+00:00"))
        partial = self.sectors()
        partial["sector_by_symbol"].pop("2317.TW")
        with self.assertRaisesRegex(DecisionToolError, "未覆蓋"):
            build_decision_policy(self.template(), bundle, partial)


class PolicyBuilderCliTests(unittest.TestCase):
    def test_cli_build_policy_produces_valid_base_policy(self):
        bundle = valid_inputs()[0]
        root = Path(__file__).resolve().parents[1]
        template = json.loads((root / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        # 內附樣板的時間晚於 fixture cutoff；只替換時間以驗證其餘欄位可直接使用。
        template["available_at"] = "2026-09-19T00:00:00+00:00"
        sectors = DecisionPolicyBuilderTests().sectors()
        with tempfile.TemporaryDirectory() as directory:
            paths = {name: Path(directory) / ("%s.json" % name) for name in ("bundle", "template", "sectors")}
            for name, payload in (("bundle", bundle), ("template", template), ("sectors", sectors)):
                paths[name].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            output = Path(directory) / "policy.json"
            completed = subprocess.run(
                [
                    sys.executable, str(root / "cli" / "portfolio_decision.py"), "--bundle", str(paths["bundle"]),
                    "build-policy", "--template", str(paths["template"]), "--sector", str(paths["sectors"]),
                    "--output", str(output),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            policy = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])


if __name__ == "__main__":
    unittest.main()
