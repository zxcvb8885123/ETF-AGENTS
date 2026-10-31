"""策略教訓的產生、套用與還原（反思旁觀者）。

每週五，反思 Agent 回頭看過去買進的股票，用**只含數值與列舉**的整理（買進信心、持有天數、報酬、期間回落、論點檢視結論、
被觸發的重估條件數），歸納最多 3 條一般性做法建議（例如「高信心買進後多半回吐，買進前先確認回檔」）。
輸入不含新聞、理由或 claim 文字，教訓因此不會被不受信任的內容誘導；每條教訓必須引用簡報內存在的 evidence_id。

教訓存成 ``config/strategy_lessons.json``（格式與讀取見 ``decision.strategy_lessons``），週六與旋鈕提案一樣經
``apply_pending`` 套用，並進入同一套試用觀察期：5 個新交易日後，相對等權基準明確變差就還原成套用前的內容。
教訓只是交易 Agent 的背景參考，不是證據，也不改硬性規則；一次只有一件變更在試用，不同時改旋鈕與教訓。
"""

from __future__ import annotations

import shutil
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.decision.strategy_lessons import (
    LESSONS_PATH, MAX_LESSONS, lesson_text_errors, load_strategy_lessons, seal_lessons, validate_lessons_file,
)

from .contracts import ReflectionError
from .repository import atomic_write_json

LESSONS_ACTION = "lessons_apply"
REVERT_ACTION = "lessons_revert"
PENDING_NAME = "pending.json"
_TEXT = {"type": "string", "minLength": 1}
LESSONS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["lessons"],
    "properties": {"lessons": {"type": "array", "maxItems": MAX_LESSONS, "items": {
        "type": "object", "additionalProperties": False, "required": ["text", "evidence_ids"],
        "properties": {"text": _TEXT, "evidence_ids": {"type": "array", "items": _TEXT, "minItems": 1}}}}},
}
MIN_GROUP = 3  # 依買進信心分組至少這麼多檔才給分組平均，避免一兩檔當趨勢


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def thesis_aggregates(
    thesis: Sequence[Mapping[str, object]], review: Mapping[str, Mapping[str, object]],
) -> Dict[str, str]:
    """持股整體與分組的數值／列舉統計，鍵即 evidence_id（``thesis.*``）。沒有持股回空字典。"""
    rows = [row for row in thesis if "return_since_entry" in row]
    evidence: Dict[str, str] = {}
    if not rows:
        return evidence
    returns = [Decimal(str(row["return_since_entry"])) for row in rows]
    drawdowns = [Decimal(str(row["max_drawdown_since_entry"])) for row in rows]
    days = [Decimal(str(row["days_held"])) for row in rows]
    evidence["thesis.holdings"] = str(len(rows))
    evidence["thesis.mean_return"] = str(_mean(returns).quantize(Decimal("0.000001")))
    evidence["thesis.mean_max_drawdown"] = str(_mean(drawdowns).quantize(Decimal("0.000001")))
    evidence["thesis.mean_days_held"] = str(_mean(days).quantize(Decimal("0.01")))
    evidence["thesis.losing_holdings"] = str(sum(1 for value in returns if value < 0))
    for state in ("on_track", "within_tolerance", "needs_review"):
        evidence["thesis.state.%s" % state] = str(sum(1 for row in thesis if row["status"] == state))
    for verdict in ("intact", "weakened", "broken", "unknown"):
        evidence["thesis.verdict.%s" % verdict] = str(sum(1 for item in review.values() if item.get("verdict") == verdict))
    evidence["thesis.triggered_conditions"] = str(sum(
        1 for item in review.values() for cond in item.get("conditions", []) if cond.get("status") == "triggered"))  # type: ignore[union-attr]
    for level in ("high", "medium", "low"):
        group = [Decimal(str(row["return_since_entry"])) for row in rows if row.get("entry_conviction") == level]
        if len(group) >= MIN_GROUP:
            evidence["thesis.conviction.%s.count" % level] = str(len(group))
            evidence["thesis.conviction.%s.mean_return" % level] = str(_mean(group).quantize(Decimal("0.000001")))
    return evidence


def lessons_brief_for_agent(
    thesis: Sequence[Mapping[str, object]], review: Mapping[str, Mapping[str, object]],
    reflection_evidence: Mapping[str, str], current: Optional[Mapping[str, object]],
) -> Dict[str, object]:
    """教訓 Agent 的簡報。``evidence`` 只放 ``thesis.*`` 聚合與反思簡報內 ``diag.*``／``cal.*`` 的數值；
    ``holdings`` 逐檔只給列舉與數值（買進信心、持有天數、報酬、回落、狀態、論點結論），沒有任何自由文字。"""
    evidence = {**{key: value for key, value in reflection_evidence.items() if key.startswith(("diag.", "cal."))},
                **thesis_aggregates(thesis, review)}
    holdings = []
    for row in thesis:
        if "return_since_entry" not in row:
            continue
        verdict = review.get(str(row["symbol"]).upper(), {})
        holdings.append({
            "entry_conviction": row.get("entry_conviction"), "entry_intent": row.get("entry_intent"), "days_held": row["days_held"],
            "return_since_entry": row["return_since_entry"], "max_drawdown_since_entry": row["max_drawdown_since_entry"],
            "state": row["status"], "thesis_verdict": verdict.get("verdict", "unknown"),
            "triggered_conditions": sum(1 for cond in verdict.get("conditions", []) if cond.get("status") == "triggered"),  # type: ignore[union-attr]
        })
    return {"evidence": evidence, "holdings": holdings, "current_lessons": list((current or {}).get("texts", [])),  # type: ignore[arg-type]
            "max_lessons": MAX_LESSONS}


def lessons_from_output(
    output: Mapping[str, object], brief: Mapping[str, object], now: datetime,
) -> Optional[Dict[str, object]]:
    """Agent 輸出轉成教訓檔（含雜湊）。每條教訓需符合文字規則並引用簡報內的 evidence_id；
    與目前教訓完全相同或沒有教訓回 None（本週不變更）。違規丟 ReflectionError。"""
    items = output.get("lessons")
    if not isinstance(items, list) or len(items) > MAX_LESSONS:
        raise ReflectionError("lessons 必須是最多 %d 條的陣列" % MAX_LESSONS)
    evidence = brief["evidence"]
    for index, item in enumerate(items):
        errors = lesson_text_errors(item["text"])
        missing = sorted(set(item["evidence_ids"]) - set(evidence))  # type: ignore[arg-type]
        if missing:
            errors.append("引用不存在的 evidence_id：%s" % ", ".join(missing))
        if errors:
            raise ReflectionError("lessons[%d] %s" % (index, "；".join(errors)))
    if not items:
        return None
    if [str(item["text"]).strip() for item in items] == list(brief["current_lessons"]):  # type: ignore[arg-type]
        return None
    return seal_lessons("lessons-%s" % now.strftime("%Y%m%dT%H%M%SZ"), now.isoformat(), items)


def pending_path(base: Path) -> Path:
    return Path(base) / "lessons" / PENDING_NAME


def save_pending(base: Path, lessons: Mapping[str, object]) -> Path:
    path = pending_path(base)
    atomic_write_json(path, dict(lessons))
    return path


def load_pending(base: Path) -> Optional[Dict[str, object]]:
    import json

    try:
        data = json.loads(pending_path(base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if not validate_lessons_file(data) else None


def apply_lessons(root: Path, base: Path, lessons: Mapping[str, object], history_append, decision_days: int, now: datetime) -> Dict[str, object]:
    """把教訓檔原子寫入 config/（先備份目前版本）並記錄歷史，進入試用觀察期。呼叫端負責取鎖與時段保護。"""
    errors = validate_lessons_file(lessons)
    if errors:
        raise ReflectionError("教訓檔不合格：%s" % "；".join(errors))
    live = Path(root) / LESSONS_PATH
    backup: Optional[Path] = None
    if live.exists():
        backup = Path(base) / "lessons" / "backups" / ("%s.json" % lessons["lessons_id"])
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live, backup)
    # 交易 Agent 以 created_at 判斷教訓何時可用：重新封存為實際套用時間，週五提案的時間不能讓週六才生效的教訓倒灌回週五的 Snapshot。
    effective = seal_lessons(str(lessons["lessons_id"]), now.isoformat(), lessons["lessons"])  # type: ignore[arg-type]
    # 先記歷史再換 live 檔：中途中止時留下「有紀錄」的狀態（試用與冷卻仍生效），而不是沒有紀錄的 live 變更。
    history_append({
        "action": LESSONS_ACTION, "applied_at": now.isoformat(), "reflection_id": effective["lessons_id"], "knobs": ["strategy_lessons"],
        "decision_days": decision_days, "backup": str(backup) if backup else None, "had_previous": backup is not None,
        "sha256_after": effective["content_sha256"], "trial": {"verification": "observe"},
    })
    try:
        atomic_write_json(live, effective)
    except BaseException:
        history_append({"action": REVERT_ACTION, "reverted_at": now.isoformat(), "of": effective["lessons_id"]})
        raise
    return {"applied": True, "lessons_id": lessons["lessons_id"], "count": len(effective["lessons"])}  # type: ignore[arg-type]


def revert_lessons(root: Path, entry: Mapping[str, object], history_append, now: datetime) -> Dict[str, object]:
    """還原成套用前的教訓（沒有前一版就刪檔）。現有檔不是該次套用的結果（被人改過）時不動，丟 ReflectionError。"""
    live = Path(root) / LESSONS_PATH
    current = load_strategy_lessons(root)
    if live.exists() and (current is None or current["sha256"] != entry["sha256_after"]):
        raise ReflectionError("目前的教訓檔不是該次套用的結果（或內容已被手動修改），請人工處理")
    if entry.get("had_previous"):
        shutil.copy2(Path(str(entry["backup"])), live)
    elif live.exists():
        live.unlink()
    history_append({"action": REVERT_ACTION, "reverted_at": now.isoformat(), "of": entry["reflection_id"]})
    return {"reverted": entry["reflection_id"]}
