"""Point-in-time fixture provider, whole-lot execution, and daily ledger."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Dict, List, Mapping, Sequence

from etf_agent.decision.contracts import artifact_content_sha256, canonical_sha256, parse_time

from .contracts import BacktestToolError, decimal_value


MONEY = Decimal("1")


def _money(value: Decimal) -> str:
    return str(value.quantize(MONEY, rounding=ROUND_HALF_UP))


class FixturePointInTimeDataProvider:
    """Select fixture versions solely by available_at and reject future data."""

    def __init__(self, versions: Sequence[Mapping[str, object]], require_provenance: bool = False):
        self.versions = [dict(item) for item in versions]
        self.require_provenance = require_provenance

    def at(self, decision_cutoff: str) -> Dict[str, object]:
        cutoff = parse_time(decision_cutoff, "decision_cutoff")
        eligible = []
        for item in self.versions:
            for field in ("artifact_id", "available_at", "content_sha256", "payload"):
                if field not in item:
                    raise BacktestToolError("時間點資料缺少 %s" % field)
            if self.require_provenance:
                for field in ("evidence_id", "source", "content_as_of", "published_at", "fetched_at"):
                    if not isinstance(item.get(field), str) or not item[field].strip():
                        raise BacktestToolError("正式歷史資料缺少來源欄位：%s" % field)
                for field in ("content_as_of", "published_at", "fetched_at"):
                    parse_time(item[field], "provider." + field)
            available = parse_time(item["available_at"], "provider.available_at")
            if item["content_sha256"] != canonical_sha256(item["payload"]):
                raise BacktestToolError("時間點資料內容雜湊不一致")
            if available <= cutoff:
                eligible.append((available, item))
        if not eligible:
            raise BacktestToolError("decision_cutoff 前沒有可用資料版本")
        eligible.sort(key=lambda item: (item[0], str(item[1]["artifact_id"])))
        return deepcopy(eligible[-1][1])


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
        if decision.get("content_sha256") != artifact_content_sha256(decision):
            raise BacktestToolError("DecisionResult.content_sha256 與內容不一致")
        if decision.get("status") not in {"approved", "no_trade", "rejected"}:
            raise BacktestToolError("DecisionResult.status 不合法")
        if decision.get("status") != "approved" and decision.get("orders"):
            raise BacktestToolError("非 approved DecisionResult 不得含訂單")
        if market.get("price_basis") != "unadjusted":
            raise BacktestToolError("ExecutionMarketData 必須使用未還原價格")
        if not isinstance(market.get("available_at"), str):
            raise BacktestToolError("ExecutionMarketData 缺少 available_at")
        if parse_time(market["available_at"], "ExecutionMarketData.available_at") > parse_time(execution_at, "execution_at"):
            raise BacktestToolError("ExecutionMarketData.available_at 晚於 execution_at")
        quotes = market.get("quotes")
        if not isinstance(quotes, Mapping):
            raise BacktestToolError("ExecutionMarketData.quotes 必須是物件")
        fills, unfilled = [], []
        available = buying_power
        for order in sorted(decision.get("orders", []), key=lambda item: (0 if item.get("side") == "sell" else 1, str(item.get("symbol")))):
            symbol = str(order.get("symbol", "")).upper()
            shares = order.get("shares")
            if not isinstance(shares, int) or isinstance(shares, bool) or shares <= 0 or shares % self.lot_size:
                raise BacktestToolError("訂單必須為正整張")
            quote = quotes.get(symbol)
            if not isinstance(quote, Mapping) or quote.get("tradable") is not True:
                unfilled.append({"symbol": symbol, "side": order.get("side"), "unfilled_shares": shares, "reason": "NOT_TRADABLE"})
                continue
            price = decimal_value(quote.get("execution_price"), "%s.execution_price" % symbol)
            available_lots = quote.get("available_lots")
            if not isinstance(available_lots, int) or isinstance(available_lots, bool) or available_lots < 0:
                raise BacktestToolError("%s.available_lots 必須是非負整數" % symbol)
            requested_lots = shares // self.lot_size
            fill_lots = min(requested_lots, available_lots)
            side = order.get("side")
            if side not in {"buy", "sell"}:
                raise BacktestToolError("訂單 side 不合法")
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
        body = {"fills": fills, "unfilled_orders": unfilled, "remaining_buying_power": _money(available)}
        body["content_sha256"] = artifact_content_sha256(body)
        return body

    def _fill(self, symbol: str, side: str, shares: int, price: Decimal) -> Dict[str, object]:
        gross = price * Decimal(shares)
        commission = max(gross * decimal_value(self.assumptions["commission_rate"], "commission_rate"), decimal_value(self.assumptions["minimum_commission"], "minimum_commission")).quantize(MONEY, rounding=ROUND_CEILING)
        tax = (gross * decimal_value(self.assumptions["sell_tax_rate"], "sell_tax_rate") if side == "sell" else Decimal("0")).quantize(MONEY, rounding=ROUND_CEILING)
        impact = -(gross + commission) if side == "buy" else gross - commission - tax
        return {"symbol": symbol, "side": side, "lots": shares // self.lot_size, "shares": shares, "execution_price": str(price), "gross_amount": _money(gross), "commission": _money(commission), "tax": _money(tax), "net_cash_impact": _money(impact)}


class AccountLedger:
    """Economic trade-date cash accounting with separately settled sale proceeds."""

    def __init__(self, account: Mapping[str, object]):
        self.settled_cash = decimal_value(account["settled_cash"], "settled_cash")
        self.unsettled_cash = decimal_value(account["unsettled_cash"], "unsettled_cash")
        self.positions = {str(item["symbol"]).upper(): {"shares": int(item["shares"]), "cost_basis": decimal_value(item["cost_basis"], "cost_basis")} for item in account["positions"]}
        self.pending_settlements: List[Dict[str, object]] = []

    def buying_power(self, reuse_sell_proceeds: bool) -> Decimal:
        return self.settled_cash + (self.unsettled_cash if reuse_sell_proceeds else Decimal("0"))

    def apply_fills(
        self, execution: Mapping[str, object], settlement_date: str,
        reuse_sell_proceeds: bool = False,
    ) -> None:
        for fill in execution["fills"]:
            symbol, shares, side = fill["symbol"], int(fill["shares"]), fill["side"]
            impact = decimal_value(fill["net_cash_impact"], "net_cash_impact")
            if side == "buy":
                available = self.settled_cash + (self.unsettled_cash if reuse_sell_proceeds else Decimal("0"))
                if -impact > available:
                    raise BacktestToolError("成交造成負現金")
                consume_unsettled = min(self.unsettled_cash, -impact) if reuse_sell_proceeds else Decimal("0")
                self.unsettled_cash -= consume_unsettled
                self._consume_unsettled_settlements(consume_unsettled)
                self.settled_cash += impact + consume_unsettled
                existing = self.positions.get(symbol, {"shares": 0, "cost_basis": Decimal("0")})
                total_cost = existing["cost_basis"] * existing["shares"] + (-impact)
                new_shares = existing["shares"] + shares
                self.positions[symbol] = {"shares": new_shares, "cost_basis": total_cost / Decimal(new_shares)}
            else:
                existing = self.positions.get(symbol)
                if existing is None or existing["shares"] < shares:
                    raise BacktestToolError("成交造成超賣：%s" % symbol)
                existing["shares"] -= shares
                if not existing["shares"]:
                    del self.positions[symbol]
                self.unsettled_cash += impact
                self.pending_settlements.append({"settlement_date": settlement_date, "amount": impact})

    def settle(self, trade_date: str) -> None:
        remaining = []
        for item in self.pending_settlements:
            if item["settlement_date"] <= trade_date:
                amount = decimal_value(item["amount"], "settlement.amount")
                self.unsettled_cash -= amount
                self.settled_cash += amount
            else:
                remaining.append(item)
        self.pending_settlements = remaining

    def _consume_unsettled_settlements(self, amount: Decimal) -> None:
        """Offset sale receivables used as same-day buying power."""
        remaining = amount
        adjusted = []
        for item in self.pending_settlements:
            value = decimal_value(item["amount"], "settlement.amount")
            if remaining and value > 0:
                consumed = min(value, remaining)
                value -= consumed
                remaining -= consumed
            if value:
                adjusted.append({"settlement_date": item["settlement_date"], "amount": value})
        if remaining:
            raise BacktestToolError("未交割賣款不足以支應買進")
        self.pending_settlements = adjusted

    def apply_actions(self, actions: Sequence[Mapping[str, object]]) -> None:
        for action in actions:
            symbol = str(action.get("symbol", "")).upper()
            if symbol not in self.positions:
                continue
            if action.get("type") == "cash_dividend":
                value = decimal_value(action.get("cash_per_share"), "cash_per_share") * self.positions[symbol]["shares"]
                self.unsettled_cash += value
                self.pending_settlements.append({"settlement_date": action["payment_date"], "amount": value})
            elif action.get("type") == "split":
                numerator, denominator = action.get("numerator"), action.get("denominator")
                if not all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in (numerator, denominator)):
                    raise BacktestToolError("split ratio 無效")
                shares = self.positions[symbol]["shares"] * numerator
                if shares % denominator or (shares // denominator) % 1000:
                    raise BacktestToolError("公司行動造成未支援零股")
                self.positions[symbol]["shares"] = shares // denominator
                self.positions[symbol]["cost_basis"] *= Decimal(denominator) / Decimal(numerator)
            else:
                raise BacktestToolError("不支援的公司行動")

    def snapshot(self, close_prices: Mapping[str, object], trade_date: str) -> Dict[str, object]:
        market_value = Decimal("0")
        positions = []
        for symbol, item in sorted(self.positions.items()):
            price = decimal_value(close_prices.get(symbol), "%s.close_price" % symbol)
            value = price * item["shares"]
            market_value += value
            positions.append({"symbol": symbol, "shares": item["shares"], "cost_basis": str(item["cost_basis"]), "close_price": str(price), "market_value": _money(value)})
        cash = self.settled_cash + self.unsettled_cash
        body = {"trade_date": trade_date, "settled_cash": _money(self.settled_cash), "unsettled_cash": _money(self.unsettled_cash), "cash": _money(cash), "positions": positions, "nav": _money(cash + market_value)}
        body["content_sha256"] = artifact_content_sha256(body)
        return body
