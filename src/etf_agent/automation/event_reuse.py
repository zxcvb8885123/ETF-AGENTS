"""事件分析師結果重用（同一交易日目標時段、輸入逐檔完全相同才沿用）。

每天會跑兩次完整決策（晚上與早上）。同一個目標交易日的前一次 run 已經為「輸入完全相同」的股票產生並通過驗證的事件分析結果，
再叫一次 Agent 只是重複花費。這裡由程式做確定性比對：

- 逐檔比對本次事件簡報列（含重大訊息、新聞、財務脈絡；不含 ``data_availability`` 的儲存筆數與時間）與前一次簡報列的內容雜湊，完全相同才算同輸入。
- 只沿用前一次 ``analyst_event.json`` 內該檔已通過驗證的項目；呼叫端還會用本次 bundle 與 Validator 重驗，驗不過就整批不重用。
- 任何一欄不同（例如隔夜出現新重大訊息）都照舊交給 Agent；找不到前一次 run 或檔案毀損一律不重用，不填假值。
"""

import copy
import json
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from etf_agent.core import canonical_sha256

ANALYST_REPORT = "analyst_event.json"
BRIEF_GLOB = "brief_event_b*.json"
REUSE_RECORD = "event_reuse.json"


def _without_availability(row: Mapping[str, object]) -> Dict[str, object]:
    """比對時不看 ``data_availability``：它只是資料庫的儲存筆數與最早、最晚取得時間，隔夜多抓一筆就會不同，
    但事件與新聞本身在 ``events``／``news`` 欄位，那些仍逐項比對；任何事件或新聞欄位不同都照舊不沿用。"""
    return {key: value for key, value in row.items() if key != "data_availability"}


def row_digest(row: Mapping[str, object]) -> str:
    return canonical_sha256(_without_availability(row))


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


def _folder_date(path: Path) -> Optional[date]:
    try:
        return datetime.strptime(path.name, "%Y-%m-%d").date()
    except ValueError:
        return None


def cross_day_chain(run_dir: Path, max_days: int) -> List[Path]:
    """本次 run 之前、已完成事件報告的 run，由新到舊：同一交易日資料夾內名稱較早者，再加上日曆日距離不超過 ``max_days`` 的較早日期資料夾。

    只認有 ``analyst_event.json`` 的 run（該檔只在合併驗證通過後才寫出）；資料夾名稱不是日期或超出天數者不納入。"""
    today = _folder_date(run_dir.parent)
    root = run_dir.parent.parent
    if today is None or max_days < 1 or not root.is_dir():
        return []
    runs: List[Tuple[date, str, Path]] = []
    for day_dir in root.iterdir():
        day = _folder_date(day_dir)
        if day is None or not day_dir.is_dir() or day > today or (today - day).days > max_days:
            continue
        for run in day_dir.iterdir():
            if not run.is_dir() or (day == today and run.name >= run_dir.name):
                continue
            if (run / ANALYST_REPORT).is_file():
                runs.append((day, run.name, run))
    runs.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in runs]


def _completed_items(run: Path) -> Optional[Dict[str, Mapping[str, object]]]:
    report = _read_json(run / ANALYST_REPORT)
    if not isinstance(report, Mapping) or report.get("status") != "completed" or not isinstance(report.get("items"), list):
        return None
    return {item["symbol"]: item for item in report["items"] if isinstance(item, Mapping) and isinstance(item.get("symbol"), str)}


def _reused_symbols(run: Path) -> Set[str]:
    record = _read_json(run / REUSE_RECORD)
    if isinstance(record, Mapping) and isinstance(record.get("symbols"), list):
        return {symbol for symbol in record["symbols"] if isinstance(symbol, str)}
    return set()


def _cross_day_source(chain: Sequence[Path], symbol: str) -> Optional[Tuple[Path, Mapping[str, object], Mapping[str, object]]]:
    """沿鏈由新到舊找 ``symbol`` 最後一次真正交給 Agent 的 run，回傳 (該 run, 當時的簡報列, 當時的事件項目)。

    中途的 run 若是「沿用」該檔（出現在 ``event_reuse.json``）就繼續往前，因為沿用只在輸入逐檔相同時才發生；
    若中途的 run 既沒有該檔簡報列、也不是沿用（例如當時沒有事件而由程式輸出 unknown），輸入已經變過，立刻停止、不沿用。"""
    for run in chain:
        items = _completed_items(run)
        if items is None:
            continue
        row = _previous_rows(run).get(symbol)
        if row is not None:
            item = items.get(symbol)
            return (run, row, item) if item is not None else None
        if symbol in _reused_symbols(run) and symbol in items:
            continue
        return None
    return None


def reusable_event_items(
    run_dir: Path, current_rows: Mapping[str, Mapping[str, object]], candidates: Sequence[str],
    cross_day_max_days: Optional[int] = None,
) -> Tuple[Optional[str], List[Dict[str, object]]]:
    """回傳 (來源 run 名稱, 可沿用的事件項目)；沒有可用來源或沒有任何相同的股票時回 (None, [])。

    ``current_rows`` 是本次事件簡報的逐檔列（已含 data_availability），``candidates`` 是本來要交給 Agent 的股票。
    ``cross_day_max_days`` 為 None 時只看同一交易日資料夾內的前一次 run；填整數時改為逐檔沿鏈往前找（可跨交易日，
    最多回看這麼多個日曆日，且以「最後一次真正交給 Agent 判斷」的 run 起算）。"""
    if cross_day_max_days is not None:
        return _reusable_cross_day(run_dir, current_rows, candidates, cross_day_max_days)
    previous = previous_run_dir(run_dir)
    if previous is None:
        return None, []
    old_items = _completed_items(previous)
    if old_items is None:
        return None, []
    old_rows = _previous_rows(previous)
    reused: List[Dict[str, object]] = []
    for symbol in candidates:
        old_row, new_row, item = old_rows.get(symbol), current_rows.get(symbol), old_items.get(symbol)
        if old_row is None or new_row is None or item is None:
            continue
        if row_digest(old_row) == row_digest(new_row):
            reused.append(copy.deepcopy(dict(item)))
    return (previous.name, reused) if reused else (None, [])


def _reusable_cross_day(
    run_dir: Path, current_rows: Mapping[str, Mapping[str, object]], candidates: Sequence[str], max_days: int,
) -> Tuple[Optional[str], List[Dict[str, object]]]:
    chain = cross_day_chain(run_dir, max_days)
    if not chain:
        return None, []
    reused: List[Dict[str, object]] = []
    sources: List[str] = []
    for symbol in candidates:
        new_row = current_rows.get(symbol)
        found = _cross_day_source(chain, symbol) if new_row is not None else None
        if found is None:
            continue
        run, old_row, item = found
        if row_digest(old_row) != row_digest(new_row):
            continue
        reused.append(copy.deepcopy(dict(item)))
        label = "%s/%s" % (run.parent.name, run.name)
        if label not in sources:
            sources.append(label)
    return ("；".join(sources), reused) if reused else (None, [])
