#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import (  # noqa: E402
    DailyPriceCollector,
    MarketDataDatabase,
    TwseDailyProvider,
    load_universe,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 TWSE 最新交易日上市行情")
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    parser.add_argument(
        "--universe", type=Path, default=ROOT / "data" / "official_universe.csv"
    )
    parser.add_argument(
        "--all-listed",
        action="store_true",
        help="開發用：忽略競賽交易池，保存端點內全部上市證券",
    )
    args = parser.parse_args()

    config = json.loads((ROOT / "config" / "data_sources.json").read_text(encoding="utf-8"))
    source = config["twse_daily"]
    provider = TwseDailyProvider(
        url=source["url"],
        timeout_seconds=int(source["timeout_seconds"]),
        user_agent=source["user_agent"],
    )
    database = MarketDataDatabase(args.database)
    collector = DailyPriceCollector(database, provider)

    try:
        result = collector.collect(load_universe(args.universe), args.all_listed)
    except Exception as error:
        print("抓取失敗：%s" % error, file=sys.stderr)
        return 1

    print("交易日：%s" % result.trade_date)
    print("TWSE 可解析資料：%d 筆" % result.fetched_rows)
    print("寫入資料庫：%d 筆" % result.stored_rows)
    print("警告：%d 筆" % len(result.warnings))
    print("run_id：%s" % result.run_id)
    for warning in result.warnings[:10]:
        print("WARN  " + warning)
    if len(result.warnings) > 10:
        print("WARN  另有 %d 筆警告未顯示" % (len(result.warnings) - 10))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
