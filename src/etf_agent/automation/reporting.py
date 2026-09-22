"""Immutable offline pipeline runs and deterministic DailyReport artifacts.

This module deliberately stops at an auditable review handoff.  It does not
authenticate with a broker or a competition platform, submit a file, or place
an order.
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

from etf_agent.decision import (
    DecisionInputValidator,
    DecisionRepository,
    DecisionResultValidator,
    DecisionToolError,
    canonical_sha256,
)
from etf_agent.reporting import (
    ResearchReportBuilder,
    ResearchReportError,
    ResearchReportValidator,
)
from etf_agent.research import ResearchResultValidator, ResearchToolError
from etf_agent.runtime import PipelineRuntimeError, build_pipeline_run


AUTOMATION_SCHEMA_VERSION = "1.0"
AUTOMATION_ENGINE_VERSION = "1.0.0"
PIPELINE_MODES = {"fixture", "official"}
PIPELINE_STATUSES = {"succeeded", "failed", "blocked", "waiting_for_agent"}
DAILY_REPORT_STATUSES = {"completed", "degraded"}


class AutomationReportingError(ValueError):
    """Raised when the pipeline cannot safely produce a review artifact."""


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AutomationReportingError("%s 必須是包含時區的時間字串" % field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AutomationReportingError("%s 無法解析：%s" % (field, value)) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutomationReportingError("%s 必須包含時區" % field)
    return parsed.astimezone(timezone.utc)


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AutomationReportingError("缺少必要字串欄位：%s" % field)
    return value.strip()


def _content_hash(payload: Mapping[str, object]) -> str:
    body = dict(payload)
    body.pop("content_sha256", None)
    return canonical_sha256(body)


def _first_difference(expected: object, actual: object, path: str = "$") -> Optional[str]:
    if type(expected) is not type(actual):
        return "%s 型別不同" % path
    if isinstance(expected, Mapping):
        if set(expected) != set(actual):
            return "%s 欄位不同" % path
        for key in sorted(expected):
            difference = _first_difference(expected[key], actual[key], "%s.%s" % (path, key))
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return "%s 長度不同" % path
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = _first_difference(left, right, "%s[%d]" % (path, index))
            if difference is not None:
                return difference
        return None
    return None if expected == actual else "%s 值不同" % path


def _validate_run_id(run_id: str, field: str = "pipeline_run_id") -> str:
    if not isinstance(run_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id
    ):
        raise AutomationReportingError(
            "%s 只能包含英數、點、底線與連字號" % field
        )
    return run_id


def _file_sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _source_ref(label: str, path: Path) -> Dict[str, object]:
    result: Dict[str, object] = {"label": label, "path": str(path)}
    digest = _file_sha256(path)
    if digest is not None:
        result["file_sha256"] = digest
    return result


def _business_date(cutoff: str) -> str:
    return _parse_time(cutoff, "decision_cutoff").astimezone(
        ZoneInfo("Asia/Taipei")
    ).date().isoformat()


class PipelineRepository:
    """Atomically save an immutable pipeline run and verify every file digest."""

    def __init__(self, root: Path):
        self.root = root

    def save(self, run_id: str, artifacts: Mapping[str, object]) -> Path:
        _validate_run_id(run_id)
        normalized: Dict[str, Tuple[str, str, bytes]] = {}
        for name, payload in artifacts.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(name)):
                raise AutomationReportingError("artifact 名稱不符合安全白名單：%s" % name)
            if isinstance(payload, Mapping):
                filename = "%s.json" % name
                content_type = "application/json"
                encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            elif isinstance(payload, str):
                filename = "%s.md" % name
                content_type = "text/markdown"
                encoded = payload.encode("utf-8")
            else:
                raise AutomationReportingError("artifact 必須是 JSON 物件或 Markdown 字串：%s" % name)
            normalized[str(name)] = (filename, content_type, encoded)
        if not normalized:
            raise AutomationReportingError("至少需要一個 artifact")

        run_dir = self.root / run_id
        if run_dir.is_symlink():
            raise AutomationReportingError("pipeline run 目錄不得是符號連結")
        manifest: Dict[str, object] = {
            "schema_version": AUTOMATION_SCHEMA_VERSION,
            "pipeline_run_id": run_id,
            "artifacts": {
                name: {
                    "filename": filename,
                    "content_type": content_type,
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
                for name, (filename, content_type, encoded) in sorted(normalized.items())
            },
        }
        manifest["manifest_sha256"] = canonical_sha256(manifest)
        if run_dir.exists():
            current = self._read_manifest(run_dir)
            self.verify(run_id)
            if current != manifest:
                raise AutomationReportingError("相同 pipeline_run_id 已存在不同內容，拒絕覆蓋")
            return run_dir

        candidate_run = artifacts.get("pipeline_run")
        if isinstance(candidate_run, Mapping):
            idempotency_key = candidate_run.get("idempotency_key")
            input_fingerprint = candidate_run.get("input_fingerprint")
            if isinstance(idempotency_key, str) and isinstance(input_fingerprint, str):
                existing = self._find_matching_run(idempotency_key)
                if existing is not None:
                    existing_run = self._read_json_artifact(existing / "pipeline_run.json")
                    if existing_run.get("input_fingerprint") != input_fingerprint:
                        raise AutomationReportingError(
                            "相同執行鍵已有不同輸入內容，拒絕重複發布"
                        )
                    return existing

        self.root.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=".%s-" % run_id, dir=str(self.root)))
        try:
            for _, (filename, _, encoded) in sorted(normalized.items()):
                (temp_dir / filename).write_bytes(encoded)
            (temp_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(str(temp_dir), str(run_dir))
        except Exception:
            for child in temp_dir.iterdir():
                child.unlink()
            temp_dir.rmdir()
            raise
        return run_dir

    def _find_matching_run(self, idempotency_key: str) -> Optional[Path]:
        if not self.root.exists():
            return None
        for candidate in sorted(self.root.iterdir(), key=lambda path: path.name):
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            try:
                self.verify(candidate.name)
                pipeline_run = self._read_json_artifact(candidate / "pipeline_run.json")
            except AutomationReportingError:
                continue
            if pipeline_run.get("idempotency_key") == idempotency_key:
                return candidate
        return None

    @staticmethod
    def _read_json_artifact(path: Path) -> Mapping[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AutomationReportingError("pipeline JSON artifact 無法讀取：%s" % path.name) from error
        if not isinstance(payload, Mapping):
            raise AutomationReportingError("pipeline JSON artifact 必須是物件：%s" % path.name)
        return payload

    def _read_manifest(self, run_dir: Path) -> Mapping[str, object]:
        try:
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AutomationReportingError("pipeline manifest 無法讀取") from error
        if not isinstance(manifest, Mapping):
            raise AutomationReportingError("pipeline manifest 必須是 JSON 物件")
        return manifest

    def verify(self, run_id: str) -> Path:
        _validate_run_id(run_id)
        run_dir = self.root / run_id
        if run_dir.is_symlink():
            raise AutomationReportingError("pipeline run 目錄不得是符號連結")
        manifest = self._read_manifest(run_dir)
        body = dict(manifest)
        recorded = body.pop("manifest_sha256", None)
        if recorded != canonical_sha256(body):
            raise AutomationReportingError("pipeline manifest_sha256 與內容不一致")
        if (
            manifest.get("schema_version") != AUTOMATION_SCHEMA_VERSION
            or manifest.get("pipeline_run_id") != run_id
        ):
            raise AutomationReportingError("pipeline manifest 身分欄位不一致")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping) or not artifacts:
            raise AutomationReportingError("pipeline manifest.artifacts 必須是非空物件")
        expected_files = {"manifest.json"}
        for name, metadata in artifacts.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(name)):
                raise AutomationReportingError("pipeline manifest 含不安全 artifact 名稱")
            if not isinstance(metadata, Mapping):
                raise AutomationReportingError("pipeline manifest artifact 格式錯誤：%s" % name)
            content_type = metadata.get("content_type")
            suffix = ".json" if content_type == "application/json" else ".md"
            filename = metadata.get("filename")
            if filename != "%s%s" % (name, suffix):
                raise AutomationReportingError("pipeline manifest artifact 檔名不一致：%s" % name)
            path = run_dir / str(filename)
            if path.is_symlink():
                raise AutomationReportingError("pipeline artifact 不得是符號連結：%s" % name)
            try:
                encoded = path.read_bytes()
            except OSError as error:
                raise AutomationReportingError("pipeline artifact 無法讀取：%s" % name) from error
            if hashlib.sha256(encoded).hexdigest() != metadata.get("sha256"):
                raise AutomationReportingError("pipeline artifact 內容與 manifest 不一致：%s" % name)
            expected_files.add(str(filename))
        actual_files = {path.name for path in run_dir.iterdir() if path.is_file()}
        if actual_files != expected_files:
            raise AutomationReportingError("pipeline run 檔案集合與 manifest 不一致")
        return run_dir


class DailyReportBuilder:
    """Build a DailyReport solely from validated source and decision artifacts."""

    def __init__(
        self,
        snapshot: Mapping[str, object],
        research_result: Mapping[str, object],
        decision_artifacts: Mapping[str, Mapping[str, object]],
        decision_run_id: str,
        perception_result: Optional[Mapping[str, object]] = None,
        perception_bundle: Optional[Mapping[str, object]] = None,
    ):
        self.snapshot = dict(snapshot)
        self.research_result = dict(research_result)
        self.decision = {name: dict(value) for name, value in decision_artifacts.items()}
        self.decision_run_id = _validate_run_id(decision_run_id, "decision_run_id")
        self.perception_result = dict(perception_result) if perception_result is not None else None
        self.perception_bundle = dict(perception_bundle) if perception_bundle is not None else None
        self._validate_inputs()

    def _validate_inputs(self) -> None:
        self.snapshot_id = _required_string(self.snapshot, "snapshot_id")
        self.decision_cutoff = _required_string(self.snapshot, "decision_cutoff")
        self.cutoff = _parse_time(self.decision_cutoff, "decision_cutoff")
        if self.snapshot.get("usable") is not True:
            raise AutomationReportingError("ResearchSnapshot 不可用，不能建立 DailyReport")
        research_errors = ResearchResultValidator(self.snapshot).validate(self.research_result)
        if research_errors:
            raise AutomationReportingError("ResearchResult 驗證失敗：%s" % "；".join(research_errors))
        self.research_run_id = _required_string(self.research_result, "run_id")
        required = {
            "decision_input", "momentum", "debate", "intent", "policy", "proposal",
            "scenario", "guard", "risk_review", "revision_history", "decision",
        }
        missing = sorted(required - set(self.decision))
        if missing:
            raise AutomationReportingError("Decision run 缺少 artifacts：%s" % ", ".join(missing))
        input_bundle = self.decision["decision_input"]
        input_errors = DecisionInputValidator(input_bundle).validate()
        if input_errors:
            raise AutomationReportingError("DecisionInputBundle 驗證失敗：%s" % "；".join(input_errors))
        if input_bundle.get("snapshot_id") != self.snapshot_id:
            raise AutomationReportingError("DecisionInputBundle.snapshot_id 與 ResearchSnapshot 不一致")
        if _parse_time(input_bundle.get("decision_cutoff"), "DecisionInputBundle.decision_cutoff") != self.cutoff:
            raise AutomationReportingError("DecisionInputBundle.decision_cutoff 與 ResearchSnapshot 不一致")
        embedded_snapshot = input_bundle.get("snapshot")
        if not isinstance(embedded_snapshot, Mapping) or dict(embedded_snapshot) != self.snapshot:
            raise AutomationReportingError("DecisionInputBundle 內嵌 Snapshot 與 ResearchSnapshot 內容不一致")
        result_errors = DecisionResultValidator(
            input_bundle,
            self.decision["policy"],
            self.decision["momentum"],
            self.decision["debate"],
            self.decision["intent"],
        ).validate(
            self.decision["proposal"],
            self.decision["scenario"],
            self.decision["guard"],
            self.decision["risk_review"],
            self.decision["revision_history"],
            self.decision["decision"],
        )
        if result_errors:
            raise AutomationReportingError("DecisionResult 驗證失敗：%s" % "；".join(result_errors))
        self.decision_result = self.decision["decision"]
        if self.decision_result.get("status") not in {"approved", "no_trade"}:
            raise AutomationReportingError(
                "DecisionResult.status=%s，不能建立可供人工檢視的 DailyReport"
                % self.decision_result.get("status")
            )
        if self.decision_result.get("snapshot_id") != self.snapshot_id:
            raise AutomationReportingError("DecisionResult.snapshot_id 與 ResearchSnapshot 不一致")
        if _parse_time(self.decision_result.get("decision_cutoff"), "DecisionResult.decision_cutoff") != self.cutoff:
            raise AutomationReportingError("DecisionResult.decision_cutoff 與 ResearchSnapshot 不一致")
        self.research_builder = ResearchReportBuilder(
            self.snapshot,
            self.research_result,
            self.perception_result,
            self.perception_bundle,
        )

    def build(
        self,
        pipeline_run_id: str,
        execution_mode: str,
        generated_at: str,
    ) -> Dict[str, object]:
        _validate_run_id(pipeline_run_id)
        if execution_mode not in PIPELINE_MODES:
            raise AutomationReportingError("execution_mode 必須是 fixture 或 official")
        if _parse_time(generated_at, "generated_at") < self.cutoff:
            raise AutomationReportingError("generated_at 不得早於 decision_cutoff")
        research_report = self.research_builder.build(
            report_id="research-report:" + pipeline_run_id,
            generated_at=generated_at,
        )
        report_errors = ResearchReportValidator(self.research_builder).validate(research_report)
        if report_errors:
            raise AutomationReportingError("ResearchReport 驗證失敗：%s" % "；".join(report_errors))
        status = "degraded" if research_report.get("status") == "degraded" else "completed"
        result: Dict[str, object] = {
            "schema_version": AUTOMATION_SCHEMA_VERSION,
            "daily_report_id": "daily-report:" + pipeline_run_id,
            "pipeline_run_id": pipeline_run_id,
            "execution_mode": execution_mode,
            "generated_at": generated_at,
            "snapshot_id": self.snapshot_id,
            "decision_cutoff": self.decision_cutoff,
            "business_date": _business_date(self.decision_cutoff),
            "status": status,
            "input_refs": {
                "snapshot_sha256": canonical_sha256(self.snapshot),
                "research_run_id": self.research_run_id,
                "research_result_sha256": canonical_sha256(self.research_result),
                "decision_id": self.decision_result.get("decision_id"),
                "decision_run_id": self.decision_run_id,
                "decision_result_sha256": canonical_sha256(self.decision_result),
                "decision_input_bundle_id": self.decision["decision_input"].get("bundle_id"),
                "decision_input_bundle_sha256": canonical_sha256(self.decision["decision_input"]),
                "policy_id": self.decision["policy"].get("policy_id"),
                "policy_sha256": self.decision["policy"].get("content_sha256"),
            },
            "research_report": research_report,
            "decision": {
                "status": self.decision_result.get("status"),
                "portfolio": self.decision_result.get("portfolio"),
                "orders": self.decision_result.get("orders"),
                "unresolved_risks": self.decision_result.get("unresolved_risks"),
                "risk_review_id": self.decision_result.get("risk_review_id"),
                "guard_id": self.decision_result.get("guard_id"),
            },
            "limitations": [
                "本報告只呈現已驗證輸入與確定性結果，不新增投資結論。",
                "本流程於人工檢視交付時結束；不會送出主辦平台或執行交易。",
            ],
        }
        result["content_sha256"] = _content_hash(result)
        return result


class DailyReportValidator:
    """Rebuild a DailyReport and reject any altered number, text, or reference."""

    def __init__(self, builder: DailyReportBuilder):
        self.builder = builder

    def validate(self, report: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        for field in (
            "daily_report_id", "pipeline_run_id", "execution_mode", "generated_at",
            "snapshot_id", "decision_cutoff", "business_date", "content_sha256",
        ):
            if not isinstance(report.get(field), str) or not str(report.get(field)).strip():
                errors.append("缺少必要字串欄位：%s" % field)
        if report.get("schema_version") != AUTOMATION_SCHEMA_VERSION:
            errors.append("schema_version 必須為 %s" % AUTOMATION_SCHEMA_VERSION)
        if report.get("status") not in DAILY_REPORT_STATUSES:
            errors.append("DailyReport.status 不合法")
        if report.get("content_sha256") != _content_hash(report):
            errors.append("DailyReport.content_sha256 與內容不一致")
        try:
            expected = self.builder.build(
                str(report.get("pipeline_run_id", "")),
                str(report.get("execution_mode", "")),
                str(report.get("generated_at", "")),
            )
            difference = _first_difference(expected, report)
            if difference is not None:
                errors.append("DailyReport 與確定性重建結果不一致：%s" % difference)
        except AutomationReportingError as error:
            errors.append(str(error))
        return errors


class DailyReportMarkdownRenderer:
    """Render a validated DailyReport without introducing new financial claims."""

    @staticmethod
    def _text(value: object) -> str:
        if value is None:
            return "unavailable"
        text = " ".join(str(value).splitlines()).strip()
        for character in ("\\", "`", "*", "_", "{", "}", "[", "]", "<", ">", "#", "|"):
            text = text.replace(character, "\\" + character)
        return text or "unavailable"

    @classmethod
    def _list(cls, value: object, empty: str = "無") -> str:
        if not isinstance(value, list) or not value:
            return empty
        return "、".join(cls._text(item) for item in value)

    def render(self, report: Mapping[str, object]) -> str:
        decision = report.get("decision")
        research = report.get("research_report")
        if not isinstance(decision, Mapping) or not isinstance(research, Mapping):
            raise AutomationReportingError("DailyReport 缺少 decision 或 research_report")
        lines = [
            "# 每日決策報告",
            "",
            "- DailyReport：`%s`" % self._text(report.get("daily_report_id")),
            "- Pipeline run：`%s`" % self._text(report.get("pipeline_run_id")),
            "- 執行模式：`%s`" % self._text(report.get("execution_mode")),
            "- 執行日：`%s`" % self._text(report.get("business_date")),
            "- 資料截止：`%s`" % self._text(report.get("decision_cutoff")),
            "- Snapshot：`%s`" % self._text(report.get("snapshot_id")),
            "- 報告狀態：`%s`" % self._text(report.get("status")),
            "",
            "> 本報告只交付人工檢視，不會送出主辦平台或執行交易。",
            "",
            "## 決策與風控",
            "",
            "- 決策狀態：`%s`" % self._text(decision.get("status")),
            "- Risk review：`%s`" % self._text(decision.get("risk_review_id")),
            "- Guard：`%s`" % self._text(decision.get("guard_id")),
            "- 未解風險：%s" % self._list(decision.get("unresolved_risks")),
            "",
            "## 配置與訂單",
            "",
        ]
        portfolio = decision.get("portfolio")
        orders = decision.get("orders")
        if isinstance(portfolio, Mapping):
            lines.append("- 現金配置：`%s`" % self._text(portfolio.get("cash_weight")))
            positions = portfolio.get("positions")
            if isinstance(positions, list) and positions:
                for position in positions:
                    if isinstance(position, Mapping):
                        lines.append(
                            "- 持倉 `%s`：目標權重 `%s`，股數 `%s`"
                            % (
                                self._text(position.get("symbol")),
                                self._text(position.get("target_weight")),
                                self._text(position.get("shares")),
                            )
                        )
        if isinstance(orders, list) and orders:
            for order in orders:
                if isinstance(order, Mapping):
                    lines.append(
                        "- 訂單候選 `%s`：%s `%s` 股"
                        % (
                            self._text(order.get("symbol")),
                            self._text(order.get("side")),
                            self._text(order.get("shares")),
                        )
                    )
        else:
            lines.append("- 無訂單候選。")
        lines.extend(["", "## 研究摘要", ""])
        coverage = research.get("coverage")
        if isinstance(coverage, Mapping):
            lines.extend(
                [
                    "- 事件研究數量：%s" % self._text(coverage.get("event_item_count")),
                    "- 市場認知資料：`%s`" % self._text(coverage.get("perception")),
                ]
            )
        lines.extend(["", "## 限制", ""])
        for limitation in report.get("limitations", []):
            lines.append("- %s" % self._text(limitation))
        return "\n".join(lines).rstrip() + "\n"


class AutomationReportingApplicationService:
    """File-oriented A0/A1 orchestration facade for the future daily-report Skill."""

    def __init__(
        self,
        builder: DailyReportBuilder,
        source_refs: Sequence[Mapping[str, object]],
        decision_run_id: str,
    ):
        self.builder = builder
        self.source_refs = [dict(item) for item in source_refs]
        self.decision_run_id = decision_run_id
        self.renderer = DailyReportMarkdownRenderer()

    @staticmethod
    def read_json(path: Path, label: str) -> Mapping[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AutomationReportingError("無法讀取 %s：%s" % (label, error)) from error
        if not isinstance(payload, Mapping):
            raise AutomationReportingError("%s 必須是 JSON 物件" % label)
        return payload

    @classmethod
    def from_paths(
        cls,
        snapshot_path: Path,
        research_path: Path,
        decision_repository_root: Path,
        decision_run_id: str,
        perception_path: Optional[Path] = None,
        perception_bundle_path: Optional[Path] = None,
    ) -> "AutomationReportingApplicationService":
        if (perception_path is None) != (perception_bundle_path is None):
            raise AutomationReportingError(
                "--perception 與 --perception-bundle 必須同時提供"
            )
        _validate_run_id(decision_run_id, "decision_run_id")
        snapshot = cls.read_json(snapshot_path, "ResearchSnapshot")
        research = cls.read_json(research_path, "ResearchResult")
        try:
            run_dir = DecisionRepository(decision_repository_root).verify(decision_run_id)
        except DecisionToolError as error:
            raise AutomationReportingError("Decision run 驗證失敗：%s" % error) from error
        names = (
            "decision_input", "momentum", "debate", "intent", "policy", "proposal",
            "scenario", "guard", "risk_review", "revision_history", "decision",
        )
        artifacts = {
            name: cls.read_json(run_dir / (name + ".json"), "Decision %s" % name)
            for name in names
        }
        perception = cls.read_json(perception_path, "MarketPerceptionResult") if perception_path else None
        perception_bundle = (
            cls.read_json(perception_bundle_path, "PerceptionDataBundle")
            if perception_bundle_path else None
        )
        refs = [
            _source_ref("ResearchSnapshot", snapshot_path),
            _source_ref("ResearchResult", research_path),
            _source_ref("DecisionRunManifest", run_dir / "manifest.json"),
        ]
        if perception_path and perception_bundle_path:
            refs.extend(
                [
                    _source_ref("MarketPerceptionResult", perception_path),
                    _source_ref("PerceptionDataBundle", perception_bundle_path),
                ]
            )
        return cls(
            DailyReportBuilder(
                snapshot,
                research,
                artifacts,
                decision_run_id,
                perception,
                perception_bundle,
            ),
            refs,
            decision_run_id,
        )

    def _run_manifest(
        self,
        pipeline_run_id: str,
        execution_mode: str,
        daily_report: Mapping[str, object],
    ) -> Dict[str, object]:
        result: Dict[str, object] = {
            "schema_version": AUTOMATION_SCHEMA_VERSION,
            "pipeline_run_id": pipeline_run_id,
            "execution_mode": execution_mode,
            "business_date": daily_report["business_date"],
            "engine_version": AUTOMATION_ENGINE_VERSION,
            "input_files": self.source_refs,
            "input_artifacts": dict(daily_report["input_refs"]),
            "output_artifacts": {
                "daily_report_id": daily_report["daily_report_id"],
                "daily_report_content_sha256": daily_report["content_sha256"],
            },
        }
        result["content_sha256"] = _content_hash(result)
        return result

    @staticmethod
    def _pipeline_run(
        pipeline_run_id: str,
        execution_mode: str,
        generated_at: str,
        status: str,
        snapshot_id: Optional[str],
        decision_cutoff: Optional[str],
        business_date: Optional[str],
        idempotency_key: Optional[str],
        input_fingerprint: Optional[str],
        stages: Sequence[Mapping[str, object]],
        errors: Sequence[str],
    ) -> Dict[str, object]:
        if status not in PIPELINE_STATUSES:
            raise AutomationReportingError("PipelineRun.status 不合法")
        try:
            return build_pipeline_run(
                schema_version=AUTOMATION_SCHEMA_VERSION,
                pipeline_run_id=pipeline_run_id,
                execution_mode=execution_mode,
                status=status,
                started_at=generated_at,
                ended_at=generated_at,
                snapshot_id=snapshot_id,
                decision_cutoff=decision_cutoff,
                business_date=business_date,
                idempotency_key=idempotency_key,
                input_fingerprint=input_fingerprint,
                stages=stages,
                errors=errors,
            )
        except PipelineRuntimeError as error:
            raise AutomationReportingError(str(error)) from error

    def build_run(
        self,
        pipeline_run_id: str,
        repository_root: Path,
        execution_mode: str,
        generated_at: str,
    ) -> Dict[str, object]:
        daily_report = self.builder.build(pipeline_run_id, execution_mode, generated_at)
        errors = DailyReportValidator(self.builder).validate(daily_report)
        if errors:
            raise AutomationReportingError("DailyReport 建立後驗證失敗：%s" % "；".join(errors))
        manifest = self._run_manifest(pipeline_run_id, execution_mode, daily_report)
        input_fingerprint = canonical_sha256(
            {
                "snapshot_sha256": daily_report["input_refs"]["snapshot_sha256"],
                "research_result_sha256": daily_report["input_refs"]["research_result_sha256"],
                "decision_run_id": daily_report["input_refs"]["decision_run_id"],
                "decision_result_sha256": daily_report["input_refs"]["decision_result_sha256"],
                "policy_sha256": daily_report["input_refs"]["policy_sha256"],
            }
        )
        idempotency_key = "pipeline:" + canonical_sha256(
            {
                "business_date": daily_report["business_date"],
                "decision_cutoff": daily_report["decision_cutoff"],
                "execution_mode": execution_mode,
                "policy_sha256": daily_report["input_refs"]["policy_sha256"],
            }
        )[:20]
        pipeline_run = self._pipeline_run(
            pipeline_run_id,
            execution_mode,
            generated_at,
            "succeeded",
            str(daily_report["snapshot_id"]),
            str(daily_report["decision_cutoff"]),
            str(daily_report["business_date"]),
            idempotency_key,
            input_fingerprint,
            [
                {"name": "research_snapshot", "status": "succeeded", "sha256": daily_report["input_refs"]["snapshot_sha256"]},
                {"name": "research", "status": "succeeded", "sha256": daily_report["input_refs"]["research_result_sha256"]},
                {"name": "decision", "status": "succeeded", "sha256": daily_report["input_refs"]["decision_result_sha256"]},
                {"name": "daily_report", "status": "succeeded", "sha256": daily_report["content_sha256"]},
            ],
            [],
        )
        run_dir = PipelineRepository(repository_root).save(
            pipeline_run_id,
            {
                "pipeline_run": pipeline_run,
                "run_manifest": manifest,
                "daily_report": daily_report,
                "daily_report_markdown": self.renderer.render(daily_report),
            },
        )
        PipelineRepository(repository_root).verify(run_dir.name)
        stored_report = (
            self.read_json(run_dir / "daily_report.json", "DailyReport")
            if run_dir.name != pipeline_run_id
            else daily_report
        )
        return {
            "ok": True,
            "status": "succeeded",
            "pipeline_run_id": run_dir.name,
            "daily_report_id": stored_report["daily_report_id"],
            "output": str(run_dir),
            "reused": run_dir.name != pipeline_run_id,
        }

    @classmethod
    def save_failure(
        cls,
        pipeline_run_id: str,
        repository_root: Path,
        execution_mode: str,
        generated_at: str,
        error: str,
        source_refs: Sequence[Mapping[str, object]],
        snapshot_id: Optional[str] = None,
        decision_cutoff: Optional[str] = None,
    ) -> Dict[str, object]:
        _validate_run_id(pipeline_run_id)
        if execution_mode not in PIPELINE_MODES:
            raise AutomationReportingError("execution_mode 必須是 fixture 或 official")
        _parse_time(generated_at, "generated_at")
        failure: Dict[str, object] = {
            "schema_version": AUTOMATION_SCHEMA_VERSION,
            "failure_report_id": "failure-report:" + pipeline_run_id,
            "pipeline_run_id": pipeline_run_id,
            "execution_mode": execution_mode,
            "generated_at": generated_at,
            "snapshot_id": snapshot_id,
            "decision_cutoff": decision_cutoff,
            "status": "failed",
            "error_code": "PIPELINE_PRECONDITION_FAILED",
            "errors": [str(error)],
            "input_files": [dict(item) for item in source_refs],
            "required_human_action": "修正錯誤後使用新的 pipeline_run_id 重跑；本次不產生 DailyReport 或 D-Plan 候選檔。",
        }
        failure["content_sha256"] = _content_hash(failure)
        business_date = _business_date(decision_cutoff) if decision_cutoff else None
        input_fingerprint = canonical_sha256({"input_files": failure["input_files"]})
        idempotency_key = (
            "failure:" + canonical_sha256(
                {
                    "business_date": business_date,
                    "decision_cutoff": decision_cutoff,
                    "execution_mode": execution_mode,
                }
            )[:20]
            if decision_cutoff else None
        )
        pipeline_run = cls._pipeline_run(
            pipeline_run_id,
            execution_mode,
            generated_at,
            "failed",
            snapshot_id,
            decision_cutoff,
            business_date,
            idempotency_key,
            input_fingerprint,
            [{"name": "precondition", "status": "failed", "error_code": failure["error_code"]}],
            [str(error)],
        )
        manifest: Dict[str, object] = {
            "schema_version": AUTOMATION_SCHEMA_VERSION,
            "pipeline_run_id": pipeline_run_id,
            "execution_mode": execution_mode,
            "engine_version": AUTOMATION_ENGINE_VERSION,
            "input_files": [dict(item) for item in source_refs],
            "output_artifacts": {
                "failure_report_id": failure["failure_report_id"],
                "failure_report_content_sha256": failure["content_sha256"],
            },
        }
        manifest["content_sha256"] = _content_hash(manifest)
        run_dir = PipelineRepository(repository_root).save(
            pipeline_run_id,
            {"pipeline_run": pipeline_run, "run_manifest": manifest, "failure_report": failure},
        )
        PipelineRepository(repository_root).verify(run_dir.name)
        return {
            "ok": False,
            "status": "failed",
            "pipeline_run_id": run_dir.name,
            "failure_report_id": failure["failure_report_id"],
            "output": str(run_dir),
            "errors": failure["errors"],
            "reused": run_dir.name != pipeline_run_id,
        }

    @classmethod
    def execute_from_paths(
        cls,
        pipeline_run_id: str,
        repository_root: Path,
        execution_mode: str,
        generated_at: str,
        snapshot_path: Path,
        research_path: Path,
        decision_repository_root: Path,
        decision_run_id: str,
        perception_path: Optional[Path] = None,
        perception_bundle_path: Optional[Path] = None,
    ) -> Dict[str, object]:
        refs = [
            _source_ref("ResearchSnapshot", snapshot_path),
            _source_ref("ResearchResult", research_path),
            _source_ref("DecisionRunManifest", decision_repository_root / decision_run_id / "manifest.json"),
        ]
        if perception_path is not None:
            refs.append(_source_ref("MarketPerceptionResult", perception_path))
        if perception_bundle_path is not None:
            refs.append(_source_ref("PerceptionDataBundle", perception_bundle_path))
        try:
            service = cls.from_paths(
                snapshot_path,
                research_path,
                decision_repository_root,
                decision_run_id,
                perception_path,
                perception_bundle_path,
            )
            return service.build_run(
                pipeline_run_id, repository_root, execution_mode, generated_at
            )
        except (AutomationReportingError, DecisionToolError, ResearchToolError, ResearchReportError, OSError, TypeError, ValueError) as error:
            snapshot_id: Optional[str] = None
            cutoff: Optional[str] = None
            try:
                snapshot = cls.read_json(snapshot_path, "ResearchSnapshot")
                candidate_id = snapshot.get("snapshot_id")
                candidate_cutoff = snapshot.get("decision_cutoff")
                if isinstance(candidate_id, str):
                    snapshot_id = candidate_id
                if isinstance(candidate_cutoff, str):
                    cutoff = candidate_cutoff
            except AutomationReportingError:
                pass
            return cls.save_failure(
                pipeline_run_id,
                repository_root,
                execution_mode,
                generated_at,
                str(error),
                refs,
                snapshot_id,
                cutoff,
            )

    def validate_run(self, repository_root: Path, pipeline_run_id: str) -> Dict[str, object]:
        run_dir = PipelineRepository(repository_root).verify(pipeline_run_id)
        daily_path = run_dir / "daily_report.json"
        if not daily_path.exists():
            return {"valid": True, "status": "failed", "errors": []}
        report = self.read_json(daily_path, "DailyReport")
        errors = DailyReportValidator(self.builder).validate(report)
        return {"valid": not errors, "status": report.get("status"), "errors": errors}
