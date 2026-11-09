"""風險 Agent 現金姿態的「資料新舊」時間表（cash_stance_recency v1 版本閘門）。

只依 bundle 重算、不含任何新聞或公告文字：每個市場層級證據的內容時間（新聞發布、時段收盤、資料日）、
距截止的小時數與分組。分組界線是台股最近交易日 13:30（與隔夜背景的 ``window_start`` 同一條線）。
內容時間晚於截止即拒絕，不靜默丟棄。Agent 不輸出任何數字，只填一個列舉並引用最新一組的證據。
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Mapping, Optional, Set

from etf_agent.core import parse_aware_time

from .fields import DecisionToolError, reject_unknown_fields

CASH_STANCE_RECENCY = "v1"
CASH_STANCE_RECENCY_FIELDS = {"version"}
RECENCY_RECONCILIATIONS = ("newest_confirms", "newest_contradicts", "newest_insufficient")
BUCKET_AFTER_CLOSE = "after_latest_close"
BUCKET_LATEST_SESSION = "latest_session"
BUCKET_ACCUMULATED = "accumulated"
BUCKETS = (BUCKET_AFTER_CLOSE, BUCKET_LATEST_SESSION, BUCKET_ACCUMULATED)
RECENCY_NOTE = (
    "分組只描述資料的內容時間（新聞發布、時段收盤、資料日），不是抓取時間，也不是程式判定的重要性。"
    "after_latest_close 的事件與新聞發生在最近一次台股收盤之後，走勢還沒反映；判斷情況有沒有改變時預設以它們為主，"
    "利多利空同樣適用，不採用時在 rationale 說明理由。新不等於已被市場消化，也可能失敗。"
)
_TENTH = Decimal("0.1")
_SECONDS_PER_HOUR = Decimal(3600)


def cash_stance_recency_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """cash_stance_recency 啟用時回傳閘門物件；沒有閘門回 None（欄位白名單與舊版完全相同）。"""
    gate = sizing.get("cash_stance_recency") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def validate_cash_stance_recency_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """閘門形狀：{version: v1}；前提（typed_evidence_v1）在重建現金姿態時檢查。"""
    gate = sizing["cash_stance_recency"]
    prefix = "position_sizing.cash_stance_recency"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, CASH_STANCE_RECENCY_FIELDS, prefix, errors)
    if gate.get("version") != CASH_STANCE_RECENCY:
        errors.append("%s.version 只允許 %s" % (prefix, CASH_STANCE_RECENCY))


def _hours(cutoff: datetime, content_time: datetime) -> str:
    seconds = Decimal(str((cutoff - content_time).total_seconds()))
    return str((seconds / _SECONDS_PER_HOUR).quantize(_TENTH, rounding=ROUND_HALF_UP))


def _time_entry(evidence_id: str, kind: str, content_time: datetime, cutoff: datetime, boundary: datetime) -> Dict[str, object]:
    if content_time > cutoff:
        raise DecisionToolError("evidence_recency：%s 的內容時間晚於 decision_cutoff" % evidence_id)
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "content_time": content_time.isoformat(),
        "hours_before_cutoff": _hours(cutoff, content_time),
        "bucket": BUCKET_AFTER_CLOSE if content_time > boundary else BUCKET_LATEST_SESSION,
    }


def evidence_recency(bundle: Mapping[str, object], market_evidence_ids: Set[str]) -> Dict[str, object]:
    """依 bundle 重算市場層級證據的內容時間表；找不到內容時間的證據記入 ``data_gaps``，不猜。"""
    from .input_context import MARKET_REGIME_EVIDENCE_ID
    from etf_agent.perception.market_news.tools import prepare
    from etf_agent.perception.overnight_market.tools import window_start

    cutoff = parse_aware_time(bundle.get("decision_cutoff"), "decision_cutoff", error=DecisionToolError)
    snapshot = bundle.get("snapshot")
    trade_date = snapshot.get("latest_trade_date") if isinstance(snapshot, Mapping) else None
    if not isinstance(trade_date, str):
        raise DecisionToolError("evidence_recency 需要 snapshot.latest_trade_date")
    boundary = window_start(trade_date)

    times: Dict[str, Dict[str, object]] = {}
    news_input = bundle.get("market_news_input")
    if isinstance(news_input, Mapping) and isinstance(news_input.get("bundle"), Mapping):
        pack = news_input["bundle"]
        for item in prepare(pack["captures"], pack["window_start"], pack["decision_cutoff"])["items"]:
            published = parse_aware_time(item["published_at"], "published_at", error=DecisionToolError)
            times[item["item_id"]] = _time_entry(item["item_id"], "news", published, cutoff, boundary)
    overnight = bundle.get("overnight_market_input")
    if isinstance(overnight, Mapping) and isinstance(overnight.get("result"), Mapping):
        for component in overnight["result"].get("components") or []:
            if isinstance(component, Mapping) and component.get("evidence_id") and component.get("session_end"):
                ended = parse_aware_time(component["session_end"], "session_end", error=DecisionToolError)
                times[str(component["evidence_id"])] = _time_entry(
                    str(component["evidence_id"]), "overnight", ended, cutoff, boundary
                )
    fear_greed = bundle.get("market_fear_greed_input")
    if isinstance(fear_greed, Mapping) and isinstance(fear_greed.get("result"), Mapping):
        for component in fear_greed["result"].get("components") or []:
            if isinstance(component, Mapping) and component.get("evidence_id") and component.get("as_of"):
                as_of = str(component["as_of"])
                if as_of > trade_date:
                    raise DecisionToolError("evidence_recency：%s 的資料日晚於台股最近交易日" % component["evidence_id"])
                times[str(component["evidence_id"])] = {
                    "evidence_id": str(component["evidence_id"]),
                    "kind": "fear_greed",
                    "content_date": as_of,
                    "bucket": BUCKET_LATEST_SESSION if as_of == trade_date else BUCKET_ACCUMULATED,
                }
    times[MARKET_REGIME_EVIDENCE_ID] = {
        "evidence_id": MARKET_REGIME_EVIDENCE_ID,
        "kind": "regime",
        "content_date": trade_date,
        "basis": "交易池 MA20／MA60 廣度，累積到台股最近交易日收盤",
        "bucket": BUCKET_ACCUMULATED,
    }

    buckets: Dict[str, List[Dict[str, object]]] = {name: [] for name in BUCKETS}
    data_gaps: List[str] = []
    for evidence_id in sorted(market_evidence_ids):
        entry = times.get(evidence_id)
        if entry is None:
            data_gaps.append("%s 找不到內容時間，無法分組" % evidence_id)
            continue
        buckets[str(entry["bucket"])].append(entry)
    for rows in buckets.values():
        rows.sort(key=lambda row: (Decimal(str(row.get("hours_before_cutoff", "999999"))), str(row["evidence_id"])))
    return {
        "version": CASH_STANCE_RECENCY,
        "decision_cutoff": cutoff.isoformat(),
        "boundary": boundary.isoformat(),
        "buckets": buckets,
        "data_gaps": data_gaps,
        "note": RECENCY_NOTE,
    }


def validate_recency_reconciliation(
    recency: Mapping[str, object], stance: Mapping[str, object], evidence: List[str], prefix: str, errors: List[str]
) -> None:
    """列舉必填且在值域內；最新一組非空時不得是 newest_insufficient 且至少引用其中一個證據，為空時只能是 newest_insufficient。
    不驗文字品質，也不要求列舉與姿態之間有對應（現金下限不由訊號決定）。"""
    value = stance.get("recency_reconciliation")
    if value not in RECENCY_RECONCILIATIONS:
        errors.append("%s.recency_reconciliation 必須是 %s" % (prefix, "／".join(RECENCY_RECONCILIATIONS)))
        return
    newest = [str(row["evidence_id"]) for row in recency["buckets"][BUCKET_AFTER_CLOSE]]  # type: ignore[index]
    if not newest:
        if value != "newest_insufficient":
            errors.append("%s.recency_reconciliation：最新一組（after_latest_close）沒有證據，只能填 newest_insufficient" % prefix)
        return
    if value == "newest_insufficient":
        errors.append(
            "%s.recency_reconciliation：最新一組有證據，不能填 newest_insufficient；"
            "請改填 newest_confirms 或 newest_contradicts，並引用其中至少一個證據（%s）" % (prefix, "、".join(newest[:5]))
        )
    elif not set(evidence) & set(newest):
        errors.append(
            "%s 最新一組（after_latest_close）有證據時，evidence_ids 至少要引用其中一個；"
            "可選：%s。引用後請在 rationale 說明你如何取捨" % (prefix, "、".join(newest[:5]))
        )
