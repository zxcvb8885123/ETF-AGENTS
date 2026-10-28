"""Date-partitioned run layout: ``<root>/<YYYY-MM-DD>/<run_id>`` (Asia/Taipei dates).

The date folder is layout only. Run IDs, manifests and hashes never include it,
so moving a run between date folders cannot change any sealed content.

Callers that know the run's target trading day (``target_session.start``) should
pass it as ``day=`` so the folder matches that day, not the day the run executed
on — see ``trade_date_from_session``/``trade_date_from_session_start`` below.
Lookups always scan via ``find_run_dir``/``list_run_dirs``, so this choice never
breaks finding an existing run.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Mapping, Optional, Type

from .timeutil import parse_aware_time

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
DAY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_UTC_STAMP = re.compile(r"(?<![0-9A-Za-z])(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})\d*Z")
_PLAIN_DAY = re.compile(r"(?<![0-9A-Za-z])(20\d{2})-?(0[1-9]|1[0-2])-?(0[1-9]|[12]\d|3[01])(?![0-9A-Za-z])")


def taipei_day(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("日期資料夾需要含時區的時間")
    return moment.astimezone(TAIPEI).strftime("%Y-%m-%d")


def day_from_run_id(run_id: str) -> Optional[str]:
    """從 run_id 內的日期推出台北日期；`...T095000Z` 視為 UTC，純日期照寫；沒有日期則回傳 None。"""
    stamp = _UTC_STAMP.search(run_id)
    if stamp:
        year, month, day, hour, minute, second = (int(part) for part in stamp.groups())
        return taipei_day(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc))
    plain = _PLAIN_DAY.search(run_id)
    if plain:
        return "%s-%s-%s" % plain.groups()
    return None


def trade_date_from_session_start(start: Optional[str]) -> Optional[str]:
    """``target_session.start``（含時區 ISO 8601）換算的台北交易日；缺少或無法解析回傳 None。"""
    if not isinstance(start, str) or not start.strip():
        return None
    try:
        return taipei_day(parse_aware_time(start, "target_session.start"))
    except ValueError:
        return None


def trade_date_from_session(session: Optional[Mapping[str, object]]) -> Optional[str]:
    """``target_session``（含 ``start``）換算的台北交易日；缺少時回傳 None，由呼叫端退回執行日期。"""
    if not isinstance(session, Mapping):
        return None
    return trade_date_from_session_start(session.get("start"))


def day_dirs(root: Path) -> List[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir() and DAY_PATTERN.fullmatch(path.name))


def list_run_dirs(root: Path) -> List[Path]:
    """所有日期資料夾底下的 run 目錄（略過暫存的 `.` 開頭目錄）。"""
    return sorted(
        run
        for day in day_dirs(root)
        for run in day.iterdir()
        if run.is_dir() and not run.name.startswith(".")
    )


def find_run_dir(root: Path, run_id: str, error: Type[Exception] = ValueError) -> Optional[Path]:
    """在日期資料夾中找 run；0 筆回傳 None，同一 run_id 出現在多個日期即拒絕。"""
    matches = [day / run_id for day in day_dirs(root) if (day / run_id).exists()]
    if len(matches) > 1:
        raise error("run_id 出現在多個日期資料夾：%s" % "、".join(str(path) for path in matches))
    return matches[0] if matches else None
