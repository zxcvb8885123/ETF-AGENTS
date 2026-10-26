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
        ],
        "source_evidence": evidence,
        "sentiment_items": sentiment_items,
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


def valid_result(tools):
    labels = sentiment_labels()
    sentiment = tools.aggregate_sentiment("2330.TW", labels)
    evidence_ids = sorted(sentiment["evidence_ids"])
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
                "priced_in_assessment": "aligned",
                "rationale": "情緒偏正向，但只作次級證據。",
                "evidence_ids": evidence_ids,
                "risk_flags": sentiment["manipulation_flags"],
                "research_status": "usable_secondary",
                "status_reason": "情緒通道達到最低覆蓋。",
            }
        ],
        "errors": [],
    }


class SentimentAnalystAgentTests(unittest.TestCase):
    def test_status_and_covered_symbols(self):
        tools = PerceptionDataTools(perception_bundle())
        status = tools.status()
        self.assertEqual(status["sentiment_item_count"], 7)
        self.assertNotIn("analyst_estimate_count", status)
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

    def test_validator_accepts_recomputed_result_and_rejects_tampering(self):
        tools = PerceptionDataTools(perception_bundle())
        payload = valid_result(tools)
        validator = MarketPerceptionResultValidator(tools)
        self.assertEqual(validator.validate(payload), [])

        tampered = copy.deepcopy(payload)
        tampered["items"][0]["sentiment"]["score"] = 0.99
        errors = validator.validate(tampered)
        self.assertTrue(any("確定性重算" in error for error in errors))

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
        payload["items"][0]["evidence_ids"] = []
        errors = MarketPerceptionResultValidator(tools).validate(payload)
        self.assertTrue(any("usable_secondary" in error for error in errors))

    def test_bundle_rejects_future_available_data(self):
        bundle = perception_bundle()
        bundle["sentiment_items"][0]["available_at"] = "2026-09-21T00:00:00+00:00"
        with self.assertRaises(PerceptionToolError):
            PerceptionDataTools(bundle)

    def test_bundle_rejects_removed_analyst_consensus_channel(self):
        bundle = perception_bundle()
        bundle["source_coverage"].append(
            {
                "channel": "analyst_consensus",
                "provider": "licensed-fixture",
                "status": "available",
                "license_status": "approved",
            }
        )
        with self.assertRaises(PerceptionToolError):
            PerceptionDataTools(bundle)

    def test_legacy_empty_consensus_channel_still_validates(self):
        """舊封存列出沒有資料的共識通道與空的共識欄位時照舊通過；帶預估、證據或共識數字一律拒絕。"""
        bundle = perception_bundle()
        bundle["source_coverage"].append(
            {
                "channel": "analyst_consensus",
                "provider": "UNAVAILABLE",
                "status": "unavailable",
                "license_status": "unknown",
            }
        )
        bundle["analyst_estimates"] = []
        tools = PerceptionDataTools(bundle)
        payload = valid_result(tools)
        payload["items"][0]["consensus_metrics"] = []
        payload["items"][0]["expectation_gaps"] = []
        validator = MarketPerceptionResultValidator(tools)
        self.assertEqual(validator.validate(payload), [])

        for field in ("consensus_metrics", "expectation_gaps"):
            stale = copy.deepcopy(payload)
            stale["items"][0][field] = [{"metric": "eps", "status": "available"}]
            errors = validator.validate(stale)
            self.assertTrue(any("%s 已停用" % field in error for error in errors), errors)

        with_estimates = copy.deepcopy(bundle)
        with_estimates["analyst_estimates"] = [{"estimate_id": "e1"}]
        with self.assertRaises(PerceptionToolError):
            PerceptionDataTools(with_estimates)

        with_evidence = copy.deepcopy(bundle)
        with_evidence["source_evidence"].append(
            dict(bundle["source_evidence"][0], evidence_id="e1-evidence", channel="analyst_consensus")
        )
        with self.assertRaises(PerceptionToolError):
            PerceptionDataTools(with_evidence)

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
            output_path = root / "validated.json"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            result_path.write_text(json.dumps(payload), encoding="utf-8")
            service = MarketPerceptionApplicationService.from_path(bundle_path)
            response = service.validate_file(result_path, output_path)
            self.assertTrue(response["valid"])
            self.assertTrue(output_path.exists())


if __name__ == "__main__":
    unittest.main()
