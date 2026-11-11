"""交易 Agent：權衡多空論點後的 TradeDecision 與確定性配置轉接。

交易 Agent 逐檔決定意圖與 buy／add 的信心等級，並對多空兩方每個 claim 採納或否決；
權重、張數、費稅與現金仍由既有 AllocationOrderEngine 依 policy 計算。
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Set

from .contracts import (
    validate_forbidden_keys,
    DecisionContext,
    DecisionToolError,
    artifact_content_sha256,
    reject_unknown_fields,
    required_string,
    string_list,
)
from .drawdown import stance_meets_floor
from .exposure import hold_conviction_enabled
from .position_review import position_ids_by_symbol, review_ids_by_symbol
from .prior_decision import prior_reference_tokens
from .dip_facts import dip_reference_tokens, validate_dip_assessment
from .valuation_facts import valuation_reference_tokens
from .recency import cash_stance_recency_gate
from .rebound import bind_effective_state, loosened_cash_buffer, rebound_gate
from .turn_facts import cash_stance_turn_facts_gate
from .exposure_facts import exposure_view_gate
from .exposure_swap import bind_exposure_view, exposure_swap_gate
from .offense import (
    CASH_RAISE_LIMITED_FIELD, bind_offense, build_offense_facts, cash_glide_enabled, cash_raise_limited, offense_cash_target, offense_gate,
)
from .decline_depth import build_ladder_state
from .quality_dip_swap import bind_quality_dip_ladder_state, bind_quality_dip_turn, quality_dip_ladder_gate, quality_dip_swap_gate
from .sizing import CASH_STANCES, CONVICTION_LEVELS, POSITION_BREADTHS, cash_ratio_gate, validate_cash_stance
from .stance import STANCE_SCHEMA_VERSION


TRADE_INTENTS = ("buy", "add", "hold", "trim", "exit", "forced_exit", "no_trade")
SIZED = {"buy", "add"}
REDUCING = {"trim", "exit", "forced_exit"}
HELD_ONLY = {"add", "hold", "trim", "exit", "forced_exit"}


def trade_decision_envelope(bundle: Mapping[str, object], debate: Mapping[str, object], decision_id: str) -> Dict[str, object]:
    return {
        "schema_version": STANCE_SCHEMA_VERSION,
        "decision_id": decision_id,
        "bundle_id": bundle["bundle_id"],
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "bundle_hash": bundle["bundle_sha256"],
        "research_debate_id": debate["debate_id"],
        "research_debate_sha256": debate["content_sha256"],
    }


def seal_trade_decision(envelope: Mapping[str, object], items: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    decision = {**dict(envelope), "status": "completed", "items": [dict(item) for item in items], "errors": []}
    decision["content_sha256"] = artifact_content_sha256(decision)
    return decision


def claims_by_symbol(debate: Mapping[str, object]) -> Dict[str, Dict[str, str]]:
    """每檔可裁決的 claim：claim_id → 來源角色（bull／bear）。"""
    result: Dict[str, Dict[str, str]] = {}
    for packet in debate.get("packets", []):
        for item in packet.get("items", []):
            symbol = str(item.get("symbol", "")).upper()
            for claim in item.get("claims", []):
                result.setdefault(symbol, {})[str(claim["claim_id"])] = str(packet["role"])
    return result


class TradeDecisionValidator:
    """交易決策必須覆蓋全部股票、逐一處理每個 claim，且意圖與持股、動能、採納方向一致。"""

    ENVELOPE = {
        "schema_version", "decision_id", "bundle_id", "snapshot_id", "decision_cutoff", "bundle_hash",
        "research_debate_id", "research_debate_sha256", "status", "items", "errors", "content_sha256",
    }
    ITEM = {
        "symbol", "intent", "conviction", "rationale", "adopted_claim_ids", "rejected_claim_ids",
        "unresolved_questions", "invalidation_conditions",
    }

    def __init__(
        self,
        bundle: Mapping[str, object],
        momentum: Mapping[str, object],
        debate: Mapping[str, object],
        symbols: Optional[Sequence[str]] = None,
        prior_context: Optional[Mapping[str, object]] = None,
        position_review: Optional[Mapping[str, object]] = None,
        hold_conviction: bool = False,
        dip_facts: Optional[Mapping[str, object]] = None,
        reentry_hint_symbols: Optional[Sequence[str]] = None,
        dip_assessment: bool = False,
        valuation_facts: Optional[Mapping[str, object]] = None,
    ):
        """``prior_context`` 是交易 brief 附的前次決策背景（PriorDecisionContext）；它不是證據，
        交易 Agent 的文字欄位出現其識別碼即拒絕。沒有附背景的舊 run 傳 None，驗證不變。

        ``position_review`` 是程式算好的持股檢視（team_inputs 2.4）：其警示 ``review_id`` 與 claim 一樣
        必須在 ``adopted_claim_ids`` 剛好處理一次、不可否決；沒有檢視的舊 run 傳 None，驗證不變。

        ``hold_conviction``（exposure_rotation 閘門）為 True 時，hold 也必填 conviction（持股還值得佔一個位置的程度）；
        預設 False，hold 帶信心即拒絕（現行）。

        ``dip_facts`` 是 quality_dip_facts 閘門的事實（team_inputs.dip_facts）：交易 brief 以 ``dip_background`` 附上，
        不是證據，文字欄位出現其識別碼即拒絕；沒有閘門的舊 run 傳 None，驗證不變。

        ``dip_assessment``（quality_dip_assessment 閘門）為 True 時，buy／add 必填列舉 ``dip_assessment``（需 ``dip_facts``，
        且決策輸入須是 typed_evidence_v1）；預設 False，出現該欄位即拒絕（舊 run 驗證與舊版相同）。

        ``reentry_hint_symbols``（reentry_cooldown v3）是近期出場的代號：未持有者各有一個 ``cooldown:<代號>`` 提示，買回時放進
        ``adopted_claim_ids`` 回應；只檢查結構（只屬於該股票、不可否決、不重複），**不強制**回應——沒回應的買單由配置取消並留旗標
        （RISK_REENTRY_COOLDOWN），不讓一個提示沒回應就耗盡修正循環。沒有 v3 的舊 run 傳 None，驗證不變。"""
        self.hold_conviction = hold_conviction
        self.dip_assessment = dip_assessment
        self.dip_facts = dip_facts
        self.context = DecisionContext(bundle)
        self.bundle = dict(bundle)
        self.debate = dict(debate)
        self.claims = claims_by_symbol(debate)
        self.reviews = review_ids_by_symbol(position_review)
        for symbol, flags in self.reviews.items():
            self.claims.setdefault(symbol, {}).update({review: "review" for review in flags})
        # 選用的「持股事實」編號：減碼／出場可引用它當依據（交易 Agent 對報酬與回落的自主判斷），但不強制處理、可不採納。
        self.position_ids = position_ids_by_symbol(position_review)
        for symbol, identifier in self.position_ids.items():
            self.claims.setdefault(symbol, {})[identifier] = "position"
        for symbol in sorted({str(item).upper() for item in (reentry_hint_symbols or ())} - self.context.held_symbols):
            self.claims.setdefault(symbol, {})["cooldown:%s" % symbol] = "cooldown"
        self.prior_tokens = prior_reference_tokens(prior_context)
        self.dip_tokens = dip_reference_tokens(dip_facts)
        # valuation_facts（估值 V2，版本閘門）：交易 brief 以 valuation_background 附上，不是證據；沒有閘門的舊 run 傳 None，驗證不變。
        self.valuation_tokens = valuation_reference_tokens(valuation_facts)
        self.available = {
            str(item.get("symbol", "")).upper()
            for item in momentum.get("items", [])
            if item.get("status") == "available"
        }
        self.universe = {str(symbol).upper() for symbol in (symbols if symbols is not None else self.context.universe)}

    def validate(self, decision: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        validate_forbidden_keys(decision, "TradeDecision", errors)
        reject_unknown_fields(decision, self.ENVELOPE, "TradeDecision", errors)
        expected = trade_decision_envelope(self.bundle, self.debate, str(decision.get("decision_id")))
        for field, value in expected.items():
            if decision.get(field) != value:
                errors.append("TradeDecision.%s 與研究辯論不一致" % field)
        try:
            required_string(decision, "decision_id")
        except DecisionToolError as error:
            errors.append("TradeDecision.%s" % error)
        if decision.get("status") != "completed":
            errors.append("只有 status=completed 的 TradeDecision 可進入配置")
        if decision.get("errors") != []:
            errors.append("TradeDecision.errors 必須為空陣列")
        if decision.get("content_sha256") != artifact_content_sha256(decision):
            errors.append("TradeDecision.content_sha256 與內容不一致")
        items = decision.get("items")
        if not isinstance(items, list):
            return errors + ["TradeDecision.items 必須是陣列"]
        seen: List[str] = []
        for index, item in enumerate(items):
            prefix = "items[%d]" % index
            if not isinstance(item, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            reject_unknown_fields(item, self.ITEM | ({"dip_assessment"} if self.dip_assessment else set()), prefix, errors)
            symbol = str(item.get("symbol", "")).upper()
            seen.append(symbol)
            self._validate_item(item, symbol, prefix, errors)
        if len(seen) != len(set(seen)):
            errors.append("TradeDecision.items 股票不得重複")
        missing = sorted(self.universe - set(seen))
        extra = sorted(set(seen) - self.universe)
        if missing:
            errors.append("TradeDecision 未裁決全部股票：%s" % ", ".join(missing))
        if extra:
            errors.append("TradeDecision 含本次範圍外股票：%s" % ", ".join(extra))
        return errors

    def _validate_item(self, item: Mapping[str, object], symbol: str, prefix: str, errors: List[str]) -> None:
        intent = item.get("intent")
        if intent not in TRADE_INTENTS:
            errors.append("%s.intent 必須是 %s" % (prefix, "／".join(TRADE_INTENTS)))
            return
        held = symbol in self.context.held_symbols
        if held and intent == "buy":
            errors.append("%s 已持股不得 buy，請用 add" % prefix)
        if not held and intent in HELD_ONLY:
            errors.append("%s 未持股不得 %s" % (prefix, intent))
        if intent in SIZED:
            if item.get("conviction") not in CONVICTION_LEVELS:
                errors.append("%s %s 必須給 conviction（%s）" % (prefix, intent, "／".join(CONVICTION_LEVELS)))
            if symbol not in self.available:
                errors.append("%s %s 需要 status=available 的動能資料" % (prefix, intent))
        elif intent == "hold" and self.hold_conviction:
            if item.get("conviction") not in CONVICTION_LEVELS:
                errors.append(
                    "%s hold 必須給 conviction（%s）：以目前資訊這檔還值得佔一個位置的程度，"
                    "定義見 data-rules.md；不是為了買別檔而給" % (prefix, "／".join(CONVICTION_LEVELS))
                )
        elif "conviction" in item and item.get("conviction") is not None:
            errors.append(
                "%s 只有 buy／add／hold 可以給 conviction" % prefix if self.hold_conviction
                else "%s 只有 buy／add 可以給 conviction" % prefix
            )
        if self.dip_assessment and intent not in SIZED and item.get("dip_assessment") is not None:
            errors.append("%s 只有 buy／add 可以給 dip_assessment" % prefix)
        if self.dip_assessment and intent in SIZED:
            if self.context.evidence_policy is None:
                errors.append("%s：quality_dip_assessment 只能用於 evidence_policy=typed_evidence_v1 的決策輸入" % prefix)
            validate_dip_assessment(item, symbol, prefix, self.dip_facts, errors)
        try:
            rationale = required_string(item, "rationale")
            adopted = string_list(item, "adopted_claim_ids")
            rejected = string_list(item, "rejected_claim_ids")
            questions = string_list(item, "unresolved_questions")
            conditions = string_list(item, "invalidation_conditions")
        except DecisionToolError as error:
            errors.append("%s.%s" % (prefix, error))
            return
        cited = sorted(
            token for token in self.prior_tokens
            if any(token in text for text in (rationale, *questions, *conditions))
        )
        if cited:
            errors.append("%s 不得引用前次決策背景（%s）；它不是本次證據" % (prefix, ", ".join(cited)))
        dip_cited = sorted(
            token for token in self.dip_tokens
            if any(token in text for text in (rationale, *questions, *conditions))
        )
        if dip_cited:
            errors.append(
                "%s 不得引用 dip_background（%s）；它是程式算的背景、不是本次證據，請引用多空 claim 的依據" % (prefix, ", ".join(dip_cited))
            )
        valuation_cited = sorted(
            token for token in self.valuation_tokens
            if any(token in text for text in (rationale, *questions, *conditions))
        )
        if valuation_cited:
            errors.append(
                "%s 不得引用 valuation_background（%s）；估值位置是程式算的背景、不是本次證據，請引用多空 claim 的依據"
                % (prefix, ", ".join(valuation_cited))
            )
        available = self.claims.get(symbol, {})
        repeated = sorted({claim for claim in (*adopted, *rejected) if (*adopted, *rejected).count(claim) > 1})
        if repeated:
            errors.append("%s claim／警示必須剛好處理一次，不得重複：%s" % (prefix, ", ".join(repeated)))
        both = set(adopted) & set(rejected)
        if both:
            errors.append("%s claim 不得同時採納與否決：%s" % (prefix, ", ".join(sorted(both))))
        unknown = sorted((set(adopted) | set(rejected)) - set(available))
        if unknown:
            errors.append("%s 引用不屬於該股票的 claim：%s" % (prefix, ", ".join(unknown)))
        optional = {claim for claim, role in available.items() if role in ("position", "cooldown")}
        missing = sorted(set(available) - optional - set(adopted) - set(rejected))
        if missing:
            errors.append("%s 未處理全部 claim：%s" % (prefix, ", ".join(missing)))
        adopted_roles: Set[str] = {available[claim] for claim in adopted if claim in available}
        if intent in SIZED and "bull" not in adopted_roles:
            errors.append(
                "%s %s 需要至少一個被採納的多頭 claim；若你不認同任何多頭論點，請改為 %s（降級），"
                "不要為了通過驗證而採納不認同的 claim" % (prefix, intent, "hold 或 no_trade" if held else "no_trade")
            )
        if intent in REDUCING and not adopted_roles & {"bear", "review", "position"}:
            errors.append(
                "%s %s 需要至少一個被採納的空頭 claim、持股檢視警示或持股事實；若沒有可採納的依據，請改為 hold（降級）"
                % (prefix, intent)
            )
        rejected_hints = sorted(claim for claim in rejected if available.get(claim) == "cooldown")
        if rejected_hints:
            errors.append("%s 冷卻提示是程式事實，只能在買回時採納、不可否決：%s" % (prefix, ", ".join(rejected_hints)))
        rejected_reviews = sorted(claim for claim in rejected if available.get(claim) == "review")
        if rejected_reviews:
            errors.append("%s 持股檢視警示是程式事實，只能採納不可否決：%s" % (prefix, ", ".join(rejected_reviews)))
        # 虧損警示的持股若要續抱或加碼，必須有多頭依據；沒有多頭論點時只剩減碼或出場。
        loss_alert = any(flag == "loss_alert" for flag in self.reviews.get(symbol, {}).values())
        if loss_alert and intent in {"hold", "add", "no_trade"} and "bull" not in adopted_roles:
            errors.append(
                "%s 虧損警示的持股 %s 需要至少一個被採納的多頭 claim；兩種合法修法：有你認同的多頭論點就補採納它，"
                "沒有就改為 trim 或 exit（降級），不要為了續抱而採納不認同的 claim" % (prefix, intent)
            )


def support_by_symbol(
    decision: Mapping[str, object], debate: Mapping[str, object]
) -> Dict[str, int]:
    """buy／add 股票的同等級排序依據：採納的多頭 claim 數減採納的空頭 claim 數。"""
    claims = claims_by_symbol(debate)
    result: Dict[str, int] = {}
    for item in decision.get("items", []):
        if item.get("intent") not in SIZED:
            continue
        symbol = str(item["symbol"]).upper()
        roles = [claims.get(symbol, {}).get(str(claim_id)) for claim_id in item.get("adopted_claim_ids", [])]
        result[symbol] = roles.count("bull") - roles.count("bear")
    return result


def apply_trade_decision(
    policy: Mapping[str, object],
    decision: Mapping[str, object],
    cash_stance: Mapping[str, object],
    bundle: Mapping[str, object],
    debate: Optional[Mapping[str, object]] = None,
    prior_context: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """把交易 Agent 的信心等級與風險 Agent 的現金姿態（與持股廣度）綁進新版 policy；呼叫端重新封存驗證。

    ``prior_context`` 是本次的前次決策背景；cash_ratio_range 啟用時用來拒絕理由引用前次 decision_id。"""
    # cash_ratio_range（版本閘門）：風險 Agent 另給 cash_grade，現金比例由 grid 對應；沒有閘門時驗證與舊版逐位相同。
    gate = cash_ratio_gate(policy.get("position_sizing") or {})
    if gate is not None and "anchor" not in gate:
        raise DecisionToolError("position_sizing.cash_ratio_range 尚未綁定 anchor（前次現金比例）")
    stance_errors: List[str] = []
    # position_sizing.offense（轉向後進攻，版本閘門）：進攻事實由程式依 bundle 重算（與 team_inputs.offense_facts 同一份），沒有閘門時完全不經過。
    offense_policy_gate = offense_gate(policy.get("position_sizing") or {})
    offense_facts = build_offense_facts(bundle, policy) if offense_policy_gate is not None else None
    validate_cash_stance(
        DecisionContext(bundle), cash_stance, "cash_stance", stance_errors, cash_ratio_range=gate, prior_context=prior_context,
        recency_gate=cash_stance_recency_gate(policy.get("position_sizing") or {}),
        turn_facts_gate=cash_stance_turn_facts_gate(policy.get("position_sizing") or {}),
        exposure_view_gate=exposure_view_gate(policy.get("position_sizing") or {}),
        **({"offense_gate": offense_policy_gate, "offense_facts": offense_facts} if offense_policy_gate is not None else {}),
    )
    if stance_errors:
        raise DecisionToolError("現金姿態無效：" + "；".join(stance_errors))
    sizing = policy.get("position_sizing")
    if not isinstance(sizing, Mapping) or "conviction_by_symbol" in sizing:
        raise DecisionToolError("DecisionPolicy 必須是啟用 position_sizing 且尚未綁定的基礎 policy")
    buffers = sizing.get("cash_buffer_by_stance")
    level = cash_stance.get("level")
    if not isinstance(buffers, Mapping) or level not in CASH_STANCES or level not in buffers:
        raise DecisionToolError("cash_stance 無法對應 position_sizing.cash_buffer_by_stance")
    control = policy.get("drawdown_control")
    floor = control.get("stance_floor") if isinstance(control, Mapping) else None
    if not stance_meets_floor(level, floor):
        raise DecisionToolError("現金姿態 %s 低於回撤下限 %s" % (level, floor))
    result = dict(policy)
    result["cash_buffer_rate"] = buffers[level]
    if gate is not None:
        result["cash_buffer_rate"] = gate["grid"][level][cash_stance["cash_grade"]]  # type: ignore[index]
    result["position_sizing"] = {
        **dict(sizing),
        **({"cash_grade": cash_stance["cash_grade"]} if gate is not None else {}),
        "cash_stance": level,
        "sizing_plan_id": decision["decision_id"],
        "sizing_plan_sha256": decision["content_sha256"],
        "conviction_by_symbol": {
            str(item["symbol"]).upper(): str(item["conviction"])
            for item in decision.get("items", [])
            if item.get("intent") in SIZED
        },
    }
    if hold_conviction_enabled(sizing):
        # exposure_rotation：hold 持股的信心另綁一張表，不併入 conviction_by_symbol（forced_swap 與現金下限都讀那張）。
        holds = {
            str(item["symbol"]).upper(): item.get("conviction")
            for item in decision.get("items", [])
            if item.get("intent") == "hold"
        }
        invalid = sorted(symbol for symbol, level in holds.items() if level not in CONVICTION_LEVELS)
        if invalid:
            raise DecisionToolError("Policy 已啟用 exposure_rotation，hold 必須有信心等級：" + "、".join(invalid))
        result["position_sizing"]["hold_conviction_by_symbol"] = holds
    if rebound_gate(sizing) is not None:
        # rebound_participation：有效狀態 = min(程式 turn_state, 風險 Agent turn_assessment)，回撤下限為 defensive 時恆 none；
        # 由程式依 bundle 重算後綁進閘門，Finalizer 以 team_inputs 重算比對。
        bound_gate = bind_effective_state(sizing, bundle, cash_stance, floor)
        # 條件式現金放寬：雙鑰匙都同意轉向（有效狀態非 none）且姿態為 defensive 時，現金比例改用該態的 defensive_cash_buffer
        # （只放寬；回撤下限為 defensive 時有效狀態恆 none，所以回撤下限優先）。前後值記進 cash_loosening 供稽核。
        loosened, loosening = loosened_cash_buffer(bound_gate, level, result["cash_buffer_rate"])
        if loosening is not None:
            result["cash_buffer_rate"] = loosened
            bound_gate = {**bound_gate, "cash_loosening": loosening}
        result["position_sizing"]["rebound_participation"] = bound_gate
    offense_effective = None
    if offense_policy_gate is not None:
        # position_sizing.offense：有效等級（缺資料、回撤下限為 defensive、帳本回撤不可用或回撤達停用線時壓回 keep_defensive）綁進閘門；
        # 防守姿態下現金目標改為 min(格點值, 該級 defensive_cash_buffer)（只放寬）。樣板預填綁定欄位即拒絕；Finalizer 以 team_inputs 重算比對。
        bound_offense = bind_offense(sizing, cash_stance, control, offense_facts)
        loosened_cash, cash_loosening = offense_cash_target(bound_offense, level, result["cash_buffer_rate"])
        if cash_loosening is not None:
            result["cash_buffer_rate"] = loosened_cash
            bound_offense = {**bound_offense, "cash_loosening": cash_loosening}
        if cash_glide_enabled(bound_offense):
            # offense.cash_glide：放寬之後的防守姿態現金目標，回升不得超過 anchor＋max_daily_raise（只在前次放寬過且回撤條件成立時）；記錄前後值供稽核。
            limited = cash_raise_limited(sizing, bound_offense, control, level, result["cash_buffer_rate"])
            bound_offense = {**bound_offense, CASH_RAISE_LIMITED_FIELD: limited}
            if limited is not None:
                result["cash_buffer_rate"] = limited["to"]
        result["position_sizing"]["offense"] = bound_offense
        offense_effective = str(bound_offense["effective_view"])
    if exposure_swap_gate(sizing) is not None:
        # agent_swap.exposure v2（版本閘門）：風險 Agent 的 exposure_view（offense 啟用時為有效等級）綁進 Policy，配置只讀 Policy；v1（影子）不綁。
        result["position_sizing"]["agent_swap"] = bind_exposure_view(sizing, cash_stance, offense_effective)
    if quality_dip_swap_gate(sizing) is not None:
        # agent_swap.quality_dip v2（版本閘門）：風險 Agent 的 turn_assessment 綁進 Policy，配置只讀 Policy；v1（影子）不綁。
        swap = result["position_sizing"].get("agent_swap", sizing["agent_swap"])  # type: ignore[index]
        result["position_sizing"]["agent_swap"] = bind_quality_dip_turn(swap, sizing, cash_stance)
        if quality_dip_ladder_gate(sizing) is not None:
            # agent_swap.quality_dip v4：梯度對今天的判定（程式算、缺資料即關閉）綁進 Policy，配置與風險 Agent 的前置檢查只讀綁定值；
            # Finalizer 以 team_inputs.decline_depth_facts、帳本綁定的 held_since_by_symbol 與本次 turn_assessment 重算比對。
            result["position_sizing"]["agent_swap"] = bind_quality_dip_ladder_state(
                result["position_sizing"]["agent_swap"],
                build_ladder_state(bundle, policy, str(cash_stance.get("turn_assessment"))),
            )
    if "positions_by_breadth" in sizing:
        breadth = cash_stance.get("breadth")
        if breadth not in POSITION_BREADTHS:
            raise DecisionToolError("Policy 已啟用 positions_by_breadth，現金姿態必須給 breadth（%s）" % "／".join(POSITION_BREADTHS))
        result["position_sizing"]["position_breadth"] = breadth
    if debate is not None:
        result["position_sizing"]["support_by_symbol"] = support_by_symbol(decision, debate)
    result["policy_id"] = "%s+%s" % (policy.get("policy_id"), decision["decision_id"])
    result.pop("content_sha256", None)
    return result
