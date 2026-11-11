"""事件分析師結果重用（同一交易日目標時段、輸入逐檔完全相同才沿用）。

每天會跑兩次完整決策（晚上與早上）。同一個目標交易日的前一次 run 已經為「輸入完全相同」的股票產生並通過驗證的事件分析結果，
再叫一次 Agent 只是重複花費。這裡由程式做確定性比對：

- 逐檔比對本次事件簡報列（含重大訊息、新聞、財務脈絡、資料可得狀態）與前一次簡報列的內容雜湊，完全相同才算同輸入。
- 只沿用前一次 ``analyst_event.json`` 內該檔已通過驗證的項目；呼叫端還會用本次 bundle 與 Validator 重驗，驗不過就整批不重用。
- 任何一欄不同（例如隔夜出現新重大訊息）都照舊交給 Agent；找不到前一次 run 或檔案毀損一律不重用，不填假值。
"""

import copy
import json
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from etf_agent.core import canonical_sha256

ANALYST_REPORT = "analyst_event.json"
BRIEF_GLOB = "brief_event_b*.json"


def row_digest(row: Mapping[str, object]) -> str:
    return canonical_sha256(dict(row))


def _read_json(path: Path) -> Optional[object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def previous_run_dir(run_dir: Path) -> Optional[Path]:
    """同一交易日資料夾內、名稱排序在本次 run 之前且有事件報告與簡報的最近一次；沒有就回 None。"""
    parent = run_dir.parent
    if not parent.is_dir():
        return None
    for sibling in sorted((path for path in parent.iterdir() if path.is_dir() and path.name < run_dir.name), key=lambda p: p.name, reverse=True):
        if (sibling / ANALYST_REPORT).is_file() and any(sibling.glob(BRIEF_GLOB)):
            return sibling
    return None


def _previous_rows(previous: Path) -> Dict[str, Mapping[str, object]]:
    rows: Dict[str, Mapping[str, object]] = {}
    for path in sorted(previous.glob(BRIEF_GLOB)):
        brief = _read_json(path)
        if not isinstance(brief, Mapping) or not isinstance(brief.get("symbols"), list):
            continue
        for row in brief["symbols"]:
            if isinstance(row, Mapping) and isinstance(row.get("symbol"), str):
                rows[row["symbol"]] = row
    return rows


def reusable_event_items(
    run_dir: Path, current_rows: Mapping[str, Mapping[str, object]], candidates: Sequence[str],
) -> Tuple[Optional[str], List[Dict[str, object]]]:
    """回傳 (來源 run 名稱, 可沿用的事件項目)；沒有可用來源或沒有任何相同的股票時回 (None, [])。

    ``current_rows`` 是本次事件簡報的逐檔列（已含 data_availability），``candidates`` 是本來要交給 Agent 的股票。"""
    previous = previous_run_dir(run_dir)
    if previous is None:
        return None, []
    report = _read_json(previous / ANALYST_REPORT)
    if not isinstance(report, Mapping) or report.get("status") != "completed" or not isinstance(report.get("items"), list):
        return None, []
    old_items = {item["symbol"]: item for item in report["items"] if isinstance(item, Mapping) and isinstance(item.get("symbol"), str)}
    old_rows = _previous_rows(previous)
    reused: List[Dict[str, object]] = []
    for symbol in candidates:
        old_row, new_row, item = old_rows.get(symbol), current_rows.get(symbol), old_items.get(symbol)
        if old_row is None or new_row is None or item is None:
            continue
        if row_digest(old_row) == row_digest(new_row):
            reused.append(copy.deepcopy(dict(item)))
    return (previous.name, reused) if reused else (None, [])
