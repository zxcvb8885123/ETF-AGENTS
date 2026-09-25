"""官方交易狀態的版本化契約、重建與可交易性判斷。

本模組刻意不呼叫模型或以行情存在推論可交易。來源 Provider 只負責交付
原始回應及欄位映射；``TradingStatusBundleBuilder`` 在固定 cutoff 與目標
交易時段下重建逐檔狀態，資料不完整時一律回傳 ``unknown``。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

from etf_agent.core import canonical_sha256, parse_aware_time
from .database import MarketDataDatabase
from .universe import normalize_symbol


TRADING_STATUS_SCHEMA_VERSION = "1.0"
TRADING_STATUS_PARSER_VERSION = "1.0"
TRADING_STATUS_STATES = {"allowed", "blocked", "unknown"}
TRADING_STATUS_COVERAGE = {"complete", "partial", "failed"}
TRADING_STATUS_APPROVAL = {"approved", "candidate", "rejected"}
DEFAULT_REQUIRED_CATEGORIES = (
    "trading_halt",
    "special_trading",
    "split_trading",
    "management",
    "disposition",
)
INFORMATIONAL_CATEGORIES = frozenset({"attention"})
LEGACY_REQUIRED_CATEGORIES = DEFAULT_REQUIRED_CATEGORIES[:-1] + ("attention", "disposition")
BLOCKING_CODES = {
    "halted",
    "suspended",
    "blocked",
    "restricted",
    "disposition",
    "special_trading",
    "split_trading",
    "management",
}
CLEAR_CODES = {"none", "clear", "active", "resumed", "cancelled", "resolved", "inactive"}


class TradingStatusError(ValueError):
    """交易狀態契約或時間點不安全。"""


def _parse_time(value: object, field: str) -> datetime:
    text = value.strip() if isinstance(value, str) else value
    return parse_aware_time(text, field, error=TradingStatusError)


def _optional_time(value: object, field: str) -> Optional[datetime]:
    if value is None or value == "":
        return None
    return _parse_time(value, field)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TradingStatusError("%s 必須是非空字串" % field)
    return value.strip()


def _market(value: object) -> str:
    normalized = _text(value, "market").upper()
    if normalized in {"上市", "TWSE"}:
        return "TWSE"
    if normalized in {"上櫃", "TPEX"}:
        return "TPEX"
    raise TradingStatusError("不支援的 market：%s" % value)


def _symbol(value: object) -> str:
    return _text(value, "symbol").upper()


@dataclass(frozen=True)
class TradingStatusRequest:
    request_id: str
    snapshot_id: str
    universe_version: str
    universe_symbols: Tuple[str, ...]
    decision_cutoff: str
    target_session_start: str
    target_session_end: str
    required_categories: Tuple[str, ...] = DEFAULT_REQUIRED_CATEGORIES
    source_config_version: str = "unconfigured"
    policy_version: str = "trading-status-policy-2"

    def as_dict(self) -> Dict[str, object]:
        return {
            "request_id": self.request_id,
            "snapshot_id": self.snapshot_id,
            "universe_version": self.universe_version,
            "universe_symbols": list(self.universe_symbols),
            "decision_cutoff": self.decision_cutoff,
            "target_session": {
                "start": self.target_session_start,
                "end": self.target_session_end,
            },
            "required_categories": list(self.required_categories),
            "source_config_version": self.source_config_version,
            "policy_version": self.policy_version,
        }

    def validate(self) -> List[str]:
        errors: List[str] = []
        for name, value in (
            ("request_id", self.request_id),
            ("snapshot_id", self.snapshot_id),
            ("universe_version", self.universe_version),
            ("source_config_version", self.source_config_version),
            ("policy_version", self.policy_version),
        ):
            if not isinstance(value, str) or not value.strip():
                errors.append("TradingStatusRequest.%s 缺少必要字串" % name)
        try:
            cutoff = _parse_time(self.decision_cutoff, "decision_cutoff")
            start = _parse_time(self.target_session_start, "target_session.start")
            end = _parse_time(self.target_session_end, "target_session.end")
            if start >= end:
                errors.append("target_session.start 必須早於 target_session.end")
            if start < cutoff:
                errors.append("target_session.start 不得早於 decision_cutoff")
        except TradingStatusError as error:
            errors.append(str(error))
        symbols = [str(item).upper() for item in self.universe_symbols]
        if not symbols or len(symbols) != len(set(symbols)):
            errors.append("universe_symbols 必須非空且不得重複")
        categories = [str(item) for item in self.required_categories]
        if not categories or len(categories) != len(set(categories)):
            errors.append("required_categories 必須非空且不得重複")
        return errors

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "TradingStatusRequest":
        session = payload.get("target_session")
        if not isinstance(session, Mapping):
            raise TradingStatusError("TradingStatusRequest 缺少 target_session")
        symbols = payload.get("universe_symbols")
        policy_version = payload.get("policy_version", "trading-status-policy-2")
        default_categories = (
            LEGACY_REQUIRED_CATEGORIES
            if policy_version == "trading-status-policy-1"
            else DEFAULT_REQUIRED_CATEGORIES
        )
        categories = payload.get("required_categories", list(default_categories))
        if not isinstance(symbols, list) or not isinstance(categories, list):
            raise TradingStatusError("universe_symbols 與 required_categories 必須是陣列")
        request = cls(
            request_id=_text(payload.get("request_id"), "request_id"),
            snapshot_id=_text(payload.get("snapshot_id"), "snapshot_id"),
            universe_version=_text(payload.get("universe_version"), "universe_version"),
            universe_symbols=tuple(_symbol(item) for item in symbols),
            decision_cutoff=_text(payload.get("decision_cutoff"), "decision_cutoff"),
            target_session_start=_text(session.get("start"), "target_session.start"),
            target_session_end=_text(session.get("end"), "target_session.end"),
            required_categories=tuple(_text(item, "required_categories") for item in categories),
            source_config_version=_text(
                payload.get("source_config_version", "unconfigured"),
                "source_config_version",
            ),
            policy_version=_text(policy_version, "policy_version"),
        )
        errors = request.validate()
        if errors:
            raise TradingStatusError("；".join(errors))
        return request


def _normalise_record(record: Mapping[str, object]) -> Dict[str, object]:
    result = {
        "record_id": _text(record.get("record_id"), "record_id"),
        "symbol": _symbol(record.get("symbol")),
        "market": _market(record.get("market")),
        "category": _text(record.get("category"), "category"),
        "status_code": _text(record.get("status_code"), "status_code").lower(),
        "effective_from": _text(record.get("effective_from"), "effective_from"),
        "effective_to": record.get("effective_to"),
        "published_at": record.get("published_at"),
        "available_at": _text(record.get("available_at"), "available_at"),
        "fetched_at": _text(record.get("fetched_at"), "fetched_at"),
        "source_id": _text(record.get("source_id"), "source_id"),
        "source_url": _text(record.get("source_url"), "source_url"),
        "raw_payload_id": record.get("raw_payload_id"),
        "raw_payload_sha256": _text(record.get("raw_payload_sha256"), "raw_payload_sha256"),
        "evidence_id": _text(record.get("evidence_id"), "evidence_id"),
        "content_sha256": _text(record.get("content_sha256"), "content_sha256"),
        "version": _text(record.get("version", "1"), "version"),
        "parser_version": _text(record.get("parser_version", TRADING_STATUS_PARSER_VERSION), "parser_version"),
    }
    for field in ("effective_from", "available_at", "fetched_at"):
        _parse_time(result[field], field)
    for field in ("effective_to", "published_at"):
        _optional_time(result[field], field)
    if result["raw_payload_id"] is not None and (
        not isinstance(result["raw_payload_id"], int) or isinstance(result["raw_payload_id"], bool)
    ):
        raise TradingStatusError("raw_payload_id 必須是整數或 null")
    expected_content_sha256 = canonical_sha256(
        {key: value for key, value in result.items() if key != "content_sha256"}
    )
    if result["content_sha256"] != expected_content_sha256:
        raise TradingStatusError("content_sha256 與交易狀態紀錄內容不一致")
    return result


def _normalise_coverage(coverage: Mapping[str, object]) -> Dict[str, object]:
    result = {
        "source_id": _text(coverage.get("source_id"), "source_id"),
        "market": _market(coverage.get("market")),
        "category": _text(coverage.get("category"), "category"),
        "approval_status": _text(coverage.get("approval_status"), "approval_status"),
        "coverage_status": _text(coverage.get("coverage_status"), "coverage_status"),
        "semantics": _text(coverage.get("semantics"), "semantics"),
        "as_of": _text(coverage.get("as_of"), "as_of"),
        "query_start": _text(coverage.get("query_start"), "query_start"),
        "query_end": _text(coverage.get("query_end"), "query_end"),
        "row_count": coverage.get("row_count"),
        "page_count": coverage.get("page_count"),
        "expected_page_count": coverage.get("expected_page_count"),
        "raw_payload_id": coverage.get("raw_payload_id"),
        "raw_payload_sha256": _text(coverage.get("raw_payload_sha256"), "raw_payload_sha256"),
        "reason": coverage.get("reason"),
    }
    if result["approval_status"] not in TRADING_STATUS_APPROVAL:
        raise TradingStatusError("不支援的 approval_status：%s" % result["approval_status"])
    if result["coverage_status"] not in TRADING_STATUS_COVERAGE:
        raise TradingStatusError("不支援的 coverage_status：%s" % result["coverage_status"])
    for field in ("as_of", "query_start", "query_end"):
        _parse_time(result[field], field)
    if _parse_time(result["query_start"], "query_start") > _parse_time(result["query_end"], "query_end"):
        raise TradingStatusError("query_start 不得晚於 query_end")
    for field in ("row_count", "page_count", "expected_page_count"):
        value = result[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TradingStatusError("%s 必須是非負整數" % field)
    if result["expected_page_count"] and result["page_count"] > result["expected_page_count"]:
        raise TradingStatusError("page_count 不得大於 expected_page_count")
    return result


def _interval_overlaps(record: Mapping[str, object], start: datetime, end: datetime) -> bool:
    effective_from = _parse_time(record["effective_from"], "effective_from")
    effective_to = _optional_time(record.get("effective_to"), "effective_to")
    if effective_to is not None and effective_to <= effective_from:
        raise TradingStatusError("effective_to 必須晚於 effective_from")
    return effective_from < end and (effective_to is None or effective_to > start)


class TradingStatusBundleBuilder:
    """在固定 request 下重建不可變 bundle 與逐檔 assessment。"""

    def __init__(
        self,
        request: TradingStatusRequest,
        records: Sequence[Mapping[str, object]],
        coverage: Sequence[Mapping[str, object]],
    ):
        self.request = request
        self.records = [_normalise_record(item) for item in records]
        self.coverage = [_normalise_coverage(item) for item in coverage]

    def build(self) -> Tuple[Dict[str, object], Dict[str, object]]:
        request_errors = self.request.validate()
        if request_errors:
            raise TradingStatusError("TradingStatusRequest 驗證失敗：" + "；".join(request_errors))
        cutoff = _parse_time(self.request.decision_cutoff, "decision_cutoff")
        session_start = _parse_time(self.request.target_session_start, "target_session.start")
        session_end = _parse_time(self.request.target_session_end, "target_session.end")
        known_symbols = set(self.request.universe_symbols)
        record_errors: List[str] = []
        evidence: Dict[str, Dict[str, object]] = {}
        record_ids: Set[str] = set()
        for index, record in enumerate(self.records):
            if record["symbol"] not in known_symbols:
                record_errors.append("records[%d] 股票不在固定交易池：%s" % (index, record["symbol"]))
            if _parse_time(record["available_at"], "available_at") > cutoff:
                record_errors.append("records[%d] available_at 晚於 decision_cutoff" % index)
            if _parse_time(record["fetched_at"], "fetched_at") > cutoff:
                record_errors.append("records[%d] fetched_at 晚於 decision_cutoff" % index)
            published = _optional_time(record.get("published_at"), "published_at")
            if published is not None and published > cutoff:
                record_errors.append("records[%d] published_at 晚於 decision_cutoff" % index)
            if record["evidence_id"] in evidence:
                record_errors.append("evidence_id 重複：%s" % record["evidence_id"])
            if record["record_id"] in record_ids:
                record_errors.append("record_id 重複：%s" % record["record_id"])
            record_ids.add(str(record["record_id"]))
            evidence[record["evidence_id"]] = {
                "evidence_id": record["evidence_id"],
                "source_id": record["source_id"],
                "source_url": record["source_url"],
                "available_at": record["available_at"],
                "published_at": record["published_at"],
                "fetched_at": record["fetched_at"],
                "raw_payload_id": record["raw_payload_id"],
                "raw_payload_sha256": record["raw_payload_sha256"],
                "content_sha256": record["content_sha256"],
            }
        if record_errors:
            raise TradingStatusError("；".join(record_errors))
        coverage_errors: List[str] = []
        coverage_by_key: Dict[Tuple[str, str], List[Dict[str, object]]] = {}
        coverage_degraded = False
        for index, item in enumerate(self.coverage):
            key = (str(item["market"]), str(item["category"]))
            coverage_by_key.setdefault(key, []).append(item)
            if item["category"] in self.request.required_categories or item["category"] not in INFORMATIONAL_CATEGORIES:
                if item["approval_status"] != "approved" or item["coverage_status"] != "complete":
                    coverage_degraded = True
            if _parse_time(item["as_of"], "as_of") > cutoff:
                coverage_errors.append("coverage[%d] as_of 晚於 decision_cutoff" % index)
            if _parse_time(item["query_end"], "query_end") > cutoff:
                coverage_errors.append("coverage[%d] query_end 晚於 decision_cutoff" % index)
        if coverage_errors:
            raise TradingStatusError("；".join(coverage_errors))

        assessments: List[Dict[str, object]] = []
        missing_coverage: List[str] = []
        for symbol in sorted(known_symbols):
            market = "TPEX" if symbol.endswith(".TWO") else "TWSE"
            symbol_records = [
                record
                for record in self.records
                if record["symbol"] == symbol
                and _interval_overlaps(record, session_start, session_end)
            ]
            states: List[str] = []
            restrictions: List[str] = []
            reasons: List[str] = []
            symbol_evidence: List[str] = []
            for category in self.request.required_categories:
                entries = coverage_by_key.get((market, category), [])
                if not entries:
                    missing_coverage.append("%s|%s" % (symbol, category))
                    states.append("unknown")
                    reasons.append("MISSING_COVERAGE:%s" % category)
                    continue
                if any(
                    item["coverage_status"] != "complete"
                    or item["approval_status"] != "approved"
                    for item in entries
                ):
                    states.append("unknown")
                    reasons.append("INCOMPLETE_COVERAGE:%s" % category)
                category_records = [item for item in symbol_records if item["category"] == category]
                active_codes = {str(item["status_code"]).lower() for item in category_records}
                if len(active_codes) > 1:
                    states.append("unknown")
                    reasons.append("CONFLICTING_STATUS:%s" % category)
                elif active_codes:
                    code = next(iter(active_codes))
                    if category == "attention" and code not in BLOCKING_CODES:
                        reasons.append("ATTENTION:%s" % code)
                    elif code in BLOCKING_CODES or (category != "attention" and code not in CLEAR_CODES):
                        states.append("blocked")
                        restrictions.append(category)
                        reasons.append("RESTRICTED:%s:%s" % (category, code))
                    elif code not in CLEAR_CODES:
                        states.append("unknown")
                        reasons.append("UNKNOWN_STATUS:%s:%s" % (category, code))
                    for item in category_records:
                        symbol_evidence.append(str(item["evidence_id"]))
            for category in INFORMATIONAL_CATEGORIES - set(self.request.required_categories):
                entries = coverage_by_key.get((market, category), [])
                if not entries or any(
                    item["approval_status"] != "approved" or item["coverage_status"] != "complete"
                    for item in entries
                ):
                    continue
                category_records = [item for item in symbol_records if item["category"] == category]
                active_codes = {str(item["status_code"]).lower() for item in category_records}
                if len(active_codes) == 1:
                    reasons.append("ATTENTION:%s" % next(iter(active_codes)))
                    symbol_evidence.extend(str(item["evidence_id"]) for item in category_records)
            if "unknown" in states:
                state = "unknown"
            elif "blocked" in states:
                state = "blocked"
            else:
                state = "allowed"
            assessments.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "state": state,
                    "restriction_categories": sorted(set(restrictions)),
                    "reason_codes": sorted(set(reasons)) or ["NO_ACTIVE_RESTRICTION"],
                    "evidence_ids": sorted(set(symbol_evidence)),
                }
            )
        bundle: Dict[str, object] = {
            "schema_version": TRADING_STATUS_SCHEMA_VERSION,
            "bundle_id": "",
            "snapshot_id": self.request.snapshot_id,
            "universe_version": self.request.universe_version,
            "decision_cutoff": self.request.decision_cutoff,
            "target_session": {
                "start": self.request.target_session_start,
                "end": self.request.target_session_end,
            },
            "request": self.request.as_dict(),
            "records": self.records,
            "source_coverage": self.coverage,
            "source_evidence": list(evidence.values()),
            "coverage_denominator": sorted(
                "%s|%s" % (symbol, category)
                for symbol in known_symbols
                for category in self.request.required_categories
            ),
            "coverage_gaps": sorted(set(missing_coverage)),
            "status": "completed" if not missing_coverage and not coverage_degraded else "degraded",
        }
        bundle_seed = {
            "request": bundle["request"],
            "records": bundle["records"],
            "source_coverage": bundle["source_coverage"],
        }
        bundle["bundle_id"] = "trading-status:" + canonical_sha256(bundle_seed)[:20]
        bundle["bundle_sha256"] = canonical_sha256(bundle)
        assessment = {
            "schema_version": TRADING_STATUS_SCHEMA_VERSION,
            "assessment_id": "",
            "bundle_id": bundle["bundle_id"],
            "snapshot_id": self.request.snapshot_id,
            "decision_cutoff": self.request.decision_cutoff,
            "target_session": bundle["target_session"],
            "policy_version": self.request.policy_version,
            "status": bundle["status"],
            "symbols": assessments,
            "coverage_gaps": sorted(set(missing_coverage)),
        }
        assessment["assessment_id"] = "tradability:" + canonical_sha256(
            {"bundle_id": bundle["bundle_id"], "symbols": assessments, "policy_version": self.request.policy_version}
        )[:20]
        assessment["assessment_sha256"] = canonical_sha256(assessment)
        return bundle, assessment


class TradingStatusBundleValidator:
    """不查詢最新資料，從 bundle 原內容重建並檢查雜湊與時間點。"""

    def validate(
        self, bundle: Mapping[str, object], assessment: Optional[Mapping[str, object]] = None
    ) -> List[str]:
        errors: List[str] = []
        try:
            bundle_allowed = {
                "schema_version", "bundle_id", "snapshot_id", "universe_version",
                "decision_cutoff", "target_session", "request", "records",
                "source_coverage", "source_evidence", "coverage_denominator",
                "coverage_gaps", "status", "bundle_sha256",
            }
            unknown_bundle = sorted(
                str(key) for key in bundle if str(key) not in bundle_allowed
            )
            if unknown_bundle:
                errors.append("TradingStatusBundle 含未允許欄位：%s" % ", ".join(unknown_bundle))
            request = TradingStatusRequest.from_dict(bundle.get("request", {}))
            records = bundle.get("records")
            coverage = bundle.get("source_coverage")
            if not isinstance(records, list) or not isinstance(coverage, list):
                raise TradingStatusError("records 與 source_coverage 必須是陣列")
            rebuilt_bundle, rebuilt_assessment = TradingStatusBundleBuilder(request, records, coverage).build()
            for field in (
                "schema_version", "snapshot_id", "universe_version", "decision_cutoff",
                "target_session", "request", "records", "source_coverage",
                "source_evidence", "coverage_denominator", "coverage_gaps", "status",
            ):
                if bundle.get(field) != rebuilt_bundle.get(field):
                    errors.append("TradingStatusBundle.%s 與確定性重建結果不一致" % field)
            if bundle.get("bundle_sha256") != canonical_sha256({k: v for k, v in bundle.items() if k != "bundle_sha256"}):
                errors.append("TradingStatusBundle.bundle_sha256 不一致")
            if assessment is not None:
                assessment_allowed = {
                    "schema_version", "assessment_id", "bundle_id", "snapshot_id",
                    "decision_cutoff", "target_session", "policy_version", "status",
                    "symbols", "coverage_gaps", "assessment_sha256",
                }
                if any(str(key) not in assessment_allowed for key in assessment):
                    errors.append("TradabilityAssessment 含未允許欄位")
                if dict(assessment) != rebuilt_assessment:
                    errors.append("TradabilityAssessment 與確定性重建結果不一致")
                if assessment.get("assessment_sha256") != canonical_sha256({k: v for k, v in assessment.items() if k != "assessment_sha256"}):
                    errors.append("TradabilityAssessment.assessment_sha256 不一致")
        except (TradingStatusError, TypeError, AttributeError) as error:
            errors.append("交易狀態驗證失敗：%s" % error)
        return errors


class TradingStatusProvider(Protocol):
    """來源邊界；Provider 不得回傳未保存的推論結果。"""

    source_id: str

    def fetch(self) -> Tuple[str, str]:
        """回傳原始 JSON 與帶時區的 fetched_at。"""


@dataclass(frozen=True)
class TradingStatusSourceDefinition:
    """單一官方狀態端點的明確欄位映射與空回應語意。"""

    source_id: str
    market: str
    category: str
    url: str
    approval_status: str
    semantics: str
    code_field: str
    status_code_field: str
    effective_from_field: str
    effective_to_field: Optional[str] = None
    published_at_field: Optional[str] = None
    rows_field: Optional[str] = None
    empty_response_complete: bool = False

    def validate(self) -> None:
        _text(self.source_id, "source_id")
        _market(self.market)
        _text(self.category, "category")
        _text(self.url, "url")
        if self.approval_status not in TRADING_STATUS_APPROVAL:
            raise TradingStatusError("不支援的 approval_status：%s" % self.approval_status)
        _text(self.semantics, "semantics")
        for field_name in ("code_field", "status_code_field", "effective_from_field"):
            _text(getattr(self, field_name), field_name)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "TradingStatusSourceDefinition":
        definition = cls(
            source_id=_text(value.get("source_id"), "source_id"),
            market=_market(value.get("market")),
            category=_text(value.get("category"), "category"),
            url=_text(value.get("url"), "url"),
            approval_status=_text(value.get("approval_status"), "approval_status"),
            semantics=_text(value.get("semantics"), "semantics"),
            code_field=_text(value.get("code_field"), "code_field"),
            status_code_field=_text(value.get("status_code_field"), "status_code_field"),
            effective_from_field=_text(value.get("effective_from_field"), "effective_from_field"),
            effective_to_field=(str(value["effective_to_field"]) if value.get("effective_to_field") else None),
            published_at_field=(str(value["published_at_field"]) if value.get("published_at_field") else None),
            rows_field=(str(value["rows_field"]) if value.get("rows_field") else None),
            empty_response_complete=bool(value.get("empty_response_complete", False)),
        )
        definition.validate()
        return definition


def parse_trading_status_payload(
    payload: str,
    definition: TradingStatusSourceDefinition,
    *,
    fetched_at: str,
    raw_payload_id: Optional[int],
    query_start: str,
    query_end: str,
    as_of: str,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    """把已保存 JSON 轉成狀態紀錄與來源覆蓋；不對空回應自行宣稱無限制。"""

    definition.validate()
    _parse_time(fetched_at, "fetched_at")
    _parse_time(query_start, "query_start")
    _parse_time(query_end, "query_end")
    _parse_time(as_of, "as_of")
    payload_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise TradingStatusError("來源回應不是有效 JSON") from error
    rows = decoded
    if definition.rows_field:
        if not isinstance(decoded, Mapping) or not isinstance(decoded.get(definition.rows_field), list):
            raise TradingStatusError("來源回應缺少 rows_field：%s" % definition.rows_field)
        rows = decoded[definition.rows_field]
    if not isinstance(rows, list) or any(not isinstance(item, Mapping) for item in rows):
        raise TradingStatusError("來源回應必須是物件陣列")
    if not rows and not definition.empty_response_complete:
        coverage_status = "failed"
        reason = "EMPTY_RESPONSE_SEMANTICS_UNKNOWN"
    else:
        coverage_status = "complete"
        reason = None
    records: List[Dict[str, object]] = []
    for index, row in enumerate(rows):
        try:
            code = _text(row.get(definition.code_field), definition.code_field)
            raw_symbol = normalize_symbol(code, definition.market)
            raw_status = _text(row.get(definition.status_code_field), definition.status_code_field).lower()
            effective_from = _text(row.get(definition.effective_from_field), definition.effective_from_field)
            _parse_time(effective_from, definition.effective_from_field)
            effective_to = row.get(definition.effective_to_field) if definition.effective_to_field else None
            if effective_to:
                _parse_time(effective_to, definition.effective_to_field or "effective_to")
            published = row.get(definition.published_at_field) if definition.published_at_field else None
        except TradingStatusError as error:
            raise TradingStatusError("來源第 %d 列：%s" % (index + 1, error)) from error
        row_hash = canonical_sha256(row)
        parsed_record = {
            "record_id": "%s:%s" % (definition.source_id, row_hash[:20]),
            "symbol": raw_symbol,
            "market": definition.market,
            "category": definition.category,
            "status_code": raw_status,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "published_at": published,
            "available_at": fetched_at,
            "fetched_at": fetched_at,
            "source_id": definition.source_id,
            "source_url": definition.url,
            "raw_payload_id": raw_payload_id,
            "raw_payload_sha256": payload_sha256,
            "evidence_id": "%s:evidence:%s" % (definition.source_id, row_hash[:20]),
            "content_sha256": "",
            "version": str(row.get("version", "1")),
            "parser_version": TRADING_STATUS_PARSER_VERSION,
        }
        parsed_record["content_sha256"] = canonical_sha256(
            {key: value for key, value in parsed_record.items() if key != "content_sha256"}
        )
        records.append(parsed_record)
    coverage = {
        "source_id": definition.source_id,
        "market": definition.market,
        "category": definition.category,
        "approval_status": definition.approval_status,
        "coverage_status": coverage_status,
        "semantics": definition.semantics,
        "as_of": as_of,
        "query_start": query_start,
        "query_end": query_end,
        "row_count": len(rows),
        "page_count": 1,
        "expected_page_count": 1,
        "raw_payload_id": raw_payload_id,
        "raw_payload_sha256": payload_sha256,
        "reason": reason,
    }
    return records, coverage


@dataclass(frozen=True)
class SavedJsonTradingStatusProvider:
    """讀取已保存的官方回應檔，供 CLI／重播使用；不宣稱檔案是真實即時資料。"""

    source_id: str
    path: Path
    fetched_at: str

    def fetch(self) -> Tuple[str, str]:
        _parse_time(self.fetched_at, "fetched_at")
        return self.path.read_text(encoding="utf-8"), self.fetched_at


@dataclass(frozen=True)
class TradingStatusCollectionResult:
    """一次交易狀態收集的可重建結果。"""

    run_ids: Tuple[str, ...]
    raw_payload_ids: Tuple[int, ...]
    bundle: Dict[str, object]
    assessment: Dict[str, object]


class TradingStatusCollector:
    """先保存原始回應，再解析並建立固定 cutoff 的狀態包。"""

    def __init__(self, database: MarketDataDatabase):
        self.database = database

    def collect(
        self,
        request: TradingStatusRequest,
        sources: Sequence[Tuple[TradingStatusSourceDefinition, TradingStatusProvider]],
    ) -> TradingStatusCollectionResult:
        request_errors = request.validate()
        if request_errors:
            raise TradingStatusError("TradingStatusRequest 驗證失敗：" + "；".join(request_errors))
        self.database.initialize()
        records: List[Dict[str, object]] = []
        coverage: List[Dict[str, object]] = []
        run_ids: List[str] = []
        payload_ids: List[int] = []
        for definition, provider in sources:
            if definition.source_id != provider.source_id:
                raise TradingStatusError("Provider source_id 與來源設定不一致")
            run_id = str(uuid.uuid4())
            run_ids.append(run_id)
            started_at = datetime.now(timezone.utc).isoformat()
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO collection_runs(run_id, source, started_at, status) VALUES (?, ?, ?, 'running')",
                    (run_id, definition.source_id, started_at),
                )
            try:
                payload, fetched_at = provider.fetch()
                if _parse_time(fetched_at, "fetched_at") > _parse_time(
                    request.decision_cutoff, "decision_cutoff"
                ):
                    raise TradingStatusError("Provider fetched_at 晚於 decision_cutoff")
                with self.database.connect() as connection:
                    raw_payload_id = self.database.insert_raw_payload(
                        connection,
                        run_id,
                        definition.source_id,
                        definition.url,
                        fetched_at,
                        payload,
                    )
                parsed_records, parsed_coverage = parse_trading_status_payload(
                    payload,
                    definition,
                    fetched_at=fetched_at,
                    raw_payload_id=raw_payload_id,
                    # 歷史重播若未提供來源查詢起點，只能使用 cutoff 作保守邊界；
                    # 不以目標時段起點（可能晚於 cutoff）偽造查詢區間。
                    query_start=request.decision_cutoff,
                    query_end=request.decision_cutoff,
                    as_of=request.decision_cutoff,
                )
                records.extend(parsed_records)
                coverage.append(parsed_coverage)
                payload_ids.append(raw_payload_id)
                with self.database.connect() as connection:
                    connection.execute(
                        """
                        UPDATE collection_runs SET finished_at = ?, status = 'success',
                        fetched_rows = ?, stored_rows = ? WHERE run_id = ?
                        """,
                        (datetime.now(timezone.utc).isoformat(), len(parsed_records), len(parsed_records), run_id),
                    )
            except Exception as error:
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE collection_runs SET finished_at = ?, status = 'failed', error_message = ? WHERE run_id = ?",
                        (datetime.now(timezone.utc).isoformat(), str(error), run_id),
                    )
                raise
        bundle, assessment = TradingStatusBundleBuilder(request, records, coverage).build()
        TradingStatusRepository().save(self.database, bundle)
        return TradingStatusCollectionResult(tuple(run_ids), tuple(payload_ids), bundle, assessment)


class TradingStatusRepository:
    """以 JSON 保存完整 bundle，SQLite 只保存可重建索引與原始回應關聯。"""

    def save(self, database: MarketDataDatabase, bundle: Mapping[str, object]) -> None:
        errors = TradingStatusBundleValidator().validate(bundle)
        if errors:
            raise TradingStatusError("不能保存未通過驗證的 bundle：" + "；".join(errors))
        database.initialize()
        with database.connect() as connection:
            connection.execute(
                """
                INSERT INTO trading_status_bundles(
                    bundle_id, snapshot_id, decision_cutoff, target_session_start,
                    target_session_end, status, content_sha256, bundle_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bundle_id) DO NOTHING
                """,
                (
                    bundle["bundle_id"], bundle["snapshot_id"], bundle["decision_cutoff"],
                    bundle["target_session"]["start"], bundle["target_session"]["end"],
                    bundle["status"], bundle["bundle_sha256"],
                    json.dumps(bundle, ensure_ascii=False, sort_keys=True),
                ),
            )
            row = connection.execute(
                "SELECT content_sha256 FROM trading_status_bundles WHERE bundle_id = ?",
                (bundle["bundle_id"],),
            ).fetchone()
            if row is None or row["content_sha256"] != bundle["bundle_sha256"]:
                raise TradingStatusError("既有交易狀態 bundle 內容不一致")

    def load(self, database: MarketDataDatabase, bundle_id: str) -> Optional[Dict[str, object]]:
        database.initialize()
        with database.connect() as connection:
            row = connection.execute(
                "SELECT bundle_json FROM trading_status_bundles WHERE bundle_id = ?",
                (bundle_id,),
            ).fetchone()
        return json.loads(row["bundle_json"]) if row else None
