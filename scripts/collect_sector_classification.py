#!/usr/bin/env python3
"""抓取 TWSE／TPEx 官方公司基本資料並建立 150 檔交易池的產業代碼分類。"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import (  # noqa: E402
    SectorClassificationBuilder,
    SectorClassificationError,
    UniverseLoader,
    capture_sector_sources,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, default=ROOT / "data" / "official_universe.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "sector_classification.json")
    parser.add_argument(
        "--raw-dir", type=Path, default=ROOT / "artifacts" / "source-audit" / "sector-classification"
    )
    args = parser.parse_args()
    run_dir = args.raw_dir / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        universe = UniverseLoader().load(args.universe)
        captures = capture_sector_sources(run_dir)
        classification = SectorClassificationBuilder(universe, captures).build()
    except (OSError, ValueError, SectorClassificationError) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}, ensure_ascii=False))
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(classification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": classification["status"],
                "version": classification["version"],
                "classified": len(classification["sector_by_symbol"]),
                "missing_symbols": classification["missing_symbols"],
                "raw_dir": str(run_dir),
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if classification["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
