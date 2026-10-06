"""以官方當期金額核對 FinMind 單季口徑，補足去年同期累計財報。"""

import calendar
import json
from datetime import date, datetime, timezone
from decimal import Decimal

from etf_agent.core import canonical_json, canonical_sha256, parse_aware_time, parse_decimal
from .corporate import CorporateRecord, FinancialStatement, FinancialStatementFact, SourceDocument, OfficialCorporateProvider
from .supplemental_sources import FinMindFundamentalProvider, FINMIND_ENDPOINT


FIELDS = {
    "revenue": ("Revenue", "營業收入"),
    "operating_profit": ("OperatingIncome", "營業利益（損失）"),
    "net_income": ("IncomeAfterTaxes", "本期淨利（淨損）"),
}
MAPPING_VERSION = "finmind-official-anchor-ytd-v1"


class OfficialCompanyProfileProvider:
    """保存官方產業代碼及原始列；不以代碼自行猜產業名稱或同業基準。"""

    source = "TWSE_COMPANY_PROFILE"
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"

    def fetch(self):
        return OfficialCorporateProvider(self.source, self.url, "TWSE", "material_event", retries=1).fetch()

    def parse(self, payload, fetched_at):
        parse_aware_time(fetched_at, "fetched_at")
        rows = json.loads(payload)
        if not isinstance(rows, list) or not rows:
            raise ValueError("官方公司基本資料為空或格式錯誤")
        seen, records = set(), []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("官方公司基本資料列必須是物件")
            code, industry = str(row.get("公司代號", "")).strip(), str(row.get("產業別", "")).strip()
            if not code or not industry:
                continue
            if code in seen:
                raise ValueError("官方公司基本資料股票重複")
            seen.add(code)
            symbol = code + ".TW"
            body = canonical_json({"company_code": code, "industry_code": industry,
                                   "raw_row": row, "timestamp_basis": "observed_at_fetch",
                                   "source_published_at": None})
            records.append(CorporateRecord(SourceDocument(
                self.source, "company-profile:" + code, "company_profile", symbol + " 官方產業代碼",
                body, self.url, fetched_at, fetched_at, fetched_at, symbol,
                "官方公司基本資料產業代碼；抓取時間僅代表觀測時間，未提供同業比較標準。")))
        return records, [], len(rows)


def verified_prior_record(anchor, raw, fetched_at, decision_cutoff):
    """缺季、股票誤配、金額／口徑衝突或未來資料均拒絕，不加總 EPS。"""
    observed = parse_aware_time(fetched_at, "fetched_at")
    if observed > parse_aware_time(decision_cutoff, "decision_cutoff"):
        raise ValueError("補抓資料晚於截止時間，不能回填歷史快照")
    statement = anchor.financial_statement
    if (statement is None or statement.industry not in {"ci", "mim"}
            or statement.statement_type != "income_statement"
            or statement.period_kind != "cumulative_to_quarter"
            or statement.currency != "TWD"
            or not anchor.document.source.startswith(("TWSE", "TPEX"))):
        raise ValueError("缺少支援業別的官方累計損益表核對基準")
    if parse_aware_time(anchor.document.available_at, "anchor.available_at") > parse_aware_time(decision_cutoff, "decision_cutoff"):
        raise ValueError("官方核對基準晚於截止時間")
    if raw.get("status") != 200 or not isinstance(raw.get("data"), list):
        raise ValueError("FinMind 財報回應無效")
    symbol = statement.symbol
    code = symbol.split(".")[0]
    indexed = {}
    for row in raw["data"]:
        if not isinstance(row, dict) or row.get("stock_id") != code:
            raise ValueError("FinMind 財報股票誤配")
        key = (row.get("date"), row.get("type"))
        if key in indexed:
            raise ValueError("FinMind 財報期間欄位重複")
        indexed[key] = row
    anchor_facts = {fact.metric_key: fact for fact in statement.facts}
    totals = {}
    proof = {}
    for year in (statement.fiscal_year, statement.fiscal_year - 1):
        totals[year] = {}
        for metric, (field, origin) in FIELDS.items():
            values = []
            for quarter in range(1, statement.fiscal_quarter + 1):
                month = quarter * 3
                end = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
                row = indexed.get((end, field))
                if row is None or row.get("origin_name") != origin:
                    raise ValueError("%s 缺少 %s %s 可比單季資料" % (symbol, end, field))
                values.append(parse_decimal(row.get("value"), field, reject_bool=True))
            totals[year][metric] = sum(values, Decimal(0))
            if year == statement.fiscal_year:
                fact = anchor_facts.get(metric)
                if fact is None or fact.value_status != "provided" or fact.currency != "TWD":
                    raise ValueError("官方基準缺少 %s" % metric)
                expected = parse_decimal(fact.value, metric) * fact.unit_multiplier
                if totals[year][metric] != expected:
                    raise ValueError("%s %s 單季合計與官方累計金額不一致" % (symbol, metric))
                proof[metric] = str(expected)
    year = statement.fiscal_year - 1
    month = statement.fiscal_quarter * 3
    end = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    body = canonical_json({
        "raw_response": raw, "raw_response_sha256": canonical_sha256(raw),
        "official_anchor": {"source": anchor.document.source,
                            "external_id": anchor.document.external_id,
                            "body_sha256": canonical_sha256(anchor.document.body),
                            "raw_body": anchor.document.body,
                            "available_at": anchor.document.available_at},
        "matched_current_ytd_twd": proof, "mapping_version": MAPPING_VERSION,
        "source_published_at": None, "timestamp_basis": "observed_at_fetch",
        "scope_limit": "當期金額相符不證明去年未重編；未取得可用時保留 reporting_scope=unknown",
    })
    document = SourceDocument(
        "FINMIND_VERIFIED_PRIOR", "%s:%s:Q%s" % (symbol, year, statement.fiscal_quarter),
        "financial_statement", "%s %s Q%s 累計損益（核對後補抓）" % (symbol, year, statement.fiscal_quarter),
        body, FINMIND_ENDPOINT, fetched_at, fetched_at, fetched_at, symbol,
        "FinMind 單季原值合計；當期三項金額與官方累計表完全相符。非官方來源。",
    )
    prior = FinancialStatement(
        symbol, "income_statement", statement.industry, year, statement.fiscal_quarter,
        "%s-01-01" % year, end, "cumulative_to_quarter", "unknown", "TWD", 1,
        observed.date().isoformat(), None, MAPPING_VERSION,
        tuple(FinancialStatementFact(metric, "SUM_Q1_Q%s:%s" % (statement.fiscal_quarter, field),
                                    totals[year][metric], "provided", "TWD", 1)
              for metric, (field, _) in FIELDS.items()),
    )
    return CorporateRecord(document, financial_statement=prior)


class FundamentalRepairProvider:
    """每檔最多一次補抓；保存原始回應，衝突逐檔降級，不覆寫官方當期。"""

    source = "FINMIND_VERIFIED_PRIOR"
    url = FINMIND_ENDPOINT

    def __init__(self, anchors, decision_cutoff=None, provider=None):
        self.anchors = list(anchors)
        self.decision_cutoff = decision_cutoff
        self.provider = provider or FinMindFundamentalProvider()
        if decision_cutoff is not None:
            parse_aware_time(decision_cutoff, "decision_cutoff")

    def fetch(self):
        captures = []
        failures = []
        for anchor in self.anchors:
            statement = anchor.financial_statement
            if statement is None or statement.industry not in {"ci", "mim"}:
                continue
            try:
                raw = self.provider.fetch("TaiwanStockFinancialStatements", statement.symbol.split(".")[0],
                                          date(statement.fiscal_year - 1, 1, 1), date.fromisoformat(statement.period_end))
            except (ValueError, RuntimeError) as error:
                failures.append("%s：%s；停止本輪補抓" % (statement.symbol, error))
                break
            captures.append({"symbol": statement.symbol, "raw": json.loads(raw),
                             "fetched_at": datetime.now(timezone.utc).isoformat()})
        return canonical_json({"captures": captures, "failures": failures}), datetime.now(timezone.utc).isoformat()

    def parse(self, payload, fetched_at):
        decoded = json.loads(payload)
        captures = decoded["captures"]
        anchors = {anchor.document.symbol: anchor for anchor in self.anchors}
        records, warnings = [], list(decoded.get("failures", []))
        for capture in captures:
            try:
                records.append(verified_prior_record(anchors[capture["symbol"]], capture["raw"],
                                                     capture["fetched_at"], self.decision_cutoff or fetched_at))
            except ValueError as error:
                warnings.append("%s：%s" % (capture["symbol"], error))
        return records, warnings, sum(len(item["raw"].get("data", [])) for item in captures)
