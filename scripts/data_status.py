#!/usr/bin/env python3
import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="查看 ETF Agent 行情資料狀態")
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    args = parser.parse_args()
    if not args.database.exists():
        print("資料庫尚未建立：%s" % args.database)
        return 1

    connection = sqlite3.connect(str(args.database))
    connection.row_factory = sqlite3.Row
    try:
        summary = connection.execute(
            """
            SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols,
                   MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
            FROM daily_prices
            """
        ).fetchone()
        latest_run = connection.execute(
            """
            SELECT run_id, source, status, started_at, finished_at,
                   fetched_rows, stored_rows, warning_count, error_message
            FROM collection_runs ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()

    print("行情筆數：%d" % summary["rows"])
    print("股票數：%d" % summary["symbols"])
    print("日期範圍：%s ～ %s" % (summary["first_date"] or "無", summary["last_date"] or "無"))
    if latest_run:
        print("最近執行：%s / %s" % (latest_run["run_id"], latest_run["status"]))
        if latest_run["error_message"]:
            print("錯誤：%s" % latest_run["error_message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
