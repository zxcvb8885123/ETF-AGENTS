#!/usr/bin/env python3
"""補齊待結算決策成交日的 TPEx 官方原始價量；不修改帳本。"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data.tpex_history import collect_quotes  # noqa: E402
from etf_agent.virtual_account.daily import OfficialCloseMarket, find_decision_run  # noqa: E402
from etf_agent.virtual_account import active_account_id  # noqa: E402
from etf_agent.virtual_account.service import VirtualAccountRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", help="預設讀取 config/active_account.json")
    args = parser.parse_args()
    try:
        args.account_id = args.account_id or active_account_id(ROOT)
        account = VirtualAccountRepository(ROOT / "artifacts/ledger", args.account_id).latest()
        state = account["state"]
        if state["provenance"]["type"] != "prepared":
            raise ValueError("帳本目前沒有待結算的 prepare 狀態")
        decisions = ROOT / "artifacts/decisions"
        run_id = find_decision_run(decisions, state["provenance"]["account_snapshot"])
        if run_id is None:
            raise ValueError("沒有與目前帳本對應的封存決策")
        bundle = json.loads((decisions / run_id / "decision_input.json").read_text(encoding="utf-8"))
        target = bundle["trading_status_bundle"]["target_session"]["end"]
        from etf_agent.virtual_account.daily import TAIPEI
        trade_date = datetime.fromisoformat(target).astimezone(TAIPEI).date()
        decision = json.loads((decisions / run_id / "decision.json").read_text(encoding="utf-8"))
        required = {row["symbol"] for row in state["positions"]} | {row["symbol"] for row in decision["orders"]}
        existing = OfficialCloseMarket(ROOT / "var/etf_agent.db").quotes(trade_date.isoformat())
        missing = required - existing.keys()
        if any(not symbol.endswith(".TWO") for symbol in missing):
            raise ValueError("缺少上市股票官方價量，不能只補上櫃：" + "、".join(sorted(missing)))
        if not missing:
            print("成交日官方價量已齊全，可執行帳本 settle")
            return 0
        print("補抓 %s 官方 TPEx 價量：%s" % (trade_date, "、".join(sorted(missing))), flush=True)
        result = collect_quotes(ROOT / "var/etf_agent.db", trade_date, missing)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, OSError, TypeError) as error:
        print("補抓停止：%s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
