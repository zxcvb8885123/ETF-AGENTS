import copy
import json
import tempfile
import unittest
from pathlib import Path

from cli.fundamental_research import FundamentalResearchApplication
from etf_agent.fundamentals import (
    FundamentalMetricsCalculator,
    FundamentalResearchResultValidator,
    FundamentalSnapshotTools,
    FundamentalToolError,
)


CUTOFF = "2026-09-21T01:00:00+00:00"
GENERATED_AT = "2026-09-21T02:00:00+00:00"


def fact(value):
    return {
        "source_field": "fixture",
        "value": value,
        "value_status": "provided",
        "currency": "TWD",
        "unit_multiplier": 1000,
    }


def statement(document_id, statement_type, year, facts, industry="ci"):
    if statement_type == "income_statement":
        period_start = "%d-01-01" % year
        period_end = "%d-06-30" % year
        period_kind = "cumulative_to_quarter"
    else:
        period_start = None
        period_end = "%d-06-30" % year
        period_kind = "point_in_time"
    evidence_id = "financial-evidence-%d" % document_id
    return {
        "document_id": document_id,
        "source": "TWSE_MOPS_%s" % statement_type.upper(),
        "external_id": "fixture:%d" % document_id,
        "version": 1,
        "document_type": "financial_statement",
        "symbol": "2330.TW",
        "title": "fixture financial statement",
        "body": "fixture",
        "source_url": "fixture://financial/%d" % document_id,
        "published_at": "2026-09-20T00:00:00+00:00",
        "available_at": "2026-09-20T01:00:00+00:00",
        "content_sha256": "financial-sha-%d" % document_id,
        "source_evidence_id": evidence_id,
        "financial_statement": {
            "statement_type": statement_type,
            "industry": industry,
            "fiscal_year": year,
            "fiscal_quarter": 2,
            "period_start": period_start,
            "period_end": period_end,
            "period_kind": period_kind,
            "reporting_scope": "consolidated",
            "currency": "TWD",
            "unit_multiplier": 1000,
            "reported_at": "2026-09-20T00:00:00+00:00",
            "source_published_at": None,
            "mapping_version": "fixture-v1",
            "facts": facts,
        },
    }


def snapshot_fixture():
    documents = [
        statement(
            11,
            "income_statement",
            2026,
            {"revenue": fact("100"), "operating_profit": fact("20"), "net_income": fact("12")},
        ),
        statement(
            12,
            "balance_sheet",
            2026,
            {"total_assets": fact("500"), "total_liabilities": fact("200")},
        ),
        statement(
            13,
            "income_statement",
            2025,
            {"revenue": fact("80"), "operating_profit": fact("8"), "net_income": fact("8")},
        ),
    ]
    return {
        "snapshot_id": "fundamental-snapshot-1",
        "decision_cutoff": CUTOFF,
        "usable": True,
        "quality_flags": [],
        "source_evidence": [
            {
                "evidence_id": document["source_evidence_id"],
                "source": document["source"],
                "authority": "mops",
                "data_type": "financial_statement",
            }
            for document in documents
        ],
        "documents": documents,
        "latest_prices": [],
    }


def request():
    return {
        "request_id": "fundamental-request-1",
        "symbols": ["2330.TW"],
        "fiscal_year": 2026,
        "fiscal_quarter": 2,
        "required_statement_types": ["income_statement", "balance_sheet"],
        "metric_keys": [
            "liabilities_to_assets_pct",
            "net_income_yoy_pct",
            "operating_margin_pct",
            "operating_margin_pp_yoy",
            "revenue_yoy_pct",
        ],
        "policy_version": "1.0",
    }


def result_draft(bundle, metrics):
    company = metrics["items"][0]
    metric_ids = sorted(
        metric["metric_id"]
        for metric in company["metrics"]
        if metric["status"] == "available"
    )
    evidence_ids = sorted(
        statement["source_evidence_id"] for statement in bundle["companies"][0]["statements"]
    )
    return {
        "schema_version": "1.0",
        "run_id": "fundamental-run-1",
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "bundle_id": bundle["bundle_id"],
        "bundle_sha256": bundle["content_sha256"],
        "metrics_id": metrics["metrics_id"],
        "metrics_sha256": metrics["content_sha256"],
        "skill_version": "1.0.0",
        "status": "completed",
        "items": [
            {
                "symbol": "2330.TW",
                "research_status": "completed",
                "status_reason": "當期與去年同期必要欄位均可比較。",
                "observations": [
                    {
                        "text": "營收、淨利、營業利益率與負債占資產比率均由同一快照重算。",
                        "evidence_ids": evidence_ids,
                        "metric_ids": metric_ids,
                    }
                ],
                "assumptions": ["官方欄位映射版本在比較期間可比。"],
                "invalidation_conditions": ["官方更正財報版本後需以新快照重跑。"],
                "limitations": [],
                "evidence_ids": evidence_ids,
                "metric_ids": metric_ids,
            }
        ],
        "errors": [],
    }


class FundamentalResearchAgentTests(unittest.TestCase):
    def setUp(self):
        self.tools = FundamentalSnapshotTools(snapshot_fixture())
        self.bundle = self.tools.build_bundle(
            request(), bundle_id="fundamental-bundle-1", generated_at=GENERATED_AT
        )
        self.calculator = FundamentalMetricsCalculator(self.tools, self.bundle)
        self.metrics = self.calculator.compute(
            metrics_id="fundamental-metrics-1", computed_at=GENERATED_AT
        )

    def test_builds_fixed_bundle_and_recomputes_metrics(self):
        self.assertEqual(self.bundle["status"], "completed")
        self.assertEqual(self.bundle["coverage"]["completed_symbol_count"], 1)
        self.assertEqual(self.tools.validate_bundle(self.bundle), [])
        self.assertEqual(self.metrics["status"], "completed")
        values = {
            item["metric_key"]: item["value"]
            for item in self.metrics["items"][0]["metrics"]
        }
        self.assertEqual(values["operating_margin_pct"], "20")
        self.assertEqual(values["liabilities_to_assets_pct"], "40")
        self.assertEqual(values["revenue_yoy_pct"], "25")
        self.assertEqual(values["net_income_yoy_pct"], "50")
        self.assertEqual(values["operating_margin_pp_yoy"], "10")
        self.assertEqual(self.calculator.validate(self.metrics), [])

    def test_result_validator_normalizes_hash_and_rejects_tampering(self):
        validator = FundamentalResearchResultValidator(
            self.tools, self.bundle, self.metrics
        )
        draft = result_draft(self.bundle, self.metrics)
        self.assertEqual(validator.validate(draft), [])
        normalized = validator.normalized(draft)
        self.assertIn("content_sha256", normalized)
        self.assertEqual(validator.validate(normalized), [])

        tampered = copy.deepcopy(normalized)
        tampered["items"][0]["observations"][0]["text"] = "未經重新驗證的修改。"
        errors = validator.validate(tampered)
        self.assertTrue(any("content_sha256" in error for error in errors))

        invalid_metric = copy.deepcopy(normalized)
        invalid_metric["items"][0]["metric_ids"] = ["unknown-metric"]
        errors = validator.validate(invalid_metric)
        self.assertTrue(any("metric_ids" in error for error in errors))

    def test_rejects_metric_tampering_and_transaction_fields(self):
        altered_metrics = copy.deepcopy(self.metrics)
        altered_metrics["items"][0]["metrics"][0]["value"] = "999"
        errors = self.calculator.validate(altered_metrics)
        self.assertTrue(any("確定性重算" in error for error in errors))

        validator = FundamentalResearchResultValidator(
            self.tools, self.bundle, self.metrics
        )
        draft = result_draft(self.bundle, self.metrics)
        draft["items"][0]["orders"] = []
        errors = validator.validate(draft)
        self.assertTrue(any("交易欄位" in error for error in errors))

    def test_missing_comparison_degrades_and_nonpositive_base_is_unavailable(self):
        snapshot = snapshot_fixture()
        snapshot["documents"] = snapshot["documents"][:2]
        tools = FundamentalSnapshotTools(snapshot)
        bundle = tools.build_bundle(request(), bundle_id="bundle-no-prior", generated_at=GENERATED_AT)
        metrics = FundamentalMetricsCalculator(tools, bundle).compute(
            metrics_id="metrics-no-prior", computed_at=GENERATED_AT
        )
        self.assertEqual(bundle["status"], "degraded")
        self.assertEqual(metrics["status"], "degraded")
        unavailable = {
            metric["metric_key"]: metric["reason_code"]
            for metric in metrics["items"][0]["metrics"]
            if metric["status"] == "unavailable"
        }
        self.assertEqual(unavailable["revenue_yoy_pct"], "COMPARABLE_FACT_NOT_REPORTED")

        snapshot = snapshot_fixture()
        snapshot["documents"][2]["financial_statement"]["facts"]["revenue"]["value"] = "0"
        tools = FundamentalSnapshotTools(snapshot)
        bundle = tools.build_bundle(request(), bundle_id="bundle-zero-base", generated_at=GENERATED_AT)
        metrics = FundamentalMetricsCalculator(tools, bundle).compute(
            metrics_id="metrics-zero-base", computed_at=GENERATED_AT
        )
        revenue = next(
            item for item in metrics["items"][0]["metrics"] if item["metric_key"] == "revenue_yoy_pct"
        )
        self.assertEqual(revenue["status"], "unavailable")
        self.assertEqual(revenue["reason_code"], "NONPOSITIVE_COMPARISON_BASE")

    def test_rejects_cutoff_after_financial_document_and_unsupported_industry(self):
        snapshot = snapshot_fixture()
        snapshot["documents"][0]["available_at"] = "2026-09-21T01:00:01+00:00"
        with self.assertRaisesRegex(FundamentalToolError, "cutoff 後"):
            FundamentalSnapshotTools(snapshot)

        snapshot = snapshot_fixture()
        snapshot["documents"][0]["financial_statement"]["industry"] = "basi"
        tools = FundamentalSnapshotTools(snapshot)
        bundle = tools.build_bundle(request(), bundle_id="bundle-bank", generated_at=GENERATED_AT)
        self.assertEqual(bundle["companies"][0]["status"], "unavailable")
        self.assertIn("UNSUPPORTED_INDUSTRY_basi", str(bundle["companies"][0]["missing_data"]))

    def test_cli_builds_validates_and_archives_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            bundle_path = root / "bundle.json"
            metrics_path = root / "metrics.json"
            draft_path = root / "draft.json"
            result_path = root / "result.json"
            archive_path = root / "archive.json"
            snapshot_path.write_text(json.dumps(snapshot_fixture()), encoding="utf-8")
            app = FundamentalResearchApplication()
            base = ["--snapshot", str(snapshot_path)]
            self.assertEqual(
                app.run(
                    base
                    + [
                        "build-bundle",
                        "--symbols",
                        "2330.TW",
                        "--fiscal-year",
                        "2026",
                        "--fiscal-quarter",
                        "2",
                        "--request-id",
                        "cli-request",
                        "--bundle-id",
                        "cli-bundle",
                        "--generated-at",
                        GENERATED_AT,
                        "--output",
                        str(bundle_path),
                    ]
                ),
                0,
            )
            self.assertEqual(
                app.run(
                    base
                    + [
                        "compute-metrics",
                        "--bundle",
                        str(bundle_path),
                        "--metrics-id",
                        "cli-metrics",
                        "--computed-at",
                        GENERATED_AT,
                        "--output",
                        str(metrics_path),
                    ]
                ),
                0,
            )
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            draft_path.write_text(json.dumps(result_draft(bundle, metrics)), encoding="utf-8")
            self.assertEqual(
                app.run(
                    base
                    + [
                        "validate-result",
                        "--bundle",
                        str(bundle_path),
                        "--metrics",
                        str(metrics_path),
                        "--input",
                        str(draft_path),
                        "--output",
                        str(result_path),
                    ]
                ),
                0,
            )
            self.assertEqual(
                app.run(
                    base
                    + [
                        "archive",
                        "--bundle",
                        str(bundle_path),
                        "--metrics",
                        str(metrics_path),
                        "--input",
                        str(result_path),
                        "--output",
                        str(archive_path),
                    ]
                ),
                0,
            )
            self.assertTrue(archive_path.exists())
            self.assertEqual(
                app.run(
                    base
                    + [
                        "archive",
                        "--bundle",
                        str(bundle_path),
                        "--metrics",
                        str(metrics_path),
                        "--input",
                        str(result_path),
                        "--output",
                        str(archive_path),
                    ]
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
