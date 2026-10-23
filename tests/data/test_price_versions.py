"""Bug B：daily_prices 是 append-only 版本表，依 cutoff 重放要拿到當時的版本。"""

import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from etf_agent.data import (
    DailyPrice,
    Instrument,
    MarketDataDatabase,
    SnapshotRepository,
)
from etf_agent.data.database import DAILY_PRICE_VERSIONS_SCHEMA_VERSION
from etf_agent.data.price_freshness import prices_current
from etf_agent.data.yfinance_history import stale_basis_symbols

INSTRUMENT = Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14")
TRADE_DATE = "2026-10-21"
FIRST_FETCH = "2026-10-21T07:30:00+00:00"  # 台北 15:30
SECOND_FETCH = "2026-10-22T09:30:00+00:00"  # 隔日重抓，內容已被來源修正

LEGACY_DAILY_PRICES_SQL = """
CREATE TABLE daily_prices (
    symbol TEXT NOT NULL REFERENCES instruments(symbol),
    trade_date TEXT NOT NULL,
    open_price TEXT,
    high_price TEXT,
    low_price TEXT,
    close_price TEXT NOT NULL,
    adjusted_close_price TEXT,
    price_change TEXT,
    volume_shares INTEGER NOT NULL,
    trade_value INTEGER NOT NULL,
    transactions INTEGER NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payloads(id),
    ex_rights_marker INTEGER,
    PRIMARY KEY (symbol, trade_date, source)
)
"""


def _price(close, volume=1000, trade_date=TRADE_DATE, adjusted=None, ex_rights=None):
    return DailyPrice(
        symbol=INSTRUMENT.symbol,
        code=INSTRUMENT.code,
        name=INSTRUMENT.name,
        trade_date=trade_date,
        open_price=Decimal("100"),
        high_price=Decimal("101"),
        low_price=Decimal("99"),
        close_price=Decimal(close),
        change=None,
        volume_shares=volume,
        trade_value=0,
        transactions=0,
        adjusted_close=Decimal(adjusted) if adjusted is not None else None,
        ex_rights=ex_rights,
    )


class _DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.db"
        self.database = MarketDataDatabase(self.path)
        self.database.initialize()
        with self.database.connect() as connection:
            self.database.upsert_instruments(connection, [INSTRUMENT], True)

    def store(self, prices, source, fetched_at):
        run_id = "run-%s-%s" % (source, fetched_at)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO collection_runs(run_id, source, started_at, status) "
                "VALUES (?, ?, ?, 'success')",
                (run_id, source, fetched_at),
            )
            payload_id = self.database.insert_raw_payload(
                connection, run_id, source, "fixture://%s" % source, fetched_at, "{}"
            )
            stored = self.database.store_prices(
                connection, prices, source, fetched_at, run_id, payload_id
            )
        return payload_id, stored

    def rows(self, sql="SELECT * FROM daily_prices ORDER BY trade_date, fetched_at"):
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(sql).fetchall()]


class StorePricesTests(_DatabaseCase):
    def test_identical_content_does_not_add_a_version(self):
        first_payload, first_stored = self.store([_price("2420")], "TWSE_STOCK_DAY_ALL", FIRST_FETCH)
        _second_payload, second_stored = self.store([_price("2420")], "TWSE_STOCK_DAY_ALL", SECOND_FETCH)
        rows = self.rows()
        self.assertEqual((first_stored, second_stored), (1, 1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["raw_payload_id"], first_payload)
        self.assertEqual(rows[0]["fetched_at"], FIRST_FETCH)

    def test_changed_content_appends_a_version_and_cutoff_selects_it(self):
        first_payload, _ = self.store([_price("2420")], "TWSE_STOCK_DAY_ALL", FIRST_FETCH)
        second_payload, _ = self.store([_price("2425")], "TWSE_STOCK_DAY_ALL", SECOND_FETCH)
        self.assertEqual([row["close_price"] for row in self.rows()], ["2420", "2425"])

        with self.database.connect() as connection:
            old = SnapshotRepository.load_prices(connection, TRADE_DATE, "2026-10-21T10:00:00+00:00")
            new = SnapshotRepository.load_prices(connection, TRADE_DATE, "2026-10-22T10:00:00+00:00")
            old_history = SnapshotRepository.load_price_history(
                connection, "2026-10-22", "2026-10-21T10:00:00+00:00", 3
            )
            new_history = SnapshotRepository.load_price_history(
                connection, "2026-10-22", "2026-10-22T10:00:00+00:00", 3
            )
            view = connection.execute(
                "SELECT close_price, fetched_at FROM analysis_daily_prices"
            ).fetchall()
        self.assertEqual((old[0]["close_price"], old[0]["raw_payload_id"]), ("2420", first_payload))
        self.assertEqual((old[0]["execution_close_price"], old[0]["execution_raw_payload_id"]), ("2420", first_payload))
        self.assertEqual((new[0]["close_price"], new[0]["raw_payload_id"]), ("2425", second_payload))
        self.assertEqual((new[0]["execution_close_price"], new[0]["execution_raw_payload_id"]), ("2425", second_payload))
        self.assertEqual([row["close_price"] for row in old_history], ["2420"])
        self.assertEqual([row["close_price"] for row in new_history], ["2425"])
        self.assertEqual([tuple(row) for row in view], [("2425", SECOND_FETCH)])

    def test_history_rebase_uses_latest_versions_before_cutoff(self):
        # 09-22 只有官方價；缺口前後的 Yahoo 還原係數先後兩個版本（0.95 → 0.90），
        # 係數依 cutoff 當時可見的 Yahoo 版本，舊 cutoff 重放不得拿到新版本。
        self.store([_price("100", trade_date="2026-09-21")], "TWSE_STOCK_DAY", "2026-09-21T08:00:00+00:00")
        self.store([_price("102", trade_date="2026-09-22")], "TWSE_STOCK_DAY", "2026-09-22T08:00:00+00:00")
        self.store([_price("104", trade_date="2026-09-23")], "TWSE_STOCK_DAY", "2026-09-23T08:00:00+00:00")
        self.store(
            [_price("100", trade_date="2026-09-21", adjusted="95"), _price("104", trade_date="2026-09-23", adjusted="98.8")],
            "YAHOO_FINANCE", "2026-09-23T08:00:00+00:00",
        )
        self.store(
            [_price("100", trade_date="2026-09-21", adjusted="90"), _price("104", trade_date="2026-09-23", adjusted="93.6")],
            "YAHOO_FINANCE", "2026-09-24T08:00:00+00:00",
        )
        with self.database.connect() as connection:
            before = SnapshotRepository.load_price_history(connection, "2026-09-24", "2026-09-23T09:00:00+00:00", 5)
            after = SnapshotRepository.load_price_history(connection, "2026-09-24", "2026-09-24T09:00:00+00:00", 5)
        self.assertEqual([(row["trade_date"], row["adjusted_close_price"]) for row in before],
                         [("2026-09-21", "95"), ("2026-09-22", "96.9000"), ("2026-09-23", "98.8")])
        self.assertEqual([(row["trade_date"], row["adjusted_close_price"]) for row in after],
                         [("2026-09-21", "90"), ("2026-09-22", "91.8000"), ("2026-09-23", "93.6")])

    def test_out_of_order_or_conflicting_versions_are_rejected(self):
        self.store([_price("2420")], "TWSE_STOCK_DAY_ALL", SECOND_FETCH)
        with self.assertRaises(ValueError):
            self.store([_price("2425")], "TWSE_STOCK_DAY_ALL", FIRST_FETCH)
        with self.assertRaises(ValueError):
            self.store([_price("2425")], "TWSE_STOCK_DAY_ALL", SECOND_FETCH)
        self.assertEqual([row["close_price"] for row in self.rows()], ["2420"])

    def test_invalid_fetch_times_are_rejected_without_writing(self):
        for fetched_at in ("2026-10-21T15:30:00", "2099-01-01T00:00:00+00:00", "2026-10-20T15:30:00+00:00"):
            with self.subTest(fetched_at=fetched_at):
                with self.assertRaises(ValueError):
                    self.store([_price("2420")], "TWSE_STOCK_DAY_ALL", fetched_at)
        self.assertEqual(self.rows(), [])

    def test_fetch_time_is_normalized_to_utc(self):
        self.store([_price("2420")], "TWSE_STOCK_DAY_ALL", "2026-10-21T15:30:00+08:00")
        self.assertEqual(self.rows()[0]["fetched_at"], FIRST_FETCH)

    def test_history_price_ranges_count_trading_days_not_versions(self):
        self.store([_price("2420")], "TWSE_STOCK_DAY", FIRST_FETCH)
        self.store([_price("2425")], "TWSE_STOCK_DAY", SECOND_FETCH)
        ranges = self.database.history_price_ranges(("TWSE_STOCK_DAY",))
        self.assertEqual(ranges[("2330.TW", "TWSE_STOCK_DAY")], (TRADE_DATE, TRADE_DATE, 1))

    def test_stale_basis_detection_only_reads_latest_versions(self):
        self.store(
            [_price("100", trade_date="2026-09-15", adjusted="100"), _price("100", trade_date="2026-09-16", adjusted="97")],
            "YAHOO_FINANCE", "2026-09-16T08:00:00+00:00",
        )
        self.store(
            [_price("100", trade_date="2026-09-15", adjusted="97"), _price("100", trade_date="2026-09-16", adjusted="100")],
            "YAHOO_FINANCE", "2026-09-17T08:00:00+00:00",
        )
        with self.database.connect() as connection:
            self.assertEqual(stale_basis_symbols(connection, "YAHOO_FINANCE", ["2330.TW"]), [])

    def test_prices_current_looks_at_the_latest_version(self):
        self.store([_price("2410", volume=100)], "YAHOO_FINANCE", "2026-10-21T05:04:00+00:00")
        self.store([_price("2420", volume=200)], "YAHOO_FINANCE", FIRST_FETCH)
        with self.database.connect() as connection:
            current, _reason = prices_current(connection, ["2330.TW"], "yahoo", date(2026, 10, 21))
        self.assertTrue(current)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "legacy.db"
        self.database = MarketDataDatabase(self.path)
        self.database.initialize()
        with self.database.connect() as connection:
            self.database.upsert_instruments(connection, [INSTRUMENT], True)
            connection.execute(
                "INSERT INTO collection_runs(run_id, source, started_at, status) VALUES ('r', 'TWSE_STOCK_DAY_ALL', ?, 'success')",
                (FIRST_FETCH,),
            )
            self.payload_id = self.database.insert_raw_payload(
                connection, "r", "TWSE_STOCK_DAY_ALL", "fixture://", FIRST_FETCH, "{}"
            )
            # 還原成舊結構：每檔每日每來源一列、主鍵不含 fetched_at。
            connection.execute("DROP VIEW IF EXISTS analysis_daily_prices")
            connection.execute("DROP TABLE daily_prices")
            connection.execute(LEGACY_DAILY_PRICES_SQL)
            connection.execute(
                "DELETE FROM schema_versions WHERE version = ?",
                (DAILY_PRICE_VERSIONS_SCHEMA_VERSION,),
            )

    def legacy_insert(self, trade_date, fetched_at, close="2420"):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO daily_prices(symbol, trade_date, close_price, volume_shares, trade_value, "
                "transactions, source, fetched_at, run_id, raw_payload_id, ex_rights_marker) "
                "VALUES ('2330.TW', ?, ?, 1, 0, 0, 'TWSE_STOCK_DAY_ALL', ?, 'r', ?, 1)",
                (trade_date, close, fetched_at, self.payload_id),
            )

    def pk_columns(self):
        with self.database.connect() as connection:
            return [
                row["name"]
                for row in sorted(
                    connection.execute("PRAGMA table_info(daily_prices)").fetchall(),
                    key=lambda item: item["pk"],
                )
                if row["pk"]
            ]

    def test_migration_keeps_rows_normalizes_time_and_is_idempotent(self):
        self.legacy_insert("2026-10-20", "2026-10-20T15:30:00+08:00")
        self.legacy_insert("2026-10-21", FIRST_FETCH)
        self.assertEqual(self.pk_columns(), ["symbol", "trade_date", "source"])

        self.database.initialize()
        self.assertEqual(self.pk_columns(), ["symbol", "trade_date", "source", "fetched_at"])
        with self.database.connect() as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM daily_prices ORDER BY trade_date")]
            versions = {int(row[0]) for row in connection.execute("SELECT version FROM schema_versions")}
            view_count = connection.execute("SELECT COUNT(*) FROM analysis_daily_prices").fetchone()[0]
            second_run = self.database.migrate_daily_price_versions(connection)
        self.assertEqual([row["fetched_at"] for row in rows], ["2026-10-20T07:30:00+00:00", FIRST_FETCH])
        self.assertEqual([row["ex_rights_marker"] for row in rows], [1, 1])
        self.assertEqual([row["raw_payload_id"] for row in rows], [self.payload_id, self.payload_id])
        self.assertIn(DAILY_PRICE_VERSIONS_SCHEMA_VERSION, versions)
        self.assertEqual(view_count, 2)
        self.assertEqual(second_run, 0)

        # 再跑一次 initialize 不改變任何列。
        self.database.initialize()
        self.assertEqual(self.pk_columns(), ["symbol", "trade_date", "source", "fetched_at"])
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0], 2)

    def test_migrated_table_accepts_new_versions(self):
        self.legacy_insert("2026-10-21", FIRST_FETCH)
        self.database.initialize()
        with self.database.connect() as connection:
            self.database.store_prices(
                connection, [_price("2425")], "TWSE_STOCK_DAY_ALL", SECOND_FETCH, "r", self.payload_id
            )
            closes = [row[0] for row in connection.execute("SELECT close_price FROM daily_prices ORDER BY fetched_at")]
        self.assertEqual(closes, ["2420", "2425"])

    def test_naive_legacy_fetch_time_aborts_and_rolls_back(self):
        self.legacy_insert("2026-10-21", "2026-10-21T15:30:00")
        with self.assertRaises(ValueError):
            self.database.initialize()
        self.assertEqual(self.pk_columns(), ["symbol", "trade_date", "source"])
        with sqlite3.connect(str(self.path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
            leftovers = connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'daily_prices_versioned'"
            ).fetchall()
        self.assertEqual(count, 1)
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
