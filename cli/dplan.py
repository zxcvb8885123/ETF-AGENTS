#!/usr/bin/env python3
"""Build or locally validate an official-format D-Plan v4.3 candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.competition import (  # noqa: E402
    DPlanError,
    DPlanContextBuilder,
    DPlanExporter,
    DPlanValidator,
    load_decision_run,
    verify_account_binding,
)


def read_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DPlanError("%s 無法讀取：%s" % (label, path)) from error
    if not isinstance(value, dict):
        raise DPlanError("%s 必須是 JSON 物件" % label)
    return value


def write_new(path: Path, payload: dict) -> None:
    if path.exists():
        raise DPlanError("輸出檔已存在，拒絕覆寫：%s" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temp_name = stream.name
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp_name, path)
    except FileExistsError as error:
        raise DPlanError("輸出檔已存在，拒絕覆寫：%s" % path) from error
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def code_version() -> str:
    """Return a reproducible local code identifier without trusting a caller string."""
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, check=True,
            capture_output=True, text=True,
        ).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        # Docker 正式執行環境不掛載 .git；改以決策程式、Skill 和設定的內容雜湊識別。
        digest = hashlib.sha256()
        included = 0
        for directory in ("src", "cli", "tradeagent", "config"):
            root = ROOT / directory
            if not root.exists():
                continue
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                if path.suffix not in {".py", ".md", ".json", ".yaml", ".yml"}:
                    continue
                digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")
                included += 1
        if not included:
            raise DPlanError("無法建立程式版本內容雜湊")
        return "sha256:%s" % digest.hexdigest()[:12]
    return "git:%s%s" % (revision, "-dirty" if dirty else "")


def build_context(args) -> tuple[dict, dict]:
    artifacts = load_decision_run(str(args.decision_repository), args.decision_run_id)
    profile = read_object(args.profile, "competition_profile")
    daily_run = read_object(args.daily_run, "每日 pipeline 摘要")
    context = DPlanContextBuilder().build(artifacts, profile, daily_run, code_version())
    return context, profile


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="由驗證過的決策 run 產生 D-Plan 候選")
    build.add_argument("--team-id", help="手動 context 時必填；自動 context 預設讀取 competition_profile")
    build.add_argument("--trade-date", required=True)
    build.add_argument("--context", type=Path, help="既有手動 context；提供時不啟用自動整理")
    build.add_argument("--profile", type=Path, default=ROOT / "config" / "competition_profile.json", help="已確認的競賽設定檔")
    build.add_argument("--daily-run", type=Path, help="本次每日流程的 pipeline.json；自動整理 context 時必填")
    build.add_argument("--context-output", type=Path, help="自動整理的 context 輸出位置（不可覆寫）")
    build.add_argument("--decision-repository", type=Path, required=True)
    build.add_argument("--decision-run-id", required=True)
    build.add_argument("--virtual-account-repository", type=Path, default=ROOT / "artifacts" / "ledger")
    build.add_argument("--virtual-account-account-id", required=True)
    build.add_argument("--virtual-account-run-id", required=True, help="決策使用的 prepare-day 封存 run")
    build.add_argument("--output", type=Path)
    validate = commands.add_parser("validate", help="執行本地結構及引用鏈檢查")
    validate.add_argument("--input", type=Path, required=True)
    context = commands.add_parser("build-context", help="由驗證過的 Decision run 自動整理 D-Plan context")
    context.add_argument("--decision-repository", type=Path, required=True)
    context.add_argument("--decision-run-id", required=True)
    context.add_argument("--profile", type=Path, default=ROOT / "config" / "competition_profile.json")
    context.add_argument("--daily-run", type=Path, required=True, help="本次每日流程的 pipeline.json")
    context.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            errors = DPlanValidator().validate(read_object(args.input, "D-Plan"))
            print(json.dumps({"valid": not errors, "validator": "local-v4.3-subset-not-organizer-server", "errors": errors}, ensure_ascii=False, indent=2))
            return 0 if not errors else 2
        if args.command == "build-context":
            generated, _ = build_context(args)
            write_new(args.output, generated)
            print(json.dumps({"ok": True, "output": str(args.output), "local_validation": "context-built-from-verified-artifacts"}, ensure_ascii=False, indent=2))
            return 0
        if args.context is not None:
            context = read_object(args.context, "D-Plan context")
            if not args.team_id:
                raise DPlanError("手動 context 匯出必須提供 --team-id")
            team_id = args.team_id
        else:
            if args.daily_run is None:
                raise DPlanError("自動整理 context 必須提供 --daily-run；或改用 --context")
            context, profile = build_context(args)
            team_id = args.team_id or str(profile["team_id"])
            if team_id != profile["team_id"]:
                raise DPlanError("--team-id 必須與 competition_profile.team_id 一致")
            context_output = args.context_output or ROOT / "artifacts" / "competition" / "contexts" / ("context_%s.json" % args.decision_run_id)
            write_new(context_output, context)
        artifacts = load_decision_run(str(args.decision_repository), args.decision_run_id)
        account = verify_account_binding(
            artifacts, str(args.virtual_account_repository),
            args.virtual_account_account_id, args.virtual_account_run_id,
        )
        plan = DPlanExporter().build(team_id, args.trade_date, context, artifacts)
        output = args.output or ROOT / "artifacts" / "competition" / ("D-Plan_%s_%s.json" % (team_id, args.trade_date))
        write_new(output, plan)
        print(json.dumps({"ok": True, "output": str(output), "account_binding": account, "local_validation": "passed", "organizer_server_validation": "not_run", "requires_human_review": True}, ensure_ascii=False, indent=2))
        return 0
    except (DPlanError, OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
