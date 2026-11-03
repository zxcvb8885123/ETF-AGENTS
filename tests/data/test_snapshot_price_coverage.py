"""最新交易日有檔缺行情時：快照退回前一齊全日要留下旗標，且 Yahoo 列的成交金額／筆數不以 0 冒充。"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.data import (
    DailyPrice,
    DataAgentService,
    Instrument,
    MarketDataDatabase,
    SnapshotRepository,
    SnapshotQualityPolicy,
)
from etf_agent.data.snapshot import PRICE_DATE_FALLBACK_FLAG_PREFIX
from etf_agent.decision import DecisionInputValidator, canonical_sha256, decision_bundle_sha256

from test_portfolio_decision_agent import decision_bundle


TSMC = Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14")
HON_HAI = Instrument("2317.TW", "2317", "鴻海", "TWSE", "2026-09-14")
DAY_BEFORE = "2026-10-20"
LATEST = "2026-10-21"
CUTOFF = "2026-10-21T13:00:00+00:00"  # 台北 21:00，兩日都已定案


def _price(instrument, trade_date, close, trade_value=1_000_000, transactions=10):
    return DailyPrice(
        symbol=instrument.symbol,
        code=instrument.code,
        name=instrument.name,
        trade_date=trade_date,
        open_price=Decimal(close),
        high_price=Decimal(close),
        low_price=Decimal(close),
        close_price=Decimal(close),
        change=None,
        volume_shares=1000,
        trade_value=trade_value,
        transactions=transactions,
        adjusted_close=Decimal(close),
    )


class SnapshotPriceCoverageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = MarketDataDatabase(Path(self.directory.name) / "test.db")
        self.database.initialize()
        with self.database.connect() as connection:
            self.database.upsert_instruments(connection, [TSMC, HON_HAI], True)
        self.service = DataAgentService(self.database)

    def store(self, prices, source, fetched_at):
        run_id = "run-%s-%s-%s" % (source, fetched_at, len(prices))
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO collection_runs(run_id, source, started_at, status) VALUES (?, ?, ?, 'success')",
                (run_id, source, fetched_at),
            )
            payload_id = self.database.insert_raw_payload(
                connection, run_id, source, "fixture://%s" % source, fetched_at, "{}"
            )
            self.database.store_prices(connection, prices, source, fetched_at, run_id, payload_id)

    def snapshot(self, cutoff=CUTOFF):
        return self.service.build_snapshot(cutoff, require_universe_validation=False)

    def coverage(self, cutoff=CUTOFF):
        with self.database.connect() as connection:
            return SnapshotRepository.latest_trade_date_coverage(connection, LATEST, cutoff)

    def seed_complete_day_before(self):
        self.store([_price(TSMC, DAY_BEFORE, "1000"), _price(HON_HAI, DAY_BEFORE, "200")],
                   "TWSE_STOCK_DAY_ALL", "2026-10-20T08:00:00+00:00")

    def test_missing_symbol_on_latest_day_falls_back_with_flag_but_stays_usable(self):
        self.seed_complete_day_before()
        self.store([_price(TSMC, LATEST, "1010")], "TWSE_STOCK_DAY_ALL", "2026-10-21T08:00:00+00:00")

        coverage = self.coverage()
        self.assertEqual(coverage, {"trade_date": DAY_BEFORE, "latest_any_date": LATEST, "missing_symbols": ["2317.TW"]})
        with self.database.connect() as connection:
            self.assertEqual(SnapshotRepository.find_latest_trade_date(connection, LATEST, CUTOFF), DAY_BEFORE)

        snapshot = self.snapshot()
        self.assertEqual(snapshot.latest_trade_date, DAY_BEFORE)
        self.assertEqual(snapshot.latest_price_symbols, 2)
        self.assertTrue(snapshot.usable)
        self.assertEqual(snapshot.quality_flags, ["%s%s_MISSING_2317.TW" % (PRICE_DATE_FALLBACK_FLAG_PREFIX, LATEST)])
        self.assertEqual(snapshot.as_dict()["quality_flags"], snapshot.quality_flags)

    def test_complete_latest_day_has_no_fallback_flag(self):
        self.seed_complete_day_before()
        self.store([_price(TSMC, LATEST, "1010"), _price(HON_HAI, LATEST, "201")],
                   "TWSE_STOCK_DAY_ALL", "2026-10-21T08:00:00+00:00")
        self.assertEqual(self.coverage(), {"trade_date": LATEST, "latest_any_date": LATEST, "missing_symbols": []})
        snapshot = self.snapshot()
        self.assertEqual(snapshot.latest_trade_date, LATEST)
        self.assertEqual(snapshot.quality_flags, [])
        self.assertTrue(snapshot.usable)

    def test_rows_fetched_after_cutoff_do_not_count_as_coverage(self):
        self.seed_complete_day_before()
        self.store([_price(TSMC, LATEST, "1010")], "TWSE_STOCK_DAY_ALL", "2026-10-21T08:00:00+00:00")
        # 鴻海的最新日行情在 cutoff 之後才抓到：此時仍退回前一日，缺檔旗標照舊。
        self.store([_price(HON_HAI, LATEST, "201")], "TWSE_STOCK_DAY_ALL", "2026-10-21T14:00:00+00:00")
        self.assertEqual(self.coverage()["missing_symbols"], ["2317.TW"])
        self.assertEqual(self.snapshot().latest_trade_date, DAY_BEFORE)
        # cutoff 往後移到抓取之後，最新日齊全。
        later = "2026-10-21T15:00:00+00:00"
        self.assertEqual(self.coverage(later)["trade_date"], LATEST)

    def test_no_complete_day_keeps_original_incomplete_coverage_behaviour(self):
        self.store([_price(TSMC, LATEST, "1010")], "TWSE_STOCK_DAY_ALL", "2026-10-21T08:00:00+00:00")
        self.assertEqual(self.coverage(), {"trade_date": LATEST, "latest_any_date": LATEST, "missing_symbols": []})
        snapshot = self.snapshot()
        self.assertFalse(snapshot.usable)
        self.assertEqual(snapshot.quality_flags, ["INCOMPLETE_PRICE_COVERAGE_1_OF_2"])

    def test_yahoo_rows_report_turnover_and_transactions_as_none(self):
        self.store([_price(TSMC, LATEST, "1010", trade_value=1_010_000, transactions=7)],
                   "TWSE_STOCK_DAY_ALL", "2026-10-21T08:00:00+00:00")
        self.store([_price(HON_HAI, LATEST, "201", trade_value=0, transactions=0)],
                   "YAHOO_FINANCE", "2026-10-21T08:00:00+00:00")
        prices = {item.symbol: item for item in self.snapshot().latest_prices}
        self.assertEqual((prices["2330.TW"].trade_value, prices["2330.TW"].transactions), (1_010_000, 7))
        self.assertEqual(prices["2317.TW"].source, "YAHOO_FINANCE")
        self.assertIsNone(prices["2317.TW"].trade_value)
        self.assertIsNone(prices["2317.TW"].transactions)
        self.assertIsNone(prices["2317.TW"].as_dict()["trade_value"])
        # 成交量仍是 Yahoo 真實欄位，照常保留。
        self.assertEqual(prices["2317.TW"].volume_shares, 1000)


class QualityPolicyFlagTests(unittest.TestCase):
    """為何不改成 MAX(trade_date)＋逐檔 MISSING_PRICE：少一檔就會讓整份快照與決策輸入 fail-closed。"""

    def setUp(self):
        self.policy = SnapshotQualityPolicy()

    def evaluate(self, **overrides):
        base = dict(universe_size=2, universe_version="v", validation=None, latest_trade_date="2026-10-21",
                    latest_price_symbols=2, require_prices=True, require_universe_validation=False)
        base.update(overrides)
        return self.policy.evaluate(**base)

    def test_fallback_flag_is_informational_and_lists_missing_symbols(self):
        flags = self.evaluate(latest_trade_date="2026-10-20", latest_any_trade_date="2026-10-21",
                              missing_symbols=["2330.TW", "2317.TW"])
        self.assertEqual(flags, ["PRICE_DATE_FALLBACK_2026-10-21_MISSING_2317.TW,2330.TW"])
        self.assertEqual(self.policy.blocking_flags(flags), [])
        self.assertTrue(all(self.policy.is_informational(flag) for flag in flags))

    def test_fallback_flag_not_emitted_without_newer_partial_day(self):
        self.assertEqual(self.evaluate(latest_any_trade_date="2026-10-21"), [])
        self.assertEqual(self.evaluate(latest_any_trade_date=None, missing_symbols=["2317.TW"]), [])
        self.assertEqual(self.evaluate(latest_trade_date=None, latest_price_symbols=0, latest_any_trade_date="2026-10-21"),
                         ["MISSING_PRICE_DATA"])

    def test_long_missing_list_is_truncated_deterministically(self):
        symbols = ["%04d.TW" % index for index in range(25)]
        flag = self.policy.price_date_fallback_flag("2026-10-21", reversed(symbols))
        self.assertTrue(flag.endswith("_AND_5_MORE"))
        self.assertIn(",".join(symbols[:20]), flag)

    def test_single_missing_symbol_on_latest_day_would_block_the_whole_snapshot(self):
        # 若改以最新日為準，少一檔就是 INCOMPLETE_PRICE_COVERAGE：阻擋性旗標，快照不可用。
        flags = self.evaluate(latest_price_symbols=1)
        self.assertEqual(flags, ["INCOMPLETE_PRICE_COVERAGE_1_OF_2"])
        self.assertEqual(self.policy.blocking_flags(flags), flags)

    def test_decision_input_rejects_snapshot_missing_one_symbol_price(self):
        # 決策輸入驗證要求 universe_size 等於最新行情檔數：單檔缺價會讓整份決策輸入被拒，不是只擋那一檔。
        bundle = decision_bundle()
        snapshot = bundle["snapshot"]
        snapshot["latest_prices"] = [row for row in snapshot["latest_prices"] if row["symbol"] != "2317.TW"]
        bundle["price_series"] = [row for row in bundle["price_series"] if row["symbol"] != "2317.TW"]
        bundle["snapshot_sha256"] = canonical_sha256(snapshot)
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        errors = DecisionInputValidator(bundle).validate()
        self.assertTrue(any("universe_size" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
