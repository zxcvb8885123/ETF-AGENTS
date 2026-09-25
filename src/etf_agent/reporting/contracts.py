"""Research Report V0 的狀態、禁用欄位、免責聲明與欄位解析 helper。"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Mapping

from etf_agent.core import parse_aware_time


REPORT_STATUSES = {"completed", "degraded"}


UPSTREAM_STATUSES = {"completed", "degraded"}


FORBIDDEN_REPORT_FIELDS = {
    "decision",
    "decisions",
    "order",
    "orders",
    "target_weight",
    "target_weights",
    "shares",
}


DISCLAIMER = "本報告僅整理已驗證研究資料，不是投資建議，且不包含權重、股數或訂單。"


class ResearchReportError(ValueError):
    """Raised when a report would lose provenance or mix incompatible inputs."""


def _parse_time(value: object, field: str) -> datetime:
    return parse_aware_time(value, field, error=ResearchReportError)


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ResearchReportError("缺少必要字串欄位：%s" % field)
    return value.strip()


def _string_list(payload: Mapping[str, object], field: str) -> List[str]:
    value = payload.get(field)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ResearchReportError("%s 必須是字串陣列" % field)
    return [item.strip() for item in value]


def _mapping_list(payload: Mapping[str, object], field: str) -> List[Dict[str, object]]:
    value = payload.get(field)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ResearchReportError("%s 必須是物件陣列" % field)
    return [dict(item) for item in value]


def _same_cutoff(left: object, right: object) -> bool:
    return _parse_time(left, "decision_cutoff") == _parse_time(right, "decision_cutoff")
