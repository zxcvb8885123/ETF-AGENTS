"""論點檢視結論（交易 Agent 的背景參考，確定性讀取與驗證）。

策略反思每週逐檔檢視「當初買進的理由現在還成立嗎」，把每檔結論（``intact``／``weakened``／``broken``）與一句摘要
存成 ``var/strategy_reflection/thesis_verdicts.json``。交易 Agent 讀它當**背景**，只給仍持有的股票：

- 不是證據，不能被 claim 引用，不能放進理由當事實依據；今天的多空論點與分析師發現永遠優先；
- 沒有任何可被引用的識別碼（摘要含證據／claim 識別碼或網址的項目整筆丟棄）；
- 結論是上週五以前的決策日內容，簡報會附 ``as_of``；超過 ``MAX_AGE_DAYS`` 或建立時間晚於決策截止時間一律視同沒有。

本模組只讀與驗證，不寫檔；寫入由 ``strategy_reflection`` 負責（決策鏈不匯入反思套件）。
檔案缺失、格式不符、雜湊不符一律視為「沒有結論」（fail-closed），不影響決策。
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Collection, Dict, List, Mapping, Optional

from etf_agent.core import canonical_sha256, parse_aware_time

VERDICTS_PATH = Path("var") / "strategy_reflection" / "thesis_verdicts.json"
SCHEMA_VERSION = "1.0"
USABLE_VERDICTS = ("intact", "weakened", "broken")  # unknown 沒有資訊，不給交易 Agent
MAX_AGE_DAYS = 8
FILE_FIELDS = {"schema_version", "verdicts_id", "created_at", "items", "content_sha256"}
ITEM_FIELDS = {"symbol", "verdict", "summary"}
_FORBIDDEN = re.compile(r"review:|bull-|bear-|fund-|tech-|event-|document:|price:|evidence|https?://", re.IGNORECASE)


def usable_item(item: object) -> bool:
    return (
        isinstance(item, Mapping) and set(item) == ITEM_FIELDS and item["verdict"] in USABLE_VERDICTS
        and isinstance(item["symbol"], str) and bool(item["symbol"])
        and isinstance(item["summary"], str) and bool(item["summary"].strip()) and not _FORBIDDEN.search(item["summary"])
    )


def seal_verdicts(verdicts_id: str, created_at: str, review: Mapping[str, Mapping[str, object]]) -> Dict[str, object]:
    """由論點檢視結果建立可寫檔內容：只留可用結論，摘要不合格的項目丟棄。"""
    items = [
        {"symbol": str(item["symbol"]).upper(), "verdict": item["verdict"], "summary": str(item["summary"]).strip()}
        for item in review.values()
    ]
    body: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION, "verdicts_id": verdicts_id, "created_at": created_at,
        "items": sorted((item for item in items if usable_item(item)), key=lambda item: str(item["symbol"])),
    }
    body["content_sha256"] = canonical_sha256(body)
    return body


def validate_verdicts_file(data: object) -> List[str]:
    if not isinstance(data, Mapping):
        return ["論點檢視結論檔必須是物件"]
    if set(data) != FILE_FIELDS:
        return ["論點檢視結論檔欄位不符"]
    errors: List[str] = []
    if data["schema_version"] != SCHEMA_VERSION:
        errors.append("論點檢視結論檔 schema_version 不符")
    items = data["items"]
    if not isinstance(items, list) or not all(usable_item(item) for item in items):
        errors.append("論點檢視結論檔 items 含不合格項目")
    body = {key: value for key, value in data.items() if key != "content_sha256"}
    if data["content_sha256"] != canonical_sha256(body):
        errors.append("論點檢視結論檔 content_sha256 與內容不一致")
    return errors


def load_thesis_verdicts(root: Path, decision_cutoff: str) -> Optional[Dict[str, object]]:
    """{"verdicts_id", "as_of", "items", "sha256"}；沒有、不合格、太舊或晚於截止時間回 None。"""
    try:
        data = json.loads((Path(root) / VERDICTS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if validate_verdicts_file(data) or not data["items"]:
        return None
    try:
        created, cutoff = parse_aware_time(data["created_at"], "created_at"), parse_aware_time(decision_cutoff, "decision_cutoff")
    except ValueError:
        return None
    if created > cutoff or cutoff - created > timedelta(days=MAX_AGE_DAYS):
        return None
    return {"verdicts_id": data["verdicts_id"], "as_of": data["created_at"], "items": list(data["items"]), "sha256": data["content_sha256"]}


def verdicts_brief(verdicts: Optional[Mapping[str, object]], held_symbols: Collection[str]) -> Optional[Dict[str, object]]:
    """放進交易 brief 的背景：只給仍持有、且在這一批的股票；沒有任何可引用的識別碼。"""
    if verdicts is None:
        return None
    held = {symbol.upper() for symbol in held_symbols}
    items = [
        {"symbol": item["symbol"], "verdict": item["verdict"], "summary": item["summary"]}
        for item in verdicts["items"]  # type: ignore[union-attr]
        if str(item["symbol"]).upper() in held
    ]
    if not items:
        return None
    return {
        "note": "以下是策略反思每週對仍持有股票做的「買進理由是否還成立」檢視，只是背景參考（as_of 之前的決策日內容，可能已過時）：不是證據，"
                "不能引用，也不能放進理由當事實依據，不能覆蓋硬性規則與風控；與今天的分析師發現、多空論點衝突時以今天的為準。",
        "as_of": verdicts["as_of"], "items": items,
    }


def bind_forced_swap(policy: Dict[str, object], verdicts: Optional[Mapping[str, object]]) -> Dict[str, object]:
    """把「論點被推翻」名單綁進 Policy（只在樣板啟用 rotation.forced_swap 時）。

    強制換股只認 Policy 內的 ``broken_symbols``＋``verdicts_ref``，事後重建不必讀會變動的結論檔；沒有可用結論時
    不綁名單（不換股）。回傳新的 policy 物件，呼叫端須重算 content_sha256。"""
    import copy

    bound = copy.deepcopy(policy)
    sizing = bound.get("position_sizing")
    rotation = sizing.get("rotation") if isinstance(sizing, dict) else None
    forced = rotation.get("forced_swap") if isinstance(rotation, dict) else None
    if not isinstance(forced, dict) or verdicts is None:
        return bound
    broken = sorted({str(item["symbol"]).upper() for item in verdicts["items"] if item["verdict"] == "broken"})  # type: ignore[union-attr]
    if broken:
        forced["broken_symbols"] = broken
        forced["verdicts_ref"] = {"verdicts_id": verdicts["verdicts_id"], "as_of": verdicts["as_of"], "sha256": verdicts["sha256"]}
    return bound
