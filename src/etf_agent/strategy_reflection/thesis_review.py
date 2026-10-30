"""論點檢視（Agent 判斷，只出報告）：每檔持股，當初買進的理由現在還成立嗎？

``thesis.py`` 只算價格事實；「理由是否還成立」是判斷，交給 Agent，但邊界很窄：

- 輸入只有：當初的買進理由與重估條件、程式算好的價格事實，以及**最新一個決策日**同一檔股票的分析師發現與多空 claim
  （帶 finding_id／claim_id，可引用）。沒有任何其他資料來源，不得上網或引用記憶。
- 對每個重估條件逐條判斷 ``triggered`` ／ ``not_triggered`` ／ ``unknown``，``triggered`` 必須引用最新發現的 ID；
  整體 verdict 為 ``intact`` ／ ``weakened`` ／ ``broken`` ／ ``unknown``。
- 短期價格波動本身不是理由失效；價格事實只是背景，不能單獨讓 verdict 變成 weakened／broken。
- **只出報告，不影響交易**：結果不進 ``brief``、不進 policy，也不給任何數字或建議委託。驗證沒過的批次標 ``unknown``，
  不會中斷反思流程（這是旁觀者的輔助欄位，不是決策鏈的一環）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

from .contracts import ReflectionError

BATCH_SIZE = 10
VERDICTS = ("intact", "weakened", "broken", "unknown")
CONDITION_STATUS = ("triggered", "not_triggered", "unknown")
_TEXT = {"type": "string", "minLength": 1}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["symbol", "verdict", "conditions", "summary", "finding_ids"],
        "properties": {
            "symbol": _TEXT, "verdict": {"enum": list(VERDICTS)}, "summary": _TEXT,
            "finding_ids": {"type": "array", "items": _TEXT},
            "conditions": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["status", "finding_ids"],
                "properties": {"status": {"enum": list(CONDITION_STATUS)}, "finding_ids": {"type": "array", "items": _TEXT}}}},
        }}}},
}


def current_context(run_dir: Path) -> Dict[str, Dict[str, object]]:
    """最新決策日各股票可引用的內容：分析師發現（finding_id）與多空 claim（claim_id）。缺檔時回空（該檔判 unknown）。"""
    try:
        team = json.loads((Path(run_dir) / "team_inputs.json").read_text(encoding="utf-8"))
        debate = json.loads((Path(run_dir) / "debate.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    context: Dict[str, Dict[str, object]] = {}
    for analyst, report in (team.get("analyst_reports") or {}).items():
        for item in report.get("items", []):
            entry = context.setdefault(str(item["symbol"]).upper(), {"findings": {}, "claims": {}})
            for finding in item.get("findings", []):
                entry["findings"][str(finding["finding_id"])] = {"analyst": analyst, "outlook": item.get("outlook"), "text": finding["text"]}  # type: ignore[index]
    for packet in debate.get("packets", []):
        for item in packet.get("items", []):
            entry = context.setdefault(str(item["symbol"]).upper(), {"findings": {}, "claims": {}})
            for claim in item.get("claims", []):
                entry["claims"][str(claim["claim_id"])] = {"role": packet["role"], "text": claim["text"]}  # type: ignore[index]
    return context


def review_briefs(
    thesis: Sequence[Mapping[str, object]], context: Mapping[str, Mapping[str, object]],
) -> List[Dict[str, object]]:
    """每檔一筆輸入；``allowed_ids`` 是該檔最新決策日的 finding／claim ID，Agent 只能引用它們。"""
    rows = []
    for row in thesis:
        symbol = str(row["symbol"])
        now = context.get(symbol, {"findings": {}, "claims": {}})
        text = row.get("text") or {}
        rows.append({
            "symbol": symbol,
            "entry": {"day": row["entry_day"], "rationale": text.get("rationale"),  # type: ignore[union-attr]
                      "invalidation_conditions": list(text.get("invalidation_conditions", []))},  # type: ignore[union-attr]
            "price_facts": {key: row[key] for key in ("status", "days_held", "return_since_entry", "max_drawdown_since_entry", "tolerance") if key in row},
            "latest_findings": now["findings"], "latest_claims": now["claims"],
            "allowed_ids": sorted([*now["findings"], *now["claims"]]),  # type: ignore[misc]
        })
    return rows


def validate_review(output: Mapping[str, object], briefs: Sequence[Mapping[str, object]]) -> List[str]:
    """涵蓋每檔剛好一次；條件數量與順序和原重估條件一致；引用的 ID 必須屬於該檔最新內容；結論前後一致。"""
    errors: List[str] = []
    items = output.get("items")
    if not isinstance(items, list):
        return ["items 必須是陣列"]
    expected = {str(b["symbol"]): b for b in briefs}
    seen = [str(i.get("symbol", "")).upper() for i in items if isinstance(i, Mapping)]
    if sorted(seen) != sorted(expected):
        errors.append("必須涵蓋每一檔剛好一次：期望 %s，實際 %s" % (sorted(expected), sorted(seen)))
        return errors
    for item in items:
        symbol = str(item["symbol"]).upper()
        brief = expected[symbol]
        allowed = set(brief["allowed_ids"])  # type: ignore[arg-type]
        conditions = item["conditions"]
        if len(conditions) != len(brief["entry"]["invalidation_conditions"]):  # type: ignore[index]
            errors.append("%s conditions 必須和原重估條件逐條對應（%d 條）" % (symbol, len(brief["entry"]["invalidation_conditions"])))  # type: ignore[index]
            continue
        for ids in [item["finding_ids"], *[c["finding_ids"] for c in conditions]]:
            unknown = sorted(set(ids) - allowed)
            if unknown:
                errors.append("%s 引用不屬於該檔最新內容的 ID：%s" % (symbol, ", ".join(unknown)))
        triggered = [c for c in conditions if c["status"] == "triggered"]
        if any(not c["finding_ids"] for c in triggered):
            errors.append("%s 判定重估條件被觸發必須引用最新發現的 ID" % symbol)
        if item["verdict"] == "broken" and not triggered:
            errors.append("%s verdict=broken 至少要有一個被觸發的重估條件" % symbol)
        if item["verdict"] == "intact" and triggered:
            errors.append("%s 有重估條件被觸發，verdict 不能是 intact" % symbol)
        if item["verdict"] in {"weakened", "broken"} and not item["finding_ids"]:
            errors.append("%s verdict=%s 必須引用最新發現的 ID" % (symbol, item["verdict"]))
    return errors


def unknown_items(briefs: Sequence[Mapping[str, object]], reason: str) -> List[Dict[str, object]]:
    """沒有檢視成功的股票：標 unknown 並寫明原因，不填假結論。"""
    return [{"symbol": b["symbol"], "verdict": "unknown", "summary": reason, "finding_ids": [],
             "conditions": [{"status": "unknown", "finding_ids": []} for _ in b["entry"]["invalidation_conditions"]]}  # type: ignore[index]
            for b in briefs]


def batches(briefs: Sequence[Mapping[str, object]], size: int = BATCH_SIZE) -> List[List[Mapping[str, object]]]:
    if size < 1:
        raise ReflectionError("批次大小必須為正")
    return [list(briefs[i:i + size]) for i in range(0, len(briefs), size)]
