"""Compact, read-only view of one Buy／Sell role input for LLM sub-agents."""

from __future__ import annotations

from typing import Dict, List, Mapping

from .contracts import DECISION_SCHEMA_VERSION, DecisionToolError, canonical_sha256


ROLE_BRIEF_SCHEMA_VERSION = "1.0"
_MOMENTUM_FIELDS = (
    "close",
    "return_5d",
    "return_20d",
    "return_60d",
    "above_ma20",
    "above_ma60",
    "atr_14_pct",
    "downside_volatility_20d",
    "average_traded_value_20d",
)
_REVENUE_FIELDS = ("revenue_period", "yoy_pct", "mom_pct", "cumulative_yoy_pct")
_RULE_FIELDS = (
    "min_positions",
    "max_positions",
    "max_stock_weight",
    "special_weight_limits",
    "cash_weight_must_be_below",
)


def build_role_brief(role_input: Mapping[str, object]) -> Dict[str, object]:
    """Summarise a role input without adding facts; packets still cite original IDs.

    完整 role input 內嵌 Snapshot 與全部 K 線，子 Agent 難以直接閱讀。摘要只重排
    同一份 role input 的既有欄位，不含對方 packet；Agent 仍須把
    ``packet_envelope`` 原樣填入 packet，並由既有 validator 對完整輸入驗證。
    """
    payload = dict(role_input)
    expected = payload.pop("role_input_sha256", None)
    if expected != canonical_sha256(payload):
        raise DecisionToolError("role_input_sha256 與角色輸入內容不一致")
    role = payload.get("role")
    if role not in {"buy", "sell"}:
        raise DecisionToolError("role 必須是 buy 或 sell")
    bundle = payload.get("decision_bundle")
    momentum = payload.get("momentum_result")
    if not isinstance(bundle, Mapping) or not isinstance(momentum, Mapping):
        raise DecisionToolError("角色輸入缺少 decision_bundle 或 momentum_result")
    snapshot = bundle.get("snapshot", {})
    account = bundle.get("account_snapshot", {})
    rules = bundle.get("rules", {})

    held = {
        str(item.get("symbol", "")).upper(): item
        for item in account.get("positions", [])
        if isinstance(item, Mapping)
    }
    momentum_by_symbol = {
        str(item.get("symbol", "")).upper(): item
        for item in momentum.get("items", [])
        if isinstance(item, Mapping)
    }
    tradability = {
        str(item.get("symbol", "")).upper(): item
        for item in (bundle.get("tradability_assessment") or {}).get("symbols", [])
        if isinstance(item, Mapping)
    }
    documents: Dict[str, List[Dict[str, object]]] = {}
    for document in snapshot.get("documents", []):
        if not isinstance(document, Mapping) or not document.get("symbol"):
            continue
        entry: Dict[str, object] = {
            "evidence_id": document.get("source_evidence_id"),
            "document_type": document.get("document_type"),
            "title": document.get("title"),
            "published_at": document.get("published_at"),
        }
        revenue = document.get("monthly_revenue")
        if isinstance(revenue, Mapping):
            entry["monthly_revenue"] = {field: revenue.get(field) for field in _REVENUE_FIELDS}
        documents.setdefault(str(document["symbol"]).upper(), []).append(entry)
    research: Dict[str, List[Dict[str, object]]] = {}
    for result in bundle.get("research_results", []):
        for item in result.get("items", []) if isinstance(result, Mapping) else []:
            if not isinstance(item, Mapping):
                continue
            research.setdefault(str(item.get("symbol", "")).upper(), []).append(
                {
                    "event_id": item.get("event_id"),
                    "direction": item.get("direction"),
                    "event_summary": item.get("event_summary"),
                    "impact_mechanism": item.get("impact_mechanism"),
                    "evidence_ids": item.get("evidence_ids", []),
                    "counter_evidence_ids": item.get("counter_evidence_ids", []),
                }
            )

    symbols: List[Dict[str, object]] = []
    for row in snapshot.get("latest_prices", []):
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol", "")).upper()
        item = momentum_by_symbol.get(symbol, {})
        status = tradability.get(symbol)
        position = held.get(symbol)
        symbols.append(
            {
                "symbol": symbol,
                "held_shares": position.get("shares") if position else 0,
                "tradability": (
                    {"state": status.get("state"), "reason_codes": status.get("reason_codes", [])}
                    if status
                    else None
                ),
                "momentum": {
                    "status": item.get("status"),
                    "evidence_ids": item.get("evidence_ids", []),
                    **{field: item.get(field) for field in _MOMENTUM_FIELDS if field in item},
                },
                "documents": documents.get(symbol, []),
                "research": research.get(symbol, []),
            }
        )

    brief: Dict[str, object] = {
        "schema_version": ROLE_BRIEF_SCHEMA_VERSION,
        "role": role,
        "role_input_sha256": expected,
        "packet_envelope": {
            "schema_version": DECISION_SCHEMA_VERSION,
            "role": role,
            "bundle_id": payload.get("decision_bundle_id"),
            "snapshot_id": bundle.get("snapshot_id"),
            "decision_cutoff": bundle.get("decision_cutoff"),
            "bundle_hash": payload.get("bundle_hash"),
            "momentum_result_id": payload.get("momentum_result_id"),
            "dependencies": {
                "decision_bundle_id": payload.get("decision_bundle_id"),
                "momentum_result_id": payload.get("momentum_result_id"),
                "role_input_sha256": expected,
                "peer_packet_ids": [],
            },
        },
        "regime_assessment": momentum.get("regime_assessment"),
        "momentum_status": momentum.get("status"),
        "momentum_errors": momentum.get("errors", []),
        "account": {
            "source_evidence_id": account.get("source_evidence_id"),
            "cash": account.get("cash"),
            "settled_cash": account.get("settled_cash"),
            "nav": account.get("nav"),
            "positions": [dict(item) for item in held.values()],
        },
        "rules": {field: rules.get(field) for field in _RULE_FIELDS},
        "tradability_status": (bundle.get("tradability_assessment") or {}).get("status"),
        "symbols": symbols,
    }
    brief["brief_sha256"] = canonical_sha256(brief)
    return brief
