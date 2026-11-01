#!/usr/bin/env python3
"""資料抓取互斥：取得 artifacts/daily/.data.lock 後才執行指令，讓 20:00 資料、20:40 新聞與 21:00 決策自己的抓資料不會同時寫資料庫。

用法：``python3 scripts/with_data_lock.py [--wait-seconds N] -- 指令 參數...``
鎖被佔用時每 5 秒重試，最多等 N 秒（預設 1800，環境變數 ETF_DATA_LOCK_WAIT_SECONDS 可覆寫）；逾時以結束碼 75 停止並通知，
不硬闖。只用標準函式庫，Windows 沒有 fcntl 時直接執行指令（沒有互斥）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

ROOT = Path(__file__).resolve().parents[1]
LOCK_RELATIVE_PATH = Path("artifacts") / "daily" / ".data.lock"
DEFAULT_WAIT_SECONDS = 1800.0
POLL_SECONDS = 5.0
TIMEOUT_EXIT = 75


def acquire(
    path: Path, wait_seconds: float, poll_seconds: float = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
):
    """取得獨占鎖並回傳開啟中的檔案（關閉即釋放）；等到逾時仍被佔用回 None。非「鎖被佔用」的 OSError 往上拋。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    deadline = monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if monotonic() >= deadline:
                handle.close()
                return None
            sleep(poll_seconds)
            continue
        except BaseException:
            handle.close()
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "command": " ".join(sys.argv[1:])[:200]}).encode("utf-8"))
        handle.flush()
        return handle


def notify(message: str) -> None:
    if sys.platform != "darwin":
        return
    script = "display notification %s with title %s" % (json.dumps(message), json.dumps("ETF Agent 資料排程"))
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def main(argv: Optional[Sequence[str]] = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description="取得資料抓取鎖後執行指令")
    parser.add_argument("--wait-seconds", type=float, default=float(os.environ.get("ETF_DATA_LOCK_WAIT_SECONDS", DEFAULT_WAIT_SECONDS)))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("需要在 -- 之後提供要執行的指令")
    if args.wait_seconds < 0:
        parser.error("--wait-seconds 不得為負")
    if fcntl is None:
        return subprocess.run(command, check=False).returncode
    try:
        handle = acquire(root / LOCK_RELATIVE_PATH, args.wait_seconds)
    except OSError as error:
        print("無法取得資料抓取鎖：%s" % error, file=sys.stderr)
        notify("無法取得資料抓取鎖：%s" % str(error)[:100])
        return 1
    if handle is None:
        message = "另一個資料抓取或每日流程仍佔用鎖，等待 %d 秒後放棄：%s" % (args.wait_seconds, " ".join(command)[:80])
        print(message, file=sys.stderr)
        notify(message)
        return TIMEOUT_EXIT
    try:
        return subprocess.run(command, check=False).returncode
    finally:
        handle.close()


if __name__ == "__main__":
    sys.exit(main())
