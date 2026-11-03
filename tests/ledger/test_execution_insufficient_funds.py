"""模擬成交的現金不足處理：省略即逐張減少（舊 run），insufficient_funds_policy=void_batch 整批作廢；主辦方作廢直接採用。"""

import unittest
from decimal import Decimal

from etf_agent.core import content_sha256
from etf_agent.ledger import (
    HOST_VOIDED, INSUFFICIENT_FUNDS_VOID_BATCH, INSUFFICIENT_FUNDS_VOIDED, ExecutionSimulator, LedgerError,
)


AT = "2026-09-21T13:30:00+08:00"


def assumptions(**extra):
    return {"lot_size": 1000, "commission_rate": "0.001425", "minimum_commission": "20", "sell_tax_rate": "0.003",
            "reuse_sell_proceeds": True, **extra}


def decision(orders):
    body = {"status": "approved", "orders": orders}
    body["content_sha256"] = content_sha256(body)
    return body


def market(**prices):
    return {"price_basis": "unadjusted", "available_at": AT,
            "quotes": {symbol: {"tradable": True, "available_lots": None, "execution_price": str(price)} for symbol, price in prices.items()}}


class InsufficientFundsPolicyTests(unittest.TestCase):
    def setUp(self):
        self.decision = decision([
            {"symbol": "2330.TW", "side": "sell", "shares": 1000},
            {"symbol": "2317.TW", "side": "buy", "shares": 3000},
        ])
        self.market = market(**{"2330.TW": 100, "2317.TW": 100})

    def test_default_reduces_buy_lot_by_lot_and_fills_the_sell(self):
        result = ExecutionSimulator(assumptions()).run(self.decision, self.market, Decimal("150000"), AT)
        self.assertEqual([(f["symbol"], f["shares"]) for f in result["fills"]], [("2330.TW", 1000), ("2317.TW", 2000)])
        self.assertEqual(result["unfilled_orders"][0]["reason"], "LIQUIDITY_OR_CASH")
        self.assertNotIn("voided", result)

    def test_void_batch_cancels_every_order_including_the_sell(self):
        simulator = ExecutionSimulator(assumptions(insufficient_funds_policy=INSUFFICIENT_FUNDS_VOID_BATCH))
        result = simulator.run(self.decision, self.market, Decimal("150000"), AT)
        self.assertEqual(result["fills"], [])
        self.assertEqual([(u["symbol"], u["side"], u["unfilled_shares"], u["reason"]) for u in result["unfilled_orders"]],
                         [("2330.TW", "sell", 1000, INSUFFICIENT_FUNDS_VOIDED), ("2317.TW", "buy", 3000, INSUFFICIENT_FUNDS_VOIDED)])
        self.assertEqual(result["voided"], {"reason": INSUFFICIENT_FUNDS_VOIDED, "void_reasons": ["insufficient_funds"]})
        self.assertEqual(result["remaining_buying_power"], "150000")
        self.assertEqual(result["content_sha256"], content_sha256(result))

    def test_void_batch_with_enough_cash_fills_normally(self):
        simulator = ExecutionSimulator(assumptions(insufficient_funds_policy=INSUFFICIENT_FUNDS_VOID_BATCH))
        result = simulator.run(self.decision, self.market, Decimal("1000000"), AT)
        self.assertEqual([(f["symbol"], f["shares"]) for f in result["fills"]], [("2330.TW", 1000), ("2317.TW", 3000)])
        self.assertEqual(result["unfilled_orders"], [])

    def test_sell_proceeds_count_toward_net_settlement_before_voiding(self):
        # 淨額交割：買力 150,000 ＋ 賣款 ≈ 99,000 夠買 2 張，不夠 3 張；void_batch 仍整批作廢，reduce_lots 成交 2 張。
        two_lots = decision([{"symbol": "2330.TW", "side": "sell", "shares": 1000}, {"symbol": "2317.TW", "side": "buy", "shares": 2000}])
        voided = ExecutionSimulator(assumptions(insufficient_funds_policy=INSUFFICIENT_FUNDS_VOID_BATCH)).run(two_lots, self.market, Decimal("150000"), AT)
        self.assertEqual(len(voided["fills"]), 2)

    def test_unknown_policy_fails_closed(self):
        with self.assertRaisesRegex(LedgerError, "insufficient_funds_policy"):
            ExecutionSimulator(assumptions(insufficient_funds_policy="reduce_then_void"))


class HostVoidBatchTests(unittest.TestCase):
    def test_host_voided_batch_has_no_fills_and_keeps_buying_power(self):
        simulator = ExecutionSimulator(assumptions())
        body = decision([{"symbol": "2317.TW", "side": "buy", "shares": 3000}])
        result = simulator.void_batch(body, market(**{"2317.TW": 100}), Decimal("500000"), AT, HOST_VOIDED, ["insufficient_funds", "no_price"])
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["unfilled_orders"], [{"symbol": "2317.TW", "side": "buy", "unfilled_shares": 3000, "reason": HOST_VOIDED}])
        self.assertEqual(result["voided"], {"reason": HOST_VOIDED, "void_reasons": ["insufficient_funds", "no_price"]})
        self.assertEqual(result["remaining_buying_power"], "500000")

    def test_void_batch_still_validates_decision_and_lots(self):
        simulator = ExecutionSimulator(assumptions())
        tampered = decision([{"symbol": "2317.TW", "side": "buy", "shares": 3000}])
        tampered["orders"][0]["shares"] = 4000
        with self.assertRaisesRegex(LedgerError, "content_sha256"):
            simulator.void_batch(tampered, market(**{"2317.TW": 100}), Decimal("1"), AT, HOST_VOIDED, ["oversell"])
        odd = decision([{"symbol": "2317.TW", "side": "buy", "shares": 1500}])
        with self.assertRaisesRegex(LedgerError, "正整張"):
            simulator.void_batch(odd, market(**{"2317.TW": 100}), Decimal("1"), AT, HOST_VOIDED, ["oversell"])


if __name__ == "__main__":
    unittest.main()
