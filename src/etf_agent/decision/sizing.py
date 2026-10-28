"""Conviction tiers, cash stance and their deterministic conversion into weights.

交易 Agent 只對 buy／add 候選給出 ``high``／``medium``／``low`` 等級，風險 Agent 只給
現金姿態與持股廣度；權重由本模組依等級乘數與 ATR 波動度確定性計算，Agent 不輸出任何權重、
股數或金額。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence

from .contracts import (
    DecisionContext,
    DecisionToolError,
    decimal_value,
    reject_unknown_fields,
    required_string,
    string_list,
)


SIZING_METHOD = "conviction_volatility_v1"
# 2026-10-28 後新增 v2：以 volatility_exponent 控制低波動偏好的強度（0 為完全不看波動度）。
# v1 的語意與程式路徑完全不動，舊 Decision run 仍以原式重建。
SIZING_METHOD_V2 = "conviction_volatility_v2"
SIZING_METHODS = (SIZING_METHOD, SIZING_METHOD_V2)
# 同等級同支持數的入選順序；省略欄位即沿用 atr_low（低波動優先）。
SELECTION_TIEBREAKS = ("atr_low", "symbol", "momentum_high")
# 2026-10-19 後新增的單股硬上限整張減碼；省略此欄位的舊 Decision run 維持原重建行為。
STOCK_WEIGHT_FIT = "v1"
# 2026-10-28 後新增：持股滿檔時未持有的新股不佔名額，已持有的 add 照常入選。
# 省略此欄位的舊 Decision run 維持新股可佔滿檔名額的原重建行為。
HELD_ADD_PRIORITY = "v1"
# 2026-10-28 後新增：滿檔時本次出場釋出的名額可以給未持有的新股（換股）。省略 rotation 的
# Decision run 維持「滿檔完全不進新股」的原重建行為；名額配對全部由程式計算，Agent 不參與。
ROTATION_VERSION = "v1"
ROTATION_FIELDS = {"version", "max_swaps_per_run", "min_new_conviction"}
CONVICTION_LEVELS = ("high", "medium", "low")
# 現金姿態由積極到防守；對應的現金緩衝由 Policy 設定，必須低於競賽現金上限。
CASH_STANCES = ("aggressive", "neutral", "defensive")
# 持股廣度由集中到分散；風險 Agent 與現金姿態一起給，對應的目標檔數由 Policy 的
# positions_by_breadth 設定。省略此欄位的舊 Decision run 維持填滿 max_positions 的原重建行為。
POSITION_BREADTHS = ("concentrated", "balanced", "diversified")
# 封頂時保留 0.5% 相對緩衝：買進手續費使成交後 NAV 變小，剛好配到上限的部位會以
# 50.0009% 之類的權重觸發 Guard。全額換手時手續費影響約 0.14%，0.5% 足以涵蓋。
LIMIT_HEADROOM = Decimal("0.995")


def validate_cash_stance(context: DecisionContext, stance: object, prefix: str, errors: List[str]) -> None:
    """現金姿態：level 三選一、理由與共同輸入中存在的證據（市場層級可引用任一股票）。"""
    if not isinstance(stance, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(stance, {"level", "breadth", "rationale", "evidence_ids"}, prefix, errors)
    if stance.get("level") not in CASH_STANCES:
        errors.append("%s.level 必須是 %s" % (prefix, "／".join(CASH_STANCES)))
    if "breadth" in stance and stance["breadth"] not in POSITION_BREADTHS:
        errors.append("%s.breadth 必須是 %s" % (prefix, "／".join(POSITION_BREADTHS)))
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


def _validate_volatility_exponent(sizing: Mapping[str, object], errors: List[str]) -> None:
    """v2 必須給 0～1 的波動度指數；v1 不得出現此欄位，確保舊 run 走原式重建。"""
    present = "volatility_exponent" in sizing
    if sizing.get("method") == SIZING_METHOD_V2:
        if not present:
            errors.append("position_sizing.volatility_exponent 是 %s 的必要欄位" % SIZING_METHOD_V2)
            return
    elif present:
        errors.append(
            "position_sizing.volatility_exponent 只允許用於 %s；%s 必須省略"
            % (SIZING_METHOD_V2, SIZING_METHOD)
        )
        return
    if not present:
        return
    try:
        exponent = decimal_value(sizing["volatility_exponent"], "position_sizing.volatility_exponent")
    except DecisionToolError as error:
        errors.append(str(error))
        return
    if exponent < 0 or exponent > 1:
        errors.append("position_sizing.volatility_exponent 必須介於 0 與 1（含）")


def _validate_rotation(sizing: Mapping[str, object], errors: List[str]) -> None:
    """滿檔換股名額的政策欄位；省略即舊行為（滿檔完全不進新股）。"""
    if "rotation" not in sizing:
        return
    rotation = sizing["rotation"]
    if not isinstance(rotation, Mapping):
        errors.append("position_sizing.rotation 必須是物件")
        return
    reject_unknown_fields(rotation, ROTATION_FIELDS, "position_sizing.rotation", errors)
    if rotation.get("version") != ROTATION_VERSION:
        errors.append("position_sizing.rotation.version 只允許 %s" % ROTATION_VERSION)
    swaps = rotation.get("max_swaps_per_run")
    if isinstance(swaps, bool) or not isinstance(swaps, int) or swaps < 0:
        errors.append("position_sizing.rotation.max_swaps_per_run 必須是非負整數")
    if rotation.get("min_new_conviction") not in CONVICTION_LEVELS:
        errors.append("position_sizing.rotation.min_new_conviction 必須是 high／medium／low 之一")
    # 換股名額的前提是「新股不佔滿檔名額」，否則釋出名額的計算沒有意義。
    if "held_add_priority" not in sizing:
        errors.append("position_sizing.rotation 必須與 held_add_priority 同時存在")


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
            "support_by_symbol", "stock_weight_fit", "positions_by_breadth", "position_breadth",
            "held_add_priority", "rotation", "volatility_exponent", "selection_tiebreak",
        },
        "DecisionPolicy.position_sizing",
        errors,
    )
    if sizing.get("method") not in SIZING_METHODS:
        errors.append("position_sizing.method 必須為 %s" % "或".join(SIZING_METHODS))
    _validate_volatility_exponent(sizing, errors)
    if "selection_tiebreak" in sizing and sizing["selection_tiebreak"] not in SELECTION_TIEBREAKS:
        errors.append(
            "position_sizing.selection_tiebreak 必須是 %s" % "／".join(SELECTION_TIEBREAKS)
        )
    if "stock_weight_fit" in sizing and sizing["stock_weight_fit"] != STOCK_WEIGHT_FIT:
        errors.append("position_sizing.stock_weight_fit 只允許 %s；舊 Decision run 省略此欄位" % STOCK_WEIGHT_FIT)
    if "held_add_priority" in sizing and sizing["held_add_priority"] != HELD_ADD_PRIORITY:
        errors.append("position_sizing.held_add_priority 只允許 %s；舊 Decision run 省略此欄位" % HELD_ADD_PRIORITY)
    _validate_rotation(sizing, errors)
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
    targets = _validate_breadth_targets(policy, sizing, errors)
    tiers = sizing.get("conviction_by_symbol")
    if "position_breadth" in sizing and targets is None:
        errors.append("position_sizing.position_breadth 需要 positions_by_breadth")
    if targets is not None and tiers is not None and sizing.get("position_breadth") not in POSITION_BREADTHS:
        errors.append("position_sizing.position_breadth 必須是 %s" % "／".join(POSITION_BREADTHS))
    if targets is not None and tiers is None and "position_breadth" in sizing:
        errors.append("position_sizing.position_breadth 只能與交易決策一起綁定")
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
    _validate_support(sizing, tiers, errors)


def _validate_breadth_targets(
    policy: Mapping[str, object], sizing: Mapping[str, object], errors: List[str]
) -> Optional[Mapping[str, object]]:
    """positions_by_breadth：三種廣度各對應一個正整數檔數且集中 ≤ 平衡 ≤ 分散；配置時再夾在 min／max_positions 之間。"""
    if "positions_by_breadth" not in sizing:
        return None
    targets = sizing["positions_by_breadth"]
    if not isinstance(targets, Mapping) or set(targets) != set(POSITION_BREADTHS):
        errors.append("position_sizing.positions_by_breadth 必須剛好包含 concentrated／balanced／diversified")
        return None
    values = [targets[level] for level in POSITION_BREADTHS]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        errors.append("position_sizing.positions_by_breadth 必須是整數")
        return None
    if any(value < 1 for value in values):
        errors.append("position_sizing.positions_by_breadth 必須是正整數")
    if not values[0] <= values[1] <= values[2]:
        errors.append("position_sizing.positions_by_breadth 必須 concentrated ≤ balanced ≤ diversified")
    return targets


def breadth_target(sizing: Mapping[str, object]) -> Optional[int]:
    """已綁定持股廣度時回傳目標檔數；舊 policy 沒有 positions_by_breadth 時回 None（沿用 max_positions）。"""
    targets = sizing.get("positions_by_breadth")
    level = sizing.get("position_breadth")
    if not isinstance(targets, Mapping) or level not in targets:
        return None
    return int(targets[level])


def _validate_support(sizing: Mapping[str, object], tiers: Mapping[str, object], errors: List[str]) -> None:
    support = sizing.get("support_by_symbol")
    if support is None:
        return
    if not isinstance(support, Mapping) or set(support) != set(tiers):
        errors.append("position_sizing.support_by_symbol 必須與 conviction_by_symbol 股票一致")
    elif any(isinstance(value, bool) or not isinstance(value, int) for value in support.values()):
        errors.append("position_sizing.support_by_symbol 必須是整數")


def conviction_weights(
    order: Sequence[str],
    tiers: Mapping[str, str],
    multipliers: Mapping[str, Decimal],
    volatility: Mapping[str, Decimal],
    volatility_floor: Decimal,
    limits: Mapping[str, Decimal],
    budget: Decimal,
    volatility_exponent: Optional[Decimal] = None,
) -> Dict[str, Decimal]:
    """Split budget ∝ multiplier / max(ATR%, floor) ** exponent; cap at limits and redistribute.

    ``volatility_exponent`` 為 None 時走 v1 原式（不經過 ``**``），保證舊 Decision run 逐位重建；
    0 代表完全不看波動度（同等級等權），1 與 v1 數值相同。
    """
    def _raw(symbol: str) -> Decimal:
        multiplier = multipliers[tiers[symbol]]
        floored = max(volatility[symbol], volatility_floor)
        if volatility_exponent is None:
            return multiplier / floored
        if volatility_exponent == 0:
            return multiplier
        return multiplier / (floored ** volatility_exponent)

    raw = {symbol: _raw(symbol) for symbol in order}
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
