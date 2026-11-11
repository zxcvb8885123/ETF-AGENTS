"""交易池依 cutoff 過濾：source_date（台北日期）晚於 cutoff 的股票不得進入較早 cutoff 的 Snapshot。"""

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.data import DailyPrice, DataAgentService, Instrument, MarketDataDatabase, SnapshotRepository
from etf_agent.data.snapshot import _universe_version

TRADE_DATE = "2026-10-21"
CUTOFF = "2026-10-21T13:00:00+00:00"  # 台北 10/21 21:00


def instrument(code, source_date):
    return Instrument("%s.TW" % code, code, "測試%s" % code, "TWSE", source_date)


def version_of(items):
    """與 SnapshotRepository.load_universe 同樣依代號排序後算 universe_version。"""
    return _universe_version([{"symbol": i.symbol, "source_date": i.source_date} for i in sorted(items, key=lambda i: i.symbol)])


def price(item, close="100"):
    return DailyPrice(
        symbol=item.symbol, code=item.code, name=item.name, trade_date=TRADE_DATE,
        open_price=Decimal(close), high_price=Decimal(close), low_price=Decimal(close), close_price=Decimal(close),
        change=None, volume_shares=1000, trade_value=1_000_000, transactions=10, adjusted_close=Decimal(close),
    )


class UniverseCutoffTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = MarketDataDatabase(Path(self.directory.name) / "test.db")
        self.database.initialize()
        self.service = DataAgentService(self.database)

    def seed(self, items, with_prices=True):
        with self.database.connect() as connection:
            self.database.upsert_instruments(connection, items, True)
            if with_prices:
                run_id, fetched = "run-1", "2026-10-21T08:00:00+00:00"
                connection.execute(
                    "INSERT INTO collection_runs(run_id, source, started_at, status) VALUES (?, ?, ?, 'success')",
                    (run_id, "TWSE_STOCK_DAY_ALL", fetched),
                )
                payload_id = self.database.insert_raw_payload(connection, run_id, "TWSE_STOCK_DAY_ALL", "fixture://x", fetched, "{}")
                self.database.store_prices(connection, [price(item) for item in items], "TWSE_STOCK_DAY_ALL", fetched, run_id, payload_id)

    def snapshot(self):
        return self.service.build_snapshot(CUTOFF, require_universe_validation=False)

    def test_all_before_cutoff_enter_snapshot(self):
        items = [instrument("2330", "2026-09-14"), instrument("2317", "2026-09-14"), instrument("2454", "2026-10-21")]
        self.seed(items)
        snapshot = self.snapshot()
        self.assertEqual(snapshot.universe_size, 3)
        self.assertEqual(snapshot.universe_version, version_of(items))

    def test_timezone_source_date_on_cutoff_day_is_included(self):
        self.seed([instrument("2330", "2026-09-14"), instrument("2317", "2026-10-21T08:50:00+08:00")])
        self.assertEqual(self.snapshot().universe_size, 2)

    def test_later_source_date_is_excluded(self):
        self.seed([instrument("2330", "2026-09-14"), instrument("2317", "2026-10-22")])
        snapshot = self.snapshot()
        self.assertEqual(snapshot.universe_size, 1)

    def test_excluded_stock_makes_old_validation_stale(self):
        full = [instrument("2330", "2026-09-14"), instrument("2317", "2026-10-22")]
        self.seed(full)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO source_feasibility_reports VALUES ('r1', '2026-10-21T00:00:00+00:00', 'passed', '{}')"
            )
            connection.execute(
                "INSERT INTO universe_validation_runs VALUES (?, ?, ?, ?, 'completed', 1, 2, 2, 0, 0, '{}')",
                ("v1", "r1", "2026-10-21T00:00:00+00:00", version_of(full)),
            )
        snapshot = self.service.build_snapshot(CUTOFF, require_universe_validation=True)
        self.assertIn("STALE_UNIVERSE_VALIDATION", snapshot.quality_flags)

    def test_matching_validation_passes(self):
        kept = [instrument("2330", "2026-09-14")]
        self.seed(kept + [instrument("2317", "2026-10-22")])
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO source_feasibility_reports VALUES ('r1', '2026-10-21T00:00:00+00:00', 'passed', '{}')"
            )
            connection.execute(
                "INSERT INTO universe_validation_runs VALUES (?, ?, ?, ?, 'completed', 1, 1, 1, 0, 0, '{}')",
                ("v1", "r1", "2026-10-21T00:00:00+00:00", version_of(kept)),
            )
        snapshot = self.service.build_snapshot(CUTOFF, require_universe_validation=True)
        self.assertNotIn("STALE_UNIVERSE_VALIDATION", snapshot.quality_flags)

    def test_all_later_is_empty_universe(self):
        self.seed([instrument("2330", "2026-10-22"), instrument("2317", "2026-11-01")], with_prices=False)
        snapshot = self.snapshot()
        self.assertEqual(snapshot.universe_size, 0)
        self.assertIn("EMPTY_COMPETITION_UNIVERSE", snapshot.quality_flags)

    def test_repository_without_cutoff_date_keeps_old_behaviour(self):
        self.seed([instrument("2330", "2026-09-14"), instrument("2317", "2026-12-31")], with_prices=False)
        with self.database.connect() as connection:
            self.assertEqual(len(SnapshotRepository.load_universe(connection)), 2)
            self.assertEqual(len(SnapshotRepository.load_universe(connection, "2026-10-21")), 1)


if __name__ == "__main__":
    unittest.main()
