#!/usr/bin/env python3
"""交易狀態 M1 的確定性 bundle／assessment CLI。"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


class ProjectRootLocator:
    def __init__(self, script_path: Path):
        self.script_path = script_path

    def locate(self) -> Path:
        for parent in self.script_path.resolve().parents:
            if (parent / "src" / "etf_agent").is_dir():
                return parent
        raise RuntimeError("找不到 ETF_AGENTS 專案根目錄")


ROOT = ProjectRootLocator(Path(__file__)).locate()
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import (  # noqa: E402
    MarketDataDatabase,
    TradingStatusBundleBuilder,
    TradingStatusBundleValidator,
    TradingStatusError,
    TradingStatusRepository,
    TradingStatusRequest,
)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TradingStatusError("無法讀取 JSON：%s：%s" % (path, error)) from error


class TradingStatusApplication:
    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description=__doc__)
        commands = parser.add_subparsers(dest="command", required=True)

        build = commands.add_parser("build-bundle", help="由保存的官方回應資料建立狀態包")
        build.add_argument("--request", type=Path, required=True)
        build.add_argument("--records", type=Path, required=True)
        build.add_argument("--coverage", type=Path, required=True)
        build.add_argument("--output", type=Path, required=True)
        build.add_argument("--database", type=Path)

        validate = commands.add_parser("validate", help="重建並驗證狀態包")
        validate.add_argument("--input", type=Path, required=True)

        status = commands.add_parser("status", help="查詢 SQLite 已保存的交易狀態包")
        status.add_argument("--database", type=Path, required=True)
        return parser

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        args = self.build_parser().parse_args(argv)
        try:
            if args.command == "build-bundle":
                return self.build_bundle(args)
            if args.command == "validate":
                return self.validate(args)
            return self.status(args)
        except (TradingStatusError, OSError, KeyError, TypeError) as error:
            print("交易狀態失敗：%s" % error, file=sys.stderr)
            return 1

    @staticmethod
    def build_bundle(args: argparse.Namespace) -> int:
        request = TradingStatusRequest.from_dict(_read_json(args.request))
        records = _read_json(args.records)
        coverage = _read_json(args.coverage)
        if not isinstance(records, list) or not isinstance(coverage, list):
            raise TradingStatusError("records 與 coverage JSON 頂層必須是陣列")
        bundle, assessment = TradingStatusBundleBuilder(request, records, coverage).build()
        output = {"bundle": bundle, "assessment": assessment}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if args.database:
            TradingStatusRepository().save(MarketDataDatabase(args.database), bundle)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if bundle["status"] == "completed" else 2

    @staticmethod
    def validate(args: argparse.Namespace) -> int:
        payload = _read_json(args.input)
        if not isinstance(payload, Mapping):
            raise TradingStatusError("輸入頂層必須是物件")
        bundle = payload.get("bundle", payload)
        assessment = payload.get("assessment") if "bundle" in payload else None
        errors = TradingStatusBundleValidator().validate(bundle, assessment)
        result = {"valid": not errors, "errors": errors}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not errors else 2

    @staticmethod
    def status(args: argparse.Namespace) -> int:
        database = MarketDataDatabase(args.database)
        database.initialize()
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT bundle_id, snapshot_id, decision_cutoff, status, created_at
                FROM trading_status_bundles
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        print(
            json.dumps(
                {"latest": dict(row) if row else None},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(TradingStatusApplication().run())
