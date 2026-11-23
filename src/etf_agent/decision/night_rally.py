"""夜盤大漲放行進攻（position_sizing.night_rally_override，版本閘門，只由人設定、策略反思不得改）。

沒有閘門時：全市場情緒為 negative 就不得 aggressive（原一致性護欄，逐位元不變）。
有閘門時：同一份決策輸入的隔夜市場背景裡，台指期夜盤（TXF_NIGHT）可用、漲幅不低於人填的
``min_night_change_pct``（百分比，例如 "5" 代表 +5%），且風險 Agent 的現金姿態引用了該夜盤的 evidence_id，
才放行 aggressive。條件只由程式依封存的隔夜資料包重算，Finalizer 與即時驗證走同一個函式。
放行只是「允許」，是否選 aggressive 仍由風險 Agent 判斷；Agent 不輸出任何數字。
"""

from decimal import Decimal, InvalidOperation
from typing import Iterable, List, Mapping, Optional

GATE_FIELD = "night_rally_override"
GATE_VERSION = "v1"
NIGHT_SYMBOL = "TXF_NIGHT"
_MAX_THRESHOLD = Decimal("10")  # 台指期單日漲跌幅上限 10%，門檻高於它永遠不會成立


def night_rally_override_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """閘門啟用時回傳閘門物件；沒有閘門回 None（行為與舊版完全相同）。"""
    gate = sizing.get(GATE_FIELD) if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def _threshold(gate: Mapping[str, object]) -> Optional[Decimal]:
    raw = gate.get("min_night_change_pct")
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def validate_night_rally_override_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """閘門形狀：{version: v1, min_night_change_pct: "<0 到 10 的百分比字串>"}；數值由人填、程式不給預設。"""
    gate = sizing[GATE_FIELD]
    prefix = "position_sizing.%s" % GATE_FIELD
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    unknown = sorted(set(gate) - {"version", "min_night_change_pct"})
    if unknown:
        errors.append("%s 含未知欄位：%s" % (prefix, "、".join(unknown)))
    if gate.get("version") != GATE_VERSION:
        errors.append("%s.version 必須是 %s" % (prefix, GATE_VERSION))
    value = _threshold(gate)
    if value is None or not Decimal("0") < value <= _MAX_THRESHOLD:
        errors.append("%s.min_night_change_pct 必須是大於 0、不超過 10 的百分比字串（由人填）" % prefix)


def night_rally_component(bundle: Mapping[str, object]) -> Optional[Mapping[str, object]]:
    """決策輸入裡可用、有 evidence_id 的台指期夜盤成分；沒有就回 None。"""
    overnight = bundle.get("overnight_market_input") if isinstance(bundle, Mapping) else None
    result = overnight.get("result") if isinstance(overnight, Mapping) else None
    if not isinstance(result, Mapping):
        return None
    for row in result.get("components") or []:
        if (isinstance(row, Mapping) and row.get("symbol") == NIGHT_SYMBOL and row.get("status") == "available"
                and isinstance(row.get("evidence_id"), str) and isinstance(row.get("change_pct"), str)):
            return row
    return None


def night_rally_allows_aggressive(
    gate: Optional[Mapping[str, object]], bundle: Mapping[str, object], evidence_ids: Iterable[str],
) -> bool:
    """閘門啟用、夜盤可用且漲幅 ≥ 門檻、姿態引用了夜盤證據時回 True；任何一項不成立回 False（fail-closed）。"""
    if gate is None or gate.get("version") != GATE_VERSION:
        return False
    threshold = _threshold(gate)
    row = night_rally_component(bundle)
    if threshold is None or row is None:
        return False
    try:
        change = Decimal(str(row["change_pct"]))
    except InvalidOperation:
        return False
    return change.is_finite() and change >= threshold and row["evidence_id"] in set(evidence_ids)


def night_rally_brief(gate: Optional[Mapping[str, object]], bundle: Mapping[str, object]) -> Optional[Mapping[str, object]]:
    """給風險 Agent 的簡報說明（背景、不可引用）；沒有閘門回 None。"""
    if gate is None:
        return None
    row = night_rally_component(bundle)
    return {
        "min_night_change_pct": gate.get("min_night_change_pct"),
        "night_evidence_id": row["evidence_id"] if row is not None else None,
        "night_change_pct": row["change_pct"] if row is not None else None,
        "rule": "全市場情緒為 negative 時，原本不得選 aggressive；若台指期夜盤漲幅不低於 min_night_change_pct，"
                "且 evidence_ids 引用 night_evidence_id，程式放行 aggressive。是否選 aggressive 仍由你判斷，"
                "理由須說明夜盤與恐懼指標（前一交易日收盤資料）的取捨。夜盤不可用（night_evidence_id 為 null）時不放行。",
    }
