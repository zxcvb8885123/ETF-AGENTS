#!/usr/bin/env python3
"""啟動唯讀虛擬帳戶績效儀表板（FastAPI），含帳戶對 0050／交易池等權買入持有的基準比較。"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402

from etf_agent.dashboard.app import create_app  # noqa: E402
from etf_agent.virtual_account import active_account_id  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT / "artifacts" / "ledger")
    parser.add_argument("--decisions", type=Path, default=ROOT / "artifacts" / "decisions", help="Decision run 目錄；用來顯示尚未成交的委託")
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db", help="行情資料庫（唯讀），供基準比較讀官方日線")
    parser.add_argument("--rules", type=Path, default=ROOT / "config" / "decision_rules.json", help="費稅與整張設定")
    parser.add_argument("--benchmarks", type=Path, default=ROOT / "config" / "benchmarks.json", help="基準清單與授權狀態")
    parser.add_argument("--account-id", help="預設讀取 config/active_account.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    app = create_app(
        args.repository, args.account_id or active_account_id(ROOT), args.decisions,
        database=args.database, rules_path=args.rules, benchmarks_path=args.benchmarks, root=ROOT,
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
