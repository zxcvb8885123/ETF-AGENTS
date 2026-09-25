"""按需研究與報告交付的確定性工作流。

這個模組只負責執行紀錄、事件候選封存、已驗證結果的接收及報告發布。
它不替代 Fact／Bull／Bear／Adjudicator，也不在缺少 Agent 輸出時補造研究結論。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from etf_agent.core import content_sha256, parse_aware_time
from etf_agent.automation.reporting import AutomationReportingApplicationService
from etf_agent.research import EventResearchApplicationService, ResearchToolError
from etf_agent.reporting import (
    ResearchReportApplicationService,
    ResearchReportError,
)
from etf_agent.virtual_account import VirtualAccountRepository
from etf_agent.decision.finalization import DecisionRepository


WORKFLOW_SCHEMA_VERSION = "1.0"
WORKFLOW_STATUSES = {
    "succeeded",
    "waiting_for_agent",
    "waiting_for_decision",
    "blocked",
    "failed",
}
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}")


class ReportWorkflowError(ValueError):
    """Raised when a report workflow cannot preserve its provenance."""


def _file_hash(path: Path) -> Optional[str]:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReportWorkflowError("無法讀取 %s：%s" % (label, error)) from error
    if not isinstance(payload, Mapping):
        raise ReportWorkflowError("%s 必須是 JSON 物件" % label)
    return payload


def _parse_time(value: object, field: str) -> datetime:
    return parse_aware_time(value, field, error=ReportWorkflowError, to_utc=False)


def _validate_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ReportWorkflowError("%s 不符合安全格式" % field)
    return value


def _source_ref(path: Path, label: str) -> Dict[str, object]:
    return {
        "label": label,
        "path": str(path),
        "exists": path.exists(),
        "sha256": _file_hash(path),
    }


class ReportWorkflowRepository:
    """以 manifest 與雜湊保存不可變的報告工作流執行結果。"""

    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, run_id: str, artifacts: Mapping[str, object]) -> Path:
        _validate_id(run_id, "workflow_run_id")
        normalized: Dict[str, Tuple[str, str, bytes]] = {}
        for name, payload in artifacts.items():
            if not _SAFE_ID.fullmatch(str(name)):
                raise ReportWorkflowError("artifact 名稱不符合安全白名單：%s" % name)
            if isinstance(payload, Mapping):
                filename = "%s.json" % name
                content_type = "application/json"
                encoded = (
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
                ).encode("utf-8")
            elif isinstance(payload, str):
                filename = "%s.md" % name
                content_type = "text/markdown"
                encoded = payload.encode("utf-8")
            else:
                raise ReportWorkflowError(
                    "artifact 必須是 JSON 物件或 Markdown 字串：%s" % name
                )
            normalized[str(name)] = (filename, content_type, encoded)
        if not normalized:
            raise ReportWorkflowError("至少需要一個 workflow artifact")

        manifest: Dict[str, object] = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "workflow_run_id": run_id,
            "artifacts": {
                name: {
                    "filename": filename,
                    "content_type": content_type,
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
                for name, (filename, content_type, encoded) in sorted(normalized.items())
            },
        }
        manifest["manifest_sha256"] = content_sha256(manifest)
        run_dir = self.root / run_id
        if run_dir.is_symlink():
            raise ReportWorkflowError("workflow run 目錄不得是符號連結")
        if run_dir.exists():
            current = self._read_manifest(run_dir)
            self.verify(run_id)
            if current != manifest:
                raise ReportWorkflowError("相同 workflow_run_id 已存在不同內容，拒絕覆寫")
            return run_dir

        self.root.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=".%s-" % run_id, dir=str(self.root)))
        try:
            for _, (filename, _, encoded) in sorted(normalized.items()):
                (temp_dir / filename).write_bytes(encoded)
            (temp_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(str(temp_dir), str(run_dir))
        except Exception:
            for child in temp_dir.iterdir():
                child.unlink()
            temp_dir.rmdir()
            raise
        return run_dir

    def _read_manifest(self, run_dir: Path) -> Mapping[str, object]:
        try:
            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise ReportWorkflowError("workflow manifest 無法讀取") from error
        if not isinstance(manifest, Mapping):
            raise ReportWorkflowError("workflow manifest 必須是 JSON 物件")
        return manifest

    def verify(self, run_id: str) -> Path:
        _validate_id(run_id, "workflow_run_id")
        run_dir = self.root / run_id
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise ReportWorkflowError("找不到 workflow run：%s" % run_id)
        manifest = self._read_manifest(run_dir)
        body = dict(manifest)
        recorded = body.pop("manifest_sha256", None)
        if recorded != content_sha256(body):
            raise ReportWorkflowError("workflow manifest_sha256 與內容不一致")
        if (
            manifest.get("schema_version") != WORKFLOW_SCHEMA_VERSION
            or manifest.get("workflow_run_id") != run_id
        ):
            raise ReportWorkflowError("workflow manifest 身分欄位不一致")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping) or not artifacts:
            raise ReportWorkflowError("workflow manifest.artifacts 必須是非空物件")
        expected = {"manifest.json"}
        for name, metadata in artifacts.items():
            if not _SAFE_ID.fullmatch(str(name)) or not isinstance(metadata, Mapping):
                raise ReportWorkflowError("workflow manifest artifact 不合法")
            filename = metadata.get("filename")
            digest = metadata.get("sha256")
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise ReportWorkflowError("workflow artifact filename 不合法")
            path = run_dir / filename
            expected.add(filename)
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ReportWorkflowError("workflow artifact 雜湊不一致：%s" % filename)
        actual = {path.name for path in run_dir.iterdir() if path.is_file()}
        if actual != expected:
            raise ReportWorkflowError("workflow run 含未封存或缺少檔案")
        return run_dir

    def find_by_idempotency(self, idempotency_key: str) -> Optional[Mapping[str, object]]:
        if not self.root.exists():
            return None
        matches: List[Mapping[str, object]] = []
        for run_dir in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            try:
                self.verify(run_dir.name)
                report = _read_json(run_dir / "execution_report.json", "execution report")
            except ReportWorkflowError:
                continue
            if report.get("idempotency_key") == idempotency_key:
                stages = report.get("stages")
                if isinstance(stages, list) and any(
                    isinstance(stage, Mapping)
                    and stage.get("name") == "idempotency"
                    and stage.get("status") == "failed"
                    for stage in stages
                ):
                    continue
                matches.append(report)
        if not matches:
            return None
        return max(
            matches,
            key=lambda report: (
                _parse_time(report.get("generated_at"), "generated_at").astimezone(timezone.utc),
                str(report.get("workflow_run_id") or ""),
            ),
        )

    def publish_latest(
        self,
        reports_root: Path,
        run_dir: Path,
        report: Mapping[str, object],
        markdown: str,
        success: bool,
    ) -> None:
        reports_root = Path(reports_root)
        reports_root.mkdir(parents=True, exist_ok=True)
        index = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "workflow_run_id": report.get("workflow_run_id"),
            "status": report.get("status"),
            "report_status": report.get("report_status"),
            "generated_at": report.get("generated_at"),
            "decision_cutoff": report.get("decision_cutoff"),
            "snapshot_id": report.get("snapshot_id"),
            "run_directory": str(run_dir),
            "execution_report": "execution_report.json",
            "execution_report_markdown": "execution_report_markdown.md",
            "content_sha256": report.get("content_sha256"),
        }
        self._atomic_write(reports_root / "latest.json", json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        self._atomic_write(reports_root / "latest.md", markdown)
        if success:
            self._atomic_write(
                reports_root / "latest_success.json",
                json.dumps(index, ensure_ascii=False, indent=2) + "\n",
            )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent), delete=False
        ) as stream:
            stream.write(content)
            temporary = stream.name
        os.replace(temporary, path)


class ReportWorkflowService:
    """準備事件研究輸入並交付可重建的研究／執行報告。"""

    def __init__(self, repository: ReportWorkflowRepository):
        self.repository = repository

    def run(
        self,
        *,
        workflow_run_id: str,
        repository_root: Path,
        reports_root: Path,
        snapshot_path: Path,
        database_path: Path,
        research_path: Optional[Path] = None,
        perception_path: Optional[Path] = None,
        perception_bundle_path: Optional[Path] = None,
        execution_mode: str = "official",
        generated_at: Optional[str] = None,
        lookback_days: int = 45,
        parent_run_id: Optional[str] = None,
        decision_repository: Optional[Path] = None,
        decision_run_id: Optional[str] = None,
        daily_report_repository: Optional[Path] = None,
        virtual_account_repository: Optional[Path] = None,
        virtual_account_account_id: Optional[str] = None,
        virtual_account_run_id: Optional[str] = None,
    ) -> Dict[str, object]:
        _validate_id(workflow_run_id, "workflow_run_id")
        if parent_run_id is not None:
            _validate_id(parent_run_id, "parent_workflow_run_id")
        if execution_mode not in {"official", "fixture"}:
            raise ReportWorkflowError("execution_mode 必須是 official 或 fixture")
        if (decision_repository is None) != (decision_run_id is None):
            raise ReportWorkflowError(
                "--decision-repository 與 --decision-run-id 必須同時提供"
            )
        account_args = (
            virtual_account_repository,
            virtual_account_account_id,
            virtual_account_run_id,
        )
        if any(value is not None for value in account_args) and not all(
            value is not None for value in account_args
        ):
            raise ReportWorkflowError(
                "--virtual-account-repository、--virtual-account-account-id 與 "
                "--virtual-account-run-id 必須同時提供"
            )
        if execution_mode == "official" and decision_run_id is not None and not all(
            value is not None for value in account_args
        ):
            raise ReportWorkflowError(
                "official DailyReport 必須提供已封存的 VirtualAccount prepare-day run"
            )
        if lookback_days <= 0:
            raise ReportWorkflowError("lookback_days 必須為正數")
        generated = generated_at or datetime.now(timezone.utc).isoformat()
        generated_time = _parse_time(generated, "generated_at")
        repository = self.repository
        snapshot_ref = _source_ref(Path(snapshot_path), "ResearchSnapshot")
        research_ref = _source_ref(Path(research_path), "ResearchResult") if research_path else None
        perception_ref = _source_ref(Path(perception_path), "MarketPerceptionResult") if perception_path else None
        bundle_ref = _source_ref(Path(perception_bundle_path), "PerceptionDataBundle") if perception_bundle_path else None
        decision_manifest_path = (
            Path(decision_repository) / str(decision_run_id) / "manifest.json"
            if decision_repository is not None and decision_run_id is not None
            else None
        )
        decision_ref = (
            _source_ref(decision_manifest_path, "DecisionRunManifest")
            if decision_manifest_path is not None
            else None
        )
        account_manifest_path = (
            Path(virtual_account_repository)
            / str(virtual_account_account_id)
            / "runs"
            / str(virtual_account_run_id)
            / "manifest.json"
            if all(value is not None for value in account_args)
            else None
        )
        account_ref = (
            _source_ref(account_manifest_path, "VirtualAccountRunManifest")
            if account_manifest_path is not None
            else None
        )
        input_fingerprint = content_sha256(
            {
                "snapshot_sha256": snapshot_ref.get("sha256"),
                "research_sha256": research_ref.get("sha256") if research_ref else None,
                "perception_sha256": perception_ref.get("sha256") if perception_ref else None,
                "perception_bundle_sha256": bundle_ref.get("sha256") if bundle_ref else None,
                "decision_sha256": decision_ref.get("sha256") if decision_ref else None,
                "virtual_account_sha256": account_ref.get("sha256") if account_ref else None,
                "database_sha256": _file_hash(Path(database_path)),
            }
        )

        snapshot: Optional[Mapping[str, object]] = None
        try:
            snapshot = _read_json(Path(snapshot_path), "ResearchSnapshot")
            snapshot_id = snapshot.get("snapshot_id")
            cutoff = snapshot.get("decision_cutoff")
            if not isinstance(snapshot_id, str) or not snapshot_id.strip():
                raise ReportWorkflowError("ResearchSnapshot 缺少 snapshot_id")
            cutoff_time = _parse_time(cutoff, "decision_cutoff")
            if generated_time < cutoff_time:
                raise ReportWorkflowError("generated_at 不得早於 decision_cutoff")
            if snapshot.get("usable") is not True:
                raise ReportWorkflowError(
                    "ResearchSnapshot 不可用：%s" % snapshot.get("quality_flags", [])
                )
            if perception_path is not None and perception_bundle_path is None:
                raise ReportWorkflowError("Market Perception 結果與資料包必須成對提供")
            if perception_path is None and perception_bundle_path is not None:
                raise ReportWorkflowError("Market Perception 結果與資料包必須成對提供")
        except (ReportWorkflowError, TypeError, ValueError) as error:
            return self._persist(
                workflow_run_id=workflow_run_id,
                repository_root=repository_root,
                reports_root=reports_root,
                execution_mode=execution_mode,
                generated_at=generated,
                parent_run_id=parent_run_id,
                snapshot=snapshot,
                snapshot_ref=snapshot_ref,
                research_ref=research_ref,
                perception_ref=perception_ref,
                bundle_ref=bundle_ref,
                input_fingerprint=input_fingerprint,
                status="failed",
                report_status=None,
                stages=[{"name": "snapshot", "status": "failed", "errors": [str(error)]}],
                candidates=self._empty_candidates(snapshot),
                agent_request=self._agent_request(workflow_run_id, snapshot, [], lookback_days),
                errors=[str(error)],
                warnings=[],
                next_action="修正 Snapshot 後以新的 workflow_run_id 重跑。",
                research_result=None,
                research_report=None,
                research_markdown=None,
                downstream=None,
                daily_report=None,
                daily_report_markdown=None,
                failure_report=None,
            )

        assert snapshot is not None
        parent_report: Optional[Mapping[str, object]] = None
        if parent_run_id is not None:
            parent_dir = repository.verify(parent_run_id)
            parent_report = _read_json(
                parent_dir / "execution_report.json", "父工作流執行報告"
            )
            parent_snapshot = _read_json(
                parent_dir / "input_snapshot.json", "父工作流 Snapshot"
            )
            if parent_report.get("status") not in {
                "waiting_for_agent", "waiting_for_decision", "blocked"
            }:
                raise ReportWorkflowError("父工作流不是可續跑狀態")
            if (
                parent_report.get("snapshot_id") != snapshot_id
                or parent_report.get("decision_cutoff") != cutoff
                or parent_report.get("execution_mode") != execution_mode
                or content_sha256(parent_snapshot) != content_sha256(snapshot)
            ):
                raise ReportWorkflowError("續跑輸入與父工作流 Snapshot／cutoff／模式不一致")
            parent_research = parent_report.get("input_refs", {}).get("research")
            if parent_report.get("status") == "waiting_for_decision" and (
                not isinstance(parent_research, Mapping)
                or parent_research.get("sha256") != (
                    research_ref.get("sha256") if research_ref else None
                )
            ):
                raise ReportWorkflowError("續跑 ResearchResult 與父工作流不一致")
        idempotency_key = "report:%s:%s:%s:%s" % (
            snapshot.get("snapshot_id"), snapshot.get("decision_cutoff"), execution_mode,
            decision_run_id or "research",
        )
        existing = repository.find_by_idempotency(idempotency_key)
        if existing is not None:
            if existing.get("input_fingerprint") != input_fingerprint:
                can_resume = (
                    parent_run_id == existing.get("workflow_run_id")
                    and existing.get("status") in {
                        "waiting_for_agent", "waiting_for_decision", "blocked"
                    }
                )
                if can_resume:
                    existing = None
                else:
                    return self._persist(
                        workflow_run_id=workflow_run_id,
                        repository_root=repository_root,
                        reports_root=reports_root,
                        execution_mode=execution_mode,
                        generated_at=generated,
                        parent_run_id=parent_run_id,
                        snapshot=snapshot,
                        snapshot_ref=snapshot_ref,
                        research_ref=research_ref,
                        perception_ref=perception_ref,
                        bundle_ref=bundle_ref,
                        input_fingerprint=input_fingerprint,
                        status="failed",
                        report_status=None,
                        stages=[{"name": "idempotency", "status": "failed", "errors": ["相同執行鍵已有不同輸入內容"]}],
                        candidates=self._empty_candidates(snapshot),
                        agent_request=self._agent_request(workflow_run_id, snapshot, [], lookback_days),
                        errors=["相同執行鍵已有不同輸入內容，拒絕覆寫既有報告。"],
                        warnings=[],
                        next_action="使用新的 cutoff 或修正輸入後重新執行。",
                        research_result=None,
                        research_report=None,
                        research_markdown=None,
                        downstream=None,
                        daily_report=None,
                        daily_report_markdown=None,
                        failure_report=None,
                    )
            if existing is not None:
                existing_run = str(existing.get("workflow_run_id"))
                existing_dir = repository.root / existing_run
                repository.verify(existing_run)
                return {
                    "ok": existing.get("status") == "succeeded",
                    "status": existing.get("status"),
                    "workflow_run_id": existing_run,
                    "output": str(existing_dir),
                    "reused": True,
                    "report_status": existing.get("report_status"),
                }

        try:
            event_service = EventResearchApplicationService.from_paths(
                Path(snapshot_path), Path(database_path)
            )
            status = event_service.tools.status()
            candidates = event_service.tools.list_events(
                lookback_days=lookback_days, limit=1000
            )
        except (ResearchToolError, OSError, TypeError, ValueError) as error:
            return self._persist(
                workflow_run_id=workflow_run_id,
                repository_root=repository_root,
                reports_root=reports_root,
                execution_mode=execution_mode,
                generated_at=generated,
                parent_run_id=parent_run_id,
                snapshot=snapshot,
                snapshot_ref=snapshot_ref,
                research_ref=research_ref,
                perception_ref=perception_ref,
                bundle_ref=bundle_ref,
                input_fingerprint=input_fingerprint,
                status="failed",
                report_status=None,
                stages=[{"name": "event_data", "status": "failed", "errors": [str(error)]}],
                candidates=self._empty_candidates(snapshot),
                agent_request=self._agent_request(workflow_run_id, snapshot, [], lookback_days),
                errors=[str(error)],
                warnings=[],
                next_action="修正資料庫、Snapshot 或來源資料後重跑。",
                research_result=None,
                research_report=None,
                research_markdown=None,
                downstream=None,
                daily_report=None,
                daily_report_markdown=None,
                failure_report=None,
            )

        candidate_payload = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "snapshot_id": snapshot.get("snapshot_id"),
            "decision_cutoff": snapshot.get("decision_cutoff"),
            "lookback_days": lookback_days,
            "event_count": len(candidates),
            "events": candidates,
            "source_status": status,
        }
        agent_request = self._agent_request(
            workflow_run_id, snapshot, candidates, lookback_days
        )
        if not candidates:
            return self._persist(
                workflow_run_id=workflow_run_id,
                repository_root=repository_root,
                reports_root=reports_root,
                execution_mode=execution_mode,
                generated_at=generated,
                parent_run_id=parent_run_id,
                snapshot=snapshot,
                snapshot_ref=snapshot_ref,
                research_ref=research_ref,
                perception_ref=perception_ref,
                bundle_ref=bundle_ref,
                input_fingerprint=input_fingerprint,
                status="blocked",
                report_status=None,
                stages=[
                    {"name": "snapshot", "status": "succeeded", "errors": []},
                    {"name": "event_data", "status": "blocked", "errors": ["沒有可供事件研究的 Snapshot 文件"]},
                    {"name": "research_report", "status": "blocked", "errors": ["沒有事件輸入，不能宣稱沒有事件"]},
                ],
                candidates=candidate_payload,
                agent_request=agent_request,
                errors=["沒有可供事件研究的 Snapshot 文件；不能宣稱沒有事件。"],
                warnings=[],
                next_action="檢查官方事件來源、available_at、資料庫掛載與 cutoff，重新建立 Snapshot。",
                research_result=None,
                research_report=None,
                research_markdown=None,
                downstream=None,
                daily_report=None,
                daily_report_markdown=None,
                failure_report=None,
            )

        if research_path is None or not Path(research_path).exists():
            missing = str(research_path or "artifacts/event_research_validated.json")
            return self._persist(
                workflow_run_id=workflow_run_id,
                repository_root=repository_root,
                reports_root=reports_root,
                execution_mode=execution_mode,
                generated_at=generated,
                parent_run_id=parent_run_id,
                snapshot=snapshot,
                snapshot_ref=snapshot_ref,
                research_ref=research_ref,
                perception_ref=perception_ref,
                bundle_ref=bundle_ref,
                input_fingerprint=input_fingerprint,
                status="waiting_for_agent",
                report_status=None,
                stages=[
                    {"name": "snapshot", "status": "succeeded", "errors": []},
                    {"name": "event_data", "status": "succeeded", "errors": []},
                    {"name": "research_agent", "status": "waiting_for_agent", "errors": ["尚未提供已驗證 ResearchResult"]},
                    {"name": "research_report", "status": "waiting_for_agent", "errors": ["等待事件研究 Agent 輸出"]},
                ],
                candidates=candidate_payload,
                agent_request=agent_request,
                errors=[],
                warnings=["事件候選已封存；研究報告等待 Agent 完成 Fact／Bull／Bear／Adjudicator。"],
                next_action="使用 daily-report Skill 完成隔離事件研究，將通過驗證的 ResearchResult 保存至：%s" % missing,
                research_result=None,
                research_report=None,
                research_markdown=None,
                downstream=None,
                daily_report=None,
                daily_report_markdown=None,
                failure_report=None,
            )

        failure_stage = "research_report"
        try:
            validation = event_service.validate_file(Path(research_path))
            if not validation.get("valid"):
                raise ReportWorkflowError("ResearchResult 驗證失敗：%s" % "；".join(validation.get("errors", [])))
            research_result = _read_json(Path(research_path), "ResearchResult")
            research_service = ResearchReportApplicationService.from_paths(
                Path(snapshot_path),
                Path(research_path),
                Path(perception_path) if perception_path else None,
                Path(perception_bundle_path) if perception_bundle_path else None,
            )
            with tempfile.TemporaryDirectory(prefix="report-workflow-") as temporary:
                temporary_root = Path(temporary)
                build_result = research_service.build_files(
                    temporary_root / "research_report.json",
                    temporary_root / "research_report.md",
                    report_id="research-report:%s" % workflow_run_id,
                    generated_at=generated,
                )
                research_report = _read_json(
                    temporary_root / "research_report.json", "ResearchReport"
                )
                research_markdown = (temporary_root / "research_report.md").read_text(
                    encoding="utf-8"
                )

            if decision_repository is None:
                return self._persist(
                    workflow_run_id=workflow_run_id,
                    repository_root=repository_root,
                    reports_root=reports_root,
                    execution_mode=execution_mode,
                    generated_at=generated,
                    parent_run_id=parent_run_id,
                    snapshot=snapshot,
                    snapshot_ref=snapshot_ref,
                    research_ref=research_ref,
                    perception_ref=perception_ref,
                    bundle_ref=bundle_ref,
                    input_fingerprint=input_fingerprint,
                    status="waiting_for_decision",
                    report_status=str(build_result.get("status")),
                    stages=[
                        {"name": "snapshot", "status": "succeeded", "errors": []},
                        {"name": "event_data", "status": "succeeded", "errors": []},
                        {"name": "research_agent", "status": "succeeded", "errors": []},
                        {"name": "research_report", "status": "succeeded", "errors": []},
                        {"name": "decision_report", "status": "waiting_for_decision", "errors": ["尚未提供已驗證 Decision run"]},
                    ],
                    candidates=candidate_payload,
                    agent_request=agent_request,
                    errors=[],
                    warnings=["Research Report 已完成；Portfolio Decision／Risk 尚未提供，不能建立 DailyReport。"],
                    next_action="完成同一 Snapshot／cutoff 的 Portfolio Decision 與 Risk run，再以 --decision-repository／--decision-run-id 續跑。",
                    research_result=research_result,
                    research_report=research_report,
                    research_markdown=research_markdown,
                    downstream=None,
                    daily_report=None,
                    daily_report_markdown=None,
                    failure_report=None,
                )

            daily_repository = Path(
                daily_report_repository or (Path(repository_root).parent / "pipeline_runs")
            )
            pipeline_run_id = "daily-%s" % workflow_run_id
            account_provenance: Optional[Dict[str, object]] = None
            if execution_mode == "official":
                failure_stage = "account_snapshot"
                assert virtual_account_repository is not None
                assert virtual_account_account_id is not None
                assert virtual_account_run_id is not None
                account_repo = VirtualAccountRepository(
                    Path(virtual_account_repository), virtual_account_account_id
                )
                account_run_dir = account_repo.verify(virtual_account_run_id)
                latest_account = account_repo.latest()
                if latest_account is None or latest_account.get("run_id") != virtual_account_run_id:
                    raise ReportWorkflowError(
                        "VirtualAccount run 不是帳戶目前 latest 狀態，拒絕產生 official DailyReport"
                    )
                account_state = _read_json(account_run_dir / "state.json", "VirtualAccountState")
                account_snapshot = _read_json(account_run_dir / "account_snapshot.json", "AccountSnapshot")
                account_source_snapshot = _read_json(account_run_dir / "snapshot.json", "VirtualAccount ResearchSnapshot")
                provenance = account_state.get("provenance")
                if not isinstance(provenance, Mapping) or provenance.get("type") != "prepared":
                    raise ReportWorkflowError("VirtualAccount run 必須是 prepare-day 封存狀態")
                if (
                    content_sha256(account_source_snapshot) != content_sha256(snapshot)
                    or provenance.get("snapshot_id") != snapshot_id
                    or provenance.get("snapshot_sha256") != content_sha256(snapshot)
                    or provenance.get("account_snapshot") != account_snapshot
                    or account_state.get("as_of") != cutoff
                ):
                    raise ReportWorkflowError(
                        "VirtualAccount prepare-day run 與工作流 Snapshot／cutoff／AccountSnapshot 不一致"
                    )
                decision_run_dir = DecisionRepository(Path(decision_repository)).verify(
                    str(decision_run_id)
                )
                decision_input = _read_json(
                    decision_run_dir / "decision_input.json", "DecisionInputBundle"
                )
                if decision_input.get("account_snapshot") != account_snapshot:
                    raise ReportWorkflowError(
                        "Decision run 使用的 AccountSnapshot 與 VirtualAccount prepare-day run 不一致"
                    )
                account_provenance = {
                    "account_id": virtual_account_account_id,
                    "run_id": virtual_account_run_id,
                    "state_id": account_state.get("state_id"),
                    "account_snapshot_sha256": content_sha256(account_snapshot),
                    "manifest_sha256": _read_json(
                        account_run_dir / "manifest.json", "VirtualAccount manifest"
                    ).get("manifest_sha256"),
                }
            failure_stage = "daily_report"
            downstream = dict(
                AutomationReportingApplicationService.execute_from_paths(
                    pipeline_run_id,
                    daily_repository,
                    execution_mode,
                    generated,
                    Path(snapshot_path),
                    Path(research_path),
                    Path(decision_repository),
                    str(decision_run_id),
                    Path(perception_path) if perception_path else None,
                    Path(perception_bundle_path) if perception_bundle_path else None,
                )
            )
            downstream["decision_run_id"] = str(decision_run_id)
            if account_provenance is not None:
                downstream["virtual_account"] = account_provenance
            output_dir = Path(str(downstream.get("output")))
            daily_report = None
            daily_report_markdown = None
            failure_report = None
            if downstream.get("ok"):
                daily_report = _read_json(output_dir / "daily_report.json", "DailyReport")
                daily_report_markdown = (
                    output_dir / "daily_report_markdown.md"
                ).read_text(encoding="utf-8")
                decision_stage = {
                    "name": "decision_report",
                    "status": "succeeded",
                    "errors": [],
                }
                workflow_status = "succeeded"
                workflow_warnings = (
                    ["DailyReport 內含降級 Research Report；請查看 missing_data."]
                    if build_result.get("status") == "degraded"
                    else []
                )
                workflow_next_action = (
                    "檢查 DailyReport、風控與人工檢視附錄；本流程不會送件或下單。"
                )
            else:
                failure_report = _read_json(
                    output_dir / "failure_report.json", "FailureReport"
                )
                decision_stage = {
                    "name": "decision_report",
                    "status": "failed",
                    "errors": list(downstream.get("errors", [])),
                }
                workflow_status = "failed"
                workflow_warnings = []
                workflow_next_action = (
                    "修正 Decision／Risk 前置資料後，以新的 workflow_run_id 重跑。"
                )
            return self._persist(
                workflow_run_id=workflow_run_id,
                repository_root=repository_root,
                reports_root=reports_root,
                execution_mode=execution_mode,
                generated_at=generated,
                parent_run_id=parent_run_id,
                snapshot=snapshot,
                snapshot_ref=snapshot_ref,
                research_ref=research_ref,
                perception_ref=perception_ref,
                bundle_ref=bundle_ref,
                input_fingerprint=input_fingerprint,
                status=workflow_status,
                report_status=str(build_result.get("status")),
                stages=[
                    {"name": "snapshot", "status": "succeeded", "errors": []},
                    {"name": "event_data", "status": "succeeded", "errors": []},
                    {"name": "research_agent", "status": "succeeded", "errors": []},
                    {"name": "research_report", "status": "succeeded", "errors": []},
                    decision_stage,
                ],
                candidates=candidate_payload,
                agent_request=agent_request,
                errors=[] if workflow_status == "succeeded" else list(downstream.get("errors", [])),
                warnings=workflow_warnings,
                next_action=workflow_next_action,
                research_result=research_result,
                research_report=research_report,
                research_markdown=research_markdown,
                downstream=downstream,
                daily_report=daily_report,
                daily_report_markdown=daily_report_markdown,
                failure_report=failure_report,
                decision_run_id=decision_run_id,
                decision_ref=decision_ref,
            )
        except (ReportWorkflowError, ResearchToolError, ResearchReportError, OSError, TypeError, ValueError) as error:
            return self._persist(
                workflow_run_id=workflow_run_id,
                repository_root=repository_root,
                reports_root=reports_root,
                execution_mode=execution_mode,
                generated_at=generated,
                parent_run_id=parent_run_id,
                snapshot=snapshot,
                snapshot_ref=snapshot_ref,
                research_ref=research_ref,
                perception_ref=perception_ref,
                bundle_ref=bundle_ref,
                input_fingerprint=input_fingerprint,
                status="failed",
                report_status=None,
                stages=[
                    {"name": "snapshot", "status": "succeeded", "errors": []},
                    {"name": "event_data", "status": "succeeded", "errors": []},
                    {"name": failure_stage, "status": "failed", "errors": [str(error)]},
                ],
                candidates=candidate_payload,
                agent_request=agent_request,
                errors=[str(error)],
                warnings=[],
                next_action="修正失敗階段的輸入或來源後，使用新的 workflow_run_id 重跑。",
                research_result=None,
                research_report=None,
                research_markdown=None,
                downstream=None,
                daily_report=None,
                daily_report_markdown=None,
                failure_report=None,
            )

    def _persist(
        self,
        *,
        workflow_run_id: str,
        repository_root: Path,
        reports_root: Path,
        execution_mode: str,
        generated_at: str,
        parent_run_id: Optional[str],
        snapshot: Optional[Mapping[str, object]],
        snapshot_ref: Mapping[str, object],
        research_ref: Optional[Mapping[str, object]],
        perception_ref: Optional[Mapping[str, object]],
        bundle_ref: Optional[Mapping[str, object]],
        input_fingerprint: str,
        status: str,
        report_status: Optional[str],
        stages: Sequence[Mapping[str, object]],
        candidates: Mapping[str, object],
        agent_request: Mapping[str, object],
        errors: Sequence[str],
        warnings: Sequence[str],
        next_action: str,
        research_result: Optional[Mapping[str, object]],
        research_report: Optional[Mapping[str, object]],
        research_markdown: Optional[str],
        downstream: Optional[Mapping[str, object]],
        daily_report: Optional[Mapping[str, object]],
        daily_report_markdown: Optional[str],
        failure_report: Optional[Mapping[str, object]],
        decision_run_id: Optional[str] = None,
        decision_ref: Optional[Mapping[str, object]] = None,
    ) -> Dict[str, object]:
        if status not in WORKFLOW_STATUSES:
            raise ReportWorkflowError("workflow status 不合法：%s" % status)
        cutoff = snapshot.get("decision_cutoff") if snapshot else None
        snapshot_id = snapshot.get("snapshot_id") if snapshot else None
        decision_key = decision_run_id or (
            str(downstream.get("decision_run_id"))
            if downstream is not None and downstream.get("decision_run_id")
            else "research"
        )
        idempotency_key = "report:%s:%s:%s:%s" % (
            snapshot_id, cutoff, execution_mode, decision_key
        )
        artifacts: Dict[str, object] = {
            "event_candidates": dict(candidates),
            "agent_request": dict(agent_request),
        }
        if snapshot is not None:
            artifacts["input_snapshot"] = dict(snapshot)
        if research_result is not None:
            artifacts["research_result"] = dict(research_result)
        if research_report is not None:
            artifacts["research_report"] = dict(research_report)
        if downstream is not None:
            artifacts["decision_report_result"] = dict(downstream)
        if daily_report is not None:
            artifacts["daily_report"] = dict(daily_report)
        if failure_report is not None:
            artifacts["failure_report"] = dict(failure_report)
        business_date = None
        if cutoff:
            try:
                business_date = (
                    _parse_time(cutoff, "decision_cutoff")
                    .astimezone(ZoneInfo("Asia/Taipei"))
                    .date()
                    .isoformat()
                )
            except ReportWorkflowError:
                business_date = None
        report: Dict[str, object] = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "workflow_run_id": workflow_run_id,
            "parent_workflow_run_id": parent_run_id,
            "execution_mode": execution_mode,
            "status": status,
            "report_status": report_status,
            "generated_at": generated_at,
            "business_date": business_date,
            "snapshot_id": snapshot_id,
            "decision_cutoff": cutoff,
            "idempotency_key": idempotency_key,
            "input_fingerprint": input_fingerprint,
            "input_refs": {
                "snapshot": dict(snapshot_ref),
                "research": dict(research_ref) if research_ref else None,
                "perception": dict(perception_ref) if perception_ref else None,
                "perception_bundle": dict(bundle_ref) if bundle_ref else None,
                "decision": dict(decision_ref) if decision_ref else None,
            },
            "stages": [dict(stage) for stage in stages],
            "errors": list(errors),
            "warnings": list(warnings),
            "next_action": next_action,
            "downstream": dict(downstream) if downstream is not None else None,
        }
        report["content_sha256"] = content_sha256(report)
        markdown = self._render_markdown(report, candidates, research_markdown)
        artifacts["execution_report"] = report
        artifacts["execution_report_markdown"] = markdown
        if research_markdown is not None:
            artifacts["research_report_markdown"] = research_markdown
        if daily_report_markdown is not None:
            artifacts["daily_report_markdown"] = daily_report_markdown
        run_dir = ReportWorkflowRepository(Path(repository_root)).save(
            workflow_run_id, artifacts
        )
        repository = ReportWorkflowRepository(Path(repository_root))
        repository.verify(workflow_run_id)
        repository.publish_latest(
            Path(reports_root),
            run_dir,
            report,
            markdown,
            success=status == "succeeded",
        )
        return {
            "ok": status == "succeeded",
            "status": status,
            "workflow_run_id": workflow_run_id,
            "report_status": report_status,
            "output": str(run_dir),
            "latest": str(Path(reports_root) / "latest.md"),
            "errors": list(errors),
            "warnings": list(warnings),
            "reused": False,
        }

    @staticmethod
    def _empty_candidates(snapshot: Optional[Mapping[str, object]]) -> Dict[str, object]:
        return {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "snapshot_id": snapshot.get("snapshot_id") if snapshot else None,
            "decision_cutoff": snapshot.get("decision_cutoff") if snapshot else None,
            "lookback_days": None,
            "event_count": 0,
            "events": [],
        }

    @staticmethod
    def _agent_request(
        workflow_run_id: str,
        snapshot: Optional[Mapping[str, object]],
        candidates: Sequence[Mapping[str, object]],
        lookback_days: int,
    ) -> Dict[str, object]:
        return {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "request_id": "event-research:%s" % workflow_run_id,
            "snapshot_id": snapshot.get("snapshot_id") if snapshot else None,
            "decision_cutoff": snapshot.get("decision_cutoff") if snapshot else None,
            "lookback_days": lookback_days,
            "candidate_event_ids": [str(item.get("event_id")) for item in candidates],
            "status": "pending",
            "role_inputs": [
                {
                    "role": "fact",
                    "reads": ["input_snapshot.json", "event_candidates.json"],
                    "writes": ["fact_packets"],
                    "peer_dependencies": [],
                },
                {
                    "role": "bull",
                    "reads": ["fact_packet"],
                    "writes": ["bull_packets"],
                    "peer_dependencies": ["bear"],
                    "isolation_required": True,
                },
                {
                    "role": "bear",
                    "reads": ["fact_packet"],
                    "writes": ["bear_packets"],
                    "peer_dependencies": ["bull"],
                    "isolation_required": True,
                },
                {
                    "role": "adjudicator",
                    "reads": ["fact_packet", "bull_packet", "bear_packet"],
                    "writes": ["research_result"],
                    "peer_dependencies": [],
                    "may_add_facts": False,
                },
            ],
            "constraints": [
                "所有角色使用相同 snapshot_id 與 decision_cutoff。",
                "Bull 與 Bear 不得讀取對方輸出。",
                "ResearchResult 必須通過 debate 與 result validator。",
                "沒有事件文件時不得推論沒有事件。",
            ],
        }

    @staticmethod
    def _render_markdown(
        report: Mapping[str, object],
        candidates: Mapping[str, object],
        research_markdown: Optional[str],
    ) -> str:
        lines = [
            "# 自動化報告工作流執行結果",
            "",
            "- 工作流 ID：`%s`" % report.get("workflow_run_id"),
            "- 狀態：`%s`" % report.get("status"),
            "- 報告狀態：`%s`" % (report.get("report_status") or "unavailable"),
            "- 執行模式：`%s`" % report.get("execution_mode"),
            "- Snapshot：`%s`" % (report.get("snapshot_id") or "unavailable"),
            "- 決策截止：`%s`" % (report.get("decision_cutoff") or "unavailable"),
            "",
            "## 執行階段",
            "",
            "| 階段 | 狀態 | 錯誤 |",
            "| --- | --- | --- |",
        ]
        for stage in report.get("stages", []):
            if isinstance(stage, Mapping):
                errors = "；".join(str(item) for item in stage.get("errors", [])) or "—"
                lines.append(
                    "| %s | `%s` | %s |"
                    % (stage.get("name"), stage.get("status"), errors.replace("|", "\\|"))
                )
        lines.extend(
            [
                "",
                "## 研究候選",
                "",
                "- 候選數：`%s`" % candidates.get("event_count", 0),
                "- 候選檔已封存，不能由零件數推論市場沒有事件。",
            ]
        )
        if report.get("warnings"):
            lines.extend(["", "## 警告", ""])
            lines.extend("- %s" % item for item in report["warnings"])
        if report.get("errors"):
            lines.extend(["", "## 錯誤", ""])
            lines.extend("- %s" % item for item in report["errors"])
        lines.extend(["", "## 下一步", "", str(report.get("next_action") or "—")])
        if research_markdown is not None:
            lines.extend(["", "---", "", research_markdown.rstrip()])
        lines.append("")
        return "\n".join(lines)
