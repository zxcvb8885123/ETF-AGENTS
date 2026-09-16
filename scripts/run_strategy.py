#!/usr/bin/env python3
"""Run event strategy V1 against a point-in-time research snapshot."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.strategy import EventDrivenStrategy, StrategyInputError
from etf_agent.strategy.event_v1 import stock_from_dict


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="研究快照 JSON")
    parser.add_argument("--output", type=Path, help="策略提案輸出；省略時印到 stdout")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "event_strategy_v1.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        strategy = EventDrivenStrategy(args.config)
        proposal = strategy.run(
            as_of=payload["as_of"],
            breadth=payload["breadth"],
            equal_weight_above_ma20=payload["equal_weight_above_ma20"],
            stocks=[stock_from_dict(item) for item in payload["stocks"]],
        )
    except (OSError, KeyError, TypeError, ValueError, StrategyInputError) as error:
        print("策略執行失敗：%s" % error, file=sys.stderr)
        return 1

    rendered = json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print("策略提案已產生：%s" % args.output)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
