import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.decision import (
    AllocationOrderEngine,
    DecisionPolicyValidator,
    DecisionToolError,
    ProposalValidator,
    TradeDecisionValidator,
    apply_trade_decision,
    build_research_debate,
    decision_policy_sha256,
    seal_trade_decision,
    trade_decision_envelope,
)

from test_daily_pipeline import FakeRunner
from test_position_sizing import SIZING
from test_portfolio_risk_decision import policy as base_policy
from test_stance import packets


ROOT = Path(__file__).resolve().parents[1]
STANCE = {"level": "neutral", "rationale": "市場中性。", "evidence_ids": ["price-2330"]}


def world():
    bundle, momentum, reports, research, bull, bear = packets()
    debate = build_research_debate(bundle, bull, bear, "debate-1")
    return bundle, momentum, reports, debate


def items():
    return [
        {"symbol": "2317.TW", "intent": "buy", "conviction": "medium", "rationale": "多頭論點成立。",
         "adopted_claim_ids": ["bull-2317-1"], "rejected_claim_ids": [], "unresolved_questions": [], "invalidation_conditions": ["跌破均線"]},
        {"symbol": "2330.TW", "intent": "hold", "conviction": None, "rationale": "多空相當，續抱。",
         "adopted_claim_ids": ["bull-2330-1"], "rejected_claim_ids": ["bear-2330-1"], "unresolved_questions": [], "invalidation_conditions": []},
    ]


def decision(bundle, debate, values):
    return seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), values)


class TradeDecisionValidatorTests(unittest.TestCase):
    def test_valid_decision_handles_every_claim(self):
        bundle, momentum, _, debate = world()
        self.assertEqual(TradeDecisionValidator(bundle, momentum, debate).validate(decision(bundle, debate, items())), [])

    def test_rejects_holding_direction_conviction_and_claim_violations(self):
        bundle, momentum, _, debate = world()
        cases = []
        add_unheld = items()
        add_unheld[0]["intent"] = "add"
        cases.append((add_unheld, "未持股不得 add"))
        buy_held = items()
        buy_held[1] = dict(buy_held[1], intent="buy", conviction="low")
        cases.append((buy_held, "已持股不得 buy"))
        no_conviction = items()
        no_conviction[0]["conviction"] = None
        cases.append((no_conviction, "必須給 conviction"))
        extra_conviction = items()
        extra_conviction[1]["conviction"] = "high"
        cases.append((extra_conviction, "只有 buy／add 可以給 conviction"))
        unhandled = items()
        unhandled[1]["rejected_claim_ids"] = []
        cases.append((unhandled, "未處理全部 claim"))
        foreign = items()
        foreign[0]["rejected_claim_ids"] = ["bear-2330-1"]
        cases.append((foreign, "不屬於該股票的 claim"))
        no_bull = items()
        no_bull[0] = dict(no_bull[0], adopted_claim_ids=[], rejected_claim_ids=["bull-2317-1"])
        cases.append((no_bull, "必須採納至少一個多頭 claim"))
        trim_without_bear = items()
        trim_without_bear[1] = dict(trim_without_bear[1], intent="trim")
        cases.append((trim_without_bear, "必須採納至少一個空頭 claim"))
        missing = items()[:1]
        cases.append((missing, "未裁決全部股票"))
        for values, message in cases:
            errors = TradeDecisionValidator(bundle, momentum, debate).validate(decision(bundle, debate, values))
            self.assertTrue(any(message in error for error in errors), (message, errors))

    def test_buy_requires_available_momentum(self):
        bundle, momentum, _, debate = world()
        insufficient = copy.deepcopy(momentum)
        for item in insufficient["items"]:
            item["status"] = "insufficient"
        errors = TradeDecisionValidator(bundle, insufficient, debate).validate(decision(bundle, debate, items()))
        self.assertTrue(any("動能資料" in error for error in errors), errors)


class TradeDecisionAllocationTests(unittest.TestCase):
    def sized_policy(self, bundle, trade):
        base = base_policy()
        base["position_sizing"] = copy.deepcopy(SIZING)
        base["content_sha256"] = decision_policy_sha256(base)
        policy = apply_trade_decision(base, trade, STANCE, bundle)
        policy["content_sha256"] = decision_policy_sha256(policy)
        return policy

    def test_conviction_and_cash_stance_flow_into_existing_allocation(self):
        bundle, _, _, debate = world()
        trade = decision(bundle, debate, items())
        policy = self.sized_policy(bundle, trade)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        self.assertEqual(policy["cash_buffer_rate"], "0.10")
        self.assertEqual(policy["position_sizing"]["conviction_by_symbol"], {"2317.TW": "medium"})
        # 配置引擎直接讀取 TradeDecision 的 symbol／intent，不需要轉接。
        proposal = AllocationOrderEngine(bundle, policy).run(trade)
        self.assertEqual(proposal["trade_intent_result_id"], "trade-1")
        self.assertEqual(list(proposal["position_sizing"]["target_weights"]), ["2317.TW"])
        self.assertGreater(Decimal(proposal["position_sizing"]["target_weights"]["2317.TW"]), 0)
        self.assertEqual(ProposalValidator(bundle, policy, trade).validate(proposal), [])

    def test_invalid_cash_stance_is_rejected(self):
        bundle, _, _, debate = world()
        base = base_policy()
        base["position_sizing"] = copy.deepcopy(SIZING)
        with self.assertRaisesRegex(DecisionToolError, "現金姿態無效"):
            apply_trade_decision(base, decision(bundle, debate, items()), dict(STANCE, evidence_ids=["no-such"]), bundle)


class TraderRunTests(unittest.TestCase):
    def test_trader_batches_retry_and_merge(self):
        bundle, momentum, reports, debate = world()
        broken = items()
        broken[1]["rejected_claim_ids"] = []
        runner = FakeRunner({"trader_b0": [{"items": broken}, {"items": items()}]})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            trade = pipeline.run_trader(bundle, momentum, reports, debate, "run-1")

        self.assertEqual([item["intent"] for item in trade["items"]], ["buy", "hold"])
        self.assertEqual(len(runner.prompts), 2)
        self.assertIn("未處理全部 claim", runner.prompts[1][1])


if __name__ == "__main__":
    unittest.main()
