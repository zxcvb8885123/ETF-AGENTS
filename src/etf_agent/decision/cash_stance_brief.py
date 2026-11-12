"""風險 Agent 現金姿態簡報縮減（``position_sizing.cash_stance_brief`` v2 版本閘門）。

只改簡報副本與提示文字，不新增 Agent 輸出欄位、不改 Validator 的引用規則、不改配置，``team_inputs`` 不升版、沒有新鍵
（``team_inputs.offense_facts`` 照舊計算與封存，Finalizer 重算不受影響）。沒有閘門時簡報的鍵、順序、提示文字與 Schema 與舊版逐位元相同。

- ``company_events: high_only``：簡報的公司事件由全交易池逐檔明細改為摘要（各 ``event_outlook`` 檔數、``materiality`` × ``direction`` 計數、
  只列 high 事件與 ``omitted`` 計數）；鍵的順序改為必讀欄位在前、公司事件摘要在最後（``evidence_recency`` 仍是第一個鍵）。
- ``offense_reference: omit``（選配，只有 ``offense`` 閘門存在時才允許）：簡報的 ``offense_facts`` 不附 ``reference`` 對照樣本。
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence

from .fields import reject_unknown_fields

CASH_STANCE_BRIEF_VERSION = "v2"
COMPANY_EVENTS_HIGH_ONLY = "high_only"
OFFENSE_REFERENCE_OMIT = "omit"
CASH_STANCE_BRIEF_FIELDS = {"version", "company_events", "offense_reference"}
CASH_STANCE_BRIEF_REQUIRED = {"version", "company_events"}
COMPANY_EVENTS_KEY = "company_events"

# 簡報鍵順序（閘門開啟時）：必讀欄位在前，公司事件摘要最後；evidence_recency 維持第一個。
# 不在清單內的鍵（例如 cash_buffer_by_stance）依原順序接在清單之後、公司事件摘要之前。
BRIEF_KEY_ORDER = (
    "evidence_recency",
    "market_level_evidence_ids", "citable_evidence_ids",
    "cash_grade_allowed_by_stance", "cash_grade_grid", "cash_ratio_anchor", "max_daily_raise", "cash_ratio_now",
    "cash_buffer_by_stance", "high_conviction_cash_floor_by_stance",
    "drawdown_control", "rules", "positions_by_breadth",
    "regime_assessment", "turn_facts", "turn_state", "market_index_facts", "exposure_facts",
    "offense_facts", "offense_cash_glide", "market_sentiment", "overnight_market",
    "analyst_outlooks", "tradability", "account",
)

COMPANY_EVENTS_NOTE = (
    "公司事件已縮成摘要：只列 materiality 為 high 的事件（含 evidence_ids，可引用）；其餘事件只在計數裡，不逐檔列出。"
)
REFERENCE_PHRASE = "五類事實與一組對照樣本"
REFERENCE_PHRASE_OMITTED = "五類事實"
BRIEF_PROMPT = (
    "本次簡報的公司事件已縮成摘要（各 event_outlook 檔數、materiality 與 direction 計數、只列 high 事件與省略計數）；"
    "引用公司事件時只用 high 事件的 evidence_ids。必讀欄位（citable_evidence_ids、現金格點等）排在前面。"
)


def cash_stance_brief_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """cash_stance_brief 啟用時回傳閘門物件；沒有閘門回 None（簡報、提示文字與 Schema 與舊版逐位元相同）。"""
    gate = sizing.get("cash_stance_brief") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def offense_reference_omitted(gate: Optional[Mapping[str, object]]) -> bool:
    return gate is not None and gate.get("offense_reference") == OFFENSE_REFERENCE_OMIT


def validate_cash_stance_brief_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """形狀 {version: v2, company_events: high_only, offense_reference?: omit}；offense_reference 需要 offense 閘門。"""
    gate = sizing["cash_stance_brief"]
    prefix = "position_sizing.cash_stance_brief"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, CASH_STANCE_BRIEF_FIELDS, prefix, errors)
    missing = sorted(CASH_STANCE_BRIEF_REQUIRED - set(gate))
    if missing:
        errors.append("%s 缺少欄位：%s" % (prefix, "、".join(missing)))
    if "version" in gate and gate["version"] != CASH_STANCE_BRIEF_VERSION:
        errors.append("%s.version 只允許 %s" % (prefix, CASH_STANCE_BRIEF_VERSION))
    if "company_events" in gate and gate["company_events"] != COMPANY_EVENTS_HIGH_ONLY:
        errors.append("%s.company_events 只允許 %s" % (prefix, COMPANY_EVENTS_HIGH_ONLY))
    if "offense_reference" in gate:
        if gate["offense_reference"] != OFFENSE_REFERENCE_OMIT:
            errors.append("%s.offense_reference 只允許 %s" % (prefix, OFFENSE_REFERENCE_OMIT))
        if "offense" not in sizing:
            errors.append("%s.offense_reference 需要 position_sizing.offense" % prefix)


def company_events_summary(items: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """事件分析師報告的摘要：各 event_outlook 檔數、materiality × direction 事件計數、只列 high 事件、省略計數。

    ``event_outlook_counts`` 加總等於交易池檔數（每個 item 一檔）；``omitted`` 是未列出的事件則數（非 high）。"""
    outlooks: Dict[str, int] = {}
    matrix: Dict[str, Dict[str, int]] = {}
    high: List[Dict[str, object]] = []
    omitted = 0
    for item in items:
        # 事件分析師報告的欄位是 outlook（舊簡報讀 event_outlook，實際恆為 null）；兩者都沒有時記 None。
        key = str(item.get("outlook", item.get("event_outlook")))
        outlooks[key] = outlooks.get(key, 0) + 1
        for event in item.get("events", []) or []:
            if not isinstance(event, Mapping):
                continue
            materiality = str(event.get("materiality"))
            direction = str(event.get("direction", "not_provided"))
            row = matrix.setdefault(materiality, {})
            row[direction] = row.get(direction, 0) + 1
            if materiality == "high":
                high.append({
                    "symbol": item.get("symbol"), "evidence_id": event.get("evidence_id"),
                    "summary": event.get("summary"), "direction": event.get("direction"),
                    "evidence_ids": [event.get("evidence_id")] if event.get("evidence_id") else [],
                })
            else:
                omitted += 1
    return {
        "note": COMPANY_EVENTS_NOTE,
        "symbols_total": len(items),
        "event_outlook_counts": dict(sorted(outlooks.items())),
        "materiality_by_direction": {m: dict(sorted(d.items())) for m, d in sorted(matrix.items())},
        "high_events": high,
        "omitted": {"non_high_events": omitted},
    }


def reorder_brief(brief: Mapping[str, object], summary: Mapping[str, object]) -> Dict[str, object]:
    """依 BRIEF_KEY_ORDER 重排簡報，並把公司事件換成摘要放最後。"""
    ordered: Dict[str, object] = {key: brief[key] for key in BRIEF_KEY_ORDER if key in brief}
    for key, value in brief.items():
        if key not in ordered and key != COMPANY_EVENTS_KEY:
            ordered[key] = value
    ordered[COMPANY_EVENTS_KEY] = dict(summary)
    return ordered
