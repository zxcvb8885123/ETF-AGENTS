import unittest
from dataclasses import replace
from pathlib import Path

from etf_agent.strategy import EventDrivenStrategy, StrategyInputError, StockResearch


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "event_strategy_v1.json"


def stock(index, *, strong_event=False, symbol=None, industry=None):
    return StockResearch(
        symbol=symbol or "%04d.TW" % (1000 + index),
        industry=industry or "industry-%d" % (index % 6),
        published_at="2026-11-09T18:00:00+08:00",
        evidence_ids=("evidence-%d" % index,),
        days_to_catalyst=-1,
        fundamental_change=0.95 if strong_event else 0.25,
        persistence=0.9 if strong_event else 0.3,
        liquidity_percentile=0.9,
        evidence_quality=1.0,
        relative_return_1d=0.02 if strong_event else -0.01,
        close_location=0.8 if strong_event else 0.45,
        volume_ratio=2.0 if strong_event else 1.0,
        downside_volatility_percentile=(index % 10) / 10,
        relative_strength_10d_percentile=1.0 - (index % 10) / 12,
        price_above_ma20=index % 3 != 0,
        market_has_reacted=True,
    )


class EventStrategyTests(unittest.TestCase):
    def setUp(self):
        self.strategy = EventDrivenStrategy(CONFIG)
        self.rows = [stock(i, strong_event=i < 10) for i in range(30)]

    def test_builds_bear_market_portfolio_with_25_positions(self):
        proposal = self.strategy.run(
            as_of="2026-11-10T08:55:00+08:00",
            breadth=0.30,
            equal_weight_above_ma20=False,
            stocks=self.rows,
        )
        self.assertEqual(proposal.regime, "bear")
        self.assertEqual(proposal.cash_weight, 0.20)
        self.assertEqual(len(proposal.positions), 25)
        self.assertAlmostEqual(sum(item.target_weight for item in proposal.positions), 0.80, places=5)
        self.assertTrue(all(item.target_weight <= 0.08 + 1e-8 for item in proposal.positions))

    def test_tsmc_is_not_mandatory_and_respects_internal_cap(self):
        without_tsmc = self.strategy.run(
            as_of="2026-11-10T08:55:00+08:00",
            breadth=0.50,
            equal_weight_above_ma20=True,
            stocks=self.rows,
        )
        self.assertNotIn("2330.TW", {item.symbol for item in without_tsmc.positions})

        rows = list(self.rows)
        rows[0] = stock(0, strong_event=True, symbol="2330.TW")
        with_tsmc = self.strategy.run(
            as_of="2026-11-10T08:55:00+08:00",
            breadth=0.70,
            equal_weight_above_ma20=True,
            stocks=rows,
        )
        tsmc = next(item for item in with_tsmc.positions if item.symbol == "2330.TW")
        self.assertLessEqual(tsmc.target_weight, 0.12)

    def test_rejects_future_information(self):
        rows = list(self.rows[:25])
        rows[0] = replace(rows[0], published_at="2026-11-10T09:00:00+08:00")
        with self.assertRaisesRegex(StrategyInputError, "只有 24 檔合格資料"):
            self.strategy.run(
                as_of="2026-11-10T08:55:00+08:00",
                breadth=0.50,
                equal_weight_above_ma20=True,
                stocks=rows,
            )

    def test_caps_unreacted_event_at_starter_weight(self):
        rows = list(self.rows)
        rows[0] = replace(rows[0], market_has_reacted=False)
        proposal = self.strategy.run(
            as_of="2026-11-10T08:55:00+08:00",
            breadth=0.70,
            equal_weight_above_ma20=True,
            stocks=rows,
        )
        starter = next(item for item in proposal.positions if item.symbol == rows[0].symbol)
        self.assertLessEqual(starter.target_weight, 0.015)
        self.assertAlmostEqual(sum(item.target_weight for item in proposal.positions), 0.95, places=5)

    def test_funds_defense_positions_without_an_event_candidate(self):
        rows = [
            replace(
                item,
                days_to_catalyst=999,
                fundamental_change=0.0,
                persistence=0.0,
                evidence_quality=0.0,
                market_has_reacted=False,
            )
            for item in self.rows
        ]
        proposal = self.strategy.run(
            as_of="2026-11-10T08:55:00+08:00",
            breadth=0.50,
            equal_weight_above_ma20=False,
            stocks=rows,
        )
        self.assertTrue(all(item.selection_role == "defense" for item in proposal.positions))
        self.assertAlmostEqual(sum(item.target_weight for item in proposal.positions), 0.88, places=5)


if __name__ == "__main__":
    unittest.main()
