#!/usr/bin/env python3
"""決定 FinMind 財報資料集今天要不要抓，並記錄完成狀態。"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data.finmind_schedule import (  # noqa: E402
    DEFAULT_HISTORY_YEARS,
    FinMindScheduleError,
    load_state,
    plan,
    record_done,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "done"))
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--state", type=Path, default=ROOT / "artifacts" / "data" / "finmind_schedule.json")
    parser.add_argument("--start", type=date.fromisoformat, help="done：本次實際使用的起日")
    parser.add_argument("--years", type=int, default=int(os.environ.get("FINMIND_YEARS", DEFAULT_HISTORY_YEARS)))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    today = datetime.now(timezone(timedelta(hours=8))).date()
    try:
        if args.command == "plan":
            result = plan(load_state(args.state), args.dataset, today, years=args.years, force=args.force)
            # 單行輸出供 start.sh 解析：動作|起日|原因
            print("%s|%s|%s" % (result["action"], result["start"] or "", result["reason"]))
        else:
            if args.start is None:
                parser.error("done 需要 --start")
            record_done(args.state, args.dataset, args.start, today)
    except FinMindScheduleError as error:
        print("FinMind 排程失敗：%s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
