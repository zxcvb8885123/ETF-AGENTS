"""個股新聞走完每日鏈：只有新聞的股票送事件 Agent、event 2.3、多頭引用新聞、Finalizer 接受。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.decision import (
    MomentumEngine,
    apply_trade_decision,
    build_research_debate,
    build_stance_brief,
    build_team_inputs,
    decision_bundle_sha256,
    decision_policy_sha256,
    seal_stance_packet,
    seal_trade_decision,
    trade_decision_envelope,
)
from etf_agent.decision.analysts import EVENT_NEWS_VERSION, report_envelope, seal_event_market_report
from etf_agent.decision_delivery.builder import build_report
from etf_agent.perception.company_news.tools import build_pack

from test_analysts import EVENT_ID, event_items, report, technical_items
from test_company_news import ROWS, TERMS_URL
from test_daily_pipeline import FakeRunner
from test_portfolio_risk_decision import policy as base_policy
from test_position_sizing import SIZING
from test_stance import bear_items, bull_items, claim
from test_team_chain import finalize, tradable_bundle
from test_trader import STANCE, items as trade_items


ROOT = Path(__file__).resolve().parents[2]
REVIEW = {"source": "FINMIND_NEWS", "license_status": "approved", "terms_url": TERMS_URL,
          "usage_scope": "competition_research", "reviewed_at": "2026-09-19T12:00:00+00:00", "evidence": "測試核對"}


def news_bundle():
    bundle = tradable_bundle()
    bundle["company_news_input"] = build_pack(ROWS, REVIEW, ["2317.TW", "2330.TW"], bundle["snapshot_id"], bundle["decision_cutoff"])
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def news_ids(bundle):
    return {item["symbol"]: item["evidence_id"] for item in bundle["company_news_input"]["items"]}


def event_rows(bundle, legacy=False):
    ids = news_ids(bundle)
    rows = [{**item, "event_outlook": item["outlook"]} for item in event_items()]
    rows[0] = {"symbol": "2317.TW", "outlook": "positive", "event_outlook": "positive",
               "findings": [{"finding_id": "2317.TW:f1", "text": "依新聞標題，接單偏正面。", "evidence_ids": [ids["2317.TW"]]}],
               "data_gaps": ["新聞只有標題"],
               "events": [{"evidence_id": ids["2317.TW"], "materiality": "high", "summary": "依新聞標題：接單。",
                           "counter_evidence": [], "pending_confirmations": ["公司尚未公告"]}]}
    if not legacy:
        rows[1]["events"] = [*rows[1]["events"],
                             {"evidence_id": ids["2330.TW"], "materiality": "low", "summary": "依新聞標題：擴產。"}]
    return rows


class CompanyNewsChainTests(unittest.TestCase):
    def setUp(self):
        self.bundle = news_bundle()
        self.momentum = MomentumEngine(self.bundle).run()

    def test_news_only_symbol_goes_to_event_agent(self):
        unknown = [{"symbol": symbol, "outlook": "unknown", "findings": [], "data_gaps": ["NO_FINANCIAL_STATEMENTS"]}
                   for symbol in ("2317.TW", "2330.TW")]
        runner = FakeRunner({"technical_b0": [{"items": technical_items()}], "fundamental_b0": [{"items": unknown}],
                             "event_b0": [{"items": event_rows(self.bundle)}]})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            reports = pipeline.run_analyst_team(self.bundle, self.momentum)
            brief = json.loads((Path(directory) / "brief_event_b0.json").read_text(encoding="utf-8"))
        prompt = dict(runner.prompts)["event_b0"]
        self.assertIn("2317.TW", prompt)
        self.assertIn("news:finmind:", prompt)
        self.assertEqual(reports["event"]["schema_version"], EVENT_NEWS_VERSION)
        self.assertEqual(sorted(row["symbol"] for row in brief["symbols"]), ["2317.TW", "2330.TW"])

    def test_high_news_flows_to_bull_and_finalization(self):
        ids = news_ids(self.bundle)
        unknown = [{"symbol": symbol, "outlook": "unknown", "findings": [], "data_gaps": ["NO_FINANCIAL_STATEMENTS"]}
                   for symbol in ("2317.TW", "2330.TW")]
        reports = {
            "technical": report(self.bundle, "technical", technical_items()),
            "fundamental": report(self.bundle, "fundamental", unknown),
            "event": seal_event_market_report(self.bundle, report_envelope(self.bundle, "event", "report-event"), event_rows(self.bundle)),
        }
        bulls = bull_items()
        bulls[0]["claims"] = [claim("bull-2317-1", ids["2317.TW"], ["2317.TW:f1"])]
        bull = seal_stance_packet(build_stance_brief(self.bundle, self.momentum, reports, {}, "bull")["packet_envelope"], bulls)
        bear = seal_stance_packet(build_stance_brief(self.bundle, self.momentum, reports, {}, "bear")["packet_envelope"], bear_items())
        debate = build_research_debate(self.bundle, bull, bear, "debate-1")
        trade = seal_trade_decision(trade_decision_envelope(self.bundle, debate, "trade-1"), trade_items())
        policy = base_policy()
        policy["position_sizing"] = copy.deepcopy(SIZING)
        policy = apply_trade_decision(policy, trade, STANCE, self.bundle)
        policy["content_sha256"] = decision_policy_sha256(policy)
        team = build_team_inputs(reports, None, STANCE)
        artifacts, result = finalize(self.bundle, self.momentum, debate, trade, policy, team)
        self.assertEqual(result["status"], "approved", result["errors"])

        delivery = build_report({**artifacts, "decision_input": self.bundle, "momentum": self.momentum, "debate": debate,
                                 "intent": trade, "policy": policy, "decision": result, "team_inputs": team}, "news-run")
        self.assertIn(ids["2317.TW"], {row["evidence_id"] for row in delivery["evidence_index"]})

    def test_event_2_2_report_on_news_bundle_rejected(self):
        legacy = copy.deepcopy(self.bundle)
        del legacy["company_news_input"]
        legacy["bundle_sha256"] = decision_bundle_sha256(legacy)
        rows = [{**item, "event_outlook": item["outlook"]} for item in event_items()]
        old = seal_event_market_report(legacy, report_envelope(legacy, "event", "report-event"), rows)
        team = build_team_inputs({"technical": report(self.bundle, "technical", technical_items()),
                                  "fundamental": report(self.bundle, "fundamental", [
                                      {"symbol": s, "outlook": "unknown", "findings": [], "data_gaps": ["x"]}
                                      for s in ("2317.TW", "2330.TW")]),
                                  "event": old}, None, STANCE)
        from etf_agent.decision.analysts import AnalystReportValidator
        errors = AnalystReportValidator(self.bundle, "event").validate(team["analyst_reports"]["event"])
        self.assertTrue(any("schema_version" in error for error in errors), errors)
        self.assertTrue(any("未分級" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
