"""基本面「現金流」：從同一快照的已核對現金流文件確定性計算，LLM 只讀結果。

每檔取最新一期 ``cash_flow_statement`` 文件，並以同快照的官方累計損益表稅前淨利再核對一次；
對不上、缺官方基準或沒有文件時輸出 unavailable 與原因，不填補假值。
"""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Mapping, Optional

from etf_agent.core import decimal_string, parse_aware_time, parse_decimal

from .contracts import DecisionToolError


CASH_FLOW_DOCUMENT_TYPE = "cash_flow_statement"
CASH_FLOW_LIMITATION = "現金流來自 FinMind（非官方）；只有稅前淨利與官方累計損益表核對相符，其餘欄位未逐項核對。"
_PCT = Decimal("0.01")


def _decimal(value: object, field: str) -> Decimal:
    return parse_decimal(value, field, error=DecisionToolError, reject_bool=True)


def _official_pretax(documents: List[Mapping[str, object]]) -> Dict[tuple, Decimal]:
    """同快照官方累計損益表的稅前淨利（元），以 (股票, 年, 季) 為鍵；同期多版本取最新可得者。"""
    chosen: Dict[tuple, tuple] = {}
    for document in documents:
        statement = document.get("financial_statement")
        if (document.get("document_type") != "financial_statement" or not isinstance(statement, Mapping)
                or statement.get("statement_type") != "income_statement"
                or not str(document.get("source", "")).startswith(("TWSE", "TPEX"))):
            continue
        fact = (statement.get("facts") or {}).get("profit_before_tax") or {}
        if fact.get("value_status") != "provided":
            continue
        key = (document["symbol"], int(statement["fiscal_year"]), int(statement["fiscal_quarter"]))
        rank = parse_aware_time(document["available_at"], "available_at", error=DecisionToolError)
        if key not in chosen or rank > chosen[key][0]:
            chosen[key] = (rank, _decimal(fact["value"], "profit_before_tax") * int(fact["unit_multiplier"]))
    return {key: value for key, (_, value) in chosen.items()}


def _fact(facts: Mapping[str, object], key: str) -> Optional[Decimal]:
    fact = facts.get(key) or {}
    if fact.get("value_status") != "provided":
        return None
    return _decimal(fact.get("value"), key)


def cash_flow_context(bundle: Mapping[str, object]) -> Dict[str, Dict[str, object]]:
    """回傳 {股票: 現金流摘要}；只含有現金流文件的股票，其餘由呼叫端標為資料缺口。"""
    documents = [d for d in bundle["snapshot"].get("documents", []) if isinstance(d, Mapping)]
    latest: Dict[str, tuple] = {}
    for document in documents:
        if document.get("document_type") != CASH_FLOW_DOCUMENT_TYPE:
            continue
        try:
            body = json.loads(str(document.get("body", "")))
        except ValueError as error:
            raise DecisionToolError("現金流文件內容無法解析：%s" % document.get("source_evidence_id")) from error
        if not isinstance(body, Mapping) or body.get("symbol") != document.get("symbol"):
            raise DecisionToolError("現金流文件股票誤配：%s" % document.get("source_evidence_id"))
        rank = (int(body["fiscal_year"]), int(body["fiscal_quarter"]),
                parse_aware_time(document["available_at"], "available_at", error=DecisionToolError))
        symbol = str(document["symbol"]).upper()
        if symbol not in latest or rank > latest[symbol][0]:
            latest[symbol] = (rank, document, body)
    official = _official_pretax(documents)
    result: Dict[str, Dict[str, object]] = {}
    for symbol, (_, document, body) in sorted(latest.items()):
        period = "%sQ%s" % (body["fiscal_year"], body["fiscal_quarter"])
        base = {"period": period, "period_end": body.get("period_end"), "period_kind": body.get("period_kind"),
                "evidence_ids": [document["source_evidence_id"]], "limitations": [CASH_FLOW_LIMITATION]}
        facts = body.get("facts") or {}
        operating, capex, pretax = (_fact(facts, key) for key in ("operating_cash_flow", "capital_expenditure", "pretax_income"))
        anchor = official.get((document["symbol"], int(body["fiscal_year"]), int(body["fiscal_quarter"])))
        if operating is None or pretax is None:
            result[symbol] = {**base, "status": "unavailable", "reason": "現金流文件缺少營業活動現金流或稅前淨利"}
            continue
        if anchor is None or anchor != pretax:
            result[symbol] = {**base, "status": "unavailable",
                              "reason": "同快照沒有可核對的官方 %s 稅前淨利，或金額不符" % period}
            continue
        free = operating + capex if capex is not None else None
        ratio = (operating / pretax * 100).quantize(_PCT, rounding=ROUND_HALF_UP) if pretax > 0 else None
        result[symbol] = {
            **base, "status": "available", "currency": "TWD",
            "operating_cash_flow": decimal_string(operating),
            "capital_expenditure": decimal_string(capex) if capex is not None else None,
            "free_cash_flow": decimal_string(free) if free is not None else None,
            "operating_cash_flow_to_pretax_income_pct": decimal_string(ratio) if ratio is not None else None,
            "notes": [note for note, missing in (
                ("未提供取得不動產、廠房及設備，無法計算自由現金流。", capex is None),
                ("稅前淨利不為正，營業現金流對稅前淨利比率不適用。", pretax <= 0),
            ) if missing],
        }
    return result
