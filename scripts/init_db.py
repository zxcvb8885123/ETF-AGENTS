#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import MarketDataDatabase  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化 ETF Agent SQLite 資料庫")
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    args = parser.parse_args()
    database = MarketDataDatabase(args.database)
    database.initialize()
    print("資料庫初始化完成：%s" % args.database)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
