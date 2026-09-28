#!/usr/bin/env python3
"""收集官方財報彙總資料並輸出固定分母的覆蓋報告。"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import (  # noqa: E402
    FinancialStatementCollector,
    FinancialStatementRequest,
    MarketDataDatabase,
    OfficialCorporateProviderFactory,
    UniverseLoader,
    latest_due_quarter,
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="收集 TWSE／TPEx 官方財報彙總資料並驗收指定年度季度"
    )
    parser.add_argument(
        "--database", type=Path, default=ROOT / "var" / "etf_agent.db"
    )
    parser.add_argument(
        "--universe", type=Path, default=ROOT / "data" / "official_universe.csv"
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / "config" / "data_sources.json"
    )
    period = parser.add_mutually_exclusive_group(required=True)
    period.add_argument(
        "--latest-due", action="store_true",
        help="依台北日期與法定公告期限自動選最近已到期的年度季度（每日排程使用）",
    )
    period.add_argument("--fiscal-year", type=int)
    parser.add_argument("--fiscal-quarter", type=int)
    parser.add_argument(
        "--report",
        type=Path,
        help="覆蓋報告 JSON 輸出位置；stdout 一律也會輸出同一內容",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.latest_due:
        args.fiscal_year, args.fiscal_quarter = latest_due_quarter(
            datetime.now(timezone(timedelta(hours=8))).date()
        )
    elif args.fiscal_quarter is None:
        print("--fiscal-year 需搭配 --fiscal-quarter", file=sys.stderr)
        return 1
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        financial_config = config.get("financial_statements")
        if not isinstance(financial_config, dict):
            raise ValueError("設定檔缺少 financial_statements")
        universe = UniverseLoader().load(args.universe)
        request = FinancialStatementRequest(
            fiscal_year=args.fiscal_year,
            fiscal_quarter=args.fiscal_quarter,
        )
        providers = OfficialCorporateProviderFactory().create(financial_config)
        result = FinancialStatementCollector(
            MarketDataDatabase(args.database), providers
        ).collect(universe, request)
        full_payload = result.as_dict()
        payload = _stdout_payload(full_payload)
    except Exception as error:
        payload = {
            "schema_version": "1.0",
            "status": "failed",
            "usable": False,
            "error": str(error),
        }
        print("財報收集失敗：%s" % error, file=sys.stderr)
        full_payload = payload
        exit_code = 1
    else:
        exit_code = 0 if result.usable else 2
        print(
            "財報收集：%s；覆蓋 %d/%d；缺漏 %d"
            % (
                result.status,
                result.covered_count,
                result.expected_count,
                len(result.gaps),
            ),
            file=sys.stderr,
        )

    output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(full_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as error:
            print("覆蓋報告寫入失敗：%s" % error, file=sys.stderr)
            if exit_code == 0:
                exit_code = 1
    print(output, end="")
    return exit_code


def _stdout_payload(full_payload: dict) -> dict:
    """保留可機讀狀態，避免把數千個觀測代號重複印到終端。"""

    payload = dict(full_payload)
    source_report = dict(payload["source_report"])
    source_report["probes"] = [
        {
            "source_id": probe["source_id"],
            "market": probe["market"],
            "status": probe["status"],
            "row_count": probe["row_count"],
            "data_date": probe["data_date"],
            "content_sha256": probe["content_sha256"],
            "errors": probe["errors"],
        }
        for probe in source_report["probes"]
    ]
    payload["source_report"] = source_report
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
