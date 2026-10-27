#!/usr/bin/env python3
"""策略反思 CLI：整理簡報、每日自動反思並套用、手動套用指定反思、回滾。

樣本不足時 auto 不呼叫 Agent。設定檔 config/decision_policy.json 只由本 CLI 以確定性程式原子改寫。
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.automation.agent_runner import ClaudeAgentRunner, DailyPipelineError  # noqa: E402
from etf_agent.calibration import CalibrationError  # noqa: E402
from etf_agent.decision_delivery.repository import publication_lock  # noqa: E402
from etf_agent.strategy_reflection import (  # noqa: E402
    ReflectionError, ReflectionRepository, apply_reflection, default_replayer, make_brief, rollback, run_auto,
)
from etf_agent.evaluation import EvaluationError, ReplayError  # noqa: E402
from etf_agent.strategy_reflection.service import load_reflection  # noqa: E402


def _daily_lock():
    return publication_lock(ROOT / "artifacts" / "daily", filename=".lock")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    parser.add_argument("--decisions", type=Path, default=ROOT / "artifacts" / "decisions")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("brief", help="只印出簡報（校準統計、目前旋鈕、evidence），不呼叫 Agent、不改任何檔案")
    auto = sub.add_parser("auto", help="每日自動：樣本足夠才呼叫 Agent；提案通過驗證與影子回放後自動套用")
    auto.add_argument("--model", help="反思使用的模型；預設沿用 Claude CLI 設定")
    auto.add_argument("--no-apply", action="store_true", help="只產生並封存反思，不套用")
    apply = sub.add_parser("apply", help="套用指定的已封存反思（同樣要通過驗證與影子回放）")
    apply.add_argument("--reflection-id", required=True)
    sub.add_parser("rollback", help="把最近一次套用的策略樣板還原為備份")
    sub.add_parser("history", help="列出套用與回滾歷史")
    args = parser.parse_args(argv)
    try:
        if args.command == "brief":
            result = make_brief(ROOT, args.database, _now().isoformat())
            result = {key: value for key, value in result.items() if key != "evidence"} | {"evidence_count": len(result["evidence"])}
        elif args.command == "history":
            result = ReflectionRepository(ROOT).history()
        else:
            if args.command == "auto":
                result = run_auto(ROOT, args.database, args.decisions,
                                  ClaudeAgentRunner(ROOT, model=args.model, max_budget_usd=3.0, timeout_seconds=600),
                                  _now(), apply=not args.no_apply, write_lock=_daily_lock)
            elif args.command == "apply":
                reflection, brief = load_reflection(ReflectionRepository(ROOT), args.reflection_id)
                replayer = default_replayer(ROOT, args.database, args.decisions, str(brief["as_of"]))
                result = apply_reflection(ROOT, args.reflection_id, replayer, _now(), write_lock=_daily_lock)
            else:
                try:
                    with _daily_lock():
                        result = rollback(ROOT, _now())
                except BlockingIOError:
                    print("每日決策進行中，稍後再試。", file=sys.stderr)
                    return 3
    except (ReflectionError, CalibrationError, EvaluationError, ReplayError, DailyPipelineError, OSError) as error:
        print("策略反思停止：%s" % error, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
