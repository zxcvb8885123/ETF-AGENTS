"""D-Plan candidate export orchestration from sealed daily-run artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional

from etf_agent.core.run_layout import list_run_dirs
from etf_agent.virtual_account import active_account_id

from .context import DPlanContextBuilder
from .datapack import check_sells
from .dplan import DPlanError, DPlanExporter, load_decision_run, verify_account_binding


TAIPEI = timezone(timedelta(hours=8))
DEFAULT_OUTPUT_DIRECTORY = Path("artifacts") / "competition_d_plan"
CODE_SNAPSHOT_DIRECTORY = Path("artifacts") / "code_versions"
# 會影響決策行為的路徑；參數與提示詞模板也算程式碼。
CODE_VERSION_PATHS = ("src", "cli", "tradeagent", "config", "scripts", "data", "start.sh", "requirements.txt")


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


def replace_json(path: Path, payload: Mapping[str, object]) -> bool:
    """Atomically write a JSON candidate, replacing an older one for the same trade date.

    Returns True when an existing file was replaced.
    """
    path = Path(path)
    replaced = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temp_name = stream.name
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return replaced


class DPlanExportService:
    """Build a submission candidate using only the current sealed daily decision.

    The caller supplies no ad-hoc identifiers in the normal path: the daily run
    summary names the decision run and target trading session, while the current
    virtual-account latest state supplies the required prepare-day run.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def _git(self, *args: str) -> bytes:
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True).stdout

    def code_version(self) -> str:
        """Return a reproducible local code identifier without trusting caller input.

        只有影響決策的路徑（CODE_VERSION_PATHS）算進版本；有未提交修改時，識別碼附上修改內容雜湊，
        並把 patch 與未追蹤檔另存到 artifacts/code_versions/，讓這個識別碼能還原出當天實際執行的程式。
        """
        try:
            revision = self._git("rev-parse", "--short=12", "HEAD").decode("utf-8").strip()
            dirty = bool(self._git("status", "--porcelain", "--", *CODE_VERSION_PATHS).strip())
            if dirty:
                return self._dirty_code_version(revision)
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
        return "git:%s" % revision

    def _dirty_code_version(self, revision: str) -> str:
        patch = self._git("diff", "--binary", "HEAD", "--", *CODE_VERSION_PATHS)
        untracked = sorted(
            item for item in self._git("ls-files", "--others", "--exclude-standard", "-z", "--", *CODE_VERSION_PATHS)
            .decode("utf-8").split("\0") if item
        )
        digest = hashlib.sha256()
        digest.update(patch)
        for relative in untracked:
            digest.update(b"\0" + relative.encode("utf-8") + b"\0")
            digest.update((self.root / relative).read_bytes())
        version = "git:%s-dirty-%s" % (revision, digest.hexdigest()[:12])
        snapshot = self.root / CODE_SNAPSHOT_DIRECTORY / version.replace(":", "_")
        if not snapshot.exists():
            staging = Path(tempfile.mkdtemp(prefix=".tmp-", dir=self._snapshot_parent()))
            (staging / "changes.patch").write_bytes(patch)
            for relative in untracked:
                target = staging / "untracked" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((self.root / relative).read_bytes())
            (staging / "manifest.json").write_text(json.dumps(
                {"code_version": version, "base_revision": revision, "paths": list(CODE_VERSION_PATHS),
                 "untracked": untracked}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            try:
                os.rename(staging, snapshot)
            except OSError:
                shutil.rmtree(staging, ignore_errors=True)
                if not snapshot.exists():
                    raise
        return version

    def _snapshot_parent(self) -> Path:
        parent = self.root / CODE_SNAPSHOT_DIRECTORY
        parent.mkdir(parents=True, exist_ok=True)
        return parent

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

    @staticmethod
    def version_path(plan_output: Path, decision_run_id: str, trade_date: str) -> Path:
        """每個決策另存一份帶台北時間的 D-Plan，依交易日分資料夾；主檔被新決策取代後仍查得到交過哪一版。"""
        try:
            stamp = datetime.strptime(decision_run_id, "decision-%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            label = stamp.astimezone(TAIPEI).strftime("%Y%m%d-%H%M%S")
        except ValueError:
            label = decision_run_id
        return plan_output.parent / "versions" / trade_date / ("%s_%s.json" % (plan_output.stem, label))

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
        datapack = self.datapack_gate(daily_run, team_id, effective_trade_date)
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
        matched = datapack["status"] == "matched"
        plan = DPlanExporter().build(
            team_id, effective_trade_date, context, artifacts,
            official_nav=datapack.get("nav_tradable") if matched else None,
            official_prices=datapack.get("close_prices") if matched else None,
        )
        if matched:
            oversell = check_sells(plan["orders"], datapack["sellable_shares"])
            if oversell:
                raise DPlanError("D-Plan 賣出超過 DataPack 持股：" + "；".join(oversell))
        version_output = self.version_path(plan_output, decision_run_id, effective_trade_date)
        for existing in (context_output, version_output):
            if existing.exists():
                raise DPlanError("輸出檔已存在，拒絕覆寫：%s" % existing)
        write_new_json(context_output, context)
        write_new_json(version_output, plan)
        # 同一交易日重跑會產生新決策，D-Plan 主檔名只含隊伍與交易日，直接取代舊檔；舊版留在 versions/<交易日>/。
        replaced_existing = replace_json(plan_output, plan)
        return {
            "status": "ready",
            "output": str(plan_output),
            "version_output": str(version_output),
            "context_output": str(context_output),
            "trade_date": effective_trade_date,
            "decision_run_id": decision_run_id,
            "replaced_existing_output": replaced_existing,
            "account_binding": account,
            "datapack": {k: datapack.get(k) for k in ("status", "datapack_sha256", "as_of_date", "generated_at", "nav_tradable")},
            "local_validation": "passed",
            "organizer_server_validation": "not_run",
            "requires_human_review": True,
        }

    @staticmethod
    def datapack_gate(daily_run: Mapping[str, object], team_id: str, trade_date: str) -> Mapping[str, object]:
        """D-Plan 只接受本次每日流程已與 DataPack 對帳一致，或明確記錄略過對帳的 run。"""
        datapack = daily_run.get("datapack")
        if not isinstance(datapack, Mapping) or datapack.get("status") not in {"matched", "skipped"}:
            raise DPlanError("本次每日流程沒有通過 DataPack 對帳，拒絕產生 D-Plan")
        if datapack["status"] == "matched":
            if datapack.get("team_id") != team_id or datapack.get("trade_date") != trade_date:
                raise DPlanError("DataPack 對帳結果的隊伍或交易日與 D-Plan 不一致")
            if not isinstance(datapack.get("sellable_shares"), Mapping):
                raise DPlanError("DataPack 對帳結果缺少可賣股數")
        return datapack

    def export_latest(self, *, account_id: Optional[str] = None, profile_path: Optional[Path] = None) -> Dict[str, object]:
        """Manually retry the newest completed daily run without copying run IDs."""
        account_id = account_id or active_account_id(self.root)
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
            # 已還原的 run，其決策已移到封存區，不能再匯出。
            if daily_run.get("rolled_back"):
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
