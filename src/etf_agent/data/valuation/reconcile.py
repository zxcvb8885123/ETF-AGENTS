"""確定性核對：從原始回應重建逐檔估值紀錄與歷史。收集器與驗證器共用同一份程式。

規則：官方當日值當標準答案；FinMind 同一資料日的 PER／PBR／殖利率（Decimal、兩位小數）完全相符才接受，
不符、任一邊缺、資料日對不上一律拒絕該檔，不取平均、不擇一、不填假值。
FinMind 在資料日之前的歷史無法逐日核對，標 ``unverified_history``。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core import canonical_sha256, decimal_string

from .contracts import (
    FINMIND_FIELDS, FINMIND_SOURCE, LICENSE_APPROVED_ON, LICENSE_SCOPE, LICENSE_STATUS, MAPPING_VERSION, METRICS,
    OFFICIAL_FIELDS, OFFICIAL_SOURCE, REASON_FINMIND_BAD_ROW, REASON_FINMIND_MISSING, REASON_FINMIND_UNAVAILABLE,
    REASON_MISMATCH, REASON_OFFICIAL_BAD_ROW, REASON_OFFICIAL_MISSING, ValuationError, normalize_metric, parse_roc_date,
    taipei_date,
)
from etf_agent.core import parse_aware_time

OFFICIAL_KEYS = {"per": "PEratio", "pbr": "PBratio", "dividend_yield": "DividendYield"}
FINMIND_KEYS = {"per": "PER", "pbr": "PBR", "dividend_yield": "dividend_yield"}


def parse_official(raw: str, fetched_at: str) -> Tuple[date, Dict[str, Mapping[str, object]]]:
    """解析官方全市場回應；整份欄位名變更、日期不一致或晚於抓取日即整份拒絕。"""
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValuationError("證交所估值回應不是 JSON") from error
    if not isinstance(rows, list) or not rows:
        raise ValuationError("證交所估值回應必須是非空陣列")
    fetched_day = taipei_date(parse_aware_time(fetched_at, "fetched_at", error=ValuationError))
    data_date = None
    by_code: Dict[str, Mapping[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not set(OFFICIAL_FIELDS) <= set(row):
            raise ValuationError("證交所估值欄位不完整或欄位名變更（需要 %s）" % "、".join(OFFICIAL_FIELDS))
        row_date = parse_roc_date(row["Date"])
        if data_date is None:
            data_date = row_date
        elif row_date != data_date:
            raise ValuationError("證交所估值回應資料日不一致")
        code = str(row["Code"]).strip()
        if not code or code in by_code:
            raise ValuationError("證交所估值代號空白或重複：%r" % code)
        by_code[code] = row
    if data_date > fetched_day:
        raise ValuationError("證交所估值資料日晚於抓取日：%s" % data_date.isoformat())
    return data_date, by_code


def parse_finmind(raw: str, code: str) -> List[Mapping[str, object]]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValuationError("FinMind PER 回應不是 JSON") from error
    if not isinstance(payload, Mapping) or payload.get("status") != 200 or not isinstance(payload.get("data"), list):
        raise ValuationError("FinMind PER 回應狀態或資料格式不正確")
    rows = []
    for item in payload["data"]:
        if not isinstance(item, Mapping) or not set(FINMIND_FIELDS) <= set(item) or item["stock_id"] != code:
            raise ValuationError("FinMind PER 欄位不完整、欄位名變更或股票代號不符")
        date.fromisoformat(str(item["date"]))
        rows.append(item)
    return rows


def _normalized(keys: Mapping[str, str], row: Mapping[str, object]):
    values, reasons = {}, {}
    for metric in METRICS:
        values[metric], reasons[metric] = normalize_metric(metric, row.get(keys[metric]))
    return values, reasons


def _text(value):
    return None if value is None else decimal_string(value)


def build_symbol_record(code: str, data_date: date, official_row: Optional[Mapping[str, object]],
                        finmind_response: Optional[Mapping[str, object]], fetched_at: str) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    """回傳 ``(逐檔紀錄, 歷史列)``。``finmind_response`` 是 ``{"raw": 回應文字}`` 或 ``{"error": 訊息}`` 或 None。"""
    record: Dict[str, object] = {
        "code": code, "data_date": data_date.isoformat(), "source": OFFICIAL_SOURCE, "history_source": FINMIND_SOURCE,
        "mapping_version": MAPPING_VERSION, "license_status": LICENSE_STATUS, "license_scope": LICENSE_SCOPE,
        "license_approved_on": LICENSE_APPROVED_ON, "published_at": None, "available_at": fetched_at, "fetched_at": fetched_at,
        "status": "rejected", "reject_reason": None, "values": {metric: None for metric in METRICS},
        "null_reasons": {}, "official_row_sha256": None, "finmind_row_sha256": None,
    }
    history: List[Dict[str, object]] = []
    if official_row is None:
        record["reject_reason"] = REASON_OFFICIAL_MISSING
        return _seal(record), history
    record["official_row_sha256"] = canonical_sha256(dict(official_row))
    try:
        official_values, official_reasons = _normalized(OFFICIAL_KEYS, official_row)
    except ValuationError:
        record["reject_reason"] = REASON_OFFICIAL_BAD_ROW
        return _seal(record), history
    if finmind_response is None or "raw" not in finmind_response:
        record["reject_reason"] = REASON_FINMIND_UNAVAILABLE
        return _seal(record), history
    try:
        rows = parse_finmind(str(finmind_response["raw"]), code)
        normalized = [(item, *_normalized(FINMIND_KEYS, item)) for item in rows]
    except (ValuationError, ValueError):
        record["reject_reason"] = REASON_FINMIND_BAD_ROW
        return _seal(record), history
    same_day = [entry for entry in normalized if entry[0]["date"] == data_date.isoformat()]
    if len(same_day) != 1:
        record["reject_reason"] = REASON_FINMIND_MISSING
        return _seal(record), history
    day_row, finmind_values, _ = same_day[0]
    record["finmind_row_sha256"] = canonical_sha256(dict(day_row))
    if finmind_values != official_values:
        record["reject_reason"] = REASON_MISMATCH
        record["mismatched_metrics"] = sorted(m for m in METRICS if finmind_values[m] != official_values[m])
        return _seal(record), history
    record["status"] = "matched"
    record["values"] = {metric: _text(official_values[metric]) for metric in METRICS}
    record["null_reasons"] = {metric: reason for metric, reason in official_reasons.items() if reason}
    for item, values, reasons in sorted(normalized, key=lambda entry: entry[0]["date"]):
        history.append({
            "code": code, "date": item["date"], "values": {m: _text(values[m]) for m in METRICS},
            "null_reasons": {m: r for m, r in reasons.items() if r},
            # 只有資料日當天與官方逐值核對過；更早的歷史只能推定口徑相同。
            "verification": "verified_against_official" if item["date"] == data_date.isoformat() else "unverified_history",
            "available_at": fetched_at, "row_sha256": canonical_sha256(dict(item)),
        })
    return _seal(record), history


def _seal(record: Dict[str, object]) -> Dict[str, object]:
    body = {key: value for key, value in record.items() if key not in {"available_at", "fetched_at"}}
    record["content_sha256"] = canonical_sha256(body)
    return record


def build_run(official_raw: Mapping[str, object], finmind_raw: Mapping[str, object], codes: Sequence[str]):
    """從原始回應重建 ``(records, history, audit)``。"""
    fetched_at = str(official_raw["fetched_at"])
    data_date, by_code = parse_official(str(official_raw["raw"]), fetched_at)
    responses = finmind_raw.get("responses", {})
    records, history = [], []
    for code in codes:
        record, rows = build_symbol_record(code, data_date, by_code.get(code), responses.get(code), fetched_at)
        records.append(record)
        history.extend(rows)
    reasons: Dict[str, int] = {}
    for record in records:
        if record["status"] != "matched":
            reasons[str(record["reject_reason"])] = reasons.get(str(record["reject_reason"]), 0) + 1
    audit = {
        "data_date": data_date.isoformat(), "requested": len(codes), "matched": sum(r["status"] == "matched" for r in records),
        "rejected": sum(r["status"] != "matched" for r in records), "reject_reasons": dict(sorted(reasons.items())),
        "official_row_count": len(by_code), "mapping_version": MAPPING_VERSION,
    }
    return {"data_date": data_date.isoformat(), "records": records}, {"history": history}, audit
