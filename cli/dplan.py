#!/usr/bin/env python3
"""Build or locally validate an official-format D-Plan v4.0 candidate."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.competition import DPlanError, DPlanExporter, DPlanValidator, load_decision_run  # noqa: E402


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="由驗證過的決策 run 產生 D-Plan 候選")
    build.add_argument("--team-id", required=True)
    build.add_argument("--trade-date", required=True)
    build.add_argument("--context", type=Path, required=True, help="本地 Agent 審閱的 sources/observations/market_view/inferences/metadata JSON")
    build.add_argument("--decision-repository", type=Path, required=True)
    build.add_argument("--decision-run-id", required=True)
    build.add_argument("--output", type=Path)
    validate = commands.add_parser("validate", help="執行本地結構及引用鏈檢查")
    validate.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            errors = DPlanValidator().validate(read_object(args.input, "D-Plan"))
            print(json.dumps({"valid": not errors, "validator": "local-v4-subset-not-organizer-server", "errors": errors}, ensure_ascii=False, indent=2))
            return 0 if not errors else 2
        context = read_object(args.context, "D-Plan context")
        artifacts = load_decision_run(str(args.decision_repository), args.decision_run_id)
        plan = DPlanExporter().build(args.team_id, args.trade_date, context, artifacts)
        output = args.output or ROOT / "artifacts" / "competition" / ("D-Plan_%s_%s.json" % (args.team_id, args.trade_date))
        write_new(output, plan)
        print(json.dumps({"ok": True, "output": str(output), "local_validation": "passed", "organizer_server_validation": "not_run", "requires_human_review": True}, ensure_ascii=False, indent=2))
        return 0
    except (DPlanError, OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
