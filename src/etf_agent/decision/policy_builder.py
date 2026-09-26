"""Compose a DecisionPolicy from a strategy template, bundle rules and sector data."""

from __future__ import annotations

from typing import Dict, List, Mapping

from .allocation import DecisionPolicyValidator, decision_policy_sha256
from .contracts import DecisionContext, DecisionToolError, parse_time


# Policy 中與硬性規則重複的欄位一律從 bundle.rules 複製，策略樣板不得自行設定。
RULE_BINDINGS = {
    "lot_size": "lot_size",
    "commission_rate": "commission_rate",
    "sell_tax_rate": "sell_tax_rate",
    "minimum_commission": "minimum_commission",
    "max_stock_weight": "max_stock_weight",
    "special_weight_limits": "special_weight_limits",
    "max_sector_weight": "max_sector_weight",
    "min_positions": "min_positions",
    "max_positions": "max_positions",
    "cash_weight_ceiling": "cash_weight_must_be_below",
    "minimum_active_share": "minimum_active_share",
    "reuse_sell_proceeds": "reuse_sell_proceeds",
}


def build_decision_policy(
    template: Mapping[str, object],
    bundle: Mapping[str, object],
    sector_classification: Mapping[str, object],
) -> Dict[str, object]:
    context = DecisionContext(bundle)
    overlap = sorted(set(template) & (set(RULE_BINDINGS) | {"sector_classification_version", "sector_by_symbol", "content_sha256"}))
    if overlap:
        raise DecisionToolError("策略樣板不得設定硬性規則或產業分類欄位：" + ", ".join(overlap))
    cutoff = parse_time(context.decision_cutoff, "decision_cutoff")
    if parse_time(sector_classification.get("available_at"), "sector_classification.available_at") > cutoff:
        raise DecisionToolError("產業分類晚於 decision_cutoff，不能用於本次決策")
    sectors = sector_classification.get("sector_by_symbol")
    if not isinstance(sectors, Mapping):
        raise DecisionToolError("產業分類缺少 sector_by_symbol")
    missing: List[str] = sorted(context.universe - set(sectors))
    if missing:
        raise DecisionToolError("產業分類未覆蓋交易池：" + ", ".join(missing))
    rules = context.bundle["rules"]
    policy: Dict[str, object] = dict(template)
    policy.update({field: rules[rule] for field, rule in RULE_BINDINGS.items()})
    policy["sector_classification_version"] = sector_classification.get("version")
    policy["sector_by_symbol"] = {
        symbol: sectors[symbol] for symbol in sorted(context.universe)
    }
    policy["content_sha256"] = decision_policy_sha256(policy)
    errors = DecisionPolicyValidator(bundle).validate(policy)
    if errors:
        raise DecisionToolError("DecisionPolicy 驗證失敗：" + "；".join(errors))
    return policy
