"""基本面研究的唯讀資料包、可重算指標與結果驗證。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core import canonical_json, content_sha256, decimal_string, parse_aware_time, parse_decimal


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


class FundamentalMetricsCalculator:
    """從已驗證的 FundamentalDataBundle 重算所有數值。"""

    def __init__(self, tools: FundamentalSnapshotTools, bundle: Mapping[str, object]):
        self.tools = tools
        self.bundle = dict(bundle)
        errors = tools.validate_bundle(self.bundle)
        if errors:
            raise FundamentalToolError("FundamentalDataBundle 無效：%s" % "; ".join(errors))
        self.bundle_id = _required_string(self.bundle, "bundle_id")
        self.bundle_sha256 = _required_string(self.bundle, "content_sha256")
        self.request = dict(self.bundle["request"])
        self.cutoff = _parse_time(self.bundle["decision_cutoff"], "decision_cutoff")

    def compute(
        self,
        *,
        metrics_id: Optional[str] = None,
        computed_at: Optional[str] = None,
    ) -> Dict[str, object]:
        metrics_id = metrics_id or str(uuid.uuid4())
        if not isinstance(metrics_id, str) or not metrics_id.strip():
            raise FundamentalToolError("metrics_id 不得為空")
        computed_at = computed_at or datetime.now(timezone.utc).isoformat()
        if _parse_time(computed_at, "computed_at") < self.cutoff:
            raise FundamentalToolError("computed_at 不得早於 decision_cutoff")
        items = [self._compute_company(dict(company)) for company in self.bundle["companies"]]
        statuses = {str(item["status"]) for item in items}
        status = (
            "completed"
            if statuses == {"completed"}
            else "unavailable"
            if statuses == {"unavailable"}
            else "degraded"
        )
        payload: Dict[str, object] = {
            "schema_version": METRICS_SCHEMA_VERSION,
            "metrics_id": metrics_id.strip(),
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.bundle_sha256,
            "snapshot_id": self.bundle["snapshot_id"],
            "decision_cutoff": self.bundle["decision_cutoff"],
            "computed_at": computed_at,
            "formula_version": FORMULA_VERSION,
            "status": status,
            "items": items,
        }
        return _with_content_sha256(payload)

    def validate(self, payload: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        try:
            if payload.get("schema_version") != METRICS_SCHEMA_VERSION:
                raise FundamentalToolError("FundamentalMetrics schema_version 必須為 1.0")
            if payload.get("bundle_id") != self.bundle_id:
                raise FundamentalToolError("FundamentalMetrics.bundle_id 不一致")
            if payload.get("bundle_sha256") != self.bundle_sha256:
                raise FundamentalToolError("FundamentalMetrics.bundle_sha256 不一致")
            if payload.get("snapshot_id") != self.bundle.get("snapshot_id"):
                raise FundamentalToolError("FundamentalMetrics.snapshot_id 不一致")
            if not _same_time(
                payload.get("decision_cutoff"), self.bundle.get("decision_cutoff"), "decision_cutoff"
            ):
                raise FundamentalToolError("FundamentalMetrics.decision_cutoff 不一致")
            if payload.get("formula_version") != FORMULA_VERSION:
                raise FundamentalToolError("FundamentalMetrics.formula_version 不支援")
            expected = self.compute(
                metrics_id=_required_string(payload, "metrics_id"),
                computed_at=_required_string(payload, "computed_at"),
            )
            if canonical_json(dict(payload)) != canonical_json(expected):
                errors.append("FundamentalMetrics 未通過確定性重算")
            if payload.get("content_sha256") != content_sha256(payload):
                errors.append("FundamentalMetrics.content_sha256 不一致")
        except FundamentalToolError as error:
            errors.append(str(error))
        return errors

    def _compute_company(self, company: Mapping[str, object]) -> Dict[str, object]:
        symbol = _required_string(company, "symbol")
        statements = company.get("statements")
        if not isinstance(statements, list):
            raise FundamentalToolError("Bundle 公司 statements 必須是陣列")
        statement_map: Dict[Tuple[str, str], Dict[str, object]] = {}
        for statement in statements:
            if not isinstance(statement, Mapping):
                raise FundamentalToolError("Bundle statements 必須是物件")
            key = (str(statement.get("scope")), str(statement.get("statement_type")))
            if key in statement_map:
                raise FundamentalToolError("Bundle 同公司同範圍財報重複")
            statement_map[key] = dict(statement)
        current_income = statement_map.get(("current", "income_statement"))
        current_balance = statement_map.get(("current", "balance_sheet"))
        prior_income = statement_map.get(("prior_year", "income_statement"))
        metric_items = [
            self._ratio_metric(
                symbol,
                "operating_margin_pct",
                current_income,
                "operating_profit",
                "revenue",
                "percent",
                company,
            ),
            self._ratio_metric(
                symbol,
                "liabilities_to_assets_pct",
                current_balance,
                "total_liabilities",
                "total_assets",
                "percent",
                company,
            ),
            self._yoy_metric(symbol, "revenue_yoy_pct", current_income, prior_income, "revenue", company),
            self._yoy_metric(symbol, "net_income_yoy_pct", current_income, prior_income, "net_income", company),
            self._margin_pp_yoy(symbol, current_income, prior_income, company),
        ]
        required = set(self.request["metric_keys"])
        metric_items = [item for item in metric_items if item["metric_key"] in required]
        available_count = sum(1 for item in metric_items if item["status"] == "available")
        unavailable_count = len(metric_items) - available_count
        if available_count == 0:
            status = "unavailable"
        elif company["status"] == "completed" and unavailable_count == 0:
            status = "completed"
        else:
            status = "degraded"
        return {
            "symbol": symbol,
            "status": status,
            "period": company["period"],
            "comparison_period": company["comparison_period"],
            "metrics": metric_items,
            "missing_data": list(company["missing_data"]),
        }

    def _ratio_metric(
        self,
        symbol: str,
        metric_key: str,
        statement: Optional[Mapping[str, object]],
        numerator_key: str,
        denominator_key: str,
        unit: str,
        company: Mapping[str, object],
    ) -> Dict[str, object]:
        numerator = self._fact(statement, numerator_key)
        denominator = self._fact(statement, denominator_key)
        if numerator is None or denominator is None:
            return self._unavailable_metric(
                symbol, metric_key, unit, company, "REQUIRED_FACT_NOT_REPORTED"
            )
        if numerator["currency"] != denominator["currency"]:
            return self._unavailable_metric(symbol, metric_key, unit, company, "CURRENCY_MISMATCH")
        denominator_value = denominator["absolute_value"]
        if denominator_value <= 0:
            return self._unavailable_metric(symbol, metric_key, unit, company, "NONPOSITIVE_DENOMINATOR")
        result = numerator["absolute_value"] / denominator_value * Decimal("100")
        return self._available_metric(
            symbol,
            metric_key,
            unit,
            company,
            result,
            [numerator, denominator],
            method="ratio_v1",
        )

    def _yoy_metric(
        self,
        symbol: str,
        metric_key: str,
        current: Optional[Mapping[str, object]],
        prior: Optional[Mapping[str, object]],
        fact_key: str,
        company: Mapping[str, object],
    ) -> Dict[str, object]:
        current_fact = self._fact(current, fact_key)
        prior_fact = self._fact(prior, fact_key)
        if current_fact is None or prior_fact is None:
            return self._unavailable_metric(
                symbol, metric_key, "percent", company, "COMPARABLE_FACT_NOT_REPORTED"
            )
        if current_fact["currency"] != prior_fact["currency"]:
            return self._unavailable_metric(symbol, metric_key, "percent", company, "CURRENCY_MISMATCH")
        if prior_fact["absolute_value"] <= 0:
            return self._unavailable_metric(
                symbol, metric_key, "percent", company, "NONPOSITIVE_COMPARISON_BASE"
            )
        result = (
            (current_fact["absolute_value"] - prior_fact["absolute_value"])
            / prior_fact["absolute_value"]
            * Decimal("100")
        )
        return self._available_metric(
            symbol,
            metric_key,
            "percent",
            company,
            result,
            [current_fact, prior_fact],
            method="same_quarter_yoy_v1",
        )

    def _margin_pp_yoy(
        self,
        symbol: str,
        current: Optional[Mapping[str, object]],
        prior: Optional[Mapping[str, object]],
        company: Mapping[str, object],
    ) -> Dict[str, object]:
        current_profit = self._fact(current, "operating_profit")
        current_revenue = self._fact(current, "revenue")
        prior_profit = self._fact(prior, "operating_profit")
        prior_revenue = self._fact(prior, "revenue")
        facts = [current_profit, current_revenue, prior_profit, prior_revenue]
        if any(item is None for item in facts):
            return self._unavailable_metric(
                symbol,
                "operating_margin_pp_yoy",
                "percentage_points",
                company,
                "COMPARABLE_FACT_NOT_REPORTED",
            )
        typed_facts = [item for item in facts if item is not None]
        currencies = {str(item["currency"]) for item in typed_facts}
        if len(currencies) != 1:
            return self._unavailable_metric(
                symbol, "operating_margin_pp_yoy", "percentage_points", company, "CURRENCY_MISMATCH"
            )
        if current_revenue["absolute_value"] <= 0 or prior_revenue["absolute_value"] <= 0:
            return self._unavailable_metric(
                symbol,
                "operating_margin_pp_yoy",
                "percentage_points",
                company,
                "NONPOSITIVE_DENOMINATOR",
            )
        current_margin = current_profit["absolute_value"] / current_revenue["absolute_value"] * Decimal("100")
        prior_margin = prior_profit["absolute_value"] / prior_revenue["absolute_value"] * Decimal("100")
        return self._available_metric(
            symbol,
            "operating_margin_pp_yoy",
            "percentage_points",
            company,
            current_margin - prior_margin,
            typed_facts,
            method="operating_margin_percentage_point_change_v1",
        )

    @staticmethod
    def _fact(
        statement: Optional[Mapping[str, object]], fact_key: str
    ) -> Optional[Dict[str, object]]:
        if statement is None:
            return None
        facts = statement.get("facts")
        if not isinstance(facts, Mapping):
            raise FundamentalToolError("財報 facts 必須是物件")
        fact = facts.get(fact_key)
        if not isinstance(fact, Mapping) or fact.get("value_status") != "provided":
            return None
        value = _decimal(fact.get("value"), "%s.value" % fact_key)
        multiplier = fact.get("unit_multiplier")
        if not isinstance(multiplier, int) or multiplier <= 0:
            raise FundamentalToolError("%s.unit_multiplier 無效" % fact_key)
        document_id = statement.get("document_id")
        if not isinstance(document_id, int) or document_id <= 0:
            raise FundamentalToolError("財報 document_id 必須是正整數")
        return {
            "fact_id": "document:%d:fact:%s" % (document_id, fact_key),
            "document_id": document_id,
            "scope": statement["scope"],
            "statement_type": statement["statement_type"],
            "fact_key": fact_key,
            "currency": _required_string(fact, "currency"),
            "unit_multiplier": multiplier,
            "value": decimal_string(value),
            "absolute_value": value * Decimal(multiplier),
            "source_evidence_id": _required_string(statement, "source_evidence_id"),
        }

    def _available_metric(
        self,
        symbol: str,
        metric_key: str,
        unit: str,
        company: Mapping[str, object],
        value: Decimal,
        facts: Sequence[Mapping[str, object]],
        *,
        method: str,
    ) -> Dict[str, object]:
        dependencies = [
            {
                "fact_id": fact["fact_id"],
                "document_id": fact["document_id"],
                "scope": fact["scope"],
                "statement_type": fact["statement_type"],
                "fact_key": fact["fact_key"],
                "value": fact["value"],
                "currency": fact["currency"],
                "unit_multiplier": fact["unit_multiplier"],
                "source_evidence_id": fact["source_evidence_id"],
            }
            for fact in facts
        ]
        return {
            "metric_id": "%s:%s:%s" % (symbol, metric_key, company["period"]),
            "symbol": symbol,
            "metric_key": metric_key,
            "status": "available",
            "value": decimal_string(value),
            "unit": unit,
            "period": company["period"],
            "comparison_period": company["comparison_period"],
            "methodology": method,
            "dependencies": dependencies,
            "reason_code": None,
        }

    def _unavailable_metric(
        self,
        symbol: str,
        metric_key: str,
        unit: str,
        company: Mapping[str, object],
        reason_code: str,
    ) -> Dict[str, object]:
        return {
            "metric_id": "%s:%s:%s" % (symbol, metric_key, company["period"]),
            "symbol": symbol,
            "metric_key": metric_key,
            "status": "unavailable",
            "value": None,
            "unit": unit,
            "period": company["period"],
            "comparison_period": company["comparison_period"],
            "methodology": None,
            "dependencies": [],
            "reason_code": reason_code,
        }


class FundamentalResearchResultValidator:
    """驗證 Agent 解讀只引用同一資料包與可重算指標。"""

    def __init__(
        self,
        tools: FundamentalSnapshotTools,
        bundle: Mapping[str, object],
        metrics: Mapping[str, object],
    ):
        self.tools = tools
        self.bundle = dict(bundle)
        self.metrics = dict(metrics)
        self.calculator = FundamentalMetricsCalculator(tools, bundle)
        metric_errors = self.calculator.validate(metrics)
        if metric_errors:
            raise FundamentalToolError("FundamentalMetrics 無效：%s" % "; ".join(metric_errors))
        self.companies = {
            str(item["symbol"]): dict(item) for item in self.bundle["companies"]
        }
        self.metric_companies = {
            str(item["symbol"]): dict(item) for item in self.metrics["items"]
        }

    def validate(self, payload: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        try:
            self._validate_payload(payload)
        except FundamentalToolError as error:
            errors.append(str(error))
        return errors

    def normalized(self, payload: Mapping[str, object]) -> Dict[str, object]:
        errors = self.validate(payload)
        if errors:
            raise FundamentalToolError("FundamentalResearchResult 無效：%s" % "; ".join(errors))
        normalized = dict(payload)
        normalized.pop("content_sha256", None)
        return _with_content_sha256(normalized)

    def _validate_payload(self, payload: Mapping[str, object]) -> None:
        if not isinstance(payload, Mapping):
            raise FundamentalToolError("FundamentalResearchResult 必須是物件")
        self._validate_forbidden_fields(payload)
        allowed = {
            "schema_version",
            "run_id",
            "snapshot_id",
            "decision_cutoff",
            "bundle_id",
            "bundle_sha256",
            "metrics_id",
            "metrics_sha256",
            "skill_version",
            "status",
            "items",
            "errors",
            "content_sha256",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise FundamentalToolError("FundamentalResearchResult 有未允許欄位：%s" % ", ".join(sorted(unknown)))
        if payload.get("schema_version") != RESULT_SCHEMA_VERSION:
            raise FundamentalToolError("FundamentalResearchResult schema_version 必須為 1.0")
        _required_string(payload, "run_id")
        if payload.get("snapshot_id") != self.bundle.get("snapshot_id"):
            raise FundamentalToolError("FundamentalResearchResult.snapshot_id 不一致")
        if not _same_time(
            payload.get("decision_cutoff"), self.bundle.get("decision_cutoff"), "decision_cutoff"
        ):
            raise FundamentalToolError("FundamentalResearchResult.decision_cutoff 不一致")
        for field, expected in (
            ("bundle_id", self.bundle.get("bundle_id")),
            ("bundle_sha256", self.bundle.get("content_sha256")),
            ("metrics_id", self.metrics.get("metrics_id")),
            ("metrics_sha256", self.metrics.get("content_sha256")),
        ):
            if payload.get(field) != expected:
                raise FundamentalToolError("FundamentalResearchResult.%s 不一致" % field)
        if payload.get("skill_version") != "1.0.0":
            raise FundamentalToolError("FundamentalResearchResult.skill_version 不支援")
        items = payload.get("items")
        if not isinstance(items, list):
            raise FundamentalToolError("FundamentalResearchResult.items 必須是陣列")
        expected_symbols = list(self.bundle["request"]["symbols"])
        received_symbols = [_required_string(item, "symbol").upper() if isinstance(item, Mapping) else "" for item in items]
        if received_symbols != expected_symbols:
            raise FundamentalToolError("FundamentalResearchResult.items 必須完整且依 request.symbols 排序")
        statuses: List[str] = []
        for item in items:
            statuses.append(self._validate_item(dict(item)))
        expected_status = (
            "completed"
            if statuses and set(statuses) == {"completed"}
            else "unavailable"
            if statuses and set(statuses) == {"unavailable"}
            else "degraded"
        )
        if payload.get("status") != expected_status:
            raise FundamentalToolError(
                "FundamentalResearchResult.status 應為 %s" % expected_status
            )
        errors = payload.get("errors")
        if not isinstance(errors, list) or any(
            not isinstance(item, str) for item in errors
        ):
            raise FundamentalToolError("FundamentalResearchResult.errors 必須是字串陣列")
        supplied_hash = payload.get("content_sha256")
        if supplied_hash is not None and supplied_hash != content_sha256(payload):
            raise FundamentalToolError("FundamentalResearchResult.content_sha256 不一致")

    def _validate_item(self, item: Mapping[str, object]) -> str:
        allowed = {
            "symbol",
            "research_status",
            "status_reason",
            "observations",
            "assumptions",
            "invalidation_conditions",
            "limitations",
            "evidence_ids",
            "metric_ids",
        }
        unknown = set(item) - allowed
        if unknown:
            raise FundamentalToolError("基本面 item 有未允許欄位：%s" % ", ".join(sorted(unknown)))
        symbol = _required_string(item, "symbol").upper()
        company = self.companies.get(symbol)
        metric_company = self.metric_companies.get(symbol)
        if company is None or metric_company is None:
            raise FundamentalToolError("基本面 item 引用不存在的股票：%s" % symbol)
        expected = str(metric_company["status"])
        if item.get("research_status") != expected:
            raise FundamentalToolError("%s.research_status 應為 %s" % (symbol, expected))
        _required_string(item, "status_reason")
        observations = item.get("observations")
        if not isinstance(observations, list):
            raise FundamentalToolError("%s.observations 必須是陣列" % symbol)
        assumptions = _optional_string_list(item, "assumptions")
        invalidations = _optional_string_list(item, "invalidation_conditions")
        limitations = _optional_string_list(item, "limitations")
        if expected == "unavailable" and not limitations:
            raise FundamentalToolError("%s unavailable 結果必須列出 limitations" % symbol)

        available_metrics = {
            str(metric["metric_id"]): metric
            for metric in metric_company["metrics"]
            if metric["status"] == "available"
        }
        allowed_evidence = {
            str(statement["source_evidence_id"]) for statement in company["statements"]
        }
        all_observation_evidence: List[str] = []
        all_observation_metrics: List[str] = []
        for index, observation in enumerate(observations):
            if not isinstance(observation, Mapping):
                raise FundamentalToolError("%s.observations[%d] 必須是物件" % (symbol, index))
            observation_allowed = {"text", "evidence_ids", "metric_ids"}
            if set(observation) - observation_allowed:
                raise FundamentalToolError("%s.observations[%d] 有未允許欄位" % (symbol, index))
            text = _required_string(observation, "text")
            if len(text) > 2000:
                raise FundamentalToolError("%s.observations[%d].text 過長" % (symbol, index))
            evidence_ids = _string_list(observation, "evidence_ids")
            metric_ids = _optional_string_list(observation, "metric_ids")
            if not set(evidence_ids).issubset(allowed_evidence):
                raise FundamentalToolError("%s.observations[%d] 引用不存在或跨股票證據" % (symbol, index))
            if not set(metric_ids).issubset(available_metrics):
                raise FundamentalToolError("%s.observations[%d] 引用不可用或跨股票指標" % (symbol, index))
            all_observation_evidence.extend(evidence_ids)
            all_observation_metrics.extend(metric_ids)
        if expected != "unavailable" and not observations:
            raise FundamentalToolError("%s 可用結果至少要有一則 observation" % symbol)
        evidence_ids = _optional_string_list(item, "evidence_ids")
        metric_ids = _optional_string_list(item, "metric_ids")
        if evidence_ids != sorted(set(all_observation_evidence)):
            raise FundamentalToolError("%s.evidence_ids 必須等於 observations 的去重引用" % symbol)
        if metric_ids != sorted(set(all_observation_metrics)):
            raise FundamentalToolError("%s.metric_ids 必須等於 observations 的去重引用" % symbol)
        return expected

    def _validate_forbidden_fields(self, value: object, path: str = "$") -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if isinstance(key, str) and key in FORBIDDEN_RESULT_FIELDS:
                    raise FundamentalToolError("結果不得含交易欄位：%s.%s" % (path, key))
                self._validate_forbidden_fields(child, "%s.%s" % (path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self._validate_forbidden_fields(child, "%s[%d]" % (path, index))


class FundamentalResearchApplicationService:
    """CLI 使用的檔案外觀；不讀取網路，也不寫入 Data Agent 資料庫。"""

    def __init__(self, tools: FundamentalSnapshotTools):
        self.tools = tools

    @classmethod
    def from_snapshot_path(cls, snapshot_path: Path) -> "FundamentalResearchApplicationService":
        return cls(FundamentalSnapshotTools.from_path(snapshot_path))

    def build_bundle_file(
        self,
        output: Path,
        request: Mapping[str, object],
        *,
        bundle_id: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> Dict[str, object]:
        bundle = self.tools.build_bundle(request, bundle_id=bundle_id, generated_at=generated_at)
        self._write_json(output, bundle)
        return bundle

    def compute_metrics_file(
        self,
        bundle_path: Path,
        output: Path,
        *,
        metrics_id: Optional[str] = None,
        computed_at: Optional[str] = None,
    ) -> Dict[str, object]:
        calculator = FundamentalMetricsCalculator(self.tools, _read_json(bundle_path, "FundamentalDataBundle"))
        metrics = calculator.compute(metrics_id=metrics_id, computed_at=computed_at)
        self._write_json(output, metrics)
        return metrics

    def validate_result_file(
        self,
        bundle_path: Path,
        metrics_path: Path,
        input_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        bundle = _read_json(bundle_path, "FundamentalDataBundle")
        metrics = _read_json(metrics_path, "FundamentalMetrics")
        result = _read_json(input_path, "FundamentalResearchResult")
        validator = FundamentalResearchResultValidator(self.tools, bundle, metrics)
        errors = validator.validate(result)
        normalized = validator.normalized(result) if not errors else None
        if normalized is not None and output is not None:
            self._write_json(output, normalized)
        return {"valid": not errors, "errors": errors, "result": normalized}

    def archive_result_file(
        self,
        bundle_path: Path,
        metrics_path: Path,
        input_path: Path,
        output: Path,
    ) -> Dict[str, object]:
        result = self.validate_result_file(bundle_path, metrics_path, input_path)
        if not result["valid"]:
            return result
        if output.exists():
            raise FundamentalToolError("archive 輸出已存在，拒絕覆寫：%s" % output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            handle.write(canonical_json(result["result"]))
            handle.write("\n")
        return result

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
