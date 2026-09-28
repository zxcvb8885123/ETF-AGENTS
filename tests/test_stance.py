import copy
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline, degraded_research_result
from etf_agent.decision import (
    MomentumEngine,
    ResearchDebateBundleValidator,
    StancePacketValidator,
    build_research_debate,
    build_stance_brief,
    seal_stance_packet,
    sentiment_unavailable_report,
)

from test_analysts import EVENT_ID, bundle_with_event, event_items, report, technical_items
from test_daily_pipeline import FakeRunner


ROOT = Path(__file__).resolve().parents[1]


def inputs():
    bundle = bundle_with_event()
    momentum = MomentumEngine(bundle).run()
    unknown = [
        {"symbol": symbol, "outlook": "unknown", "findings": [], "data_gaps": ["NO_FINANCIAL_STATEMENTS"]}
        for symbol in ("2317.TW", "2330.TW")
    ]
    reports = {
        "technical": report(bundle, "technical", technical_items()),
        "fundamental": report(bundle, "fundamental", unknown),
        "event": report(bundle, "event", event_items()),
        "sentiment": sentiment_unavailable_report(bundle),
    }
    research = degraded_research_result(bundle["snapshot"], "research-1")
    return bundle, momentum, reports, research


def claim(claim_id, evidence, findings=()):
    return {"claim_id": claim_id, "text": "依分析報告。", "evidence_ids": [evidence], "finding_ids": list(findings)}


def bull_items():
    return [
        {"symbol": "2317.TW", "strength": "moderate", "claims": [claim("bull-2317-1", "price-2317", ["tech-2317-1"])], "invalidation_conditions": ["跌破均線"]},
        {"symbol": "2330.TW", "strength": "weak", "claims": [claim("bull-2330-1", EVENT_ID, ["event-2330-1"])], "invalidation_conditions": []},
    ]


def bear_items():
    return [
        {"symbol": "2317.TW", "strength": "none", "claims": [], "invalidation_conditions": []},
        {"symbol": "2330.TW", "strength": "moderate", "claims": [claim("bear-2330-1", "price-2330")], "invalidation_conditions": ["基本面資料補齊且正向"]},
    ]


def packets():
    bundle, momentum, reports, research = inputs()
    bull = seal_stance_packet(build_stance_brief(bundle, momentum, reports, research, "bull")["packet_envelope"], bull_items())
    bear = seal_stance_packet(build_stance_brief(bundle, momentum, reports, research, "bear")["packet_envelope"], bear_items())
    return bundle, momentum, reports, research, bull, bear


class StancePacketTests(unittest.TestCase):
    def test_valid_bull_and_bear_form_a_debate(self):
        bundle, momentum, reports, research, bull, bear = packets()
        self.assertEqual(StancePacketValidator(bundle, momentum, reports, research, "bull").validate(bull), [])
        self.assertEqual(StancePacketValidator(bundle, momentum, reports, research, "bear").validate(bear), [])
        debate = build_research_debate(bundle, bull, bear, "debate-1")
        self.assertEqual(ResearchDebateBundleValidator(bundle, momentum, reports, research).validate(debate), [])

    def test_briefs_share_identical_content_except_role(self):
        bundle, momentum, reports, research = inputs()
        bull = build_stance_brief(bundle, momentum, reports, research, "bull")
        bear = build_stance_brief(bundle, momentum, reports, research, "bear")
        self.assertEqual(bull["symbols"], bear["symbols"])
        self.assertEqual(bull["packet_envelope"]["shared_input_sha256"], bear["packet_envelope"]["shared_input_sha256"])
        self.assertNotEqual(bull["packet_envelope"]["dependencies"], bear["packet_envelope"]["dependencies"])

    def test_rejects_coverage_strength_claim_and_reference_violations(self):
        bundle, momentum, reports, research, _, _ = packets()
        envelope = build_stance_brief(bundle, momentum, reports, research, "bull")["packet_envelope"]
        cases = []
        cases.append((bull_items()[:1], "未對全部股票表態"))
        none_with_claims = bull_items()
        none_with_claims[0]["strength"] = "none"
        cases.append((none_with_claims, "strength=none"))
        strong_empty = bull_items()
        strong_empty[1] = dict(strong_empty[1], strength="strong", claims=[])
        cases.append((strong_empty, "至少有一個 claim"))
        prefix = bull_items()
        prefix[0]["claims"][0]["claim_id"] = "bear-2317-1"
        cases.append((prefix, "必須以 bull- 開頭"))
        foreign = bull_items()
        foreign[0]["claims"][0]["evidence_ids"] = ["price-2330"]
        cases.append((foreign, "不屬於"))
        other_finding = bull_items()
        other_finding[0]["claims"][0]["finding_ids"] = ["tech-2330-1"]
        cases.append((other_finding, "finding 不屬於"))
        missing_finding = bull_items()
        missing_finding[0]["claims"][0]["finding_ids"] = ["tech-9999-1"]
        cases.append((missing_finding, "不存在的 finding"))
        numbers = bull_items()
        numbers[0]["target_weight"] = "0.1"
        cases.append((numbers, "不得包含配置"))
        for items, message in cases:
            errors = StancePacketValidator(bundle, momentum, reports, research, "bull").validate(seal_stance_packet(envelope, items))
            self.assertTrue(any(message in error for error in errors), (message, errors))

    def test_packet_built_from_different_inputs_is_rejected(self):
        bundle, momentum, reports, research, bull, bear = packets()
        other_research = dict(research, run_id="research-2")
        errors = StancePacketValidator(bundle, momentum, reports, other_research, "bull").validate(bull)
        self.assertTrue(any("隔離輸入不一致" in error for error in errors), errors)
        swapped = build_research_debate(bundle, bear, bull, "debate-1")
        errors = ResearchDebateBundleValidator(bundle, momentum, reports, research).validate(swapped)
        self.assertTrue(any("依序剛好包含 bull 與 bear" in error for error in errors), errors)


class ResearchTeamTests(unittest.TestCase):
    def test_team_runs_isolated_roles_and_validates_debate(self):
        bundle, momentum, reports, research = inputs()
        broken = copy.deepcopy(bear_items())
        broken[1]["claims"][0]["claim_id"] = "bull-2330-9"
        runner = FakeRunner({"bull_b0": [{"items": bull_items()}], "bear_b0": [{"items": broken}, {"items": bear_items()}]})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            debate = pipeline.run_research_team(bundle, momentum, reports, research, "run-1")

        self.assertEqual([packet["role"] for packet in debate["packets"]], ["bull", "bear"])
        self.assertEqual([name for name, _ in runner.prompts], ["bull_b0", "bear_b0", "bear_b0"])
        bull_prompt = runner.prompts[0][1]
        self.assertIn("brief_bull_b0.json", bull_prompt)
        self.assertNotIn("bear", bull_prompt.replace("$bull-researcher", ""))


if __name__ == "__main__":
    unittest.main()
