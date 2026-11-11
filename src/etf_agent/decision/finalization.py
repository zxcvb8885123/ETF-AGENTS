"""Final decision reconstruction and immutable run storage."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

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
from .momentum import MomentumResultValidator
from .risk import GuardValidator,RiskReviewValidator, ScenarioValidator
from .revision import RevisionHistoryValidator
from .analysts import analyst_report_names, MARKET_SCOPED_VERSIONS, fundamental_period_policy, validate_fundamental_metrics
from .prior_decision import PriorDecisionContextValidator
from .drawdown import validate_drawdown_control
from .exposure_swap import EXPOSURE_SWAP_V2, exposure_swap_gate
from .quality_dip_ruling import RULING_KEY, validate_team_quality_dip_ruling
from .decline_depth import DECLINE_DEPTH_FACTS_KEY, validate_decline_depth_facts, validate_ladder_state
from .offense import OFFENSE_FACTS_KEY, offense_gate, validate_offense_binding, validate_offense_facts
from .quality_dip_swap import QUALITY_DIP_TURN_BOUND_VERSIONS, quality_dip_ladder_gate, quality_dip_swap_gate, quality_dip_v3_gate
from .valuation_facts import VALUATION_FACTS_KEY, validate_valuation_facts, valuation_facts_gate, valuation_root_for
from .exposure import exposure_rotation_gate, hold_conviction_enabled, validate_exposure_review, validate_held_since
from .position_review import validate_position_review
from .selection_ranking import SelectionRankingValidator, rank_by_symbol
from .rotation import validate_portfolio_slots
from .cash_ratio import validate_cash_ratio_anchor
from .recency import cash_stance_recency_gate
from .rebound import rebound_gate, validate_effective_state, validate_turn_state
from .turn_facts import cash_stance_turn_facts_gate
from .swap_nomination import SWAP_NOMINATION_KEY, agent_swap_gate, validate_team_swap_nomination
from .dip_facts import DIP_FACTS_KEY, quality_dip_assessment_gate, quality_dip_facts_gate, validate_dip_facts
from .exposure_facts import EXPOSURE_FACTS_KEY, exposure_view_gate, validate_exposure_facts
from .reentry_cooldown import recent_exit_symbols
from .release_order import RELEASE_ORDER_KEY, validate_team_release_order
from .held_weight_facts import HELD_WEIGHT_FACTS_KEY, validate_held_weight_facts
from .sizing import (
    cash_ratio_gate, cash_ratio_selection_check, drawdown_derisk_v5, held_weight_facts_gate, reentry_cooldown_agent_only,
    reentry_hint_symbols, validate_cash_stance,
)
from .stance import STANCE_SCHEMA_VERSION, ResearchDebateBundleValidator
from .trader import SIZED, TradeDecisionValidator, support_by_symbol


DECISION_ENGINE_VERSION = "1.0.0"
TEAM_INPUT_FIELDS = {"schema_version", "analyst_reports", "research_result", "cash_stance"}
INTEGRATED_TEAM_VERSION = "2.1"
INTEGRATED_TEAM_FIELDS = {"schema_version", "analyst_reports", "cash_stance"}
MEMORY_TEAM_VERSION = "2.2"
MEMORY_TEAM_FIELDS = INTEGRATED_TEAM_FIELDS | {"prior_decision_context"}
ROTATION_TEAM_VERSION = "2.3"
ROTATION_TEAM_FIELDS = MEMORY_TEAM_FIELDS | {"portfolio_slots"}
REVIEW_TEAM_VERSION = "2.4"
REVIEW_TEAM_FIELDS = ROTATION_TEAM_FIELDS | {"position_review"}
RANKING_TEAM_VERSION = "2.5"
RANKING_TEAM_FIELDS = REVIEW_TEAM_FIELDS | {"selection_ranking"}
# 2.6：再加基本面分析師看到的程式計算指標 ``fundamental_metrics``（bundle 有 fundamental_period_policy 時必為 2.6，
# 沒有財報時為 null），Finalizer 以同一份決策輸入整份重算比對；沒有該閘門的舊 bundle／舊 run 維持 2.5 以前。
FUNDAMENTAL_TEAM_VERSION = "2.6"
FUNDAMENTAL_TEAM_FIELDS = RANKING_TEAM_FIELDS | {"fundamental_metrics"}
# 2.7：再加程式計算的曝險檢視 ``exposure_review``（每檔 beta／捕捉與組合 beta），須與 position_sizing.exposure_rotation 閘門
# 同時存在（雙向）；Finalizer 以 ``validate_exposure_review`` 整份重算比對。是 2.6 的超集，所以也須與 fundamental_period_policy 同時存在。
EXPOSURE_TEAM_VERSION = "2.7"
EXPOSURE_TEAM_FIELDS = FUNDAMENTAL_TEAM_FIELDS | {"exposure_review"}
# 2.8：再加程式計算的反彈參與狀態 ``turn_state``（position_sizing.rebound_participation 啟用才有），須與該閘門同時存在（雙向）；
# Finalizer 以 ``validate_turn_state`` 整份重算，並以它與風險 Agent 的 turn_assessment 重算 Policy 綁定的 effective_state。
# 是 2.7 的超集（含 exposure_review），所以也須與 exposure_rotation（v3）同時存在。
REBOUND_TEAM_VERSION = "2.8"
REBOUND_TEAM_FIELDS = EXPOSURE_TEAM_FIELDS | {"turn_state"}
# 2.9：再加交易 Agent 第二輪的換股提名 ``swap_nomination``（position_sizing.agent_swap 啟用才有，v1 影子與 v2 正式欄位集合相同），
# 須與該閘門同時存在（雙向）；Finalizer 以 ``SwapNominationValidator`` 整份重建驗證。是 2.8 的超集，所以也須與
# rebound_participation（v2）、exposure_rotation（v3）同時存在。目前只支援 v1（配置不讀提名）。
SWAP_TEAM_VERSION = "2.9"
SWAP_TEAM_FIELDS = REBOUND_TEAM_FIELDS | {SWAP_NOMINATION_KEY}
# 2.10：再加風險 Agent 對 quality_dip 配對的裁決 ``quality_dip_ruling``（position_sizing.agent_swap.quality_dip version=v3 才有，
# 見 docs/quality_dip_agent_sizing_plan.md），須與該閘門同時存在（雙向）；是 2.9 的超集。v1／v2 或沒有 quality_dip 的 run 仍是 2.9。
QUALITY_RULING_TEAM_VERSION = "2.10"
QUALITY_RULING_TEAM_FIELDS = SWAP_TEAM_FIELDS | {RULING_KEY}
TEAM_FIELDS_BY_VERSION = {
    STANCE_SCHEMA_VERSION: TEAM_INPUT_FIELDS,
    INTEGRATED_TEAM_VERSION: INTEGRATED_TEAM_FIELDS,
    MEMORY_TEAM_VERSION: MEMORY_TEAM_FIELDS,
    ROTATION_TEAM_VERSION: ROTATION_TEAM_FIELDS,
    REVIEW_TEAM_VERSION: REVIEW_TEAM_FIELDS,
    RANKING_TEAM_VERSION: RANKING_TEAM_FIELDS,
    FUNDAMENTAL_TEAM_VERSION: FUNDAMENTAL_TEAM_FIELDS,
    EXPOSURE_TEAM_VERSION: EXPOSURE_TEAM_FIELDS,
    REBOUND_TEAM_VERSION: REBOUND_TEAM_FIELDS,
    SWAP_TEAM_VERSION: SWAP_TEAM_FIELDS,
    QUALITY_RULING_TEAM_VERSION: QUALITY_RULING_TEAM_FIELDS,
}
# 2.2 起都附前次決策背景；2.3 再加程式算好的名額脈絡。
# 2.4 再加程式算好的持股檢視（停損／停利警示），是 2.3 的超集。
# 2.5 再加交易 Agent 第二輪的入選排序 ``selection_ranking``（同信心等級內的先後），是 2.4 的超集。
# 2.6 再加程式計算的基本面指標 ``fundamental_metrics``，是 2.5 的超集；2.7 再加曝險檢視 ``exposure_review``，是 2.6 的超集。
SWAP_TEAM_VERSIONS = (SWAP_TEAM_VERSION, QUALITY_RULING_TEAM_VERSION)
REBOUND_TEAM_VERSIONS = (REBOUND_TEAM_VERSION,) + SWAP_TEAM_VERSIONS
EXPOSURE_TEAM_VERSIONS = (EXPOSURE_TEAM_VERSION,) + REBOUND_TEAM_VERSIONS
FUNDAMENTAL_TEAM_VERSIONS = (FUNDAMENTAL_TEAM_VERSION,) + EXPOSURE_TEAM_VERSIONS
MEMORY_TEAM_VERSIONS = (MEMORY_TEAM_VERSION, ROTATION_TEAM_VERSION, REVIEW_TEAM_VERSION, RANKING_TEAM_VERSION) + FUNDAMENTAL_TEAM_VERSIONS
SLOTS_TEAM_VERSIONS = (ROTATION_TEAM_VERSION, REVIEW_TEAM_VERSION, RANKING_TEAM_VERSION) + FUNDAMENTAL_TEAM_VERSIONS
REVIEW_TEAM_VERSIONS = (REVIEW_TEAM_VERSION, RANKING_TEAM_VERSION) + FUNDAMENTAL_TEAM_VERSIONS
RANKING_TEAM_VERSIONS = (RANKING_TEAM_VERSION,) + FUNDAMENTAL_TEAM_VERSIONS
_UNSET = object()


def build_team_inputs(
    analyst_reports: Mapping[str, Mapping[str, object]],
    research_result: Optional[Mapping[str, object]],
    cash_stance: Mapping[str, object],
    prior_decision_context: Optional[Mapping[str, object]] = None,
    portfolio_slots: Optional[Mapping[str, object]] = None,
    position_review: Optional[Mapping[str, object]] = None,
    selection_ranking: Optional[Mapping[str, object]] = None,
    fundamental_metrics: object = _UNSET,
    exposure_review: Optional[Mapping[str, object]] = None,
    turn_state: Optional[Mapping[str, object]] = None,
    dip_facts: Optional[Mapping[str, object]] = None,
    swap_nomination: Optional[Mapping[str, object]] = None,
    exposure_facts: Optional[Mapping[str, object]] = None,
    quality_dip_ruling: Optional[Mapping[str, object]] = None,
    release_order: Optional[Mapping[str, object]] = None,
    held_weight_facts: Optional[Mapping[str, object]] = None,
    valuation_facts: Optional[Mapping[str, object]] = None,
    decline_depth_facts: Optional[Mapping[str, object]] = None,
    offense_facts: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """Decision run 的 team_inputs artifact：重建多空辯論、交易決策與現金姿態所需的全部輸入。

    2.0：舊封存，另含事件研究 ``research_result``；2.1：三份分析報告與現金姿態；
    2.2：再加交易 Agent 看到的前次決策背景 ``prior_decision_context``（available 或 unavailable 都要存）；
    2.3：再加程式算好的名額脈絡 ``portfolio_slots``（滿檔換股），是 2.2 的超集；
    2.4：再加程式算好的持股檢視 ``position_review``（停損／停利警示），是 2.3 的超集；
    2.5：再加交易 Agent 第二輪的入選排序 ``selection_ranking``（同信心等級內的先後），是 2.4 的超集；
    2.6：再加程式計算的基本面指標 ``fundamental_metrics``（有傳入即為 2.6，沒有財報時傳 None 存為 null），是 2.5 的超集；
    2.7：再加程式計算的曝險檢視 ``exposure_review``（position_sizing.exposure_rotation 啟用才有），是 2.6 的超集；
    2.8：再加程式計算的反彈參與狀態 ``turn_state``（position_sizing.rebound_participation 啟用才有），是 2.7 的超集；
    2.9：再加交易 Agent 第二輪的換股提名 ``swap_nomination``（position_sizing.agent_swap 啟用才有），是 2.8 的超集；
    2.10：再加風險 Agent 對 quality_dip 配對的裁決 ``quality_dip_ruling``（position_sizing.agent_swap.quality_dip version=v3 才有），是 2.9 的超集。

    選配鍵 ``dip_facts``（position_sizing.quality_dip_facts 啟用才有）：不升版本、不排進線性版本鏈，只要求 2.6 以上
    （需要 fundamental_metrics）；Finalizer 以 Policy 閘門雙向檢查。沒有傳入時 team_inputs 與舊版逐鍵相同。

    選配鍵 ``exposure_facts``（position_sizing.exposure_view 啟用才有）：做法同 dip_facts，不升版本，只要求 2.7 以上
    （需要 exposure_review 同口徑的 beta）；Finalizer 以 Policy 閘門雙向檢查並整份重算。沒有傳入時 team_inputs 逐鍵不變。

    選配鍵 ``release_order``（position_sizing.drawdown_derisk v5 才有）：交易 Agent 第二輪給的回撤減碼釋出順序，做法同 dip_facts，
    不升版本（2.9 已被換股提名占用，且 v5 的前提只有 selection_ranking），只要求 2.5 以上；Finalizer 以 Policy 閘門雙向檢查並整份
    重建驗證、比對 Policy 綁定。沒有傳入時 team_inputs 逐鍵不變。

    選配鍵 ``held_weight_facts``（position_sizing.held_weight_budget=v2 才有）：程式算的持股權重事實，做法同 dip_facts，不升版本，
    只要求 2.5 以上；Finalizer 以 Policy 閘門雙向檢查並整份重算。沒有傳入時 team_inputs 逐鍵不變。

    選配鍵 ``decline_depth_facts``（position_sizing.agent_swap.quality_dip v4 才有）：程式算的交易池連跌深度事實與對照樣本，做法同 dip_facts，
    不升版本、不排進線性版本鏈，只要求 2.10（v4 是 v3 的超集），與 v4 閘門雙向同時存在，Finalizer 整份重算。
    選配鍵 ``offense_facts``（position_sizing.offense 才有，轉向後進攻）：程式算的交易池與加權指數進攻事實（賣壓竭盡、廣度回升、量能、情緒極端、
    指數站回與對照樣本），做法同 dip_facts，不升版本（2.10 已被 quality_dip v3 占用），只要求 2.9 以上（需要換股提名）；與 offense 閘門雙向同時存在，
    Finalizer 整份重算。沒有傳入時 team_inputs 逐鍵不變。
    選配鍵 ``valuation_facts``（position_sizing.valuation_facts 才有，估值 V2）：程式算的 PER／PBR 位置事實，做法同 dip_facts，
    不升版本、不要求特定版本；Finalizer 以 Policy 閘門雙向檢查，並以同一個估值資料夾整份重算。沒有傳入時 team_inputs 逐鍵不變。
    """
    if prior_decision_context is not None and research_result is not None:
        raise DecisionToolError("team_inputs 不能同時含舊版事件研究與前次決策背景")
    if portfolio_slots is not None and prior_decision_context is None:
        raise DecisionToolError("team_inputs 2.3 的名額脈絡必須與前次決策背景同時存在")
    if position_review is not None and portfolio_slots is None:
        raise DecisionToolError("team_inputs 2.4 的持股檢視必須與名額脈絡同時存在")
    if selection_ranking is not None and position_review is None:
        raise DecisionToolError("team_inputs 2.5 的入選排序必須與持股檢視同時存在")
    if fundamental_metrics is not _UNSET and selection_ranking is None:
        raise DecisionToolError("team_inputs 2.6 的基本面指標必須與入選排序同時存在")
    if fundamental_metrics is not _UNSET and fundamental_metrics is not None and not isinstance(fundamental_metrics, Mapping):
        raise DecisionToolError("team_inputs 2.6 的 fundamental_metrics 必須是物件或 None")
    if exposure_review is not None and fundamental_metrics is _UNSET:
        raise DecisionToolError("team_inputs 2.7 的曝險檢視必須與基本面指標（2.6）同時存在")
    if exposure_review is not None and not isinstance(exposure_review, Mapping):
        raise DecisionToolError("team_inputs 2.7 的 exposure_review 必須是物件")
    if turn_state is not None and exposure_review is None:
        raise DecisionToolError("team_inputs 2.8 的反彈參與狀態必須與曝險檢視（2.7）同時存在")
    if turn_state is not None and not isinstance(turn_state, Mapping):
        raise DecisionToolError("team_inputs 2.8 的 turn_state 必須是物件")
    if dip_facts is not None and (fundamental_metrics is _UNSET or not isinstance(dip_facts, Mapping)):
        raise DecisionToolError("team_inputs 的 dip_facts 必須是物件，且只能用於 2.6 以上（需要基本面指標）")
    if exposure_facts is not None and (exposure_review is None or not isinstance(exposure_facts, Mapping)):
        raise DecisionToolError("team_inputs 的 exposure_facts 必須是物件，且只能用於 2.7 以上（需要曝險檢視）")
    if release_order is not None and (selection_ranking is None or not isinstance(release_order, Mapping)):
        raise DecisionToolError("team_inputs 的 release_order 必須是物件，且只能用於 2.5 以上（需要入選排序）")
    if held_weight_facts is not None and (selection_ranking is None or not isinstance(held_weight_facts, Mapping)):
        raise DecisionToolError("team_inputs 的 held_weight_facts 必須是物件，且只能用於 2.5 以上（需要入選排序）")
    if swap_nomination is not None and turn_state is None:
        raise DecisionToolError("team_inputs 2.9 的換股提名必須與反彈參與狀態（2.8）同時存在")
    if swap_nomination is not None and not isinstance(swap_nomination, Mapping):
        raise DecisionToolError("team_inputs 2.9 的 swap_nomination 必須是物件")
    if quality_dip_ruling is not None and (swap_nomination is None or not isinstance(quality_dip_ruling, Mapping)):
        raise DecisionToolError("team_inputs 2.10 的 quality_dip_ruling 必須是物件，且須與換股提名（2.9）同時存在")
    if decline_depth_facts is not None and (quality_dip_ruling is None or not isinstance(decline_depth_facts, Mapping)):
        raise DecisionToolError("team_inputs 的 decline_depth_facts 必須是物件，且只能用於 2.10（quality_dip v4 是 v3 的超集）")
    if offense_facts is not None and (swap_nomination is None or not isinstance(offense_facts, Mapping)):
        raise DecisionToolError("team_inputs 的 offense_facts 必須是物件，且只能用於 2.9 以上（需要換股提名）")
    if quality_dip_ruling is not None:
        version = QUALITY_RULING_TEAM_VERSION
    elif swap_nomination is not None:
        version = SWAP_TEAM_VERSION
    elif turn_state is not None:
        version = REBOUND_TEAM_VERSION
    elif exposure_review is not None:
        version = EXPOSURE_TEAM_VERSION
    elif fundamental_metrics is not _UNSET:
        version = FUNDAMENTAL_TEAM_VERSION
    elif selection_ranking is not None:
        version = RANKING_TEAM_VERSION
    elif position_review is not None:
        version = REVIEW_TEAM_VERSION
    elif portfolio_slots is not None:
        version = ROTATION_TEAM_VERSION
    elif prior_decision_context is not None:
        version = MEMORY_TEAM_VERSION
    elif research_result is None:
        version = INTEGRATED_TEAM_VERSION
    else:
        version = STANCE_SCHEMA_VERSION
    team = {
        "schema_version": version,
        "analyst_reports": {name: dict(analyst_reports[name]) for name in analyst_report_names(analyst_reports)},
        "cash_stance": dict(cash_stance),
    }
    if research_result is not None:
        team["research_result"] = dict(research_result)
    if prior_decision_context is not None:
        team["prior_decision_context"] = dict(prior_decision_context)
    if portfolio_slots is not None:
        team["portfolio_slots"] = dict(portfolio_slots)
    if position_review is not None:
        team["position_review"] = dict(position_review)
    if selection_ranking is not None:
        team["selection_ranking"] = dict(selection_ranking)
    if fundamental_metrics is not _UNSET:
        team["fundamental_metrics"] = dict(fundamental_metrics) if fundamental_metrics is not None else None
    if exposure_review is not None:
        team["exposure_review"] = dict(exposure_review)
    if turn_state is not None:
        team["turn_state"] = dict(turn_state)
    if swap_nomination is not None:
        team[SWAP_NOMINATION_KEY] = dict(swap_nomination)
    if quality_dip_ruling is not None:
        team[RULING_KEY] = dict(quality_dip_ruling)
    if dip_facts is not None:
        team[DIP_FACTS_KEY] = dict(dip_facts)
    if exposure_facts is not None:
        team[EXPOSURE_FACTS_KEY] = dict(exposure_facts)
    if release_order is not None:
        team[RELEASE_ORDER_KEY] = dict(release_order)
    if held_weight_facts is not None:
        team[HELD_WEIGHT_FACTS_KEY] = dict(held_weight_facts)
    if decline_depth_facts is not None:
        team[DECLINE_DEPTH_FACTS_KEY] = dict(decline_depth_facts)
    if offense_facts is not None:
        team[OFFENSE_FACTS_KEY] = dict(offense_facts)
    if valuation_facts is not None:
        if not isinstance(valuation_facts, Mapping):
            raise DecisionToolError("team_inputs 的 valuation_facts 必須是物件")
        team[VALUATION_FACTS_KEY] = dict(valuation_facts)
    return team


# 以 ``history_ref.head_run_id`` 讀回帳本 NAV 歷史（舊到新，止於該 head）；讀不到須拋例外，Finalizer 視為驗證失敗。
NavHistoryProvider = Callable[[str], Sequence[Mapping[str, object]]]
# 以 ``drawdown_control.history_ref.head_run_id`` 讀回帳本逐狀態的持股（舊到新，止於該 head；每列 run_id、as_of、symbols），
# exposure_rotation 重算持有天數用；讀不到須拋例外，Finalizer 視為驗證失敗。
HoldingsHistoryProvider = Callable[[str], Sequence[Mapping[str, object]]]


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
        decision_repository: Optional[Path] = None,
        nav_history_provider: Optional[NavHistoryProvider] = None,
        holdings_history_provider: Optional[HoldingsHistoryProvider] = None,
    ):
        self.decision_repository = decision_repository
        self.nav_history_provider = nav_history_provider
        # 沒有明確傳入時，取 ledger_nav_history_provider 附的同帳本持股歷史來源（沒有帳本來源則為 None，閘門啟用即拒絕）。
        self.holdings_history_provider = holdings_history_provider or getattr(nav_history_provider, "holdings_history", None)
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
        body["team_inputs_sha256"] = canonical_sha256(self.team_inputs) if self.team_inputs is not None else None
        body["decision_id"] = "decision:" + canonical_sha256(body)[:20]
        body["content_sha256"] = artifact_content_sha256(body)
        return body

    def _validate_drawdown_history(self) -> List[str]:
        """policy 的 drawdown_control 帶 ``history_ref`` 時，以 head_run_id 讀帳本歷史重算峰值；fail-closed。

        沒有 ``history_ref`` 的舊 policy 不需要帳本，行為與舊版相同。"""
        control = self.policy.get("drawdown_control")
        ref = control.get("history_ref") if isinstance(control, Mapping) else None
        if ref is None:
            return []
        prefix = "DecisionPolicy.drawdown_control.history_ref"
        head = ref.get("head_run_id") if isinstance(ref, Mapping) else None
        if not isinstance(head, str) or not head:
            return ["%s.head_run_id 必須是非空字串" % prefix]
        if self.nav_history_provider is None:
            return ["%s 需要帳本 NAV 歷史才能重算峰值，但呼叫端未提供帳本來源" % prefix]
        try:
            history = list(self.nav_history_provider(head))
        except Exception as error:  # 帳本缺 head、雜湊鏈損毀或讀取失敗一律拒絕
            return ["%s 無法由帳本讀回歷史（head_run_id=%s）：%s" % (prefix, head, error)]
        errors: List[str] = []
        validate_drawdown_control(self.policy, self.bundle, errors, history)
        return errors

    def _validate_reentry_cooldown(self) -> List[str]:
        """reentry_cooldown v2：以決策庫整份重算「交易 Agent 自己的出場」名單，與 Policy 綁定的名單比對；fail-closed。

        v1 與沒有閘門的舊 policy 不碰決策庫，行為與舊版相同。"""
        sizing = self.policy.get("position_sizing")
        if not isinstance(sizing, Mapping) or not reentry_cooldown_agent_only(sizing):
            return []
        cooldown = sizing["reentry_cooldown"]
        prefix = "DecisionPolicy.position_sizing.reentry_cooldown"
        if cooldown.get("blocked_symbols") is None or cooldown.get("source_run_ids") is None:
            return ["%s v2 必須綁定 blocked_symbols 與 source_run_ids" % prefix]
        days = cooldown.get("days")
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            return ["%s.days 必須是正整數" % prefix]
        try:
            symbols, run_ids = recent_exit_symbols(self.decision_repository, self.bundle, days, agent_only=True)
        except Exception as error:  # 決策庫損毀、manifest 或 intent.json 不可用一律拒絕，不當成沒有出場
            return ["%s 無法由決策庫重算冷卻名單：%s" % (prefix, error)]
        if cooldown["blocked_symbols"] != symbols or cooldown["source_run_ids"] != run_ids:
            return ["%s 的名單與決策庫重算不一致（重算：%s／%s）" % (prefix, symbols, run_ids)]
        return []

    def _validate_cash_ratio_anchor(self) -> List[str]:
        """policy 有 cash_ratio_range 時，以決策庫（前次 run）或本次帳戶重算 anchor 比對；fail-closed。

        前次決策背景（team_inputs 2.2 以上）為 available 時 anchor 必須是同一個 run；``prior_decision`` 而呼叫端沒給決策庫即拒絕。
        沒有閘門的舊 policy 不讀決策庫，行為與舊版相同。"""
        team = self.team_inputs or {}
        prior = team.get("prior_decision_context") if isinstance(team.get("prior_decision_context"), Mapping) else None
        return validate_cash_ratio_anchor(self.policy, self.bundle, self.decision_repository, prior)

    def validate_inputs(
        self,
        proposal: Mapping[str, object],
        scenario: Mapping[str, object],
        guard: Mapping[str, object],
        risk_review: Mapping[str, object],
        history: Mapping[str, object],
    ) -> List[str]:
        errors: List[str] = []
        errors.extend(self._validate_drawdown_history())
        # exposure_rotation（版本閘門）：持有天數以帳本獨立重算；沒有閘門的舊 policy 不碰帳本。
        validate_held_since(self.policy, self.bundle, self.holdings_history_provider, errors, self.nav_history_provider)
        errors.extend(self._validate_cash_ratio_anchor())
        errors.extend(self._validate_reentry_cooldown())
        errors.extend(self._validate_team_chain())
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
        # 回撤的結構檢查與帳本檢查可能指出同一個問題：去除完全相同的訊息，保留原順序。
        return list(dict.fromkeys(errors))


    def _validate_team_chain(self) -> List[str]:
        """重建驗證多空辯論、交易決策、現金姿態，以及 policy 綁定的等級與姿態；舊版 1.0 鏈已不再支援。"""
        if self.debate.get("schema_version") != STANCE_SCHEMA_VERSION:
            return ["ResearchDebateBundle.schema_version 必須為 %s；舊版 1.0 決策鏈已移除" % STANCE_SCHEMA_VERSION]
        team = self.team_inputs
        if team is None:
            return ["決策鏈需要 team_inputs（分析報告與現金姿態；舊封存另含事件研究）才能重建驗證"]
        # 動能結果由程式重算；共同輸入只綁 result_id，不重算會讓改寫的數值通過。
        errors: List[str] = [
            "momentum：%s" % error for error in MomentumResultValidator(self.bundle).validate(self.momentum)
        ]
        version = team.get("schema_version")
        fields = TEAM_FIELDS_BY_VERSION.get(str(version))
        if fields is None:
            return [
                "team_inputs.schema_version 必須為 2.0（舊封存）、2.1（事件併入分析團隊）、"
                "2.2（附前次決策背景）、2.3（再附名額脈絡）、2.4（再附持股檢視）、2.5（再附入選排序）、"
                "2.6（再附基本面指標）、2.7（再附曝險檢視）、2.8（再附反彈參與狀態）、2.9（再附換股提名）或 2.10（再附優質股裁決）"
            ]
        # quality_dip_facts（版本閘門）的 dip_facts 是選配鍵：與 Policy 閘門雙向同時存在；沒有閘門的舊 run 欄位檢查與舊版相同。
        dip_gate = quality_dip_facts_gate(self.policy.get("position_sizing"))
        if set(team) - {
            DIP_FACTS_KEY, EXPOSURE_FACTS_KEY, RELEASE_ORDER_KEY, HELD_WEIGHT_FACTS_KEY, VALUATION_FACTS_KEY, DECLINE_DEPTH_FACTS_KEY,
            OFFENSE_FACTS_KEY,
        } != fields:
            return ["team_inputs 欄位必須剛好是：%s" % "、".join(sorted(fields))]
        # drawdown_derisk v5（版本閘門）的 release_order 也是選配鍵：與 Policy 閘門雙向同時存在、須 2.5 以上，整份重建驗證並比對 Policy 綁定。
        # 沒有 v5 的舊 run 完全不碰這段。
        if (RELEASE_ORDER_KEY in team) != drawdown_derisk_v5(self.policy.get("position_sizing")):
            errors.append("DecisionPolicy 的 position_sizing.drawdown_derisk v5 必須與 team_inputs.release_order 同時存在")
        elif RELEASE_ORDER_KEY in team:
            if version not in RANKING_TEAM_VERSIONS:
                errors.append("team_inputs.release_order 只能用於 2.5 以上（需要入選排序）")
            else:
                validate_team_release_order(self.bundle, self.policy, self.intent, self.debate, team, errors)
        # held_weight_budget=v2（版本閘門）的 held_weight_facts 也是選配鍵：與 Policy 閘門雙向同時存在、須 2.5 以上，整份重算比對。
        # 沒有 v2 的舊 run 完全不碰這段。
        if (HELD_WEIGHT_FACTS_KEY in team) != held_weight_facts_gate(self.policy.get("position_sizing")):
            errors.append("DecisionPolicy 的 position_sizing.held_weight_budget=v2 必須與 team_inputs.held_weight_facts 同時存在")
        elif HELD_WEIGHT_FACTS_KEY in team:
            if version not in RANKING_TEAM_VERSIONS:
                errors.append("team_inputs.held_weight_facts 只能用於 2.5 以上（需要入選排序）")
            else:
                validate_held_weight_facts(self.bundle, self.policy, team, errors)
        # exposure_view（版本閘門）的 exposure_facts 也是選配鍵：與 Policy 閘門雙向同時存在、須 2.7 以上，整份重算比對。
        view_gate = exposure_view_gate(self.policy.get("position_sizing"))
        if (EXPOSURE_FACTS_KEY in team) != (view_gate is not None):
            errors.append("DecisionPolicy 的 position_sizing.exposure_view 必須與 team_inputs.exposure_facts 同時存在")
        elif view_gate is not None:
            if version not in EXPOSURE_TEAM_VERSIONS:
                errors.append("team_inputs.exposure_facts 只能用於 2.7 以上（需要曝險檢視）")
            else:
                validate_exposure_facts(self.bundle, self.policy, team, errors)
        # valuation_facts（估值 V2，版本閘門）也是選配鍵：與 Policy 閘門雙向同時存在，以同一個估值資料夾（由決策庫位置推得）整份重算。
        # 沒有閘門的舊 run 完全不碰這段、不讀估值資料夾。
        valuation_gate = valuation_facts_gate(self.policy.get("position_sizing"))
        if (VALUATION_FACTS_KEY in team) != (valuation_gate is not None):
            errors.append("DecisionPolicy 的 position_sizing.valuation_facts 必須與 team_inputs.valuation_facts 同時存在")
        elif valuation_gate is not None:
            validate_valuation_facts(valuation_root_for(self.decision_repository), self.bundle, self.policy, team, errors)
        # agent_swap.quality_dip v4（版本閘門，連跌後逢低布局）的 decline_depth_facts 也是選配鍵：與 v4 的 ladder 雙向同時存在、須 2.10，整份重算比對。
        # 沒有 v4 的舊 run 完全不碰這段。
        ladder_gate = quality_dip_ladder_gate(self.policy.get("position_sizing"))
        if (DECLINE_DEPTH_FACTS_KEY in team) != (ladder_gate is not None):
            errors.append("DecisionPolicy 的 position_sizing.agent_swap.quality_dip v4（ladder）必須與 team_inputs.decline_depth_facts 同時存在")
        elif ladder_gate is not None:
            if version != QUALITY_RULING_TEAM_VERSION:
                errors.append("team_inputs.decline_depth_facts 只能用於 2.10（quality_dip v4 是 v3 的超集）")
            else:
                validate_decline_depth_facts(self.bundle, self.policy, team, errors)
        # offense（轉向後進攻，版本閘門）的 offense_facts 也是選配鍵：與 Policy 閘門雙向同時存在、須 2.9 以上，整份重算比對。
        # 沒有 offense 的舊 run 完全不碰這段。
        offense_policy_gate = offense_gate(self.policy.get("position_sizing"))
        if (OFFENSE_FACTS_KEY in team) != (offense_policy_gate is not None):
            errors.append("DecisionPolicy 的 position_sizing.offense 必須與 team_inputs.offense_facts 同時存在")
        elif offense_policy_gate is not None:
            if version not in SWAP_TEAM_VERSIONS:
                errors.append("team_inputs.offense_facts 只能用於 2.9 以上（需要換股提名）")
            else:
                validate_offense_facts(self.bundle, self.policy, team, errors)
        if (DIP_FACTS_KEY in team) != (dip_gate is not None):
            errors.append("DecisionPolicy 的 position_sizing.quality_dip_facts 必須與 team_inputs.dip_facts 同時存在")
        elif dip_gate is not None:
            if version not in FUNDAMENTAL_TEAM_VERSIONS:
                errors.append("team_inputs.dip_facts 只能用於 2.6 以上（需要基本面指標）")
            else:
                validate_dip_facts(self.bundle, self.policy, team, errors)
        integrated = version != STANCE_SCHEMA_VERSION
        # 2.6 與 bundle 的 fundamental_period_policy 必須同時存在；基本面指標以同一份決策輸入整份重算比對。
        # 沒有該閘門的舊 bundle／舊 run 不重算基本面指標，行為與舊版相同。
        if (version in FUNDAMENTAL_TEAM_VERSIONS) != (fundamental_period_policy(self.bundle) is not None):
            errors.append("team_inputs 2.6／2.7 的基本面指標必須與 DecisionInputBundle.fundamental_period_policy 同時存在")
        # 2.7 與 policy 的 position_sizing.exposure_rotation 必須同時存在（雙向）；曝險檢視以 bundle 與 policy 整份重算比對。
        exposure_gate = exposure_rotation_gate(self.policy.get("position_sizing"))
        if (version in EXPOSURE_TEAM_VERSIONS) != (exposure_gate is not None):
            errors.append("DecisionPolicy 的 position_sizing.exposure_rotation 必須與 team_inputs 2.7 同時存在（2.8 是 2.7 的超集）")
        if version in EXPOSURE_TEAM_VERSIONS and exposure_gate is not None:
            validate_exposure_review(self.bundle, self.policy, team.get("exposure_review"), errors)
        # 2.8 與 policy 的 position_sizing.rebound_participation 必須同時存在（雙向）；turn_state 以 bundle 與 policy 整份重算比對。
        rebound = rebound_gate(self.policy.get("position_sizing"))
        if (version in REBOUND_TEAM_VERSIONS) != (rebound is not None):
            errors.append("DecisionPolicy 的 position_sizing.rebound_participation 必須與 team_inputs 2.8 同時存在（2.9 是 2.8 的超集）")
        if version in REBOUND_TEAM_VERSIONS and rebound is not None:
            validate_turn_state(self.bundle, self.policy, team.get("turn_state"), errors)
        # 2.9 與 policy 的 position_sizing.agent_swap 必須同時存在（雙向）；換股提名以同一份輸入整份重建驗證。
        # 沒有閘門的舊 run（2.8 以下）完全不碰這段。
        swap_gate = agent_swap_gate(self.policy.get("position_sizing"))
        if (version in SWAP_TEAM_VERSIONS) != (swap_gate is not None):
            errors.append("DecisionPolicy 的 position_sizing.agent_swap 必須與 team_inputs 2.9 同時存在（2.10 是 2.9 的超集）")
        elif version in SWAP_TEAM_VERSIONS:
            validate_team_swap_nomination(self.bundle, self.policy, self.intent, self.debate, team, errors)
            # 2.10 與 quality_dip v3 必須同時存在（雙向）；裁決以同一份 team_inputs 整份重建驗證並比對 Policy 綁定。
            # 沒有 v3 的舊 run（2.9）完全不碰這段。
            if (version == QUALITY_RULING_TEAM_VERSION) != (quality_dip_v3_gate(self.policy.get("position_sizing")) is not None):
                errors.append("DecisionPolicy 的 position_sizing.agent_swap.quality_dip 必須與 team_inputs 2.10 同時存在（v3 才用 2.10）")
            elif version == QUALITY_RULING_TEAM_VERSION:
                validate_team_quality_dip_ruling(self.bundle, self.policy, team, errors)
        if version in FUNDAMENTAL_TEAM_VERSIONS:
            errors.extend(
                "team_inputs.fundamental_metrics：%s" % error
                for error in validate_fundamental_metrics(self.bundle, team.get("fundamental_metrics"))
            )
        # 2.2／2.3 的前次決策背景先自行重建驗證，再交給 TradeDecisionValidator 檢查交易 Agent 沒有引用它。
        prior = team.get("prior_decision_context") if version in MEMORY_TEAM_VERSIONS else None
        if version in MEMORY_TEAM_VERSIONS:
            # cash_ratio_range.selection_check（版本閘門）開啟時另以決策庫重跑前次 run 的挑選；沒有閘門的舊 run 只驗來源內容。
            selection_check = cash_ratio_selection_check(self.policy.get("position_sizing") or {})
            errors.extend(
                "team_inputs.prior_decision_context：%s" % error
                for error in PriorDecisionContextValidator(
                    self.bundle, self.decision_repository, selection_check=selection_check
                ).validate(prior)
            )
        # 2.3 的名額脈絡全部由程式算；以最終 policy 重算比對（build_portfolio_slots 只讀
        # apply_trade_decision 不會改動的欄位，所以與交易階段用基礎 policy 建出的結果相同）。
        if version in SLOTS_TEAM_VERSIONS:
            validate_portfolio_slots(
                self.bundle, self.policy, self.debate, team.get("portfolio_slots"), errors
            )
        # 持股檢視與 policy 的 position_review 必須同時存在；數值以 bundle 與 policy 重算比對。
        review = team.get("position_review") if version in REVIEW_TEAM_VERSIONS else None
        if version in REVIEW_TEAM_VERSIONS:
            validate_position_review(self.bundle, self.policy, review, errors)
        if (version in REVIEW_TEAM_VERSIONS) != ("position_review" in self.policy):
            errors.append("DecisionPolicy 的 position_review 必須與 team_inputs 2.4 同時存在")
        reports = team.get("analyst_reports")
        research = {} if integrated else team.get("research_result")
        if not isinstance(reports, Mapping) or not isinstance(research, Mapping):
            return errors + ["team_inputs 的分析報告或舊版 ResearchResult 格式無效"]
        try:
            analyst_report_names(reports)
        except DecisionToolError as error:
            return errors + [str(error)]
        if integrated:
            if set(reports) != {"technical", "fundamental", "event"} or reports["event"].get("schema_version") not in MARKET_SCOPED_VERSIONS:
                errors.append("team_inputs 2.1／2.2 必須使用三份分析報告及 event 2.2 或 2.3（含個股新聞）")
        else:
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
        errors.extend(
            TradeDecisionValidator(
                self.bundle, self.momentum, self.debate, prior_context=prior, position_review=review,
                hold_conviction=hold_conviction_enabled(self.policy),
                dip_facts=team.get(DIP_FACTS_KEY) if dip_gate is not None else None,
                reentry_hint_symbols=reentry_hint_symbols(self.policy.get("position_sizing")),
                dip_assessment=quality_dip_assessment_gate(self.policy.get("position_sizing")) is not None,
                valuation_facts=team.get(VALUATION_FACTS_KEY) if valuation_facts_gate(self.policy.get("position_sizing")) is not None else None,
            ).validate(self.intent)
        )
        stance = team.get("cash_stance")
        sizing = self.policy.get("position_sizing")
        # cash_ratio_range（版本閘門）：現金姿態另含 cash_grade，必須在 policy 綁定的 anchor 算出的今日可選格點內。
        gate = cash_ratio_gate(sizing) if isinstance(sizing, Mapping) else None
        validate_cash_stance(
            self.context, stance, "team_inputs.cash_stance", errors, cash_ratio_range=gate, prior_context=prior,
            recency_gate=cash_stance_recency_gate(sizing), turn_facts_gate=cash_stance_turn_facts_gate(sizing),
            exposure_view_gate=view_gate,
            exposure_facts=team.get(EXPOSURE_FACTS_KEY) if view_gate is not None else None,
            **({"offense_gate": offense_policy_gate, "offense_facts": team.get(OFFENSE_FACTS_KEY)} if offense_policy_gate is not None else {}),
        )
        if not isinstance(sizing, Mapping):
            return errors + ["DecisionPolicy 必須啟用 position_sizing"]
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
        if "support_by_symbol" in sizing:
            expected["support_by_symbol"] = support_by_symbol(self.intent, self.debate)
        if "positions_by_breadth" in sizing:
            expected["position_breadth"] = stance.get("breadth") if isinstance(stance, Mapping) else None
        if gate is not None:
            expected["cash_grade"] = stance.get("cash_grade") if isinstance(stance, Mapping) else None
        if version in REBOUND_TEAM_VERSIONS:
            validate_effective_state(self.policy, team, errors)
        if hold_conviction_enabled(sizing):
            expected["hold_conviction_by_symbol"] = {
                str(item.get("symbol", "")).upper(): item.get("conviction")
                for item in self.intent.get("items", [])
                if isinstance(item, Mapping) and item.get("intent") == "hold"
            }
        # 入選排序與 policy 的 selection_ranking 必須同時存在；排序以交易決策重建驗證，名次由程式綁定。
        ranking = team.get("selection_ranking") if version in RANKING_TEAM_VERSIONS else None
        if (version in RANKING_TEAM_VERSIONS) != ("selection_ranking" in sizing):
            errors.append("DecisionPolicy 的 position_sizing.selection_ranking 必須與 team_inputs 2.5／2.6 同時存在")
        if version in RANKING_TEAM_VERSIONS:
            if not isinstance(ranking, Mapping):
                errors.append("team_inputs.selection_ranking 必須是物件")
            else:
                errors.extend(
                    "team_inputs.selection_ranking：%s"
                    % error for error in SelectionRankingValidator(self.bundle, self.intent).validate(ranking)
                )
                expected["rank_by_symbol"] = rank_by_symbol(ranking)
                expected["selection_ranking_id"] = ranking.get("ranking_id")
                expected["selection_ranking_sha256"] = ranking.get("content_sha256")
        for field, value in expected.items():
            if sizing.get(field) != value:
                errors.append("DecisionPolicy.position_sizing.%s 與交易決策或現金姿態不一致" % field)
        # agent_swap.exposure v2（版本閘門）：Policy 綁定的 view 必須等於風險 Agent 現金姿態的 exposure_view（配置只讀 Policy）。
        # v1（影子）不綁定；沒有閘門的舊 run 完全不碰這段。
        # position_sizing.offense：Policy 綁定的有效等級、壓回原因與現金放寬記錄必須等於以現金姿態、offense_facts 與回撤下限重算的結果；
        # 此時曝險配對的 view 綁的是有效等級（可能被壓回 keep_defensive），不是風險 Agent 的原始列舉。
        if offense_policy_gate is not None:
            validate_offense_binding(self.policy, team, errors)
        exposure_swap = exposure_swap_gate(sizing)
        if exposure_swap is not None:
            bound_view = exposure_swap.get("view") if exposure_swap.get("version") == EXPOSURE_SWAP_V2 else None
            expected_view = stance.get("exposure_view") if isinstance(stance, Mapping) and exposure_swap.get("version") == EXPOSURE_SWAP_V2 else None
            if offense_policy_gate is not None and exposure_swap.get("version") == EXPOSURE_SWAP_V2:
                expected_view = offense_policy_gate.get("effective_view")
            if bound_view != expected_view:
                errors.append("DecisionPolicy.position_sizing.agent_swap.exposure.view 與現金姿態的 exposure_view 不一致")
        # agent_swap.quality_dip v2（版本閘門）：Policy 綁定的 turn_assessment 必須等於風險 Agent 現金姿態的 turn_assessment。
        # v1（影子）不綁定（sizing 驗證已拒絕預填）；沒有閘門的舊 run 完全不碰這段。
        quality_swap = quality_dip_swap_gate(sizing)
        if quality_swap is not None:
            v2 = quality_swap.get("version") in QUALITY_DIP_TURN_BOUND_VERSIONS
            bound_turn = quality_swap.get("turn_assessment") if v2 else None
            expected_turn = stance.get("turn_assessment") if isinstance(stance, Mapping) and v2 else None
            if bound_turn != expected_turn or (v2 and expected_turn is None):
                errors.append("DecisionPolicy.position_sizing.agent_swap.quality_dip.turn_assessment 與現金姿態的 turn_assessment 不一致")
            # v4：梯度狀態（程式算）必須與以 team_inputs.decline_depth_facts、帳本綁定的持有起點與本次 turn_assessment 重算的結果一致。
            validate_ladder_state(self.bundle, self.policy, team, errors)
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
        decision_repository: Optional[Path] = None,
        nav_history_provider: Optional[NavHistoryProvider] = None,
        holdings_history_provider: Optional[HoldingsHistoryProvider] = None,
    ):
        self.finalizer = DecisionFinalizer(
            bundle, policy, momentum_result, debate, intent_result, team_inputs, decision_repository,
            nav_history_provider, holdings_history_provider,
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
        super().__init__(root, schema_version=DECISION_SCHEMA_VERSION, error=DecisionToolError, dated=True)
