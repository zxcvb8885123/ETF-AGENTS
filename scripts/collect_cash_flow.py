#!/usr/bin/env python3
"""把已擷取的 FinMind 現金流量表，以官方累計稅前淨利核對後轉成 Snapshot 文件；不連網、不跑 Agent。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data.cash_flow import VerifiedCashFlowProvider  # noqa: E402
from etf_agent.data.corporate import CorporateDataCollector  # noqa: E402
from etf_agent.data.database import MarketDataDatabase  # noqa: E402
from etf_agent.data.universe import UniverseLoader  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    parser.add_argument("--universe", type=Path, default=ROOT / "data" / "official_universe.csv")
    args = parser.parse_args()
    try:
        universe = UniverseLoader().load(args.universe)
        provider = VerifiedCashFlowProvider(args.database, [item.symbol for item in universe])
        result = CorporateDataCollector(MarketDataDatabase(args.database), [provider]).collect(universe)
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "completed", "run_id": result.run_id, "stored_documents": result.stored_documents,
                      "warnings": len(result.warnings), "failures": list(result.failures)},
                     ensure_ascii=False, indent=2))
    # 部分股票核對不過或尚無資料屬預期（exit 2），決策時以資料缺口呈現。
    return 1 if result.failures else 2 if result.warnings else 0


if __name__ == "__main__":
    raise SystemExit(main())
