"""市場情緒的列舉、錯誤型別與欄位解析 helper。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Mapping

from etf_agent.core import parse_aware_time


RESULT_STATUSES = {"completed", "degraded", "failed"}


RESEARCH_STATUSES = {"usable_secondary", "pending", "unavailable", "excluded"}


SENTIMENT_STANCES = {"positive", "negative", "neutral", "mixed"}


RELEVANCE_STATES = {"relevant", "ambiguous", "irrelevant"}


SENTIMENT_DIRECTIONS = SENTIMENT_STANCES | {"insufficient", "unavailable"}


CHANNEL_STATES = {"available", "insufficient", "unavailable"}


LICENSE_STATES = {"approved", "restricted", "unknown"}


SOURCE_CHANNELS = {"sentiment"}


PRICED_IN_STATES = {
    "underappreciated",
    "aligned",
    "crowded",
    "contradicted",
    "unknown",
}


class PerceptionToolError(ValueError):
    """Raised when perception data cannot be used safely."""


def _parse_time(value: object, field: str) -> datetime:
    return parse_aware_time(value, field, error=PerceptionToolError)


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PerceptionToolError("缺少必要字串欄位：%s" % field)
    return value.strip()


def _string_list(payload: Mapping[str, object], field: str) -> List[str]:
    value = payload.get(field)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise PerceptionToolError("%s 必須是字串陣列" % field)
    return [item.strip() for item in value]
