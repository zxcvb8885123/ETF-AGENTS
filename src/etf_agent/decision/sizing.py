"""Risk-agent conviction tiers and their deterministic conversion into weights.

Portfolio Risk 子 Agent 只對 buy／add 候選給出 ``high``／``medium``／``low`` 等級、
理由與證據；權重由本模組依等級乘數與 ATR 波動度確定性計算，Agent 不輸出任何
權重、股數或金額。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, Sequence

from .contracts import (
    DECISION_SCHEMA_VERSION,
    DecisionContext,
    DecisionToolError,
    artifact_content_sha256,
    decimal_value,
    reject_unknown_fields,
    required_string,
    string_list,
)
from .trade_intent import _validate_forbidden_keys


SIZING_METHOD = "conviction_volatility_v1"
CONVICTION_LEVELS = ("high", "medium", "low")
SIZED_INTENTS = {"buy", "add"}


def sizing_candidates(intent_result: Mapping[str, object]) -> List[str]:
    return sorted(
        str(item.get("symbol", "")).upper()
        for item in intent_result.get("items", [])
        if isinstance(item, Mapping) and item.get("intent") in SIZED_INTENTS
    )


class SizingPlanValidator:
    """Check a SizingPlan covers exactly the adjudicated buy／add candidates."""

    ENVELOPE = {
        "schema_version", "plan_id", "bundle_id", "snapshot_id", "decision_cutoff",
        "bundle_hash", "trade_intent_result_id", "trade_intent_sha256", "items",
        "errors", "content_sha256",
    }
    ITEM = {"symbol", "conviction", "rationale", "evidence_ids"}

    def __init__(self, bundle: Mapping[str, object], intent_result: Mapping[str, object]):
        self.context = DecisionContext(bundle)
        self.intent_result = dict(intent_result)

    def validate(self, plan: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        _validate_forbidden_keys(plan, "SizingPlan", errors)
        reject_unknown_fields(plan, self.ENVELOPE, "SizingPlan", errors)
        for field in ("plan_id", "bundle_id", "snapshot_id", "decision_cutoff", "bundle_hash"):
            try:
                required_string(plan, field)
            except DecisionToolError as error:
                errors.append("SizingPlan.%s" % error)
        if plan.get("schema_version") != DECISION_SCHEMA_VERSION:
            errors.append("SizingPlan.schema_version 必須為 %s" % DECISION_SCHEMA_VERSION)
        expected = {
            "bundle_id": self.context.bundle_id,
            "snapshot_id": self.context.snapshot_id,
            "decision_cutoff": self.context.decision_cutoff,
            "bundle_hash": self.context.bundle_hash,
            "trade_intent_result_id": self.intent_result.get("result_id"),
            "trade_intent_sha256": self.intent_result.get("content_sha256"),
        }
        for field, value in expected.items():
            if plan.get(field) != value:
                errors.append("SizingPlan.%s 與共同輸入不一致" % field)
        if plan.get("content_sha256") != artifact_content_sha256(plan):
            errors.append("SizingPlan.content_sha256 與內容不一致")
        if plan.get("errors") != []:
            errors.append("SizingPlan.errors 必須為空陣列；無法分級時不得產生 SizingPlan")
        items = plan.get("items")
        if not isinstance(items, list):
            errors.append("SizingPlan.items 必須是陣列")
            return errors
        seen: List[str] = []
        for index, item in enumerate(items):
            prefix = "SizingPlan.items[%d]" % index
            if not isinstance(item, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            reject_unknown_fields(item, self.ITEM, prefix, errors)
            symbol = str(item.get("symbol", "")).upper()
            seen.append(symbol)
            if item.get("conviction") not in CONVICTION_LEVELS:
                errors.append("%s.conviction 必須是 %s" % (prefix, "／".join(CONVICTION_LEVELS)))
            try:
                required_string(item, "rationale")
                evidence = string_list(item, "evidence_ids")
                if not evidence:
                    errors.append("%s.evidence_ids 不得為空" % prefix)
                self.context.validate_evidence(evidence, symbol, prefix, errors)
            except DecisionToolError as error:
                errors.append("%s.%s" % (prefix, error))
        if len(seen) != len(set(seen)):
            errors.append("SizingPlan.items 股票不得重複")
        candidates = sizing_candidates(self.intent_result)
        missing = sorted(set(candidates) - set(seen))
        extra = sorted(set(seen) - set(candidates))
        if missing:
            errors.append("SizingPlan 未分級全部 buy／add 候選：%s" % ", ".join(missing))
        if extra:
            errors.append("SizingPlan 含非 buy／add 候選：%s" % ", ".join(extra))
        return errors


def apply_sizing_plan(
    policy: Mapping[str, object], plan: Mapping[str, object]
) -> Dict[str, object]:
    """Bind validated tiers into a new policy version; caller re-seals and validates it."""
    sizing = policy.get("position_sizing")
    if not isinstance(sizing, Mapping):
        raise DecisionToolError("DecisionPolicy 未啟用 position_sizing，不能套用 SizingPlan")
    if "conviction_by_symbol" in sizing:
        raise DecisionToolError("DecisionPolicy 已套用 SizingPlan，須從基礎 policy 重新套用")
    result = dict(policy)
    result["position_sizing"] = {
        **dict(sizing),
        "sizing_plan_id": plan.get("plan_id"),
        "sizing_plan_sha256": plan.get("content_sha256"),
        "conviction_by_symbol": {
            str(item["symbol"]).upper(): str(item["conviction"])
            for item in plan.get("items", [])
        },
    }
    result["policy_id"] = "%s+%s" % (policy.get("policy_id"), plan.get("plan_id"))
    result.pop("content_sha256", None)
    return result


def validate_position_sizing(
    sizing: object, universe: Sequence[str], errors: List[str]
) -> None:
    if not isinstance(sizing, Mapping):
        errors.append("DecisionPolicy.position_sizing 必須是物件")
        return
    reject_unknown_fields(
        sizing,
        {
            "method", "conviction_multipliers", "volatility_floor",
            "sizing_plan_id", "sizing_plan_sha256", "conviction_by_symbol",
        },
        "DecisionPolicy.position_sizing",
        errors,
    )
    if sizing.get("method") != SIZING_METHOD:
        errors.append("position_sizing.method 必須為 %s" % SIZING_METHOD)
    multipliers = sizing.get("conviction_multipliers")
    if not isinstance(multipliers, Mapping) or set(multipliers) != set(CONVICTION_LEVELS):
        errors.append("position_sizing.conviction_multipliers 必須剛好包含 high／medium／low")
    else:
        try:
            values = [
                decimal_value(multipliers[level], "conviction_multipliers.%s" % level)
                for level in CONVICTION_LEVELS
            ]
            if any(value <= 0 for value in values):
                errors.append("position_sizing.conviction_multipliers 必須大於 0")
            if not values[0] >= values[1] >= values[2]:
                errors.append("position_sizing.conviction_multipliers 必須 high ≥ medium ≥ low")
        except DecisionToolError as error:
            errors.append(str(error))
    try:
        floor = decimal_value(sizing.get("volatility_floor"), "position_sizing.volatility_floor")
        if floor <= 0 or floor >= 1:
            errors.append("position_sizing.volatility_floor 必須介於 0 與 1（不含）")
    except DecisionToolError as error:
        errors.append(str(error))
    tiers = sizing.get("conviction_by_symbol")
    bound = [field for field in ("sizing_plan_id", "sizing_plan_sha256") if sizing.get(field)]
    if tiers is None:
        if bound:
            errors.append("position_sizing 有 SizingPlan 綁定卻缺少 conviction_by_symbol")
        return
    if len(bound) != 2:
        errors.append("position_sizing.conviction_by_symbol 必須綁定 sizing_plan_id 與 sizing_plan_sha256")
    if not isinstance(tiers, Mapping):
        errors.append("position_sizing.conviction_by_symbol 必須是物件")
        return
    for symbol, tier in tiers.items():
        if symbol != str(symbol).upper() or symbol not in universe:
            errors.append("position_sizing.conviction_by_symbol 含交易池外或非大寫股票：%s" % symbol)
        if tier not in CONVICTION_LEVELS:
            errors.append("position_sizing.conviction_by_symbol.%s 等級無效" % symbol)


def conviction_weights(
    order: Sequence[str],
    tiers: Mapping[str, str],
    multipliers: Mapping[str, Decimal],
    volatility: Mapping[str, Decimal],
    volatility_floor: Decimal,
    limits: Mapping[str, Decimal],
    budget: Decimal,
) -> Dict[str, Decimal]:
    """Split budget ∝ multiplier / max(ATR%, floor); cap at limits and redistribute."""
    raw = {
        symbol: multipliers[tiers[symbol]] / max(volatility[symbol], volatility_floor)
        for symbol in order
    }
    weights = {symbol: Decimal("0") for symbol in order}
    remaining = list(order)
    left = max(budget, Decimal("0"))
    while remaining and left > 0:
        total = sum(raw[symbol] for symbol in remaining)
        proposed = {symbol: left * raw[symbol] / total for symbol in remaining}
        capped = [symbol for symbol in remaining if proposed[symbol] > limits[symbol]]
        if not capped:
            weights.update(proposed)
            break
        for symbol in capped:
            weights[symbol] = limits[symbol]
            left -= limits[symbol]
            remaining.remove(symbol)
    return weights
