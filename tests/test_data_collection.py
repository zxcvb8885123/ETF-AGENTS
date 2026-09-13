import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.data import (
    DailyPriceCollector,
    Instrument,
    MarketDataDatabase,
    TwseDailyProvider,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "twse_stock_day_all.json"


class FixtureProvider(TwseDailyProvider):
    def __init__(self):
        super().__init__("fixture://twse")

    def fetch(self):
        return FIXTURE.read_text(encoding="utf-8"), "2026-09-11T06:00:00+00:00"


class DataCollectionTests(unittest.TestCase):
    def test_parses_roc_date_and_numeric_fields(self):
        prices, warnings = TwseDailyProvider.parse(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(warnings, [])
        self.assertEqual(prices[0].symbol, "2330.TW")
        self.assertEqual(prices[0].trade_date, "2026-09-11")
        self.assertEqual(str(prices[0].close_price), "1255.00")
        self.assertEqual(prices[0].volume_shares, 20000)

    def test_collects_only_universe_and_is_idempotent(self):
        universe = [Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-07-31")]
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            collector = DailyPriceCollector(database, FixtureProvider())
            first = collector.collect(universe)
            second = collector.collect(universe)
            self.assertEqual(first.stored_rows, 1)
            self.assertEqual(second.stored_rows, 1)
            with database.connect() as connection:
                price_count = connection.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
                raw_count = connection.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0]
                run_count = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
            self.assertEqual(price_count, 1)
            self.assertEqual(raw_count, 2)
            self.assertEqual(run_count, 2)

    def test_rejects_empty_universe_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            collector = DailyPriceCollector(database, FixtureProvider())
            with self.assertRaisesRegex(ValueError, "官方交易池是空的"):
                collector.collect([])


if __name__ == "__main__":
    unittest.main()
