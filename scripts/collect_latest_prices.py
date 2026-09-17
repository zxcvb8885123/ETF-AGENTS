#!/usr/bin/env python3
"""Collect latest TWSE and TPEx prices for the official universe."""

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import (  # noqa: E402
    LatestPriceCollector,
    LatestPriceProviderFactory,
    MarketDataDatabase,
    UniverseLoader,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取官方交易池的 TWSE／TPEx 最新交易日行情"
    )
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    parser.add_argument(
        "--universe", type=Path, default=ROOT / "data" / "official_universe.csv"
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / "config" / "data_sources.json"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        result = LatestPriceCollector(
            MarketDataDatabase(args.database),
            LatestPriceProviderFactory().create(config),
        ).collect(UniverseLoader().load(args.universe))
    except Exception as error:
        print("抓取失敗：%s" % error, file=sys.stderr)
        return 1

    for collection in result.collections:
        print(
            "%s：交易日 %s，可解析 %d 筆，寫入 %d 筆，警告 %d 筆，run_id %s"
            % (
                collection.source,
                collection.trade_date,
                collection.fetched_rows,
                collection.stored_rows,
                len(collection.warnings),
                collection.run_id,
            )
        )
        for warning in collection.warnings[:10]:
            print("WARN  " + warning)
    print("合計寫入：%d 筆" % result.stored_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
