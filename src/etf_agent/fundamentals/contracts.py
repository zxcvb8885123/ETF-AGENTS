"""基本面研究的版本、列舉、錯誤型別與欄位解析 helper。"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping

from etf_agent.core import content_sha256, parse_aware_time, parse_decimal


BUNDLE_SCHEMA_VERSION = "1.0"


METRICS_SCHEMA_VERSION = "1.0"


RESULT_SCHEMA_VERSION = "1.0"


FORMULA_VERSION = "1.0"


POLICY_VERSION = "1.0"


SUPPORTED_INDUSTRIES = {"ci", "mim"}


REQUIRED_STATEMENT_TYPES = ("income_statement", "balance_sheet")


METRIC_KEYS = (
    "operating_margin_pct",
    "liabilities_to_assets_pct",
    "revenue_yoy_pct",
    "net_income_yoy_pct",
    "operating_margin_pp_yoy",
)


RESEARCH_STATUSES = {"completed", "degraded", "unavailable"}


RESULT_STATUSES = {"completed", "degraded", "unavailable"}


FORBIDDEN_RESULT_FIELDS = {
    "allocation",
    "allocations",
    "cash_target",
    "order",
    "orders",
    "position_size",
    "shares",
    "target_price",
    "target_weight",
    "trade",
    "trades",
    "weight",
    "weights",
}


class FundamentalToolError(ValueError):
    """Raised when a fundamental-research artifact is unsafe to use."""


def _with_content_sha256(payload: Mapping[str, object]) -> Dict[str, object]:
    result = dict(payload)
    result["content_sha256"] = content_sha256(result)
    return result


def _parse_time(value: object, field: str) -> datetime:
    return parse_aware_time(value, field, error=FundamentalToolError)


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise FundamentalToolError("缺少必要字串欄位：%s" % field)
    return value.strip()


def _string_list(payload: Mapping[str, object], field: str) -> List[str]:
    value = payload.get(field)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise FundamentalToolError("%s 必須是非空字串陣列" % field)
    return [item.strip() for item in value]


def _optional_string_list(payload: Mapping[str, object], field: str) -> List[str]:
    value = payload.get(field)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise FundamentalToolError("%s 必須是字串陣列" % field)
    return [item.strip() for item in value]


def _decimal(value: object, field: str) -> Decimal:
    return parse_decimal(value, field, error=FundamentalToolError)


def _same_time(left: object, right: object, label: str) -> bool:
    return _parse_time(left, label) == _parse_time(right, label)


def _period(year: int, quarter: int) -> str:
    return "%dQ%d" % (year, quarter)


def _read_json(path: Path, label: str) -> Dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FundamentalToolError("無法讀取 %s：%s" % (label, error)) from error
    if not isinstance(payload, Mapping):
        raise FundamentalToolError("%s 必須是 JSON 物件" % label)
    return dict(payload)
