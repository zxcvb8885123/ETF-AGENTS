#!/usr/bin/env python3
"""驗證已封存的政府開放 CSV／官方 JSON 對照；不改正式來源設定。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data.source_audit import verify_ogd_crosscheck  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        result = verify_ogd_crosscheck(report, args.audit_dir)
    except (OSError, json.JSONDecodeError) as error:
        result = {"valid": False, "errors": ["稽核報告無法讀取：%s" % error], "sources": []}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
