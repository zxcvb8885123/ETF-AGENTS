#!/usr/bin/env python3
"""事件資料 Skill 的物件化確定性入口。"""

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Sequence


class ProjectRootLocator:
    def __init__(self, script_path: Path):
        self.script_path = script_path

    def locate(self) -> Path:
        for parent in self.script_path.resolve().parents:
            if (parent / "src" / "etf_agent").is_dir():
                return parent
        raise RuntimeError("找不到 ETF_AGENTS 專案根目錄")


ROOT = ProjectRootLocator(Path(__file__)).locate()
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import (  # noqa: E402
    CorporateDataCollector,
    DataAgentStatusRepository,
    DataAgentService,
    LatestPriceCollector,
    LatestPriceProviderFactory,
    MarketDataDatabase,
    OfficialCorporateProviderFactory,
    UniverseLoader,
    YFinanceHistoryCollector,
)


class DataAgentApplication:
    """解析 CLI 命令並協調 Data Agent 物件。"""

    def __init__(
        self,
        root: Path = ROOT,
        universe_loader: Optional[UniverseLoader] = None,
        latest_provider_factory: Optional[LatestPriceProviderFactory] = None,
        corporate_provider_factory: Optional[OfficialCorporateProviderFactory] = None,
    ):
        self.root = root
        self.universe_loader = universe_loader or UniverseLoader()
        self.latest_provider_factory = (
            latest_provider_factory or LatestPriceProviderFactory()
        )
        self.corporate_provider_factory = (
            corporate_provider_factory or OfficialCorporateProviderFactory()
        )

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        args = self.build_parser().parse_args(argv)
        if args.command == "collect":
            return self.collect_data(args.database, args.universe)
        if args.command == "collect-prices":
            return self.collect_prices(args.database, args.universe, args.config)
        if args.command == "snapshot":
            return self.build_snapshot(args)
        return self.show_status(args.database)

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description="事件資料 Agent：收集官方事件資料並建立研究快照"
        )
        commands = parser.add_subparsers(dest="command", required=True)

        collect = commands.add_parser("collect", help="抓取官方月營收與重大訊息")
        self.add_shared_arguments(collect)
        collect.add_argument(
            "--universe",
            type=Path,
            default=self.root / "data" / "official_universe.csv",
        )

        collect_prices = commands.add_parser(
            "collect-prices", help="更新官方最新行情與兩年歷史行情"
        )
        self.add_shared_arguments(collect_prices)
        collect_prices.add_argument(
            "--universe",
            type=Path,
            default=self.root / "data" / "official_universe.csv",
        )
        collect_prices.add_argument(
            "--config",
            type=Path,
            default=self.root / "config" / "data_sources.json",
        )

        snapshot = commands.add_parser(
            "snapshot", help="建立指定截止時間的研究快照"
        )
        self.add_shared_arguments(snapshot)
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
        snapshot.add_argument(
            "--allow-unvalidated-universe",
            action="store_true",
            help="僅限診斷；沒有可用交易池驗證時仍建立快照",
        )

        status = commands.add_parser("status", help="顯示 Data Agent 資料狀態")
        self.add_shared_arguments(status)
        return parser

    def add_shared_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--database",
            type=Path,
            default=self.root / "var" / "etf_agent.db",
        )

    def collect_data(self, database_path: Path, universe_path: Path) -> int:
        config = json.loads(
            (self.root / "config" / "data_sources.json").read_text(
                encoding="utf-8"
            )
        )["corporate_data"]
        providers = self.corporate_provider_factory.create(config)
        collector = CorporateDataCollector(MarketDataDatabase(database_path), providers)
        try:
            result = collector.collect(self.universe_loader.load(universe_path))
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

    def collect_prices(
        self,
        database_path: Path,
        universe_path: Path,
        config_path: Path,
    ) -> int:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            database = MarketDataDatabase(database_path)
            universe = self.universe_loader.load(universe_path)
            latest_result = LatestPriceCollector(
                database,
                self.latest_provider_factory.create(config),
            ).collect(universe)
            history_config = config["yfinance_daily"]
            official_end = database.latest_complete_trade_date(
                ("TWSE_STOCK_DAY_ALL", "TPEX_MAINBOARD_QUOTES")
            )
            if official_end is None:
                raise ValueError("缺少完整官方最新行情，不能決定歷史行情安全截止日")
            history_result = YFinanceHistoryCollector(
                database,
                batch_size=int(history_config["batch_size"]),
                retries=int(history_config["retries"]),
                progress=lambda message: print(message, flush=True),
            ).refresh(
                universe,
                date.fromisoformat(official_end),
                lookback_years=int(history_config.get("lookback_years", 2)),
                overlap_days=int(history_config.get("overlap_days", 7)),
            )
        except Exception as error:
            print("抓取失敗：%s" % error, file=sys.stderr)
            return 1

        for collection in latest_result.collections:
            print(
                "%s：%s，寫入 %d 筆，警告 %d 筆，run_id %s"
                % (
                    collection.source,
                    collection.trade_date,
                    collection.stored_rows,
                    len(collection.warnings),
                    collection.run_id,
                )
            )
        print("官方最新行情合計寫入：%d 筆" % latest_result.stored_rows)
        print(
            "歷史行情：%s 至 %s，寫入 %d 筆，缺少 %d 檔"
            % (
                history_result.start_date,
                history_result.end_date,
                history_result.stored_rows,
                len(history_result.missing_symbols),
            )
        )
        if history_result.missing_symbols:
            print("MISSING  " + ", ".join(history_result.missing_symbols))
        return 0 if not history_result.missing_symbols else 2

    @staticmethod
    def build_snapshot(args: argparse.Namespace) -> int:
        try:
            snapshot = DataAgentService(
                MarketDataDatabase(args.database)
            ).build_snapshot(
                args.decision_cutoff,
                run_id=args.run_id,
                require_prices=not args.allow_missing_prices,
                require_universe_validation=not args.allow_unvalidated_universe,
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
        print(
            "交易池驗證：%s；可交易 %d／不可交易 %d／不一致 %d"
            % (
                snapshot.universe_validation_status or "無",
                snapshot.tradable_symbols,
                snapshot.not_tradable_symbols,
                snapshot.universe_mismatches,
            )
        )
        for flag in snapshot.quality_flags:
            print("QUALITY  " + flag)
        return 0 if snapshot.usable else 2

    @staticmethod
    def show_status(database_path: Path) -> int:
        if not database_path.exists():
            print("資料庫尚未建立：%s" % database_path, file=sys.stderr)
            return 1
        try:
            status = DataAgentStatusRepository(
                MarketDataDatabase(database_path)
            ).load()
        except Exception as error:
            print("讀取 Data Agent 狀態失敗：%s" % error, file=sys.stderr)
            return 1

        print(
            "公司文件：%d 版本 / %d 筆（月營收 %d、重大訊息 %d）"
            % (
                status.documents.versions,
                status.documents.documents,
                status.documents.monthly_rows,
                status.documents.event_rows,
            )
        )
        print(
            "研究快照：%d；品質問題：%d"
            % (status.snapshot_count, status.quality_issue_count)
        )
        if status.latest_collection:
            latest = status.latest_collection
            print(
                "最近收集：%s / %s / 讀取 %d / 寫入 %d / 警告 %d"
                % (
                    latest.run_id,
                    latest.status,
                    latest.fetched_rows,
                    latest.stored_rows,
                    latest.warning_count,
                )
            )
        if status.latest_source_report:
            latest = status.latest_source_report
            print(
                "最近來源探測：%s / %s / %s"
                % (
                    latest.report_id,
                    latest.status,
                    latest.generated_at,
                )
            )
        else:
            print("最近來源探測：無")
        if status.latest_universe_validation:
            latest = status.latest_universe_validation
            print(
                "最近交易池驗證：%s / %s / usable=%s / 可交易 %d／不可交易 %d／不一致 %d／總計 %d"
                % (
                    latest.validation_id,
                    latest.status,
                    "是" if latest.usable else "否",
                    latest.tradable_count,
                    latest.not_tradable_count,
                    latest.mismatch_count,
                    latest.total_count,
                )
            )
        else:
            print("最近交易池驗證：無")
        return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    return DataAgentApplication().run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
