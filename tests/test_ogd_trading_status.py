import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.data import (
    OgdTradingStatusMapper,
    build_trading_status_from_capture,
    closed_dates_from_calendar,
    latest_capture_before,
    load_approvals,
    load_not_applicable,
    next_trading_session,
)
from etf_agent.data.trading_status import TradingStatusBundleValidator, TradingStatusError


FETCHED = "2026-09-26T08:00:00+00:00"
CSV = {
    "TPEX_SPECIAL_OGD": "資料日期,證券代號,證券名稱,變更交易,分盤交易,管理股票,停止交易\n\"1150926\",\"6488\",\"環球晶\",\"\",\"\",\"Ｙ\",\"\"\n",
    "TPEX_DISPOSAL_OGD": "公布日期,證券代號,證券名稱,處置起訖時間,處置內容\n\"1150926\",\"3374\",\"精材\",\"115/09/29～115/10/05\",\"第一次處置\"\n",
    "TWSE_HALTS_OGD": "證券代號,證券名稱,暫停交易日期,暫停交易時間,恢復交易日期,恢復交易時間\n\"2330\",\"台積電\",\"1150926\",\"090000\",\"\",\"\"\n",
    "TWSE_SPECIAL_OGD": "證券代號,證券名稱,分盤集合競價(以**表示)\n\"1213\",\"大飲\",\"  \"\n",
    "TWSE_DISPOSAL_OGD": "公布日期,證券代號,證券名稱,處置起迄時間,處置內容\n\"1150926\",\"1213\",\"大飲\",\"115/09/29～115/10/05\",\"第一次處置\"\n",
    "TWSE_CALENDAR_OGD": "名稱,日期,星期,說明\n\"教師節\",\"1150928\",\"一\",\"依規定放假1日。\"\n\"國慶日\",\"1151009\",\"五\",\"補假。\"\n",
}
SNAPSHOT = {
    "snapshot_id": "snapshot-ts",
    "universe_version": "universe-ts",
    "decision_cutoff": "2026-09-26T10:00:00+00:00",
    "latest_prices": [{"symbol": symbol} for symbol in ("2317.TW", "2330.TW", "3374.TWO", "6488.TWO")],
}


def write_capture(root: Path, name: str = "capture-1", fetched: str = FETCHED) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    sources = []
    for source_id, text in CSV.items():
        raw = text.encode("utf-8")
        (directory / ("%s.csv" % source_id)).write_bytes(raw)
        sources.append({
            "source_id": source_id, "status": "captured_candidate_only", "url": "https://example.gov.tw/%s" % source_id,
            "sha256": hashlib.sha256(raw).hexdigest(), "row_count": text.count("\n") - 1,
            "fetch_started_at": fetched, "fetch_finished_at": fetched,
        })
    (directory / "manifest.json").write_text(json.dumps({"status": "captured_candidate_only", "sources": sources}), encoding="utf-8")
    return directory


def approvals(max_age=72):
    return {
        source_id: {"source_id": source_id, "approved_by": "tester", "approved_at": "2026-09-26T00:00:00+08:00",
                    "evidence": "docs/source_audit/fixture.md", "max_age_hours": max_age}
        for source_id in ("TPEX_SPECIAL_OGD", "TPEX_DISPOSAL_OGD", "TWSE_HALTS_OGD", "TWSE_SPECIAL_OGD", "TWSE_DISPOSAL_OGD")
    }


class TradingCalendarTests(unittest.TestCase):
    def test_holidays_and_weekends_are_skipped(self):
        closed = closed_dates_from_calendar([
            {"kind": "calendar_event", "event_date": "2026-09-28", "name": "教師節"},
            {"kind": "calendar_event", "event_date": "2026-10-02", "name": "國曆新年開始交易日"},
        ])
        self.assertEqual(closed, {"2026-09-28"})
        # 週五晚上 cutoff：跳過週末與 9/28 教師節。
        session = next_trading_session("2026-09-25T12:00:00+00:00", closed)
        self.assertEqual(session["start"], "2026-09-29T09:00:00+08:00")


class OgdMapperTests(unittest.TestCase):
    def build(self, approved):
        with tempfile.TemporaryDirectory() as directory:
            capture = write_capture(Path(directory))
            bundle, assessment, session = build_trading_status_from_capture(SNAPSHOT, capture, approved)
        return bundle, assessment, session

    def test_without_approval_everything_stays_unknown(self):
        bundle, assessment, session = self.build({})
        self.assertEqual(session["start"], "2026-09-29T09:00:00+08:00")
        self.assertEqual({item["state"] for item in assessment["symbols"]}, {"unknown"})
        self.assertEqual({item["approval_status"] for item in bundle["source_coverage"]}, {"candidate"})
        self.assertEqual(TradingStatusBundleValidator().validate(bundle, assessment), [])

    def test_approved_sources_block_restrictions_and_keep_missing_categories_unknown(self):
        bundle, assessment, _ = self.build(approvals())
        states = {item["symbol"]: item for item in assessment["symbols"]}
        self.assertEqual(states["6488.TWO"]["state"], "blocked")
        self.assertIn("management", states["6488.TWO"]["restriction_categories"])
        self.assertEqual(states["3374.TWO"]["state"], "blocked")
        self.assertIn("disposition", states["3374.TWO"]["restriction_categories"])
        # TWSE 沒有「管理股票」來源：不推論，維持 unknown。
        self.assertEqual(states["2317.TW"]["state"], "unknown")
        self.assertIn("MISSING_COVERAGE:management", states["2317.TW"]["reason_codes"])
        self.assertTrue(any("RESTRICTED:trading_halt" in code for code in states["2330.TW"]["reason_codes"]))
        self.assertEqual(TradingStatusBundleValidator().validate(bundle, assessment), [])

    def test_stale_capture_is_partial_even_when_approved(self):
        bundle, assessment, _ = self.build(approvals(max_age=1))
        self.assertEqual({item["coverage_status"] for item in bundle["source_coverage"]}, {"partial"})
        self.assertEqual({item["reason"] for item in bundle["source_coverage"] if item["approval_status"] == "approved"}, {"STALE_CAPTURE"})

    def test_tampered_capture_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = write_capture(Path(directory))
            (capture / "TWSE_HALTS_OGD.csv").write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(TradingStatusError, "雜湊不一致"):
                OgdTradingStatusMapper(json.loads((capture / "manifest.json").read_text(encoding="utf-8")), capture)

    def test_no_capture_means_no_coverage(self):
        bundle, assessment, session = build_trading_status_from_capture(SNAPSHOT, None, approvals())
        self.assertEqual(bundle["source_coverage"], [])
        self.assertEqual({item["state"] for item in assessment["symbols"]}, {"unknown"})
        self.assertEqual(session["start"], "2026-09-28T09:00:00+08:00")


TWSE_MANAGEMENT = {
    "market": "TWSE", "category": "management",
    "basis": "臺灣證券交易所營業細則第 52 條：管理股票係經證交所終止上市之有價證券",
    "evidence": "docs/source_audit/2026-09-28_twse_management_basis.md",
    "approved_by": "tester", "approved_at": "2026-09-26T00:00:00+08:00",
}


class NotApplicablePolicyTests(unittest.TestCase):
    def test_approved_not_applicable_policy_lets_clean_twse_stock_become_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = write_capture(Path(directory))
            bundle, assessment, _ = build_trading_status_from_capture(SNAPSHOT, capture, approvals(), [TWSE_MANAGEMENT])
        states = {item["symbol"]: item for item in assessment["symbols"]}
        self.assertEqual(states["2317.TW"]["state"], "allowed")
        self.assertEqual(states["2330.TW"]["state"], "blocked")
        self.assertIn("trading_halt", states["2330.TW"]["restriction_categories"])
        policy = [item for item in bundle["source_coverage"] if item["semantics"] == "not_applicable_policy"]
        self.assertEqual([item["source_id"] for item in policy], ["POLICY_NOT_APPLICABLE:TWSE:management"])
        self.assertIn("第 52 條", policy[0]["reason"])
        self.assertEqual(TradingStatusBundleValidator().validate(bundle, assessment), [])

    def test_not_applicable_requires_evidence_and_cannot_override_a_real_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approvals.json"
            path.write_text(json.dumps({"approved_sources": [], "not_applicable": [TWSE_MANAGEMENT]}), encoding="utf-8")
            self.assertEqual(len(load_not_applicable(path)), 1)
            for broken, message in (
                (dict(TWSE_MANAGEMENT, market="TPEX"), "已有對應來源"),
                ({key: value for key, value in TWSE_MANAGEMENT.items() if key != "basis"}, "basis"),
                (dict(TWSE_MANAGEMENT, category="unknown"), "市場或類別無效"),
            ):
                path.write_text(json.dumps({"not_applicable": [broken]}), encoding="utf-8")
                with self.assertRaisesRegex(TradingStatusError, message):
                    load_not_applicable(path)


class ApprovalAndCaptureSelectionTests(unittest.TestCase):
    def test_approvals_require_complete_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approvals.json"
            path.write_text(json.dumps({"approved_sources": list(approvals().values())}), encoding="utf-8")
            self.assertEqual(set(load_approvals(path)), set(approvals()))
            for broken, message in (
                ({"source_id": "TWSE_HALTS_OGD", "approved_by": "tester"}, "approved_at"),
                (dict(approvals()["TWSE_HALTS_OGD"], source_id="UNKNOWN"), "可核准"),
                (dict(approvals()["TWSE_HALTS_OGD"], max_age_hours=0), "max_age_hours"),
            ):
                path.write_text(json.dumps({"approved_sources": [broken]}), encoding="utf-8")
                with self.assertRaisesRegex(TradingStatusError, message):
                    load_approvals(path)

    def test_latest_capture_not_after_cutoff_is_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_capture(root, "early", "2026-09-26T06:00:00+00:00")
            write_capture(root, "late", "2026-09-26T09:00:00+00:00")
            write_capture(root, "future", "2026-09-26T11:00:00+00:00")
            self.assertEqual(latest_capture_before(root, SNAPSHOT["decision_cutoff"]).name, "late")
            self.assertIsNone(latest_capture_before(root, "2026-09-26T05:00:00+00:00"))


if __name__ == "__main__":
    unittest.main()
