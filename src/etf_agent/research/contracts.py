"""Event Research Agent 的列舉、錯誤型別與欄位解析 helper。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Mapping

from etf_agent.core import parse_aware_time, parse_decimal


DIRECTIONS = {"positive", "negative", "mixed", "neutral", "uncertain"}


RESEARCH_STATUSES = {"candidate", "pending", "excluded"}


RESULT_STATUSES = {"completed", "degraded", "failed"}


EVIDENCE_QUALITY = {"verified", "partial", "insufficient"}


NOVELTY_CLASSES = {"new", "update", "repeat", "correction", "unclear"}


MATERIALITY_LEVELS = {"high", "medium", "low", "unknown"}


HORIZON_STATES = {"within_competition", "after_competition", "unknown"}


DEBATE_OUTCOMES = {"bull", "bear", "balanced", "indeterminate"}


PRICE_RESPONSE_STATES = {"confirmed", "unconfirmed", "contradicted", "unavailable"}


SUBAGENT_ROLES = {"fact", "bull", "bear", "adjudicator"}


EVENT_TYPES = {
    "monthly_revenue",
    "earnings",
    "guidance",
    "material_event",
    "corporate_action",
    "contract",
    "m_and_a",
    "management",
    "product",
    "regulatory",
    "industry",
    "macro",
    "other",
}


class ResearchToolError(ValueError):
    """Raised when a research tool cannot safely answer the request."""


def _parse_time(value: object, field: str) -> datetime:
    return parse_aware_time(value, field, error=ResearchToolError)


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ResearchToolError("缺少必要字串欄位：%s" % field)
    return value.strip()


def _string_list(payload: Mapping[str, object], field: str) -> List[str]:
    value = payload.get(field)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ResearchToolError("%s 必須是非空字串陣列" % field)
    return [item.strip() for item in value]


def _decimal(value: object) -> Decimal:
    return parse_decimal(value, "數值", error=ResearchToolError, parse_message="無法解析數值：%(value)s")
