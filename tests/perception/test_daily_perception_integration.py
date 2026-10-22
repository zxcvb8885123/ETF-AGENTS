"""授權資料到每日決策的整合與拒絕路徑。"""
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.agent_runner import AgentCall
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.automation.perception_stage import build_daily_perception
from etf_agent.decision import AnalystReportValidator, decision_bundle_sha256
from etf_agent.decision.analysts import sentiment_report
from etf_agent.decision.contracts import artifact_content_sha256
from etf_agent.perception import MarketPerceptionApplicationService, PerceptionDataTools, PerceptionToolError
from test_portfolio_decision_agent import decision_bundle
from test_sentiment_analyst_agent import perception_bundle, sentiment_labels


class DailyPerceptionIntegrationTests(unittest.TestCase):
    def service(self):
        return MarketPerceptionApplicationService(PerceptionDataTools(perception_bundle()))

    def test_build_covers_unavailable_symbols_and_both_consensus_versions(self):
        result = self.service().build_result(["2330.TW", "2317.TW"], sentiment_labels(), "daily")
        self.assertEqual(result["status"], "degraded")
        by_symbol = {row["symbol"]: row for row in result["items"]}
        self.assertEqual(by_symbol["2317.TW"]["research_status"], "unavailable")
        metric = by_symbol["2330.TW"]["consensus_metrics"][0]
        self.assertEqual(metric["contributor_count"], 3)
        self.assertIn("e1-evidence", metric["evidence_ids"])
        self.assertIn("e5-evidence", metric["evidence_ids"])

    def test_missing_label_rejected(self):
        with self.assertRaises(PerceptionToolError):
            self.service().build_result(["2330.TW"], sentiment_labels()[:-1], "daily")

    def test_decision_adapter_rebuild_rejects_tampering(self):
        bundle = decision_bundle()
        data = perception_bundle()
        data["snapshot_id"] = bundle["snapshot_id"]
        data["decision_cutoff"] = bundle["decision_cutoff"]
        result = MarketPerceptionApplicationService(PerceptionDataTools(data)).build_result(
            ["2330.TW", "2317.TW"], sentiment_labels(), "daily")
        bundle["perception_inputs"] = [{"bundle": data, "result": result}]
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        report = sentiment_report(bundle)
        self.assertEqual(AnalystReportValidator(bundle, "sentiment").validate(report), [])
        row = next(row for row in report["items"] if row["symbol"] == "2330.TW")
        self.assertEqual(len(row["findings"]), 2)
        row["findings"][0]["text"] = "遭改寫的方向"
        report["content_sha256"] = artifact_content_sha256(report)
        self.assertTrue(AnalystReportValidator(bundle, "sentiment").validate(report))

    def test_automatic_label_stage_resume_and_source_change(self):
        class Runner:
            calls = 0
            def run(self, task, run_dir):
                self.calls += 1
                return AgentCall(task.name, 1, {"labels": sentiment_labels()})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = perception_bundle()
            source = root / "source.json"
            snapshot = root / "snapshot.json"
            source.write_text(json.dumps(data), encoding="utf-8")
            snapshot.write_text(json.dumps({"snapshot_id": data["snapshot_id"], "decision_cutoff": data["decision_cutoff"],
                                           "latest_prices": [{"symbol": "2330.TW"}, {"symbol": "2317.TW"}]}), encoding="utf-8")
            runner = Runner()
            pipeline = DailyDecisionPipeline(root, root, runner, root / "decisions", resume=True, log=lambda x: None)
            output = build_daily_perception(pipeline, snapshot, source)
            self.assertEqual(json.loads(output.read_text())["status"], "degraded")
            build_daily_perception(pipeline, snapshot, source)
            self.assertEqual(runner.calls, 1)
            data["sentiment_items"][0]["text"] += " 修訂"
            source.write_text(json.dumps(data), encoding="utf-8")
            build_daily_perception(pipeline, snapshot, source)
            self.assertEqual(runner.calls, 2)

    def test_unknown_license_and_future_input_rejected(self):
        for mode in ("license", "future"):
            data = perception_bundle()
            if mode == "license":
                data["source_evidence"][0]["license_status"] = "unknown"
            else:
                data["sentiment_items"][0]["available_at"] = "2026-09-21T01:00:00+00:00"
            with self.assertRaises(PerceptionToolError):
                PerceptionDataTools(data)
