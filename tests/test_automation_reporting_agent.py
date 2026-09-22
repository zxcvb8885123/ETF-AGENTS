import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation import (
    AutomationReportingApplicationService,
    PipelineRepository,
)
from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionFinalizer,
    DecisionRepository,
    MomentumEngine,
    RevisionHistoryBuilder,
    ScenarioEngine,
)
from tests.test_portfolio_risk_decision import policy, risk_review, valid_inputs


GENERATED_AT = "2026-09-20T01:05:00+00:00"


def research_result(snapshot):
    return {
        "schema_version": "2.1",
        "run_id": "automation-research-1",
        "snapshot_id": snapshot["snapshot_id"],
        "decision_cutoff": snapshot["decision_cutoff"],
        "skill_version": "2.1.0",
        "status": "completed",
        "items": [],
        "errors": [],
    }


def save_decision_run(root: Path, rejected=False):
    bundle, momentum, debate, intent = valid_inputs()
    settings = policy()
    proposal = AllocationOrderEngine(bundle, settings).run(intent)
    scenario = ScenarioEngine(bundle, settings).run(proposal)
    guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
    review = risk_review(
        bundle, proposal, scenario, guard, "reject" if rejected else "approve"
    )
    history = RevisionHistoryBuilder(bundle, settings, intent).create(
        proposal, scenario, guard, review
    )
    decision = DecisionFinalizer(bundle, settings, momentum, debate, intent).run(
        proposal, scenario, guard, review, history
    )
    artifacts = {
        "decision_input": bundle,
        "momentum": momentum,
        "debate": debate,
        "intent": intent,
        "policy": settings,
        "proposal": proposal,
        "scenario": scenario,
        "guard": guard,
        "risk_review": review,
        "revision_history": history,
        "decision": decision,
    }
    repository = DecisionRepository(root)
    repository.save("decision-run-1", artifacts)
    return bundle["snapshot"], decision


class AutomationReportingTests(unittest.TestCase):
    def write_inputs(self, root: Path, rejected=False):
        decision_root = root / "decision-runs"
        snapshot, decision = save_decision_run(decision_root, rejected=rejected)
        snapshot_path = root / "snapshot.json"
        research_path = root / "research.json"
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        research_path.write_text(
            json.dumps(research_result(snapshot), ensure_ascii=False), encoding="utf-8"
        )
        return snapshot_path, research_path, decision_root, decision

    def test_builds_idempotent_degraded_daily_report_and_validates_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, research_path, decision_root, decision = self.write_inputs(root)
            output = root / "pipeline-runs"
            result = AutomationReportingApplicationService.execute_from_paths(
                "pipeline-1", output, "fixture", GENERATED_AT,
                snapshot_path, research_path, decision_root, "decision-run-1",
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "succeeded")
            run_dir = output / "pipeline-1"
            self.assertTrue((run_dir / "daily_report.json").exists())
            self.assertTrue((run_dir / "daily_report_markdown.md").exists())
            report = json.loads((run_dir / "daily_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "degraded")
            self.assertEqual(report["decision"]["status"], decision["status"])
            self.assertIn("不會送出主辦平台", (run_dir / "daily_report_markdown.md").read_text(encoding="utf-8"))
            self.assertEqual(PipelineRepository(output).verify("pipeline-1"), run_dir)

            service = AutomationReportingApplicationService.from_paths(
                snapshot_path, research_path, decision_root, "decision-run-1"
            )
            self.assertTrue(service.validate_run(output, "pipeline-1")["valid"])
            same_run = AutomationReportingApplicationService.execute_from_paths(
                "pipeline-1", output, "fixture", GENERATED_AT,
                snapshot_path, research_path, decision_root, "decision-run-1",
            )
            self.assertEqual(same_run["status"], "succeeded")
            repeated = AutomationReportingApplicationService.execute_from_paths(
                "pipeline-repeat", output, "fixture", GENERATED_AT,
                snapshot_path, research_path, decision_root, "decision-run-1",
            )
            self.assertTrue(repeated["reused"])
            self.assertEqual(repeated["pipeline_run_id"], "pipeline-1")
            self.assertEqual(repeated["daily_report_id"], "daily-report:pipeline-1")

    def test_same_execution_key_with_different_input_creates_failure_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, research_path, decision_root, _ = self.write_inputs(root)
            output = root / "pipeline-runs"
            AutomationReportingApplicationService.execute_from_paths(
                "pipeline-original", output, "fixture", GENERATED_AT,
                snapshot_path, research_path, decision_root, "decision-run-1",
            )
            research = json.loads(research_path.read_text(encoding="utf-8"))
            research["skill_version"] = "2.1.1"
            research_path.write_text(json.dumps(research, ensure_ascii=False), encoding="utf-8")
            result = AutomationReportingApplicationService.execute_from_paths(
                "pipeline-conflict", output, "fixture", GENERATED_AT,
                snapshot_path, research_path, decision_root, "decision-run-1",
            )
            self.assertFalse(result["ok"])
            self.assertIn("相同執行鍵", result["errors"][0])
            self.assertTrue((output / "pipeline-conflict" / "failure_report.json").exists())

    def test_rejects_altered_report_and_archive_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, research_path, decision_root, _ = self.write_inputs(root)
            output = root / "pipeline-runs"
            AutomationReportingApplicationService.execute_from_paths(
                "pipeline-2", output, "fixture", GENERATED_AT,
                snapshot_path, research_path, decision_root, "decision-run-1",
            )
            report_path = output / "pipeline-2" / "daily_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["decision"]["orders"] = []
            report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(Exception, "manifest 不一致"):
                PipelineRepository(output).verify("pipeline-2")

    def test_snapshot_content_mismatch_creates_failure_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, research_path, decision_root, _ = self.write_inputs(root)
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["quality_flags"] = ["TAMPERED"]
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            output = root / "pipeline-runs"
            result = AutomationReportingApplicationService.execute_from_paths(
                "pipeline-3", output, "fixture", GENERATED_AT,
                snapshot_path, research_path, decision_root, "decision-run-1",
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            self.assertTrue((output / "pipeline-3" / "failure_report.json").exists())
            self.assertFalse((output / "pipeline-3" / "daily_report.json").exists())

    def test_rejected_decision_creates_failure_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, research_path, decision_root, _ = self.write_inputs(
                root, rejected=True
            )
            output = root / "pipeline-runs"
            result = AutomationReportingApplicationService.execute_from_paths(
                "pipeline-4", output, "fixture", GENERATED_AT,
                snapshot_path, research_path, decision_root, "decision-run-1",
            )
            self.assertFalse(result["ok"])
            failure = json.loads(
                (output / "pipeline-4" / "failure_report.json").read_text(encoding="utf-8")
            )
            self.assertIn("DecisionResult.status=rejected", failure["errors"][0])

    def test_cli_build_validate_and_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, research_path, decision_root, _ = self.write_inputs(root)
            output = root / "pipeline-runs"
            script = Path(__file__).parents[1] / "cli" / "daily_report.py"
            common = [
                "--snapshot", str(snapshot_path),
                "--research", str(research_path), "--decision-repository", str(decision_root),
                "--decision-run-id", "decision-run-1", "--pipeline-run-id", "pipeline-cli",
                "--repository", str(output),
            ]
            completed = subprocess.run(
                [sys.executable, str(script), "run", *common, "--execution-mode", "fixture", "--generated-at", GENERATED_AT],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            completed = subprocess.run(
                [sys.executable, str(script), "validate", *common], capture_output=True, text=True, check=False
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            completed = subprocess.run(
                [sys.executable, str(script), "verify-run", "--pipeline-run-id", "pipeline-cli", "--repository", str(output)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
