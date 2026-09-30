#!/usr/bin/env python3
"""歷史時點重放：指定日期區間，用每日決策鏈逐日決策並照比賽規則估值。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.automation.daily_pipeline import ClaudeAgentRunner  # noqa: E402
from etf_agent.backtest.replay import ReplayError, ReplayRequest  # noqa: E402
from etf_agent.backtest.replay.report import render_markdown  # noqa: E402
from etf_agent.backtest.replay.runner import ReplayRunner  # noqa: E402
from etf_agent.data import calendar_from_capture, latest_calendar_capture_before  # noqa: E402
from etf_agent.data.evidence import TAIPEI_TIMEZONE  # noqa: E402
from datetime import datetime  # noqa: E402


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="執行或續跑重放")
    run.add_argument("--start", required=True)
    run.add_argument("--end", required=True)
    run.add_argument("--backtest-id", required=True)
    run.add_argument("--source-db", type=Path, default=ROOT / "var" / "etf_agent.db")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--model")
    run.add_argument("--max-budget-usd", type=float, default=3.0)
    report = commands.add_parser("report", help="重新輸出已完成重放的 Markdown 報告")
    report.add_argument("--backtest-id", required=True)
    args = parser.parse_args(argv)
    directory = ROOT / "artifacts" / "backtest_runs" / args.backtest_id
    try:
        if args.command == "report":
            payload = json.loads((directory / "report.json").read_text(encoding="utf-8"))
            print(render_markdown(payload))
            return 0
        capture = latest_calendar_capture_before(
            ROOT / "artifacts" / "source-audit" / "captures", datetime.now(TAIPEI_TIMEZONE).isoformat()
        )
        if capture is None:
            raise ReplayError("找不到封存的官方開休市日期表")
        runner = ReplayRunner(
            ROOT, directory, args.source_db, ReplayRequest.parse(args.start, args.end),
            calendar_from_capture(capture),
            ClaudeAgentRunner(ROOT, model=args.model, max_budget_usd=args.max_budget_usd),
            log=lambda message: print(message, flush=True), resume=args.resume,
        )
        result = runner.run()
    except (ReplayError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "report": str(directory / "report.md"), "total_return": result["total_return"],
                      "rule_breaches": len(result["rule_breaches"])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
