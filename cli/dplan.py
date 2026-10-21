#!/usr/bin/env python3
"""Build or locally validate an official-format D-Plan v4.3 candidate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.competition import (  # noqa: E402
    DPlanContextBuilder,
    DPlanError,
    DPlanExportService,
    DPlanExporter,
    DPlanValidator,
    load_decision_run,
    verify_account_binding,
)
from etf_agent.competition.service import read_object, write_new_json  # noqa: E402


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    latest = commands.add_parser("build-latest", help="匯出最新可用每日決策；不需輸入 run ID 或路徑")
    latest.add_argument("--account-id", default="ai-cup-2026")
    latest.add_argument("--profile", type=Path, default=ROOT / "config" / "competition_profile.json")

    build = commands.add_parser("build", help="進階入口：由指定封存決策匯出 D-Plan 候選")
    build.add_argument("--team-id", help="僅手動 context 匯出時必填；須與 profile 一致")
    build.add_argument("--trade-date", help="省略時由每日 run 的 target_session 自動取得")
    build.add_argument("--context", type=Path, help="既有手動 context；提供時不啟用自動整理")
    build.add_argument("--profile", type=Path, default=ROOT / "config" / "competition_profile.json")
    build.add_argument("--daily-run", type=Path, help="本次每日流程的 pipeline.json；自動整理 context 時必填")
    build.add_argument("--context-output", type=Path, help="自動整理的 context 輸出位置（不可覆寫）")
    build.add_argument("--decision-repository", type=Path, required=True)
    build.add_argument("--decision-run-id", required=True)
    build.add_argument("--virtual-account-repository", type=Path, default=ROOT / "artifacts" / "virtual_accounts")
    build.add_argument("--virtual-account-account-id", required=True)
    build.add_argument("--virtual-account-run-id", required=True, help="決策使用的 prepare-day 封存 run")
    build.add_argument("--output", type=Path)

    validate = commands.add_parser("validate", help="執行本地結構及引用鏈檢查")
    validate.add_argument("--input", type=Path, required=True)
    context = commands.add_parser("build-context", help="進階入口：由指定 Decision run 自動整理 D-Plan context")
    context.add_argument("--decision-repository", type=Path, required=True)
    context.add_argument("--decision-run-id", required=True)
    context.add_argument("--profile", type=Path, default=ROOT / "config" / "competition_profile.json")
    context.add_argument("--daily-run", type=Path, required=True)
    context.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    service = DPlanExportService(ROOT)
    try:
        if args.command == "validate":
            errors = DPlanValidator().validate(read_object(args.input, "D-Plan"))
            print(json.dumps({"valid": not errors, "validator": "local-v4.3-subset-not-organizer-server", "errors": errors}, ensure_ascii=False, indent=2))
            return 0 if not errors else 2
        if args.command == "build-latest":
            print(json.dumps(service.export_latest(account_id=args.account_id, profile_path=args.profile), ensure_ascii=False, indent=2))
            return 0
        if args.command == "build-context":
            artifacts = load_decision_run(str(args.decision_repository), args.decision_run_id)
            context = DPlanContextBuilder().build(
                artifacts,
                read_object(args.profile, "competition_profile"),
                read_object(args.daily_run, "每日 pipeline 摘要"),
                service.code_version(),
            )
            write_new_json(args.output, context)
            print(json.dumps({"ok": True, "output": str(args.output), "local_validation": "context-built-from-verified-artifacts"}, ensure_ascii=False, indent=2))
            return 0
        if args.context is not None:
            if not args.team_id or not args.trade_date:
                raise DPlanError("手動 context 匯出必須提供 --team-id 與 --trade-date")
            artifacts = load_decision_run(str(args.decision_repository), args.decision_run_id)
            verify_account_binding(
                artifacts,
                str(args.virtual_account_repository),
                args.virtual_account_account_id,
                args.virtual_account_run_id,
            )
            plan = DPlanExporter().build(args.team_id, args.trade_date, read_object(args.context, "D-Plan context"), artifacts)
            output = args.output or ROOT / "artifacts" / "competition_d_plan" / ("D-Plan_%s_%s.json" % (args.team_id, args.trade_date))
            write_new_json(output, plan)
            print(json.dumps({"status": "ready", "output": str(output), "local_validation": "passed", "organizer_server_validation": "not_run", "requires_human_review": True}, ensure_ascii=False, indent=2))
            return 0
        if args.daily_run is None:
            raise DPlanError("自動整理 context 必須提供 --daily-run；日常手動重試請使用 build-latest")
        daily_run = read_object(args.daily_run, "每日 pipeline 摘要")
        profile = read_object(args.profile, "competition_profile")
        if args.team_id is not None and args.team_id != profile.get("team_id"):
            raise DPlanError("--team-id 必須與 competition_profile.team_id 一致")
        result = service.export(
            decision_repository=args.decision_repository,
            decision_run_id=args.decision_run_id,
            daily_run=daily_run,
            profile_path=args.profile,
            account_id=args.virtual_account_account_id,
            account_run_id=args.virtual_account_run_id,
            output=args.output,
            context_output=args.context_output,
            trade_date=args.trade_date,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DPlanError, OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
