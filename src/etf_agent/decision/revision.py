"""Deterministic proposal/review revision-chain construction and replay."""

from __future__ import annotations

from typing import Dict, List, Mapping

from .allocation import AllocationOrderEngine, ProposalValidator
from .contracts import (
    DECISION_SCHEMA_VERSION,
    DecisionContext,
    DecisionToolError,
    artifact_content_sha256,
    canonical_sha256,
    intent_artifact_id,
)
from .risk import GuardValidator, RiskReviewValidator, ScenarioValidator, revision_effects


REVISION_HISTORY_VERSION = "1.0.0"


class RevisionHistoryBuilder:
    """Create append-only history artifacts; validation replays every transition."""

    def __init__(
        self,
        bundle: Mapping[str, object],
        policy: Mapping[str, object],
        intent: Mapping[str, object],
    ):
        self.context = DecisionContext(bundle)
        self.bundle = dict(bundle)
        self.policy = dict(policy)
        self.intent = dict(intent)

    def create(
        self,
        proposal: Mapping[str, object],
        scenario: Mapping[str, object],
        guard: Mapping[str, object],
        review: Mapping[str, object],
    ) -> Dict[str, object]:
        body = self._body([self._entry(proposal, scenario, guard, review)])
        errors = RevisionHistoryValidator(self.bundle, self.policy, self.intent).validate(body)
        if errors:
            raise DecisionToolError("RevisionHistory 建立失敗：" + "；".join(errors))
        return body

    def append(
        self,
        history: Mapping[str, object],
        proposal: Mapping[str, object],
        scenario: Mapping[str, object],
        guard: Mapping[str, object],
        review: Mapping[str, object],
    ) -> Dict[str, object]:
        errors = RevisionHistoryValidator(self.bundle, self.policy, self.intent).validate(history)
        if errors:
            raise DecisionToolError("既有 RevisionHistory 驗證失敗：" + "；".join(errors))
        entries = list(history.get("entries", []))
        entries.append(self._entry(proposal, scenario, guard, review))
        body = self._body(entries)
        errors = RevisionHistoryValidator(self.bundle, self.policy, self.intent).validate(body)
        if errors:
            raise DecisionToolError("RevisionHistory 追加失敗：" + "；".join(errors))
        return body

    @staticmethod
    def _entry(proposal, scenario, guard, review) -> Dict[str, object]:
        return {
            "revision_index": proposal.get("revision_count"),
            "proposal": dict(proposal),
            "scenario": dict(scenario),
            "guard": dict(guard),
            "risk_review": dict(review),
        }

    def _body(self, entries: List[Mapping[str, object]]) -> Dict[str, object]:
        body: Dict[str, object] = {
            "schema_version": DECISION_SCHEMA_VERSION,
            "history_id": "",
            "bundle_id": self.context.bundle_id,
            "snapshot_id": self.context.snapshot_id,
            "decision_cutoff": self.context.decision_cutoff,
            "bundle_hash": self.context.bundle_hash,
            "policy_id": self.policy.get("policy_id"),
            "policy_hash": self.policy.get("content_sha256"),
            "trade_intent_result_id": intent_artifact_id(self.intent),
            "engine_version": REVISION_HISTORY_VERSION,
            "entries": entries,
        }
        body["history_id"] = "history:" + canonical_sha256(body)[:20]
        body["content_sha256"] = artifact_content_sha256(body)
        return body


class RevisionHistoryValidator:
    """Replay revision zero and every authorized risk-reduction transition."""

    def __init__(self, bundle, policy, intent):
        self.context = DecisionContext(bundle)
        self.bundle = dict(bundle)
        self.policy = dict(policy)
        self.intent = dict(intent)

    def validate(self, history: Mapping[str, object], require_terminal: bool = False) -> List[str]:
        errors: List[str] = []
        allowed = {
            "schema_version", "history_id", "bundle_id", "snapshot_id",
            "decision_cutoff", "bundle_hash", "policy_id", "policy_hash",
            "trade_intent_result_id", "engine_version", "entries", "content_sha256",
        }
        unknown = sorted(set(history) - allowed)
        if unknown:
            errors.append("RevisionHistory 含未允許欄位：%s" % ", ".join(unknown))
        if history.get("schema_version") != DECISION_SCHEMA_VERSION:
            errors.append("RevisionHistory.schema_version 不一致")
        expected_links = {
            "bundle_id": self.context.bundle_id,
            "snapshot_id": self.context.snapshot_id,
            "decision_cutoff": self.context.decision_cutoff,
            "bundle_hash": self.context.bundle_hash,
            "policy_id": self.policy.get("policy_id"),
            "policy_hash": self.policy.get("content_sha256"),
            "trade_intent_result_id": intent_artifact_id(self.intent),
        }
        for field, value in expected_links.items():
            if history.get(field) != value:
                errors.append("RevisionHistory.%s 不一致" % field)
        if history.get("content_sha256") != artifact_content_sha256(history):
            errors.append("RevisionHistory.content_sha256 與內容不一致")
        id_payload = dict(history)
        id_payload.pop("content_sha256", None)
        id_payload["history_id"] = ""
        expected_id = "history:" + canonical_sha256(id_payload)[:20]
        if history.get("history_id") != expected_id:
            errors.append("RevisionHistory.history_id 與內容不一致")
        entries = history.get("entries")
        if not isinstance(entries, list) or not entries:
            return errors + ["RevisionHistory.entries 必須是非空陣列"]
        if len(entries) > int(self.policy["max_revisions"]) + 1:
            errors.append("RevisionHistory 超過三次修正上限")
        previous = None
        for index, raw in enumerate(entries):
            if not isinstance(raw, Mapping):
                errors.append("RevisionHistory.entries[%d] 必須是物件" % index)
                continue
            if set(raw) != {"revision_index", "proposal", "scenario", "guard", "risk_review"}:
                errors.append("RevisionHistory.entries[%d] 欄位不符合白名單" % index)
                continue
            proposal = raw.get("proposal", {})
            scenario = raw.get("scenario", {})
            guard = raw.get("guard", {})
            review = raw.get("risk_review", {})
            if not all(isinstance(value, Mapping) for value in (proposal, scenario, guard, review)):
                errors.append("RevisionHistory.entries[%d] artifacts 必須是物件" % index)
                continue
            if raw.get("revision_index") != index or proposal.get("revision_count") != index:
                errors.append("RevisionHistory 修正序號必須從 0 連續遞增")
            errors.extend(ProposalValidator(self.bundle, self.policy, self.intent).validate(proposal))
            errors.extend(ScenarioValidator(self.bundle, self.policy).validate(proposal, scenario))
            errors.extend(GuardValidator(self.bundle, self.policy).validate(proposal, scenario, guard))
            errors.extend(RiskReviewValidator(self.bundle, self.policy).validate(proposal, scenario, guard, review))
            if index == 0:
                expected = AllocationOrderEngine(self.bundle, self.policy).run(self.intent)
                if dict(proposal) != expected:
                    errors.append("初始 Proposal 必須直接由基礎政策產生，不得自帶覆寫")
            elif previous is not None:
                prior_proposal = previous["proposal"]
                prior_review = previous["risk_review"]
                if prior_review.get("decision") != "revise":
                    errors.append("只有前一版 decision=revise 才能建立下一版")
                else:
                    try:
                        effects = revision_effects(prior_review, prior_proposal)
                        expected = AllocationOrderEngine(self.bundle, self.policy).run(
                            self.intent,
                            excluded_symbols=effects["excluded_symbols"],
                            overrides=effects["overrides"],
                            revision_count=index,
                            parent_proposal_id=prior_proposal.get("proposal_id"),
                        )
                        if dict(proposal) != expected:
                            errors.append("Proposal 修正未忠實套用前一版 RiskReview")
                    except DecisionToolError as error:
                        errors.append(str(error))
            previous = raw
        if require_terminal:
            last = entries[-1]
            last_review = last.get("risk_review") if isinstance(last, Mapping) else None
            if not isinstance(last_review, Mapping):
                errors.append("RevisionHistory 最後一版缺少 RiskReview")
            elif last_review.get("decision") == "revise":
                errors.append("RevisionHistory 最後一版不得停在 revise")
        return errors
