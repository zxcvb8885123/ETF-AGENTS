"""交易背景封存（``position_sizing.trader_background_record`` 版本閘門，紀錄用、不影響配置）。

交易 Agent 的簡報有兩段背景：策略教訓 ``strategy_lessons``（``config/strategy_lessons.json``）與論點檢視
``thesis_review_background``（``var/strategy_reflection/thesis_verdicts.json``）。兩個來源檔每週會被策略反思改寫，
舊做法每次 run 當場讀檔、沒有封存，事後無法知道交易 Agent 看到哪一版。本模組把兩份檔案的完整內容存進 run 目錄的
``trader_background.json``，並以選配鍵 ``team_inputs.trader_background`` 進 Decision run（做法同 ``valuation_facts``：
不升 ``team_inputs`` 版本、與 Policy 閘門雙向同時存在）。

Finalizer 重算的是：封存內容是否自洽（欄位白名單、內容雜湊、教訓與結論檔各自的格式與雜湊）、時間是否合法
（建立時間不晚於 ``decision_cutoff``、結論檔未超過有效天數）、與 Policy 綁定的 ``agent_swap.verdicts_ref`` 是否一致。
**限制**：它無法證明來源檔當時沒有別的版本；來源檔事後被改不影響封存。

閘門只由人設定、策略反思不得改；沒有閘門的舊 Policy／舊 run：簡報、``team_inputs`` 與重建逐位元不變。
背景不是證據：簡報形狀（``lessons_brief``／``verdicts_brief``）不變，仍沒有任何可引用的識別碼。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from etf_agent.core import canonical_sha256, parse_aware_time

from .fields import reject_unknown_fields
from .strategy_lessons import lessons_from_file, load_strategy_lessons_file, validate_lessons_file
from .thesis_verdicts import MAX_AGE_DAYS, load_thesis_verdicts_file, validate_verdicts_file, verdicts_from_file

TRADER_BACKGROUND_V1 = "v1"
TRADER_BACKGROUND_GATE = "trader_background_record"
TRADER_BACKGROUND_KEY = "trader_background"  # team_inputs 的選配鍵：與 Policy 閘門雙向同時存在
TRADER_BACKGROUND_FILE = "trader_background"  # run 目錄的封存檔名（trader_ 開頭，研究員拒讀樣式 trader_* 涵蓋）
GATE_FIELDS = {"version"}
RECORD_FIELDS = {"version", "strategy_lessons", "thesis_verdicts", "content_sha256"}


def trader_background_gate(sizing: object) -> Optional[Mapping[str, object]]:
    gate = sizing.get(TRADER_BACKGROUND_GATE) if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def validate_trader_background_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """閘門形狀：{version: v1}；沒有 Policy 層前提（兩個來源檔都沒有時紀錄兩欄為 None，不讓決策失敗）。"""
    gate = sizing[TRADER_BACKGROUND_GATE]
    prefix = "position_sizing.%s" % TRADER_BACKGROUND_GATE
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, GATE_FIELDS, prefix, errors)
    if gate.get("version") != TRADER_BACKGROUND_V1:
        errors.append("%s.version 只允許 %s" % (prefix, TRADER_BACKGROUND_V1))


def _seal(body: Dict[str, object]) -> Dict[str, object]:
    body["content_sha256"] = canonical_sha256(body)
    return body


def build_trader_background(root: Path, bundle: Mapping[str, object]) -> Dict[str, object]:
    """讀兩個來源檔的完整內容並封存；可用與否的判斷與 ``load_strategy_lessons``／``load_thesis_verdicts`` 完全一致。"""
    cutoff = str(bundle["decision_cutoff"])
    return _seal({
        "version": TRADER_BACKGROUND_V1,
        "strategy_lessons": load_strategy_lessons_file(root, cutoff),
        "thesis_verdicts": load_thesis_verdicts_file(root, cutoff),
    })


def validate_trader_background(record: object, bundle: Mapping[str, object], policy: Mapping[str, object]) -> List[str]:
    """封存紀錄的自洽性、時間合法性與 verdicts_ref 一致性；回傳錯誤清單（空＝合格）。"""
    prefix = "team_inputs.trader_background"
    if not isinstance(record, Mapping):
        return ["%s 必須是物件" % prefix]
    errors: List[str] = []
    reject_unknown_fields(record, RECORD_FIELDS, prefix, errors)
    if errors or set(record) != RECORD_FIELDS:
        return errors or ["%s 欄位不符：缺少 %s" % (prefix, sorted(RECORD_FIELDS - set(record)))]
    if record["version"] != TRADER_BACKGROUND_V1:
        errors.append("%s.version 只允許 %s" % (prefix, TRADER_BACKGROUND_V1))
    body = {key: value for key, value in record.items() if key != "content_sha256"}
    if record["content_sha256"] != canonical_sha256(body):
        errors.append("%s.content_sha256 與內容不一致" % prefix)
    try:
        cutoff = parse_aware_time(bundle["decision_cutoff"], "decision_cutoff")
    except (KeyError, TypeError, ValueError):
        return errors + ["%s 無法判讀 decision_cutoff" % prefix]
    lessons, verdicts = record["strategy_lessons"], record["thesis_verdicts"]
    if lessons is not None:
        lesson_errors = validate_lessons_file(lessons)
        errors.extend("%s.strategy_lessons：%s" % (prefix, error) for error in lesson_errors)
        if not lesson_errors:
            if not lessons["lessons"]:
                errors.append("%s.strategy_lessons 沒有教訓時必須記 null" % prefix)
            errors.extend(_created_at_errors(prefix + ".strategy_lessons", lessons, cutoff))
    if verdicts is not None:
        verdict_errors = validate_verdicts_file(verdicts)
        errors.extend("%s.thesis_verdicts：%s" % (prefix, error) for error in verdict_errors)
        if not verdict_errors:
            if not verdicts["items"]:
                errors.append("%s.thesis_verdicts 沒有結論時必須記 null" % prefix)
            time_errors = _created_at_errors(prefix + ".thesis_verdicts", verdicts, cutoff)
            errors.extend(time_errors)
            if not time_errors and cutoff - parse_aware_time(verdicts["created_at"], "created_at") > timedelta(days=MAX_AGE_DAYS):
                errors.append("%s.thesis_verdicts 超過有效天數 %d 天，應記 null" % (prefix, MAX_AGE_DAYS))
    sizing = policy.get("position_sizing")
    swap = sizing.get("agent_swap") if isinstance(sizing, Mapping) else None
    ref = swap.get("verdicts_ref") if isinstance(swap, Mapping) else None
    if isinstance(ref, Mapping):
        if not isinstance(verdicts, Mapping) or verdicts.get("content_sha256") != ref.get("sha256"):
            errors.append("%s.thesis_verdicts 必須與 DecisionPolicy 綁定的 agent_swap.verdicts_ref 雜湊一致" % prefix)
    return errors


def _created_at_errors(label: str, data: Mapping[str, object], cutoff) -> List[str]:
    try:
        if parse_aware_time(data["created_at"], "created_at") > cutoff:
            return ["%s 建立時間晚於 decision_cutoff" % label]
    except (KeyError, TypeError, ValueError):
        return ["%s 建立時間無法判讀" % label]
    return []


def lessons_from_record(record: Optional[Mapping[str, object]]) -> Optional[Dict[str, object]]:
    """與 ``load_strategy_lessons`` 同形狀：{"lessons_id", "texts", "sha256"}；紀錄沒有教訓回 None。"""
    data = record.get("strategy_lessons") if record is not None else None
    return None if data is None else lessons_from_file(data)


def verdicts_from_record(record: Optional[Mapping[str, object]]) -> Optional[Dict[str, object]]:
    """與 ``load_thesis_verdicts`` 同形狀：{"verdicts_id", "as_of", "items", "sha256"}；紀錄沒有結論回 None。"""
    data = record.get("thesis_verdicts") if record is not None else None
    return None if data is None else verdicts_from_file(data)
