import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from etf_agent.data import (
    TradingCalendar,
    TradingCalendarError,
    calendar_from_capture,
    latest_calendar_capture_before,
)
from etf_agent.data.evidence import TAIPEI_TIMEZONE


ROOT = Path(__file__).resolve().parents[1]
# 節錄自 2026（民國 115）年 TWSE 開休市日期表。
CALENDAR_CSV = (
    "名稱,日期,星期,說明\n"
    "\"農曆春節前最後交易日\",\"1150211\",\"三\",\"農曆春節前最後交易。\"\n"
    "\"市場無交易，僅辦理結算交割作業\",\"1150212\",\"四\",\"\"\n"
    "\"市場無交易，僅辦理結算交割作業\",\"1150213\",\"五\",\"\"\n"
    "\"農曆除夕及春節\",\"1150216\",\"一\",\"放假\"\n"
    "\"農曆除夕及春節\",\"1150217\",\"二\",\"放假\"\n"
    "\"農曆除夕及春節\",\"1150218\",\"三\",\"放假\"\n"
    "\"農曆除夕及春節\",\"1150219\",\"四\",\"放假\"\n"
    "\"農曆除夕及春節\",\"1150220\",\"五\",\"補假\"\n"
    "\"農曆春節後開始交易日\",\"1150223\",\"一\",\"開始交易。\"\n"
    "\"中秋節\",\"1150925\",\"五\",\"依規定放假1日。\"\n"
    "\"孔子誕辰紀念日/ 教師節\",\"1150928\",\"一\",\"依規定放假1日。\"\n"
    "\"國慶日\",\"1151009\",\"五\",\"補假\"\n"
    "\"國慶日\",\"1151010\",\"六\",\"依規定放假1日。\"\n"
)
FETCHED = "2026-09-28T08:00:00+00:00"


def write_calendar_capture(root: Path, name: str, fetched: str = FETCHED, text: str = CALENDAR_CSV, others_failed: bool = False) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    raw = text.encode("utf-8")
    (directory / "TWSE_CALENDAR_OGD.csv").write_bytes(raw)
    sources = [{
        "source_id": "TWSE_CALENDAR_OGD", "status": "captured_candidate_only",
        "sha256": hashlib.sha256(raw).hexdigest(), "fetch_started_at": fetched, "fetch_finished_at": fetched,
    }]
    if others_failed:
        sources.append({"source_id": "TWSE_HALTS_OGD", "status": "failed", "fetch_finished_at": fetched})
    (directory / "manifest.json").write_text(
        json.dumps({"status": "failed" if others_failed else "captured_candidate_only", "sources": sources}), encoding="utf-8"
    )
    return directory


def facts(rows):
    return [{"kind": "calendar_event", "event_date": day, "name": name} for day, name in rows]


class TradingCalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calendar = calendar_from_capture(write_calendar_capture(self.root, "capture-1"))

    def tearDown(self):
        self.temp.cleanup()

    def test_national_day_make_up_holiday_is_closed(self):
        self.assertFalse(self.calendar.is_trading_day(date(2026, 10, 9)))
        self.assertTrue(self.calendar.is_trading_day(date(2026, 10, 8)))
        self.assertEqual(self.calendar.next_trading_day(date(2026, 10, 8)), date(2026, 10, 12))

    def test_settlement_skips_holidays_and_weekends(self):
        # 10/8 成交：10/9 補假、10/10–11 週末 → T+1 10/12、T+2 10/13。
        self.assertEqual(self.calendar.add_settlement_days(date(2026, 10, 8), 2), date(2026, 10, 13))
        # 9/24 成交：9/25 中秋節、9/26–27 週末、9/28 教師節 → T+2 為 9/30。
        self.assertEqual(self.calendar.add_settlement_days(date(2026, 9, 24), 2), date(2026, 9, 30))

    def test_settlement_only_days_count_for_settlement_but_not_trading(self):
        self.assertFalse(self.calendar.is_trading_day(date(2026, 2, 12)))
        self.assertTrue(self.calendar.is_settlement_day(date(2026, 2, 12)))
        self.assertEqual(self.calendar.add_settlement_days(date(2026, 2, 11), 2), date(2026, 2, 13))
        self.assertEqual(self.calendar.next_trading_day(date(2026, 2, 11)), date(2026, 2, 23))

    def test_trading_marks_are_not_holidays(self):
        self.assertTrue(self.calendar.is_trading_day(date(2026, 2, 11)))
        self.assertTrue(self.calendar.is_trading_day(date(2026, 2, 23)))

    def test_session_after_friday_cutoff_skips_weekend_and_holiday(self):
        session = self.calendar.next_trading_session("2026-10-08T18:00:00+08:00")
        self.assertEqual(session, {"start": "2026-10-12T09:00:00+08:00", "end": "2026-10-12T13:30:00+08:00"})

    def test_uncovered_year_is_rejected(self):
        with self.assertRaisesRegex(TradingCalendarError, "未涵蓋 2027"):
            self.calendar.is_trading_day(date(2027, 1, 4))
        # 12/30 的 T+2 跨年，日曆未涵蓋 2027 年時拒絕而非推估。
        with self.assertRaisesRegex(TradingCalendarError, "未涵蓋 2027"):
            self.calendar.add_settlement_days(date(2026, 12, 31), 2)

    def test_empty_calendar_and_naive_cutoff_are_rejected(self):
        with self.assertRaisesRegex(TradingCalendarError, "沒有任何日期"):
            TradingCalendar.from_ogd_facts([], "sha")
        with self.assertRaises(TradingCalendarError):
            self.calendar.next_trading_session("2026-10-08T18:00:00")

    def test_provenance_changes_with_content(self):
        other = TradingCalendar.from_ogd_facts(facts([("2026-10-09", "國慶日")]), "sha-2")
        self.assertEqual(self.calendar.provenance()["basis"], "twse_ogd_calendar")
        self.assertNotEqual(self.calendar.provenance()["calendar_sha256"], other.provenance()["calendar_sha256"])

    def test_tampered_calendar_capture_is_rejected(self):
        capture = write_calendar_capture(self.root, "capture-2")
        (capture / "TWSE_CALENDAR_OGD.csv").write_text("名稱,日期,星期,說明\n", encoding="utf-8")
        with self.assertRaisesRegex(TradingCalendarError, "雜湊不一致"):
            calendar_from_capture(capture)

    def test_latest_capture_respects_time_limit_and_ignores_other_failed_sources(self):
        write_calendar_capture(self.root, "capture-late", fetched="2026-09-29T08:00:00+00:00")
        partial = write_calendar_capture(self.root, "capture-partial", fetched="2026-09-28T09:00:00+00:00", others_failed=True)
        self.assertEqual(latest_calendar_capture_before(self.root, "2026-09-28T18:00:00+08:00"), partial)
        self.assertIsNone(latest_calendar_capture_before(self.root, "2026-09-27T00:00:00+08:00"))


class DailyPipelineTradingDayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("run_daily_pipeline", ROOT / "scripts" / "run_daily_pipeline.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_holiday_is_skipped_and_missing_calendar_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(TradingCalendarError, "找不到"):
                self.module.trading_day_check(root, datetime(2026, 10, 9, 18, tzinfo=TAIPEI_TIMEZONE))
            write_calendar_capture(root, "capture-1")
            self.assertFalse(self.module.trading_day_check(root, datetime(2026, 10, 9, 18, tzinfo=TAIPEI_TIMEZONE)))
            self.assertTrue(self.module.trading_day_check(root, datetime(2026, 10, 8, 18, tzinfo=TAIPEI_TIMEZONE)))
            self.assertFalse(self.module.trading_day_check(root, datetime(2026, 10, 10, 18, tzinfo=TAIPEI_TIMEZONE)))


if __name__ == "__main__":
    unittest.main()
