"""官方財報彙總資料的請求、覆蓋驗收與來源可行性結果。"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Sequence, Tuple

from .contracts import SourceFeasibilityReport, SourceProbeResult

from .corporate import (
    CorporateCollectionResult,
    CorporateDataCollector,
    CorporateResponse,
    OfficialCorporateProvider,
    _is_empty_financial_placeholder,
)
from .database import MarketDataDatabase
from .universe import Instrument


FINANCIAL_SCHEMA_VERSION = "1.0"
REQUIRED_STATEMENT_TYPES = ("income_statement", "balance_sheet")


@dataclass(frozen=True)
class FinancialStatementRequest:
    """固定財報驗收分母的請求契約。"""

    fiscal_year: int
    fiscal_quarter: int
    required_statement_types: Tuple[str, ...] = REQUIRED_STATEMENT_TYPES

    def __post_init__(self) -> None:
        if self.fiscal_year < 1912:
            raise ValueError("fiscal_year 必須是西元年度")
        if self.fiscal_quarter not in {1, 2, 3, 4}:
            raise ValueError("fiscal_quarter 必須介於 1 與 4")
        if not self.required_statement_types:
            raise ValueError("至少要要求一種財報")
        unknown = set(self.required_statement_types) - set(REQUIRED_STATEMENT_TYPES)
        if unknown:
            raise ValueError("不支援的財報類型：%s" % ", ".join(sorted(unknown)))


@dataclass(frozen=True)
class FinancialCoverageGap:
    symbol: str
    statement_type: str
    reason_code: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "symbol": self.symbol,
            "statement_type": self.statement_type,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class FinancialCollectionResult:
    request: FinancialStatementRequest
    collection: CorporateCollectionResult
    source_report: SourceFeasibilityReport
    expected_count: int
    covered_count: int
    gaps: Tuple[FinancialCoverageGap, ...]
    unexpected_period_count: int

    @property
    def status(self) -> str:
        if self.source_report.status == "failed":
            return "failed"
        if self.gaps or self.source_report.status != "passed":
            return "degraded"
        return "completed"

    @property
    def usable(self) -> bool:
        return self.status == "completed"

    def as_dict(self) -> Dict[str, object]:
        return {
            "schema_version": FINANCIAL_SCHEMA_VERSION,
            "status": self.status,
            "usable": self.usable,
            "request": {
                "fiscal_year": self.request.fiscal_year,
                "fiscal_quarter": self.request.fiscal_quarter,
                "required_statement_types": list(self.request.required_statement_types),
            },
            "collection": {
                "run_id": self.collection.run_id,
                "fetched_rows": self.collection.fetched_rows,
                "stored_documents": self.collection.stored_documents,
                "duplicate_documents": self.collection.duplicate_documents,
                "warning_count": self.collection.warning_count,
                "warnings": list(self.collection.warnings),
                "failures": list(self.collection.failures),
            },
            "coverage": {
                "expected_count": self.expected_count,
                "covered_count": self.covered_count,
                "missing_count": len(self.gaps),
                "gaps": [item.as_dict() for item in self.gaps],
                "unexpected_period_count": self.unexpected_period_count,
            },
            "source_report": self.source_report.as_dict(),
        }


class FinancialStatementCollector:
    """以固定交易池分母收集、保存並驗收官方財報彙總資料。"""

    def __init__(
        self,
        database: MarketDataDatabase,
        providers: Sequence[OfficialCorporateProvider],
    ):
        if not providers:
            raise ValueError("未設定財報資料來源")
        invalid = [
            provider.source
            for provider in providers
            if provider.document_type != "financial_statement"
        ]
        if invalid:
            raise ValueError("財報收集器不可使用非財報來源：%s" % ", ".join(invalid))
        self.database = database
        self.providers = tuple(providers)

    def collect(
        self,
        universe: Sequence[Instrument],
        request: FinancialStatementRequest,
    ) -> FinancialCollectionResult:
        if not universe:
            raise ValueError("官方交易池是空的；請先填入 data/official_universe.csv")
        collection = CorporateDataCollector(self.database, self.providers).collect(universe)
        source_report = self._source_report(collection)
        self.database.save_source_feasibility_report(source_report)

        allowed_symbols = {item.symbol for item in universe}
        covered = set()
        unexpected_period_count = 0
        for record in collection.records:
            statement = record.financial_statement
            if statement is None or statement.symbol not in allowed_symbols:
                continue
            if (
                statement.fiscal_year != request.fiscal_year
                or statement.fiscal_quarter != request.fiscal_quarter
            ):
                unexpected_period_count += 1
                continue
            if statement.statement_type in request.required_statement_types:
                covered.add((statement.symbol, statement.statement_type))

        gaps = tuple(
            FinancialCoverageGap(
                symbol=instrument.symbol,
                statement_type=statement_type,
                reason_code="TARGET_PERIOD_NOT_VERIFIED",
            )
            for instrument in sorted(universe, key=lambda item: item.symbol)
            for statement_type in request.required_statement_types
            if (instrument.symbol, statement_type) not in covered
        )
        return FinancialCollectionResult(
            request=request,
            collection=collection,
            source_report=source_report,
            expected_count=len(universe) * len(request.required_statement_types),
            covered_count=len(covered),
            gaps=gaps,
            unexpected_period_count=unexpected_period_count,
        )

    def _source_report(
        self, collection: CorporateCollectionResult
    ) -> SourceFeasibilityReport:
        responses = {response.source: response for response in collection.responses}
        failures_by_source = {
            item.split("：", 1)[0]: item for item in collection.failures
        }
        probes = [
            _financial_probe(provider, responses.get(provider.source), failures_by_source)
            for provider in self.providers
        ]
        failed = [probe for probe in probes if probe.status == "failed"]
        if failed and len(failed) == len(probes):
            status = "failed"
        elif failed:
            status = "degraded"
        else:
            status = "passed"
        return SourceFeasibilityReport(
            schema_version=FINANCIAL_SCHEMA_VERSION,
            report_id=str(uuid.uuid4()),
            generated_at=datetime.now(timezone.utc).isoformat(),
            status=status,
            probes=probes,
            errors=list(collection.failures),
        )


def _financial_probe(
    provider: OfficialCorporateProvider,
    response: CorporateResponse | None,
    failures_by_source: Dict[str, str],
) -> SourceProbeResult:
    failure = failures_by_source.get(provider.source)
    if response is None:
        return SourceProbeResult(
            source_id=provider.source,
            market=provider.market,
            data_type="financial_statement",
            url=provider.url,
            status="failed",
            fetched_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=0,
            http_status=None,
            format="json",
            row_count=0,
            data_date=None,
            errors=[failure or "FETCH_FAILED"],
        )
    schema_fields: List[str] = []
    try:
        rows = json.loads(response.payload)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            schema_fields = sorted(str(field) for field in rows[0])
    except json.JSONDecodeError:
        pass
    valid_records = [
        item.financial_statement
        for item in response.records
        if item.financial_statement is not None
    ]
    errors: List[str] = []
    if response.fatal_error:
        errors.append("PARSE_FAILURE: %s" % response.fatal_error)
    is_placeholder_response = _is_placeholder_response(response.payload)
    if response.fetched_rows and not valid_records and not is_placeholder_response:
        errors.append("NO_VALID_FINANCIAL_ROWS")
    if not response.fetched_rows:
        errors.append("EMPTY_RESPONSE")
    taipei = timezone(timedelta(hours=8))
    data_dates = sorted(
        {
            datetime.fromisoformat(statement.reported_at).astimezone(taipei).date().isoformat()
            for statement in valid_records
            if statement is not None
        }
    )
    return SourceProbeResult(
        source_id=provider.source,
        market=provider.market,
        data_type="financial_statement",
        url=provider.url,
        status="failed" if errors else "passed",
        fetched_at=response.fetched_at,
        duration_ms=0,
        http_status=200,
        format="json",
        row_count=response.fetched_rows,
        data_date=data_dates[-1] if data_dates else None,
        schema_fields=schema_fields,
        observed_codes=sorted(
            statement.symbol.split(".", 1)[0]
            for statement in valid_records
            if statement is not None
        ),
        content_sha256=hashlib.sha256(response.payload.encode("utf-8")).hexdigest(),
        errors=errors,
    )


def _is_placeholder_response(payload: str) -> bool:
    try:
        rows = json.loads(payload)
    except json.JSONDecodeError:
        return False
    return bool(rows) and isinstance(rows, list) and all(
        isinstance(row, dict) and _is_empty_financial_placeholder(row) for row in rows
    )
