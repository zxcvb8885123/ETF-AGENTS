"""Final decision reconstruction and immutable run storage."""

from __future__ import annotations

import json
import os
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
    ) -> Dict[str, object]:
        errors = self.validate_inputs(proposal, scenario, guard, risk_review)
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
        result: Mapping[str, object],
    ) -> List[str]:
        expected = self.finalizer.run(proposal, scenario, guard, risk_review)
        return [] if dict(result) == expected else ["DecisionResult 與完整重建結果不一致"]


class DecisionRepository:
    """Atomically save one immutable run and reject conflicting reuse."""

    def __init__(self, root: Path):
        self.root = root

    def save(self, run_id: str, artifacts: Mapping[str, Mapping[str, object]]) -> Path:
        if not run_id or "/" in run_id or run_id in {".", ".."}:
            raise DecisionToolError("run_id 不得為空或包含路徑字元")
        run_dir = self.root / run_id
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
