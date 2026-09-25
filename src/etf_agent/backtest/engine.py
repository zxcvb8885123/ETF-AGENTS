"""Point-in-time fixture provider for historical replay."""

from __future__ import annotations

from copy import deepcopy
from typing import Dict, Mapping, Sequence

from etf_agent.core import canonical_sha256

from .contracts import BacktestToolError, parse_time


class FixturePointInTimeDataProvider:
    """Select fixture versions solely by available_at and reject future data."""

    def __init__(self, versions: Sequence[Mapping[str, object]], require_provenance: bool = False):
        self.versions = [dict(item) for item in versions]
        self.require_provenance = require_provenance

    def at(self, decision_cutoff: str) -> Dict[str, object]:
        cutoff = parse_time(decision_cutoff, "decision_cutoff")
        eligible = []
        for item in self.versions:
            for field in ("artifact_id", "available_at", "content_sha256", "payload"):
                if field not in item:
                    raise BacktestToolError("時間點資料缺少 %s" % field)
            if self.require_provenance:
                for field in ("evidence_id", "source", "content_as_of", "published_at", "fetched_at"):
                    if not isinstance(item.get(field), str) or not item[field].strip():
                        raise BacktestToolError("正式歷史資料缺少來源欄位：%s" % field)
                for field in ("content_as_of", "published_at", "fetched_at"):
                    parse_time(item[field], "provider." + field)
            available = parse_time(item["available_at"], "provider.available_at")
            if item["content_sha256"] != canonical_sha256(item["payload"]):
                raise BacktestToolError("時間點資料內容雜湊不一致")
            if available <= cutoff:
                eligible.append((available, item))
        if not eligible:
            raise BacktestToolError("decision_cutoff 前沒有可用資料版本")
        eligible.sort(key=lambda item: (item[0], str(item[1]["artifact_id"])))
        return deepcopy(eligible[-1][1])
