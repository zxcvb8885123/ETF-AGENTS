#!/usr/bin/env python3
"""顯示 Data Agent 的行情、文件與驗證狀態。"""

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import DataAgentStatusRepository, MarketDataDatabase, format_status_time  # noqa: E402


class DataStatusApplication:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        parser = argparse.ArgumentParser(description="查看 ETF Agent 行情資料狀態")
        parser.add_argument(
            "--database",
            type=Path,
            default=self.root / "var" / "etf_agent.db",
        )
        args = parser.parse_args(argv)
        if not args.database.exists():
            print("資料庫尚未建立：%s" % args.database)
            return 1

        try:
            status = DataAgentStatusRepository(
                MarketDataDatabase(args.database)
            ).load()
        except Exception as error:
            print("讀取資料狀態失敗：%s" % error, file=sys.stderr)
            return 1

        print("行情筆數：%d" % status.prices.row_count)
        print("股票數：%d" % status.prices.symbol_count)
        print(
            "日期範圍：%s ～ %s"
            % (status.prices.first_date or "無", status.prices.last_date or "無")
        )
        print("最新交易日股票數：%d" % status.prices.latest_symbol_count)
        print(
            "來源："
            + ", ".join(
                "%s=%d" % (item.source, item.row_count)
                for item in status.prices.sources
            )
        )
        print(
            "公司文件：%d 版本 / %d 筆資料（月營收 %d、重大訊息 %d）"
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
                "資料更新：%s；最後更新：%s"
                % (
                    "成功" if latest.status == "success" else "未成功（%s）" % latest.status,
                    format_status_time(latest.finished_at),
                )
            )
            if latest.error_message:
                print("錯誤：%s" % latest.error_message)
        if status.latest_source_report:
            latest = status.latest_source_report
            print(
                "來源檢查：%s；最後更新：%s"
                % (
                    "通過" if latest.status == "passed" else "未通過（%s）" % latest.status,
                    format_status_time(latest.generated_at),
                )
            )
        else:
            print("來源檢查：尚無紀錄")
        if status.latest_universe_validation:
            latest = status.latest_universe_validation
            print(
                "交易池：%s；可交易 %d／不可交易 %d／不一致 %d／總計 %d"
                % (
                    "可用" if latest.usable else "不可用",
                    latest.tradable_count,
                    latest.not_tradable_count,
                    latest.mismatch_count,
                    latest.total_count,
                )
            )
        else:
            print("交易池：尚未驗證")
        return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    return DataStatusApplication().run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
