#!/usr/bin/env python3
"""帳戶匯入、時間點對帳及決策帳戶輸出入口。"""

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.accounts import AccountDataError, AccountImporter, AccountRunRepository, AccountValidator  # noqa: E402
from etf_agent.accounts.integration import export_decision_account, reconcile  # noqa: E402


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AccountDataError("無法讀取 JSON：%s" % path) from error


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_approved_sources(path: Path) -> Mapping[str, object]:
    payload = read_json(path)
    if not isinstance(payload, Mapping) or payload.get("schema_version") != "1.0" or not isinstance(payload.get("approved_sources"), Mapping):
        raise AccountDataError("來源核准設定格式錯誤")
    return payload["approved_sources"]


def reconciliation_markdown(value: Mapping[str, object]) -> str:
    lines = [
        "# 帳戶對帳結果", "", "- run ID：`%s`" % value["run_id"],
        "- Snapshot：`%s`" % value["snapshot_id"], "- cutoff：`%s`" % value["decision_cutoff"],
        "- 狀態：`%s`" % value["status"], "", "| 檢查 | 結果 |", "| --- | --- |",
    ]
    lines.extend("| %s | %s |" % (name, "通過" if ok else "阻擋") for name, ok in value["checks"].items())
    lines.extend(["", "## 對帳數值", "", "| 欄位 | 金額 |", "| --- | ---: |"])
    for field in ("reported_cash", "calculated_cash", "available_cash", "calculated_market_value", "reported_nav", "calculated_nav", "nav_drift_rate"):
        lines.append("| %s | %s |" % (field, value[field]))
    lines.extend(["", "## 缺漏與限制", ""])
    errors = value.get("errors", [])
    lines.extend("- `%s`" % error for error in errors) if errors else lines.append("- 無")
    lines.extend(["", "決策契約相容：`%s`" % value["decision_compatible"], ""])
    return "\n".join(lines)


class AccountDataApplication:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description=__doc__)
        commands = parser.add_subparsers(dest="command", required=True)
        imp = commands.add_parser("import", help="匯入標準化 JSON 帳戶匯出檔並封存原件")
        imp.add_argument("--input", type=Path, required=True)
        imp.add_argument("--cutoff", required=True)
        imp.add_argument("--run-id", required=True)
        imp.add_argument("--mode", choices=("fixture", "official"), required=True)
        imp.add_argument("--sources-config", type=Path, default=self.root / "config" / "account_sources.json")
        imp.add_argument("--repository", type=Path, default=self.root / "artifacts" / "account_runs")
        validate = commands.add_parser("validate", help="驗證帳戶資料包")
        validate.add_argument("--input", type=Path, required=True)
        rec = commands.add_parser("reconcile", help="以固定 Snapshot 對帳 NAV 與現金")
        rec.add_argument("--account", type=Path, required=True)
        rec.add_argument("--snapshot", type=Path, required=True)
        rec.add_argument("--max-nav-drift-rate", required=True)
        rec.add_argument("--output", type=Path, required=True)
        rec.add_argument("--markdown", type=Path)
        export = commands.add_parser("export-decision-account", help="輸出通過驗證的決策帳戶欄位")
        export.add_argument("--account", type=Path, required=True)
        export.add_argument("--reconciliation", type=Path, required=True)
        export.add_argument("--snapshot", type=Path, required=True)
        export.add_argument("--output", type=Path, required=True)
        export.add_argument("--sources-config", type=Path, default=self.root / "config" / "account_sources.json")
        verify = commands.add_parser("verify-run", help="驗證不可變帳戶 run 與檔案雜湊")
        verify.add_argument("--run-id", required=True)
        verify.add_argument("--repository", type=Path, default=self.root / "artifacts" / "account_runs")
        return parser

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        args = self.build_parser().parse_args(argv)
        try:
            if args.command == "import":
                approved = read_approved_sources(args.sources_config)
                bundle = AccountImporter().import_file(args.input, args.cutoff, args.run_id, args.mode, approved)
                run = AccountRunRepository(args.repository).save(args.run_id, {"account_bundle.json": bundle}, args.input)
                print(str(run))
                return 0
            if args.command == "validate":
                payload = read_json(args.input)
                if not isinstance(payload, Mapping):
                    raise AccountDataError("帳戶資料包頂層必須是物件")
                errors = AccountValidator().validate(payload)
                print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
                return 0 if not errors else 2
            if args.command == "reconcile":
                account, snapshot = read_json(args.account), read_json(args.snapshot)
                if not isinstance(account, Mapping) or not isinstance(snapshot, Mapping):
                    raise AccountDataError("帳戶與 Snapshot 必須是 JSON 物件")
                result = reconcile(account, snapshot, args.max_nav_drift_rate)
                write_json(args.output, result)
                markdown_path = args.markdown or args.output.with_suffix(".md")
                markdown_path.parent.mkdir(parents=True, exist_ok=True)
                markdown_path.write_text(reconciliation_markdown(result), encoding="utf-8")
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result["status"] == "passed" else 2
            if args.command == "export-decision-account":
                account, result, snapshot = read_json(args.account), read_json(args.reconciliation), read_json(args.snapshot)
                if not all(isinstance(v, Mapping) for v in (account, result, snapshot)):
                    raise AccountDataError("帳戶、對帳結果與 Snapshot 須為 JSON 物件")
                decision_account = export_decision_account(account, result, snapshot, read_approved_sources(args.sources_config))
                write_json(args.output, decision_account)
                print(str(args.output))
                return 0
            path = AccountRunRepository(args.repository).verify(args.run_id)
            print(str(path))
            return 0
        except (AccountDataError, OSError, KeyError, TypeError) as error:
            print("帳戶資料失敗：%s" % error, file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(AccountDataApplication().run())
