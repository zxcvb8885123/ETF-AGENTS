#!/usr/bin/env python3
"""Incrementally refresh two years of TWSE and TPEx daily OHLCV through yfinance."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import (  # noqa: E402
    MarketDataDatabase,
    UniverseLoader,
    YFinanceHistoryCollector,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    parser.add_argument("--universe", type=Path, default=ROOT / "data" / "official_universe.csv")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        help="預設使用 TWSE／TPEx 都已收盤的最近官方交易日",
    )
    parser.add_argument("--batch-size", type=int, default=30)
    args = parser.parse_args()
    config = json.loads((ROOT / "config" / "data_sources.json").read_text(encoding="utf-8"))
    source = config["yfinance_daily"]
    try:
        database = MarketDataDatabase(args.database)
        database.initialize()
        end = args.end
        if end is None:
            official_end = database.latest_complete_trade_date(
                ("TWSE_STOCK_DAY_ALL", "TPEX_MAINBOARD_QUOTES")
            )
            if official_end is None:
                raise ValueError("缺少完整官方最新行情；請先執行 collect_latest_prices.py")
            end = date.fromisoformat(official_end)
        collector = YFinanceHistoryCollector(
            database,
            batch_size=args.batch_size,
            retries=int(source["retries"]),
            progress=lambda message: print(message, flush=True),
        )
        universe = UniverseLoader().load(args.universe)
        if args.start:
            result = collector.collect(universe, args.start, end)
            run_ids = (result.run_id,)
        else:
            result = collector.refresh(
                universe,
                end,
                lookback_years=int(source.get("lookback_years", 2)),
                overlap_days=int(source.get("overlap_days", 7)),
            )
            run_ids = result.run_ids
    except Exception as error:
        print("歷史行情抓取失敗：%s" % error, file=sys.stderr)
        return 1

    print("期間：%s 至 %s" % (result.start_date, result.end_date))
    print("要求股票：%d" % result.requested_symbols)
    print("缺少股票：%d" % len(result.missing_symbols))
    print("寫入日線：%d" % result.stored_rows)
    print("警告：%d" % len(result.warnings))
    print("run_id：%s" % ", ".join(run_ids))
    if result.missing_symbols:
        print("MISSING  " + ", ".join(result.missing_symbols))
    for warning in result.warnings[:20]:
        print("WARN  " + warning)
    if len(result.warnings) > 20:
        print("WARN  另有 %d 筆警告未顯示" % (len(result.warnings) - 20))
    return 0 if not result.missing_symbols else 2


if __name__ == "__main__":
    raise SystemExit(main())
