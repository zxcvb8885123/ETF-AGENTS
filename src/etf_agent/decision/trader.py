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
from .position_review import position_ids_by_symbol, review_ids_by_symbol
from .prior_decision import prior_reference_tokens
from .sizing import CASH_STANCES, CONVICTION_LEVELS, POSITION_BREADTHS, validate_cash_stance
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
    ):
        """``prior_context`` 是交易 brief 附的前次決策背景（PriorDecisionContext）；它不是證據，
        交易 Agent 的文字欄位出現其識別碼即拒絕。沒有附背景的舊 run 傳 None，驗證不變。

        ``position_review`` 是程式算好的持股檢視（team_inputs 2.4）：其警示 ``review_id`` 與 claim 一樣
        必須在 ``adopted_claim_ids`` 剛好處理一次、不可否決；沒有檢視的舊 run 傳 None，驗證不變。"""
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
        self.prior_tokens = prior_reference_tokens(prior_context)
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
            reject_unknown_fields(item, self.ITEM, prefix, errors)
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
        elif "conviction" in item and item.get("conviction") is not None:
            errors.append("%s 只有 buy／add 可以給 conviction" % prefix)
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
        optional = {claim for claim, role in available.items() if role == "position"}
        missing = sorted(set(available) - optional - set(adopted) - set(rejected))
        if missing:
            errors.append("%s 未處理全部 claim：%s" % (prefix, ", ".join(missing)))
        adopted_roles: Set[str] = {available[claim] for claim in adopted if claim in available}
        if intent in SIZED and "bull" not in adopted_roles:
            errors.append("%s %s 必須採納至少一個多頭 claim" % (prefix, intent))
        if intent in REDUCING and not adopted_roles & {"bear", "review", "position"}:
            errors.append("%s %s 必須採納至少一個空頭 claim、持股檢視警示或持股事實" % (prefix, intent))
        rejected_reviews = sorted(claim for claim in rejected if available.get(claim) == "review")
        if rejected_reviews:
            errors.append("%s 持股檢視警示是程式事實，只能採納不可否決：%s" % (prefix, ", ".join(rejected_reviews)))
        # 虧損警示的持股若要續抱或加碼，必須有多頭依據；沒有多頭論點時只剩減碼或出場。
        loss_alert = any(flag == "loss_alert" for flag in self.reviews.get(symbol, {}).values())
        if loss_alert and intent in {"hold", "add", "no_trade"} and "bull" not in adopted_roles:
            errors.append("%s 虧損警示的持股 %s 必須採納至少一個多頭 claim，否則請 trim 或 exit" % (prefix, intent))


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
) -> Dict[str, object]:
    """把交易 Agent 的信心等級與風險 Agent 的現金姿態（與持股廣度）綁進新版 policy；呼叫端重新封存驗證。"""
    stance_errors: List[str] = []
    validate_cash_stance(DecisionContext(bundle), cash_stance, "cash_stance", stance_errors)
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
    result["position_sizing"] = {
        **dict(sizing),
        "cash_stance": level,
        "sizing_plan_id": decision["decision_id"],
        "sizing_plan_sha256": decision["content_sha256"],
        "conviction_by_symbol": {
            str(item["symbol"]).upper(): str(item["conviction"])
            for item in decision.get("items", [])
            if item.get("intent") in SIZED
        },
    }
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
