"""臨時休市（例如颱風假）：由官方行情缺口推斷，並併入交易日曆。"""
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from etf_agent.data import (
    TradingCalendar,
    TradingCalendarError,
    infer_closed_days,
    load_inferred_closed_days,
    update_inferred_closed_days,
)
from etf_agent.data.evidence import TAIPEI_TIMEZONE

TYPHOON_DAY = "2026-09-21"  # 週一
FACTS = [{"kind": "calendar_event", "event_date": "2026-10-10", "name": "國慶日"}]


def official_calendar():
    return TradingCalendar.from_ogd_facts(FACTS, "fixture-sha")


class InferredClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "prices.db"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE daily_prices (symbol TEXT, trade_date TEXT, source TEXT)")
        connection.commit()
        connection.close()
        # 颱風假前一個交易日（09-18）與後一個交易日（09-22）兩市場都有行情。
        self.add("2330.TW", "2026-09-18", "TWSE_STOCK_DAY")
        self.add("2330.TW", "2026-09-22", "TWSE_STOCK_DAY")
        self.add("6488.TWO", "2026-09-18", "TPEX_TRADING_STOCK")
        self.add("6488.TWO", "2026-09-22", "TPEX_TRADING_STOCK")

    def tearDown(self):
        self.temp.cleanup()

    def add(self, symbol, day, source):
        connection = sqlite3.connect(self.database)
        connection.execute("INSERT INTO daily_prices VALUES (?, ?, ?)", (symbol, day, source))
        connection.commit()
        connection.close()

    def infer(self, today="2026-09-22"):
        return infer_closed_days(self.database, official_calendar(), date.fromisoformat(today))

    def test_gap_confirmed_by_later_day_in_both_markets_is_inferred(self):
        found = self.infer()
        self.assertEqual(list(found), [TYPHOON_DAY])
        self.assertEqual(found[TYPHOON_DAY]["database_rows_on_day"], 0)
        self.assertEqual(found[TYPHOON_DAY]["markets"]["TWSE"]["first_date_after"], "2026-09-22")

    def test_weekend_and_official_holidays_are_not_reported(self):
        self.assertNotIn("2026-09-19", self.infer())
        self.assertNotIn("2026-09-20", self.infer())

    def test_same_evening_without_a_later_day_is_not_inferred(self):
        self.assertEqual(self.infer(today=TYPHOON_DAY), {})

    def test_one_market_missing_later_day_is_not_inferred(self):
        connection = sqlite3.connect(self.database)
        connection.execute("DELETE FROM daily_prices WHERE source = 'TPEX_TRADING_STOCK' AND trade_date = '2026-09-22'")
        connection.commit()
        connection.close()
        self.assertEqual(self.infer(), {})

    def test_any_row_on_the_day_blocks_inference(self):
        self.add("2330.TW", TYPHOON_DAY, "YAHOO_FINANCE")
        self.assertEqual(self.infer(), {})

    def test_no_price_history_at_all_is_not_inferred(self):
        connection = sqlite3.connect(self.database)
        connection.execute("DELETE FROM daily_prices")
        connection.commit()
        connection.close()
        self.assertEqual(self.infer(), {})

    def test_update_saves_evidence_once_and_never_rewrites(self):
        now = datetime(2026, 9, 22, 20, 0, tzinfo=TAIPEI_TIMEZONE)
        captures = self.root / "trading_status"
        self.assertEqual(update_inferred_closed_days(self.database, captures, official_calendar(), now), [TYPHOON_DAY])
        saved = load_inferred_closed_days(captures)
        self.assertEqual(list(saved), [TYPHOON_DAY])
        self.assertIn("record_sha256", saved[TYPHOON_DAY])
        later = datetime(2026, 9, 23, 20, 0, tzinfo=TAIPEI_TIMEZONE)
        self.assertEqual(update_inferred_closed_days(self.database, captures, official_calendar(), later), [])
        self.assertEqual(load_inferred_closed_days(captures), saved)

    def test_corrupt_record_file_is_rejected(self):
        captures = self.root / "trading_status"
        captures.mkdir()
        (captures / "inferred_closed_days.json").write_text(json.dumps({"closed_days": {"2026-09-20": {}}}), encoding="utf-8")
        with self.assertRaises(TradingCalendarError):
            load_inferred_closed_days(captures)
        (captures / "inferred_closed_days.json").write_text("not json", encoding="utf-8")
        with self.assertRaises(TradingCalendarError):
            load_inferred_closed_days(captures)

    def test_load_without_file_is_empty(self):
        self.assertEqual(load_inferred_closed_days(self.root / "nowhere"), {})


class InferredClosureCalendarTests(unittest.TestCase):
    def setUp(self):
        self.official = official_calendar()
        self.inferred = self.official.with_inferred_closed([TYPHOON_DAY])

    def test_session_and_settlement_skip_the_inferred_day(self):
        self.assertTrue(self.official.next_trading_session("2026-09-18T20:00:00+08:00")["start"].startswith(TYPHOON_DAY))
        self.assertTrue(self.inferred.next_trading_session("2026-09-18T20:00:00+08:00")["start"].startswith("2026-09-22"))
        self.assertFalse(self.inferred.is_trading_day(date(2026, 9, 21)))
        self.assertFalse(self.inferred.is_settlement_day(date(2026, 9, 21)))
        # 09-18 成交的 T+2 原為 09-22，臨時休市後順延到 09-23。
        self.assertEqual(self.official.add_settlement_days(date(2026, 9, 18), 2), date(2026, 9, 22))
        self.assertEqual(self.inferred.add_settlement_days(date(2026, 9, 18), 2), date(2026, 9, 23))

    def test_provenance_lists_inferred_days_separately(self):
        official = self.official.provenance()
        inferred = self.inferred.provenance()
        self.assertNotIn("inferred_closed_days", official)
        self.assertEqual(inferred["inferred_closed_days"], [TYPHOON_DAY])
        self.assertEqual(inferred["source_sha256"], official["source_sha256"])
        self.assertNotEqual(inferred["calendar_sha256"], official["calendar_sha256"])


if __name__ == "__main__":
    unittest.main()
