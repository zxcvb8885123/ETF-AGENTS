#!/usr/bin/env python3
"""Incrementally refresh two years of TWSE and TPEx daily OHLCV through yfinance."""

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
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
        help="預設使用 Asia/Taipei 當日；是否已有完整日線仍由覆蓋驗證判定",
    )
    parser.add_argument("--batch-size", type=int, default=30)
    args = parser.parse_args()
    config = json.loads((ROOT / "config" / "data_sources.json").read_text(encoding="utf-8"))
    source = config["yfinance_daily"]
    try:
        database = MarketDataDatabase(args.database)
        database.initialize()
        end = args.end or datetime.now(timezone(timedelta(hours=8))).date()
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
        with database.connect() as connection:
            end_symbols = {
                str(row["symbol"])
                for row in connection.execute(
                    "SELECT symbol FROM daily_prices WHERE source = ? AND trade_date = ?",
                    ("YAHOO_FINANCE", end.isoformat()),
                )
            }
        missing_end = sorted({item.symbol for item in universe} - end_symbols)
    except Exception as error:
        print("歷史行情抓取失敗：%s" % error, file=sys.stderr)
        return 1

    print("期間：%s 至 %s" % (result.start_date, result.end_date))
    print("要求股票：%d" % result.requested_symbols)
    print("缺少股票：%d" % len(result.missing_symbols))
    print("截止日 %s 覆蓋：%d/%d" % (end, len(universe) - len(missing_end), len(universe)))
    print("寫入日線：%d" % result.stored_rows)
    print("警告：%d" % len(result.warnings))
    print("run_id：%s" % ", ".join(run_ids))
    if result.missing_symbols:
        print("MISSING  " + ", ".join(result.missing_symbols))
    if missing_end:
        print("MISSING_END  " + ", ".join(missing_end))
    for warning in result.warnings[:20]:
        print("WARN  " + warning)
    if len(result.warnings) > 20:
        print("WARN  另有 %d 筆警告未顯示" % (len(result.warnings) - 20))
    return 0 if not result.missing_symbols and not missing_end else 2


if __name__ == "__main__":
    raise SystemExit(main())
