"""確定性統計工具：前瞻報酬、Rank IC、分組平均。全部使用 Decimal，不用浮點數。"""

from __future__ import annotations

from decimal import Decimal, getcontext
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

getcontext().prec = 40

QUANT = Decimal("0.000001")


def forward_return(entry_price: Decimal, exit_price: Decimal) -> Decimal:
    if entry_price <= 0 or exit_price <= 0:
        raise ValueError("價格必須為正")
    return exit_price / entry_price - Decimal(1)


def mean(values: Sequence[Decimal]) -> Optional[Decimal]:
    return sum(values, Decimal(0)) / len(values) if values else None


def format_decimal(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value.quantize(QUANT))


def average_ranks(values: Sequence[Decimal]) -> List[Decimal]:
    """由小到大的名次；相同值取平均名次。"""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [Decimal(0)] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        shared = Decimal(start + end) / 2 + 1
        for position in range(start, end + 1):
            ranks[order[position]] = shared
        start = end + 1
    return ranks


def rank_ic(pairs: Sequence[Tuple[Decimal, Decimal]]) -> Optional[Decimal]:
    """Spearman 相關。少於 3 筆，或任一邊全為同一值時沒有定義，回傳 None。"""
    if len(pairs) < 3:
        return None
    signals = [Decimal(signal) for signal, _ in pairs]
    returns = [Decimal(value) for _, value in pairs]
    if len(set(signals)) == 1 or len(set(returns)) == 1:
        return None
    left, right = average_ranks(signals), average_ranks(returns)
    left_mean, right_mean = mean(left), mean(right)
    covariance = sum(((a - left_mean) * (b - right_mean) for a, b in zip(left, right)), Decimal(0))
    left_var = sum(((a - left_mean) ** 2 for a in left), Decimal(0))
    right_var = sum(((b - right_mean) ** 2 for b in right), Decimal(0))
    return covariance / (left_var * right_var).sqrt()


def percentile(sorted_values: Sequence[Decimal], fraction: Decimal) -> Optional[Decimal]:
    """最近名次法；輸入需已由小到大排序。"""
    if not sorted_values:
        return None
    rank = int((fraction * len(sorted_values)).to_integral_value(rounding="ROUND_CEILING"))
    return sorted_values[min(max(rank, 1), len(sorted_values)) - 1]


def group_summary(rows: Sequence[Mapping[str, object]], key: str) -> Dict[str, Dict[str, object]]:
    """依 key 分組，回報筆數、平均報酬與平均超額報酬。"""
    groups: Dict[str, List[Mapping[str, object]]] = {}
    for row in rows:
        groups.setdefault(str(row[key]), []).append(row)
    return {
        name: {
            "n_obs": len(items),
            "mean_return": format_decimal(mean([item["return"] for item in items])),
            "mean_excess_return": format_decimal(mean([item["excess"] for item in items])),
        }
        for name, items in sorted(groups.items())
    }
