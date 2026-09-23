#!/usr/bin/env python3
"""按需執行事件研究與固定格式報告交付工作流。"""

import argparse
import json
import sys
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

from etf_agent.automation import (  # noqa: E402
    ReportWorkflowError,
    ReportWorkflowRepository,
    ReportWorkflowService,
)


def _default_run_id(prefix: str = "daily") -> str:
    return "%s-%s" % (
        prefix,
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    )


class ReportWorkflowApplication:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        args = self.build_parser().parse_args(argv)
        try:
            if args.command == "status":
                return self.status(args)
            if args.command == "verify":
                path = ReportWorkflowRepository(args.repository).verify(args.run_id)
                self.emit({"valid": True, "output": str(path)})
                return 0
            if args.command == "resume":
                run_id = args.run_id or _default_run_id("resume")
                parent_run_id = args.from_run_id
            else:
                run_id = args.run_id or _default_run_id()
                parent_run_id = None
            result = ReportWorkflowService(
                ReportWorkflowRepository(args.repository)
            ).run(
                workflow_run_id=run_id,
                repository_root=args.repository,
                reports_root=args.reports,
                snapshot_path=args.snapshot,
                database_path=args.database,
                research_path=args.research,
                perception_path=args.perception,
                perception_bundle_path=args.perception_bundle,
                execution_mode=args.execution_mode,
                generated_at=args.generated_at,
                lookback_days=args.lookback_days,
                parent_run_id=parent_run_id,
                decision_repository=args.decision_repository,
                decision_run_id=args.decision_run_id,
                daily_report_repository=args.daily_report_repository,
            )
            self.emit(result)
            return self._exit_code(result.get("status"))
        except (ReportWorkflowError, OSError, TypeError, ValueError) as error:
            self.emit({"ok": False, "status": "failed", "error": str(error)})
            return 1

    def status(self, args: argparse.Namespace) -> int:
        latest = args.reports / "latest.json"
        if not latest.exists():
            self.emit(
                {
                    "ok": True,
                    "status": "not_started",
                    "latest": str(latest),
                }
            )
            return 0
        try:
            payload = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            self.emit({"ok": False, "status": "failed", "error": str(error)})
            return 1
        self.emit({"ok": True, "status": payload.get("status"), "latest": payload})
        return self._exit_code(payload.get("status"))

    @staticmethod
    def _exit_code(status: object) -> int:
        if status == "succeeded":
            return 0
        if status in {"waiting_for_agent", "waiting_for_decision", "blocked"}:
            return 3
        if status == "failed":
            return 2
        return 1

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description=__doc__)
        commands = parser.add_subparsers(dest="command", required=True)

        run = commands.add_parser("run", help="建立或更新一次報告工作流")
        self.add_workflow_arguments(run)

        resume = commands.add_parser("resume", help="從等待或阻擋的工作流建立續跑 attempt")
        self.add_workflow_arguments(resume)
        resume.add_argument("--from-run-id", required=True)

        status = commands.add_parser("status", help="顯示最近一次工作流結果")
        status.add_argument(
            "--reports", type=Path, default=self.root / "artifacts" / "reports"
        )

        verify = commands.add_parser("verify", help="驗證已封存工作流的 manifest 與雜湊")
        verify.add_argument("--run-id", required=True)
        verify.add_argument(
            "--repository", type=Path, default=self.root / "artifacts" / "report_runs"
        )
        return parser

    def add_workflow_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--snapshot",
            type=Path,
            default=self.root / "artifacts" / "research_snapshot_latest.json",
        )
        parser.add_argument(
            "--database", type=Path, default=self.root / "var" / "etf_agent.db"
        )
        parser.add_argument(
            "--research",
            type=Path,
            default=self.root / "artifacts" / "event_research_validated.json",
        )
        parser.add_argument("--perception", type=Path)
        parser.add_argument("--perception-bundle", type=Path)
        parser.add_argument(
            "--repository", type=Path, default=self.root / "artifacts" / "report_runs"
        )
        parser.add_argument(
            "--reports", type=Path, default=self.root / "artifacts" / "reports"
        )
        parser.add_argument("--decision-repository", type=Path)
        parser.add_argument("--decision-run-id")
        parser.add_argument(
            "--daily-report-repository",
            type=Path,
            default=self.root / "artifacts" / "pipeline_runs",
        )
        parser.add_argument("--run-id")
        parser.add_argument("--execution-mode", choices=("official", "fixture"), default="official")
        parser.add_argument("--generated-at")
        parser.add_argument("--lookback-days", type=int, default=45)

    @staticmethod
    def emit(payload: object) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(ReportWorkflowApplication().run())
