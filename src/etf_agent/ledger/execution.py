"""Whole-lot order execution with commission and sell tax.

Used by the virtual account so fills, fees and cash follow one rule set.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
from typing import Dict, Mapping, Sequence

from etf_agent.core import content_sha256

from .contracts import MONEY, LedgerError, decimal_value, money_string, parse_time


# 現金不足時的處理：省略（舊 Decision run）即逐張減少買單；void_batch 與主辦方相同，整批作廢、一張都不成交。
INSUFFICIENT_FUNDS_REDUCE_LOTS = "reduce_lots"
INSUFFICIENT_FUNDS_VOID_BATCH = "void_batch"
INSUFFICIENT_FUNDS_POLICIES = (INSUFFICIENT_FUNDS_REDUCE_LOTS, INSUFFICIENT_FUNDS_VOID_BATCH)
# 整批未成交的原因代碼：主辦方資料包判定作廢（host_voided）或模擬現金不足整批作廢。
HOST_VOIDED = "HOST_VOIDED"
INSUFFICIENT_FUNDS_VOIDED = "INSUFFICIENT_FUNDS_VOID_BATCH"


class ExecutionSimulator:
    """Fill approved orders in full lots at explicitly unadjusted market prices.

    A quote's ``available_lots`` caps the fill; ``None`` means no liquidity cap
    (competition accounting fills every tradable order in full). Buying power
    still limits buys in every case: by default the buy is reduced lot by lot
    (legacy runs); with ``insufficient_funds_policy = "void_batch"`` the whole
    batch is voided like the competition host does.
    """

    def __init__(self, assumptions: Mapping[str, object]):
        self.assumptions = dict(assumptions)
        self.lot_size = int(assumptions["lot_size"])
        policy = self.assumptions.get("insufficient_funds_policy", INSUFFICIENT_FUNDS_REDUCE_LOTS)
        if policy not in INSUFFICIENT_FUNDS_POLICIES:
            raise LedgerError("insufficient_funds_policy 只允許 %s" % "／".join(INSUFFICIENT_FUNDS_POLICIES))
        self.insufficient_funds_policy = str(policy)

    def _check_inputs(self, decision: Mapping[str, object], market: Mapping[str, object], execution_at: str) -> Mapping[str, object]:
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
        return quotes

    @staticmethod
    def _sorted_orders(decision: Mapping[str, object]) -> list:
        return sorted(decision.get("orders", []), key=lambda item: (0 if item.get("side") == "sell" else 1, str(item.get("symbol"))))

    def void_batch(
        self,
        decision: Mapping[str, object],
        market: Mapping[str, object],
        buying_power: Decimal,
        execution_at: str,
        reason: str,
        void_reasons: Sequence[str] = (),
    ) -> Dict[str, object]:
        """整批作廢：一張都不成交，每筆訂單列入 unfilled_orders，買力原封不動。

        主辦方資料包判定前一日委託作廢（reason=HOST_VOIDED）時由帳本直接採用，不自行模擬成交。"""
        self._check_inputs(decision, market, execution_at)
        unfilled = []
        for order in self._sorted_orders(decision):
            shares = order.get("shares")
            if not isinstance(shares, int) or isinstance(shares, bool) or shares <= 0 or shares % self.lot_size:
                raise LedgerError("訂單必須為正整張")
            unfilled.append({"symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
                             "unfilled_shares": shares, "reason": reason})
        body: Dict[str, object] = {"fills": [], "unfilled_orders": unfilled, "remaining_buying_power": money_string(buying_power),
                                   "voided": {"reason": reason, "void_reasons": [str(item) for item in void_reasons]}}
        body["content_sha256"] = content_sha256(body)
        return body

    def run(
        self,
        decision: Mapping[str, object],
        market: Mapping[str, object],
        buying_power: Decimal,
        execution_at: str,
    ) -> Dict[str, object]:
        quotes = self._check_inputs(decision, market, execution_at)
        fills, unfilled = [], []
        available = buying_power
        for order in self._sorted_orders(decision):
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
            if available_lots is not None and (
                not isinstance(available_lots, int) or isinstance(available_lots, bool) or available_lots < 0
            ):
                raise LedgerError("%s.available_lots 必須是非負整數或 null（不設量能上限）" % symbol)
            requested_lots = shares // self.lot_size
            fill_lots = requested_lots if available_lots is None else min(requested_lots, available_lots)
            side = order.get("side")
            if side not in {"buy", "sell"}:
                raise LedgerError("訂單 side 不合法")
            while fill_lots:
                trial = self._fill(symbol, side, fill_lots * self.lot_size, price)
                if side == "sell" or -decimal_value(trial["net_cash_impact"], "net_cash_impact") <= available:
                    break
                if self.insufficient_funds_policy == INSUFFICIENT_FUNDS_VOID_BATCH:
                    # 與主辦方相同：任一買單湊不到現金就整批作廢（含已排在前面的賣單），不逐張減少。
                    return self.void_batch(decision, market, buying_power, execution_at, INSUFFICIENT_FUNDS_VOIDED, ["insufficient_funds"])
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

    def fill(self, symbol: str, side: str, shares: int, price: Decimal) -> Dict[str, object]:
        """單筆整張成交的費稅與現金影響；基準（買入持有）計算也用同一套公式。"""
        gross = price * Decimal(shares)
        commission = max(gross * decimal_value(self.assumptions["commission_rate"], "commission_rate"), decimal_value(self.assumptions["minimum_commission"], "minimum_commission")).quantize(MONEY, rounding=ROUND_CEILING)
        tax = (gross * decimal_value(self.assumptions["sell_tax_rate"], "sell_tax_rate") if side == "sell" else Decimal("0")).quantize(MONEY, rounding=ROUND_CEILING)
        impact = -(gross + commission) if side == "buy" else gross - commission - tax
        return {"symbol": symbol, "side": side, "lots": shares // self.lot_size, "shares": shares, "execution_price": str(price), "gross_amount": money_string(gross), "commission": money_string(commission), "tax": money_string(tax), "net_cash_impact": money_string(impact)}

    _fill = fill
