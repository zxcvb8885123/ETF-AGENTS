"""Bug A：盤中抓到、尚未定案的 Yahoo 日線不得進 Snapshot 或資料庫。"""

import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from etf_agent.data import (
    DailyPrice,
    DataAgentService,
    Instrument,
    MarketDataDatabase,
    SnapshotRepository,
    YFinanceHistoryCollector,
    is_price_finalized,
    latest_finalized_price_date,
    price_finalized_at,
)

INSTRUMENT = Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14")
TRADE_DATE = "2026-10-21"
INTRADAY = "2026-10-21T05:04:00+00:00"  # 台北 13:04，Yahoo 已回傳當日未定案日線
AFTER_FINALIZED = "2026-10-21T07:30:00+00:00"  # 台北 15:30


def _price(close, volume):
    return DailyPrice(
        symbol=INSTRUMENT.symbol,
        code=INSTRUMENT.code,
        name=INSTRUMENT.name,
        trade_date=TRADE_DATE,
        open_price=Decimal("2400"),
        high_price=Decimal("2430"),
        low_price=Decimal("2390"),
        close_price=Decimal(close),
        change=None,
        volume_shares=volume,
        trade_value=0,
        transactions=0,
        adjusted_close=Decimal(close),
    )


class PriceFinalityHelpersTests(unittest.TestCase):
    def test_latest_finalized_price_date_switches_at_taipei_15_00(self):
        before = datetime.fromisoformat("2026-10-23T14:59:59+08:00")
        after = datetime.fromisoformat("2026-10-23T15:00:00+08:00")
        self.assertEqual(latest_finalized_price_date(before), date(2026, 10, 22))
        self.assertEqual(latest_finalized_price_date(after), date(2026, 10, 23))
        # UTC 表示同一時刻也得到同樣結果。
        self.assertEqual(
            latest_finalized_price_date(datetime.fromisoformat("2026-10-23T06:59:59+00:00")),
            date(2026, 10, 22),
        )

    def test_naive_times_are_rejected(self):
        with self.assertRaises(ValueError):
            latest_finalized_price_date(datetime(2026, 10, 23, 16, 0))
        with self.assertRaises(ValueError):
            is_price_finalized(TRADE_DATE, "2026-10-21T15:30:00")

    def test_is_price_finalized_compares_against_trade_date_15_00(self):
        self.assertEqual(
            price_finalized_at(TRADE_DATE).isoformat(), "2026-10-21T15:00:00+08:00"
        )
        self.assertFalse(is_price_finalized(TRADE_DATE, INTRADAY))
        self.assertTrue(is_price_finalized(TRADE_DATE, "2026-10-21T07:00:00+00:00"))
        self.assertTrue(is_price_finalized(TRADE_DATE, AFTER_FINALIZED))


class SnapshotFinalityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = MarketDataDatabase(Path(self.directory.name) / "test.db")
        self.database.initialize()
        with self.database.connect() as connection:
            self.database.upsert_instruments(connection, [INSTRUMENT], True)
        self.service = DataAgentService(self.database)

    def store(self, prices, source, fetched_at):
        run_id = "run-%s-%s" % (source, fetched_at)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO collection_runs(run_id, source, started_at, status) VALUES (?, ?, ?, 'success')",
                (run_id, source, fetched_at),
            )
            payload_id = self.database.insert_raw_payload(
                connection, run_id, source, "fixture://%s" % source, fetched_at, "{}"
            )
            self.database.store_prices(connection, prices, source, fetched_at, run_id, payload_id)
        return payload_id

    def snapshot(self, cutoff):
        return self.service.build_snapshot(cutoff, require_universe_validation=False)

    def test_intraday_yahoo_row_is_excluded_until_a_finalized_version_exists(self):
        self.store([_price("2410", 13_000_000)], "YAHOO_FINANCE", INTRADAY)

        # cutoff 在收盤後、定案前：唯一的列是盤中抓的，該日不得進 Snapshot。
        pending = self.snapshot("2026-10-21T06:00:00+00:00")
        self.assertIsNone(pending.latest_trade_date)
        self.assertIn("MISSING_PRICE_DATA", pending.quality_flags)

        # 定案後 cutoff 也一樣：盤中版本永遠不算定案。
        still_pending = self.snapshot("2026-10-21T08:00:00+00:00")
        self.assertIsNone(still_pending.latest_trade_date)

        official_payload = self.store(
            [_price("2420", 27_095_005)], "TWSE_STOCK_DAY_ALL", AFTER_FINALIZED
        )
        official_only = self.snapshot("2026-10-21T08:00:00+00:00")
        self.assertEqual(official_only.latest_trade_date, TRADE_DATE)
        self.assertEqual(official_only.latest_prices[0].source, "TWSE_STOCK_DAY_ALL")
        self.assertEqual(official_only.latest_prices[0].close_price, "2420")

        yahoo_payload = self.store(
            [_price("2421", 22_321_809)], "YAHOO_FINANCE", "2026-10-21T07:31:00+00:00"
        )
        finalized = self.snapshot("2026-10-21T08:00:00+00:00")
        self.assertEqual(finalized.latest_prices[0].source, "YAHOO_FINANCE")
        self.assertEqual(finalized.latest_prices[0].close_price, "2421")
        self.assertEqual(finalized.latest_prices[0].execution_close_price, "2420")
        self.assertEqual(
            finalized.latest_prices[0].source_evidence_id,
            "price:%d:2330.TW:%s" % (yahoo_payload, TRADE_DATE),
        )
        self.assertEqual(
            finalized.latest_prices[0].execution_source_evidence_id,
            "price:%d:2330.TW:%s" % (official_payload, TRADE_DATE),
        )

    def test_identical_content_after_finalization_still_records_a_finalized_version(self):
        self.store([_price("2420", 27_000_000)], "YAHOO_FINANCE", INTRADAY)
        self.store([_price("2420", 27_000_000)], "YAHOO_FINANCE", AFTER_FINALIZED)
        with self.database.connect() as connection:
            fetched = [
                row[0]
                for row in connection.execute(
                    "SELECT fetched_at FROM daily_prices ORDER BY fetched_at"
                )
            ]
        self.assertEqual(fetched, [INTRADAY, AFTER_FINALIZED])
        self.assertEqual(self.snapshot("2026-10-21T08:00:00+00:00").latest_trade_date, TRADE_DATE)

    def test_price_history_excludes_intraday_yahoo_rows(self):
        self.store([_price("2410", 13_000_000)], "YAHOO_FINANCE", INTRADAY)
        with self.database.connect() as connection:
            history = SnapshotRepository.load_price_history(
                connection, "2026-10-22", "2026-10-22T08:00:00+00:00", 5
            )
        self.assertEqual(history, [])


class YahooCollectorFinalityTests(unittest.TestCase):
    def _frame(self):
        import pandas

        index = pandas.to_datetime(["2026-10-20", "2026-10-21"])
        return pandas.DataFrame(
            {
                "Open": [100.0, 101.0],
                "High": [102.0, 103.0],
                "Low": [99.0, 100.0],
                "Close": [101.0, 102.0],
                "Adj Close": [101.0, 102.0],
                "Volume": [1000, 500],
            },
            index=index,
        )

    def test_unfinalized_bar_is_skipped_and_reported(self):
        prices, _payload, warnings, skipped = YFinanceHistoryCollector._convert_frame(
            INSTRUMENT,
            self._frame(),
            date(2026, 10, 1),
            date(2026, 10, 21),
            datetime.fromisoformat(INTRADAY),
        )
        self.assertEqual([item.trade_date for item in prices], ["2026-10-20"])
        self.assertEqual(skipped, ["2026-10-21"])
        self.assertEqual(warnings, [])

    def test_finalized_bars_are_kept(self):
        prices, _payload, _warnings, skipped = YFinanceHistoryCollector._convert_frame(
            INSTRUMENT,
            self._frame(),
            date(2026, 10, 1),
            date(2026, 10, 21),
            datetime.fromisoformat(AFTER_FINALIZED),
        )
        self.assertEqual([item.trade_date for item in prices], ["2026-10-20", "2026-10-21"])
        self.assertEqual(skipped, [])

    def test_naive_fetch_time_is_rejected(self):
        with self.assertRaises(ValueError):
            YFinanceHistoryCollector._convert_frame(
                INSTRUMENT, self._frame(), date(2026, 10, 1), date(2026, 10, 21),
                datetime(2026, 10, 21, 16, 0),
            )

    def test_default_fetch_time_is_now_so_past_bars_are_kept(self):
        prices, _payload, _warnings, skipped = YFinanceHistoryCollector._convert_frame(
            INSTRUMENT, self._frame(), date(2026, 10, 1), date(2026, 10, 21)
        )
        self.assertEqual(len(prices) + len(skipped), 2)
        if datetime.now(timezone.utc) >= price_finalized_at("2026-10-21"):
            self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main()
