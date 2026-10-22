from dataclasses import dataclass
from datetime import timedelta, timezone
from decimal import Decimal
from typing import List, Optional, Tuple



TAIPEI_TIMEZONE = timezone(timedelta(hours=8))
FINANCIAL_MAPPING_VERSION = "twse-tpex-openapi-2026-10-v2"


@dataclass(frozen=True)
class SourceDocument:
    source: str
    external_id: str
    document_type: str
    title: str
    body: str
    source_url: str
    published_at: str
    available_at: str
    fetched_at: str
    symbol: str
    evidence: str


@dataclass(frozen=True)
class MonthlyRevenue:
    symbol: str
    revenue_period: str
    currency: str
    unit_multiplier: int
    current_revenue: Optional[Decimal]
    previous_month_revenue: Optional[Decimal]
    previous_year_revenue: Optional[Decimal]
    mom_pct: Optional[Decimal]
    yoy_pct: Optional[Decimal]
    cumulative_revenue: Optional[Decimal]
    previous_year_cumulative_revenue: Optional[Decimal]
    cumulative_yoy_pct: Optional[Decimal]
    note: str


@dataclass(frozen=True)
class FinancialStatementFact:
    """業別映射後的單一財報事實。

    ``source_field`` 永遠指向來源回應欄位或可重算的來源欄位合計；沒有來源欄位時保留
    ``not_reported``，而不是將其他業別的相近欄位硬轉換過來。
    """

    metric_key: str
    source_field: Optional[str]
    value: Optional[Decimal]
    value_status: str
    currency: str
    unit_multiplier: int


@dataclass(frozen=True)
class FinancialStatement:
    symbol: str
    statement_type: str
    industry: str
    fiscal_year: int
    fiscal_quarter: int
    period_start: Optional[str]
    period_end: str
    period_kind: str
    reporting_scope: str
    currency: str
    unit_multiplier: int
    reported_at: str
    source_published_at: Optional[str]
    mapping_version: str
    facts: Tuple[FinancialStatementFact, ...]


@dataclass(frozen=True)
class CorporateRecord:
    document: SourceDocument
    monthly_revenue: Optional[MonthlyRevenue] = None
    financial_statement: Optional[FinancialStatement] = None


@dataclass(frozen=True)
class CorporateResponse:
    source: str
    endpoint: str
    payload: str
    fetched_at: str
    records: List[CorporateRecord]
    warnings: List[str]
    fetched_rows: int
    fatal_error: Optional[str] = None


@dataclass(frozen=True)
class CorporateCollectionResult:
    run_id: str
    fetched_rows: int
    stored_documents: int
    duplicate_documents: int
    warning_count: int
    warnings: List[str]
    records: Tuple[CorporateRecord, ...] = ()
    failures: Tuple[str, ...] = ()
    responses: Tuple[CorporateResponse, ...] = ()
