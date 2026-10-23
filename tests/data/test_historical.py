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
    HistoricalCollectionError,
    HistoricalPriceCollector,
    HistoricalPriceProvider,
    RequestGate,
    SourceBlockedError,
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

    def __init__(self, failed_symbols=()):
        self.failed_symbols = set(failed_symbols)

    def source_for(self, instrument):
        if instrument.market.upper() != "TWSE":
            raise ValueError("fixture 只支援上市")
        return self.twse_source

    def fetch(self, instrument, month):
        if instrument.symbol in self.failed_symbols:
            raise TimeoutError("fixture timeout")
        source = self.twse_source
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


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedOpener:
    """依序回應 HTTP 狀態碼或成功的 TWSE 月行情。"""

    payload = json.dumps({"stat": "OK", "data": [
        ["115/09/01", "1", "1", "10", "11", "9", "10", "+1", "1", ""],
    ]})

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, request, timeout):
        import urllib.error

        self.calls += 1
        step = self.script.pop(0)
        if step == "ok":
            return _Response(self.payload)
        raise urllib.error.HTTPError(request.full_url, step, "throttled", {}, None)


class _Response:
    def __init__(self, text):
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.text.encode("utf-8")


def seed_latest_price(database, instrument, source, trade_date="2026-09-17"):
    database.initialize()
    with database.connect() as connection:
        database.upsert_instruments(connection, [instrument], True)
        run_id = "seed-%s-%s-%s" % (source, instrument.symbol, trade_date)
        connection.execute(
            "INSERT INTO collection_runs(run_id, source, started_at, status) VALUES (?, ?, ?, 'success')",
            (run_id, source, "%sT08:00:00+00:00" % trade_date),
        )
        payload_id = database.insert_raw_payload(
            connection,
            run_id,
            source,
            "fixture://latest/%s" % instrument.symbol,
            "%sT08:00:00+00:00" % trade_date,
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
            "%sT08:00:00+00:00" % trade_date,
            run_id,
            payload_id,
        )


class HistoricalProviderTests(unittest.TestCase):
    def test_snapshot_uses_latest_fully_covered_day_and_yahoo_research_price(self):
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
            [("2330.TW", "YAHOO_FINANCE"), ("3718.TWO", "YAHOO_FINANCE")],
        )
        # 2330 同日另有官方價：決策用價與其證據取官方列；3718 沒有官方價則不填。
        first, second = snapshot.latest_prices
        self.assertEqual(first.execution_price_source, "TWSE_STOCK_DAY_ALL")
        self.assertIsNotNone(first.execution_close_price)
        self.assertNotEqual(first.execution_source_evidence_id, first.source_evidence_id)
        self.assertIn(
            first.execution_source_evidence_id,
            [item.evidence_id for item in snapshot.source_evidence],
        )
        self.assertIsNone(second.execution_close_price)

    def test_research_snapshot_fills_same_day_yahoo_gap_with_official_price(self):
        """Yahoo 只缺當日時，同日官方價仍要被讀取，不因其他日期有 Yahoo 就丟棄。"""
        universe = [
            Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14"),
            Instrument("3718.TWO", "3718", "中光電投控", "TPEX", "2026-09-14"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            for instrument in universe:
                seed_latest_price(database, instrument, "YAHOO_FINANCE", "2026-09-21")
                source = "TWSE_STOCK_DAY_ALL" if instrument.market == "TWSE" else "TPEX_MAINBOARD_QUOTES"
                seed_latest_price(database, instrument, source, "2026-09-22")
            seed_latest_price(database, universe[0], "YAHOO_FINANCE", "2026-09-22")
            snapshot = DataAgentService(database).build_snapshot(
                "2026-09-22T18:00:00+08:00", require_universe_validation=False
            )
            with database.connect() as connection:
                view_rows = connection.execute(
                    "SELECT symbol, source FROM analysis_daily_prices "
                    "WHERE trade_date = '2026-09-22' ORDER BY symbol"
                ).fetchall()
        self.assertTrue(snapshot.usable)
        self.assertEqual(snapshot.latest_trade_date, "2026-09-22")
        self.assertEqual(snapshot.latest_price_symbols, 2)
        expected = [("2330.TW", "YAHOO_FINANCE"), ("3718.TWO", "TPEX_MAINBOARD_QUOTES")]
        self.assertEqual(
            [(item.symbol, item.source) for item in snapshot.latest_prices], expected
        )
        self.assertEqual([tuple(row) for row in view_rows], expected)

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
        for _symbols, kwargs in provider.calls:
            self.assertIs(kwargs["auto_adjust"], False)

    def test_refresh_refetches_whole_series_when_yahoo_basis_changes(self):
        import pandas

        class DividendYFinance:
            """除息前 Adj Close 等於 Close；除息後 Yahoo 把除息日前全部往下調整。"""

            def __init__(self):
                self.ex_date = None
                self.calls = []

            def download(self, symbols, start, end, **_kwargs):
                self.calls.append(start)
                index = pandas.bdate_range(start, pandas.Timestamp(end) - pandas.Timedelta(days=1))
                index = index[(index >= "2026-09-01") & (index <= "2026-09-18")]
                close = [100.0] * len(index)
                adjusted = [
                    value * (0.97 if self.ex_date and day < pandas.Timestamp(self.ex_date) else 1.0)
                    for value, day in zip(close, index)
                ]
                return pandas.DataFrame(
                    {"Open": close, "High": close, "Low": close, "Close": close,
                     "Adj Close": adjusted, "Volume": [1000] * len(index)},
                    index=index,
                )

        provider = DividendYFinance()
        instrument = Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14")
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            collector = YFinanceHistoryCollector(database, retries=0)
            with patch("etf_agent.data.yfinance_history.importlib.import_module", return_value=provider):
                collector.collect([instrument], date(2026, 9, 1), date(2026, 9, 18))
                unchanged = collector.refresh([instrument], date(2026, 9, 18), overlap_days=3)
                provider.ex_date = "2026-09-17"
                rebased = collector.refresh([instrument], date(2026, 9, 18), overlap_days=3)
            with database.connect() as connection:
                ratios = {
                    str(row["trade_date"]): Decimal(row["adjusted_close_price"]) / Decimal(row["close_price"])
                    for row in connection.execute(
                        "SELECT trade_date, close_price, adjusted_close_price FROM daily_prices"
                    )
                }

        self.assertEqual(unchanged.rebased_symbols, ())
        self.assertEqual(rebased.rebased_symbols, ("2330.TW",))
        self.assertEqual(rebased.warnings, ())
        # 增量只重抓 09-15 起；偵測到基準改變後從兩年回看起點整段重抓。
        self.assertEqual(provider.calls[-2:], ["2026-09-15", "2024-09-18"])
        self.assertEqual(ratios["2026-09-01"], Decimal("0.97"))
        self.assertEqual(ratios["2026-09-16"], Decimal("0.97"))
        self.assertEqual(ratios["2026-09-17"], Decimal("1"))

    def test_refresh_refetches_whole_series_when_yahoo_rewrites_close_for_split(self):
        import pandas

        class SplitYFinance:
            """配股後 Yahoo 把事件前的 Close 與 Adj Close 同比例縮小，Adj÷Close 不變。"""

            def __init__(self):
                self.ex_date = None
                self.calls = []

            def download(self, symbols, start, end, **_kwargs):
                self.calls.append(start)
                index = pandas.bdate_range(start, pandas.Timestamp(end) - pandas.Timedelta(days=1))
                index = index[(index >= "2026-09-01") & (index <= "2026-09-18")]
                scale = [0.5 if self.ex_date and day < pandas.Timestamp(self.ex_date) else 1.0 for day in index]
                close = [100.0 * value for value in scale]
                return pandas.DataFrame(
                    {"Open": close, "High": close, "Low": close, "Close": close,
                     "Adj Close": close, "Volume": [1000] * len(index)},
                    index=index,
                )

        provider = SplitYFinance()
        instrument = Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14")
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            collector = YFinanceHistoryCollector(database, retries=0)
            with patch("etf_agent.data.yfinance_history.importlib.import_module", return_value=provider):
                collector.collect([instrument], date(2026, 9, 1), date(2026, 9, 18))
                unchanged = collector.refresh([instrument], date(2026, 9, 18), overlap_days=3)
                provider.ex_date = "2026-09-17"
                rebased = collector.refresh([instrument], date(2026, 9, 18), overlap_days=3)
                settled = collector.refresh([instrument], date(2026, 9, 18), overlap_days=3)
            with database.connect() as connection:
                closes = {
                    str(row["trade_date"]): Decimal(row["close_price"])
                    for row in connection.execute("SELECT trade_date, close_price FROM daily_prices")
                }

        self.assertEqual(unchanged.rebased_symbols, ())
        self.assertEqual(rebased.rebased_symbols, ("2330.TW",))
        self.assertEqual(rebased.warnings, ())
        self.assertEqual(settled.rebased_symbols, ())
        # 事件前最早的列也回到新基準，序列沒有假跳空。
        self.assertEqual(closes["2026-09-01"], Decimal("50"))
        self.assertEqual(closes["2026-09-16"], Decimal("50"))
        self.assertEqual(closes["2026-09-17"], Decimal("100"))

    def test_stale_basis_detection_tolerates_rounding_only(self):
        from etf_agent.data.yfinance_history import stale_basis_symbols

        instrument = Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14")
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            database.initialize()
            with database.connect() as connection:
                database.upsert_instruments(connection, [instrument], True)
                connection.execute(
                    "INSERT INTO collection_runs(run_id, source, started_at, status) "
                    "VALUES ('r', 'YAHOO_FINANCE', '2026-09-18T00:00:00+00:00', 'success')"
                )
                payload = connection.execute(
                    "INSERT INTO raw_payloads(run_id, source, endpoint, fetched_at, sha256, payload_json) "
                    "VALUES ('r', 'YAHOO_FINANCE', 'fixture://', '2026-09-18T00:00:00+00:00', 'sha', '{}')"
                ).lastrowid
                for trade_date, adjusted in (
                    ("2026-09-15", "96.9999"), ("2026-09-16", "97"), ("2026-09-17", "96.9999"),
                ):
                    connection.execute(
                        "INSERT INTO daily_prices(symbol, trade_date, open_price, high_price, low_price, "
                        "close_price, adjusted_close_price, volume_shares, trade_value, transactions, "
                        "source, fetched_at, run_id, raw_payload_id) VALUES "
                        "('2330.TW', ?, '100', '100', '100', '100', ?, 1, 0, 0, 'YAHOO_FINANCE', "
                        "'2026-09-18T00:00:00+00:00', 'r', ?)",
                        (trade_date, adjusted, payload),
                    )
                rounding_only = stale_basis_symbols(connection, "YAHOO_FINANCE", ["2330.TW"])
                connection.execute(
                    "UPDATE daily_prices SET adjusted_close_price = '100' WHERE trade_date = '2026-09-15'"
                )
                stale = stale_basis_symbols(connection, "YAHOO_FINANCE", ["2330.TW"])
        self.assertEqual(rounding_only, [])
        self.assertEqual(stale, ["2330.TW"])

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
        self.assertIs(prices[0].ex_rights, False)

    def test_twse_month_keeps_ex_rights_marker_and_stores_it(self):
        instrument = Instrument("2379.TW", "2379", "瑞昱", "TWSE", "2026-09-01")
        payload = json.dumps({"stat": "OK", "data": [
            ["115/09/07", "1", "1", "720.00", "738.00", "706.00", "729.00", "+16.00", "1", ""],
            ["115/09/08", "1", "1", "717.00", "721.00", "680.00", "692.00", "X0.00", "1", ""],
        ]})
        prices, warnings = HistoricalPriceProvider.parse_twse(payload, instrument)
        self.assertEqual(warnings, [])
        self.assertEqual([(p.trade_date, p.change, p.ex_rights) for p in prices], [
            ("2026-09-07", Decimal("16.00"), False), ("2026-09-08", Decimal("0.00"), True),
        ])
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            database.initialize()
            with database.connect() as connection:
                database.upsert_instruments(connection, [instrument], True)
                connection.execute(
                    "INSERT INTO collection_runs(run_id, source, started_at, status) "
                    "VALUES ('r', 'TWSE_STOCK_DAY', '2026-09-08T10:00:00+00:00', 'success')"
                )
                payload_id = database.insert_raw_payload(
                    connection, "r", "TWSE_STOCK_DAY", "fixture://", "2026-09-08T10:00:00+00:00", payload
                )
                database.upsert_prices(connection, prices, "TWSE_STOCK_DAY",
                                       "2026-09-08T10:00:00+00:00", "r", payload_id)
                stored = connection.execute(
                    "SELECT trade_date, ex_rights_marker FROM daily_prices ORDER BY trade_date"
                ).fetchall()
        self.assertEqual([tuple(row) for row in stored], [("2026-09-07", 0), ("2026-09-08", 1)])

    def test_initialize_adds_ex_rights_column_to_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            database.initialize()
            with database.connect() as connection:
                connection.execute("DROP VIEW IF EXISTS analysis_daily_prices")
                connection.execute("ALTER TABLE daily_prices DROP COLUMN ex_rights_marker")
            database.initialize()
            with database.connect() as connection:
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(daily_prices)")}
        self.assertIn("ex_rights_marker", columns)

    def test_request_gate_spaces_requests_and_backs_off_then_blocks(self):
        clock = FakeClock()
        gate = RequestGate(min_interval_seconds=2, backoff_seconds=(30, 120), clock=clock, sleep=clock.sleep)
        for _ in range(3):
            epoch = gate.acquire()
        self.assertEqual(clock.sleeps, [2, 2])
        gate.throttled(epoch)
        stale = epoch
        epoch = gate.acquire()
        self.assertEqual(clock.sleeps[-1], 30)
        # 暫停前已送出的請求也被限流時，不重複累加退避。
        gate.throttled(stale)
        gate.throttled(epoch)
        epoch = gate.acquire()
        self.assertEqual(clock.sleeps[-1], 120)
        gate.success()
        gate.throttled(epoch)
        gate.acquire()
        self.assertEqual(clock.sleeps[-1], 30)
        gate.throttled(gate._epoch)
        gate.acquire()
        gate.throttled(gate._epoch)
        with self.assertRaises(SourceBlockedError):
            gate.acquire()
        self.assertEqual(gate.throttle_events, 6)

    def test_throttled_month_request_waits_and_succeeds(self):
        clock = FakeClock()
        opener = ScriptedOpener([308, 403, "ok"])
        provider = HistoricalPriceProvider(
            "https://example.com/STOCK_DAY", opener=opener,
            gate=RequestGate(min_interval_seconds=2, backoff_seconds=(30, 120, 300),
                             clock=clock, sleep=clock.sleep),
        )
        instrument = Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-01")
        with tempfile.TemporaryDirectory() as directory:
            collector = HistoricalPriceCollector(
                MarketDataDatabase(Path(directory) / "test.db"), provider, max_workers=1, retries=0
            )
            result = collector.collect([instrument], date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual(result.stored_rows, 1)
        self.assertEqual(opener.calls, 3)
        self.assertEqual(clock.sleeps, [30, 120])

    def test_persistent_throttle_stops_without_more_requests(self):
        clock = FakeClock()
        opener = ScriptedOpener([403] * 10)
        provider = HistoricalPriceProvider(
            "https://example.com/STOCK_DAY", opener=opener,
            gate=RequestGate(min_interval_seconds=2, backoff_seconds=(30, 120),
                             clock=clock, sleep=clock.sleep),
        )
        universe = [
            Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-01"),
            Instrument("2317.TW", "2317", "鴻海", "TWSE", "2026-09-01"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            collector = HistoricalPriceCollector(
                MarketDataDatabase(Path(directory) / "test.db"), provider, max_workers=1, retries=3
            )
            with self.assertRaisesRegex(HistoricalCollectionError, "限流熔斷"):
                collector.collect(universe, date(2026, 8, 1), date(2026, 9, 30))
        # 一次請求 + 兩段退避後各一次，第三次限流即熔斷；第二檔與 9 月都不再送出。
        self.assertEqual(opener.calls, 3)
        self.assertEqual(clock.sleeps, [30, 120])

    def test_official_month_provider_rejects_tpex(self):
        instrument = Instrument("5274.TWO", "5274", "信驊", "TPEX", "2026-07-31")
        provider = HistoricalPriceProvider("https://example.com/STOCK_DAY")
        with self.assertRaisesRegex(ValueError, "需設定 tpex_url"):
            provider.fetch(instrument, date(2026, 9, 1))

    def test_parses_tpex_month_and_derives_ex_rights_from_reference_price(self):
        instrument = Instrument("1785.TWO", "1785", "光洋科", "TPEX", "2026-07-31")
        payload = json.dumps({"tables": [{"data": [
            ["115/07/15", "7,845", "953,135", "123.00", "123.50", "120.00", "121.00", "1.00", "6,726"],
            ["115/07/16", "8,313", "963,347", "119.00", "119.50", "114.00", "114.00", "-5.00", "7,861"],
            ["115/07/17", "14,552", "1,563,582", "108.00", "111.00", "105.00", "107.50", "-6.50", "10,148"],
            ["115/07/20", "0", "0", "--", "--", "--", "--", "---", "0"],
        ]}]})
        prices, warnings = HistoricalPriceProvider.parse_tpex(payload, instrument)
        self.assertEqual([(p.trade_date, p.ex_rights) for p in prices], [
            # 月內第一列沒有前一日可比，標記未知而不是猜測。
            ("2026-07-15", None),
            # 參考價 114 − (−5) = 119 ≠ 前收 121：除息日。
            ("2026-07-16", True),
            # 參考價 107.5 + 6.5 = 114 = 前收：一般交易日。
            ("2026-07-17", False),
        ])
        self.assertEqual(prices[0].volume_shares, 7845000)
        self.assertEqual(prices[0].trade_value, 953135000)
        self.assertEqual(len(warnings), 1)

    def test_tpex_provider_requests_roc_free_month_url_and_tags_source(self):
        instrument = Instrument("1785.TWO", "1785", "光洋科", "TPEX", "2026-07-31")
        urls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"tables": [{"data": [
                    ["115/07/15", "1", "1", "10", "11", "9", "10", "0.00", "1"],
                ]}]}).encode("utf-8")

        def opener(request, timeout):
            urls.append(request.full_url)
            return Response()

        provider = HistoricalPriceProvider(
            "https://example.com/STOCK_DAY",
            tpex_url="https://example.com/tradingStock",
            opener=opener,
            gate=RequestGate(min_interval_seconds=0),
        )
        response = provider.fetch(instrument, date(2026, 7, 1))
        self.assertEqual(response.source, "TPEX_TRADING_STOCK")
        self.assertIn("code=1785", urls[0])
        self.assertIn("date=2026%2F07%2F01", urls[0])

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

    def test_refresh_after_long_gap_refetches_whole_history(self):
        universe = [Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14")]
        batches = plan_history_refresh(universe, {"2330.TW": date(2026, 9, 10)}, date(2026, 9, 18),
                                       lookback_years=2, overlap_days=7)
        self.assertEqual([(batch.start_date, [item.symbol for item in batch.instruments]) for batch in batches],
                         [(date(2024, 9, 18), ["2330.TW"])])

    def test_official_refresh_requires_safe_end_and_reports_coverage(self):
        universe = [
            Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            seed_latest_price(database, universe[0], "TWSE_STOCK_DAY_ALL")
            service = OfficialHistoricalRefreshService(
                database, OfficialHistoryFixtureProvider(), max_workers=1, retries=0
            )
            with self.assertRaisesRegex(ValueError, "晚於最近完整官方交易日"):
                service.refresh(universe, end=date(2026, 9, 18))

            first = service.refresh(universe, start=date(2026, 9, 1))
            self.assertEqual(first.status, "completed")
            self.assertEqual(first.exit_code, 0)
            self.assertEqual(first.safe_end_date, "2026-09-17")
            self.assertEqual([row.end_coverage for row in first.coverage], ["present"])
            self.assertEqual(
                [row.range_coverage for row in first.coverage],
                ["unverified_without_official_calendar"],
            )
            with database.connect() as connection:
                sources = connection.execute(
                    "SELECT symbol, source FROM analysis_daily_prices WHERE trade_date = '2026-09-17' ORDER BY symbol"
                ).fetchall()
            self.assertEqual(
                [(row["symbol"], row["source"]) for row in sources],
                [("2330.TW", "TWSE_STOCK_DAY")],
            )

            second = service.refresh(universe)
            self.assertEqual(second.batches[0].start_date, "2026-09-10")
            self.assertEqual(second.status, "completed")

    def test_official_refresh_reaches_day_reported_by_faster_market(self):
        """TWSE 全市場當日行情晚一天時，仍可依 TPEx 當日公告日補抓上市個股。"""
        universe = [
            Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14"),
            Instrument("3718.TWO", "3718", "中光電投控", "TPEX", "2026-09-14"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            seed_latest_price(database, universe[0], "TWSE_STOCK_DAY_ALL", "2026-09-17")
            seed_latest_price(database, universe[1], "TPEX_MAINBOARD_QUOTES", "2026-09-18")
            self.assertEqual(
                database.latest_complete_trade_date(("TWSE_STOCK_DAY_ALL", "TPEX_MAINBOARD_QUOTES")), "2026-09-17"
            )
            self.assertEqual(
                database.latest_reported_trade_date(("TWSE_STOCK_DAY_ALL", "TPEX_MAINBOARD_QUOTES")), "2026-09-18"
            )
            service = OfficialHistoricalRefreshService(
                database, OfficialHistoryFixtureProvider(), max_workers=1, retries=0
            )
            with self.assertRaisesRegex(ValueError, "只支援上市"):
                service.refresh(universe, start=date(2026, 9, 1), end=date(2026, 9, 18))
            twse_only = service.refresh([universe[0]], start=date(2026, 9, 1), end=date(2026, 9, 18))
            self.assertEqual(twse_only.safe_end_date, "2026-09-18")
            self.assertEqual(twse_only.requested_symbols, 1)
            # 抓不到指定終止日的個股不被當成完整：覆蓋報告逐檔標示 missing，且不會以更晚日期蒙混。
            self.assertEqual([row.end_coverage for row in twse_only.coverage], ["missing"])
            self.assertNotEqual(twse_only.exit_code, 0)
            with self.assertRaisesRegex(ValueError, "晚於最近完整官方交易日"):
                service.refresh([universe[0]], end=date(2026, 9, 19))

    def test_reported_trade_date_needs_at_least_one_source(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            database.initialize()
            self.assertIsNone(database.latest_reported_trade_date(("TWSE_STOCK_DAY_ALL",)))
            with self.assertRaises(ValueError):
                database.latest_reported_trade_date(())

    def test_official_refresh_preserves_failed_run_and_returns_failed_report(self):
        universe = [
            Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14"),
            Instrument("2317.TW", "2317", "鴻海", "TWSE", "2026-09-14"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            seed_latest_price(database, universe[0], "TWSE_STOCK_DAY_ALL")
            seed_latest_price(database, universe[1], "TWSE_STOCK_DAY_ALL")
            result = OfficialHistoricalRefreshService(
                database,
                OfficialHistoryFixtureProvider(failed_symbols=("2317.TW",)),
                max_workers=1,
                retries=0,
            ).refresh(universe, start=date(2026, 9, 1))
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.batches[0].status, "failed")
            self.assertEqual(
                next(item for item in result.coverage if item.symbol == "2317.TW").end_coverage,
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
