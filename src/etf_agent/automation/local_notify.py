"""本機通知與一行狀態紀錄，供每日排程腳本與 D-Plan 看門狗共用。

- 通知只用 macOS `display notification`（不用會阻塞排程的 `display alert`／`display dialog`），
  任何失敗都只回傳 False，不拋出，絕不讓排程 run 因通知失敗而失敗。
- 狀態紀錄是 `var/logs/daily_status.log` 的 TSV 一行：ISO 時間（台北）、run 類型、最終狀態、目標交易日、
  run_id、D-Plan 路徑或原因。只附加、不改寫；寫不進去同樣只回傳 None。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

TAIPEI = timezone(timedelta(hours=8))
STATUS_LOG_NAME = "daily_status.log"
NOTIFY_TIMEOUT_SECONDS = 15  # osascript 偶爾會卡在通知中心；不能讓排程 run 因通知而停住
# 最終狀態的白名單；看門狗與文件都以此為準。
RUN_STATUSES = ("approved", "no_trade", "rejected", "blocked", "fallback", "failed", "skipped")
DETAIL_MAX_CHARS = 500


def _applescript_quote(text: str) -> str:
    """AppleScript 字串字面值：只跳脫反斜線與雙引號、換行轉空白；中文原樣保留（不能用 json 的 \\uXXXX）。"""
    clean = str(text).replace("\r", " ").replace("\n", " ")
    return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str, *, log: Optional[Callable[[str], None]] = None,
           timeout: float = NOTIFY_TIMEOUT_SECONDS, platform: Optional[str] = None) -> bool:
    """送出本機通知；回傳是否成功。非 macOS、osascript 不存在、逾時、非零結束（含通知權限被拒）都回 False。"""
    if (platform or sys.platform) != "darwin":
        return False
    script = "display notification %s with title %s" % (_applescript_quote(message), _applescript_quote(title))
    try:
        completed = subprocess.run(["osascript", "-e", script], check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        if log is not None:
            log("[通知] 本機通知未送出（不影響流程）：%s" % error)
        return False
    if completed.returncode != 0:
        if log is not None:
            log("[通知] osascript 結束碼 %d（不影響流程）：%s" % (completed.returncode, (completed.stderr or "").strip()[-200:]))
        return False
    return True


def classify_run_type(now: datetime) -> str:
    """排程 run 的類型：中午前是「morning」（07:00 重做當日時段），其餘是「evening」（21:00 做隔天備案）。"""
    if now.tzinfo is None:
        raise ValueError("now 必須含時區")
    return "morning" if now.astimezone(TAIPEI).time() < time(12, 0) else "evening"


def format_status_line(now: datetime, run_type: str, status: str, trade_date: object, run_id: object, detail: object) -> str:
    """一行 TSV；detail 去掉換行與多餘空白並截斷，確保一筆剛好一行。"""
    if now.tzinfo is None:
        raise ValueError("now 必須含時區")
    clean = re.sub(r"\s+", " ", str(detail or "")).strip()[:DETAIL_MAX_CHARS]
    return "\t".join([
        now.astimezone(TAIPEI).isoformat(timespec="seconds"),
        "run_type=%s" % run_type,
        "status=%s" % status,
        "trade_date=%s" % (trade_date or "-"),
        "run_id=%s" % (run_id or "-"),
        "detail=%s" % (clean or "-"),
    ])


def append_status_line(logs_root: Path, line: str, *, log: Optional[Callable[[str], None]] = None) -> Optional[Path]:
    """附加一行到 ``logs_root/daily_status.log``；寫不進去只記日誌並回傳 None。"""
    path = Path(logs_root) / STATUS_LOG_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as error:
        if log is not None:
            log("[狀態] 無法寫入 %s（不影響流程）：%s" % (path, error))
        return None
    return path


def parse_status_line(line: str) -> dict:
    """把 ``format_status_line`` 的一行還原成 dict（``at`` 加上各 key=value）；格式不符回空 dict。"""
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 2:
        return {}
    record = {"at": parts[0]}
    for part in parts[1:]:
        key, _, value = part.partition("=")
        if key:
            record[key] = value
    return record
