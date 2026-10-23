"""以官方累計損益表的稅前淨利核對 FinMind 現金流量表，轉成可放進 Snapshot 的現金流文件。

FinMind 現金流量表是資料商來源，不是官方。只有同一期間的累計稅前淨利與官方損益表
``profit_before_tax`` 完全相符時才轉成 ``cash_flow_statement`` 文件；不符、缺期間、股票誤配、
同一時間出現兩個不同數值或業別不支援時逐檔略過並記警告，不填補假值。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from etf_agent.core import canonical_json, parse_aware_time, parse_decimal

from .corporate_contracts import CorporateRecord, SourceDocument
from .supplemental_sources import FINMIND_ENDPOINT


CASH_FLOW_SOURCE = "FINMIND_VERIFIED_CASH_FLOW"
CASH_FLOW_DOCUMENT_TYPE = "cash_flow_statement"
CASH_FLOW_DATASET = "TaiwanStockCashFlowsStatement"
MAPPING_VERSION = "finmind-cash-flow-pretax-anchor-v1"
SUPPORTED_INDUSTRIES = ("ci", "mim")
# (FinMind type, origin_name)；同時比對兩者，避免同名欄位在不同公司代表不同項目。
FIELDS = {
    "operating_cash_flow": ("CashFlowsFromOperatingActivities", "營業活動之淨現金流入（流出）"),
    "capital_expenditure": ("PropertyAndPlantAndEquipment", "取得不動產、廠房及設備"),
    "pretax_income": ("IncomeBeforeIncomeTaxFromContinuingOperations", "繼續營業單位稅前淨利（淨損）"),
}
REQUIRED_FIELDS = ("operating_cash_flow", "pretax_income")


def _latest_anchors(connection: sqlite3.Connection, symbols: List[str]) -> List[Dict[str, object]]:
    """每檔最新一期、最新版本的官方累計損益表稅前淨利。"""
    rows = connection.execute(
        """
        SELECT s.symbol, s.industry, s.fiscal_year, s.fiscal_quarter, s.period_end, s.period_kind,
               s.currency, d.source, d.external_id, d.available_at, d.content_sha256,
               f.value AS profit_before_tax, f.unit_multiplier, f.value_status
        FROM financial_statements s
        JOIN source_documents d ON d.id = s.document_id
        JOIN financial_statement_facts f ON f.document_id = s.document_id AND f.metric_key = 'profit_before_tax'
        WHERE s.statement_type = 'income_statement'
        ORDER BY s.symbol, s.fiscal_year DESC, s.fiscal_quarter DESC, d.available_at DESC
        """
    ).fetchall()
    anchors: Dict[str, Dict[str, object]] = {}
    wanted = set(symbols)
    for row in rows:
        if row["symbol"] in wanted and row["symbol"] not in anchors and str(row["source"]).startswith(("TWSE", "TPEX")):
            anchors[row["symbol"]] = dict(row)
    return [anchors[symbol] for symbol in sorted(anchors)]


def _fact_rows(connection: sqlite3.Connection, code: str, period_end: str) -> List[Dict[str, object]]:
    types = tuple(field for field, _ in FIELDS.values())
    rows = connection.execute(
        "SELECT stock_id, period_end, fact_type, origin_name, value, available_at, content_sha256, raw_payload_id "
        "FROM supplemental_facts WHERE dataset = ? AND stock_id = ? AND period_end = ? AND fact_type IN (%s) "
        "ORDER BY fact_type, available_at, content_sha256" % ",".join("?" * len(types)),
        (CASH_FLOW_DATASET, code, period_end, *types),
    ).fetchall()
    return [dict(row) for row in rows]


def _pick(rows: List[Mapping[str, object]], field: str, origin: str, observed: datetime) -> Optional[Mapping[str, object]]:
    """同一欄位取觀測時間前最新的一筆；最新時間有兩個不同數值時拒絕。"""
    matching = [row for row in rows if row["fact_type"] == field and row["origin_name"] == origin
                and parse_aware_time(row["available_at"], "available_at") <= observed]
    if not matching:
        return None
    latest = max(parse_aware_time(row["available_at"], "available_at") for row in matching)
    values = {str(row["value"]) for row in matching if parse_aware_time(row["available_at"], "available_at") == latest}
    if len(values) > 1:
        raise ValueError("%s 同一時間有多個不同數值" % field)
    return next(row for row in reversed(matching) if parse_aware_time(row["available_at"], "available_at") == latest)


def verified_cash_flow_record(anchor: Mapping[str, object], rows: List[Mapping[str, object]], observed_at: str) -> CorporateRecord:
    """稅前淨利與官方累計損益表相符才建立文件。

    文件的發布、可得與抓取時間一律是本次核對的觀測時間，不回填到較早的快照；官方基準與
    FinMind 事實各自的時間只留在內容中供稽核。
    """
    observed = parse_aware_time(observed_at, "observed_at")
    symbol = str(anchor["symbol"])
    code = symbol.split(".")[0]
    if (anchor["industry"] not in SUPPORTED_INDUSTRIES or anchor["period_kind"] != "cumulative_to_quarter"
            or anchor["currency"] != "TWD" or anchor["value_status"] != "provided"):
        raise ValueError("%s 缺少支援業別的官方累計稅前淨利" % symbol)
    if parse_aware_time(anchor["available_at"], "anchor.available_at") > observed:
        raise ValueError("%s 官方核對基準晚於觀測時間" % symbol)
    if any(row["stock_id"] != code or row["period_end"] != anchor["period_end"] for row in rows):
        raise ValueError("%s 現金流資料股票或期間誤配" % symbol)
    picked: Dict[str, Optional[Mapping[str, object]]] = {
        key: _pick(rows, field, origin, observed) for key, (field, origin) in FIELDS.items()
    }
    missing = [key for key in REQUIRED_FIELDS if picked[key] is None]
    if missing:
        raise ValueError("%s %s 缺少現金流欄位：%s" % (symbol, anchor["period_end"], "、".join(missing)))
    official = parse_decimal(anchor["profit_before_tax"], "profit_before_tax", reject_bool=True) * int(anchor["unit_multiplier"])
    pretax = parse_decimal(picked["pretax_income"]["value"], "pretax_income", reject_bool=True)
    if pretax != official:
        raise ValueError("%s 現金流量表稅前淨利 %s 與官方累計 %s 不一致" % (symbol, pretax, official))
    used = [row for row in picked.values() if row is not None]
    available_at = observed.isoformat()
    facts = {
        key: {"value": str(parse_decimal(row["value"], key, reject_bool=True)), "source_field": FIELDS[key][0],
              "origin_name": FIELDS[key][1], "value_status": "provided"} if row is not None else
             {"value": None, "source_field": None, "origin_name": FIELDS[key][1], "value_status": "not_reported"}
        for key, row in picked.items()
    }
    body = canonical_json({
        "symbol": symbol, "fiscal_year": int(anchor["fiscal_year"]), "fiscal_quarter": int(anchor["fiscal_quarter"]),
        "period_end": anchor["period_end"], "period_kind": "cumulative_to_quarter", "currency": "TWD",
        "unit_multiplier": 1, "facts": facts,
        "official_anchor": {"source": anchor["source"], "external_id": anchor["external_id"],
                            "content_sha256": anchor["content_sha256"], "available_at": anchor["available_at"],
                            "profit_before_tax_twd": str(official)},
        "fact_rows": [{"fact_type": row["fact_type"], "origin_name": row["origin_name"], "value": row["value"],
                       "available_at": row["available_at"], "content_sha256": row["content_sha256"],
                       "raw_payload_id": row["raw_payload_id"]} for row in used],
        "mapping_version": MAPPING_VERSION, "timestamp_basis": "observed_at_verification",
        "scope_limit": "FinMind 累計現金流量表；稅前淨利與官方累計損益表相符，其餘欄位未逐項對官方核對。",
    })
    document = SourceDocument(
        CASH_FLOW_SOURCE, "%s:%s:Q%s" % (symbol, anchor["fiscal_year"], anchor["fiscal_quarter"]),
        CASH_FLOW_DOCUMENT_TYPE, "%s %s Q%s 累計現金流量（稅前淨利已核對官方）" % (symbol, anchor["fiscal_year"], anchor["fiscal_quarter"]),
        body, FINMIND_ENDPOINT, available_at, available_at, available_at, symbol,
        "FinMind 累計現金流量表；稅前淨利與官方累計損益表完全相符。非官方來源。",
    )
    return CorporateRecord(document)


class VerifiedCashFlowProvider:
    """從本機資料庫讀官方損益表與 FinMind 現金流候選，核對後轉成文件；不連網。"""

    source = CASH_FLOW_SOURCE
    url = FINMIND_ENDPOINT

    def __init__(self, database_path: Path, symbols: List[str]):
        self.database_path = Path(database_path).resolve()
        self.symbols = sorted(symbols)

    def fetch(self) -> Tuple[str, str]:
        observed_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            captures = [
                {"anchor": anchor, "rows": _fact_rows(connection, str(anchor["symbol"]).split(".")[0], str(anchor["period_end"]))}
                for anchor in _latest_anchors(connection, self.symbols)
            ]
        return canonical_json({"observed_at": observed_at, "captures": captures}), observed_at

    def parse(self, payload: str, fetched_at: str):
        decoded = json.loads(payload)
        records, warnings = [], []
        for capture in decoded["captures"]:
            try:
                records.append(verified_cash_flow_record(capture["anchor"], capture["rows"], decoded["observed_at"]))
            except ValueError as error:
                warnings.append(str(error))
        return records, warnings, sum(len(capture["rows"]) for capture in decoded["captures"])
