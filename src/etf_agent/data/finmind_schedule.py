"""FinMind 財報候選的抓取排程：首次補歷史，之後只在財報公布區間內每天更新。

FinMind 財報只是候選資料，不進正式 Snapshot；抓取時間即 available_at，補抓歷史不會回填到過去。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Optional

# 法定公告期限前後：年報 3/31、Q1 5/15、Q2 8/14、Q3 11/14；期限前少數公司提早公布，期限後留幾天給補件。
FILING_WINDOWS = (((2, 15), (4, 10)), ((4, 20), (5, 25)), ((7, 20), (8, 24)), ((10, 20), (11, 24)))
DEFAULT_HISTORY_YEARS = 3
MIN_HISTORY_YEARS, MAX_HISTORY_YEARS = 2, 5


class FinMindScheduleError(ValueError):
    pass


def in_filing_window(today: date) -> bool:
    return any(date(today.year, *start) <= today <= date(today.year, *end) for start, end in FILING_WINDOWS)


def history_start(today: date, years: int) -> date:
    if not MIN_HISTORY_YEARS <= years <= MAX_HISTORY_YEARS:
        raise FinMindScheduleError("FinMind 歷史財報年數必須介於 %d 與 %d" % (MIN_HISTORY_YEARS, MAX_HISTORY_YEARS))
    return date(today.year - years, 1, 1)


def load_state(path: Path) -> Dict[str, Dict[str, str]]:
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise FinMindScheduleError("FinMind 排程狀態無法讀取：%s" % error) from error
    if not isinstance(state, dict):
        raise FinMindScheduleError("FinMind 排程狀態格式不正確")
    return state


def plan(state: Dict[str, Dict[str, str]], dataset: str, today: date, *,
         years: int = DEFAULT_HISTORY_YEARS, force: bool = False) -> Dict[str, Optional[str]]:
    """回傳 {action: backfill|update|skip, start, reason}。"""
    wanted = history_start(today, years)
    entry = state.get(dataset) or {}
    try:
        backfilled_from = date.fromisoformat(entry["backfilled_from"])
        last_done = date.fromisoformat(entry["last_done"])
    except (KeyError, ValueError):
        return {"action": "backfill", "start": wanted.isoformat(), "reason": "尚未補過歷史財報"}
    if backfilled_from > wanted:
        return {"action": "backfill", "start": wanted.isoformat(), "reason": "歷史年數增加，補更早的財報"}
    if force:
        return {"action": "update", "start": date(today.year - 1, 1, 1).isoformat(), "reason": "強制更新"}
    if in_filing_window(today):
        if last_done >= today:
            return {"action": "skip", "start": None, "reason": "今天已更新過"}
        return {"action": "update", "start": date(today.year - 1, 1, 1).isoformat(), "reason": "財報公布區間內"}
    return {"action": "skip", "start": None, "reason": "不在財報公布區間"}


def record_done(path: Path, dataset: str, start: date, today: date) -> None:
    state = load_state(path)
    entry = dict(state.get(dataset) or {})
    previous = entry.get("backfilled_from")
    entry["backfilled_from"] = min(start.isoformat(), previous) if previous else start.isoformat()
    entry["last_done"] = today.isoformat()
    state[dataset] = entry
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".finmind_schedule.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
