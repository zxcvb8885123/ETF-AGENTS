"""Final decision reconstruction and immutable run storage."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Mapping, Optional

from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.research import ResearchResultValidator

from .allocation import ProposalValidator
from .contracts import (
    DECISION_SCHEMA_VERSION,
    DecisionContext,
    DecisionToolError,
    artifact_content_sha256,
    canonical_sha256,
    intent_artifact_id,
)
from .risk import GuardValidator, RiskReviewValidator, ScenarioValidator
from .revision import RevisionHistoryValidator
from .analysts import ANALYSTS
from .sizing import validate_cash_stance
from .stance import STANCE_SCHEMA_VERSION, ResearchDebateBundleValidator
from .trade_intent import TradeDebateValidator, TradeIntentResultValidator
from .trader import SIZED, TradeDecisionValidator


DECISION_ENGINE_VERSION = "1.0.0"
TEAM_INPUT_FIELDS = {"schema_version", "analyst_reports", "research_result", "cash_stance"}


def build_team_inputs(
    analyst_reports: Mapping[str, Mapping[str, object]],
    research_result: Mapping[str, object],
    cash_stance: Mapping[str, object],
) -> Dict[str, object]:
    """新鏈 Decision run 的 team_inputs artifact：重建多空辯論與現金姿態所需的全部輸入。"""
    return {
        "schema_version": STANCE_SCHEMA_VERSION,
        "analyst_reports": {name: dict(analyst_reports[name]) for name in ANALYSTS},
        "research_result": dict(research_result),
        "cash_stance": dict(cash_stance),
    }


def is_team_chain(debate: Mapping[str, object]) -> bool:
    """ResearchDebateBundle 2.0 代表分析團隊 → 多空研究 → 交易 Agent 的新鏈。"""
    return debate.get("schema_version") == STANCE_SCHEMA_VERSION


class DecisionFinalizer:
    """Create a final status only from fully validated deterministic artifacts."""

    def __init__(
        self,
        bundle: Mapping[str, object],
        policy: Mapping[str, object],
        momentum_result: Mapping[str, object],
        debate: Mapping[str, object],
        intent_result: Mapping[str, object],
        team_inputs: Optional[Mapping[str, object]] = None,
    ):
        self.context = DecisionContext(bundle)
        self.bundle = dict(bundle)
        self.policy = dict(policy)
        self.momentum = dict(momentum_result)
        self.debate = dict(debate)
        self.intent = dict(intent_result)
        self.team_inputs = dict(team_inputs) if isinstance(team_inputs, Mapping) else None

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
            "trade_intent_result_id": intent_artifact_id(self.intent),
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
        if is_team_chain(self.debate):
            # 只在新鏈加入，舊鏈 DecisionResult 內容與 ID 維持不變。
            body["team_inputs_sha256"] = canonical_sha256(self.team_inputs) if self.team_inputs is not None else None
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
        if is_team_chain(self.debate):
            errors.extend(self._validate_team_chain())
        else:
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


    def _validate_team_chain(self) -> List[str]:
        """新鏈：重建驗證多空辯論、交易決策、現金姿態，以及 policy 綁定的等級與姿態。"""
        team = self.team_inputs
        if team is None:
            return ["分析團隊決策鏈需要 team_inputs（四份分析報告、事件研究與現金姿態）才能重建驗證"]
        errors: List[str] = []
        unknown = sorted(set(team) - TEAM_INPUT_FIELDS)
        if unknown or set(team) != TEAM_INPUT_FIELDS:
            return ["team_inputs 欄位必須剛好是：%s" % "、".join(sorted(TEAM_INPUT_FIELDS))]
        if team.get("schema_version") != STANCE_SCHEMA_VERSION:
            errors.append("team_inputs.schema_version 必須為 %s" % STANCE_SCHEMA_VERSION)
        reports = team.get("analyst_reports")
        research = team.get("research_result")
        if not isinstance(reports, Mapping) or set(reports) != set(ANALYSTS) or not isinstance(research, Mapping):
            return errors + ["team_inputs 必須包含四份分析報告與 ResearchResult"]
        errors.extend(
            "ResearchResult：%s" % error
            for error in ResearchResultValidator(self.bundle["snapshot"]).validate(research)
        )
        try:
            errors.extend(
                ResearchDebateBundleValidator(self.bundle, self.momentum, reports, research).validate(self.debate)
            )
        except DecisionToolError as error:
            errors.append(str(error))
        errors.extend(TradeDecisionValidator(self.bundle, self.momentum, self.debate).validate(self.intent))
        stance = team.get("cash_stance")
        validate_cash_stance(self.context, stance, "team_inputs.cash_stance", errors)
        sizing = self.policy.get("position_sizing")
        if not isinstance(sizing, Mapping):
            return errors + ["新鏈 DecisionPolicy 必須啟用 position_sizing"]
        expected = {
            "cash_stance": stance.get("level") if isinstance(stance, Mapping) else None,
            "sizing_plan_id": self.intent.get("decision_id"),
            "sizing_plan_sha256": self.intent.get("content_sha256"),
            "conviction_by_symbol": {
                str(item.get("symbol", "")).upper(): item.get("conviction")
                for item in self.intent.get("items", [])
                if isinstance(item, Mapping) and item.get("intent") in SIZED
            },
        }
        for field, value in expected.items():
            if sizing.get(field) != value:
                errors.append("DecisionPolicy.position_sizing.%s 與交易決策或現金姿態不一致" % field)
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
        team_inputs: Optional[Mapping[str, object]] = None,
    ):
        self.finalizer = DecisionFinalizer(
            bundle, policy, momentum_result, debate, intent_result, team_inputs
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


class DecisionRepository(ImmutableRunStore):
    """Atomically save one immutable decision run and reject conflicting reuse."""

    def __init__(self, root: Path):
        super().__init__(root, schema_version=DECISION_SCHEMA_VERSION, error=DecisionToolError)
