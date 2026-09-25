"""唯讀 ResearchSnapshot 的已驗證財報，建立 FundamentalDataBundle。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.core import canonical_json, content_sha256
from .contracts import (
    BUNDLE_SCHEMA_VERSION,
    METRIC_KEYS,
    POLICY_VERSION,
    REQUIRED_STATEMENT_TYPES,
    SUPPORTED_INDUSTRIES,
    FundamentalToolError,
    _decimal,
    _parse_time,
    _period,
    _read_json,
    _required_string,
    _same_time,
    _string_list,
    _with_content_sha256,
)


class FundamentalSnapshotTools:
    """從一份不可變 ResearchSnapshot 建立基本面研究資料包。"""

    def __init__(self, snapshot: Mapping[str, object]):
        self.snapshot = dict(snapshot)
        self.snapshot_id = _required_string(self.snapshot, "snapshot_id")
        self.decision_cutoff = _required_string(self.snapshot, "decision_cutoff")
        self.cutoff = _parse_time(self.decision_cutoff, "decision_cutoff")
        if self.snapshot.get("usable") is not True:
            raise FundamentalToolError(
                "Snapshot 不可用：%s" % self.snapshot.get("quality_flags", [])
            )
        documents = self.snapshot.get("documents")
        evidence = self.snapshot.get("source_evidence")
        if not isinstance(documents, list):
            raise FundamentalToolError("Snapshot.documents 必須是陣列")
        if not isinstance(evidence, list) or not evidence:
            raise FundamentalToolError("Snapshot 缺少 source_evidence")
        self.documents = [dict(item) for item in documents if isinstance(item, Mapping)]
        if len(self.documents) != len(documents):
            raise FundamentalToolError("Snapshot.documents 含有非物件項目")
        self.evidence = [dict(item) for item in evidence if isinstance(item, Mapping)]
        if len(self.evidence) != len(evidence):
            raise FundamentalToolError("Snapshot.source_evidence 含有非物件項目")
        self.evidence_by_id: Dict[str, Dict[str, object]] = {}
        for index, item in enumerate(self.evidence):
            evidence_id = _required_string(item, "evidence_id")
            if evidence_id in self.evidence_by_id:
                raise FundamentalToolError(
                    "Snapshot.source_evidence[%d] evidence_id 重複" % index
                )
            self.evidence_by_id[evidence_id] = item
        self._financial_documents = [
            self._normalize_document(document, index)
            for index, document in enumerate(self.documents)
            if document.get("document_type") == "financial_statement"
        ]

    @classmethod
    def from_path(cls, path: Path) -> "FundamentalSnapshotTools":
        return cls(_read_json(path, "ResearchSnapshot"))

    def _normalize_document(
        self, document: Mapping[str, object], index: int
    ) -> Dict[str, object]:
        prefix = "documents[%d]" % index
        symbol = _required_string(document, "symbol").upper()
        evidence_id = _required_string(document, "source_evidence_id")
        linked = self.evidence_by_id.get(evidence_id)
        if linked is None:
            raise FundamentalToolError("%s 引用不存在的 source_evidence_id" % prefix)
        if linked.get("data_type") not in {None, "financial_statement"}:
            raise FundamentalToolError("%s 引用不是財報來源" % prefix)
        available = _parse_time(document.get("available_at"), "%s.available_at" % prefix)
        published = _parse_time(document.get("published_at"), "%s.published_at" % prefix)
        if published > available:
            raise FundamentalToolError("%s.published_at 不得晚於 available_at" % prefix)
        if available > self.cutoff:
            raise FundamentalToolError("Snapshot 包含 cutoff 後才可得的財報")
        statement = document.get("financial_statement")
        if not isinstance(statement, Mapping):
            raise FundamentalToolError("%s.financial_statement 必須是物件" % prefix)
        statement_type = _required_string(statement, "statement_type")
        if statement_type not in REQUIRED_STATEMENT_TYPES:
            raise FundamentalToolError("%s 不支援的 statement_type" % prefix)
        fiscal_year = statement.get("fiscal_year")
        fiscal_quarter = statement.get("fiscal_quarter")
        if not isinstance(fiscal_year, int) or fiscal_year < 1912:
            raise FundamentalToolError("%s.fiscal_year 無效" % prefix)
        if not isinstance(fiscal_quarter, int) or fiscal_quarter not in {1, 2, 3, 4}:
            raise FundamentalToolError("%s.fiscal_quarter 無效" % prefix)
        facts = statement.get("facts")
        if not isinstance(facts, Mapping):
            raise FundamentalToolError("%s.financial_statement.facts 必須是物件" % prefix)
        normalized_facts: Dict[str, Dict[str, object]] = {}
        for fact_key, fact in facts.items():
            if not isinstance(fact_key, str) or not fact_key or not isinstance(fact, Mapping):
                raise FundamentalToolError("%s.financial_statement.facts 格式錯誤" % prefix)
            status = fact.get("value_status")
            if status not in {"provided", "not_reported"}:
                raise FundamentalToolError("%s.%s.value_status 無效" % (prefix, fact_key))
            currency = _required_string(fact, "currency")
            multiplier = fact.get("unit_multiplier")
            if not isinstance(multiplier, int) or multiplier <= 0:
                raise FundamentalToolError("%s.%s.unit_multiplier 無效" % (prefix, fact_key))
            if status == "provided":
                if fact.get("source_field") is None:
                    raise FundamentalToolError("%s.%s 缺少 source_field" % (prefix, fact_key))
                _decimal(fact.get("value"), "%s.%s.value" % (prefix, fact_key))
            elif fact.get("value") is not None:
                raise FundamentalToolError("%s.%s not_reported 不得有 value" % (prefix, fact_key))
            normalized_facts[fact_key] = dict(fact)
        return {
            "document_id": document.get("document_id"),
            "symbol": symbol,
            "source": _required_string(document, "source"),
            "version": document.get("version"),
            "source_evidence_id": evidence_id,
            "content_sha256": _required_string(document, "content_sha256"),
            "published_at": str(document["published_at"]),
            "available_at": str(document["available_at"]),
            "statement_type": statement_type,
            "industry": _required_string(statement, "industry"),
            "fiscal_year": fiscal_year,
            "fiscal_quarter": fiscal_quarter,
            "period_start": statement.get("period_start"),
            "period_end": _required_string(statement, "period_end"),
            "period_kind": _required_string(statement, "period_kind"),
            "reporting_scope": _required_string(statement, "reporting_scope"),
            "currency": _required_string(statement, "currency"),
            "unit_multiplier": statement.get("unit_multiplier"),
            "reported_at": _required_string(statement, "reported_at"),
            "source_published_at": statement.get("source_published_at"),
            "mapping_version": _required_string(statement, "mapping_version"),
            "facts": normalized_facts,
        }

    def status(self) -> Dict[str, object]:
        symbols = sorted({str(item["symbol"]) for item in self._financial_documents})
        supported = sorted(
            {
                str(item["symbol"])
                for item in self._financial_documents
                if str(item["industry"]).lower() in SUPPORTED_INDUSTRIES
            }
        )
        return {
            "snapshot_id": self.snapshot_id,
            "decision_cutoff": self.decision_cutoff,
            "financial_document_count": len(self._financial_documents),
            "financial_symbol_count": len(symbols),
            "supported_general_industry_symbols": supported,
            "unsupported_industries": sorted(
                {
                    str(item["industry"])
                    for item in self._financial_documents
                    if str(item["industry"]).lower() not in SUPPORTED_INDUSTRIES
                }
            ),
        }

    def build_bundle(
        self,
        request: Mapping[str, object],
        *,
        bundle_id: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> Dict[str, object]:
        normalized_request = self._normalize_request(request)
        bundle_id = bundle_id or str(uuid.uuid4())
        if not isinstance(bundle_id, str) or not bundle_id.strip():
            raise FundamentalToolError("bundle_id 不得為空")
        generated_at = generated_at or datetime.now(timezone.utc).isoformat()
        if _parse_time(generated_at, "generated_at") < self.cutoff:
            raise FundamentalToolError("generated_at 不得早於 decision_cutoff")

        companies: List[Dict[str, object]] = []
        selected_evidence: Dict[str, Dict[str, object]] = {}
        for symbol in normalized_request["symbols"]:
            company = self._build_company_bundle(
                symbol,
                normalized_request["fiscal_year"],
                normalized_request["fiscal_quarter"],
                normalized_request["metric_keys"],
            )
            companies.append(company)
            for document in company["statements"]:
                evidence_id = str(document["source_evidence_id"])
                selected_evidence[evidence_id] = self.evidence_by_id[evidence_id]

        statuses = {str(company["status"]) for company in companies}
        status = (
            "completed"
            if statuses == {"completed"}
            else "unavailable"
            if statuses == {"unavailable"}
            else "degraded"
        )
        payload: Dict[str, object] = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "bundle_id": bundle_id.strip(),
            "snapshot_id": self.snapshot_id,
            "snapshot_sha256": content_sha256(self.snapshot),
            "decision_cutoff": self.decision_cutoff,
            "generated_at": generated_at,
            "status": status,
            "request": normalized_request,
            "coverage": {
                "requested_symbol_count": len(normalized_request["symbols"]),
                "completed_symbol_count": sum(
                    1 for company in companies if company["status"] == "completed"
                ),
                "degraded_symbol_count": sum(
                    1 for company in companies if company["status"] == "degraded"
                ),
                "unavailable_symbol_count": sum(
                    1 for company in companies if company["status"] == "unavailable"
                ),
            },
            "source_evidence": [
                selected_evidence[key] for key in sorted(selected_evidence)
            ],
            "companies": companies,
        }
        return _with_content_sha256(payload)

    def validate_bundle(self, payload: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        try:
            if payload.get("schema_version") != BUNDLE_SCHEMA_VERSION:
                raise FundamentalToolError("FundamentalDataBundle schema_version 必須為 1.0")
            if payload.get("snapshot_id") != self.snapshot_id:
                raise FundamentalToolError("FundamentalDataBundle.snapshot_id 與 Snapshot 不一致")
            if not _same_time(
                payload.get("decision_cutoff"), self.decision_cutoff, "decision_cutoff"
            ):
                raise FundamentalToolError(
                    "FundamentalDataBundle.decision_cutoff 與 Snapshot 不一致"
                )
            if payload.get("snapshot_sha256") != content_sha256(self.snapshot):
                raise FundamentalToolError("FundamentalDataBundle.snapshot_sha256 不一致")
            bundle_id = _required_string(payload, "bundle_id")
            generated_at = _required_string(payload, "generated_at")
            request = payload.get("request")
            if not isinstance(request, Mapping):
                raise FundamentalToolError("FundamentalDataBundle.request 必須是物件")
            expected = self.build_bundle(
                request,
                bundle_id=bundle_id,
                generated_at=generated_at,
            )
            if canonical_json(dict(payload)) != canonical_json(expected):
                errors.append("FundamentalDataBundle 未通過確定性重建")
            supplied_hash = payload.get("content_sha256")
            if supplied_hash != content_sha256(payload):
                errors.append("FundamentalDataBundle.content_sha256 不一致")
        except FundamentalToolError as error:
            errors.append(str(error))
        return errors

    def _normalize_request(self, request: Mapping[str, object]) -> Dict[str, object]:
        request_id = _required_string(request, "request_id")
        symbols = _string_list(request, "symbols")
        normalized_symbols = [symbol.upper() for symbol in symbols]
        if normalized_symbols != sorted(set(normalized_symbols)):
            raise FundamentalToolError("request.symbols 必須去重且依字母排序")
        fiscal_year = request.get("fiscal_year")
        fiscal_quarter = request.get("fiscal_quarter")
        if not isinstance(fiscal_year, int) or fiscal_year < 1912:
            raise FundamentalToolError("request.fiscal_year 必須是西元年度")
        if not isinstance(fiscal_quarter, int) or fiscal_quarter not in {1, 2, 3, 4}:
            raise FundamentalToolError("request.fiscal_quarter 必須介於 1 與 4")
        statement_types = _string_list(request, "required_statement_types")
        if tuple(statement_types) != REQUIRED_STATEMENT_TYPES:
            raise FundamentalToolError("request.required_statement_types 必須是損益表與資產負債表")
        metric_keys = _string_list(request, "metric_keys")
        if metric_keys != sorted(set(metric_keys)):
            raise FundamentalToolError("request.metric_keys 必須去重且依字母排序")
        unknown = set(metric_keys) - set(METRIC_KEYS)
        if unknown:
            raise FundamentalToolError("request.metric_keys 含不支援指標：%s" % ", ".join(sorted(unknown)))
        if request.get("policy_version") != POLICY_VERSION:
            raise FundamentalToolError("request.policy_version 不支援")
        return {
            "request_id": request_id,
            "symbols": normalized_symbols,
            "fiscal_year": fiscal_year,
            "fiscal_quarter": fiscal_quarter,
            "required_statement_types": list(REQUIRED_STATEMENT_TYPES),
            "metric_keys": metric_keys,
            "policy_version": POLICY_VERSION,
        }

    def _build_company_bundle(
        self,
        symbol: str,
        fiscal_year: int,
        fiscal_quarter: int,
        metric_keys: Sequence[str],
    ) -> Dict[str, object]:
        current = self._statement_map(symbol, fiscal_year, fiscal_quarter)
        prior = self._statement_map(symbol, fiscal_year - 1, fiscal_quarter)
        missing: List[Dict[str, object]] = []
        statements: List[Dict[str, object]] = []
        for scope, records in (("current", current), ("prior_year", prior)):
            for statement_type, record in records.items():
                if record is not None:
                    copy = dict(record)
                    copy["scope"] = scope
                    statements.append(copy)

        current_missing = False
        for statement_type in REQUIRED_STATEMENT_TYPES:
            current_record = current[statement_type]
            if current_record is None:
                current_missing = True
                missing.append(
                    {
                        "scope": "current",
                        "statement_type": statement_type,
                        "reason_code": "CURRENT_STATEMENT_MISSING",
                    }
                )

        industries = {
            str(record["industry"]).lower()
            for record in current.values()
            if record is not None
        }
        unsupported = sorted(industries - SUPPORTED_INDUSTRIES)
        if unsupported:
            missing.append(
                {
                    "scope": "current",
                    "statement_type": "all",
                    "reason_code": "UNSUPPORTED_INDUSTRY_%s" % "_".join(unsupported),
                }
            )

        needs_prior_income = any(
            key in {"revenue_yoy_pct", "net_income_yoy_pct", "operating_margin_pp_yoy"}
            for key in metric_keys
        )
        if needs_prior_income and prior["income_statement"] is None:
            missing.append(
                {
                    "scope": "prior_year",
                    "statement_type": "income_statement",
                    "reason_code": "COMPARISON_STATEMENT_MISSING",
                }
            )
        if current_missing or unsupported:
            status = "unavailable"
        elif missing:
            status = "degraded"
        else:
            status = "completed"
        statements.sort(
            key=lambda item: (
                str(item["scope"]),
                str(item["statement_type"]),
                int(item["document_id"]) if isinstance(item.get("document_id"), int) else -1,
            )
        )
        return {
            "symbol": symbol,
            "status": status,
            "period": _period(fiscal_year, fiscal_quarter),
            "comparison_period": _period(fiscal_year - 1, fiscal_quarter),
            "statements": statements,
            "missing_data": missing,
        }

    def _statement_map(
        self, symbol: str, fiscal_year: int, fiscal_quarter: int
    ) -> Dict[str, Optional[Dict[str, object]]]:
        result: Dict[str, Optional[Dict[str, object]]] = {
            statement_type: None for statement_type in REQUIRED_STATEMENT_TYPES
        }
        for statement_type in REQUIRED_STATEMENT_TYPES:
            matches = [
                item
                for item in self._financial_documents
                if item["symbol"] == symbol
                and item["statement_type"] == statement_type
                and item["fiscal_year"] == fiscal_year
                and item["fiscal_quarter"] == fiscal_quarter
            ]
            if len(matches) > 1:
                raise FundamentalToolError(
                    "%s %s %s 有多份財報，不能依來源順序選擇"
                    % (symbol, _period(fiscal_year, fiscal_quarter), statement_type)
                )
            if matches:
                result[statement_type] = matches[0]
        return result
