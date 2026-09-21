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
        has_analysis_view = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name='analysis_daily_prices'"
        ).fetchone()
        table = "analysis_daily_prices" if has_analysis_view else "daily_prices"
        summary = connection.execute(
            """
            SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols,
                   MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
            FROM %s
            """ % table
        ).fetchone()
        latest_symbols = connection.execute(
            "SELECT COUNT(DISTINCT symbol) AS value FROM %s WHERE trade_date=(SELECT MAX(trade_date) FROM %s)"
            % (table, table)
        ).fetchone()["value"]
        sources = connection.execute(
            "SELECT source, COUNT(*) AS rows FROM %s GROUP BY source ORDER BY rows DESC"
            % table
        ).fetchall()
        latest_run = connection.execute(
            """
            SELECT run_id, source, status, started_at, finished_at,
                   fetched_rows, stored_rows, warning_count, error_message
            FROM collection_runs ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
        has_documents = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_documents'"
        ).fetchone()
        if has_documents:
            documents = connection.execute(
                """
                SELECT COUNT(*) AS rows,
                       COUNT(DISTINCT source || '|' || external_id) AS logical_rows,
                       SUM(document_type = 'monthly_revenue') AS monthly_rows,
                       SUM(document_type = 'material_event') AS event_rows
                FROM source_documents
                """
            ).fetchone()
            snapshots = connection.execute(
                "SELECT COUNT(*) AS value FROM research_snapshots"
            ).fetchone()["value"]
            issues = connection.execute(
                "SELECT COUNT(*) AS value FROM quality_issues"
            ).fetchone()["value"]
        else:
            documents = None
            snapshots = 0
            issues = 0
    finally:
        connection.close()

    print("行情筆數：%d" % summary["rows"])
    print("股票數：%d" % summary["symbols"])
    print("日期範圍：%s ～ %s" % (summary["first_date"] or "無", summary["last_date"] or "無"))
    print("最新交易日股票數：%d" % latest_symbols)
    print("來源：" + ", ".join("%s=%d" % (row["source"], row["rows"]) for row in sources))
    if documents:
        print(
            "公司文件：%d 版本 / %d 筆資料（月營收 %d、重大訊息 %d）"
            % (
                documents["rows"],
                documents["logical_rows"],
                documents["monthly_rows"] or 0,
                documents["event_rows"] or 0,
            )
        )
        print("研究快照：%d；品質問題：%d" % (snapshots, issues))
    if latest_run:
        print("最近執行：%s / %s" % (latest_run["run_id"], latest_run["status"]))
        if latest_run["error_message"]:
            print("錯誤：%s" % latest_run["error_message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
