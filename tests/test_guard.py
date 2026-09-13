import unittest
from pathlib import Path

from etf_agent import CompetitionGuard, Portfolio, Position


ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "config" / "competition_rules.json"
SYMBOLS = ["%04d.TW" % number for number in range(1, 31)]
SYMBOLS[0] = "2330.TW"


def valid_portfolio(cash=20.0):
    positions = tuple(Position(symbol, 1, 2.6) for symbol in SYMBOLS)
    return Portfolio(positions=positions, cash=cash)


class CompetitionGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = CompetitionGuard(RULES, SYMBOLS)
        self.benchmark = {"2330.TW": 0.05}

    def test_accepts_portfolio_within_hard_limits(self):
        result = self.guard.validate(valid_portfolio(), self.benchmark)
        self.assertTrue(result.passed, result.errors)
        self.assertGreaterEqual(result.active_share, 0.2)

    def test_rejects_cash_at_or_above_25_percent(self):
        result = self.guard.validate(valid_portfolio(cash=30.0), self.benchmark)
        self.assertFalse(result.passed)
        self.assertIn("現金部位必須低於 NAV 的 25%", result.errors)

    def test_rejects_non_universe_symbol(self):
        portfolio = Portfolio(
            positions=valid_portfolio().positions[:-1] + (Position("9999.TW", 1, 2.6),),
            cash=20.0,
        )
        result = self.guard.validate(portfolio, self.benchmark)
        self.assertFalse(result.passed)
        self.assertTrue(any("9999.TW" in error for error in result.errors))

    def test_applies_weight_limit_to_lowercase_symbol(self):
        positions = (Position("0001.tw", 10, 10.0),) + tuple(
            Position(symbol, 1, 1.0) for symbol in SYMBOLS[1:20]
        )
        guard = CompetitionGuard(RULES, ["0001.TW"] + SYMBOLS[1:20])
        result = guard.validate(Portfolio(positions=positions, cash=0.0), self.benchmark)
        self.assertFalse(result.passed)
        self.assertTrue(any("0001.tw 權重" in error for error in result.errors))

    def test_active_share_formula(self):
        self.assertAlmostEqual(
            CompetitionGuard.calculate_active_share({"A": 0.6}, {"A": 0.4, "B": 0.2}),
            0.2,
        )


if __name__ == "__main__":
    unittest.main()
