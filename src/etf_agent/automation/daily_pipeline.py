"""每日決策鏈：確定性工具與 CLI 執行的隔離子 Agent。

Agent 只輸出受 JSON Schema 限制的判斷內容（items、等級、審查結論）；envelope、
ID、內容雜湊、驗證、配置與封存全部由 Python 完成。每個 Agent 輸出都先經既有
Validator 檢查，失敗時把錯誤回饋重試一次，仍失敗就停止，不以預設值補上判斷。
"""


from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionFinalizer,
    DecisionInputValidator,
    DecisionResultValidator,
    DecisionToolError,
    MomentumEngine,
    PortfolioDecisionApplicationService,
    RevisionHistoryBuilder,
    ScenarioEngine,
    apply_trade_decision,
    bind_drawdown,
    build_decision_policy,
    build_portfolio_slots,
    apply_selection_ranking,
    build_position_review,
    build_team_inputs,
    decision_policy_sha256,
    fundamental_period_policy,
    revision_effects,
)
from etf_agent.decision.allocation import DecisionPolicyValidator
from etf_agent.decision.cash_ratio import bind_cash_ratio_anchor
from etf_agent.decision.exposure import bind_holding_days, build_exposure_review, exposure_rotation_gate
from etf_agent.decision.reentry_cooldown import bind_reentry_cooldown
from etf_agent.decision.thesis_verdicts import bind_forced_swap, load_thesis_verdicts
from etf_agent.research import ResearchResultValidator

from .agent_runner import (  # noqa: F401 — 對外仍由本模組匯出
    AgentCall,
    AgentRunner,
    AgentTask,
    ClaudeAgentRunner,
    CodexAgentRunner,
    OpenCodeAgentRunner,
    DailyPipelineError,
    TransientAgentError,
    _TEXT,
    _object,
    _strings,
)
from .agent_schemas import (  # noqa: F401 — 對外仍由本模組匯出
    ANALYST_SCHEMA,
    CASH_STANCE_SCHEMA,
    EVENT_ANALYST_SCHEMA,
    EVENT_BATCH_SIZE,
    REVIEW_SCHEMA,
    STANCE_SCHEMA,
    TRADE_SCHEMA,
    _EVENT_RULES,
    _SHARED_RULES,
    _TRADE_ITEM,
    analyst_schema,
    event_analyst_schema,
    stance_schema,
)
from .batching import merge_batch_items, universe_batches  # noqa: F401 — 對外仍由本模組匯出
from .pipeline_base import (  # noqa: F401 — 對外仍由本模組匯出
    ANALYST_TASK_PREFIXES,
    MAX_PARALLEL_WORKERS,
    PipelineBase,
    PipelineResult,
    compact_brief_json,
    degraded_research_result,
)
from .risk_stages import RiskStagesMixin
from .team_stages import TeamStagesMixin


class DailyDecisionPipeline(PipelineBase, TeamStagesMixin, RiskStagesMixin):
    """由 Snapshot、帳戶快照與交易狀態包跑完一次可封存的 Portfolio Decision。

    職責分檔：``pipeline_base``（Agent 步驟、重試、平行任務）、``team_stages``（分析師、多空、交易）、
    ``risk_stages``（現金姿態、風險審查）；本檔只保留 ``run`` 的階段串接。"""

    def run(
        self,
        snapshot_path: Path,
        account_snapshot_path: Path,
        trading_status_path: Path,
        rules_path: Path,
        database_path: Path,
        policy_template_path: Path,
        sector_path: Path,
        decision_run_id: str,
        perception_bundle_path: Optional[Path] = None,
        perception_result_path: Optional[Path] = None,
        fear_greed_root: Optional[Path] = None,
        fear_greed_approvals_path: Optional[Path] = None,
        market_news_root: Optional[Path] = None,
        nav_history: Optional[Sequence[Mapping[str, object]]] = None,
        applied_decision_run_ids: Optional[Sequence[str]] = None,
        active_share_holdings_path: Optional[Path] = None,
        active_share_approvals_path: Optional[Path] = None,
        overnight_root: Optional[Path] = None,
        overnight_approvals_path: Optional[Path] = None,
        holdings_history: Optional[Sequence[Mapping[str, object]]] = None,
    ) -> PipelineResult:
        """``applied_decision_run_ids`` 是帳本實際結算過的 Decision run（舊到新），用來在前一交易時段有多個封存 run 時
        挑出真正交出去的那一個；不給時取該時段 cutoff 最新者。

        ``holdings_history`` 是帳本逐狀態的持股（舊到新，與 ``nav_history`` 同一條鏈）：只有 Policy 啟用
        position_sizing.exposure_rotation 才用，沒有它時閘門啟用即拒絕。"""
        result = PipelineResult(status="failed")
        try:
            self._run(
                result, snapshot_path, account_snapshot_path, trading_status_path,
                rules_path, database_path, policy_template_path, sector_path, decision_run_id,
                perception_bundle_path, perception_result_path,
                fear_greed_root, fear_greed_approvals_path, market_news_root, nav_history,
                applied_decision_run_ids,
                active_share_holdings_path=active_share_holdings_path,
                active_share_approvals_path=active_share_approvals_path,
                overnight_root=overnight_root, overnight_approvals_path=overnight_approvals_path,
                holdings_history=holdings_history,
            )
            result.status = "completed"
        except Exception as error:
            self._finish_failed(result, error)
        result.agent_calls = self.calls
        result.stage_durations = dict(self.stage_durations)
        result.skill_versions = self.skill_versions()
        return result

    def _finish_failed(self, result: PipelineResult, error: Exception) -> None:
        """預期內的錯誤記進 result；預期之外的（TypeError、AttributeError…）仍 fail-closed 往上拋，
        但先把已花的 Agent 呼叫與階段耗時留在 last_result，呼叫端才能寫進執行紀錄，稽核不會因此遺失。"""
        if isinstance(error, (DailyPipelineError, DecisionToolError, OSError, ValueError, KeyError)):
            result.errors.append(str(error))
            return
        self._record_unexpected(result, error)
        raise error

    def _record_unexpected(self, result: PipelineResult, error: Exception) -> None:
        result.errors.append("未預期的 %s：%s" % (type(error).__name__, error))
        result.agent_calls = self.calls
        result.stage_durations = dict(self.stage_durations)
        result.skill_versions = self.skill_versions()
        self.last_result = result
        self.log("[錯誤] 決策鏈發生未預期的 %s：%s（Agent 呼叫紀錄已保留）" % (type(error).__name__, error))

    def _run(
        self, result: PipelineResult, snapshot_path: Path, account_snapshot_path: Path,
        trading_status_path: Path, rules_path: Path, database_path: Path,
        policy_template_path: Path, sector_path: Path, decision_run_id: str,
        perception_bundle_path: Optional[Path], perception_result_path: Optional[Path],
        fear_greed_root: Optional[Path] = None, fear_greed_approvals_path: Optional[Path] = None,
        market_news_root: Optional[Path] = None,
        nav_history: Optional[Sequence[Mapping[str, object]]] = None,
        applied_decision_run_ids: Optional[Sequence[str]] = None,
        active_share_holdings_path: Optional[Path] = None,
        active_share_approvals_path: Optional[Path] = None,
        overnight_root: Optional[Path] = None,
        overnight_approvals_path: Optional[Path] = None,
        holdings_history: Optional[Sequence[Mapping[str, object]]] = None,
    ) -> None:
        service = PortfolioDecisionApplicationService
        chain = {"schema_version": "2.1", "event_analysis_mode": "integrated_analyst"}
        chain_path = self._path("decision_chain")
        if chain_path.exists():
            if service.read_json(chain_path, " 決策鏈版本") != chain:
                raise DailyPipelineError("run 的決策鏈版本不符；請建立新 run，不改寫舊輸出")
        elif any(self.run_dir.glob("*_raw_*.json")):
            raise DailyPipelineError("舊版 run 不可直接續跑事件整合新鏈；請建立新 run，舊輸出保留供稽核")
        self.save_artifact("decision_chain", chain)
        if perception_bundle_path is not None and perception_result_path is None:
            from .perception_stage import build_daily_perception
            perception_result_path = build_daily_perception(self, snapshot_path, perception_bundle_path)
        market_news = None
        if market_news_root is not None:
            from etf_agent.perception.market_news import MarketNewsError
            from .market_news_stage import build_daily_market_news
            try:
                with self._timed("market_news"):
                    market_news = build_daily_market_news(
                        self, service.read_json(snapshot_path, " ResearchSnapshot"), market_news_root, database_path)
            except (MarketNewsError, DailyPipelineError) as error:
                # 新聞只是次級輸入：失敗時降級為恐懼貪婪指標，不讓整天交不出決策。
                self.save_artifact("market_news_error", {"error": str(error)})
                self.log("[新聞] 未接入（%s），市場情緒改用恐懼貪婪指標" % error)
        self.log("[decision 1] 建立並驗證 DecisionInputBundle")
        # 公司事件由分析團隊交付，不另產生四子 Agent 的 ResearchResult。
        built = service.build_input_file(
            snapshot_path, database_path, rules_path, self._path("decision_input"),
            trading_status_path=trading_status_path, account_path=account_snapshot_path,
            perception_bundle_path=perception_bundle_path, perception_result_path=perception_result_path,
            fear_greed_root=fear_greed_root, fear_greed_approvals_path=fear_greed_approvals_path,
            overnight_root=overnight_root, overnight_approvals_path=overnight_approvals_path,
            market_news_input=market_news,
            active_share_holdings_path=active_share_holdings_path,
            active_share_approvals_path=active_share_approvals_path,
        )
        if not built["valid"]:
            raise DailyPipelineError("DecisionInputBundle 驗證失敗：" + "；".join(built["errors"]))
        bundle = dict(service.read_json(self._path("decision_input"), " DecisionInputBundle"))
        if bundle.get("perception_inputs"):
            pair = bundle["perception_inputs"][0]
            self.save_artifact("perception_bundle", pair["bundle"])
            self.save_artifact("perception_result", pair["result"])
        self._decide(result, bundle, database_path, policy_template_path, sector_path, decision_run_id, nav_history,
                     applied_decision_run_ids=applied_decision_run_ids, holdings_history=holdings_history)

    def run_from_bundle(
        self,
        bundle_path: Path,
        database_path: Path,
        policy_template_path: Path,
        sector_path: Path,
        decision_run_id: str,
        nav_history: Optional[Sequence[Mapping[str, object]]] = None,
        stop_after: Optional[str] = None,
        applied_decision_run_ids: Optional[Sequence[str]] = None,
        holdings_history: Optional[Sequence[Mapping[str, object]]] = None,
    ) -> PipelineResult:
        """從已封存的 DecisionInputBundle 重跑決策鏈（評測用）；不抓資料、不碰帳本，Bundle 先重新驗證。

        ``stop_after="analysts"`` 時跑完分析團隊就停，只評測分析師、省下多空與交易的用量。
        """
        if stop_after not in {None, "analysts"}:
            raise ValueError("stop_after 只支援 analysts")
        result = PipelineResult(status="failed")
        try:
            service = PortfolioDecisionApplicationService
            bundle = dict(service.read_json(Path(bundle_path), " DecisionInputBundle"))
            errors = DecisionInputValidator(bundle).validate()
            if errors:
                raise DailyPipelineError("DecisionInputBundle 驗證失敗：" + "；".join(errors))
            self.save_artifact("decision_input", bundle)
            self._decide(result, bundle, database_path, policy_template_path, sector_path, decision_run_id,
                         nav_history, stop_after, applied_decision_run_ids, holdings_history)
            result.status = "completed"
        except Exception as error:
            self._finish_failed(result, error)
        result.agent_calls = self.calls
        result.stage_durations = dict(self.stage_durations)
        result.skill_versions = self.skill_versions()
        return result

    def _decide(
        self, result: PipelineResult, bundle: Mapping[str, object], database_path: Path,
        policy_template_path: Path, sector_path: Path, decision_run_id: str,
        nav_history: Optional[Sequence[Mapping[str, object]]] = None, stop_after: Optional[str] = None,
        applied_decision_run_ids: Optional[Sequence[str]] = None,
        holdings_history: Optional[Sequence[Mapping[str, object]]] = None,
    ) -> None:
        service = PortfolioDecisionApplicationService
        # 本次 policy 的 history_ref 由同一份 nav_history 綁定；重建驗證以它回應 head_run_id，其餘仍由 Validator 重算比對。
        history_provider = _history_provider(nav_history, holdings_history)
        decision = service(bundle, nav_history_provider=history_provider, decision_repository=self.decision_repository)

        # 完整鏈先驗策略與帳本前提，缺資料時不可先花分析師／研究員用量；只評測分析師不需要交易策略。
        base_policy = None
        if stop_after != "analysts":
            base_policy = build_decision_policy(
                service.read_json(policy_template_path, " 策略樣板"), bundle,
                service.read_json(sector_path, " 產業分類"),
            )
            base_policy = bind_drawdown(base_policy, bundle, nav_history)
            base_policy = bind_holding_days(base_policy, bundle, holdings_history, nav_history)
            if exposure_rotation_gate(base_policy.get("position_sizing")) is not None and fundamental_period_policy(bundle) is None:
                raise DailyPipelineError("policy 啟用 exposure_rotation 時 bundle 必須有 fundamental_period_policy（team_inputs 2.7 是 2.6 的超集）")
            base_policy["content_sha256"] = decision_policy_sha256(base_policy)
            errors = DecisionPolicyValidator(bundle).validate(base_policy, nav_history)
            if errors:
                raise DailyPipelineError("策略與帳本前置驗證失敗：" + "；".join(errors))

        self.log("[decision 2] 計算動能")
        momentum = MomentumEngine(bundle).run()
        self.save_artifact("momentum", momentum)

        self.log("[decision 3] 分析團隊：技術／基本面／事件（逐批），情緒無核准來源時 unavailable")
        with self._timed("analysts"):
            analysts = self.run_analyst_team(bundle, momentum, database_path)
        if stop_after == "analysts":
            return

        # 空物件表示新鏈沒有獨立事件研究輸入，不是假造 completed／空事件結果。
        research: Dict[str, object] = {}

        self.log("[decision 4] 多頭／空頭研究員（互相隔離、逐批）")
        with self._timed("research"):
            debate = self.run_research_team(bundle, momentum, analysts, research, decision_run_id)

        # 策略與帳本已在 Agent 呼叫前綁定；以下背景仍在研究完成後建立。
        assert base_policy is not None
        # 強制換股只認「論點被推翻」名單；名單綁進 policy，重建驗證不讀結論檔。
        base_policy = bind_forced_swap(base_policy, load_thesis_verdicts(self.root, str(bundle["decision_cutoff"])))
        # 出場冷卻名單由決策庫確定性取得並綁進 policy；重建驗證只讀 policy，不重掃決策庫。
        base_policy = bind_reentry_cooldown(base_policy, self.decision_repository, bundle)
        # 前次決策背景先載入（只給交易 Agent）：cash_ratio_range 的前次現金比例必須取自同一個前次 run。
        prior_context = self.load_prior_decision_context(bundle, applied_decision_run_ids)
        # 姿態內現金子等級（版本閘門）：前次現金比例 anchor 由程式綁進 policy，只給風險 Agent 當背景；沒有閘門原樣不變。
        base_policy = bind_cash_ratio_anchor(base_policy, bundle, self.decision_repository, prior_context)
        base_policy["content_sha256"] = decision_policy_sha256(base_policy)
        # 沒有 rotation 的樣板一律不建名額脈絡，team_inputs 維持 2.2、brief 與舊版逐位相同。
        slots = (
            build_portfolio_slots(bundle, base_policy, debate)
            if isinstance((base_policy.get("position_sizing") or {}).get("rotation"), Mapping)
            else None
        )

        # 沒有 position_review 的樣板一律不建持股檢視，team_inputs 維持 2.3、brief 與舊版逐位相同。
        position_review = build_position_review(bundle, base_policy) if "position_review" in base_policy else None
        if position_review is not None:
            if slots is None:
                raise DailyPipelineError("policy 啟用 position_review 時也必須啟用 position_sizing.rotation（team_inputs 2.4 含名額脈絡）")
            self.save_artifact("position_review", position_review)

        # 沒有 exposure_rotation 的樣板一律不建曝險檢視，team_inputs 維持 2.6、brief 與舊版逐位相同。
        # 曝險檢視讀帳戶與同 Snapshot 行情（beta 對交易池等權）；team_inputs 2.7 是 2.6 的超集，所以也須有 fundamental_period_policy。
        exposure_review = None
        if exposure_rotation_gate(base_policy.get("position_sizing")) is not None:
            if position_review is None:
                raise DailyPipelineError("policy 啟用 exposure_rotation 時也必須啟用 position_review（hold 信心與曝險檢視建立在持股檢視之上）")
            if fundamental_period_policy(bundle) is None:
                raise DailyPipelineError("policy 啟用 exposure_rotation 時 bundle 必須有 fundamental_period_policy（team_inputs 2.7 是 2.6 的超集）")
            exposure_review = build_exposure_review(bundle, base_policy)
            self.save_artifact("exposure_review", exposure_review)

        self.log("[decision 5] 交易 Agent：整合多頭與空頭研究（附前次決策背景，不可引用）")
        with self._timed("trader"):
            trade = self.run_trader(
                bundle, momentum, analysts, debate, decision_run_id, prior_context, slots, position_review,
                exposure_review,
            )

        # 樣板啟用 position_sizing.selection_ranking 才加這一步；沒有的樣板維持舊排序與 team_inputs 版本。
        ranking = None
        if (base_policy.get("position_sizing") or {}).get("selection_ranking") is not None:
            if position_review is None:
                raise DailyPipelineError("policy 啟用 selection_ranking 時也必須啟用 position_review（team_inputs 2.5 含持股檢視）")
            self.log("[decision 5b] 交易 Agent 第二輪：同信心等級內決定入選先後")
            with self._timed("ranking"):
                ranking = self.run_selection_ranking(bundle, momentum, analysts, debate, trade, decision_run_id)

        self.log("[decision 6] 建立 policy，風險 Agent 給現金姿態")
        self.save_artifact("policy_base", base_policy)
        with self._timed("cash_stance"):
            stance = self.run_cash_stance(bundle, momentum, analysts, base_policy.get("drawdown_control"),
                                          (base_policy.get("position_sizing") or {}).get("positions_by_breadth"),
                                          (base_policy.get("position_sizing") or {}).get("cash_buffer_by_stance"),
                                          (base_policy.get("position_sizing") or {}).get("high_conviction_cash_floor"),
                                          (base_policy.get("position_sizing") or {}).get("cash_ratio_range"),
                                          prior_context,
                                          (base_policy.get("position_sizing") or {}).get("cash_stance_recency"),
                                          (base_policy.get("position_sizing") or {}).get("cash_stance_turn_facts"))
        policy = apply_trade_decision(base_policy, trade, stance, bundle, debate, prior_context)
        if ranking is not None:
            policy = apply_selection_ranking(policy, ranking)
        policy["content_sha256"] = decision_policy_sha256(policy)
        policy_errors = DecisionPolicyValidator(bundle).validate(policy, nav_history)
        if policy_errors:
            raise DailyPipelineError("綁定交易決策與現金姿態後 policy 無效：" + "；".join(policy_errors))
        self.save_artifact("policy", policy)
        result.cash_stance = str(stance["level"])
        result.position_breadth = stance.get("breadth")
        if "cash_grade" in stance:
            self.log("  [現金子等級] %s／%s，現金比例 %s（前次比例 %s）" % (
                stance["level"], stance["cash_grade"], policy["cash_buffer_rate"],
                policy["position_sizing"]["cash_ratio_range"]["anchor"]["ratio"]))

        self.log("[decision 7] 配置、情境、Guard 與風險審查（最多 %d 次修正）" % int(policy["max_revisions"]))
        review_started = time.monotonic()
        engine = AllocationOrderEngine(bundle, policy)
        proposal = engine.run(trade)
        history: Optional[Dict[str, object]] = None
        builder = RevisionHistoryBuilder(bundle, policy, trade)
        while True:
            revision = int(proposal["revision_count"])
            scenario = ScenarioEngine(bundle, policy).run(proposal)
            guard = CompetitionGuardV2(bundle, policy).run(proposal, scenario)
            review = self._review(bundle, policy, proposal, scenario, guard, decision_run_id)
            for name, payload in (("proposal", proposal), ("scenario", scenario), ("guard", guard), ("risk_review", review)):
                self.save_artifact("%s_r%d" % (name, revision), payload)
            history = (
                builder.create(proposal, scenario, guard, review)
                if history is None
                else builder.append(history, proposal, scenario, guard, review)
            )
            if review["decision"] != "revise":
                break
            effects = revision_effects(review, proposal)
            proposal = engine.run(
                trade,
                excluded_symbols=effects["excluded_symbols"],
                overrides=effects["overrides"],
                revision_count=revision + 1,
                parent_proposal_id=str(proposal["proposal_id"]),
            )
        self.stage_durations["allocation_review"] = round(time.monotonic() - review_started, 1)

        self.log("[decision 8] finalize、完整重建驗證與封存")
        # bundle 有 fundamental_period_policy 時 team_inputs 升為 2.6，封存分析師看到的基本面指標供 Finalizer 重算比對；
        # 沒有該閘門的舊 bundle（評測重跑）維持 2.5 以前。
        team = build_team_inputs(
            analysts, None, stance, prior_context, slots, position_review, ranking,
            **({"fundamental_metrics": self.fundamental_metrics} if fundamental_period_policy(bundle) is not None else {}),
            **({"exposure_review": exposure_review} if exposure_review is not None else {}),
        )
        final = DecisionFinalizer(bundle, policy, momentum, debate, trade, team, self.decision_repository, history_provider).run(
            proposal, scenario, guard, review, history
        )
        errors = DecisionResultValidator(bundle, policy, momentum, debate, trade, team, self.decision_repository, history_provider).validate(
            proposal, scenario, guard, review, history, final
        )
        if errors:
            raise DailyPipelineError("DecisionResult 驗證失敗：" + "；".join(errors))
        paths = {
            "momentum": self.save_artifact("momentum", momentum),
            "debate": self.save_artifact("debate", debate),
            "intent": self.save_artifact("intent", trade),
            "policy": self._path("policy"),
            "proposal": self.save_artifact("proposal", proposal),
            "scenario": self.save_artifact("scenario", scenario),
            "guard": self.save_artifact("guard", guard),
            "risk_review": self.save_artifact("risk_review", review),
            "revision_history": self.save_artifact("revision_history", history),
            "decision": self.save_artifact("decision", final),
            "team_inputs": self.save_artifact("team_inputs", team),
        }
        decision.save_run_files(decision_run_id, self.decision_repository, paths)
        result.decision_status = str(final["status"])
        result.decision_run_id = decision_run_id
        result.orders = list(final.get("orders", []))



def _history_provider(
    nav_history: Optional[Sequence[Mapping[str, object]]],
    holdings_history: Optional[Sequence[Mapping[str, object]]] = None,
):
    """把本次綁定 policy 用的 NAV 歷史包成 NavHistoryProvider；head 不符即拒絕。沒有歷史時回傳 None。

    ``holdings_history`` 是同一條帳本鏈的逐狀態持股，附成 provider 的 ``holdings_history`` 屬性供 Finalizer
    重算持有天數（沒有時不附，閘門啟用即被 Finalizer 拒絕）。"""
    if nav_history is None:
        return None
    rows = [dict(row) for row in nav_history]

    def provider(head_run_id: str) -> List[Dict[str, object]]:
        if not rows or rows[-1].get("run_id") != head_run_id:
            raise DailyPipelineError("帳本 NAV 歷史的最新一筆不是 history_ref.head_run_id：%s" % head_run_id)
        return rows
    if holdings_history is not None:
        held_rows = [dict(row) for row in holdings_history]

        def holdings(head_run_id: str) -> List[Dict[str, object]]:
            if not held_rows or held_rows[-1].get("run_id") != head_run_id:
                raise DailyPipelineError("帳本持股歷史的最新一筆不是 history_ref.head_run_id：%s" % head_run_id)
            return held_rows
        provider.holdings_history = holdings  # type: ignore[attr-defined]
    return provider


def validate_research_result(snapshot: Mapping[str, object], result: Mapping[str, object]) -> List[str]:
    return ResearchResultValidator(snapshot).validate(result)
