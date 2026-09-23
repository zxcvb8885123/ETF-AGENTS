"""Final decision reconstruction and immutable run storage."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Mapping

from .allocation import ProposalValidator
from .contracts import (
    DECISION_SCHEMA_VERSION,
    DecisionContext,
    DecisionToolError,
    artifact_content_sha256,
    canonical_sha256,
)
from .risk import GuardValidator, RiskReviewValidator, ScenarioValidator
from .revision import RevisionHistoryValidator
from .trade_intent import TradeDebateValidator, TradeIntentResultValidator


DECISION_ENGINE_VERSION = "1.0.0"


class DecisionFinalizer:
    """Create a final status only from fully validated deterministic artifacts."""

    def __init__(
        self,
        bundle: Mapping[str, object],
        policy: Mapping[str, object],
        momentum_result: Mapping[str, object],
        debate: Mapping[str, object],
        intent_result: Mapping[str, object],
    ):
        self.context = DecisionContext(bundle)
        self.bundle = dict(bundle)
        self.policy = dict(policy)
        self.momentum = dict(momentum_result)
        self.debate = dict(debate)
        self.intent = dict(intent_result)

    def run(
        self,
        proposal: Mapping[str, object],
        scenario: Mapping[str, object],
        guard: Mapping[str, object],
        risk_review: Mapping[str, object],
        history: Mapping[str, object],
    ) -> Dict[str, object]:
        errors = self.validate_inputs(proposal, scenario, guard, risk_review, history)
        risk_decision = risk_review.get("decision")
        if errors or risk_decision != "approve" or guard.get("passed") is not True:
            status = "rejected"
        elif not proposal.get("order_proposal", {}).get("orders"):
            status = "no_trade"
        else:
            status = "approved"
        body: Dict[str, object] = {
            "schema_version": DECISION_SCHEMA_VERSION,
            "decision_id": "",
            "bundle_id": self.context.bundle_id,
            "snapshot_id": self.context.snapshot_id,
            "decision_cutoff": self.context.decision_cutoff,
            "bundle_hash": self.context.bundle_hash,
            "trade_intent_result_id": self.intent.get("result_id"),
            "policy_id": self.policy.get("policy_id"),
            "policy_hash": self.policy.get("content_sha256"),
            "proposal_id": proposal.get("proposal_id"),
            "scenario_id": scenario.get("scenario_id"),
            "guard_id": guard.get("guard_id"),
            "risk_review_id": risk_review.get("review_id"),
            "revision_history_id": history.get("history_id"),
            "revision_count": proposal.get("revision_count", 0),
            "engine_version": DECISION_ENGINE_VERSION,
            "status": status,
            "portfolio": proposal.get("allocation_proposal") if status in {"approved", "no_trade"} else None,
            "orders": proposal.get("order_proposal", {}).get("orders", []) if status == "approved" else [],
            "unresolved_risks": risk_review.get("unresolved_questions", []),
            "errors": errors,
        }
        body["decision_id"] = "decision:" + canonical_sha256(body)[:20]
        body["content_sha256"] = artifact_content_sha256(body)
        return body

    def validate_inputs(
        self,
        proposal: Mapping[str, object],
        scenario: Mapping[str, object],
        guard: Mapping[str, object],
        risk_review: Mapping[str, object],
        history: Mapping[str, object],
    ) -> List[str]:
        errors: List[str] = []
        errors.extend(
            TradeDebateValidator(self.bundle, self.momentum).validate(self.debate)
        )
        errors.extend(
            TradeIntentResultValidator(
                self.bundle, self.momentum, self.debate
            ).validate(self.intent)
        )
        errors.extend(ProposalValidator(self.bundle, self.policy, self.intent).validate(proposal))
        errors.extend(ScenarioValidator(self.bundle, self.policy).validate(proposal, scenario))
        errors.extend(GuardValidator(self.bundle, self.policy).validate(proposal, scenario, guard))
        errors.extend(
            RiskReviewValidator(self.bundle, self.policy).validate(
                proposal, scenario, guard, risk_review
            )
        )
        errors.extend(
            RevisionHistoryValidator(self.bundle, self.policy, self.intent).validate(
                history, require_terminal=True
            )
        )
        entries = history.get("entries", [])
        if isinstance(entries, list) and entries:
            latest = entries[-1]
            matches = isinstance(latest, Mapping)
            if matches:
                for field, value in (
                    ("proposal", proposal), ("scenario", scenario),
                    ("guard", guard), ("risk_review", risk_review),
                ):
                    candidate = latest.get(field)
                    if not isinstance(candidate, Mapping) or dict(candidate) != dict(value):
                        matches = False
                        break
            if not matches:
                errors.append("最終 artifacts 必須等於 RevisionHistory 最後一版")
        return errors


class DecisionResultValidator:
    """Rebuild the complete final result without calling any model."""

    def __init__(
        self,
        bundle: Mapping[str, object],
        policy: Mapping[str, object],
        momentum_result: Mapping[str, object],
        debate: Mapping[str, object],
        intent_result: Mapping[str, object],
    ):
        self.finalizer = DecisionFinalizer(
            bundle, policy, momentum_result, debate, intent_result
        )

    def validate(
        self,
        proposal: Mapping[str, object],
        scenario: Mapping[str, object],
        guard: Mapping[str, object],
        risk_review: Mapping[str, object],
        history: Mapping[str, object],
        result: Mapping[str, object],
    ) -> List[str]:
        expected = self.finalizer.run(proposal, scenario, guard, risk_review, history)
        return [] if dict(result) == expected else ["DecisionResult 與完整重建結果不一致"]


class DecisionRepository:
    """Atomically save one immutable run and reject conflicting reuse."""

    def __init__(self, root: Path):
        self.root = root

    def save(self, run_id: str, artifacts: Mapping[str, Mapping[str, object]]) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise DecisionToolError("run_id 只能包含英數、點、底線與連字號")
        for name in artifacts:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
                raise DecisionToolError("artifact 名稱不符合安全白名單：%s" % name)
        run_dir = self.root / run_id
        if run_dir.is_symlink():
            raise DecisionToolError("run 目錄不得是符號連結")
        manifest = {
            "schema_version": DECISION_SCHEMA_VERSION,
            "run_id": run_id,
            "artifacts": {
                name: {
                    "filename": "%s.json" % name,
                    "sha256": canonical_sha256(payload),
                }
                for name, payload in sorted(artifacts.items())
            },
        }
        manifest["manifest_sha256"] = canonical_sha256(manifest)
        if run_dir.exists():
            existing = run_dir / "manifest.json"
            if not existing.exists():
                raise DecisionToolError("既有 run 缺少 manifest，拒絕覆蓋")
            try:
                current = json.loads(existing.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise DecisionToolError("既有 manifest 無法讀取") from error
            self.verify(run_id)
            if current != manifest:
                raise DecisionToolError("相同 run_id 已存在不同內容，拒絕覆蓋")
            return run_dir
        self.root.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=".%s-" % run_id, dir=str(self.root)))
        try:
            for name, payload in sorted(artifacts.items()):
                (temp_dir / (name + ".json")).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
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

    def verify(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise DecisionToolError("run_id 只能包含英數、點、底線與連字號")
        run_dir = self.root / run_id
        if run_dir.is_symlink():
            raise DecisionToolError("run 目錄不得是符號連結")
        manifest_path = run_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DecisionToolError("run manifest 無法讀取") from error
        manifest_body = dict(manifest)
        recorded = manifest_body.pop("manifest_sha256", None)
        if recorded != canonical_sha256(manifest_body):
            raise DecisionToolError("manifest_sha256 與內容不一致")
        if manifest.get("run_id") != run_id or manifest.get("schema_version") != DECISION_SCHEMA_VERSION:
            raise DecisionToolError("manifest 身分欄位不一致")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise DecisionToolError("manifest.artifacts 必須是物件")
        expected_files = {"manifest.json"}
        for name, metadata in artifacts.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(name)):
                raise DecisionToolError("manifest 含不安全 artifact 名稱")
            if not isinstance(metadata, Mapping) or metadata.get("filename") != "%s.json" % name:
                raise DecisionToolError("manifest artifact 檔名不一致：%s" % name)
            path = run_dir / str(metadata["filename"])
            if path.is_symlink():
                raise DecisionToolError("artifact 不得是符號連結：%s" % name)
            expected_files.add(path.name)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise DecisionToolError("artifact 無法讀取：%s" % name) from error
            if not isinstance(payload, Mapping) or canonical_sha256(payload) != metadata.get("sha256"):
                raise DecisionToolError("artifact 內容與 manifest 不一致：%s" % name)
        actual_files = {path.name for path in run_dir.iterdir() if path.is_file()}
        if actual_files != expected_files:
            raise DecisionToolError("run 目錄檔案集合與 manifest 不一致")
        return run_dir
