"""決策契約的基礎：列舉、錯誤型別、欄位解析、內容雜湊與禁用欄位檢查。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Mapping, Set

from etf_agent.core import canonical_sha256, content_sha256, parse_aware_time, parse_decimal


DECISION_SCHEMA_VERSION = "1.0"
INTENTS = {
    "buy",
    "add",
    "hold",
    "trim",
    "exit",
    "forced_exit",
    "exclude",
    "no_trade",
}


class DecisionToolError(ValueError):
    """Raised when a decision artifact cannot be used safely."""


def parse_time(value: object, field: str) -> datetime:
    return parse_aware_time(value, field, error=DecisionToolError)


def required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DecisionToolError("缺少必要字串欄位：%s" % field)
    return value.strip()


def string_list(payload: Mapping[str, object], field: str) -> List[str]:
    value = payload.get(field)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise DecisionToolError("%s 必須是字串陣列" % field)
    return [item.strip() for item in value]


def decimal_value(value: object, field: str) -> Decimal:
    return parse_decimal(value, field, error=DecisionToolError, parse_message="%(field)s 無法解析為數值")


def snapshot_execution_price(row: Mapping[str, object], field: str) -> Decimal:
    """決策與帳務用價：優先同日官方原價，沒有時退回 analysis_close_price。

    analysis_close_price 是調整價，重放過去日期時含該日之後的除權息，不是當時成交價。
    """
    if row.get("execution_close_price") is not None:
        return decimal_value(row.get("execution_close_price"), field + ".execution_close_price")
    return decimal_value(row.get("analysis_close_price"), field + ".analysis_close_price")


def decision_bundle_sha256(bundle: Mapping[str, object]) -> str:
    payload = dict(bundle)
    payload.pop("bundle_sha256", None)
    return canonical_sha256(payload)


def decision_rules_sha256(rules: Mapping[str, object]) -> str:
    payload = dict(rules)
    payload.pop("config_sha256", None)
    return canonical_sha256(payload)


def artifact_content_sha256(artifact: Mapping[str, object]) -> str:
    return content_sha256(artifact)


# Agent 輸出不得含任何數字化配置欄位；權重、股數、費稅與訂單只由程式計算。
FORBIDDEN_DECISION_KEYS = {
    "allocation",
    "allocation_pct",
    "cash_amount",
    "commission",
    "estimated_fee",
    "target_weight",
    "target_percentage",
    "weight",
    "shares",
    "quantity",
    "units",
    "lots",
    "lot_count",
    "notional",
    "position_size",
    "position_value",
    "fee",
    "fees",
    "tax",
    "transaction_tax",
    "order",
    "orders",
    "order_side",
    "order_type",
}


def validate_forbidden_keys(value: object, path: str, errors: List[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_DECISION_KEYS:
                errors.append("%s 不得包含配置、股數、費稅或訂單欄位：%s" % (path, key))
            validate_forbidden_keys(child, "%s.%s" % (path, key), errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            validate_forbidden_keys(child, "%s[%d]" % (path, index), errors)


def intent_artifact_id(intent: Mapping[str, object]) -> object:
    """TradeDecision 的 decision_id。"""
    return intent.get("decision_id")


def reject_unknown_fields(
    payload: Mapping[str, object],
    allowed: Set[str],
    prefix: str,
    errors: List[str],
) -> None:
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        errors.append("%s 含未允許欄位：%s" % (prefix, ", ".join(unknown)))
