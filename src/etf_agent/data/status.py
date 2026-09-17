"""Data Agent 狀態查詢物件。"""

from dataclasses import dataclass
from typing import List, Optional

from .database import MarketDataDatabase


@dataclass(frozen=True)
class DocumentStatus:
    versions: int
    documents: int
    monthly_rows: int
    event_rows: int


@dataclass(frozen=True)
class PriceSourceStatus:
    source: str
    row_count: int


@dataclass(frozen=True)
class PriceStatus:
    row_count: int
    symbol_count: int
    first_date: Optional[str]
    last_date: Optional[str]
    latest_symbol_count: int
    sources: List[PriceSourceStatus]


@dataclass(frozen=True)
class CollectionRunStatus:
    run_id: str
    source: str
    status: str
    started_at: str
    finished_at: Optional[str]
    fetched_rows: int
    stored_rows: int
    warning_count: int
    error_message: Optional[str]


@dataclass(frozen=True)
class SourceReportStatus:
    report_id: str
    generated_at: str
    status: str


@dataclass(frozen=True)
class UniverseValidationStatus:
    validation_id: str
    generated_at: str
    status: str
    usable: bool
    total_count: int
    tradable_count: int
    not_tradable_count: int
    mismatch_count: int


@dataclass(frozen=True)
class DataAgentStatus:
    prices: PriceStatus
    documents: DocumentStatus
    snapshot_count: int
    quality_issue_count: int
    latest_collection: Optional[CollectionRunStatus]
    latest_source_report: Optional[SourceReportStatus]
    latest_universe_validation: Optional[UniverseValidationStatus]


class DataAgentStatusRepository:
    """從 SQLite 讀取 Data Agent 狀態並轉成資料物件。"""

    def __init__(self, database: MarketDataDatabase):
        self.database = database

    def load(self) -> DataAgentStatus:
        self.database.initialize()
        with self.database.connect() as connection:
            prices = connection.execute(
                """
                SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols,
                       MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
                FROM analysis_daily_prices
                """
            ).fetchone()
            latest_symbol_count = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT symbol) AS value
                    FROM analysis_daily_prices
                    WHERE trade_date=(SELECT MAX(trade_date) FROM analysis_daily_prices)
                    """
                ).fetchone()["value"]
            )
            price_sources = connection.execute(
                """
                SELECT source, COUNT(*) AS rows
                FROM analysis_daily_prices
                GROUP BY source ORDER BY rows DESC
                """
            ).fetchall()
            documents = connection.execute(
                """
                SELECT COUNT(*) AS versions,
                       COUNT(DISTINCT source || '|' || external_id) AS documents,
                       SUM(document_type = 'monthly_revenue') AS monthly_rows,
                       SUM(document_type = 'material_event') AS event_rows
                FROM source_documents
                """
            ).fetchone()
            snapshot_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS value FROM research_snapshots"
                ).fetchone()["value"]
            )
            quality_issue_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS value FROM quality_issues"
                ).fetchone()["value"]
            )
            collection = connection.execute(
                """
                SELECT run_id, source, status, started_at, finished_at,
                       fetched_rows, stored_rows, warning_count, error_message
                FROM collection_runs
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            source_report = connection.execute(
                """
                SELECT report_id, generated_at, status
                FROM source_feasibility_reports
                ORDER BY generated_at DESC LIMIT 1
                """
            ).fetchone()
            validation = connection.execute(
                """
                SELECT validation_id, generated_at, status, usable,
                       total_count, tradable_count, not_tradable_count, mismatch_count
                FROM universe_validation_runs
                ORDER BY generated_at DESC LIMIT 1
                """
            ).fetchone()

        return DataAgentStatus(
            prices=PriceStatus(
                row_count=int(prices["rows"]),
                symbol_count=int(prices["symbols"]),
                first_date=(
                    str(prices["first_date"])
                    if prices["first_date"] is not None
                    else None
                ),
                last_date=(
                    str(prices["last_date"])
                    if prices["last_date"] is not None
                    else None
                ),
                latest_symbol_count=latest_symbol_count,
                sources=[
                    PriceSourceStatus(
                        source=str(row["source"]), row_count=int(row["rows"])
                    )
                    for row in price_sources
                ],
            ),
            documents=DocumentStatus(
                versions=int(documents["versions"]),
                documents=int(documents["documents"]),
                monthly_rows=int(documents["monthly_rows"] or 0),
                event_rows=int(documents["event_rows"] or 0),
            ),
            snapshot_count=snapshot_count,
            quality_issue_count=quality_issue_count,
            latest_collection=(
                CollectionRunStatus(
                    run_id=str(collection["run_id"]),
                    source=str(collection["source"]),
                    status=str(collection["status"]),
                    started_at=str(collection["started_at"]),
                    finished_at=(
                        str(collection["finished_at"])
                        if collection["finished_at"] is not None
                        else None
                    ),
                    fetched_rows=int(collection["fetched_rows"]),
                    stored_rows=int(collection["stored_rows"]),
                    warning_count=int(collection["warning_count"]),
                    error_message=(
                        str(collection["error_message"])
                        if collection["error_message"] is not None
                        else None
                    ),
                )
                if collection is not None
                else None
            ),
            latest_source_report=(
                SourceReportStatus(
                    report_id=str(source_report["report_id"]),
                    generated_at=str(source_report["generated_at"]),
                    status=str(source_report["status"]),
                )
                if source_report is not None
                else None
            ),
            latest_universe_validation=(
                UniverseValidationStatus(
                    validation_id=str(validation["validation_id"]),
                    generated_at=str(validation["generated_at"]),
                    status=str(validation["status"]),
                    usable=bool(validation["usable"]),
                    total_count=int(validation["total_count"]),
                    tradable_count=int(validation["tradable_count"]),
                    not_tradable_count=int(validation["not_tradable_count"]),
                    mismatch_count=int(validation["mismatch_count"]),
                )
                if validation is not None
                else None
            ),
        )
