#!/usr/bin/env python3
"""Research Report V0 的確定性建立與驗證入口。"""

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

from etf_agent.reporting import (  # noqa: E402
    ResearchReportApplicationService,
    ResearchReportError,
)


class ResearchReportApplication:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        args = self.build_parser().parse_args(argv)
        try:
            service = ResearchReportApplicationService.from_paths(
                args.snapshot,
                args.research,
                args.perception,
                args.perception_bundle,
            )
            if args.command == "build":
                result = service.build_files(
                    args.json_output,
                    args.markdown_output,
                    report_id=args.report_id,
                    generated_at=args.generated_at,
                )
            else:
                result = service.validate_file(args.input)
                self.emit(result)
                return 0 if result["valid"] else 2
        except (ResearchReportError, OSError, TypeError, ValueError) as error:
            self.emit({"ok": False, "error": str(error)})
            return 1
        self.emit({"ok": True, "data": result})
        return 0

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description=__doc__)
        commands = parser.add_subparsers(dest="command", required=True)
        for name, help_text in (
            ("build", "建立並驗證 ResearchReport JSON 與 Markdown"),
            ("validate", "以同一組上游輸入重建並驗證 ResearchReport"),
        ):
            command = commands.add_parser(name, help=help_text)
            self.add_inputs(command)
            if name == "build":
                command.add_argument(
                    "--json-output",
                    type=Path,
                    default=self.root / "artifacts" / "research_report.json",
                )
                command.add_argument(
                    "--markdown-output",
                    type=Path,
                    default=self.root / "artifacts" / "research_report.md",
                )
                command.add_argument("--report-id")
                command.add_argument("--generated-at")
            else:
                command.add_argument("--input", type=Path, required=True)
        return parser

    def add_inputs(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--snapshot",
            type=Path,
            default=self.root / "artifacts" / "research_snapshot_latest.json",
        )
        parser.add_argument(
            "--research",
            type=Path,
            default=self.root / "artifacts" / "event_research_validated.json",
        )
        parser.add_argument("--perception", type=Path)
        parser.add_argument("--perception-bundle", type=Path)

    @staticmethod
    def emit(payload) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(ResearchReportApplication().run())
