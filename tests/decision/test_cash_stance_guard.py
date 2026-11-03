"""風險 Agent 現金姿態（typed_evidence_v1）：至少一個市場層級證據、負面市場情緒不得 aggressive、brief 不附交易意圖統計。"""

import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline, degraded_research_result
from etf_agent.automation.pipeline_base import output_revision_diff
from etf_agent.decision import MARKET_REGIME_EVIDENCE_ID, MomentumEngine, apply_trade_decision, DecisionToolError
from etf_agent.decision.contracts import DecisionContext
from etf_agent.decision.sizing import validate_cash_stance
from etf_agent.perception.market_fear_greed import attach_fear_greed, evaluate

from test_daily_pipeline import FakeRunner
from test_market_fear_greed import pack
from test_typed_evidence import typed_bundle, typed_reports

ROOT = Path(__file__).resolve().parents[2]
STANCE = {"level": "neutral", "breadth": "balanced", "rationale": "市場中性。", "evidence_ids": ["price-2330", MARKET_REGIME_EVIDENCE_ID]}


def negative_sentiment_bundle(typed=True):
    """恐懼貪婪 fixture 的分數 34.2 屬 fear，全市場情緒為 negative。"""
    from test_analysts import bundle_with_event
    base = typed_bundle() if typed else bundle_with_event()
    return attach_fear_greed(base, {"bundle": pack(), "result": evaluate(pack())})


class CashStanceGuardTests(unittest.TestCase):
    def errors(self, bundle, stance):
        errors = []
        validate_cash_stance(DecisionContext(bundle), stance, "cash_stance", errors)
        return errors

    def test_market_level_evidence_is_required_under_the_gate(self):
        bundle = typed_bundle()
        self.assertEqual(self.errors(bundle, STANCE), [])
        price_only = dict(STANCE, evidence_ids=["price-2330"])
        self.assertTrue(any("市場層級證據" in error for error in self.errors(bundle, price_only)))
        from test_analysts import bundle_with_event
        self.assertEqual(self.errors(bundle_with_event(), price_only), [])  # 舊 bundle 維持原行為

    def test_fear_greed_components_count_as_market_level(self):
        bundle = negative_sentiment_bundle()
        context = DecisionContext(bundle)
        components = [row["evidence_id"] for row in context.market_sentiment["fear_greed"]["components"] if row.get("evidence_id")]
        self.assertTrue(components)
        self.assertTrue(set(components) <= context.market_evidence_ids)
        self.assertEqual(self.errors(bundle, dict(STANCE, evidence_ids=["price-2330", components[0]])), [])

    def test_negative_sentiment_rejects_aggressive_only_under_the_gate(self):
        bundle = negative_sentiment_bundle()
        self.assertEqual(DecisionContext(bundle).market_sentiment["outlook"], "negative")
        aggressive = dict(STANCE, level="aggressive")
        self.assertTrue(any("一致性護欄" in error for error in self.errors(bundle, aggressive)))
        self.assertEqual(self.errors(bundle, dict(STANCE, level="defensive")), [])
        legacy = negative_sentiment_bundle(typed=False)
        self.assertEqual(self.errors(legacy, dict(aggressive, evidence_ids=["price-2330"])), [])

    def test_apply_trade_decision_enforces_the_guard(self):
        from test_position_sizing import SIZING
        from test_portfolio_risk_decision import policy as base_policy
        import copy
        bundle = negative_sentiment_bundle()
        base = base_policy()
        base["position_sizing"] = copy.deepcopy(SIZING)
        decision = {"decision_id": "trade-1", "content_sha256": "x" * 64, "items": []}
        with self.assertRaisesRegex(DecisionToolError, "一致性護欄"):
            apply_trade_decision(base, decision, dict(STANCE, level="aggressive"), bundle)
        self.assertEqual(apply_trade_decision(base, decision, STANCE, bundle)["position_sizing"]["cash_stance"], "neutral")


class CashStanceBriefTests(unittest.TestCase):
    def test_brief_omits_trader_statistics_and_lists_market_level_ids(self):
        bundle = typed_bundle()
        momentum = MomentumEngine(bundle).run()
        reports = typed_reports(bundle)
        runner = FakeRunner({"cash_stance": [dict(STANCE, evidence_ids=["price-2330"]), STANCE]})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            stance = pipeline.run_cash_stance(bundle, momentum, reports)
            brief = json.loads((Path(directory) / "brief_cash_stance.json").read_text(encoding="utf-8"))
        self.assertEqual(stance, STANCE)
        self.assertEqual(len(runner.prompts), 2)
        self.assertNotIn("trade_intents", brief)
        self.assertNotIn("buy_add_convictions", brief)
        self.assertEqual(brief["market_level_evidence_ids"], [MARKET_REGIME_EVIDENCE_ID])
        self.assertEqual(brief["regime_assessment"]["evidence_id"], MARKET_REGIME_EVIDENCE_ID)
        self.assertIn(MARKET_REGIME_EVIDENCE_ID, brief["citable_evidence_ids"])
        self.assertIn("market_level_evidence_ids", runner.prompts[0][1])
        self.assertIn("不附交易 Agent 的意圖與信心統計", runner.prompts[0][1])
        # 驗證回饋的措辭中性：並列兩種合法修法。
        self.assertIn("（1）降級", runner.prompts[1][1])
        self.assertIn("（2）補依據", runner.prompts[1][1])
        self.assertIn("市場層級證據", runner.prompts[1][1])
        diff = next(call["revision_diff"] for call in pipeline.calls if call.get("revision_diff") is not None)
        self.assertEqual(diff["changed_fields"], 0)


class RevisionDiffTests(unittest.TestCase):
    def test_diff_marks_direction_per_symbol_and_counts(self):
        before = {"items": [
            {"symbol": "2317.TW", "intent": "hold", "conviction": None, "adopted_claim_ids": ["a"], "rejected_claim_ids": []},
            {"symbol": "2330.TW", "strength": "none", "claims": []},
        ]}
        after = {"items": [
            {"symbol": "2317.TW", "intent": "add", "conviction": "medium", "adopted_claim_ids": ["a", "b"], "rejected_claim_ids": []},
            {"symbol": "2330.TW", "strength": "weak", "claims": [{}]},
        ]}
        diff = output_revision_diff(before, after)
        self.assertEqual(diff["more_aggressive"], 3)  # intent、conviction、strength；計數變化標 other
        self.assertEqual(diff["more_conservative"], 0)
        changes = {(c["symbol"], c["field"]): c for c in diff["changes"]}
        self.assertEqual(changes[("2317.TW", "intent")]["direction"], "more_aggressive")
        self.assertEqual(changes[("2317.TW", "adopted_claim_ids_count")]["direction"], "other")
        self.assertEqual(changes[("2330.TW", "strength")]["after"], "weak")
        calmer = output_revision_diff(after, before)
        self.assertEqual(calmer["more_conservative"], 3)
        self.assertEqual(output_revision_diff({"level": "neutral"}, {"level": "aggressive"})["changes"][0]["direction"], "more_aggressive")
        self.assertEqual(output_revision_diff(after, after)["changed_fields"], 0)


if __name__ == "__main__":
    unittest.main()
