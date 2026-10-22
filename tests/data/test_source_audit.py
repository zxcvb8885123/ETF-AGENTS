"""政府開放資料候選對照只驗證封存內容，不升級正式來源。"""

import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.data.source_audit import (
    roc_date_to_iso,
    roc_interval_to_iso,
    verify_ogd_crosscheck,
)


class OpenDataCrosscheckTests(unittest.TestCase):
    def test_roc_dates_reject_invalid_or_reversed_intervals(self):
        self.assertEqual(roc_date_to_iso("1150924"), "2026-09-24")
        self.assertEqual(roc_date_to_iso("115/09/24"), "2026-09-24")
        self.assertEqual(
            roc_interval_to_iso("1150929～1151006"),
            {"start_date": "2026-09-29", "end_date": "2026-10-06"},
        )
        for value in ("1150229", "115/9/24", "", "1150924 09:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                roc_date_to_iso(value)
        with self.assertRaises(ValueError):
            roc_interval_to_iso("1151006～1150929")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["日期", "代號", "狀態"])
        writer.writeheader()
        writer.writerow({"日期": "1150924", "代號": "3374", "狀態": "處置"})
        raw = output.getvalue().encode("utf-8")
        (self.root / "TPEX_TEST_OGD.csv").write_bytes(raw)
        (self.root / "TPEX_TEST.json").write_text(
            json.dumps([
                {"Date": "1150924", "Code": "3374", "Status": "處置"},
                {"Date": "1150923", "Code": "4979", "Status": "注意"},
            ], ensure_ascii=False),
            encoding="utf-8",
        )
        self.report = {"sources": [{
            "source_id": "TPEX_TEST_OGD",
            "json_source_id": "TPEX_TEST",
            "mapped_fields": {"日期": "Date", "代號": "Code", "狀態": "Status"},
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "row_count": 1,
            "json_row_count": 2,
            "csv_date_counts": {"1150924": 1},
            "json_date_counts": {"1150924": 1, "1150923": 1},
            "json_rows_after_date_filter": 1,
        }]}

    def test_date_filtered_candidate_matches_without_approving_source(self):
        result = verify_ogd_crosscheck(self.report, self.root)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["sources"][0]["status"], "matched_candidate_only")

    def test_changed_raw_payload_or_row_fails(self):
        path = self.root / "TPEX_TEST_OGD.csv"
        path.write_bytes(path.read_bytes().replace(b"3374", b"4979"))
        result = verify_ogd_crosscheck(self.report, self.root)
        self.assertFalse(result["valid"])
        self.assertIn("雜湊", result["errors"][0])

    def test_date_distribution_mismatch_fails(self):
        self.report["sources"][0]["json_date_counts"] = {"1150924": 2}
        result = verify_ogd_crosscheck(self.report, self.root)
        self.assertFalse(result["valid"])
        self.assertIn("公告日期", result["errors"][0])

    def test_unknown_field_or_unsafe_source_id_fails(self):
        source = self.report["sources"][0]
        source["mapped_fields"]["不存在"] = "Absent"
        result = verify_ogd_crosscheck(self.report, self.root)
        self.assertFalse(result["valid"])
        source["source_id"] = "../TPEX_TEST_OGD"
        result = verify_ogd_crosscheck(self.report, self.root)
        self.assertFalse(result["valid"])
        self.assertIn("來源 ID", result["errors"][0])

    def test_halt_and_resume_event_rows_are_not_current_status(self):
        raw = "資料日期,證券代號,暫停交易日期,恢復交易日期\n115,1788,1150618,\n115,1788,,1150622\n".encode()
        source = self.report["sources"][0]
        source.update({
            "source_id": "TPEX_HALTS_HISTORY_OGD",
            "json_source_id": "TPEX_HALTS_HISTORY",
            "mapped_fields": {"證券代號": "Code", "暫停交易日期": "Halt", "恢復交易日期": "Resume"},
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "row_count": 2,
            "json_row_count": 2,
        })
        source.pop("csv_date_counts")
        source.pop("json_date_counts")
        source.pop("json_rows_after_date_filter")
        (self.root / "TPEX_HALTS_HISTORY_OGD.csv").write_bytes(raw)
        json_path = self.root / "TPEX_HALTS_HISTORY.json"
        json_path.write_text(json.dumps([
            {"Code": "1788", "Halt": "1150618", "Resume": ""},
            {"Code": "1788", "Halt": "", "Resume": "1150622"},
        ]), encoding="utf-8")
        result = verify_ogd_crosscheck(self.report, self.root)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["sources"][0]["halt_events"], 1)
        self.assertEqual(result["sources"][0]["resume_events"], 1)
        self.assertFalse(result["sources"][0]["current_status_inferred"])
        invalid_raw = raw.replace(b"115,1788,1150618,", b"115,1788,1150618,1150622")
        (self.root / "TPEX_HALTS_HISTORY_OGD.csv").write_bytes(invalid_raw)
        source["sha256"] = hashlib.sha256(invalid_raw).hexdigest()
        source["size"] = len(invalid_raw)
        json_path.write_text(json.dumps([
            {"Code": "1788", "Halt": "1150618", "Resume": "1150622"},
            {"Code": "1788", "Halt": "", "Resume": "1150622"},
        ]), encoding="utf-8")
        result = verify_ogd_crosscheck(self.report, self.root)
        self.assertFalse(result["valid"])
        self.assertIn("只有一種事件日期", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
