"""策略教訓（交易 Agent 的背景參考，確定性讀取與驗證）。

策略反思每週回頭檢視過去買進的股票，把「哪裡有問題、之後該怎麼做」整理成最多 3 條短教訓，存成
``config/strategy_lessons.json``。交易 Agent 每天讀它當**背景**：

- 不是證據，不能被 claim 引用，不能放進理由當事實依據；
- 不能覆蓋硬性規則、風控、Validator 與競賽限制（那些仍由程式強制）；
- 教訓只能是一般性的做法建議，不含個股代號、年份、識別碼或網址。

本模組只讀與驗證，不寫檔；寫入與試用觀察由 ``strategy_reflection`` 負責（決策鏈不匯入反思套件）。
檔案缺失、格式不符、雜湊不符或任何一條教訓不合格，一律視為「沒有教訓」（fail-closed），不影響決策。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from etf_agent.core import canonical_sha256, parse_aware_time

LESSONS_PATH = Path("config") / "strategy_lessons.json"
SCHEMA_VERSION = "1.0"
MAX_LESSONS = 3
MIN_TEXT = 10
MAX_TEXT = 120
FILE_FIELDS = {"schema_version", "lessons_id", "created_at", "lessons", "content_sha256"}
LESSON_FIELDS = {"text", "evidence_ids"}
# 個股代號（4 碼以上數字）與各種證據／claim 識別碼、網址：教訓必須是一般性做法，不得夾帶。
_FORBIDDEN = re.compile(r"(?<!\d)\d{4,}(?!\d)|review:|bull-|bear-|fund-|tech-|event-|document:|price:|evidence|https?://", re.IGNORECASE)


def lesson_text_errors(text: object) -> List[str]:
    if not isinstance(text, str) or not text.strip():
        return ["教訓必須是非空文字"]
    errors = []
    if not MIN_TEXT <= len(text.strip()) <= MAX_TEXT:
        errors.append("教訓長度必須在 %d～%d 字之間" % (MIN_TEXT, MAX_TEXT))
    if _FORBIDDEN.search(text):
        errors.append("教訓不得含個股代號、年份、識別碼或網址：%s" % text[:30])
    return errors


def seal_lessons(lessons_id: str, created_at: str, lessons: List[Mapping[str, object]]) -> Dict[str, object]:
    body: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION, "lessons_id": lessons_id, "created_at": created_at,
        "lessons": [{"text": str(item["text"]).strip(), "evidence_ids": list(item["evidence_ids"])} for item in lessons],
    }
    body["content_sha256"] = canonical_sha256(body)
    return body


def validate_lessons_file(data: object) -> List[str]:
    """結構、數量、文字規則與雜湊；回傳錯誤清單（空＝合格）。"""
    if not isinstance(data, Mapping):
        return ["教訓檔必須是物件"]
    errors: List[str] = []
    if set(data) != FILE_FIELDS:
        errors.append("教訓檔欄位不符：多出 %s、缺少 %s" % (sorted(set(data) - FILE_FIELDS), sorted(FILE_FIELDS - set(data))))
        return errors
    if data["schema_version"] != SCHEMA_VERSION:
        errors.append("教訓檔 schema_version 不符")
    lessons = data["lessons"]
    if not isinstance(lessons, list) or len(lessons) > MAX_LESSONS:
        return errors + ["教訓必須是最多 %d 條的陣列" % MAX_LESSONS]
    for index, item in enumerate(lessons):
        if not isinstance(item, Mapping) or set(item) != LESSON_FIELDS:
            errors.append("lessons[%d] 欄位不符" % index)
            continue
        errors.extend("lessons[%d] %s" % (index, error) for error in lesson_text_errors(item["text"]))
        if not isinstance(item["evidence_ids"], list) or not item["evidence_ids"]:
            errors.append("lessons[%d] 至少要有一個 evidence_ids" % index)
    body = {key: value for key, value in data.items() if key != "content_sha256"}
    if data["content_sha256"] != canonical_sha256(body):
        errors.append("教訓檔 content_sha256 與內容不一致")
    return errors


def load_strategy_lessons(root: Path, decision_cutoff: Optional[str] = None) -> Optional[Dict[str, object]]:
    """交易 Agent 的背景教訓：{"lessons_id", "texts", "sha256"}；沒有或不合格回 None（不影響決策）。

    給了 ``decision_cutoff`` 時，建立時間晚於截止時間（或無法判讀）的教訓視同沒有，避免重跑舊 Snapshot 讀到後來才產生的經驗。"""
    path = Path(root) / LESSONS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if validate_lessons_file(data) or not data["lessons"]:
        return None
    if decision_cutoff is not None:
        try:
            if parse_aware_time(data["created_at"], "created_at") > parse_aware_time(decision_cutoff, "decision_cutoff"):
                return None
        except ValueError:
            return None
    return {"lessons_id": data["lessons_id"], "texts": [str(item["text"]) for item in data["lessons"]], "sha256": data["content_sha256"]}


def lessons_brief(lessons: Optional[Mapping[str, object]]) -> Optional[Dict[str, object]]:
    """放進交易 brief 的背景：只給文字與使用規則，不給 ID（沒有任何可被引用的識別碼）。"""
    if lessons is None:
        return None
    return {
        "note": "以下是策略反思回頭檢視過去買進後整理的一般性做法建議，只是背景參考：不是證據，不能放進理由當事實依據，"
                "不能引用，也不能覆蓋硬性規則、風控與驗證；與今天的多空論點衝突時以今天的論點為準。",
        "lessons": list(lessons["texts"]),  # type: ignore[arg-type]
    }
