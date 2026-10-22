import json
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional, Tuple

from etf_agent.core import canonical_json
from .universe import normalize_symbol
from .corporate_contracts import CorporateRecord, CorporateResponse, FINANCIAL_MAPPING_VERSION, FinancialStatement, MonthlyRevenue, SourceDocument, TAIPEI_TIMEZONE
from .corporate_parsing import _financial_facts, _financial_period, _is_empty_financial_placeholder, _optional_decimal, _parse_quarter, _parse_roc_date, _parse_roc_datetime, _parse_roc_month, _parse_roc_year, _require_aware_datetime, _required_text, _required_text_by_normalized_key, _text, _utc_iso


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
        mapping_version: str = FINANCIAL_MAPPING_VERSION,
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
        canonical_body = canonical_json(row)
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
            body=canonical_json(row),
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
                    item.get("mapping_version", FINANCIAL_MAPPING_VERSION)
                ),
                timeout_seconds=timeout_seconds,
                retries=int(config.get("retries", 3)),
                user_agent=user_agent,
            )
            for item in sources
        ]
