"""D-Plan candidate export orchestration from sealed daily-run artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional

from etf_agent.core.run_layout import list_run_dirs

from .context import DPlanContextBuilder
from .dplan import DPlanError, DPlanExporter, load_decision_run, verify_account_binding


TAIPEI = timezone(timedelta(hours=8))
DEFAULT_OUTPUT_DIRECTORY = Path("artifacts") / "competition_d_plan"


def read_object(path: Path, label: str) -> Dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DPlanError("%s 無法讀取：%s" % (label, path)) from error
    if not isinstance(value, dict):
        raise DPlanError("%s 必須是 JSON 物件" % label)
    return value


def write_new_json(path: Path, payload: Mapping[str, object]) -> None:
    """Atomically create an immutable JSON artifact without overwriting a candidate."""
    path = Path(path)
    if path.exists():
        raise DPlanError("輸出檔已存在，拒絕覆寫：%s" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: Optional[str] = None
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


class DPlanExportService:
    """Build a submission candidate using only the current sealed daily decision.

    The caller supplies no ad-hoc identifiers in the normal path: the daily run
    summary names the decision run and target trading session, while the current
    virtual-account latest state supplies the required prepare-day run.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def code_version(self) -> str:
        """Return a reproducible local code identifier without trusting caller input."""
        try:
            revision = subprocess.run(
                ["git", "rev-parse", "--short=12", "HEAD"], cwd=self.root, check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            dirty = bool(subprocess.run(
                ["git", "status", "--porcelain"], cwd=self.root, check=True,
                capture_output=True, text=True,
            ).stdout.strip())
        except (OSError, subprocess.CalledProcessError):
            digest = hashlib.sha256()
            included = 0
            for directory in ("src", "cli", "tradeagent", "config"):
                source = self.root / directory
                if not source.exists():
                    continue
                for path in sorted(item for item in source.rglob("*") if item.is_file()):
                    if path.suffix not in {".py", ".md", ".json", ".yaml", ".yml"}:
                        continue
                    digest.update(str(path.relative_to(self.root)).encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(path.read_bytes())
                    digest.update(b"\0")
                    included += 1
            if not included:
                raise DPlanError("無法建立程式版本內容雜湊")
            return "sha256:%s" % digest.hexdigest()[:12]
        return "git:%s%s" % (revision, "-dirty" if dirty else "")

    @staticmethod
    def trade_date(daily_run: Mapping[str, object]) -> str:
        session = daily_run.get("target_session")
        if not isinstance(session, Mapping):
            raise DPlanError("每日流程摘要缺少 target_session，無法決定 D-Plan trade_date")
        value = session.get("start")
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as error:
            raise DPlanError("target_session.start 無法解析為含時區時間") from error
        if parsed.tzinfo is None:
            raise DPlanError("target_session.start 缺少時區")
        return parsed.astimezone(TAIPEI).date().isoformat()

    def output_paths(self, team_id: str, trade_date: str, decision_run_id: str) -> tuple[Path, Path]:
        root = self.root / DEFAULT_OUTPUT_DIRECTORY
        return (
            root / ("D-Plan_%s_%s.json" % (team_id, trade_date)),
            root / "contexts" / ("context_%s.json" % decision_run_id),
        )

    def current_account_run_id(self, account_id: str) -> str:
        latest = read_object(
            self.root / "artifacts" / "ledger" / account_id / "latest.json",
            "VirtualAccount latest",
        )
        run_id = latest.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise DPlanError("VirtualAccount latest 缺少 run_id")
        return run_id

    def export(
        self,
        *,
        decision_repository: Path,
        decision_run_id: str,
        daily_run: Mapping[str, object],
        profile_path: Path,
        account_id: str,
        account_run_id: str,
        output: Optional[Path] = None,
        context_output: Optional[Path] = None,
        trade_date: Optional[str] = None,
    ) -> Dict[str, object]:
        profile = read_object(profile_path, "competition_profile")
        team_id = profile.get("team_id")
        if not isinstance(team_id, str) or not team_id:
            raise DPlanError("competition_profile.team_id 不得為空")
        effective_trade_date = trade_date or self.trade_date(daily_run)
        plan_output, generated_context_output = self.output_paths(team_id, effective_trade_date, decision_run_id)
        plan_output = Path(output) if output is not None else plan_output
        context_output = Path(context_output) if context_output is not None else generated_context_output

        # Validate all inputs before creating either immutable output file.
        artifacts = load_decision_run(str(decision_repository), decision_run_id)
        context = DPlanContextBuilder().build(artifacts, profile, daily_run, self.code_version())
        account = verify_account_binding(
            artifacts,
            str(self.root / "artifacts" / "ledger"),
            account_id,
            account_run_id,
        )
        plan = DPlanExporter().build(team_id, effective_trade_date, context, artifacts)
        if plan_output.exists():
            raise DPlanError("輸出檔已存在，拒絕覆寫：%s" % plan_output)
        if context_output.exists():
            raise DPlanError("context 輸出檔已存在，拒絕覆寫：%s" % context_output)
        write_new_json(context_output, context)
        write_new_json(plan_output, plan)
        return {
            "status": "ready",
            "output": str(plan_output),
            "context_output": str(context_output),
            "trade_date": effective_trade_date,
            "decision_run_id": decision_run_id,
            "account_binding": account,
            "local_validation": "passed",
            "organizer_server_validation": "not_run",
            "requires_human_review": True,
        }

    def export_latest(self, *, account_id: str = "ai-cup-2026", profile_path: Optional[Path] = None) -> Dict[str, object]:
        """Manually retry the newest completed daily run without copying run IDs."""
        candidates = []
        daily_root = self.root / "artifacts" / "daily"
        for run_dir in list_run_dirs(daily_root):
            summary_path = run_dir / "pipeline.json"
            if not summary_path.is_file():
                continue
            try:
                daily_run = read_object(summary_path, "每日 pipeline 摘要")
            except DPlanError:
                continue
            if daily_run.get("status") != "completed" or daily_run.get("decision_status") not in {"approved", "no_trade"}:
                continue
            decision_run_id = daily_run.get("decision_run_id")
            finished_at = daily_run.get("finished_at")
            if not isinstance(decision_run_id, str) or not isinstance(finished_at, str):
                continue
            try:
                completed = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                if completed.tzinfo is None:
                    continue
            except ValueError:
                continue
            candidates.append((completed, daily_run, decision_run_id))
        if not candidates:
            raise DPlanError("找不到可匯出的 approved/no_trade 每日決策")
        _, daily_run, decision_run_id = max(candidates, key=lambda item: item[0])
        return self.export(
            decision_repository=self.root / "artifacts" / "decisions",
            decision_run_id=decision_run_id,
            daily_run=daily_run,
            profile_path=profile_path or self.root / "config" / "competition_profile.json",
            account_id=account_id,
            account_run_id=self.current_account_run_id(account_id),
        )
