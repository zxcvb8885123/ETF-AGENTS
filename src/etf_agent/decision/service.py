"""Application facade for the portfolio-decision Skill CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

from etf_agent.data import MarketDataDatabase, SnapshotRepository

from .contracts import (
    DecisionInputValidator,
    DecisionToolError,
    artifact_content_sha256,
)
from .input_builder import DEFAULT_LOOKBACK_BARS, DecisionInputBuilder
from .momentum import MomentumEngine, MomentumResultValidator
from .allocation import AllocationOrderEngine, ProposalValidator
from .finalization import DecisionFinalizer, DecisionRepository, DecisionResultValidator
from .risk import (
    CompetitionGuardV2,
    GuardValidator,
    RiskReviewValidator,
    ScenarioEngine,
    ScenarioValidator,
    revision_effects,
)
from .revision import RevisionHistoryBuilder, RevisionHistoryValidator
from .trade_intent import (
    BuyIntentPacketValidator,
    SellIntentPacketValidator,
    TradeDebateValidator,
    TradeIntentResultValidator,
    build_role_input_artifact,
)


class PortfolioDecisionApplicationService:
    """Read one input bundle and expose deterministic portfolio operations."""

    def __init__(self, bundle: Mapping[str, object]):
        self.bundle = dict(bundle)

    @classmethod
    def from_path(cls, path: Path) -> "PortfolioDecisionApplicationService":
        return cls(cls.read_json(path, " DecisionInputBundle"))

    @staticmethod
    def read_json(path: Path, label: str) -> Mapping[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DecisionToolError("無法讀取%s：%s" % (label, error)) from error
        if not isinstance(payload, Mapping):
            raise DecisionToolError("%s必須是 JSON 物件" % label)
        return payload

    @staticmethod
    def _write(payload: Mapping[str, object], output: Optional[Path]) -> None:
        if output is None:
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def build_input_file(
        cls,
        snapshot_path: Path,
        database_path: Path,
        rules_path: Path,
        output: Path,
        research_paths: Sequence[Path] = (),
        trading_status_path: Optional[Path] = None,
        account_path: Optional[Path] = None,
        lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    ) -> Dict[str, object]:
        """由 Snapshot 與 SQLite 歷史行情建立 DecisionInputBundle；未附帳戶時輸出樣板。"""
        if lookback_bars < 1:
            raise DecisionToolError("lookback_bars 必須至少為 1")
        snapshot = cls.read_json(snapshot_path, " ResearchSnapshot")
        latest_date = snapshot.get("latest_trade_date")
        cutoff = snapshot.get("decision_cutoff")
        if not isinstance(latest_date, str) or not isinstance(cutoff, str):
            raise DecisionToolError("Snapshot 缺少 latest_trade_date 或 decision_cutoff")
        if not database_path.is_file():
            raise DecisionToolError("找不到行情資料庫：%s" % database_path)
        repository = SnapshotRepository(MarketDataDatabase(database_path))
        with repository.connect() as connection:
            # 最後一根 K 線取自 Snapshot，歷史只需 lookback - 1 根。
            history = [
                dict(row)
                for row in repository.load_price_history(
                    connection, latest_date, cutoff, lookback_bars - 1
                )
            ]
        builder = DecisionInputBuilder(
            snapshot,
            cls.read_json(rules_path, " DecisionRules"),
            history,
            research_results=[cls.read_json(path, " ResearchResult") for path in research_paths],
            trading_status=(
                cls.read_json(trading_status_path, " 交易狀態包")
                if trading_status_path is not None
                else None
            ),
            account_snapshot=(
                cls.read_json(account_path, " AccountSnapshot")
                if account_path is not None
                else None
            ),
        )
        bundle = builder.build()
        errors = builder.validate(bundle)
        cls._write(bundle, output)
        return {
            "valid": not errors,
            "errors": errors,
            "output": str(output),
            "bundle_id": bundle["bundle_id"],
            "template": "account_snapshot" not in bundle,
        }

    def validate_input(self) -> Dict[str, object]:
        errors = DecisionInputValidator(self.bundle).validate()
        return {"valid": not errors, "errors": errors}

    def _require_input(self) -> None:
        errors = DecisionInputValidator(self.bundle).validate()
        if errors:
            raise DecisionToolError("DecisionInputBundle 驗證失敗：" + "；".join(errors))

    def compute_momentum(self, output: Optional[Path] = None) -> Dict[str, object]:
        self._require_input()
        result = MomentumEngine(self.bundle).run()
        self._write(result, output)
        return result

    def build_role_input(
        self,
        role: str,
        momentum_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        result = build_role_input_artifact(self.bundle, momentum, role)
        self._write(result, output)
        return result

    def seal_artifact_file(
        self, input_path: Path, output: Optional[Path] = None
    ) -> Dict[str, object]:
        artifact = dict(self.read_json(input_path, " decision artifact"))
        artifact["content_sha256"] = artifact_content_sha256(artifact)
        self._write(artifact, output)
        return artifact

    def validate_momentum_file(self, path: Path) -> Dict[str, object]:
        result = self.read_json(path, " MomentumResult")
        errors = MomentumResultValidator(self.bundle).validate(result)
        return {"valid": not errors, "errors": errors}

    def validate_packet_file(
        self,
        role: str,
        momentum_path: Path,
        packet_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        packet = self.read_json(packet_path, " IntentPacket")
        validator = (
            BuyIntentPacketValidator(self.bundle, momentum)
            if role == "buy"
            else SellIntentPacketValidator(self.bundle, momentum)
        )
        errors = validator.validate(packet)
        response: Dict[str, object] = {"valid": not errors, "errors": errors}
        if not errors:
            self._write(packet, output)
            if output is not None:
                response["output"] = str(output)
        return response

    def validate_debate_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        errors = TradeDebateValidator(self.bundle, momentum).validate(debate)
        response: Dict[str, object] = {"valid": not errors, "errors": errors}
        if not errors:
            self._write(debate, output)
            if output is not None:
                response["output"] = str(output)
        return response

    def validate_intent_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        result_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        result = self.read_json(result_path, " TradeIntentResult")
        errors = TradeIntentResultValidator(
            self.bundle, momentum, debate
        ).validate(result)
        response: Dict[str, object] = {"valid": not errors, "errors": errors}
        if not errors:
            self._write(result, output)
            if output is not None:
                response["output"] = str(output)
        return response

    def compute_proposal_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        intent_path: Path,
        policy_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        self._require_input()
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        intent = self.read_json(intent_path, " TradeIntentResult")
        policy = self.read_json(policy_path, " DecisionPolicy")
        errors = TradeIntentResultValidator(self.bundle, momentum, debate).validate(intent)
        if errors:
            raise DecisionToolError("TradeIntentResult 驗證失敗：" + "；".join(errors))
        result = AllocationOrderEngine(self.bundle, policy).run(intent)
        self._write(result, output)
        return result

    def validate_proposal_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        intent_path: Path,
        policy_path: Path,
        proposal_path: Path,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        intent = self.read_json(intent_path, " TradeIntentResult")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        errors = TradeIntentResultValidator(self.bundle, momentum, debate).validate(intent)
        errors.extend(ProposalValidator(self.bundle, policy, intent).validate(proposal))
        return {"valid": not errors, "errors": errors}

    def compute_scenario_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        intent_path: Path,
        policy_path: Path,
        proposal_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        self._require_input()
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        intent = self.read_json(intent_path, " TradeIntentResult")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        errors = TradeIntentResultValidator(self.bundle, momentum, debate).validate(intent)
        errors.extend(ProposalValidator(self.bundle, policy, intent).validate(proposal))
        if errors:
            raise DecisionToolError("ProposalBundle 驗證失敗：" + "；".join(errors))
        result = ScenarioEngine(self.bundle, policy).run(proposal)
        self._write(result, output)
        return result

    def compute_guard_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        intent_path: Path,
        policy_path: Path,
        proposal_path: Path,
        scenario_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        self._require_input()
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        intent = self.read_json(intent_path, " TradeIntentResult")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        scenario = self.read_json(scenario_path, " ScenarioResult")
        errors = TradeIntentResultValidator(self.bundle, momentum, debate).validate(intent)
        errors.extend(ProposalValidator(self.bundle, policy, intent).validate(proposal))
        errors.extend(ScenarioValidator(self.bundle, policy).validate(proposal, scenario))
        if errors:
            raise DecisionToolError("ScenarioResult 驗證失敗：" + "；".join(errors))
        result = CompetitionGuardV2(self.bundle, policy).run(proposal, scenario)
        self._write(result, output)
        return result

    def validate_risk_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        intent_path: Path,
        policy_path: Path,
        proposal_path: Path,
        scenario_path: Path,
        guard_path: Path,
        review_path: Path,
    ) -> Dict[str, object]:
        self._require_input()
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        intent = self.read_json(intent_path, " TradeIntentResult")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        scenario = self.read_json(scenario_path, " ScenarioResult")
        guard = self.read_json(guard_path, " GuardResult")
        review = self.read_json(review_path, " RiskReview")
        errors = TradeIntentResultValidator(self.bundle, momentum, debate).validate(intent)
        errors.extend(ProposalValidator(self.bundle, policy, intent).validate(proposal))
        errors.extend(ScenarioValidator(self.bundle, policy).validate(proposal, scenario))
        errors.extend(GuardValidator(self.bundle, policy).validate(proposal, scenario, guard))
        errors.extend(
            RiskReviewValidator(self.bundle, policy).validate(
                proposal, scenario, guard, review
            )
        )
        return {"valid": not errors, "errors": errors}

    def revise_proposal_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        intent_path: Path,
        policy_path: Path,
        proposal_path: Path,
        scenario_path: Path,
        guard_path: Path,
        review_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        intent = self.read_json(intent_path, " TradeIntentResult")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        scenario = self.read_json(scenario_path, " ScenarioResult")
        guard = self.read_json(guard_path, " GuardResult")
        review = self.read_json(review_path, " RiskReview")
        errors = TradeIntentResultValidator(self.bundle, momentum, debate).validate(intent)
        errors.extend(ProposalValidator(self.bundle, policy, intent).validate(proposal))
        errors.extend(ScenarioValidator(self.bundle, policy).validate(proposal, scenario))
        errors.extend(GuardValidator(self.bundle, policy).validate(proposal, scenario, guard))
        errors.extend(RiskReviewValidator(self.bundle, policy).validate(
            proposal, scenario, guard, review
        ))
        if errors:
            raise DecisionToolError("RiskReview 驗證失敗：" + "；".join(errors))
        if review.get("decision") != "revise":
            raise DecisionToolError("只有 decision=revise 可以重算提案")
        next_revision = int(proposal.get("revision_count", 0)) + 1
        if next_revision != review.get("revision_index") or next_revision > int(
            policy["max_revisions"]
        ):
            raise DecisionToolError("修正序號不連續或超過上限")
        effects = revision_effects(review, proposal)
        result = AllocationOrderEngine(self.bundle, policy).run(
            intent,
            excluded_symbols=effects["excluded_symbols"],
            overrides=effects["overrides"],
            revision_count=next_revision,
            parent_proposal_id=str(proposal["proposal_id"]),
        )
        self._write(result, output)
        return result

    def build_history_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        intent_path: Path,
        policy_path: Path,
        proposal_path: Path,
        scenario_path: Path,
        guard_path: Path,
        review_path: Path,
        output: Optional[Path] = None,
        history_path: Optional[Path] = None,
    ) -> Dict[str, object]:
        self._require_input()
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        intent = self.read_json(intent_path, " TradeIntentResult")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        scenario = self.read_json(scenario_path, " ScenarioResult")
        guard = self.read_json(guard_path, " GuardResult")
        review = self.read_json(review_path, " RiskReview")
        intent_errors = TradeIntentResultValidator(self.bundle, momentum, debate).validate(intent)
        if intent_errors:
            raise DecisionToolError("TradeIntentResult 驗證失敗：" + "；".join(intent_errors))
        builder = RevisionHistoryBuilder(self.bundle, policy, intent)
        result = (
            builder.append(
                self.read_json(history_path, " RevisionHistory"),
                proposal, scenario, guard, review,
            )
            if history_path is not None
            else builder.create(proposal, scenario, guard, review)
        )
        self._write(result, output)
        return result

    def validate_history_file(
        self, momentum_path: Path, debate_path: Path, intent_path: Path,
        policy_path: Path, history_path: Path
    ) -> Dict[str, object]:
        self._require_input()
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        intent = self.read_json(intent_path, " TradeIntentResult")
        policy = self.read_json(policy_path, " DecisionPolicy")
        history = self.read_json(history_path, " RevisionHistory")
        errors = TradeIntentResultValidator(self.bundle, momentum, debate).validate(intent)
        errors.extend(RevisionHistoryValidator(self.bundle, policy, intent).validate(history))
        return {"valid": not errors, "errors": errors}

    def finalize_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        intent_path: Path,
        policy_path: Path,
        proposal_path: Path,
        scenario_path: Path,
        guard_path: Path,
        review_path: Path,
        history_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        intent = self.read_json(intent_path, " TradeIntentResult")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        scenario = self.read_json(scenario_path, " ScenarioResult")
        guard = self.read_json(guard_path, " GuardResult")
        review = self.read_json(review_path, " RiskReview")
        history = self.read_json(history_path, " RevisionHistory")
        result = DecisionFinalizer(self.bundle, policy, momentum, debate, intent).run(
            proposal, scenario, guard, review, history
        )
        self._write(result, output)
        return result

    def validate_decision_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        intent_path: Path,
        policy_path: Path,
        proposal_path: Path,
        scenario_path: Path,
        guard_path: Path,
        review_path: Path,
        history_path: Path,
        result_path: Path,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        intent = self.read_json(intent_path, " TradeIntentResult")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        scenario = self.read_json(scenario_path, " ScenarioResult")
        guard = self.read_json(guard_path, " GuardResult")
        review = self.read_json(review_path, " RiskReview")
        history = self.read_json(history_path, " RevisionHistory")
        result = self.read_json(result_path, " DecisionResult")
        errors = DecisionResultValidator(
            self.bundle, policy, momentum, debate, intent
        ).validate(
            proposal, scenario, guard, review, history, result
        )
        return {"valid": not errors, "errors": errors}

    def save_run_files(
        self,
        run_id: str,
        repository_root: Path,
        paths: Mapping[str, Path],
    ) -> Dict[str, object]:
        artifacts: Dict[str, Mapping[str, object]] = {
            name: self.read_json(path, " %s" % name) for name, path in paths.items()
        }
        required = {
            "momentum",
            "debate",
            "intent",
            "policy",
            "proposal",
            "scenario",
            "guard",
            "risk_review",
            "revision_history",
            "decision",
        }
        missing = sorted(required - set(artifacts))
        if missing:
            raise DecisionToolError("保存前缺少 artifacts：" + ", ".join(missing))
        errors = MomentumResultValidator(self.bundle).validate(artifacts["momentum"])
        errors.extend(
            DecisionResultValidator(
                self.bundle,
                artifacts["policy"],
                artifacts["momentum"],
                artifacts["debate"],
                artifacts["intent"],
            ).validate(
                artifacts["proposal"],
                artifacts["scenario"],
                artifacts["guard"],
                artifacts["risk_review"],
                artifacts["revision_history"],
                artifacts["decision"],
            )
        )
        if errors:
            raise DecisionToolError("DecisionResult 保存前驗證失敗：" + "；".join(errors))
        artifacts = {"decision_input": self.bundle, **artifacts}
        output = DecisionRepository(repository_root).save(run_id, artifacts)
        DecisionRepository(repository_root).verify(run_id)
        return {"ok": True, "output": str(output), "verified": True}
