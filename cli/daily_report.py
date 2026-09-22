#!/usr/bin/env python3
"""建立、驗證或檢查離線 DailyReport pipeline run。"""

import argparse
import json
import sys
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

from etf_agent.automation import (  # noqa: E402
    AutomationReportingApplicationService,
    AutomationReportingError,
    PipelineRepository,
)


class DailyReportApplication:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        args = self.build_parser().parse_args(argv)
        try:
            if args.command == "verify-run":
                run_dir = PipelineRepository(args.repository).verify(args.pipeline_run_id)
                self.emit({"valid": True, "output": str(run_dir)})
                return 0
            if args.command == "run":
                result = AutomationReportingApplicationService.execute_from_paths(
                    args.pipeline_run_id,
                    args.repository,
                    args.execution_mode,
                    args.generated_at,
                    args.snapshot,
                    args.research,
                    args.decision_repository,
                    args.decision_run_id,
                    args.perception,
                    args.perception_bundle,
                )
                self.emit(result)
                return 0 if result["ok"] else 2
            service = AutomationReportingApplicationService.from_paths(
                args.snapshot,
                args.research,
                args.decision_repository,
                args.decision_run_id,
                args.perception,
                args.perception_bundle,
            )
            result = service.validate_run(args.repository, args.pipeline_run_id)
            self.emit(result)
            return 0 if result["valid"] else 2
        except (AutomationReportingError, OSError, TypeError, ValueError) as error:
            self.emit({"ok": False, "error": str(error)})
            return 1

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description=__doc__)
        commands = parser.add_subparsers(dest="command", required=True)
        for name, help_text in (
            ("run", "建立已驗證 DailyReport，失敗時封存 FailureReport"),
            ("validate", "以同一組上游輸入重建並驗證 DailyReport"),
        ):
            command = commands.add_parser(name, help=help_text)
            self.add_inputs(command)
            command.add_argument("--pipeline-run-id", required=True)
            command.add_argument(
                "--repository",
                type=Path,
                default=self.root / "artifacts" / "pipeline_runs",
            )
            if name == "run":
                command.add_argument(
                    "--execution-mode", choices=("fixture", "official"), required=True
                )
                command.add_argument("--generated-at", required=True)
        verify = commands.add_parser("verify-run", help="驗證已封存 pipeline run 檔案完整性")
        verify.add_argument("--pipeline-run-id", required=True)
        verify.add_argument(
            "--repository",
            type=Path,
            default=self.root / "artifacts" / "pipeline_runs",
        )
        return parser

    @staticmethod
    def add_inputs(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--snapshot", type=Path, required=True)
        parser.add_argument("--research", type=Path, required=True)
        parser.add_argument("--decision-repository", type=Path, required=True)
        parser.add_argument("--decision-run-id", required=True)
        parser.add_argument("--perception", type=Path)
        parser.add_argument("--perception-bundle", type=Path)

    @staticmethod
    def emit(payload: object) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(DailyReportApplication().run())
