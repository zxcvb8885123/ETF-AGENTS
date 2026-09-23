#!/usr/bin/env python3
"""歷史回測、整張成交與跨日帳務工具入口。"""

import argparse
import json
import sys
from pathlib import Path


def _root(path: Path) -> Path:
    for parent in path.resolve().parents:
        if (parent / "src" / "etf_agent").is_dir():
            return parent
    raise RuntimeError("找不到 ETF-AGENTS 專案根目錄")


ROOT = _root(Path(__file__))
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.backtest import BacktestRepository, BacktestRequestValidator, BacktestService, BacktestToolError  # noqa: E402


def read_json(path: Path, label: str):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BacktestToolError("無法讀取%s：%s" % (label, error)) from error
    if not isinstance(value, dict):
        raise BacktestToolError("%s 必須是 JSON 物件" % label)
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--daily-inputs", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-request")
    commands.add_parser("inspect-coverage")
    replay = commands.add_parser("replay")
    replay.add_argument("--output", type=Path)
    validate = commands.add_parser("validate-run")
    validate.add_argument("--input", type=Path, required=True)
    save = commands.add_parser("save-run")
    save.add_argument("--input", type=Path, required=True)
    save.add_argument("--run-id", required=True)
    save.add_argument("--repository", type=Path, default=ROOT / "artifacts" / "backtests")
    report = commands.add_parser("build-report")
    report.add_argument("--input", type=Path, required=True)
    report.add_argument("--json-output", type=Path)
    report.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    try:
        request = read_json(args.request, " BacktestRequest")
        if args.command == "validate-request":
            errors = BacktestRequestValidator().validate(request)
            print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
            return 0 if not errors else 2
        service = BacktestService()
        if args.command == "build-report":
            report_data = service.build_report(request, read_json(args.input, " BacktestRun"))
            if args.json_output:
                args.json_output.parent.mkdir(parents=True, exist_ok=True)
                args.json_output.write_text(json.dumps(report_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if args.markdown_output:
                args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
                args.markdown_output.write_text(service.render_report_markdown(report_data), encoding="utf-8")
            print(json.dumps({"ok": True, "data": report_data}, ensure_ascii=False, indent=2))
            return 0
        if args.daily_inputs is None:
            raise BacktestToolError("inspect-coverage、replay、validate-run 與 save-run 必須提供 --daily-inputs")
        inputs = read_json(args.daily_inputs, " daily inputs")
        if args.command == "inspect-coverage":
            result = service.inspect_fixture(request, inputs)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["valid"] else 2
        if args.command == "replay":
            result = service.run_fixture(request, inputs)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"ok": True, "data": result}, ensure_ascii=False, indent=2))
            return 0
        result = read_json(args.input, " BacktestRun")
        if args.command == "save-run":
            errors = service.validate_fixture(request, inputs, result)
            if errors:
                print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
                return 2
            output = BacktestRepository(args.repository).save(args.run_id, {"request": request, "daily_inputs": inputs, "run": result})
            BacktestRepository(args.repository).verify(args.run_id)
            print(json.dumps({"ok": True, "output": str(output)}, ensure_ascii=False, indent=2))
            return 0
        errors = service.validate_fixture(request, inputs, result)
        print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
        return 0 if not errors else 2
    except (BacktestToolError, OSError, TypeError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
