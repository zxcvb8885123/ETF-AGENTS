#!/usr/bin/env python3
"""Deterministic entry point for the event-data skill."""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def find_project_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "src" / "etf_agent").is_dir():
            return parent
    raise RuntimeError("找不到 ETF_AGENTS 專案根目錄")


ROOT = find_project_root()
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import (  # noqa: E402
    CorporateDataCollector,
    DataAgentService,
    MarketDataDatabase,
    OfficialCorporateProvider,
    load_universe,
)


def add_shared_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database", type=Path, default=ROOT / "var" / "etf_agent.db"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Event Data Agent：收集官方事件資料並建立研究快照"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="抓取官方月營收與重大訊息")
    add_shared_arguments(collect)
    collect.add_argument(
        "--universe", type=Path, default=ROOT / "data" / "official_universe.csv"
    )

    snapshot = commands.add_parser("snapshot", help="建立指定截止時間的研究快照")
    add_shared_arguments(snapshot)
    snapshot.add_argument(
        "--decision-cutoff",
        default=datetime.now(timezone.utc).isoformat(),
        help="必須包含時區，例如 2026-09-16T13:30:00+08:00",
    )
    snapshot.add_argument("--run-id")
    snapshot.add_argument("--output", type=Path)
    snapshot.add_argument(
        "--allow-missing-prices",
        action="store_true",
        help="研究或測試用；沒有完整行情時仍建立可用快照",
    )

    status = commands.add_parser("status", help="顯示 Data Agent 資料狀態")
    add_shared_arguments(status)
    return parser


def collect_data(database_path: Path, universe_path: Path) -> int:
    config = json.loads(
        (ROOT / "config" / "data_sources.json").read_text(encoding="utf-8")
    )["corporate_data"]
    providers = [
        OfficialCorporateProvider(
            source=item["source"],
            url=item["url"],
            market=item["market"],
            document_type=item["document_type"],
            timeout_seconds=int(config["timeout_seconds"]),
            user_agent=config["user_agent"],
        )
        for item in config["sources"]
    ]
    collector = CorporateDataCollector(MarketDataDatabase(database_path), providers)
    try:
        result = collector.collect(load_universe(universe_path))
    except Exception as error:
        print("抓取失敗：%s" % error, file=sys.stderr)
        return 1

    print("官方回應：%d 筆" % result.fetched_rows)
    print("新增文件：%d 筆" % result.stored_documents)
    print("重複文件：%d 筆" % result.duplicate_documents)
    print("警告：%d 筆" % result.warning_count)
    print("run_id：%s" % result.run_id)
    for warning in result.warnings[:10]:
        print("WARN  " + warning)
    if len(result.warnings) > 10:
        print("WARN  另有 %d 筆警告未顯示" % (len(result.warnings) - 10))
    return 0


def build_snapshot(args: argparse.Namespace) -> int:
    try:
        snapshot = DataAgentService(MarketDataDatabase(args.database)).build_snapshot(
            args.decision_cutoff,
            run_id=args.run_id,
            require_prices=not args.allow_missing_prices,
        )
    except Exception as error:
        print("建立失敗：%s" % error, file=sys.stderr)
        return 1

    output = json.dumps(snapshot.as_dict(), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
        print("研究快照：%s" % args.output)
    else:
        print(output)
    print("可用：%s" % ("是" if snapshot.usable else "否"))
    print("文件：%d 筆" % len(snapshot.documents))
    print(
        "最新行情覆蓋：%d / %d"
        % (snapshot.latest_price_symbols, snapshot.universe_size)
    )
    for flag in snapshot.quality_flags:
        print("QUALITY  " + flag)
    return 0 if snapshot.usable else 2


def show_status(database_path: Path) -> int:
    if not database_path.exists():
        print("資料庫尚未建立：%s" % database_path, file=sys.stderr)
        return 1
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    try:
        documents = connection.execute(
            """
            SELECT COUNT(*) AS versions,
                   COUNT(DISTINCT source || '|' || external_id) AS documents,
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
        latest_run = connection.execute(
            """
            SELECT run_id, status, fetched_rows, stored_rows, warning_count
            FROM collection_runs WHERE source = 'CORPORATE_DATA'
            ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError as error:
        print("資料庫尚未初始化 Data Agent 結構：%s" % error, file=sys.stderr)
        return 1
    finally:
        connection.close()

    print(
        "公司文件：%d 版本 / %d 筆（月營收 %d、重大訊息 %d）"
        % (
            documents["versions"],
            documents["documents"],
            documents["monthly_rows"] or 0,
            documents["event_rows"] or 0,
        )
    )
    print("研究快照：%d；品質問題：%d" % (snapshots, issues))
    if latest_run:
        print(
            "最近收集：%s / %s / 讀取 %d / 寫入 %d / 警告 %d"
            % (
                latest_run["run_id"],
                latest_run["status"],
                latest_run["fetched_rows"],
                latest_run["stored_rows"],
                latest_run["warning_count"],
            )
        )
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "collect":
        return collect_data(args.database, args.universe)
    if args.command == "snapshot":
        return build_snapshot(args)
    return show_status(args.database)


if __name__ == "__main__":
    raise SystemExit(main())
