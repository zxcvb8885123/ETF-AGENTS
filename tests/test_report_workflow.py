import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from etf_agent.automation import (
    ReportWorkflowRepository,
    ReportWorkflowService,
)
from etf_agent.data import MarketDataDatabase


class _FakeTools:
    def status(self):
        return {
            "snapshot_id": "snapshot-1",
            "decision_cutoff": "2026-09-20T00:55:00+00:00",
            "usable": True,
            "document_count": 1,
            "evidence_count": 1,
        }

    def list_events(self, *, lookback_days, limit):
        return [
            {
                "event_id": "event-1",
                "symbol": "2330.TW",
                "document_type": "monthly_revenue",
                "published_at": "2026-09-19T00:00:00+00:00",
                "available_at": "2026-09-19T01:00:00+00:00",
                "source_evidence_id": "document-evidence-1",
            }
        ]

    def validate_result(self, payload):
        return []


class _FakeEventService:
    def __init__(self):
        self.tools = _FakeTools()

    def validate_file(self, path):
        return {"valid": True, "errors": []}


class _FakeResearchService:
    def build_files(self, json_output, markdown_output, report_id, generated_at):
        report = {
            "schema_version": "1.0",
            "report_id": report_id,
            "status": "degraded",
            "generated_at": generated_at,
        }
        json_output.write_text(json.dumps(report), encoding="utf-8")
        markdown_output.write_text("# Research Report\n", encoding="utf-8")
        return {"valid": True, "status": "degraded"}


def _snapshot():
    return {
        "snapshot_id": "snapshot-1",
        "decision_cutoff": "2026-09-20T00:55:00+00:00",
        "usable": True,
        "quality_flags": [],
        "universe_size": 1,
        "latest_trade_date": "2026-09-19",
        "source_evidence": [{"evidence_id": "document-evidence-1"}],
        "documents": [{"source_evidence_id": "document-evidence-1"}],
        "latest_prices": [],
    }


class ReportWorkflowTests(unittest.TestCase):
    def write_snapshot(self, root):
        snapshot_path = root / "snapshot.json"
        snapshot_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        database_path = root / "prices.db"
        MarketDataDatabase(database_path).initialize()
        return snapshot_path, database_path

    def test_empty_snapshot_documents_is_blocked_and_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = _snapshot()
            snapshot["documents"] = []
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            database_path = root / "prices.db"
            MarketDataDatabase(database_path).initialize()
            result = ReportWorkflowService(
                ReportWorkflowRepository(root / "runs")
            ).run(
                workflow_run_id="blocked-1",
                repository_root=root / "runs",
                reports_root=root / "reports",
                snapshot_path=snapshot_path,
                database_path=database_path,
                generated_at="2026-09-20T01:00:00+00:00",
            )
            self.assertEqual(result["status"], "blocked")
            self.assertTrue((root / "reports" / "latest.md").exists())
            self.assertTrue(
                (root / "runs" / "blocked-1" / "event_candidates.json").exists()
            )
            self.assertEqual(
                ReportWorkflowRepository(root / "runs").verify("blocked-1"),
                root / "runs" / "blocked-1",
            )

    def test_waiting_run_can_resume_with_verified_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, database_path = self.write_snapshot(root)
            repository = ReportWorkflowRepository(root / "runs")
            with patch(
                "etf_agent.automation.workflow.EventResearchApplicationService.from_paths",
                return_value=_FakeEventService(),
            ):
                waiting = ReportWorkflowService(repository).run(
                    workflow_run_id="waiting-1",
                    repository_root=root / "runs",
                    reports_root=root / "reports",
                    snapshot_path=snapshot_path,
                    database_path=database_path,
                    generated_at="2026-09-20T01:00:00+00:00",
                )
            self.assertEqual(waiting["status"], "waiting_for_agent")
            research_path = root / "research.json"
            research_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
            with patch(
                "etf_agent.automation.workflow.EventResearchApplicationService.from_paths",
                return_value=_FakeEventService(),
            ), patch(
                "etf_agent.automation.workflow.ResearchReportApplicationService.from_paths",
                return_value=_FakeResearchService(),
            ):
                resumed = ReportWorkflowService(repository).run(
                    workflow_run_id="resume-1",
                    repository_root=root / "runs",
                    reports_root=root / "reports",
                    snapshot_path=snapshot_path,
                    database_path=database_path,
                    research_path=research_path,
                    generated_at="2026-09-20T01:01:00+00:00",
                    parent_run_id="waiting-1",
                )
            self.assertEqual(resumed["status"], "waiting_for_decision")
            self.assertEqual(resumed["report_status"], "degraded")
            self.assertTrue((root / "runs" / "resume-1" / "research_report.json").exists())
            report = json.loads(
                (root / "runs" / "resume-1" / "execution_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["parent_workflow_run_id"], "waiting-1")

    def test_verified_research_with_decision_run_publishes_daily_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, database_path = self.write_snapshot(root)
            research_path = root / "research.json"
            research_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
            decision_repository = root / "decisions"
            decision_run_id = "decision-1"
            (decision_repository / decision_run_id).mkdir(parents=True)
            (decision_repository / decision_run_id / "manifest.json").write_text(
                json.dumps({"decision_run_id": decision_run_id}), encoding="utf-8"
            )
            pipeline_output = root / "pipeline" / "daily-resume-1"
            pipeline_output.mkdir(parents=True)
            (pipeline_output / "daily_report.json").write_text(
                json.dumps({"status": "ready", "snapshot_id": "snapshot-1"}),
                encoding="utf-8",
            )
            (pipeline_output / "daily_report_markdown.md").write_text(
                "# Daily Report\n", encoding="utf-8"
            )
            with patch(
                "etf_agent.automation.workflow.EventResearchApplicationService.from_paths",
                return_value=_FakeEventService(),
            ), patch(
                "etf_agent.automation.workflow.ResearchReportApplicationService.from_paths",
                return_value=_FakeResearchService(),
            ), patch(
                "etf_agent.automation.workflow.AutomationReportingApplicationService.execute_from_paths",
                return_value={
                    "ok": True,
                    "status": "succeeded",
                    "output": str(pipeline_output),
                },
            ):
                result = ReportWorkflowService(
                    ReportWorkflowRepository(root / "runs")
                ).run(
                    workflow_run_id="daily-resume-1",
                    repository_root=root / "runs",
                    reports_root=root / "reports",
                    snapshot_path=snapshot_path,
                    database_path=database_path,
                    research_path=research_path,
                    decision_repository=decision_repository,
                    decision_run_id=decision_run_id,
                    daily_report_repository=root / "pipeline",
                    generated_at="2026-09-20T01:01:00+00:00",
                )
            self.assertEqual(result["status"], "succeeded")
            self.assertTrue((root / "runs" / "daily-resume-1" / "daily_report.json").exists())
            self.assertTrue((root / "reports" / "latest_success.json").exists())
            report = json.loads(
                (root / "runs" / "daily-resume-1" / "execution_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["downstream"]["decision_run_id"], decision_run_id)

    def test_same_key_different_input_without_resume_is_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, database_path = self.write_snapshot(root)
            repository = ReportWorkflowRepository(root / "runs")
            with patch(
                "etf_agent.automation.workflow.EventResearchApplicationService.from_paths",
                return_value=_FakeEventService(),
            ):
                ReportWorkflowService(repository).run(
                    workflow_run_id="waiting-2",
                    repository_root=root / "runs",
                    reports_root=root / "reports",
                    snapshot_path=snapshot_path,
                    database_path=database_path,
                    generated_at="2026-09-20T01:00:00+00:00",
                )
                research_path = root / "research.json"
                research_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
                result = ReportWorkflowService(repository).run(
                    workflow_run_id="conflict-1",
                    repository_root=root / "runs",
                    reports_root=root / "reports",
                    snapshot_path=snapshot_path,
                    database_path=database_path,
                    research_path=research_path,
                    generated_at="2026-09-20T01:01:00+00:00",
                )
            self.assertEqual(result["status"], "failed")
            self.assertIn("相同執行鍵", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
