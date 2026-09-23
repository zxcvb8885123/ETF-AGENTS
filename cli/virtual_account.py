#!/usr/bin/env python3
"""競賽虛擬帳戶開帳、決策快照與模擬成交帳務入口。"""

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.decision.contracts import DecisionInputValidator, canonical_sha256, decision_bundle_sha256  # noqa: E402
from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository, VirtualAccountService  # noqa: E402


def read_json(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VirtualAccountError("無法讀取 JSON：%s" % path) from error
    if not isinstance(value, Mapping):
        raise VirtualAccountError("JSON 頂層必須是物件：%s" % path)
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT / "artifacts" / "virtual_accounts")
    parser.add_argument("--account-id", default="ai-cup-2026")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="只使用一次設定本金建立空倉虛擬帳戶")
    init.add_argument("--rules", type=Path, default=ROOT / "config" / "competition_rules.json")
    init.add_argument("--started-at", required=True)

    prepare = commands.add_parser("prepare-day", help="結算到期款項並產生決策前帳戶快照")
    prepare.add_argument("--snapshot", type=Path, required=True)
    prepare.add_argument("--run-id", required=True)
    prepare.add_argument("--account-output", type=Path, required=True)
    prepare.add_argument("--corporate-actions", type=Path)

    attach = commands.add_parser("attach-account", help="將虛擬 AccountSnapshot 綁入決策輸入包並驗證")
    attach.add_argument("--template", type=Path, required=True)
    attach.add_argument("--account-snapshot", type=Path, required=True)
    attach.add_argument("--snapshot", type=Path, required=True)
    attach.add_argument("--output", type=Path, required=True)

    apply = commands.add_parser("apply-decision", help="驗證正式 Decision run 後套用模擬成交並封存日終帳本")
    apply.add_argument("--decision-repository", type=Path, default=ROOT / "artifacts" / "portfolio_decisions")
    apply.add_argument("--decision-run-id", required=True)
    apply.add_argument("--execution-market", type=Path, required=True)
    apply.add_argument("--close-market", type=Path, required=True)
    apply.add_argument("--settlement-date", required=True)
    apply.add_argument("--run-id", required=True)

    commands.add_parser("status", help="顯示已驗證的最新帳戶狀態")
    verify = commands.add_parser("verify", help="驗證最新帳本 run 與 manifest")
    verify.add_argument("--run-id")

    args = parser.parse_args(argv)
    repository = VirtualAccountRepository(args.repository, args.account_id)
    service = VirtualAccountService(repository)
    try:
        if args.command == "init":
            result = service.initialize(args.rules, args.account_id, args.started_at)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "prepare-day":
            actions = read_json(args.corporate_actions).get("items", []) if args.corporate_actions else []
            if not isinstance(actions, list):
                raise VirtualAccountError("corporate_actions.items 必須是陣列")
            result = service.prepare_day(args.snapshot, args.run_id, actions)
            write_json(args.account_output, result["account_snapshot"])
            print(json.dumps({"run_id": result["run_id"], "state_id": result["state"]["state_id"], "account_snapshot": str(args.account_output)}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "attach-account":
            template = dict(read_json(args.template))
            account = read_json(args.account_snapshot)
            snapshot = read_json(args.snapshot)
            if template.get("snapshot_id") != snapshot.get("snapshot_id") or template.get("decision_cutoff") != snapshot.get("decision_cutoff"):
                raise VirtualAccountError("template 與 ResearchSnapshot id／cutoff 不一致")
            if canonical_sha256(template.get("snapshot")) != canonical_sha256(snapshot):
                raise VirtualAccountError("template 內嵌 Snapshot 與來源 Snapshot 不一致")
            if account.get("available_at") != snapshot.get("decision_cutoff") or account.get("valuation_at") != snapshot.get("decision_cutoff"):
                raise VirtualAccountError("虛擬 AccountSnapshot cutoff 不一致")
            template["account_snapshot"] = dict(account)
            template["snapshot_sha256"] = canonical_sha256(snapshot)
            template.pop("bundle_sha256", None)
            template["bundle_sha256"] = decision_bundle_sha256(template)
            errors = DecisionInputValidator(template).validate()
            if errors:
                raise VirtualAccountError("DecisionInputBundle 驗證失敗：" + "；".join(errors))
            write_json(args.output, template)
            print(str(args.output))
            return 0
        if args.command == "apply-decision":
            result = service.apply_decision(args.decision_repository, args.decision_run_id, args.execution_market, args.close_market, args.settlement_date, args.run_id)
            print(json.dumps({"run_id": result["run_id"], "state": result["state"], "transition": result["transition"]}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "status":
            current = repository.latest()
            print(json.dumps({"run_id": current["run_id"], "state": current["state"]}, ensure_ascii=False, indent=2))
            return 0
        run_id = args.run_id or repository.latest()["run_id"]
        print(str(repository.verify(run_id)))
        return 0
    except (VirtualAccountError, OSError, KeyError, TypeError, ValueError) as error:
        print("虛擬帳戶失敗：%s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
