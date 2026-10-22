#!/usr/bin/env python3
"""競賽虛擬帳戶開帳、決策快照與模擬成交帳務入口。"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import TradingCalendar, calendar_from_capture, latest_calendar_capture_before  # noqa: E402
from etf_agent.decision.contracts import DecisionInputValidator, canonical_sha256, decision_bundle_sha256  # noqa: E402
from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository, VirtualAccountService, active_account_id  # noqa: E402
from etf_agent.virtual_account.daily import DailyAccountRunner  # noqa: E402


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


def load_calendar(captures_root: Path, limit_time: str) -> TradingCalendar:
    """limit_time 前最近一次封存的官方開休市日曆；找不到時停止，不退回週一至週五近似。"""
    capture = latest_calendar_capture_before(captures_root, limit_time)
    if capture is None:
        raise VirtualAccountError("%s 前沒有封存的官方開休市日期表，不能計算交割日：%s" % (limit_time, captures_root))
    return calendar_from_capture(capture)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT / "artifacts" / "ledger")
    parser.add_argument("--account-id", help="預設讀取 config/active_account.json")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="只使用一次設定本金建立空倉虛擬帳戶")
    init.add_argument("--rules", type=Path, default=ROOT / "config" / "competition_rules.json")
    init.add_argument("--started-at", required=True)

    ensure = commands.add_parser("ensure-init", help="全新帳戶自動開帳；已有帳本驗證後沿用")
    ensure.add_argument("--rules", type=Path, default=ROOT / "config" / "competition_rules.json")

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
    apply.add_argument("--decision-repository", type=Path, default=ROOT / "artifacts" / "decisions")
    apply.add_argument("--decision-run-id", required=True)
    apply.add_argument("--execution-market", type=Path, required=True)
    apply.add_argument("--close-market", type=Path, required=True)
    apply.add_argument("--settlement-date", required=True)
    apply.add_argument("--run-id", required=True)

    for name, help_text in (
        ("settle", "以執行日官方收盤價結算最新 prepare 狀態對應的 Decision run"),
        ("daily", "每日推進：先結算前一份決策，再以新 Snapshot 建立決策前帳戶快照"),
    ):
        daily = commands.add_parser(name, help=help_text)
        daily.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
        daily.add_argument("--decision-repository", type=Path, default=ROOT / "artifacts" / "decisions")
        daily.add_argument("--settlement-days", type=int, default=2)
        daily.add_argument(
            "--captures-root", type=Path, default=ROOT / "artifacts" / "data" / "trading_status",
            help="政府開放 CSV 封存根目錄；使用其中最新的開休市日期表計算交割日",
        )
        if name == "daily":
            daily.add_argument("--snapshot", type=Path, required=True)
            daily.add_argument("--run-id", required=True)
            daily.add_argument("--account-output", type=Path, required=True)

    opening = commands.add_parser("open-competition", help="比賽第一天開全新帳戶並設為使用中；舊帳戶保留不刪")
    opening.add_argument("--new-account-id", required=True)
    opening.add_argument("--datapack", type=Path, help="第一個交易日的主辦方 DataPack；提供時核對全現金、0 檔且金額等於規則本金")

    check = commands.add_parser("reconcile", help="不跑 Agent，用主辦方 DataPack 檢查最新帳戶快照；一致回傳 0，不一致回傳 2")
    check.add_argument("--datapack", type=Path, required=True)
    check.add_argument("--cash-tolerance", default=None, help="現金差容忍比率（分母 nav_tradable）；預設 0.0005")

    commands.add_parser("status", help="顯示已驗證的最新帳戶狀態")
    verify = commands.add_parser("verify", help="驗證最新帳本 run 與 manifest")
    verify.add_argument("--run-id")

    args = parser.parse_args(argv)
    if args.command == "open-competition":
        from etf_agent.competition import open_competition_account
        try:
            result = open_competition_account(ROOT, args.new_account_id, datapack_path=args.datapack)
        except (VirtualAccountError, OSError, ValueError) as error:
            print("開新帳戶失敗：%s" % error, file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    args.account_id = args.account_id or active_account_id(ROOT)
    if args.command == "reconcile":
        from decimal import Decimal
        from etf_agent.competition import reconcile_account
        from etf_agent.competition.datapack import DEFAULT_CASH_TOLERANCE_RATE
        try:
            result = reconcile_account(ROOT, args.account_id, args.datapack,
                                       Decimal(args.cash_tolerance) if args.cash_tolerance else DEFAULT_CASH_TOLERANCE_RATE)
        except (VirtualAccountError, OSError, ValueError, ArithmeticError) as error:
            print("對帳失敗：%s" % error, file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "matched" else 2
    repository = VirtualAccountRepository(args.repository, args.account_id)
    service = VirtualAccountService(repository)
    try:
        if args.command == "init":
            result = service.initialize(args.rules, args.account_id, args.started_at)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "ensure-init":
            result = service.ensure_initialized(
                args.rules, args.account_id, datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
            print(json.dumps({"reused": result["reused"], "account_id": args.account_id}, ensure_ascii=False))
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
        if args.command in {"settle", "daily"}:
            limit_time = (read_json(args.snapshot).get("decision_cutoff") if args.command == "daily"
                          else datetime.now(timezone.utc).isoformat())
            calendar = load_calendar(args.captures_root, str(limit_time))
            runner = DailyAccountRunner(repository, args.database, args.decision_repository, args.settlement_days, calendar)
            result = runner.settle() if args.command == "settle" else runner.run(args.snapshot, args.run_id, args.account_output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
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
