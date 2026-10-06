import copy
import unittest
from decimal import Decimal

from etf_agent.core import canonical_sha256, parse_decimal
from etf_agent.decision import (
    DecisionToolError, MomentumEngine, TechnicalTrendHistoryTools,
    TechnicalTrendHistoryValidator, build_analyst_brief, decision_bundle_sha256,
)
from test_portfolio_decision_agent import decision_bundle, refresh_bundle_hashes


class TechnicalTrendHistoryTests(unittest.TestCase):
    def test_complete_path_and_rolling_means_use_only_preceding_prices(self):
        bundle = decision_bundle()
        history = TechnicalTrendHistoryTools(bundle).for_symbol("2330.TW")
        bars = bundle["price_series"][0]["bars"]
        self.assertEqual(len(history["rows"]), len(bars))
        self.assertEqual(history["rows"][0]["trade_date"], bars[0]["trade_date"])
        self.assertIsNone(history["rows"][18]["ma20"])
        self.assertEqual(history["rows"][19]["ma20"], "109.5")
        self.assertIsNone(history["rows"][58]["ma60"])
        self.assertEqual(history["rows"][59]["ma60"], "129.5")
        for index, row in enumerate(history["rows"]):
            self.assertEqual(parse_decimal(row["close"], "close"), parse_decimal(bars[index]["close"], "close"))
            if index >= 19:
                expected = sum(parse_decimal(bar["close"], "close") for bar in bars[index-19:index+1]) / Decimal(20)
                self.assertEqual(parse_decimal(row["ma20"], "ma20"), expected)
        self.assertEqual(TechnicalTrendHistoryValidator(bundle).validate(history, "2330.TW"), [])

    def test_short_history_keeps_null_means_and_is_passed_to_technical_only(self):
        bundle = decision_bundle()
        bundle["price_series"][0]["bars"] = bundle["price_series"][0]["bars"][-5:]
        refresh_bundle_hashes(bundle)
        momentum = MomentumEngine(bundle).run()
        brief = build_analyst_brief(bundle, momentum, "technical")
        row = next(row for row in brief["symbols"] if row["symbol"] == "2330.TW")
        self.assertEqual(row["momentum"]["status"], "insufficient")
        self.assertEqual(row["trend_history"]["observation_count"], 5)
        self.assertTrue(all(r["ma20"] is None and r["ma60"] is None for r in row["trend_history"]["rows"]))
        for role in ("fundamental", "event"):
            self.assertTrue(all("trend_history" not in r for r in build_analyst_brief(bundle, momentum, role)["symbols"]))

    def test_rejects_modified_paths_even_with_resealed_hash_and_foreign_symbol(self):
        bundle = decision_bundle()
        history = TechnicalTrendHistoryTools(bundle).for_symbol("2330.TW")
        for field, value in (("close", "999"), ("ma20", "999"), ("trade_date", "2099-01-01")):
            modified = copy.deepcopy(history)
            modified["rows"][-1][field] = value
            modified["content_sha256"] = canonical_sha256({k:v for k,v in modified.items() if k != "content_sha256"})
            self.assertTrue(TechnicalTrendHistoryValidator(bundle).validate(modified, "2330.TW"))
        self.assertTrue(TechnicalTrendHistoryValidator(bundle).validate(history, "2317.TW"))

    def test_rejects_future_data_timezone_missing_and_source_version_mismatch(self):
        for field, value in (("available_at", "2026-09-21T01:00:00+00:00"), ("available_at", "2026-09-19T16:00:00")):
            bundle = decision_bundle()
            bundle["price_series"][0]["bars"][-1][field] = value
            refresh_bundle_hashes(bundle)
            with self.assertRaises(DecisionToolError):
                TechnicalTrendHistoryTools(bundle)
        bundle = decision_bundle()
        bundle["price_series"][0]["bars"][-1]["close"] = "999"
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        with self.assertRaises(DecisionToolError):
            TechnicalTrendHistoryTools(bundle)
