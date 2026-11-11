"""liquidity_fit v1（版本閘門）：Policy 驗證與配置引擎的買單流動性縮減。

沒有該欄位的舊 Policy 逐位元維持原行為；縮減只動買單、向下取整張、不足一張取消。
"""

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from etf_agent.decision import AllocationOrderEngine
from etf_agent.decision.liquidity_fit import liquidity_fit_gate, liquidity_fit_limit

from test_agent_swap_policy import errors_for, sizing_with


def liquidity_errors(sizing):
    return [e for e in errors_for(sizing) if "liquidity_fit" in e]


def with_gate(**changes):
    sizing = sizing_with("v2")
    sizing["liquidity_fit"] = dict({"version": "v1", "max_participation": "0.10"}, **changes)
    return sizing


class GateTests(unittest.TestCase):
    def test_valid_gate_and_absent_gate(self):
        self.assertEqual(liquidity_errors(with_gate()), [])
        self.assertEqual(liquidity_fit_limit(with_gate()), Decimal("0.10"))
        self.assertIsNone(liquidity_fit_gate(sizing_with("v2")))
        self.assertIsNone(liquidity_fit_limit(sizing_with("v2")))

    def test_bad_values_are_rejected(self):
        for label, changes in {
            "版本值域外": {"version": "v2"}, "0": {"max_participation": "0"}, "大於 1": {"max_participation": "1.5"},
            "非字串": {"max_participation": 0.1}, "布林": {"max_participation": True}, "非數字": {"max_participation": "abc"},
        }.items():
            self.assertTrue(liquidity_errors(with_gate(**changes)), label)

    def test_number_is_required_and_unknown_fields_rejected(self):
        sizing = with_gate()
        del sizing["liquidity_fit"]["max_participation"]
        self.assertTrue(liquidity_errors(sizing))
        sizing = with_gate()
        sizing["liquidity_fit"]["extra"] = 1
        self.assertTrue(liquidity_errors(sizing))
        sizing = with_gate()
        sizing["liquidity_fit"] = "v1"
        self.assertTrue(liquidity_errors(sizing))


def fake_order(symbol, side, shares, intent, effective):
    gross = Decimal(shares) * Decimal("10")
    return {"symbol": symbol, "side": side, "shares": shares, "intent": intent, "gross_amount": str(gross),
            "net_cash_impact": str(-gross if side == "buy" else gross)}


class FitLiquidityTests(unittest.TestCase):
    def engine(self, sizing):
        engine = AllocationOrderEngine.__new__(AllocationOrderEngine)
        engine.policy = {"position_sizing": sizing}
        engine.context = SimpleNamespace(bundle={})
        engine._order = fake_order
        return engine

    def run_fit(self, sizing, orders, volumes):
        items = [{"symbol": s, "average_volume_20d": v} for s, v in volumes.items()]
        with mock.patch("etf_agent.decision.allocation.MomentumEngine") as momentum:
            momentum.return_value.run.return_value = {"items": items}
            return self.engine(sizing)._fit_liquidity(orders, Decimal("0"), {"lot_size": 1000})

    def test_without_the_gate_orders_are_returned_unchanged(self):
        orders = [fake_order("A.TW", "buy", 900000, "buy", {})]
        fitted, cash, flags = self.run_fit({}, orders, {"A.TW": "100000"})
        self.assertIs(fitted, orders)
        self.assertEqual((cash, flags), (Decimal("0"), []))

    def test_oversized_buy_is_shrunk_to_whole_lots_and_cash_is_returned(self):
        orders = [fake_order("A.TW", "buy", 900000, "buy", {}), fake_order("B.TW", "sell", 900000, "exit", {})]
        fitted, cash, flags = self.run_fit(with_gate(), orders, {"A.TW": "123456", "B.TW": "1000"})
        buy = next(o for o in fitted if o["side"] == "buy")
        self.assertEqual(buy["shares"], 12000)  # 0.10 × 123456 = 12345.6 → 12 張
        self.assertEqual(buy["intent"], "buy")
        self.assertEqual(next(o for o in fitted if o["side"] == "sell")["shares"], 900000)  # 賣單不動
        self.assertEqual(cash, Decimal("9000000") - Decimal("120000"))
        self.assertEqual([f["code"] for f in flags], ["LIQUIDITY_LIMIT"])

    def test_below_one_lot_cancels_the_buy(self):
        fitted, cash, flags = self.run_fit(with_gate(), [fake_order("A.TW", "buy", 5000, "buy", {})], {"A.TW": "5000"})
        self.assertEqual(fitted, [])
        self.assertEqual(cash, Decimal("50000"))
        self.assertEqual(flags[0]["code"], "LIQUIDITY_LIMIT")

    def test_buy_within_the_limit_is_untouched_and_missing_volume_is_flagged_not_cut(self):
        within = fake_order("A.TW", "buy", 10000, "buy", {})
        unknown = fake_order("B.TW", "buy", 10000, "add", {})
        fitted, cash, flags = self.run_fit(with_gate(), [within, unknown], {"A.TW": "100000", "B.TW": None})
        self.assertEqual({o["symbol"]: o["shares"] for o in fitted}, {"A.TW": 10000, "B.TW": 10000})
        self.assertEqual([f["code"] for f in flags], ["LIQUIDITY_UNAVAILABLE"])
        self.assertEqual(cash, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
