import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.automation.event_research_runner import run_material_event_research
from etf_agent.data import MarketDataDatabase
from etf_agent.research import ResearchResultValidator

from test_daily_pipeline import FakeRunner
from test_event_research_agent import snapshot_fixture


ROOT = Path(__file__).resolve().parents[1]
EVENT = {"symbol": "2330.TW", "evidence_id": "document-evidence-1"}
LINK = {"from": "營收年增", "to": "後續獲利", "relationship": "需確認毛利", "support": "inference", "evidence_ids": ["document-evidence-1"]}
FACT = {
    "event_summary": "八月營收年增 12.34%。",
    "verified_facts": [
        {"name": "營收年增率", "value": "12.34", "unit": "percent", "period": "2026-08",
         "evidence_id": "document-evidence-1", "comparison_basis": "previous_year_same_month"}
    ],
    "reference_frames": [
        {"kind": "prior_year_same_month", "actual": "100", "reference": "89", "unit": "TWD x 1000",
         "is_market_expectation": False, "evidence_ids": ["document-evidence-1"]}
    ],
    "open_questions": ["缺少市場共識。"],
}
BULL = {"thesis": "營收成長可能延續。", "causal_chain": [LINK], "assumptions": ["需求延續"], "failure_conditions": ["下月轉負"],
        "unresolved_questions": [], "evidence_ids": ["document-evidence-1"], "catalysts": ["新產能"]}
BEAR = {"thesis": "營收未必轉化為獲利。", "causal_chain": [LINK], "assumptions": ["毛利下滑"], "failure_conditions": ["毛利改善"],
        "unresolved_questions": [], "evidence_ids": ["document-evidence-1"], "risks": ["匯率"]}
ADJUDICATOR = {
    "prevailing_case": "indeterminate", "direction": "uncertain", "research_status": "pending",
    "rationale": "獲利傳導尚未確認。", "status_reason": "缺少市場共識與毛利資料。",
    "surviving_claims": ["營收年增"], "rejected_claims": [], "unresolved_questions": ["毛利率"],
    "event_type": "monthly_revenue", "impact_mechanism": "營收成長需轉化為獲利才有意義。",
    "evidence_quality": "verified",
    "novelty": {"classification": "update", "rationale": "例行月營收更新。", "prior_event_ids": []},
    "materiality": {"level": "medium", "horizon": "unknown", "affected_metrics": ["revenue"], "causal_chain": [LINK], "rationale": "影響待確認。"},
    "risk_flags": ["NO_MARKET_EXPECTATION"], "uncertainties": ["毛利"], "invalidation_signals": ["下月營收轉負"],
    "counter_evidence_ids": [], "price_status": "unavailable", "price_interpretation": "事件脈絡沒有價格特徵。",
}


class MaterialEventResearchTests(unittest.TestCase):
    def run_research(self, outputs):
        runner = FakeRunner(outputs)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = snapshot_fixture()
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            database = MarketDataDatabase(root / "prices.db")
            database.initialize()
            pipeline = DailyDecisionPipeline(ROOT, root, runner, root / "repo", log=lambda _: None)
            result = run_material_event_research(pipeline, snapshot, snapshot_path, root / "prices.db", [EVENT], "research")
            saved = sorted(path.name for path in root.glob("research_e0_*.json"))
        return snapshot, runner, result, saved

    def test_high_event_runs_isolated_debate_and_assembles_valid_result(self):
        snapshot, runner, result, saved = self.run_research(
            {"research_e0_fact": [FACT], "research_e0_bull": [BULL], "research_e0_bear": [BEAR], "research_e0_adjudicator": [ADJUDICATOR]}
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(ResearchResultValidator(snapshot).validate(result), [])
        item = result["items"][0]
        self.assertEqual(item["research_process"]["independence"], "bull_bear_independent")
        self.assertEqual(item["price_confirmation"]["status"], "unavailable")
        self.assertIn("research_e0_debate.json", saved)
        bull_prompt = next(prompt for name, prompt in runner.prompts if name == "research_e0_bull")
        self.assertIn("research_e0_fact.json", bull_prompt)
        self.assertNotIn("research_e0_bear", bull_prompt)

    def test_invalid_role_output_is_retried(self):
        broken = copy.deepcopy(BULL)
        broken["evidence_ids"] = ["no-such-evidence"]
        _, runner, result, _ = self.run_research(
            {"research_e0_fact": [FACT], "research_e0_bull": [broken, BULL], "research_e0_bear": [BEAR], "research_e0_adjudicator": [ADJUDICATOR]}
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual([name for name, _ in runner.prompts].count("research_e0_bull"), 2)

    def test_failed_event_degrades_result_without_inventing_research(self):
        candidate = dict(ADJUDICATOR, research_status="candidate")
        _, _, result, _ = self.run_research(
            {"research_e0_fact": [FACT], "research_e0_bull": [BULL], "research_e0_bear": [BEAR], "research_e0_adjudicator": [candidate, candidate]}
        )
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["items"], [])
        self.assertIn("document-evidence-1", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
