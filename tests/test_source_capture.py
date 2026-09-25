"""交易日候選來源封存須保留失敗證據，但不能核准來源。"""

import io
import tempfile
import unittest
from pathlib import Path

from etf_agent.data.source_capture import OGD_SOURCE_IDS, capture_ogd_candidates


class _Response:
    status = 200
    headers = {"Date": "Tue, 29 Sep 2026 01:30:00 GMT", "Content-Type": "text/csv"}

    def __init__(self, raw):
        self.stream = io.BytesIO(raw)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()

    def read(self):
        return self.stream.read()


class CandidateCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.report = {"sources": [
            {
                "source_id": source_id,
                "url": "https://www.tpex.org.tw/example/%s" % source_id,
                "dataset_url": "https://data.gov.tw/dataset/11736",
            }
            for source_id in sorted(OGD_SOURCE_IDS)
        ]}

    def test_captures_all_eight_without_approval(self):
        manifest = capture_ogd_candidates(
            self.report,
            self.root,
            fetch=lambda request, timeout: _Response(b"Date,Code\n1150929,2330\n"),
            now=lambda: "2026-09-29T01:30:00+00:00",
        )
        self.assertEqual(manifest["status"], "captured_candidate_only")
        self.assertEqual(manifest["approved_sources"], 0)
        self.assertEqual(len(manifest["sources"]), 8)
        for item in manifest["sources"]:
            self.assertEqual(item["row_count"], 1)
            self.assertFalse(item["available_at_verified"])
            self.assertTrue(Path(item["raw_path"]).is_file())
        self.assertTrue((self.root / manifest["run_id"] / "manifest.json").is_file())

    def test_invalid_csv_is_archived_and_run_fails_closed(self):
        manifest = capture_ogd_candidates(
            self.report,
            self.root,
            fetch=lambda request, timeout: _Response(b"Date,Code\n1150929\n"),
        )
        self.assertEqual(manifest["status"], "partial_failure")
        self.assertEqual(manifest["approved_sources"], 0)
        for item in manifest["sources"]:
            self.assertEqual(item["status"], "failed")
            self.assertTrue(Path(item["raw_path"]).is_file())
            self.assertIn("CSV", item["reason"])

    def test_rejects_unlisted_host_before_fetch(self):
        self.report["sources"][0]["url"] = "https://example.com/csv"
        with self.assertRaisesRegex(ValueError, "官方 HTTPS"):
            capture_ogd_candidates(self.report, self.root, fetch=lambda *_args, **_kwargs: None)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
