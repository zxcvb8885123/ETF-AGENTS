import hashlib
import json
import sqlite3
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence, Tuple

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
class CorporateRecord:
    document: SourceDocument
    monthly_revenue: Optional[MonthlyRevenue] = None


@dataclass(frozen=True)
class CorporateResponse:
    source: str
    endpoint: str
    payload: str
    fetched_at: str
    records: List[CorporateRecord]
    warnings: List[str]
    fetched_rows: int


@dataclass(frozen=True)
class CorporateCollectionResult:
    run_id: str
    fetched_rows: int
    stored_documents: int
    duplicate_documents: int
    warning_count: int
    warnings: List[str]


class OfficialCorporateProvider:
    def __init__(
        self,
        source: str,
        url: str,
        market: str,
        document_type: str,
        timeout_seconds: int = 30,
        user_agent: str = "ETF-Agent-AICUP-2026/0.1",
    ):
        if document_type not in {"monthly_revenue", "material_event"}:
            raise ValueError("不支援的公司資料類型：%s" % document_type)
        self.source = source
        self.url = url
        self.market = market
        self.document_type = document_type
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def fetch(self) -> Tuple[str, str]:
        request = urllib.request.Request(
            self.url,
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = response.read().decode("utf-8-sig")
        return payload, datetime.now(timezone.utc).isoformat()

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
            try:
                if self.document_type == "monthly_revenue":
                    records.append(self._parse_monthly_revenue(row, fetched_at))
                else:
                    records.append(self._parse_material_event(row, fetched_at))
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
            responses = [provider.fetch_and_parse() for provider in self.providers]
            allowed = {item.symbol for item in universe}
            warnings: List[str] = []
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
        return Decimal(text)
    except InvalidOperation:
        raise ValueError("無法解析數字：%s" % value)


def _decimal_text(value: Optional[Decimal]) -> Optional[str]:
    return str(value) if value is not None else None


def _require_aware_datetime(value: str, field: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("%s 必須包含時區" % field)
    return parsed


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
