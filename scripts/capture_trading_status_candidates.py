#!/usr/bin/env python3
"""封存八份政府開放交易狀態候選 CSV 的時間版本。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datetime import datetime, timezone  # noqa: E402

from etf_agent.data import latest_calendar_capture_before  # noqa: E402
from etf_agent.data.source_capture import capture_ogd_candidates  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "docs/source_audit/2026-09-25_ogd_crosscheck.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/data/trading_status")
    parser.add_argument("--only-if-missing", action="store_true", help="已有開休市日期表就不重抓")
    args = parser.parse_args()
    try:
        if args.only_if_missing and latest_calendar_capture_before(args.output_dir, datetime.now(timezone.utc).isoformat()):
            print(json.dumps({"status": "skipped", "reason": "已有封存的開休市日期表"}, ensure_ascii=False))
            return 0
        report = json.loads(args.report.read_text(encoding="utf-8"))
        manifest = capture_ogd_candidates(report, args.output_dir, keep_latest_only=True)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"] == "captured_candidate_only" else 2


if __name__ == "__main__":
    raise SystemExit(main())
