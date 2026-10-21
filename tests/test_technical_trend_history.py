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

    def test_summary_keeps_endpoints_extrema_recent_and_rebuilds(self):
        bundle = decision_bundle()
        tools = TechnicalTrendHistoryTools(bundle)
        full = tools.for_symbol("2330.TW")
        summary = tools.summary_for_symbol("2330.TW")
        self.assertEqual(summary["observation_count"], len(full["rows"]))
        self.assertLess(len(summary["rows"]), len(full["rows"]))
        self.assertEqual(summary["rows"][0], full["rows"][0])
        self.assertEqual(summary["rows"][-5:], full["rows"][-5:])
        self.assertEqual(summary["source_series_sha256"], full["source_series_sha256"])
        self.assertEqual(TechnicalTrendHistoryValidator(bundle).validate_summary(summary, "2330.TW"), [])
        tampered = copy.deepcopy(summary)
        tampered["rows"][0]["close"] = "999"
        tampered["content_sha256"] = canonical_sha256({key: value for key, value in tampered.items() if key != "content_sha256"})
        self.assertTrue(TechnicalTrendHistoryValidator(bundle).validate_summary(tampered, "2330.TW"))

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


class TechnicalGateTests(unittest.TestCase):
    UP = {"status": "available", "above_ma20": True, "above_ma60": True,
          "return_5d": 0.01, "return_20d": 0.05, "return_60d": 0.1}

    def test_clear_trends_are_gated_and_everything_else_stays_mixed(self):
        from etf_agent.decision.analysts import technical_trend_gate
        self.assertEqual(technical_trend_gate(self.UP), "clear_up")
        down = {**self.UP, "above_ma20": False, "above_ma60": False,
                "return_5d": -0.01, "return_20d": -0.05, "return_60d": -0.1}
        self.assertEqual(technical_trend_gate(down), "clear_down")
        for change in (
            {"status": "insufficient"}, {"above_ma60": False}, {"return_5d": -0.01},
            {"return_60d": 0}, {"return_20d": None}, {"above_ma20": None}, {"return_5d": True},
        ):
            self.assertEqual(technical_trend_gate({**self.UP, **change}), "mixed", change)
        self.assertEqual(technical_trend_gate({}), "mixed")

    def test_gate_is_off_by_default_and_never_gates_without_clear_trend(self):
        bundle = decision_bundle()
        momentum = MomentumEngine(bundle).run()
        plain = build_analyst_brief(bundle, momentum, "technical")
        self.assertNotIn("technical_gate", plain)
        self.assertTrue(all("rows" in row["trend_history"] for row in plain["symbols"]))

    def test_gated_brief_keeps_evidence_and_full_history_for_mixed_symbols(self):
        bundle = decision_bundle()
        momentum = MomentumEngine(bundle).run()
        for item in momentum["items"]:
            if item["symbol"] == "2330.TW":
                item.update(self.UP)
            else:
                item["status"] = "insufficient"
        brief = build_analyst_brief(bundle, momentum, "technical", technical_gate=True)
        rows = {row["symbol"]: row for row in brief["symbols"]}
        gated = rows["2330.TW"]["trend_history"]
        self.assertEqual(gated["mode"], "momentum_only")
        self.assertEqual(gated["evidence_ids"], rows["2330.TW"]["momentum"]["evidence_ids"])
        self.assertNotIn("rows", gated)
        self.assertEqual(brief["technical_gate"]["momentum_only_symbols"], ["2330.TW"])
        self.assertEqual(
            brief["technical_gate"]["full_history_symbols"], len(rows) - 1,
        )
        for symbol, row in rows.items():
            if symbol != "2330.TW":
                self.assertIn("rows", row["trend_history"])
        # 其他分析師不受影響
        self.assertNotIn("technical_gate", build_analyst_brief(bundle, momentum, "fundamental", technical_gate=True))
