#!/usr/bin/env python3
"""封存八份政府開放交易狀態候選 CSV 的時間版本。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data.source_capture import capture_ogd_candidates  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "docs/source_audit/2026-09-25_ogd_crosscheck.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/source-audit/captures")
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        manifest = capture_ogd_candidates(report, args.output_dir)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"] == "captured_candidate_only" else 2


if __name__ == "__main__":
    raise SystemExit(main())
