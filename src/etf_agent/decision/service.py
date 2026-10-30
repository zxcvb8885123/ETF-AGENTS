"""Application facade for the portfolio-decision Skill CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.core.run_layout import trade_date_from_session
from etf_agent.data import MarketDataDatabase, SnapshotRepository

from .contracts import (
    DecisionInputValidator,
    DecisionToolError,
    artifact_content_sha256,
)
from .active_share import load_active_etf_benchmarks
from .input_builder import DEFAULT_LOOKBACK_BARS, DecisionInputBuilder
from .policy_builder import build_decision_policy
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
from .trader import TradeDecisionValidator


def target_trade_date(bundle: Mapping[str, object]) -> Optional[str]:
    """DecisionInputBundle 的目標交易日（跟 D-Plan 一致）；缺少 trading_status_bundle 時回傳 None。"""
    trading_status_bundle = bundle.get("trading_status_bundle") or {}
    return trade_date_from_session(trading_status_bundle.get("target_session"))


class PortfolioDecisionApplicationService:
    """Read one input bundle and expose deterministic portfolio operations."""

    def __init__(self, bundle: Mapping[str, object], position_review: Optional[Mapping[str, object]] = None):
        """``position_review`` 是 team_inputs 2.4 的持股檢視；有它的 run 必須讓每個驗證路徑都認得警示 ID，否則合法的
        ``review:…`` 引用會被當成未知 claim。舊 run 傳 None，驗證不變。"""
        self.bundle = dict(bundle)
        self.position_review = position_review

    @classmethod
    def from_path(cls, path: Path, position_review_path: Optional[Path] = None) -> "PortfolioDecisionApplicationService":
        review = cls.read_json(position_review_path, " PositionReview") if position_review_path else None
        return cls(cls.read_json(path, " DecisionInputBundle"), review)

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
        perception_bundle_path: Optional[Path] = None,
        perception_result_path: Optional[Path] = None,
        fear_greed_root: Optional[Path] = None,
        fear_greed_approvals_path: Optional[Path] = None,
        overnight_root: Optional[Path] = None,
        overnight_approvals_path: Optional[Path] = None,
        market_news_input: Optional[Mapping[str, object]] = None,
        active_share_holdings_path: Optional[Path] = None,
        active_share_approvals_path: Optional[Path] = None,
        company_news: bool = True,
    ) -> Dict[str, object]:
        """由 Snapshot 與 SQLite 歷史行情建立 DecisionInputBundle；未附帳戶時輸出樣板。

        ``company_news`` 為真時，FinMind 個股新聞在截止時間前已核准才附上（白名單媒體、截止前 3 天）；未核准則不附。

        指定 ``fear_greed_root`` 時，附上截止前最後一次保存的恐懼貪婪抓取；沒有任何抓取則不附。
        指定 ``overnight_root`` 時，同樣附上截止前最後一次保存的隔夜市場抓取（只給風險 Agent 現金姿態與報告背景）。
        規則有 ``required_benchmark_ids`` 時，必須提供主動型 ETF 持股檔與來源核准檔，缺少即拒絕。
        """
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
        if (perception_bundle_path is None) != (perception_result_path is None):
            raise DecisionToolError("情緒資料包與研究結果必須成對提供")
        if (fear_greed_root is None) != (fear_greed_approvals_path is None):
            raise DecisionToolError("恐懼貪婪抓取目錄與來源核准檔必須成對提供")
        fear_greed = None
        if fear_greed_root is not None:
            from etf_agent.perception.market_fear_greed import build_fear_greed_input
            fear_greed = build_fear_greed_input(fear_greed_root, fear_greed_approvals_path, snapshot.get("snapshot_id"), cutoff)
        if (overnight_root is None) != (overnight_approvals_path is None):
            raise DecisionToolError("隔夜市場抓取目錄與來源核准檔必須成對提供")
        overnight = None
        if overnight_root is not None:
            from etf_agent.perception.overnight_market import build_overnight_market_input
            overnight = build_overnight_market_input(
                overnight_root, overnight_approvals_path, snapshot.get("snapshot_id"), cutoff, latest_date)
        company_news_input = None
        if company_news:
            from etf_agent.perception.company_news import build_company_news_input
            company_news_input = build_company_news_input(database_path, snapshot)
        rules = cls.read_json(rules_path, " DecisionRules")
        required_benchmarks = rules.get("required_benchmark_ids") or []
        benchmarks: List[Dict[str, object]] = []
        if required_benchmarks:
            if active_share_holdings_path is None or active_share_approvals_path is None:
                raise DecisionToolError("規則要求 Active Share 基準，必須提供主動型 ETF 持股檔與來源核准檔")
            benchmarks = load_active_etf_benchmarks(
                active_share_holdings_path, active_share_approvals_path, required_benchmarks, cutoff
            )
        builder = DecisionInputBuilder(
            snapshot,
            rules,
            history,
            research_results=[cls.read_json(path, " ResearchResult") for path in research_paths],
            perception_inputs=([{"bundle": cls.read_json(perception_bundle_path, " PerceptionDataBundle"),
                                 "result": cls.read_json(perception_result_path, " MarketPerceptionResult")}]
                               if perception_bundle_path is not None else []),
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
            market_fear_greed_input=fear_greed,
            overnight_market_input=overnight,
            market_news_input=market_news_input,
            benchmarks=benchmarks,
            company_news_input=company_news_input,
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

    def build_policy_file(
        self, template_path: Path, sector_path: Path, output: Path
    ) -> Dict[str, object]:
        policy = build_decision_policy(
            self.read_json(template_path, " 策略樣板"),
            self.bundle,
            self.read_json(sector_path, " 產業分類"),
        )
        self._write(policy, output)
        return {"ok": True, "policy_id": policy["policy_id"], "content_sha256": policy["content_sha256"], "output": str(output)}

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

    def validate_trade_decision_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        result_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " ResearchDebateBundle")
        result = self.read_json(result_path, " TradeDecision")
        errors = TradeDecisionValidator(
            self.bundle, momentum, debate, position_review=self.position_review
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
        debate = self.read_json(debate_path, " ResearchDebateBundle")
        intent = self.read_json(intent_path, " TradeDecision")
        policy = self.read_json(policy_path, " DecisionPolicy")
        errors = TradeDecisionValidator(self.bundle, momentum, debate, position_review=self.position_review).validate(intent)
        if errors:
            raise DecisionToolError("TradeDecision 驗證失敗：" + "；".join(errors))
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
        debate = self.read_json(debate_path, " ResearchDebateBundle")
        intent = self.read_json(intent_path, " TradeDecision")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        errors = TradeDecisionValidator(self.bundle, momentum, debate, position_review=self.position_review).validate(intent)
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
        debate = self.read_json(debate_path, " ResearchDebateBundle")
        intent = self.read_json(intent_path, " TradeDecision")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        errors = TradeDecisionValidator(self.bundle, momentum, debate, position_review=self.position_review).validate(intent)
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
        debate = self.read_json(debate_path, " ResearchDebateBundle")
        intent = self.read_json(intent_path, " TradeDecision")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        scenario = self.read_json(scenario_path, " ScenarioResult")
        errors = TradeDecisionValidator(self.bundle, momentum, debate, position_review=self.position_review).validate(intent)
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
        debate = self.read_json(debate_path, " ResearchDebateBundle")
        intent = self.read_json(intent_path, " TradeDecision")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        scenario = self.read_json(scenario_path, " ScenarioResult")
        guard = self.read_json(guard_path, " GuardResult")
        review = self.read_json(review_path, " RiskReview")
        errors = TradeDecisionValidator(self.bundle, momentum, debate, position_review=self.position_review).validate(intent)
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
        debate = self.read_json(debate_path, " ResearchDebateBundle")
        intent = self.read_json(intent_path, " TradeDecision")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        scenario = self.read_json(scenario_path, " ScenarioResult")
        guard = self.read_json(guard_path, " GuardResult")
        review = self.read_json(review_path, " RiskReview")
        errors = TradeDecisionValidator(self.bundle, momentum, debate, position_review=self.position_review).validate(intent)
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
        debate = self.read_json(debate_path, " ResearchDebateBundle")
        intent = self.read_json(intent_path, " TradeDecision")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        scenario = self.read_json(scenario_path, " ScenarioResult")
        guard = self.read_json(guard_path, " GuardResult")
        review = self.read_json(review_path, " RiskReview")
        intent_errors = TradeDecisionValidator(self.bundle, momentum, debate, position_review=self.position_review).validate(intent)
        if intent_errors:
            raise DecisionToolError("TradeDecision 驗證失敗：" + "；".join(intent_errors))
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
        debate = self.read_json(debate_path, " ResearchDebateBundle")
        intent = self.read_json(intent_path, " TradeDecision")
        policy = self.read_json(policy_path, " DecisionPolicy")
        history = self.read_json(history_path, " RevisionHistory")
        errors = TradeDecisionValidator(self.bundle, momentum, debate, position_review=self.position_review).validate(intent)
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
        team_inputs_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " ResearchDebateBundle")
        intent = self.read_json(intent_path, " TradeDecision")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        scenario = self.read_json(scenario_path, " ScenarioResult")
        guard = self.read_json(guard_path, " GuardResult")
        review = self.read_json(review_path, " RiskReview")
        history = self.read_json(history_path, " RevisionHistory")
        team_inputs = self.read_json(team_inputs_path, " team_inputs")
        result = DecisionFinalizer(self.bundle, policy, momentum, debate, intent, team_inputs).run(
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
        team_inputs_path: Path,
        result_path: Path,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " ResearchDebateBundle")
        intent = self.read_json(intent_path, " TradeDecision")
        policy = self.read_json(policy_path, " DecisionPolicy")
        proposal = self.read_json(proposal_path, " ProposalBundle")
        scenario = self.read_json(scenario_path, " ScenarioResult")
        guard = self.read_json(guard_path, " GuardResult")
        review = self.read_json(review_path, " RiskReview")
        history = self.read_json(history_path, " RevisionHistory")
        team_inputs = self.read_json(team_inputs_path, " team_inputs")
        result = self.read_json(result_path, " DecisionResult")
        errors = DecisionResultValidator(
            self.bundle, policy, momentum, debate, intent, team_inputs
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
            "team_inputs",
        }
        missing = sorted(required - set(artifacts))
        if missing:
            raise DecisionToolError("保存前缺少 artifacts：" + ", ".join(missing))
        unknown = sorted(set(artifacts) - required)
        if unknown:
            raise DecisionToolError("保存時含未允許 artifacts：" + ", ".join(unknown))
        errors = MomentumResultValidator(self.bundle).validate(artifacts["momentum"])
        errors.extend(
            DecisionResultValidator(
                self.bundle,
                artifacts["policy"],
                artifacts["momentum"],
                artifacts["debate"],
                artifacts["intent"],
                artifacts["team_inputs"],
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
        # 資料夾日期用目標交易日（跟 D-Plan 一致），不是本次執行的日期；缺少 trading_status_bundle
        # 時（例如評測重跑）退回執行日期，不影響 run_id、manifest 或雜湊封印。
        output = DecisionRepository(repository_root).save(run_id, artifacts, day=target_trade_date(self.bundle))
        DecisionRepository(repository_root).verify(run_id)
        return {"ok": True, "output": str(output), "verified": True}
