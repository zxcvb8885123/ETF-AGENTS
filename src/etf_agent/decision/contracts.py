"""Point-in-time contracts shared by portfolio decision agents.

相容轉出層：實作已依職責拆成 `fields.py`（列舉、錯誤型別、欄位解析、雜湊、禁用欄位）、
`input_validator.py`（DecisionInputValidator）與 `input_context.py`（DecisionContext）。
既有 `from etf_agent.decision.contracts import ...` 路徑維持可用。
"""

from __future__ import annotations

from etf_agent.core import canonical_sha256, content_sha256, parse_aware_time, parse_decimal

from .fields import (
    DECISION_SCHEMA_VERSION,
    FORBIDDEN_DECISION_KEYS,
    INTENTS,
    DecisionToolError,
    artifact_content_sha256,
    decimal_value,
    decision_bundle_sha256,
    decision_rules_sha256,
    intent_artifact_id,
    parse_time,
    reject_unknown_fields,
    required_string,
    snapshot_execution_price,
    string_list,
    validate_forbidden_keys,
)
from .input_context import DecisionContext
from .input_validator import DecisionInputValidator

__all__ = [
    "DECISION_SCHEMA_VERSION",
    "FORBIDDEN_DECISION_KEYS",
    "INTENTS",
    "DecisionContext",
    "DecisionInputValidator",
    "DecisionToolError",
    "artifact_content_sha256",
    "canonical_sha256",
    "content_sha256",
    "decimal_value",
    "decision_bundle_sha256",
    "decision_rules_sha256",
    "intent_artifact_id",
    "parse_aware_time",
    "parse_decimal",
    "parse_time",
    "reject_unknown_fields",
    "required_string",
    "snapshot_execution_price",
    "string_list",
    "validate_forbidden_keys",
]
