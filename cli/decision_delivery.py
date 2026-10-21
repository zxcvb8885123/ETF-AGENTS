#!/usr/bin/env python3
"""由已封存 Decision run 建立或重建驗證中文最終報告。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from etf_agent.decision_delivery import build_delivery, validate_delivery


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="建立最終閱讀版或阻擋診斷")
    build.add_argument("--decision-repository", type=Path, default=ROOT / "artifacts/decision_runs")
    build.add_argument("--decision-run-id", required=True)
    build.add_argument("--output-repository", type=Path, default=ROOT / "artifacts/decision_reports")
    validate = commands.add_parser("validate", help="重建交付及檢查閱讀副本")
    validate.add_argument("--output-repository", type=Path, default=ROOT / "artifacts/decision_reports")
    validate.add_argument("--delivery-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build_delivery(args.decision_repository, args.decision_run_id, args.output_repository)
            code = 0 if result["report_status"] == "ready" else 2
        else:
            result = validate_delivery(args.output_repository, args.delivery_id)
            code = 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
