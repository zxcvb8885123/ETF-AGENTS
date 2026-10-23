"""基準買入持有的確定性計算：整張買進、費稅、每日估值、報酬與回撤。

所有數值由 Decimal 計算；費稅用 ledger.ExecutionSimulator 的同一套公式。
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.ledger import ExecutionSimulator

from .contracts import MONEY_PLACES, RATE_PLACES, BenchmarkError, FeeSchedule


def money(value: Decimal) -> str:
    return str(value.quantize(MONEY_PLACES, rounding=ROUND_HALF_EVEN))


def rate(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value.quantize(RATE_PLACES, rounding=ROUND_HALF_EVEN))


def whole_lot_buy(
    simulator: ExecutionSimulator, symbol: str, budget: Decimal, price: Decimal
) -> Optional[Dict[str, object]]:
    """在 budget 內以 price 買進最多整張；含手續費後仍超過 budget 就少一張；一張都買不起回 None。"""
    if price <= 0:
        raise BenchmarkError("%s 的成交價必須為正數" % symbol)
    if budget <= 0:
        return None
    lots = int(budget // (price * simulator.lot_size))
    while lots > 0:
        fill = simulator.fill(symbol, "buy", lots * simulator.lot_size, price)
        if -Decimal(str(fill["net_cash_impact"])) <= budget:
            return fill
        lots -= 1
    return None


def buy_and_hold(
    capital: Decimal,
    budgets: Mapping[str, Decimal],
    execution_prices: Mapping[str, Decimal],
    fees: FeeSchedule,
) -> Dict[str, object]:
    """以各標的預算在起始日整張買進，剩餘現金留著；回傳成交、持股與現金。"""
    simulator = ExecutionSimulator(fees.assumptions())
    fills: List[Dict[str, object]] = []
    positions: Dict[str, int] = {}
    cash = capital
    commission = Decimal(0)
    for symbol in sorted(budgets):
        price = execution_prices.get(symbol)
        if price is None:
            raise BenchmarkError("%s 缺少起始日成交價" % symbol)
        fill = whole_lot_buy(simulator, symbol, budgets[symbol], price)
        if fill is None:
            continue
        fills.append(fill)
        positions[symbol] = positions.get(symbol, 0) + int(fill["shares"])
        cash += Decimal(str(fill["net_cash_impact"]))
        commission += Decimal(str(fill["commission"]))
    if cash < 0:
        raise BenchmarkError("買入持有造成負現金")
    return {"cash": cash, "positions": positions, "fills": fills, "commission": commission}


def valuation(
    cash: Decimal, positions: Mapping[str, int], closes: Mapping[str, Decimal]
) -> Tuple[Optional[Decimal], List[str]]:
    """現金＋持股 × 官方收盤價；任一持股缺收盤價就回 (None, 缺價標的)。"""
    missing = sorted(symbol for symbol in positions if symbol not in closes)
    if missing:
        return None, missing
    total = cash
    for symbol, shares in positions.items():
        total += closes[symbol] * shares
    return total, []


def cumulative_return(nav: Decimal, initial: Decimal) -> Decimal:
    if initial <= 0:
        raise BenchmarkError("初始資金必須為正數")
    return nav / initial - 1


def max_drawdown(values: Sequence[Decimal]) -> Optional[Decimal]:
    """峰值到谷底的最大跌幅（正數代表回撤）；少於兩點回 None。"""
    if len(values) < 2:
        return None
    peak = values[0]
    worst = Decimal(0)
    for value in values:
        if value > peak:
            peak = value
        if peak > 0:
            drawdown = (peak - value) / peak
            if drawdown > worst:
                worst = drawdown
    return worst


def relative_drawdown(account: Sequence[Decimal], benchmark: Sequence[Decimal]) -> Optional[Decimal]:
    """帳戶 NAV ÷ 基準 NAV（同日對齊）序列的最大回撤：相對基準落後最多的幅度。"""
    if len(account) != len(benchmark):
        raise BenchmarkError("相對回撤的兩個序列長度必須相同")
    ratios = []
    for numerator, denominator in zip(account, benchmark):
        if denominator <= 0:
            raise BenchmarkError("基準 NAV 必須為正數")
        ratios.append(numerator / denominator)
    return max_drawdown(ratios)
