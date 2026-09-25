#!/usr/bin/env python3
"""從封存的八份 CSV 建立 150 檔候選事實報告；不核准來源。"""

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data.ogd_candidate_facts import build_candidate_fact_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--universe", type=Path, default=ROOT / "data/official_universe.csv")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        with args.universe.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
            if len(rows) != 150 or any(row["market"] not in {"TWSE", "TPEX"} for row in rows):
                raise ValueError("官方交易池必須恰有 150 檔且市場欄位有效")
            universe = {
                row["symbol"].strip() + (".TWO" if row["market"] == "TPEX" else ".TW")
                for row in rows
            }
            if len(universe) != 150:
                raise ValueError("官方交易池代號重複")
        report = build_candidate_fact_report(manifest, args.manifest.parent, universe)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, csv.Error, json.JSONDecodeError) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({
        "status": report["status"],
        "candidate_hit_symbols": len(report["candidate_hit_symbols"]),
        "approved_sources": report["approved_sources"],
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
