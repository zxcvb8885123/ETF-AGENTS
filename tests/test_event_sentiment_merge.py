import copy
import unittest

from etf_agent.decision.analysts import (
    AnalystReportValidator, analyst_report_names, report_envelope,
    seal_event_sentiment_report, seal_report, sentiment_report,
)
from etf_agent.decision.contracts import DecisionToolError, decision_bundle_sha256, canonical_sha256
from etf_agent.decision.event_sentiment_report import render_event_sentiment_report
from etf_agent.perception import MarketPerceptionApplicationService, PerceptionDataTools
from test_analysts import bundle_with_event, event_items, report, technical_items
from test_sentiment_analyst_agent import perception_bundle, sentiment_labels
from test_team_chain import team_world, finalize


def merged(bundle, items=None):
    rows = items or [dict(row, event_outlook=row["outlook"]) for row in event_items()]
    return seal_event_sentiment_report(bundle, report_envelope(bundle, "event", "merged"), rows)


class EventSentimentMergeTests(unittest.TestCase):
    def test_missing_sentiment_stays_unknown_and_event_retains_direction(self):
        bundle = bundle_with_event()
        output = merged(bundle)
        self.assertEqual(AnalystReportValidator(bundle, "event").validate(output), [])
        self.assertEqual(output["items"][1]["outlook"], "positive")
        self.assertEqual(output["items"][1]["sentiment"]["outlook"], "unknown")
        text = render_event_sentiment_report(bundle, output)
        self.assertIn("未收到通過來源與時間核對", text)
        self.assertNotIn("NO_LICENSED", text)

    def test_changed_sentiment_rejected_even_when_resealed(self):
        bundle = bundle_with_event()
        output = merged(bundle)
        output["items"][1]["sentiment"]["outlook"] = "positive"
        output = seal_report(output, output["items"])
        self.assertTrue(AnalystReportValidator(bundle, "event").validate(output))
        with self.assertRaises(DecisionToolError):
            render_event_sentiment_report(bundle, output)

    def test_no_event_and_no_sentiment_cannot_be_neutral(self):
        bundle = bundle_with_event()
        output = merged(bundle)
        output["items"][0]["outlook"] = "neutral"
        output = seal_report(output, output["items"])
        self.assertTrue(any("綜合看法" in e for e in AnalystReportValidator(bundle, "event").validate(output)))

    def test_event_direction_cannot_use_price_evidence(self):
        bundle = bundle_with_event()
        output = merged(bundle)
        output["items"][1]["findings"][0]["evidence_ids"] = ["price-2330"]
        output = seal_report(output, output["items"])
        self.assertTrue(any("只能引用" in e for e in AnalystReportValidator(bundle, "event").validate(output)))

    def test_model_cannot_overwrite_sentiment_channel(self):
        bundle = bundle_with_event()
        rows = [dict(row, event_outlook=row["outlook"], sentiment={}) for row in event_items()]
        with self.assertRaises(DecisionToolError):
            merged(bundle, rows)

    def test_approved_perception_adapter_is_preserved(self):
        bundle = bundle_with_event()
        data = perception_bundle()
        data["snapshot_id"] = bundle["snapshot_id"]
        data["decision_cutoff"] = bundle["decision_cutoff"]
        result = MarketPerceptionApplicationService(PerceptionDataTools(data)).build_result(
            ["2330.TW", "2317.TW"], sentiment_labels(), "daily")
        bundle["perception_inputs"] = [{"bundle": data, "result": result}]
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        output = merged(bundle)
        self.assertEqual(AnalystReportValidator(bundle, "event").validate(output), [])
        expected = next(row for row in sentiment_report(bundle)["items"] if row["symbol"] == "2330.TW")
        self.assertEqual(output["items"][1]["sentiment"], {k: v for k, v in expected.items() if k != "symbol"})
        # 下游可以引用合併報告內的固定情緒 finding，不要求模型重寫該通道。
        from etf_agent.decision import MomentumEngine, StancePacketValidator, build_stance_brief, seal_stance_packet
        from etf_agent.automation.daily_pipeline import degraded_research_result
        from test_stance import bull_items
        reports = {"technical": report(bundle, "technical", technical_items()),
                   "fundamental": report(bundle, "fundamental", [
                       {"symbol": s, "outlook": "unknown", "findings": [], "data_gaps": ["缺少財報"]}
                       for s in ("2317.TW", "2330.TW")]), "event": output}
        momentum = MomentumEngine(bundle).run()
        research = degraded_research_result(bundle["snapshot"], "research")
        rows = bull_items()
        rows[1]["claims"][0]["finding_ids"] = [expected["findings"][0]["finding_id"]]
        rows[1]["claims"][0]["evidence_ids"] = expected["findings"][0]["evidence_ids"]
        packet = seal_stance_packet(build_stance_brief(bundle, momentum, reports, research, "bull")["packet_envelope"], rows)
        self.assertEqual(StancePacketValidator(bundle, momentum, reports, research, "bull").validate(packet), [])

    def test_new_three_report_chain_and_legacy_four_report_chain_rebuild(self):
        bundle, momentum, debate, trade, policy, team, reports, research = team_world()
        self.assertEqual(len(analyst_report_names(reports)), 4)
        from etf_agent.decision.stance import shared_input_sha256
        self.assertEqual(shared_input_sha256(bundle, momentum, reports, research), canonical_sha256({
            "bundle_hash": bundle["bundle_sha256"], "momentum_result_id": momentum["result_id"],
            "analyst_reports": {name: reports[name]["content_sha256"] for name in ("technical", "fundamental", "event", "sentiment")},
            "research_result": canonical_sha256(research)}))
        new_reports = {key: value for key, value in reports.items() if key != "sentiment"}
        new_reports["event"] = merged(bundle)
        self.assertEqual(len(analyst_report_names(new_reports)), 3)
        from etf_agent.decision import (
            build_stance_brief, seal_stance_packet, build_research_debate, build_team_inputs,
            seal_trade_decision, trade_decision_envelope, apply_trade_decision, decision_policy_sha256,
        )
        from test_stance import bull_items, bear_items
        from test_trader import STANCE, items as trade_items
        bull = seal_stance_packet(build_stance_brief(bundle, momentum, new_reports, research, "bull")["packet_envelope"], bull_items())
        bear = seal_stance_packet(build_stance_brief(bundle, momentum, new_reports, research, "bear")["packet_envelope"], bear_items())
        debate = build_research_debate(bundle, bull, bear, "new-debate")
        trade = seal_trade_decision(trade_decision_envelope(bundle, debate, "new-trade"), trade_items())
        from test_portfolio_risk_decision import policy as base_policy
        from test_position_sizing import SIZING
        policy = base_policy()
        policy["position_sizing"] = copy.deepcopy(SIZING)
        policy = apply_trade_decision(policy, trade, STANCE, bundle)
        policy["content_sha256"] = decision_policy_sha256(policy)
        team = build_team_inputs(new_reports, research, STANCE)
        _, decision = finalize(bundle, momentum, debate, trade, policy, team)
        self.assertEqual(decision["status"], "approved", decision["errors"])
        with self.assertRaises(DecisionToolError):
            analyst_report_names({**new_reports, "sentiment": reports["sentiment"]})
        with self.assertRaises(DecisionToolError):
            analyst_report_names({key: value for key, value in reports.items() if key != "sentiment"})

    def test_future_event_and_version_mismatch_rejected(self):
        for field, value in (("available_at", "2026-10-01T00:00:00+00:00"),
                             ("available_at", "2026-09-19T09:00:00")):
            bundle = bundle_with_event()
            bundle["snapshot"]["documents"][0][field] = value
            bundle["snapshot_sha256"] = canonical_sha256(bundle["snapshot"])
            bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
            with self.assertRaises(DecisionToolError):
                AnalystReportValidator(bundle, "event")
        bundle = bundle_with_event()
        output = merged(bundle)
        output["snapshot_id"] = "other-snapshot"
        output = seal_report(output, output["items"])
        self.assertTrue(AnalystReportValidator(bundle, "event").validate(output))
