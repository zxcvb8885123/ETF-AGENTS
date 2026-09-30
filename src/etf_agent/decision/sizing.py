"""Conviction tiers, cash stance and their deterministic conversion into weights.

交易 Agent 只對 buy／add 候選給出 ``high``／``medium``／``low`` 等級，風險 Agent 只給
現金姿態；權重由本模組依等級乘數與 ATR 波動度確定性計算，Agent 不輸出任何權重、
股數或金額。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, Sequence

from .contracts import (
    DecisionContext,
    DecisionToolError,
    decimal_value,
    reject_unknown_fields,
    required_string,
    string_list,
)


SIZING_METHOD = "conviction_volatility_v1"
CONVICTION_LEVELS = ("high", "medium", "low")
# 現金姿態由積極到防守；對應的現金緩衝由 Policy 設定，必須低於競賽現金上限。
CASH_STANCES = ("aggressive", "neutral", "defensive")
# 封頂時保留 0.5% 相對緩衝：買進手續費使成交後 NAV 變小，剛好配到上限的部位會以
# 50.0009% 之類的權重觸發 Guard。全額換手時手續費影響約 0.14%，0.5% 足以涵蓋。
LIMIT_HEADROOM = Decimal("0.995")


def validate_cash_stance(context: DecisionContext, stance: object, prefix: str, errors: List[str]) -> None:
    """現金姿態：level 三選一、理由與共同輸入中存在的證據（市場層級可引用任一股票）。"""
    if not isinstance(stance, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(stance, {"level", "rationale", "evidence_ids"}, prefix, errors)
    if stance.get("level") not in CASH_STANCES:
        errors.append("%s.level 必須是 %s" % (prefix, "／".join(CASH_STANCES)))
    try:
        required_string(stance, "rationale")
        evidence = string_list(stance, "evidence_ids")
    except DecisionToolError as error:
        errors.append("%s.%s" % (prefix, error))
        return
    if not evidence:
        errors.append("%s.evidence_ids 不得為空" % prefix)
    unknown = [item for item in evidence if item not in context.evidence_symbols]
    if unknown:
        errors.append("%s 引用不存在：%s" % (prefix, ", ".join(unknown)))


def validate_position_sizing(
    policy: Mapping[str, object], universe: Sequence[str], errors: List[str]
) -> None:
    sizing = policy.get("position_sizing")
    if not isinstance(sizing, Mapping):
        errors.append("DecisionPolicy.position_sizing 必須是物件")
        return
    reject_unknown_fields(
        sizing,
        {
            "method", "conviction_multipliers", "volatility_floor", "cash_buffer_by_stance",
            "cash_stance", "sizing_plan_id", "sizing_plan_sha256", "conviction_by_symbol",
        },
        "DecisionPolicy.position_sizing",
        errors,
    )
    if sizing.get("method") != SIZING_METHOD:
        errors.append("position_sizing.method 必須為 %s" % SIZING_METHOD)
    multipliers = sizing.get("conviction_multipliers")
    if not isinstance(multipliers, Mapping) or set(multipliers) != set(CONVICTION_LEVELS):
        errors.append("position_sizing.conviction_multipliers 必須剛好包含 high／medium／low")
    else:
        try:
            values = [
                decimal_value(multipliers[level], "conviction_multipliers.%s" % level)
                for level in CONVICTION_LEVELS
            ]
            if any(value <= 0 for value in values):
                errors.append("position_sizing.conviction_multipliers 必須大於 0")
            if not values[0] >= values[1] >= values[2]:
                errors.append("position_sizing.conviction_multipliers 必須 high ≥ medium ≥ low")
        except DecisionToolError as error:
            errors.append(str(error))
    try:
        floor = decimal_value(sizing.get("volatility_floor"), "position_sizing.volatility_floor")
        if floor <= 0 or floor >= 1:
            errors.append("position_sizing.volatility_floor 必須介於 0 與 1（不含）")
    except DecisionToolError as error:
        errors.append(str(error))
    buffers = sizing.get("cash_buffer_by_stance")
    if not isinstance(buffers, Mapping) or set(buffers) != set(CASH_STANCES):
        errors.append("position_sizing.cash_buffer_by_stance 必須剛好包含 aggressive／neutral／defensive")
        buffers = None
    else:
        try:
            values = [decimal_value(buffers[level], "cash_buffer_by_stance.%s" % level) for level in CASH_STANCES]
            ceiling = decimal_value(policy.get("cash_weight_ceiling"), "cash_weight_ceiling")
            if any(value < 0 or value >= ceiling for value in values):
                errors.append("position_sizing.cash_buffer_by_stance 必須不小於 0 且低於 cash_weight_ceiling")
            if not values[0] <= values[1] <= values[2]:
                errors.append("position_sizing.cash_buffer_by_stance 必須 aggressive ≤ neutral ≤ defensive")
        except DecisionToolError as error:
            errors.append(str(error))
    tiers = sizing.get("conviction_by_symbol")
    bound = [
        field for field in ("sizing_plan_id", "sizing_plan_sha256", "cash_stance") if sizing.get(field)
    ]
    if tiers is None:
        if bound:
            errors.append("position_sizing 有交易決策綁定卻缺少 conviction_by_symbol")
        return
    if len(bound) != 3:
        errors.append("position_sizing.conviction_by_symbol 必須綁定 sizing_plan_id、sizing_plan_sha256 與 cash_stance")
    stance = sizing.get("cash_stance")
    if stance not in CASH_STANCES:
        errors.append("position_sizing.cash_stance 必須是 %s" % "／".join(CASH_STANCES))
    elif buffers is not None:
        try:
            if decimal_value(policy.get("cash_buffer_rate"), "cash_buffer_rate") != decimal_value(
                buffers[stance], "cash_buffer_by_stance.%s" % stance
            ):
                errors.append("cash_buffer_rate 必須等於 cash_stance 對應的現金緩衝")
        except DecisionToolError as error:
            errors.append(str(error))
    if not isinstance(tiers, Mapping):
        errors.append("position_sizing.conviction_by_symbol 必須是物件")
        return
    for symbol, tier in tiers.items():
        if symbol != str(symbol).upper() or symbol not in universe:
            errors.append("position_sizing.conviction_by_symbol 含交易池外或非大寫股票：%s" % symbol)
        if tier not in CONVICTION_LEVELS:
            errors.append("position_sizing.conviction_by_symbol.%s 等級無效" % symbol)


def conviction_weights(
    order: Sequence[str],
    tiers: Mapping[str, str],
    multipliers: Mapping[str, Decimal],
    volatility: Mapping[str, Decimal],
    volatility_floor: Decimal,
    limits: Mapping[str, Decimal],
    budget: Decimal,
) -> Dict[str, Decimal]:
    """Split budget ∝ multiplier / max(ATR%, floor); cap at limits and redistribute."""
    raw = {
        symbol: multipliers[tiers[symbol]] / max(volatility[symbol], volatility_floor)
        for symbol in order
    }
    weights = {symbol: Decimal("0") for symbol in order}
    remaining = list(order)
    left = max(budget, Decimal("0"))
    while remaining and left > 0:
        total = sum(raw[symbol] for symbol in remaining)
        proposed = {symbol: left * raw[symbol] / total for symbol in remaining}
        capped = [symbol for symbol in remaining if proposed[symbol] > limits[symbol]]
        if not capped:
            weights.update(proposed)
            break
        for symbol in capped:
            weights[symbol] = limits[symbol]
            left -= limits[symbol]
            remaining.remove(symbol)
    return weights
