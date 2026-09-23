import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.perception import PerceptionDataTools
from etf_agent.reporting import (
    ResearchReportApplicationService,
    ResearchReportBuilder,
    ResearchReportError,
    ResearchReportMarkdownRenderer,
    ResearchReportValidator,
)
from tests.test_event_research_agent import snapshot_fixture, valid_result
from tests.test_sentiment_analyst_agent import (
    perception_bundle,
    valid_result as valid_perception_result,
)


GENERATED_AT = "2026-09-20T01:05:00+00:00"


def perception_result():
    tools = PerceptionDataTools(perception_bundle())
    return valid_perception_result(tools)


class ResearchReportTests(unittest.TestCase):
    def test_builds_completed_report_with_perception(self):
        builder = ResearchReportBuilder(
            snapshot_fixture(),
            valid_result(),
            perception_result(),
            perception_bundle(),
        )
        report = builder.build("report-1", GENERATED_AT)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["coverage"]["event_item_count"], 1)
        self.assertEqual(report["coverage"]["perception"], "available")
        self.assertEqual(report["companies"][0]["symbol"], "2330.TW")
        self.assertEqual(
            report["companies"][0]["events"][0]["event_id"],
            "monthly-revenue:2330:2026-08",
        )
        self.assertEqual(
            report["companies"][0]["perception"]["research_status"],
            "usable_secondary",
        )
        self.assertEqual(ResearchReportValidator(builder).validate(report), [])

    def test_missing_perception_produces_degraded_report(self):
        builder = ResearchReportBuilder(snapshot_fixture(), valid_result())
        report = builder.build("report-2", GENERATED_AT)
        self.assertEqual(report["status"], "degraded")
        self.assertIn("MARKET_PERCEPTION_UNAVAILABLE", report["missing_data"])
        company = report["companies"][0]
        self.assertEqual(company["perception"]["status"], "unavailable")
        self.assertIn("MARKET_PERCEPTION_UNAVAILABLE", company["missing_data"])
        markdown = ResearchReportMarkdownRenderer().render(report)
        self.assertIn("unavailable", markdown)
        self.assertIn("不包含權重、股數或訂單", markdown)

    def test_rejects_snapshot_and_cutoff_mismatch(self):
        research = valid_result()
        research["snapshot_id"] = "different"
        with self.assertRaisesRegex(ResearchReportError, "snapshot_id"):
            ResearchReportBuilder(snapshot_fixture(), research)

        research = valid_result()
        research["decision_cutoff"] = "2026-09-20T00:56:00+00:00"
        with self.assertRaisesRegex(ResearchReportError, "decision_cutoff"):
            ResearchReportBuilder(snapshot_fixture(), research)

    def test_rejects_missing_event_evidence(self):
        research = valid_result()
        research["items"][0]["evidence_ids"] = ["missing-evidence"]
        with self.assertRaisesRegex(ResearchReportError, "引用不存在"):
            ResearchReportBuilder(snapshot_fixture(), research)

    def test_perception_result_and_bundle_must_be_paired(self):
        with self.assertRaisesRegex(ResearchReportError, "必須同時提供"):
            ResearchReportBuilder(
                snapshot_fixture(), valid_result(), perception_result(), None
            )

    def test_validator_rejects_altered_report(self):
        builder = ResearchReportBuilder(snapshot_fixture(), valid_result())
        report = builder.build("report-3", GENERATED_AT)
        report["companies"][0]["events"][0]["direction"] = "negative"
        errors = ResearchReportValidator(builder).validate(report)
        self.assertTrue(any("確定性重建" in error for error in errors))

    def test_validator_rejects_transaction_fields(self):
        builder = ResearchReportBuilder(snapshot_fixture(), valid_result())
        report = builder.build("report-transaction", GENERATED_AT)
        report["orders"] = []
        errors = ResearchReportValidator(builder).validate(report)
        self.assertTrue(any("交易欄位" in error for error in errors))

    def test_rejects_failed_upstream_result(self):
        research = valid_result()
        research["status"] = "failed"
        with self.assertRaisesRegex(ResearchReportError, "completed 或 degraded"):
            ResearchReportBuilder(snapshot_fixture(), research)

    def test_markdown_escapes_untrusted_headings_and_html(self):
        research = valid_result()
        research["items"][0]["event_summary"] = "# 假標題\n<script>alert(1)</script>"
        builder = ResearchReportBuilder(snapshot_fixture(), research)
        report = builder.build("report-4", GENERATED_AT)
        markdown = ResearchReportMarkdownRenderer().render(report)
        self.assertIn("\\# 假標題", markdown)
        self.assertIn("\\<script\\>", markdown)
        self.assertNotIn("\n<script>", markdown)

    def test_generated_at_must_not_precede_cutoff(self):
        builder = ResearchReportBuilder(snapshot_fixture(), valid_result())
        with self.assertRaisesRegex(ResearchReportError, "generated_at"):
            builder.build("report-5", "2026-09-20T00:54:59+00:00")

    def test_application_service_writes_same_source_json_and_markdown(self):
        snapshot = snapshot_fixture()
        research = valid_result()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            research_path = root / "research.json"
            json_output = root / "report.json"
            markdown_output = root / "report.md"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            research_path.write_text(json.dumps(research), encoding="utf-8")
            service = ResearchReportApplicationService.from_paths(
                snapshot_path, research_path
            )
            result = service.build_files(
                json_output,
                markdown_output,
                report_id="report-6",
                generated_at=GENERATED_AT,
            )
            self.assertTrue(result["valid"])
            self.assertTrue(json_output.exists())
            self.assertTrue(markdown_output.exists())
            self.assertTrue(service.validate_file(json_output)["valid"])

    def test_sources_include_only_used_evidence(self):
        snapshot = snapshot_fixture()
        snapshot["source_evidence"].append(
            {
                "evidence_id": "unused-evidence",
                "source": "UNUSED",
                "authority": "other",
                "data_type": "other",
            }
        )
        report = ResearchReportBuilder(snapshot, valid_result()).build(
            "report-7", GENERATED_AT
        )
        source_ids = {source["evidence_id"] for source in report["sources"]}
        self.assertNotIn("unused-evidence", source_ids)
        self.assertIn("document-evidence-1", source_ids)
        self.assertIn("price-evidence-1", source_ids)


if __name__ == "__main__":
    unittest.main()
