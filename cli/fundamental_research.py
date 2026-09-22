#!/usr/bin/env python3
"""基本面研究 Agent 的唯讀資料包、指標與結果驗證入口。"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence


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

from etf_agent.fundamentals import (  # noqa: E402
    FundamentalResearchApplicationService,
    FundamentalToolError,
)
from etf_agent.fundamentals.analysis import METRIC_KEYS, POLICY_VERSION  # noqa: E402


class FundamentalResearchApplication:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        args = self.build_parser().parse_args(argv)
        try:
            service = FundamentalResearchApplicationService.from_snapshot_path(
                args.snapshot
            )
            if args.command == "status":
                result = service.tools.status()
                return self.emit_result(result, "completed")
            if args.command == "build-bundle":
                request = {
                    "request_id": args.request_id or str(uuid.uuid4()),
                    "symbols": sorted(
                        {symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()}
                    ),
                    "fiscal_year": args.fiscal_year,
                    "fiscal_quarter": args.fiscal_quarter,
                    "required_statement_types": ["income_statement", "balance_sheet"],
                    "metric_keys": sorted(args.metric or list(METRIC_KEYS)),
                    "policy_version": POLICY_VERSION,
                }
                result = service.build_bundle_file(
                    args.output,
                    request,
                    bundle_id=args.bundle_id,
                    generated_at=args.generated_at,
                )
                return self.emit_result(result, str(result["status"]))
            if args.command == "compute-metrics":
                result = service.compute_metrics_file(
                    args.bundle,
                    args.output,
                    metrics_id=args.metrics_id,
                    computed_at=args.computed_at,
                )
                return self.emit_result(result, str(result["status"]))
            if args.command == "validate-result":
                result = service.validate_result_file(
                    args.bundle, args.metrics, args.input, args.output
                )
                self.emit({"ok": result["valid"], "data": result})
                if not result["valid"]:
                    return 2
                return 0 if result["result"]["status"] == "completed" else 2
            result = service.archive_result_file(
                args.bundle, args.metrics, args.input, args.output
            )
            self.emit({"ok": result["valid"], "data": result})
            if not result["valid"]:
                return 2
            return 0 if result["result"]["status"] == "completed" else 2
        except (FundamentalToolError, OSError, TypeError, ValueError) as error:
            self.emit({"ok": False, "error": str(error)})
            return 1

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--snapshot",
            type=Path,
            default=self.root / "artifacts" / "research_snapshot_latest.json",
            help="同一 cutoff 的可用 ResearchSnapshot JSON",
        )
        commands = parser.add_subparsers(dest="command", required=True)
        commands.add_parser("status", help="檢查財報 Snapshot、業別及可用範圍")

        bundle = commands.add_parser(
            "build-bundle", help="建立固定 Snapshot 的 FundamentalDataBundle"
        )
        bundle.add_argument("--symbols", required=True, help="以逗號分隔的股票代號")
        bundle.add_argument("--fiscal-year", type=int, required=True)
        bundle.add_argument("--fiscal-quarter", type=int, required=True)
        bundle.add_argument("--metric", action="append", choices=METRIC_KEYS)
        bundle.add_argument("--request-id")
        bundle.add_argument("--bundle-id")
        bundle.add_argument("--generated-at")
        bundle.add_argument("--output", type=Path, required=True)

        metrics = commands.add_parser(
            "compute-metrics", help="從 Bundle 重算確定性財務指標"
        )
        metrics.add_argument("--bundle", type=Path, required=True)
        metrics.add_argument("--metrics-id")
        metrics.add_argument("--computed-at")
        metrics.add_argument("--output", type=Path, required=True)

        validate = commands.add_parser(
            "validate-result", help="驗證基本面研究草稿及產出含雜湊的結果"
        )
        self._add_result_paths(validate)
        validate.add_argument("--output", type=Path, required=True)

        archive = commands.add_parser(
            "archive", help="驗證後以不覆寫方式封存基本面研究結果"
        )
        self._add_result_paths(archive)
        archive.add_argument("--output", type=Path, required=True)
        return parser

    @staticmethod
    def _add_result_paths(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--bundle", type=Path, required=True)
        parser.add_argument("--metrics", type=Path, required=True)
        parser.add_argument("--input", type=Path, required=True)

    @staticmethod
    def emit_result(payload: dict, status: str) -> int:
        FundamentalResearchApplication.emit({"ok": True, "data": payload})
        return 0 if status == "completed" else 2

    @staticmethod
    def emit(payload: dict) -> None:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: Optional[Sequence[str]] = None) -> int:
    return FundamentalResearchApplication().run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
