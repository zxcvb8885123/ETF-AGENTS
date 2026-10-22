import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.perception import (
    MarketPerceptionApplicationService,
    MarketPerceptionResultValidator,
    PerceptionDataTools,
    PerceptionToolError,
)


CUTOFF = "2026-09-20T00:55:00+00:00"


def perception_bundle():
    evidence = []
    sentiment_items = []
    sentiment_rows = [
        ("s1", "forum-a", "author-1", "c1", "2026-09-19T00:00:00+00:00"),
        ("s2", "forum-a", "author-2", "c2", "2026-09-18T00:00:00+00:00"),
        ("s3", "forum-b", "author-3", "c3", "2026-09-17T00:00:00+00:00"),
        ("s4", "forum-b", "author-4", "c4", "2026-09-16T00:00:00+00:00"),
        ("s5", "forum-a", "author-5", "c5", "2026-09-05T00:00:00+00:00"),
        ("s6", "forum-b", "author-6", "c6", "2026-09-03T00:00:00+00:00"),
        ("s7", "forum-b", "author-7", "c1", "2026-09-19T01:00:00+00:00"),
    ]
    for item_id, source_name, author_id, canonical_id, available_at in sentiment_rows:
        evidence_id = "%s-evidence" % item_id
        evidence.append(
            {
                "evidence_id": evidence_id,
                "symbol": "2330.TW",
                "channel": "sentiment",
                "source_name": source_name,
                "source_url": "fixture://%s" % item_id,
                "published_at": available_at,
                "available_at": available_at,
                "content_sha256": "%s-sha" % item_id,
                "license_status": "approved",
            }
        )
        sentiment_items.append(
            {
                "item_id": item_id,
                "symbol": "2330.TW",
                "source_type": "forum_post",
                "source_name": source_name,
                "author_id": author_id,
                "canonical_content_id": canonical_id,
                "text": "台積電測試情緒內容 %s" % item_id,
                "published_at": available_at,
                "available_at": available_at,
                "evidence_id": evidence_id,
            }
        )

    estimate_rows = [
        ("e1", "broker-a", "9", "2026-08-01T00:00:00+00:00"),
        ("e2", "broker-b", "10", "2026-08-02T00:00:00+00:00"),
        ("e3", "broker-a", "12", "2026-09-10T00:00:00+00:00"),
        ("e4", "broker-b", "11", "2026-09-11T00:00:00+00:00"),
        ("e5", "broker-c", "13", "2026-09-12T00:00:00+00:00"),
    ]
    estimates = []
    for estimate_id, contributor, value, available_at in estimate_rows:
        evidence_id = "%s-evidence" % estimate_id
        evidence.append(
            {
                "evidence_id": evidence_id,
                "symbol": "2330.TW",
                "channel": "analyst_consensus",
                "source_name": "licensed-fixture",
                "source_url": "fixture://%s" % estimate_id,
                "published_at": available_at,
                "available_at": available_at,
                "content_sha256": "%s-sha" % estimate_id,
                "license_status": "approved",
            }
        )
        estimates.append(
            {
                "estimate_id": estimate_id,
                "symbol": "2330.TW",
                "metric": "eps",
                "forecast_period": "2026FY",
                "value": value,
                "unit": "TWD/share",
                "currency": "TWD",
                "contributor_id": contributor,
                "provider": "licensed-fixture",
                "published_at": available_at,
                "available_at": available_at,
                "evidence_id": evidence_id,
            }
        )
    return {
        "schema_version": "1.0",
        "bundle_id": "perception-bundle-1",
        "snapshot_id": "snapshot-1",
        "decision_cutoff": CUTOFF,
        "generated_at": "2026-09-20T01:00:00+00:00",
        "source_coverage": [
            {
                "channel": "sentiment",
                "provider": "fixture-forums",
                "status": "available",
                "license_status": "approved",
                "history_start": "2026-08-01",
                "notes": "測試資料",
            },
            {
                "channel": "analyst_consensus",
                "provider": "licensed-fixture",
                "status": "available",
                "license_status": "approved",
                "history_start": "2026-08-01",
                "notes": "測試資料",
            },
        ],
        "source_evidence": evidence,
        "sentiment_items": sentiment_items,
        "analyst_estimates": estimates,
    }


def sentiment_labels():
    stances = {
        "s1": "positive",
        "s2": "positive",
        "s3": "positive",
        "s4": "positive",
        "s5": "negative",
        "s6": "neutral",
        "s7": "positive",
    }
    return [
        {
            "item_id": item_id,
            "symbol": "2330.TW",
            "evidence_id": "%s-evidence" % item_id,
            "relevance": "relevant",
            "stance": stance,
            "rationale": "與公司直接相關的測試標籤。",
            "model_version": "fixture-model-1",
        }
        for item_id, stance in stances.items()
    ]


def event_result():
    return {
        "schema_version": "2.1",
        "run_id": "research-run-1",
        "snapshot_id": "snapshot-1",
        "decision_cutoff": CUTOFF,
        "status": "completed",
        "items": [
            {
                "event_id": "earnings:2330:2026fy",
                "symbol": "2330.TW",
                "fact_values": [
                    {
                        "name": "diluted_eps_2026fy",
                        "value": "12.6",
                        "unit": "TWD/share",
                        "period": "2026FY",
                        "evidence_id": "event-evidence-1",
                    }
                ],
            }
        ],
    }


def valid_result(tools):
    labels = sentiment_labels()
    sentiment = tools.aggregate_sentiment("2330.TW", labels)
    consensus = tools.compute_consensus_revision("2330.TW", "eps", "2026FY")
    evidence_ids = sorted(set(sentiment["evidence_ids"] + consensus["evidence_ids"]))
    return {
        "schema_version": "1.0",
        "run_id": "perception-run-1",
        "snapshot_id": "snapshot-1",
        "decision_cutoff": CUTOFF,
        "skill_version": "1.0.0",
        "status": "completed",
        "items": [
            {
                "symbol": "2330.TW",
                "event_result_ids": ["research-run-1"],
                "sentiment_labels": labels,
                "sentiment": sentiment,
                "consensus_metrics": [consensus],
                "expectation_gaps": [],
                "priced_in_assessment": "aligned",
                "rationale": "情緒偏正向且共識上修，但只作次級證據。",
                "evidence_ids": evidence_ids,
                "risk_flags": sentiment["manipulation_flags"],
                "research_status": "usable_secondary",
                "status_reason": "情緒與分析師通道均達到最低覆蓋。",
            }
        ],
        "errors": [],
    }


class SentimentAnalystAgentTests(unittest.TestCase):
    def test_status_and_covered_symbols(self):
        tools = PerceptionDataTools(perception_bundle())
        status = tools.status()
        self.assertEqual(status["sentiment_item_count"], 7)
        self.assertEqual(status["analyst_estimate_count"], 5)
        covered = tools.list_covered_symbols()["symbols"]
        self.assertEqual(covered[0]["symbol"], "2330.TW")
        self.assertEqual(covered[0]["sentiment_items"], 7)

    def test_aggregate_sentiment_deduplicates_and_requires_diversity(self):
        tools = PerceptionDataTools(perception_bundle())
        result = tools.aggregate_sentiment("2330.TW", sentiment_labels())
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["direction"], "positive")
        self.assertEqual(result["item_count"], 6)
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["score"], 0.5)
        self.assertEqual(len(result["evidence_ids"]), 6)

    def test_invalid_sentiment_label_is_rejected(self):
        tools = PerceptionDataTools(perception_bundle())
        labels = sentiment_labels()
        labels[0]["evidence_id"] = "wrong"
        with self.assertRaises(PerceptionToolError):
            tools.aggregate_sentiment("2330.TW", labels)

    def test_sentiment_labels_must_cover_full_window(self):
        tools = PerceptionDataTools(perception_bundle())
        with self.assertRaises(PerceptionToolError):
            tools.aggregate_sentiment("2330.TW", sentiment_labels()[:-1])

    def test_consensus_uses_latest_estimate_per_contributor(self):
        tools = PerceptionDataTools(perception_bundle())
        result = tools.compute_consensus_revision("2330.TW", "eps", "2026FY")
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["median"], "12")
        self.assertEqual(result["contributor_count"], 3)
        self.assertEqual(result["prior_median"], "9.5")
        self.assertAlmostEqual(result["revision_pct"], 26.31578947)

    def test_compare_event_expectations_requires_matching_unit(self):
        tools = PerceptionDataTools(perception_bundle())
        result = tools.compare_event_expectations(
            event_result(),
            "earnings:2330:2026fy",
            "diluted_eps_2026fy",
            "eps",
            "2026FY",
        )
        self.assertEqual(result["classification"], "above")
        self.assertEqual(result["surprise_pct"], 5.0)

        mismatch = event_result()
        mismatch["items"][0]["fact_values"][0]["unit"] = "TWD x 1000"
        result = tools.compare_event_expectations(
            mismatch,
            "earnings:2330:2026fy",
            "diluted_eps_2026fy",
            "eps",
            "2026FY",
        )
        self.assertEqual(result["classification"], "unavailable")

    def test_validator_accepts_recomputed_result_and_rejects_tampering(self):
        tools = PerceptionDataTools(perception_bundle())
        payload = valid_result(tools)
        validator = MarketPerceptionResultValidator(tools, event_result())
        self.assertEqual(validator.validate(payload), [])

        tampered = copy.deepcopy(payload)
        tampered["items"][0]["sentiment"]["score"] = 0.99
        errors = validator.validate(tampered)
        self.assertTrue(any("確定性重算" in error for error in errors))

    def test_validator_recomputes_expectation_gap(self):
        tools = PerceptionDataTools(perception_bundle())
        event = event_result()
        payload = valid_result(tools)
        payload["items"][0]["expectation_gaps"] = [
            tools.compare_event_expectations(
                event,
                "earnings:2330:2026fy",
                "diluted_eps_2026fy",
                "eps",
                "2026FY",
            )
        ]
        validator = MarketPerceptionResultValidator(tools, event)
        self.assertEqual(validator.validate(payload), [])

        payload["items"][0]["expectation_gaps"][0]["surprise_pct"] = 99
        errors = validator.validate(payload)
        self.assertTrue(any("expectation_gaps" in error for error in errors))

    def test_usable_secondary_requires_available_channel(self):
        tools = PerceptionDataTools(perception_bundle())
        payload = valid_result(tools)
        irrelevant = sentiment_labels()
        for label in irrelevant:
            label["relevance"] = "irrelevant"
        payload["items"][0]["sentiment_labels"] = irrelevant
        payload["items"][0]["sentiment"] = tools.aggregate_sentiment(
            "2330.TW", irrelevant
        )
        payload["items"][0]["consensus_metrics"] = []
        payload["items"][0]["evidence_ids"] = []
        errors = MarketPerceptionResultValidator(tools).validate(payload)
        self.assertTrue(any("usable_secondary" in error for error in errors))

    def test_bundle_rejects_future_available_data(self):
        bundle = perception_bundle()
        bundle["sentiment_items"][0]["available_at"] = "2026-09-21T00:00:00+00:00"
        with self.assertRaises(PerceptionToolError):
            PerceptionDataTools(bundle)

    def test_bundle_rejects_unapproved_source_usage(self):
        bundle = perception_bundle()
        bundle["source_evidence"][0]["license_status"] = "unknown"
        with self.assertRaises(PerceptionToolError):
            PerceptionDataTools(bundle)

    def test_application_service_validates_and_writes_result(self):
        bundle = perception_bundle()
        tools = PerceptionDataTools(bundle)
        payload = valid_result(tools)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_path = root / "bundle.json"
            result_path = root / "result.json"
            event_path = root / "event.json"
            output_path = root / "validated.json"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            result_path.write_text(json.dumps(payload), encoding="utf-8")
            event_path.write_text(json.dumps(event_result()), encoding="utf-8")
            service = MarketPerceptionApplicationService.from_path(bundle_path)
            response = service.validate_file(result_path, output_path, event_path)
            self.assertTrue(response["valid"])
            self.assertTrue(output_path.exists())


if __name__ == "__main__":
    unittest.main()
