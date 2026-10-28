"""滿檔換股的名額脈絡（確定性工具）。

交易 Agent 分批平行執行、各批看不到其他批的候選，因此「賣哪一檔換哪一檔」的配對不能交給
LLM；本模組只把程式算好的名額狀態與全池多空強度統計交給它當背景，實際配對由
``allocation.AllocationOrderEngine`` 依信心等級與 ``position_sizing.rotation`` 決定。

名額、檔數與上限全部由程式計算，Agent 不輸出任何數字；Finalizer 以相同輸入重算整份比對，
任何改寫一律拒絕。兩個強度統計只是對**共同輸入** ``debate`` 的計數，不是任何一批的輸出，也
沒有 ``evidence_id``，只能當背景，不能當證據引用。
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Set

from .fields import DecisionToolError, reject_unknown_fields
from .stance import STRENGTHS


PORTFOLIO_SLOTS_VERSION = "1.0"
PORTFOLIO_SLOTS_FIELDS = {
    "schema_version", "held_positions", "max_positions", "free_slots", "positions_by_breadth",
    "rotation", "unheld_by_bull_strength", "held_by_bear_strength", "note",
}
SLOTS_NOTE = (
    "free_slots 為 0 時，未持有的新股只能用本次全池出場釋出的名額，且由程式依信心等級配對；"
    "出場必須採納至少一個空頭 claim，不得為了騰出名額而出場，也不得為了填滿名額而買進。"
    "名額、權重、股數與現金一律由程式計算，本區塊只是背景，沒有 evidence_id，不可引用。"
)


def _packet(debate: Mapping[str, object], role: str) -> Mapping[str, object]:
    for packet in debate.get("packets", []):
        if isinstance(packet, Mapping) and packet.get("role") == role:
            return packet
    raise DecisionToolError("ResearchDebateBundle 缺少 %s 論點封包" % role)


def _strength_counts(packet: Mapping[str, object], symbols: Set[str]) -> Dict[str, int]:
    """封包內屬於 symbols 的股票依 strength 計數；未知強度不計入，缺漏不補假值。"""
    counts = {level: 0 for level in STRENGTHS}
    for item in packet.get("items", []):
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol", "")).upper()
        level = str(item.get("strength", ""))
        if symbol in symbols and level in counts:
            counts[level] += 1
    return counts


def build_portfolio_slots(
    bundle: Mapping[str, object],
    policy: Mapping[str, object],
    debate: Mapping[str, object],
) -> Dict[str, object]:
    """交易 Agent 的名額脈絡。

    只讀 ``apply_trade_decision`` 不會改動的 policy 欄位（``max_positions``、
    ``position_sizing.positions_by_breadth``、``position_sizing.rotation``），因此由基礎 policy
    與最終 policy 建出的結果完全相同，Finalizer 才能用最終 policy 重算比對。
    """
    account = bundle["account_snapshot"]
    held = {str(item["symbol"]).upper() for item in account["positions"]}
    universe = {
        str(row["symbol"]).upper() for row in bundle["snapshot"]["latest_prices"]
    }
    max_positions = int(policy["max_positions"])
    sizing = policy.get("position_sizing") or {}
    breadth = sizing.get("positions_by_breadth")
    rotation = sizing.get("rotation")
    return {
        "schema_version": PORTFOLIO_SLOTS_VERSION,
        "held_positions": len(held),
        "max_positions": max_positions,
        # 真正的空名額；風險 Agent 稍後給的持股廣度可能更小，目標檔數不在交易階段決定。
        "free_slots": max(0, max_positions - len(held)),
        "positions_by_breadth": dict(breadth) if isinstance(breadth, Mapping) else None,
        "rotation": {"enabled": False} if not isinstance(rotation, Mapping) else {
            "enabled": True,
            "max_swaps_per_run": int(rotation["max_swaps_per_run"]),
            "min_new_conviction": str(rotation["min_new_conviction"]),
        },
        "unheld_by_bull_strength": _strength_counts(_packet(debate, "bull"), universe - held),
        "held_by_bear_strength": _strength_counts(_packet(debate, "bear"), held),
        "note": SLOTS_NOTE,
    }


def validate_portfolio_slots(
    bundle: Mapping[str, object],
    policy: Mapping[str, object],
    debate: Mapping[str, object],
    slots: object,
    errors: List[str],
) -> None:
    """以同一輸入重算整份名額脈絡並逐欄比對；不一致即拒絕（fail-closed）。"""
    prefix = "team_inputs.portfolio_slots"
    if not isinstance(slots, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(slots, PORTFOLIO_SLOTS_FIELDS, prefix, errors)
    try:
        expected = build_portfolio_slots(bundle, policy, debate)
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        errors.append("%s 無法以共同輸入重算：%s" % (prefix, error))
        return
    if dict(slots) != expected:
        errors.append("%s 與重算結果不同，拒絕改寫" % prefix)
