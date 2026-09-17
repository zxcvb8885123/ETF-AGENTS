import json
import unittest
from datetime import date

from etf_agent.data import (
    HistoricalPriceProvider,
    Instrument,
    month_starts,
    plan_history_refresh,
)


class HistoricalProviderTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
