"""回測用交易狀態包：沒有歷史處置／暫停資料時，明確標示為假設。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, Mapping, Tuple

from etf_agent.core import canonical_sha256
from etf_agent.data.trading_status import (
    DEFAULT_REQUIRED_CATEGORIES,
    TradingStatusBundleBuilder,
    TradingStatusRequest,
)

from .contracts import ASSUMED_STATUS_SOURCE_ID, SESSION_CLOSE, SESSION_OPEN, TAIPEI

ASSUMPTION_TEXT = (
    "回測期間沒有版本化的歷史處置、暫停、變更交易等資料；全部交易池視為可交易。"
    "此為回測假設，不是官方狀態查詢結果。"
)
CONFIG_VERSION = "backtest-assumed-status-v1"


def build_assumed_status(snapshot: Mapping[str, object], trade_day: date) -> Tuple[Dict[str, object], Dict[str, object]]:
    """目標時段固定為決策日當天 09:00–13:30；cutoff 為當日 08:55，早於開盤。"""
    start = datetime.combine(trade_day, SESSION_OPEN, TAIPEI).isoformat()
    end = datetime.combine(trade_day, SESSION_CLOSE, TAIPEI).isoformat()
    request = TradingStatusRequest.from_snapshot(snapshot, start, end, source_config_version=CONFIG_VERSION)
    cutoff = str(snapshot["decision_cutoff"])
    digest = canonical_sha256({"assumption": ASSUMPTION_TEXT, "version": CONFIG_VERSION})
    coverage = [
        {
            "source_id": ASSUMED_STATUS_SOURCE_ID,
            "market": market,
            "category": category,
            "approval_status": "approved",
            "coverage_status": "complete",
            "semantics": "backtest_assumption_all_allowed",
            "as_of": cutoff,
            "query_start": cutoff,
            "query_end": cutoff,
            "row_count": 0,
            "page_count": 1,
            "expected_page_count": 1,
            "raw_payload_id": None,
            "raw_payload_sha256": digest,
            "reason": ASSUMPTION_TEXT,
        }
        for market in ("TWSE", "TPEX")
        for category in DEFAULT_REQUIRED_CATEGORIES
    ]
    return TradingStatusBundleBuilder(request, [], coverage).build()
