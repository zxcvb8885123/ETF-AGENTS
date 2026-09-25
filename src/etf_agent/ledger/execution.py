"""Whole-lot order execution with commission and sell tax.

Shared by historical backtests and the forward virtual account so both use
the same fill, fee and cash rules.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
from typing import Dict, Mapping

from etf_agent.core import content_sha256

from .contracts import MONEY, LedgerError, decimal_value, money_string, parse_time


class ExecutionSimulator:
    """Fill approved orders in full lots at explicitly unadjusted market prices."""

    def __init__(self, assumptions: Mapping[str, object]):
        self.assumptions = dict(assumptions)
        self.lot_size = int(assumptions["lot_size"])

    def run(
        self,
        decision: Mapping[str, object],
        market: Mapping[str, object],
        buying_power: Decimal,
        execution_at: str,
    ) -> Dict[str, object]:
        if decision.get("content_sha256") != content_sha256(decision):
            raise LedgerError("DecisionResult.content_sha256 與內容不一致")
        if decision.get("status") not in {"approved", "no_trade", "rejected"}:
            raise LedgerError("DecisionResult.status 不合法")
        if decision.get("status") != "approved" and decision.get("orders"):
            raise LedgerError("非 approved DecisionResult 不得含訂單")
        if market.get("price_basis") != "unadjusted":
            raise LedgerError("ExecutionMarketData 必須使用未還原價格")
        if not isinstance(market.get("available_at"), str):
            raise LedgerError("ExecutionMarketData 缺少 available_at")
        if parse_time(market["available_at"], "ExecutionMarketData.available_at") > parse_time(execution_at, "execution_at"):
            raise LedgerError("ExecutionMarketData.available_at 晚於 execution_at")
        quotes = market.get("quotes")
        if not isinstance(quotes, Mapping):
            raise LedgerError("ExecutionMarketData.quotes 必須是物件")
        fills, unfilled = [], []
        available = buying_power
        for order in sorted(decision.get("orders", []), key=lambda item: (0 if item.get("side") == "sell" else 1, str(item.get("symbol")))):
            symbol = str(order.get("symbol", "")).upper()
            shares = order.get("shares")
            if not isinstance(shares, int) or isinstance(shares, bool) or shares <= 0 or shares % self.lot_size:
                raise LedgerError("訂單必須為正整張")
            quote = quotes.get(symbol)
            if not isinstance(quote, Mapping) or quote.get("tradable") is not True:
                unfilled.append({"symbol": symbol, "side": order.get("side"), "unfilled_shares": shares, "reason": "NOT_TRADABLE"})
                continue
            price = decimal_value(quote.get("execution_price"), "%s.execution_price" % symbol)
            available_lots = quote.get("available_lots")
            if not isinstance(available_lots, int) or isinstance(available_lots, bool) or available_lots < 0:
                raise LedgerError("%s.available_lots 必須是非負整數" % symbol)
            requested_lots = shares // self.lot_size
            fill_lots = min(requested_lots, available_lots)
            side = order.get("side")
            if side not in {"buy", "sell"}:
                raise LedgerError("訂單 side 不合法")
            while fill_lots:
                trial = self._fill(symbol, side, fill_lots * self.lot_size, price)
                if side == "sell" or -decimal_value(trial["net_cash_impact"], "net_cash_impact") <= available:
                    break
                fill_lots -= 1
            if fill_lots:
                fill = self._fill(symbol, side, fill_lots * self.lot_size, price)
                fills.append(fill)
                if side == "buy":
                    available += decimal_value(fill["net_cash_impact"], "net_cash_impact")
                elif self.assumptions["reuse_sell_proceeds"]:
                    available += decimal_value(fill["net_cash_impact"], "net_cash_impact")
            unfilled_lots = requested_lots - fill_lots
            if unfilled_lots:
                unfilled.append({"symbol": symbol, "side": side, "unfilled_shares": unfilled_lots * self.lot_size, "reason": "LIQUIDITY_OR_CASH"})
        body = {"fills": fills, "unfilled_orders": unfilled, "remaining_buying_power": money_string(available)}
        body["content_sha256"] = content_sha256(body)
        return body

    def _fill(self, symbol: str, side: str, shares: int, price: Decimal) -> Dict[str, object]:
        gross = price * Decimal(shares)
        commission = max(gross * decimal_value(self.assumptions["commission_rate"], "commission_rate"), decimal_value(self.assumptions["minimum_commission"], "minimum_commission")).quantize(MONEY, rounding=ROUND_CEILING)
        tax = (gross * decimal_value(self.assumptions["sell_tax_rate"], "sell_tax_rate") if side == "sell" else Decimal("0")).quantize(MONEY, rounding=ROUND_CEILING)
        impact = -(gross + commission) if side == "buy" else gross - commission - tax
        return {"symbol": symbol, "side": side, "lots": shares // self.lot_size, "shares": shares, "execution_price": str(price), "gross_amount": money_string(gross), "commission": money_string(commission), "tax": money_string(tax), "net_cash_impact": money_string(impact)}
