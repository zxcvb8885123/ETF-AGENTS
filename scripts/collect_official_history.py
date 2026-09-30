#!/usr/bin/env python3
"""以官方 TWSE 個股月行情補上市帳本價量並輸出覆蓋報告。"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import (  # noqa: E402
    HistoricalPriceProvider,
    MarketDataDatabase,
    OfficialHistoricalRefreshService,
    UniverseLoader,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    parser.add_argument(
        "--universe", type=Path, default=ROOT / "data" / "official_universe.csv"
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / "config" / "data_sources.json"
    )
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--lookback-years", type=int)
    parser.add_argument("--overlap-days", type=int)
    parser.add_argument("--max-workers", type=int)
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "artifacts" / "official-history" / "latest.json",
        help="覆蓋報告 JSON 路徑；標準輸出也會印出相同內容。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        settings = config["historical_daily"]
        universe = [
            item for item in UniverseLoader().load(args.universe)
            if item.market.upper() == "TWSE"
        ]
        if not universe:
            raise ValueError("交易池沒有上市股票")
        provider = HistoricalPriceProvider(
            settings["twse_url"],
            timeout_seconds=int(settings["timeout_seconds"]),
            user_agent=str(settings["user_agent"]),
        )
        result = OfficialHistoricalRefreshService(
            MarketDataDatabase(args.database),
            provider,
            max_workers=int(
                args.max_workers
                if args.max_workers is not None
                else settings.get("max_workers", 4)
            ),
            retries=int(settings["retries"]),
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        ).refresh(
            universe,
            start=args.start,
            end=args.end,
            lookback_years=int(
                args.lookback_years
                if args.lookback_years is not None
                else settings.get("lookback_years", 2)
            ),
            overlap_days=int(
                args.overlap_days
                if args.overlap_days is not None
                else settings.get("overlap_days", 7)
            ),
        )
    except Exception as error:
        print("官方歷史行情抓取失敗：%s" % error, file=sys.stderr)
        return 1

    body = result.as_dict()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(body, ensure_ascii=False, indent=2))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
