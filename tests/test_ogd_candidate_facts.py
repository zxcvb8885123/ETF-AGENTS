"""候選 CSV 只產生有來源的事實，不產生正式交易許可。"""

import hashlib
import tempfile
import unittest
from pathlib import Path

from etf_agent.data.ogd_candidate_facts import (
    build_candidate_fact_report,
    extract_ogd_candidate_facts,
)


FETCHED = "2026-09-25T05:27:14+00:00"


class OgdCandidateFactTests(unittest.TestCase):
    def test_fixed_universe_report_verifies_each_raw_payload(self):
        samples = {
            "TPEX_SPECIAL_OGD": "資料日期,證券代號,變更交易,分盤交易,管理股票,停止交易\n1150924,3374,Ｙ,,,\n",
            "TPEX_ATTENTION_OGD": "公告日期,證券代號,注意交易資訊\n1150924,3374,注意\n",
            "TPEX_DISPOSAL_OGD": "公布日期,證券代號,處置起訖時間,處置內容\n1150924,3374,1150929~1151006,順延\n",
            "TPEX_HALTS_HISTORY_OGD": "資料日期,證券代號,暫停交易日期,暫停交易時間,恢復交易日期,恢復交易時間\n115,3374,1150924,080000,,\n",
            "TWSE_SPECIAL_OGD": "證券代號,分盤集合競價(以**表示)\n3443,**\n",
            "TWSE_DISPOSAL_OGD": "公布日期,證券代號,處置起迄時間,處置內容\n1150924,3443,115/09/29～115/10/06,順延\n",
            "TWSE_HALTS_OGD": "證券代號,暫停交易日期,暫停交易時間,恢復交易日期,恢復交易時間\n3443,1150924,080000,1150929,080000\n",
            "TWSE_CALENDAR_OGD": "日期,名稱,說明\n1150925,中秋節,休市\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = []
            for source_id, value in samples.items():
                raw = value.encode("utf-8")
                (root / (source_id + ".csv")).write_bytes(raw)
                entries.append({
                    "source_id": source_id,
                    "status": "captured_candidate_only",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "row_count": 1,
                    "fetch_finished_at": FETCHED,
                })
            universe = {"3374.TWO", "3443.TW"} | {"%04d.TW" % code for code in range(100, 248)}
            manifest = {"run_id": "test-run", "sources": entries}
            report = build_candidate_fact_report(manifest, root, universe)
            self.assertEqual(report["candidate_hit_symbols"], ["3374.TWO", "3443.TW"])
            self.assertEqual(report["approved_sources"], 0)
            self.assertEqual(report["formal_tradability_assessments"], 0)
            (root / "TPEX_SPECIAL_OGD.csv").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "雜湊"):
                build_candidate_fact_report(manifest, root, universe)

    def test_disposition_keeps_announcement_and_tentative_dates_separate(self):
        raw = (
            "公布日期,證券代號,處置起訖時間,處置內容\n"
            "1150924,3374,1150929~1151006,遇休市則順延\n"
            "1150924,33741,1150929~1151006,遇休市則順延\n"
        ).encode("utf-8")
        facts, summary = extract_ogd_candidate_facts(
            "TPEX_DISPOSAL_OGD", raw, fetched_at=FETCHED
        )
        self.assertEqual(summary["excluded_nonstock_rows"], 1)
        self.assertFalse(summary["approved"])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["symbol"], "3374.TWO")
        self.assertEqual(facts[0]["announced_on"], "2026-09-24")
        self.assertEqual(facts[0]["tentative_interval"]["start_date"], "2026-09-29")
        self.assertTrue(facts[0]["extension_mentioned"])
        self.assertNotIn("state", facts[0])

    def test_halt_event_requires_real_date_time_and_timezone(self):
        raw = (
            "資料日期,證券代號,暫停交易日期,暫停交易時間,恢復交易日期,恢復交易時間\n"
            "115,3491,1150924,080000,,\n"
        ).encode()
        facts, _ = extract_ogd_candidate_facts(
            "TPEX_HALTS_HISTORY_OGD", raw, fetched_at=FETCHED
        )
        self.assertEqual(facts[0]["event_date"], "2026-09-24")
        self.assertEqual(facts[0]["event_time"], "08:00:00")
        for altered, fetch_time in (
            (raw.replace(b"080000", b"250000"), FETCHED),
            (raw, "2026-09-25T13:27:14"),
            (raw.replace(b"115,3491", b"114,3491"), FETCHED),
        ):
            with self.subTest(altered=altered, fetch_time=fetch_time), self.assertRaises(ValueError):
                extract_ogd_candidate_facts(
                    "TPEX_HALTS_HISTORY_OGD", altered, fetched_at=fetch_time
                )

    def test_special_status_date_is_not_invented_for_twse(self):
        raw = "證券代號,分盤集合競價(以**表示)\n3443,**\n".encode()
        facts, _ = extract_ogd_candidate_facts(
            "TWSE_SPECIAL_OGD", raw, fetched_at=FETCHED
        )
        self.assertEqual(facts[0]["symbol"], "3443.TW")
        self.assertTrue(facts[0]["split_trading"])
        self.assertIsNone(facts[0]["content_date"])
        self.assertTrue(facts[0]["candidate_only"])


if __name__ == "__main__":
    unittest.main()
