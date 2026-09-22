import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from etf_agent.data import (
    DataAgentService,
    DailyPrice,
    HistoricalPriceProvider,
    HistoricalResponse,
    Instrument,
    MarketDataDatabase,
    OfficialHistoricalRefreshService,
    YFinanceHistoryCollector,
    month_starts,
    plan_history_refresh,
)


class OfficialHistoryFixtureProvider:
    twse_source = "TWSE_STOCK_DAY"
    tpex_source = "TPEX_TRADING_STOCK"

    def __init__(self, failed_symbols=()):
        self.failed_symbols = set(failed_symbols)

    def fetch(self, instrument, month):
        if instrument.symbol in self.failed_symbols:
            raise TimeoutError("fixture timeout")
        source = (
            self.tpex_source
            if instrument.market.upper() in {"TPEX", "OTC", "上櫃"}
            else self.twse_source
        )
        price = DailyPrice(
            symbol=instrument.symbol,
            code=instrument.code,
            name=instrument.name,
            trade_date="2026-09-17",
            open_price=Decimal("100"),
            high_price=Decimal("101"),
            low_price=Decimal("99"),
            close_price=Decimal("100"),
            change=Decimal("1"),
            volume_shares=1000,
            trade_value=100000,
            transactions=1,
        )
        return HistoricalResponse(
            prices=(price,),
            payload=json.dumps({"symbol": instrument.symbol}),
            endpoint="fixture://%s/%s" % (source, month.isoformat()),
            fetched_at="2026-09-17T06:00:00+00:00",
            source=source,
            warnings=(),
        )


def seed_latest_price(database, instrument, source, trade_date="2026-09-17"):
    database.initialize()
    with database.connect() as connection:
        database.upsert_instruments(connection, [instrument], True)
        run_id = "seed-%s-%s-%s" % (source, instrument.symbol, trade_date)
        connection.execute(
            "INSERT INTO collection_runs(run_id, source, started_at, status) VALUES (?, ?, ?, 'success')",
            (run_id, source, "%sT06:00:00+00:00" % trade_date),
        )
        payload_id = database.insert_raw_payload(
            connection,
            run_id,
            source,
            "fixture://latest/%s" % instrument.symbol,
            "%sT06:00:00+00:00" % trade_date,
            "{}",
        )
        database.upsert_prices(
            connection,
            [
                DailyPrice(
                    symbol=instrument.symbol,
                    code=instrument.code,
                    name=instrument.name,
                    trade_date=trade_date,
                    open_price=Decimal("100"),
                    high_price=Decimal("101"),
                    low_price=Decimal("99"),
                    close_price=Decimal("100"),
                    change=Decimal("1"),
                    volume_shares=1000,
                    trade_value=100000,
                    transactions=1,
                )
            ],
            source,
            "%sT06:00:00+00:00" % trade_date,
            run_id,
            payload_id,
        )


class HistoricalProviderTests(unittest.TestCase):
    def test_snapshot_uses_latest_fully_covered_day_and_official_price(self):
        universe = [
            Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14"),
            Instrument("3718.TWO", "3718", "中光電投控", "TPEX", "2026-09-14"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            for instrument in universe:
                seed_latest_price(
                    database, instrument, "YAHOO_FINANCE", "2026-09-21"
                )
            seed_latest_price(
                database, universe[0], "TWSE_STOCK_DAY_ALL", "2026-09-21"
            )
            seed_latest_price(
                database, universe[1], "TPEX_MAINBOARD_QUOTES", "2026-09-22"
            )

            snapshot = DataAgentService(database).build_snapshot(
                "2026-09-22T18:00:00+08:00",
                require_universe_validation=False,
            )

        self.assertTrue(snapshot.usable)
        self.assertEqual(snapshot.latest_trade_date, "2026-09-21")
        self.assertEqual(snapshot.latest_price_symbols, 2)
        self.assertEqual(
            [(item.symbol, item.source) for item in snapshot.latest_prices],
            [("2330.TW", "TWSE_STOCK_DAY_ALL"), ("3718.TWO", "YAHOO_FINANCE")],
        )

    def test_yfinance_collection_requests_day_after_safe_end(self):
        class EmptyColumns:
            nlevels = 1

        class EmptyFrame:
            columns = EmptyColumns()
            empty = True

            def dropna(self, **_kwargs):
                return self

        class RecordingYFinance:
            def __init__(self):
                self.calls = []

            def download(self, symbols, **kwargs):
                self.calls.append((symbols, kwargs))
                return EmptyFrame()

        provider = RecordingYFinance()
        instrument = Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14")
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            collector = YFinanceHistoryCollector(database, retries=0)
            with patch("etf_agent.data.yfinance_history.importlib.import_module", return_value=provider):
                result = collector.collect(
                    [instrument], date(2026, 9, 10), date(2026, 9, 21)
                )

        self.assertEqual(result.missing_symbols, ("2330.TW",))
        self.assertEqual(provider.calls[0][1]["end"], "2026-09-22")

    def test_month_starts_includes_partial_boundary_months(self):
        self.assertEqual(
            month_starts(date(2024, 9, 13), date(2026, 9, 13))[0],
            date(2024, 9, 1),
        )
        self.assertEqual(len(month_starts(date(2024, 9, 13), date(2026, 9, 13))), 25)

    def test_parses_twse_month(self):
        instrument = Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-07-31")
        payload = json.dumps(
            {
                "stat": "OK",
                "data": [["115/09/01", "31,855,287", "77,463,413,685", "2395", "2440", "2390", "2440", "+35", "65,271", ""]],
            }
        )
        prices, warnings = HistoricalPriceProvider.parse_twse(payload, instrument)
        self.assertEqual(warnings, [])
        self.assertEqual(prices[0].trade_date, "2026-09-01")
        self.assertEqual(prices[0].volume_shares, 31855287)
        self.assertEqual(prices[0].trade_value, 77463413685)

    def test_parses_tpex_units(self):
        instrument = Instrument("5274.TWO", "5274", "信驊", "TPEX", "2026-07-31")
        payload = json.dumps(
            {
                "stat": "ok",
                "tables": [{"data": [["115/09/01", "504", "8,595,448", "16210", "17670", "16000", "17120", "1055", "11,458"]]}],
            }
        )
        prices, warnings = HistoricalPriceProvider.parse_tpex(payload, instrument)
        self.assertEqual(warnings, [])
        self.assertEqual(prices[0].volume_shares, 504000)
        self.assertEqual(prices[0].trade_value, 8595448000)

    def test_incremental_refresh_backfills_missing_symbols_and_overlaps_existing(self):
        universe = [
            Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14"),
            Instrument("3718.TWO", "3718", "中光電投控", "TPEX", "2026-09-14"),
        ]
        batches = plan_history_refresh(
            universe,
            {"2330.TW": date(2026, 9, 11)},
            date(2026, 9, 18),
            lookback_years=2,
            overlap_days=7,
        )
        self.assertEqual(
            [(batch.start_date, [item.symbol for item in batch.instruments]) for batch in batches],
            [
                (date(2024, 9, 18), ["3718.TWO"]),
                (date(2026, 9, 4), ["2330.TW"]),
            ],
        )

    def test_official_refresh_requires_safe_end_and_reports_coverage(self):
        universe = [
            Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14"),
            Instrument("3718.TWO", "3718", "中光電投控", "TPEX", "2026-09-14"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            seed_latest_price(database, universe[0], "TWSE_STOCK_DAY_ALL")
            seed_latest_price(database, universe[1], "TPEX_MAINBOARD_QUOTES")
            service = OfficialHistoricalRefreshService(
                database, OfficialHistoryFixtureProvider(), max_workers=1, retries=0
            )
            with self.assertRaisesRegex(ValueError, "晚於最近完整官方交易日"):
                service.refresh(universe, end=date(2026, 9, 18))

            first = service.refresh(universe, start=date(2026, 9, 1))
            self.assertEqual(first.status, "completed")
            self.assertEqual(first.exit_code, 0)
            self.assertEqual(first.safe_end_date, "2026-09-17")
            self.assertEqual([row.end_coverage for row in first.coverage], ["present", "present"])
            self.assertEqual(
                [row.range_coverage for row in first.coverage],
                ["unverified_without_official_calendar"] * 2,
            )
            with database.connect() as connection:
                sources = connection.execute(
                    "SELECT symbol, source FROM analysis_daily_prices WHERE trade_date = '2026-09-17' ORDER BY symbol"
                ).fetchall()
            self.assertEqual(
                [(row["symbol"], row["source"]) for row in sources],
                [("2330.TW", "TWSE_STOCK_DAY"), ("3718.TWO", "TPEX_TRADING_STOCK")],
            )

            second = service.refresh(universe)
            self.assertEqual(second.batches[0].start_date, "2026-09-10")
            self.assertEqual(second.status, "completed")

    def test_official_refresh_preserves_failed_run_and_returns_failed_report(self):
        universe = [
            Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14"),
            Instrument("3718.TWO", "3718", "中光電投控", "TPEX", "2026-09-14"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            seed_latest_price(database, universe[0], "TWSE_STOCK_DAY_ALL")
            seed_latest_price(database, universe[1], "TPEX_MAINBOARD_QUOTES")
            result = OfficialHistoricalRefreshService(
                database,
                OfficialHistoryFixtureProvider(failed_symbols=("3718.TWO",)),
                max_workers=1,
                retries=0,
            ).refresh(universe, start=date(2026, 9, 1))
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.batches[0].status, "failed")
            self.assertEqual(
                next(item for item in result.coverage if item.symbol == "3718.TWO").end_coverage,
                "missing",
            )
            with database.connect() as connection:
                run = connection.execute(
                    "SELECT status FROM collection_runs WHERE run_id = ?",
                    (result.batches[0].run_id,),
                ).fetchone()
            self.assertEqual(run["status"], "failed")


if __name__ == "__main__":
    unittest.main()
