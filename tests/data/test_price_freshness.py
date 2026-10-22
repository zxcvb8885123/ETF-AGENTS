import tempfile
import unittest
from datetime import date
from pathlib import Path

from etf_agent.data import MarketDataDatabase
from etf_agent.data.price_freshness import prices_current

DAY = date(2026, 10, 22)
AFTER_CLOSE = "2026-10-22T07:20:00+00:00"  # 台北 15:20
BEFORE_READY = "2026-10-22T05:00:00+00:00"  # 台北 13:00


class PricesCurrentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        database = MarketDataDatabase(Path(self.directory.name) / "test.db")
        database.initialize()
        self.connection_manager = database.connect()
        self.connection = self.connection_manager.__enter__()
        self.addCleanup(self.connection_manager.__exit__, None, None, None)
        self.connection.execute("PRAGMA foreign_keys = OFF")

    def add(self, symbol, source, fetched_at=AFTER_CLOSE, trade_date=DAY.isoformat()):
        self.connection.execute(
            "INSERT INTO daily_prices (symbol, trade_date, close_price, volume_shares, trade_value, transactions, "
            "source, fetched_at, run_id, raw_payload_id) VALUES (?, ?, '1', 1, 1, 1, ?, ?, 'r', 1)",
            (symbol, trade_date, source, fetched_at),
        )

    def test_complete_rows_fetched_after_close_are_current(self):
        self.add("2330.TW", "YAHOO_FINANCE")
        self.add("3260.TWO", "YAHOO_FINANCE")
        current, _ = prices_current(self.connection, ["2330.TW", "3260.TWO"], "yahoo", DAY)
        self.assertTrue(current)

    def test_rows_use_the_source_of_their_market_for_each_step(self):
        self.add("2330.TW", "TWSE_STOCK_DAY_ALL")
        self.add("3260.TWO", "TPEX_MAINBOARD_QUOTES")
        self.assertTrue(prices_current(self.connection, ["2330.TW", "3260.TWO"], "official_latest", DAY)[0])
        self.assertFalse(prices_current(self.connection, ["2330.TW", "3260.TWO"], "official_history", DAY)[0])

    def test_missing_symbol_is_not_current(self):
        self.add("2330.TW", "YAHOO_FINANCE")
        current, reason = prices_current(self.connection, ["2330.TW", "2317.TW"], "yahoo", DAY)
        self.assertFalse(current)
        self.assertIn("缺 1 檔", reason)

    def test_rows_fetched_before_ready_time_are_not_final(self):
        self.add("2330.TW", "YAHOO_FINANCE", fetched_at=BEFORE_READY)
        self.assertFalse(prices_current(self.connection, ["2330.TW"], "yahoo", DAY)[0])

    def test_other_trade_date_is_not_current(self):
        self.add("2330.TW", "YAHOO_FINANCE", trade_date="2026-10-21")
        self.assertFalse(prices_current(self.connection, ["2330.TW"], "yahoo", DAY)[0])

    def test_unreadable_fetch_time_and_unknown_step_fail_closed(self):
        self.add("2330.TW", "YAHOO_FINANCE", fetched_at="not-a-time")
        self.assertFalse(prices_current(self.connection, ["2330.TW"], "yahoo", DAY)[0])
        with self.assertRaises(ValueError):
            prices_current(self.connection, ["2330.TW"], "unknown", DAY)
        self.assertFalse(prices_current(self.connection, [], "yahoo", DAY)[0])


if __name__ == "__main__":
    unittest.main()
