#!/usr/bin/env python3
"""擷取 FinMind 財報或個股新聞候選資料。"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data.database import MarketDataDatabase  # noqa: E402
from etf_agent.data.supplemental_sources import (  # noqa: E402
    FINMIND_DATASETS,
    FinMindFundamentalCollector,
    FinMindFundamentalProvider,
    FinMindNewsCollector,
    FinMindNewsProvider,
    SupplementalRepository,
)
from etf_agent.data.universe import UniverseLoader  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=("finmind", "finmind-news"))
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    parser.add_argument("--dataset", choices=sorted(FINMIND_DATASETS))
    parser.add_argument("--stock-id")
    parser.add_argument("--universe", type=Path)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--date", type=date.fromisoformat,
                        help="個股新聞查詢日；FinMind 每次只回傳一天")
    parser.add_argument("--request-delay", type=float, default=1.0,
                        help="FinMind 逐股請求間隔秒數，預設 1 秒")
    args = parser.parse_args(argv)
    if args.request_delay < 0:
        parser.error("--request-delay 不得小於 0")
    repository = SupplementalRepository(MarketDataDatabase(args.database))
    try:
        if bool(args.stock_id) == bool(args.universe):
            parser.error("FinMind 須指定 --stock-id 或 --universe 其中一種")
        ids = ([args.stock_id] if args.stock_id else
               [item.code for item in UniverseLoader().load(args.universe)])
        if args.source == "finmind":
            if not args.dataset or not args.start or not args.end:
                parser.error("finmind 需要 --dataset、--start、--end")
            collector = FinMindFundamentalCollector(
                repository, FinMindFundamentalProvider()
            )
        else:
            if not args.date:
                parser.error("finmind-news 需要 --date")
            collector = FinMindNewsCollector(repository, FinMindNewsProvider())
        results = []
        for index, stock_id in enumerate(ids):
            if index:
                time.sleep(args.request_delay)
            if args.source == "finmind":
                results.append(collector.collect(
                    args.dataset, stock_id, args.start, args.end
                ))
            else:
                results.append(collector.collect(stock_id, args.date))
    except (ValueError, RuntimeError, OSError) as error:
        print("補充資料擷取失敗：%s" % error, file=sys.stderr)
        return 1
    missing = sum(item.fetched_rows == 0 for item in results)
    print(json.dumps({"source": args.source, "requested": len(results),
                      "empty_results": missing,
                      "fetched_rows": sum(item.fetched_rows for item in results),
                      "stored_rows": sum(item.stored_rows for item in results),
                      "run_ids": [item.run_id for item in results]}, ensure_ascii=False))
    return 2 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
