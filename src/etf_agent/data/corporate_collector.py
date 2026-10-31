import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import List, Sequence, Tuple

from etf_agent.core import parse_aware_time
from .database import MarketDataDatabase
from .universe import Instrument
from .corporate_contracts import CorporateCollectionResult, CorporateRecord, CorporateResponse
from .corporate_provider import OfficialCorporateProvider
from .corporate_parsing import _decimal_text


class CorporateDataCollector:
    def __init__(
        self,
        database: MarketDataDatabase,
        providers: Sequence[OfficialCorporateProvider],
    ):
        self.database = database
        self.providers = providers

    def collect(self, universe: Sequence[Instrument]) -> CorporateCollectionResult:
        if not universe:
            raise ValueError("官方交易池是空的；請先填入 data/official_universe.csv")
        if not self.providers:
            raise ValueError("未設定公司資料來源")

        run_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()
        self.database.initialize()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO collection_runs(run_id, source, started_at, status) "
                "VALUES (?, 'CORPORATE_DATA', ?, 'running')",
                (run_id, started_at),
            )

        try:
            responses: List[CorporateResponse] = []
            failures: List[str] = []
            for provider in self.providers:
                try:
                    payload, fetched_at = provider.fetch()
                except Exception as error:
                    message = "%s：取得失敗：%s" % (provider.source, error)
                    failures.append(message)
                    continue
                try:
                    records, response_warnings, fetched_rows = provider.parse(
                        payload, fetched_at
                    )
                    fatal_error = None
                except Exception as error:
                    records = []
                    response_warnings = ["回應無法解析：%s" % error]
                    fetched_rows = 0
                    fatal_error = str(error)
                    failures.append("%s：解析失敗：%s" % (provider.source, error))
                responses.append(
                    CorporateResponse(
                        source=provider.source,
                        endpoint=provider.url,
                        payload=payload,
                        fetched_at=fetched_at,
                        records=records,
                        warnings=response_warnings,
                        fetched_rows=fetched_rows,
                        fatal_error=fatal_error,
                    )
                )
            if not responses:
                raise ValueError("所有公司資料來源皆無法取得：%s" % "; ".join(failures))
            allowed = {item.symbol for item in universe}
            warnings: List[str] = list(failures)
            stored = 0
            duplicates = 0
            fetched_rows = sum(item.fetched_rows for item in responses)

            with self.database.connect() as connection:
                self.database.upsert_instruments(connection, universe, True)
                for response in responses:
                    warnings.extend(
                        "%s：%s" % (response.source, warning)
                        for warning in response.warnings
                    )
                    for warning in response.warnings:
                        _insert_quality_issue(
                            connection,
                            run_id,
                            response.source,
                            "warning",
                            "PARSE_WARNING",
                            warning,
                        )
                    if response.fatal_error:
                        _insert_quality_issue(
                            connection,
                            run_id,
                            response.source,
                            "error",
                            "PARSE_FAILURE",
                            response.fatal_error,
                        )
                    raw_payload_id = self.database.insert_raw_payload(
                        connection,
                        run_id,
                        response.source,
                        response.endpoint,
                        response.fetched_at,
                        response.payload,
                    )
                    for record in response.records:
                        if record.document.symbol not in allowed:
                            continue
                        document_id, created = _store_record(
                            connection, record, raw_payload_id
                        )
                        if created:
                            stored += 1
                        else:
                            duplicates += 1
                connection.execute(
                    """
                    UPDATE collection_runs
                    SET finished_at = ?, status = 'success', fetched_rows = ?,
                        stored_rows = ?, warning_count = ?
                    WHERE run_id = ?
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        fetched_rows,
                        stored,
                        len(warnings),
                        run_id,
                    ),
                )
            return CorporateCollectionResult(
                run_id=run_id,
                fetched_rows=fetched_rows,
                stored_documents=stored,
                duplicate_documents=duplicates,
                warning_count=len(warnings),
                warnings=warnings,
                records=tuple(
                    record for response in responses for record in response.records
                ),
                failures=tuple(failures),
                responses=tuple(responses),
            )
        except Exception as error:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE collection_runs
                    SET finished_at = ?, status = 'failed', error_message = ?
                    WHERE run_id = ?
                    """,
                    (datetime.now(timezone.utc).isoformat(), str(error), run_id),
                )
            raise


def _fetched_before(candidate: object, existing: object) -> bool:
    """candidate 的抓取時間嚴格早於 existing；任一無法解析成含時區時間時視為否（維持原行為）。"""
    try:
        return parse_aware_time(candidate, "fetched_at") < parse_aware_time(existing, "fetched_at")
    except Exception:
        return False


def _store_record(
    connection: sqlite3.Connection,
    record: CorporateRecord,
    raw_payload_id: int,
) -> Tuple[int, bool]:
    document = record.document
    content = json.dumps(
        {
            "document_type": document.document_type,
            "title": document.title,
            "body": document.body,
            "published_at": document.published_at,
            "symbol": document.symbol,
            "financial_statement": (
                {
                    "statement_type": record.financial_statement.statement_type,
                    "industry": record.financial_statement.industry,
                    "fiscal_year": record.financial_statement.fiscal_year,
                    "fiscal_quarter": record.financial_statement.fiscal_quarter,
                    "period_start": record.financial_statement.period_start,
                    "period_end": record.financial_statement.period_end,
                    "period_kind": record.financial_statement.period_kind,
                    "reporting_scope": record.financial_statement.reporting_scope,
                    "currency": record.financial_statement.currency,
                    "unit_multiplier": record.financial_statement.unit_multiplier,
                    "mapping_version": record.financial_statement.mapping_version,
                    "facts": [
                        {
                            "metric_key": fact.metric_key,
                            "source_field": fact.source_field,
                            "value": _decimal_text(fact.value),
                            "value_status": fact.value_status,
                            "currency": fact.currency,
                            "unit_multiplier": fact.unit_multiplier,
                        }
                        for fact in record.financial_statement.facts
                    ],
                }
                if record.financial_statement
                else None
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    previous = connection.execute(
        """
        SELECT id, version, content_sha256, fetched_at FROM source_documents
        WHERE source = ? AND external_id = ?
        ORDER BY version DESC LIMIT 1
        """,
        (document.source, document.external_id),
    ).fetchone()
    # 只與最新版本比較：內容 A→B→A 時，回到 A 是新的一版，Snapshot 才會選到最新狀態。
    if previous and previous["content_sha256"] == content_sha256:
        return int(previous["id"]), False
    # 抓取時間比現有最新版更早的舊內容不得蓋過新版（亂序或快取的舊列會把更正倒轉回去）。
    if previous and _fetched_before(document.fetched_at, previous["fetched_at"]):
        return int(previous["id"]), False
    version = int(previous["version"]) + 1 if previous else 1
    supersedes_id = int(previous["id"]) if previous else None
    cursor = connection.execute(
        """
        INSERT INTO source_documents(
            source, external_id, version, document_type, title, body,
            source_url, published_at, available_at, fetched_at,
            content_sha256, raw_payload_id, supersedes_document_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document.source,
            document.external_id,
            version,
            document.document_type,
            document.title,
            document.body,
            document.source_url,
            document.published_at,
            document.available_at,
            document.fetched_at,
            content_sha256,
            raw_payload_id,
            supersedes_id,
        ),
    )
    document_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO document_instruments(document_id, symbol, evidence) VALUES (?, ?, ?)",
        (document_id, document.symbol, document.evidence),
    )
    if record.monthly_revenue:
        revenue = record.monthly_revenue
        connection.execute(
            """
            INSERT INTO monthly_revenues(
                document_id, symbol, revenue_period, currency, unit_multiplier,
                current_revenue, previous_month_revenue, previous_year_revenue,
                mom_pct, yoy_pct, cumulative_revenue,
                previous_year_cumulative_revenue, cumulative_yoy_pct, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                revenue.symbol,
                revenue.revenue_period,
                revenue.currency,
                revenue.unit_multiplier,
                _decimal_text(revenue.current_revenue),
                _decimal_text(revenue.previous_month_revenue),
                _decimal_text(revenue.previous_year_revenue),
                _decimal_text(revenue.mom_pct),
                _decimal_text(revenue.yoy_pct),
                _decimal_text(revenue.cumulative_revenue),
                _decimal_text(revenue.previous_year_cumulative_revenue),
                _decimal_text(revenue.cumulative_yoy_pct),
                revenue.note,
            ),
        )
    if record.financial_statement:
        statement = record.financial_statement
        facts_json = json.dumps(
            {
                fact.metric_key: {
                    "source_field": fact.source_field,
                    "value": _decimal_text(fact.value),
                    "value_status": fact.value_status,
                    "currency": fact.currency,
                    "unit_multiplier": fact.unit_multiplier,
                }
                for fact in statement.facts
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """
            INSERT INTO financial_statements(
                document_id, symbol, statement_type, industry,
                fiscal_year, fiscal_quarter, period_start, period_end,
                period_kind, reporting_scope, currency, unit_multiplier,
                reported_at, source_published_at, mapping_version, facts_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                statement.symbol,
                statement.statement_type,
                statement.industry,
                statement.fiscal_year,
                statement.fiscal_quarter,
                statement.period_start,
                statement.period_end,
                statement.period_kind,
                statement.reporting_scope,
                statement.currency,
                statement.unit_multiplier,
                statement.reported_at,
                statement.source_published_at,
                statement.mapping_version,
                facts_json,
            ),
        )
        connection.executemany(
            """
            INSERT INTO financial_statement_facts(
                document_id, metric_key, source_field, value, value_status,
                currency, unit_multiplier
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    document_id,
                    fact.metric_key,
                    fact.source_field,
                    _decimal_text(fact.value),
                    fact.value_status,
                    fact.currency,
                    fact.unit_multiplier,
                )
                for fact in statement.facts
            ],
        )
    return document_id, True


def _insert_quality_issue(
    connection: sqlite3.Connection,
    run_id: str,
    source: str,
    severity: str,
    issue_code: str,
    message: str,
) -> None:
    connection.execute(
        """
        INSERT INTO quality_issues(
            run_id, source, severity, issue_code, message
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, source, severity, issue_code, message),
    )
