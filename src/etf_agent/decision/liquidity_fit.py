"""買單流動性縮減：``position_sizing.liquidity_fit`` v1（版本閘門，只由人設定；策略反思不得改）。

起因：2026-11-12 的 run，交易 Agent 想買的 8415.TWO，買單是 20 日均量的 1.64 倍；風險 Agent 只能用 ``remove_candidate``
整筆移除，沒有「縮小」可用。本閘門讓配置引擎在其他縮減（現金、單股、產業、換手）之後，把每筆買單的股數縮到
「人填的 ``max_participation`` × 20 日均量」以內（向下取整張）；縮到不足一張就取消該筆買單。

- 只縮買單，不碰賣單，不改寫交易 Agent 的意圖；縮減原因寫在旗標 ``LIQUIDITY_LIMIT``。
- 均量取 ``MomentumEngine`` 的 ``average_volume_20d``（與風險審查的流動性事實同一來源）；缺均量的買單不縮、留旗標
  ``LIQUIDITY_UNAVAILABLE``，由風險審查原本的 flagged 機制處理。
- 只有版本與一個人填的數字（字串小數，大於 0 且不高於 1）。沒有此欄位的舊 Policy／舊 run 逐位元維持原行為。
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Mapping, Optional

from .fields import DecisionToolError, decimal_value, reject_unknown_fields

LIQUIDITY_FIT_V1 = "v1"
LIQUIDITY_FIT_FIELDS = frozenset({"version", "max_participation"})


def liquidity_fit_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """啟用時回傳閘門物件；沒有（或形狀不合，已被驗證拒絕）回 None。"""
    gate = sizing.get("liquidity_fit") if isinstance(sizing, Mapping) else None
    if isinstance(gate, Mapping) and gate.get("version") == LIQUIDITY_FIT_V1:
        return gate
    return None


def liquidity_fit_limit(sizing: object) -> Optional[Decimal]:
    gate = liquidity_fit_gate(sizing)
    if gate is None:
        return None
    return decimal_value(gate["max_participation"], "liquidity_fit.max_participation")


def validate_liquidity_fit_gate(policy: Mapping[str, object], errors: List[str]) -> None:
    gate = (policy.get("position_sizing") or {}).get("liquidity_fit")  # type: ignore[union-attr]
    prefix = "position_sizing.liquidity_fit"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, LIQUIDITY_FIT_FIELDS, prefix, errors)
    if gate.get("version") != LIQUIDITY_FIT_V1:
        errors.append("%s.version 只允許 %s" % (prefix, LIQUIDITY_FIT_V1))
    if "max_participation" not in gate:
        errors.append("%s 缺少 max_participation（數值由人填，程式不設預設）" % prefix)
        return
    raw = gate["max_participation"]
    if isinstance(raw, bool) or not isinstance(raw, str):
        errors.append("%s.max_participation 必須是數字字串" % prefix)
        return
    try:
        value = decimal_value(raw, "%s.max_participation" % prefix)
    except DecisionToolError as error:
        errors.append(str(error))
        return
    if value <= 0 or value > 1:
        errors.append("%s.max_participation 必須大於 0 且不高於 1" % prefix)
