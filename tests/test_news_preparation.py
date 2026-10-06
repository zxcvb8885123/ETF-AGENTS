import copy
import unittest
from etf_agent.core import canonical_sha256
from etf_agent.perception.contracts import PerceptionToolError
from etf_agent.perception.news_preparation import extract_news_metadata, prepare_news_candidates
from etf_agent.perception.news_diagnostic import build_news_diagnostic, render_news_diagnostic


class NewsPreparationTests(unittest.TestCase):
    def inputs(self):
        row = {"id": 1, "stock_id": "2881", "title": "公司業務擴展新聞標題完整文字測試原始資料", "link": "https://example.org/1", "publisher": "測試來源", "source_time_raw": "2026-10-02 08:00:00", "available_at": "2026-10-02T09:00:00+00:00", "content_sha256": "candidate"}
        html = '<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-10-02T08:00:00+00:00","articleBody":"測試原文"}</script>'
        capture = {"url": row["link"], "status": "fetched", "observed_at": "2026-10-03T00:00:00+00:00", "html": html, "html_sha256": canonical_sha256(html)}
        return row, capture

    def prepared(self):
        row, capture = self.inputs()
        return prepare_news_candidates([row], [capture], "2026-10-03T01:00:00+00:00")

    def labels(self):
        return [{"item_id": "news-candidate-1", "symbol": "2881.TW", "stance": "positive", "relevance": "relevant", "channel": "news", "rationale": "測試標題方向", "model_version": "fixture"}]

    def test_metadata_keeps_explicit_timezone_and_does_not_guess_naive_time(self):
        row, capture = self.inputs()
        self.assertEqual(extract_news_metadata(capture["html"])["published_at"], "2026-10-02T08:00:00+00:00")
        naive = capture["html"].replace("+00:00", "")
        self.assertIsNone(extract_news_metadata(naive)["published_at"])

    def test_repeat_url_versions_preserved_and_not_approved(self):
        row, capture = self.inputs()
        other = dict(row, id=2, content_sha256="second")
        prepared = prepare_news_candidates([row, other], [capture], "2026-10-03T01:00:00+00:00")
        self.assertEqual(len(prepared["items"]), 1)
        self.assertEqual(prepared["items"][0]["candidate_ids"], [1, 2])
        self.assertEqual(prepared["items"][0]["license_status"], "unverified")
        self.assertIs(prepared["formal_sentiment_available"], False)

    def test_future_candidate_capture_and_changed_html_rejected(self):
        row, capture = self.inputs()
        for mode in ("row", "page", "hash"):
            r, c = copy.deepcopy(row), copy.deepcopy(capture)
            if mode == "row": r["available_at"] = "2026-10-04T00:00:00+00:00"
            elif mode == "page": c["observed_at"] = "2026-10-04T00:00:00+00:00"
            else: c["html"] += "changed"
            with self.assertRaises(PerceptionToolError): prepare_news_candidates([r], [c], "2026-10-03T01:00:00+00:00")

    def test_missing_duplicate_foreign_stock_and_unsupported_labels_rejected(self):
        labels = self.labels()
        cases = [[], labels + labels, [dict(labels[0], symbol="2308.TW")], [dict(labels[0], stance="buy")]]
        for case in cases:
            with self.assertRaises(PerceptionToolError): build_news_diagnostic(self.prepared(), case)

    def test_headline_counts_rebuilt_and_formal_unavailable(self):
        result = build_news_diagnostic(self.prepared(), self.labels())
        self.assertEqual(result["items"][0]["headline_stance_counts"], {"positive": 1})
        self.assertEqual(result["label_basis"], "candidate_title_only")
        self.assertIs(result["formal_sentiment_available"], False)

    def test_report_rejects_modified_counts(self):
        prepared = self.prepared()
        result = build_news_diagnostic(prepared, self.labels())
        self.assertIn("2881", render_news_diagnostic(prepared, result))
        result["items"][0]["headline_stance_counts"]["positive"] = 99
        with self.assertRaises(PerceptionToolError):
            render_news_diagnostic(prepared, result)

    def test_shared_content_across_urls_counted_once(self):
        row, capture = self.inputs()
        other = dict(row, id=2, link="https://example.org/2", publisher="轉載站")
        prepared = prepare_news_candidates([row, other], [capture], "2026-10-03T01:00:00+00:00")
        labels = self.labels() + [dict(self.labels()[0], item_id="news-candidate-2")]
        result = build_news_diagnostic(prepared, labels)
        self.assertEqual(result["items"][0]["unique_items"], 1)
        self.assertEqual(result["items"][0]["headline_stance_counts"], {"positive": 1})
        labels[1]["stance"] = "negative"
        with self.assertRaises(PerceptionToolError): build_news_diagnostic(prepared, labels)
