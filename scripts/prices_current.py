#!/usr/bin/env python3
"""檢查資料庫是否已有收盤後定案的行情；exit 0 代表已是最新、可略過該行情步驟，1 代表需要抓取。"""

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import MarketDataDatabase, UniverseLoader  # noqa: E402
from etf_agent.data.price_freshness import STEP_SOURCES, prices_current  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", required=True, choices=sorted(STEP_SOURCES))
    parser.add_argument("--date", type=date.fromisoformat, help="預設使用 Asia/Taipei 當日")
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    parser.add_argument("--universe", type=Path, default=ROOT / "data" / "official_universe.csv")
    args = parser.parse_args()
    try:
        trade_date = args.date or datetime.now(timezone(timedelta(hours=8))).date()
        symbols = [item.symbol for item in UniverseLoader().load(args.universe)]
        with MarketDataDatabase(args.database).connect() as connection:
            current, reason = prices_current(connection, symbols, args.step, trade_date)
    except Exception as error:  # 判斷不出來就當作需要抓取
        print("行情新舊判斷失敗，將重新抓取：%s" % error, file=sys.stderr)
        return 1
    print("[%s] %s：%s" % (args.step, "略過抓取" if current else "需要抓取", reason))
    return 0 if current else 1


if __name__ == "__main__":
    raise SystemExit(main())
