#!/usr/bin/env python3
"""Download two years of TWSE and TPEx daily OHLCV through yfinance."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import (  # noqa: E402
    MarketDataDatabase,
    YFinanceHistoryCollector,
    load_universe,
)


def two_year_start(end: date) -> date:
    try:
        return end.replace(year=end.year - 2)
    except ValueError:
        return end.replace(year=end.year - 2, day=28)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    parser.add_argument("--universe", type=Path, default=ROOT / "data" / "official_universe.csv")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--batch-size", type=int, default=30)
    args = parser.parse_args()
    start = args.start or two_year_start(args.end)

    config = json.loads((ROOT / "config" / "data_sources.json").read_text(encoding="utf-8"))
    source = config["yfinance_daily"]
    try:
        collector = YFinanceHistoryCollector(
            MarketDataDatabase(args.database),
            batch_size=args.batch_size,
            retries=int(source["retries"]),
            progress=lambda message: print(message, flush=True),
        )
        result = collector.collect(load_universe(args.universe), start, args.end)
    except Exception as error:
        print("歷史行情抓取失敗：%s" % error, file=sys.stderr)
        return 1

    print("期間：%s 至 %s" % (result.start_date, result.end_date))
    print("要求股票：%d" % result.requested_symbols)
    print("缺少股票：%d" % len(result.missing_symbols))
    print("寫入日線：%d" % result.stored_rows)
    print("警告：%d" % len(result.warnings))
    print("run_id：%s" % result.run_id)
    if result.missing_symbols:
        print("MISSING  " + ", ".join(result.missing_symbols))
    for warning in result.warnings[:20]:
        print("WARN  " + warning)
    if len(result.warnings) > 20:
        print("WARN  另有 %d 筆警告未顯示" % (len(result.warnings) - 20))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
