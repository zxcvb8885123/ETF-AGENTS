import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from etf_agent.automation import (
    ReportWorkflowError,
    ReportWorkflowRepository,
    ReportWorkflowService,
)
from etf_agent.data import MarketDataDatabase
from etf_agent.research import ResearchToolError


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
            reused = ReportWorkflowService(repository).run(
                workflow_run_id="repeat-1",
                repository_root=root / "runs",
                reports_root=root / "reports",
                snapshot_path=snapshot_path,
                database_path=database_path,
                research_path=research_path,
                generated_at="2026-09-20T01:02:00+00:00",
            )
            self.assertTrue(reused["reused"])
            self.assertEqual(reused["workflow_run_id"], "resume-1")
            self.assertEqual(reused["status"], "waiting_for_decision")

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
                    execution_mode="fixture",
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

    def _run(self, root, snapshot_path, database_path, run_id, **kwargs):
        return ReportWorkflowService(ReportWorkflowRepository(root / "runs")).run(
            workflow_run_id=run_id,
            repository_root=root / "runs",
            reports_root=root / "reports",
            snapshot_path=snapshot_path,
            database_path=database_path,
            generated_at="2026-09-20T01:00:00+00:00",
            **kwargs,
        )

    def _execution_report(self, root, run_id):
        return json.loads(
            (root / "runs" / run_id / "execution_report.json").read_text(encoding="utf-8")
        )

    def test_unusable_snapshot_is_failed_at_snapshot_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = _snapshot()
            snapshot["usable"] = False
            snapshot["quality_flags"] = ["MISSING_PRICES"]
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            database_path = root / "prices.db"
            MarketDataDatabase(database_path).initialize()
            result = self._run(root, snapshot_path, database_path, "unusable-1")
            self.assertEqual(result["status"], "failed")
            report = self._execution_report(root, "unusable-1")
            self.assertEqual([stage["name"] for stage in report["stages"]], ["snapshot"])
            self.assertIn("MISSING_PRICES", report["errors"][0])

    def test_event_data_error_is_failed_at_event_data_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, database_path = self.write_snapshot(root)
            with patch(
                "etf_agent.automation.workflow.EventResearchApplicationService.from_paths",
                side_effect=ResearchToolError("資料庫不可讀"),
            ):
                result = self._run(root, snapshot_path, database_path, "event-fail-1")
            self.assertEqual(result["status"], "failed")
            report = self._execution_report(root, "event-fail-1")
            self.assertEqual(
                [(stage["name"], stage["status"]) for stage in report["stages"]],
                [("event_data", "failed")],
            )

    def test_invalid_research_result_is_failed_without_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, database_path = self.write_snapshot(root)
            research_path = root / "research.json"
            research_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
            invalid = _FakeEventService()
            invalid.validate_file = lambda path: {"valid": False, "errors": ["引用不存在"]}
            with patch(
                "etf_agent.automation.workflow.EventResearchApplicationService.from_paths",
                return_value=invalid,
            ):
                result = self._run(
                    root, snapshot_path, database_path, "invalid-research-1",
                    research_path=research_path,
                )
            self.assertEqual(result["status"], "failed")
            report = self._execution_report(root, "invalid-research-1")
            self.assertEqual(
                [(stage["name"], stage["status"]) for stage in report["stages"]],
                [("snapshot", "succeeded"), ("event_data", "succeeded"), ("research_report", "failed")],
            )
            self.assertIn("引用不存在", report["errors"][0])
            self.assertFalse((root / "runs" / "invalid-research-1" / "research_report.json").exists())

    def test_official_missing_virtual_account_run_fails_at_account_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, database_path = self.write_snapshot(root)
            research_path = root / "research.json"
            research_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
            with patch(
                "etf_agent.automation.workflow.EventResearchApplicationService.from_paths",
                return_value=_FakeEventService(),
            ), patch(
                "etf_agent.automation.workflow.ResearchReportApplicationService.from_paths",
                return_value=_FakeResearchService(),
            ):
                result = self._run(
                    root, snapshot_path, database_path, "account-fail-1",
                    research_path=research_path,
                    decision_repository=root / "decisions",
                    decision_run_id="decision-1",
                    virtual_account_repository=root / "accounts",
                    virtual_account_account_id="ai-cup-2026",
                    virtual_account_run_id="missing-run",
                )
            self.assertEqual(result["status"], "failed")
            report = self._execution_report(root, "account-fail-1")
            self.assertEqual(report["stages"][-1]["name"], "account_snapshot")
            self.assertEqual(report["stages"][-1]["status"], "failed")

    def test_official_daily_report_requires_virtual_account_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, database_path = self.write_snapshot(root)
            with self.assertRaisesRegex(ReportWorkflowError, "VirtualAccount prepare-day run"):
                ReportWorkflowService(ReportWorkflowRepository(root / "runs")).run(
                    workflow_run_id="official-daily-1",
                    repository_root=root / "runs",
                    reports_root=root / "reports",
                    snapshot_path=snapshot_path,
                    database_path=database_path,
                    decision_repository=root / "decisions",
                    decision_run_id="decision-1",
                    generated_at="2026-09-20T01:01:00+00:00",
                )

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
            with patch(
                "etf_agent.automation.workflow.EventResearchApplicationService.from_paths",
                return_value=_FakeEventService(),
            ), patch(
                "etf_agent.automation.workflow.ResearchReportApplicationService.from_paths",
                return_value=_FakeResearchService(),
            ):
                resumed = ReportWorkflowService(repository).run(
                    workflow_run_id="legitimate-resume-1",
                    repository_root=root / "runs",
                    reports_root=root / "reports",
                    snapshot_path=snapshot_path,
                    database_path=database_path,
                    research_path=research_path,
                    generated_at="2026-09-20T01:02:00+00:00",
                    parent_run_id="waiting-2",
                )
            self.assertEqual(resumed["status"], "waiting_for_decision")

    def test_resume_rejects_missing_or_changed_parent_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, database_path = self.write_snapshot(root)
            repository = ReportWorkflowRepository(root / "runs")
            service = ReportWorkflowService(repository)
            with self.assertRaisesRegex(ReportWorkflowError, "找不到 workflow run"):
                service.run(
                    workflow_run_id="resume-missing",
                    repository_root=root / "runs",
                    reports_root=root / "reports",
                    snapshot_path=snapshot_path,
                    database_path=database_path,
                    parent_run_id="missing-parent",
                    generated_at="2026-09-20T01:00:00+00:00",
                )
            with patch(
                "etf_agent.automation.workflow.EventResearchApplicationService.from_paths",
                return_value=_FakeEventService(),
            ):
                service.run(
                    workflow_run_id="parent-1",
                    repository_root=root / "runs",
                    reports_root=root / "reports",
                    snapshot_path=snapshot_path,
                    database_path=database_path,
                    generated_at="2026-09-20T01:00:00+00:00",
                )
            altered = _snapshot()
            altered["latest_trade_date"] = "2026-09-18"
            snapshot_path.write_text(json.dumps(altered), encoding="utf-8")
            with self.assertRaisesRegex(ReportWorkflowError, "Snapshot／cutoff／模式不一致"):
                service.run(
                    workflow_run_id="resume-altered",
                    repository_root=root / "runs",
                    reports_root=root / "reports",
                    snapshot_path=snapshot_path,
                    database_path=database_path,
                    parent_run_id="parent-1",
                    generated_at="2026-09-20T01:01:00+00:00",
                )

    def test_missing_decision_run_publishes_failure_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, database_path = self.write_snapshot(root)
            research_path = root / "research.json"
            research_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
            with patch(
                "etf_agent.automation.workflow.EventResearchApplicationService.from_paths",
                return_value=_FakeEventService(),
            ), patch(
                "etf_agent.automation.workflow.ResearchReportApplicationService.from_paths",
                return_value=_FakeResearchService(),
            ):
                result = ReportWorkflowService(
                    ReportWorkflowRepository(root / "runs")
                ).run(
                    workflow_run_id="missing-decision-1",
                    repository_root=root / "runs",
                    reports_root=root / "reports",
                    snapshot_path=snapshot_path,
                    database_path=database_path,
                    research_path=research_path,
                    decision_repository=root / "decisions",
                    decision_run_id="absent-1",
                    daily_report_repository=root / "pipeline",
                    execution_mode="fixture",
                    generated_at="2026-09-20T01:01:00+00:00",
                )
            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                (root / "runs" / "missing-decision-1" / "failure_report.json").exists()
            )
            self.assertFalse(
                (root / "runs" / "missing-decision-1" / "daily_report.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
