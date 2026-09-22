import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.data import (
    DataAgentService,
    DailyPriceCollector,
    Instrument,
    MarketDataDatabase,
    SnapshotPrice,
    TpexDailyProvider,
    TwseDailyProvider,
    collect_latest_prices,
)
from etf_agent.data.evidence import SourceEvidenceBuilder


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "twse_stock_day_all.json"
TPEX_FIXTURE = ROOT / "tests" / "fixtures" / "universe_validation_tpex.json"


class FixtureProvider(TwseDailyProvider):
    def __init__(self):
        super().__init__("fixture://twse")

    def fetch(self):
        return FIXTURE.read_text(encoding="utf-8"), "2026-09-11T06:00:00+00:00"


class TpexFixtureProvider(TpexDailyProvider):
    def __init__(self):
        super().__init__("fixture://tpex")

    def fetch(self):
        return TPEX_FIXTURE.read_text(encoding="utf-8"), "2026-09-17T06:00:00+00:00"


class DataCollectionTests(unittest.TestCase):
    def test_price_evidence_id_is_unique_per_symbol_in_one_raw_response(self):
        first = SourceEvidenceBuilder.price_evidence_id(
            {"raw_payload_id": 42, "symbol": "2330.TW", "trade_date": "2026-09-21"}
        )
        second = SourceEvidenceBuilder.price_evidence_id(
            {"raw_payload_id": 42, "symbol": "3718.TWO", "trade_date": "2026-09-21"}
        )

        self.assertNotEqual(first, second)
        self.assertEqual(first, "price:42:2330.TW:2026-09-21")

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

    def test_snapshot_exposes_price_evidence_and_locks_selected_version(self):
        universe = [Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-07-31")]
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            DailyPriceCollector(database, FixtureProvider()).collect(universe)
            service = DataAgentService(database)

            before_fetch = service.build_snapshot(
                "2026-09-11T05:59:59+00:00",
                require_universe_validation=False,
            )
            before_close = service.build_snapshot(
                "2026-09-11T01:00:00+00:00",
                require_universe_validation=False,
            )
            snapshot = service.build_snapshot(
                "2026-09-11T06:00:01+00:00",
                require_universe_validation=False,
            )

            self.assertIsNone(before_fetch.latest_trade_date)
            self.assertIsNone(before_close.latest_trade_date)
            self.assertEqual(before_fetch.source_evidence, [])
            self.assertEqual(snapshot.latest_price_symbols, 1)
            self.assertIsInstance(snapshot.latest_prices[0], SnapshotPrice)
            self.assertEqual(snapshot.latest_prices[0].symbol, "2330.TW")
            evidence_id = snapshot.latest_prices[0].source_evidence_id
            evidence = snapshot.source_evidence[0]
            self.assertEqual(evidence.evidence_id, evidence_id)
            self.assertEqual(evidence.authority, "twse")
            self.assertEqual(evidence.url, "fixture://twse")
            self.assertEqual(
                evidence.content_as_of, "2026-09-11T13:30:00+08:00"
            )
            self.assertEqual(evidence.fetched_at, "2026-09-11T14:00:00+08:00")
            serialized = snapshot.as_dict()
            self.assertEqual(serialized["latest_prices"][0]["symbol"], "2330.TW")
            self.assertEqual(
                serialized["source_evidence"][0]["evidence_id"], evidence_id
            )
            with database.connect() as connection:
                link = connection.execute(
                    """
                    SELECT symbol, trade_date, source, raw_payload_id
                    FROM snapshot_prices WHERE snapshot_id = ?
                    """,
                    (snapshot.snapshot_id,),
                ).fetchone()
            locked_raw_payload_id = link["raw_payload_id"]
            self.assertEqual(link["symbol"], "2330.TW")
            self.assertEqual(link["trade_date"], "2026-09-11")
            self.assertEqual(link["source"], "TWSE_STOCK_DAY_ALL")
            self.assertGreater(locked_raw_payload_id, 0)

            DailyPriceCollector(database, FixtureProvider()).collect(universe)
            with database.connect() as connection:
                locked_after_refresh = connection.execute(
                    "SELECT raw_payload_id FROM snapshot_prices WHERE snapshot_id = ?",
                    (snapshot.snapshot_id,),
                ).fetchone()["raw_payload_id"]
                current_raw_payload_id = connection.execute(
                    "SELECT raw_payload_id FROM daily_prices WHERE symbol = '2330.TW'"
                ).fetchone()["raw_payload_id"]
            self.assertEqual(locked_after_refresh, locked_raw_payload_id)
            self.assertNotEqual(current_raw_payload_id, locked_raw_payload_id)

    def test_parses_and_collects_tpex_latest_price(self):
        prices, warnings = TpexDailyProvider.parse(
            TPEX_FIXTURE.read_text(encoding="utf-8")
        )
        self.assertEqual(warnings, [])
        self.assertEqual(prices[1].symbol, "3718.TWO")
        self.assertEqual(prices[1].trade_date, "2026-09-17")
        self.assertEqual(str(prices[1].close_price), "95.00")
        self.assertEqual(prices[1].volume_shares, 16475000)

        universe = [
            Instrument("3718.TWO", "3718", "中光電投控", "TPEX", "2026-09-14")
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            result = DailyPriceCollector(database, TpexFixtureProvider()).collect(universe)
            self.assertEqual(result.source, "TPEX_MAINBOARD_QUOTES")
            self.assertEqual(result.market, "TPEX")
            self.assertEqual(result.stored_rows, 1)
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT symbol, source FROM daily_prices"
                ).fetchone()
            self.assertEqual(row["symbol"], "3718.TWO")
            self.assertEqual(row["source"], "TPEX_MAINBOARD_QUOTES")

    def test_tpex_corporate_action_change_does_not_drop_quote(self):
        payload = json.dumps(
            [
                {
                    "Date": "1150917",
                    "SecuritiesCompanyCode": "3718",
                    "CompanyName": "中光電投控",
                    "Close": "64.10",
                    "Change": "除息",
                    "Open": "----",
                    "High": "65.00",
                    "Low": "63.50",
                    "TradingShares": "1,000",
                    "TransactionAmount": "64,100",
                    "TransactionNumber": "10",
                }
            ],
            ensure_ascii=False,
        )
        prices, warnings = TpexDailyProvider.parse(payload)
        self.assertEqual(len(prices), 1)
        self.assertIsNone(prices[0].change)
        self.assertIsNone(prices[0].open_price)
        self.assertEqual(len(warnings), 1)
        self.assertIn("除息", warnings[0])

    def test_collects_twse_and_tpex_as_one_latest_price_operation(self):
        universe = [
            Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14"),
            Instrument("3718.TWO", "3718", "中光電投控", "TPEX", "2026-09-14"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            result = collect_latest_prices(
                database,
                universe,
                [FixtureProvider(), TpexFixtureProvider()],
            )
            self.assertEqual(result.stored_rows, 2)
            self.assertEqual(len(result.collections), 2)
            with database.connect() as connection:
                rows = connection.execute(
                    "SELECT symbol, source FROM analysis_daily_prices ORDER BY symbol"
                ).fetchall()
                source_dates = connection.execute(
                    "SELECT symbol, source_date FROM instruments ORDER BY symbol"
                ).fetchall()
            self.assertEqual(
                [(row["symbol"], row["source"]) for row in rows],
                [
                    ("2330.TW", "TWSE_STOCK_DAY_ALL"),
                    ("3718.TWO", "TPEX_MAINBOARD_QUOTES"),
                ],
            )
            self.assertEqual(
                [(row["symbol"], row["source_date"]) for row in source_dates],
                [("2330.TW", "2026-09-14"), ("3718.TWO", "2026-09-14")],
            )
            self.assertEqual(
                database.latest_complete_trade_date(
                    ("TWSE_STOCK_DAY_ALL", "TPEX_MAINBOARD_QUOTES")
                ),
                "2026-09-11",
            )

    def test_rejects_empty_universe_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            collector = DailyPriceCollector(database, FixtureProvider())
            with self.assertRaisesRegex(ValueError, "官方交易池是空的"):
                collector.collect([])


if __name__ == "__main__":
    unittest.main()
