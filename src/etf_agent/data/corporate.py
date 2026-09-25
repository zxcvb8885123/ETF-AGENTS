import hashlib
import json
import sqlite3
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core import parse_aware_time
from .database import MarketDataDatabase
from .universe import Instrument, normalize_symbol


TAIPEI_TIMEZONE = timezone(timedelta(hours=8))


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

    ``source_field`` 永遠指向官方回應欄位；沒有官方欄位時保留
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


class OfficialCorporateProvider:
    def __init__(
        self,
        source: str,
        url: str,
        market: str,
        document_type: str,
        statement_type: Optional[str] = None,
        industry: Optional[str] = None,
        unit_multiplier: int = 1000,
        mapping_version: str = "twse-tpex-openapi-2026-09-v1",
        timeout_seconds: int = 30,
        retries: int = 3,
        user_agent: str = "ETF-Agent-AICUP-2026/0.1",
    ):
        if document_type not in {
            "monthly_revenue",
            "material_event",
            "financial_statement",
        }:
            raise ValueError("不支援的公司資料類型：%s" % document_type)
        if document_type == "financial_statement":
            if statement_type not in {"income_statement", "balance_sheet"}:
                raise ValueError("財報必須指定支援的 statement_type")
            if not industry:
                raise ValueError("財報必須指定業別")
            if unit_multiplier < 1:
                raise ValueError("財報金額 unit_multiplier 必須為正整數")
        elif statement_type is not None or industry is not None:
            raise ValueError("只有財報來源可以指定 statement_type 或 industry")
        if retries < 1 or retries > 3:
            raise ValueError("retries 必須介於 1 與 3")
        self.source = source
        self.url = url
        self.market = market
        self.document_type = document_type
        self.statement_type = statement_type
        self.industry = industry
        self.unit_multiplier = unit_multiplier
        self.mapping_version = mapping_version
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.user_agent = user_agent

    def fetch(self) -> Tuple[str, str]:
        request = urllib.request.Request(
            self.url,
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
        )
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = response.read().decode("utf-8-sig")
                return payload, datetime.now(timezone.utc).isoformat()
            except Exception as error:
                last_error = error
                if attempt + 1 < self.retries:
                    time.sleep(0.25 * (2 ** attempt))
        assert last_error is not None
        raise last_error

    def fetch_and_parse(self) -> CorporateResponse:
        payload, fetched_at = self.fetch()
        records, warnings, fetched_rows = self.parse(payload, fetched_at)
        return CorporateResponse(
            source=self.source,
            endpoint=self.url,
            payload=payload,
            fetched_at=fetched_at,
            records=records,
            warnings=warnings,
            fetched_rows=fetched_rows,
        )

    def parse(
        self, payload: str, fetched_at: str
    ) -> Tuple[List[CorporateRecord], List[str], int]:
        fetched_at = _utc_iso(_require_aware_datetime(fetched_at, "fetched_at"))
        rows = json.loads(payload)
        if not isinstance(rows, list):
            raise ValueError("官方公司資料回應必須是 JSON 陣列")
        records: List[CorporateRecord] = []
        warnings: List[str] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                warnings.append("第 %d 筆不是物件，已略過" % (index + 1))
                continue
            if (
                self.document_type == "financial_statement"
                and _is_empty_financial_placeholder(row)
            ):
                # TPEx 部分沒有適用公司之業別端點會回傳一筆欄位完整、
                # 公司代號與公司名稱皆空白的佔位資料列。它不是公司資料，
                # 也不能當成解析錯誤或拿來填補覆蓋分母。
                continue
            try:
                if self.document_type == "monthly_revenue":
                    records.append(self._parse_monthly_revenue(row, fetched_at))
                elif self.document_type == "material_event":
                    records.append(self._parse_material_event(row, fetched_at))
                else:
                    records.append(self._parse_financial_statement(row, fetched_at))
            except (KeyError, ValueError) as error:
                warnings.append("第 %d 筆無法解析：%s" % (index + 1, error))
        return records, warnings, len(rows)

    def _parse_monthly_revenue(
        self, row: Dict[str, object], fetched_at: str
    ) -> CorporateRecord:
        code = _required_text(row, "公司代號", "SecuritiesCompanyCode")
        company_name = _required_text(row, "公司名稱", "CompanyName")
        report_date = _parse_roc_date(_required_text(row, "出表日期", "Date"))
        revenue_period = _parse_roc_month(_required_text(row, "資料年月"))
        symbol = normalize_symbol(code, self.market)
        published_at = _utc_iso(
            datetime.combine(
                report_date, datetime.min.time(), tzinfo=TAIPEI_TIMEZONE
            )
        )
        canonical_body = json.dumps(row, ensure_ascii=False, sort_keys=True)
        document = SourceDocument(
            source=self.source,
            external_id="monthly-revenue:%s:%s" % (code, revenue_period),
            document_type="monthly_revenue",
            title="%s %s 月營收" % (company_name, revenue_period),
            body=canonical_body,
            source_url=self.url,
            published_at=published_at,
            available_at=fetched_at,
            fetched_at=fetched_at,
            symbol=symbol,
            evidence="公司代號=%s" % code,
        )
        revenue = MonthlyRevenue(
            symbol=symbol,
            revenue_period=revenue_period,
            currency="TWD",
            unit_multiplier=1000,
            current_revenue=_optional_decimal(row.get("營業收入-當月營收")),
            previous_month_revenue=_optional_decimal(row.get("營業收入-上月營收")),
            previous_year_revenue=_optional_decimal(row.get("營業收入-去年當月營收")),
            mom_pct=_optional_decimal(row.get("營業收入-上月比較增減(%)")),
            yoy_pct=_optional_decimal(row.get("營業收入-去年同月增減(%)")),
            cumulative_revenue=_optional_decimal(row.get("累計營業收入-當月累計營收")),
            previous_year_cumulative_revenue=_optional_decimal(
                row.get("累計營業收入-去年累計營收")
            ),
            cumulative_yoy_pct=_optional_decimal(
                row.get("累計營業收入-前期比較增減(%)")
            ),
            note=_text(row.get("備註")),
        )
        return CorporateRecord(document=document, monthly_revenue=revenue)

    def _parse_material_event(
        self, row: Dict[str, object], fetched_at: str
    ) -> CorporateRecord:
        code = _required_text(row, "公司代號", "SecuritiesCompanyCode")
        company_name = _required_text(row, "公司名稱", "CompanyName")
        speaking_date = _required_text(row, "發言日期")
        speaking_time = _required_text(row, "發言時間").zfill(6)
        published_at = _utc_iso(_parse_roc_datetime(speaking_date, speaking_time))
        title = _required_text_by_normalized_key(row, "主旨")
        clause = _text(row.get("符合條款"))
        fact_date = _text(row.get("事實發生日"))
        symbol = normalize_symbol(code, self.market)
        external_id = "material-event:%s:%s:%s:%s:%s" % (
            code,
            speaking_date,
            speaking_time,
            clause,
            fact_date,
        )
        document = SourceDocument(
            source=self.source,
            external_id=external_id,
            document_type="material_event",
            title=title,
            body=_text(row.get("說明")),
            source_url=self.url,
            published_at=published_at,
            available_at=fetched_at,
            fetched_at=fetched_at,
            symbol=symbol,
            evidence="%s（%s）" % (company_name, code),
        )
        return CorporateRecord(document=document)

    def _parse_financial_statement(
        self, row: Dict[str, object], fetched_at: str
    ) -> CorporateRecord:
        code = _required_text(row, "公司代號", "SecuritiesCompanyCode")
        company_name = _required_text(row, "公司名稱", "CompanyName")
        report_date = _parse_roc_date(_required_text(row, "出表日期", "Date"))
        fiscal_year = _parse_roc_year(_required_text(row, "年度", "Year"))
        fiscal_quarter = _parse_quarter(_required_text(row, "季別", "Season"))
        assert self.statement_type is not None
        assert self.industry is not None
        symbol = normalize_symbol(code, self.market)
        reported_at = _utc_iso(
            datetime.combine(report_date, datetime.min.time(), tzinfo=TAIPEI_TIMEZONE)
        )
        period_start, period_end, period_kind = _financial_period(
            fiscal_year, fiscal_quarter, self.statement_type
        )
        facts = _financial_facts(
            row,
            self.statement_type,
            self.industry,
            self.unit_multiplier,
        )
        statement_label = (
            "綜合損益表"
            if self.statement_type == "income_statement"
            else "資產負債表"
        )
        document = SourceDocument(
            source=self.source,
            external_id="financial-statement:%s:%s:%s:%d:%d"
            % (
                self.statement_type,
                self.industry,
                code,
                fiscal_year,
                fiscal_quarter,
            ),
            document_type="financial_statement",
            title="%s %dQ%d %s（%s）"
            % (company_name, fiscal_year, fiscal_quarter, statement_label, self.industry),
            body=json.dumps(row, ensure_ascii=False, sort_keys=True),
            source_url=self.url,
            # OpenAPI 的「出表日期」是資料產製日，非公司公告時間；實際
            # publication time 另於財報契約保留 null。此欄僅供既有文件
            # 索引的內容時間排序，時間隔離仍以 available_at 關閉未取得資料。
            published_at=reported_at,
            available_at=fetched_at,
            fetched_at=fetched_at,
            symbol=symbol,
            evidence="公司代號=%s；年度=%s；季別=%s" % (code, fiscal_year, fiscal_quarter),
        )
        statement = FinancialStatement(
            symbol=symbol,
            statement_type=self.statement_type,
            industry=self.industry,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            period_start=period_start,
            period_end=period_end,
            period_kind=period_kind,
            reporting_scope="unknown",
            currency="TWD",
            unit_multiplier=self.unit_multiplier,
            reported_at=reported_at,
            source_published_at=None,
            mapping_version=self.mapping_version,
            facts=facts,
        )
        return CorporateRecord(document=document, financial_statement=statement)


class OfficialCorporateProviderFactory:
    """從 allowlist 設定建立官方公司資料 provider 物件。"""

    def create(
        self, config: Mapping[str, object]
    ) -> List[OfficialCorporateProvider]:
        sources = config.get("sources")
        if not isinstance(sources, list):
            raise ValueError("corporate_data 缺少 sources 設定")
        timeout_seconds = int(config.get("timeout_seconds", 30))
        user_agent = str(config.get("user_agent", "ETF-Agent-AICUP-2026/0.1"))
        return [
            OfficialCorporateProvider(
                source=str(item["source"]),
                url=str(item["url"]),
                market=str(item["market"]),
                document_type=str(item["document_type"]),
                statement_type=(
                    str(item["statement_type"])
                    if item.get("statement_type") is not None
                    else None
                ),
                industry=(
                    str(item["industry"]) if item.get("industry") is not None else None
                ),
                unit_multiplier=int(item.get("unit_multiplier", 1000)),
                mapping_version=str(
                    item.get("mapping_version", "twse-tpex-openapi-2026-09-v1")
                ),
                timeout_seconds=timeout_seconds,
                retries=int(config.get("retries", 3)),
                user_agent=user_agent,
            )
            for item in sources
        ]


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
    duplicate = connection.execute(
        """
        SELECT id FROM source_documents
        WHERE source = ? AND external_id = ? AND content_sha256 = ?
        """,
        (document.source, document.external_id, content_sha256),
    ).fetchone()
    if duplicate:
        return int(duplicate["id"]), False

    previous = connection.execute(
        """
        SELECT id, version FROM source_documents
        WHERE source = ? AND external_id = ?
        ORDER BY version DESC LIMIT 1
        """,
        (document.source, document.external_id),
    ).fetchone()
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


def _parse_roc_date(value: str):
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) != 7:
        raise ValueError("無法解析民國日期：%s" % value)
    return datetime(
        int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7])
    ).date()


def _parse_roc_month(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) != 5:
        raise ValueError("無法解析民國年月：%s" % value)
    year = int(digits[:3]) + 1911
    month = int(digits[3:5])
    if not 1 <= month <= 12:
        raise ValueError("月份不正確：%s" % value)
    return "%04d-%02d" % (year, month)


def _parse_roc_year(value: str) -> int:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) != 3:
        raise ValueError("無法解析民國年度：%s" % value)
    return int(digits) + 1911


def _parse_quarter(value: str) -> int:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) != 1 or not 1 <= int(digits) <= 4:
        raise ValueError("季度必須介於 1 與 4：%s" % value)
    return int(digits)


def _financial_period(
    fiscal_year: int, fiscal_quarter: int, statement_type: str
) -> Tuple[Optional[str], str, str]:
    quarter_end_month = fiscal_quarter * 3
    if quarter_end_month == 12:
        period_end = date(fiscal_year, 12, 31)
    else:
        period_end = date(fiscal_year, quarter_end_month + 1, 1) - timedelta(days=1)
    if statement_type == "income_statement":
        return (
            date(fiscal_year, 1, 1).isoformat(),
            period_end.isoformat(),
            "cumulative_to_quarter",
        )
    if statement_type == "balance_sheet":
        return None, period_end.isoformat(), "point_in_time"
    raise ValueError("不支援的財報類型：%s" % statement_type)


_COMMON_INCOME_METRICS = (
    ("profit_before_tax", ("稅前淨利（淨損）",)),
    ("net_income", ("本期淨利（淨損）",)),
    ("basic_eps", ("基本每股盈餘（元）",)),
)

_COMMON_BALANCE_METRICS = (
    ("total_assets", ("資產總額", "資產總計")),
    ("total_liabilities", ("負債總額", "負債總計")),
    ("total_equity", ("權益總額", "權益總計")),
)

_INDUSTRY_FINANCIAL_METRICS = {
    ("income_statement", "ci"): (
        ("revenue", ("營業收入",)),
        ("operating_profit", ("營業利益（損失）",)),
    ),
    ("income_statement", "mim"): (
        ("revenue", ("營業收入",)),
        ("operating_profit", ("營業利益（損失）",)),
    ),
}


def _financial_facts(
    row: Mapping[str, object],
    statement_type: str,
    industry: str,
    unit_multiplier: int,
) -> Tuple[FinancialStatementFact, ...]:
    """以明確業別 mapping 產生可比較事實，原始欄位仍完整留在文件 body。"""

    if statement_type == "income_statement":
        definitions = (
            _INDUSTRY_FINANCIAL_METRICS.get((statement_type, industry), ())
            + _COMMON_INCOME_METRICS
        )
    elif statement_type == "balance_sheet":
        definitions = _COMMON_BALANCE_METRICS
    else:
        raise ValueError("不支援的財報類型：%s" % statement_type)

    facts: List[FinancialStatementFact] = []
    for metric_key, source_fields in definitions:
        source_field, raw_value = _first_nonempty_field(row, source_fields)
        if source_field is None:
            facts.append(
                FinancialStatementFact(
                    metric_key=metric_key,
                    source_field=None,
                    value=None,
                    value_status="not_reported",
                    currency="TWD",
                    unit_multiplier=(1 if metric_key == "basic_eps" else unit_multiplier),
                )
            )
            continue
        facts.append(
            FinancialStatementFact(
                metric_key=metric_key,
                source_field=source_field,
                value=_optional_decimal(raw_value),
                value_status="provided",
                currency="TWD",
                unit_multiplier=(1 if metric_key == "basic_eps" else unit_multiplier),
            )
        )
    return tuple(facts)


def _first_nonempty_field(
    row: Mapping[str, object], source_fields: Sequence[str]
) -> Tuple[Optional[str], Optional[object]]:
    for source_field in source_fields:
        value = row.get(source_field)
        if _text(value) not in {"", "-", "--", "N/A", "null", "None"}:
            return source_field, value
    return None, None


def _is_empty_financial_placeholder(row: Mapping[str, object]) -> bool:
    return not _text(row.get("公司代號") or row.get("SecuritiesCompanyCode")) and not _text(
        row.get("公司名稱") or row.get("CompanyName")
    )


def _parse_roc_datetime(date_value: str, time_value: str) -> datetime:
    date = _parse_roc_date(date_value)
    digits = "".join(character for character in time_value if character.isdigit()).zfill(6)
    if len(digits) != 6:
        raise ValueError("無法解析時間：%s" % time_value)
    return datetime(
        date.year,
        date.month,
        date.day,
        int(digits[:2]),
        int(digits[2:4]),
        int(digits[4:6]),
        tzinfo=TAIPEI_TIMEZONE,
    )


def _required_text(row: Dict[str, object], *keys: str) -> str:
    for key in keys:
        value = _text(row.get(key))
        if value:
            return value
    raise ValueError("缺少必要欄位：%s" % "/".join(keys))


def _required_text_by_normalized_key(row: Dict[str, object], key: str) -> str:
    for raw_key, raw_value in row.items():
        if str(raw_key).strip() == key:
            value = _text(raw_value)
            if value:
                return value
    raise ValueError("缺少必要欄位：%s" % key)


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _optional_decimal(value: object) -> Optional[Decimal]:
    text = _text(value).replace(",", "")
    if text in {"", "-", "--", "N/A", "null", "None"}:
        return None
    try:
        parsed = Decimal(text)
        if not parsed.is_finite():
            raise ValueError("數字必須為有限值：%s" % value)
        return parsed
    except InvalidOperation:
        raise ValueError("無法解析數字：%s" % value)


def _decimal_text(value: Optional[Decimal]) -> Optional[str]:
    return str(value) if value is not None else None


def _require_aware_datetime(value: str, field: str) -> datetime:
    return parse_aware_time(value, field, to_utc=False)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
