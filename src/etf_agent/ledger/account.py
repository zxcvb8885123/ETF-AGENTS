"""Trade-date cash ledger with separately settled sale proceeds and corporate actions."""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, Sequence

from etf_agent.core import content_sha256

from .contracts import LedgerError, decimal_value, money_string


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
                    raise LedgerError("成交造成負現金")
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
                    raise LedgerError("成交造成超賣：%s" % symbol)
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
            raise LedgerError("未交割賣款不足以支應買進")
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
                    raise LedgerError("split ratio 無效")
                shares = self.positions[symbol]["shares"] * numerator
                if shares % denominator or (shares // denominator) % 1000:
                    raise LedgerError("公司行動造成未支援零股")
                self.positions[symbol]["shares"] = shares // denominator
                self.positions[symbol]["cost_basis"] *= Decimal(denominator) / Decimal(numerator)
            else:
                raise LedgerError("不支援的公司行動")

    def snapshot(self, close_prices: Mapping[str, object], trade_date: str) -> Dict[str, object]:
        market_value = Decimal("0")
        positions = []
        for symbol, item in sorted(self.positions.items()):
            price = decimal_value(close_prices.get(symbol), "%s.close_price" % symbol)
            value = price * item["shares"]
            market_value += value
            positions.append({"symbol": symbol, "shares": item["shares"], "cost_basis": str(item["cost_basis"]), "close_price": str(price), "market_value": money_string(value)})
        cash = self.settled_cash + self.unsettled_cash
        body = {"trade_date": trade_date, "settled_cash": money_string(self.settled_cash), "unsettled_cash": money_string(self.unsettled_cash), "cash": money_string(cash), "positions": positions, "nav": money_string(cash + market_value)}
        body["content_sha256"] = content_sha256(body)
        return body
