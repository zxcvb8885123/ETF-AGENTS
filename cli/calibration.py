#!/usr/bin/env python3
"""校準旁觀者 CLI：記錄已封存決策、價格到齊後對答案、審計決策價與成交均價的差距。"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.calibration import MIN_DECISION_DAYS, CalibrationError, CalibrationRepository, evaluate, execution_gap_report, intent_landing_report, record_all, shadow_swap_report  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    snapshot = sub.add_parser("snapshot", help="把已封存的 Decision run 記成觀測（可重複執行，已記錄的不會重複）")
    snapshot.add_argument("--decisions", type=Path, default=ROOT / "artifacts" / "decisions")
    snapshot.add_argument("--output", type=Path, default=ROOT / "artifacts" / "calibration")
    report = sub.add_parser("report", help="以官方成交均價對答案；完整決策日不足 %d 天時不給結論" % MIN_DECISION_DAYS)
    report.add_argument("--observations", type=Path, default=ROOT / "artifacts" / "calibration")
    report.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    report.add_argument("--as-of", default=None, help="含時區的 ISO 時間；預設現在")
    audit = sub.add_parser("gap-audit", help="決策用的前日收盤，與當日成交均價、開盤差多少")
    audit.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    audit.add_argument("--as-of", default=None)
    audit.add_argument("--days", type=int, default=60)
    landing = sub.add_parser("intent-landing", help="統計 buy／add 意圖的落地率、旗標次數與 hold／add 被賣出的案例（唯讀）")
    landing.add_argument("--decisions", type=Path, default=ROOT / "artifacts" / "decisions")
    landing.add_argument("--since", default=None, help="只算該日（YYYY-MM-DD）含以後的 run")
    shadow = sub.add_parser("shadow-swap", help="統計 agent_swap v1 影子提名，並離線估算若開 v2 會成交哪幾對（唯讀；無影子資料時明確回報）")
    shadow.add_argument("--decisions", type=Path, default=ROOT / "artifacts" / "decisions")
    shadow.add_argument("--since", default=None, help="只算該日（YYYY-MM-DD）含以後的 run")
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = record_all(args.decisions, CalibrationRepository(args.output))
        elif args.command == "report":
            result = evaluate(CalibrationRepository(args.observations), args.database, args.as_of or _now())
        elif args.command == "shadow-swap":
            result = shadow_swap_report(args.decisions, args.since)
        elif args.command == "intent-landing":
            result = intent_landing_report(args.decisions, args.since)
        else:
            result = execution_gap_report(args.database, args.as_of or _now(), args.days)
    except CalibrationError as error:
        print("校準失敗：%s" % error, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
