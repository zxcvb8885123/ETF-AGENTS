"""將同一快照的財報接到事件解讀，保留原值、單位、期間與引用。"""

from typing import Mapping

from etf_agent.core import decimal_string, parse_decimal, parse_aware_time
from etf_agent.fundamentals import FundamentalSnapshotTools
from etf_agent.fundamentals.contracts import FundamentalToolError
from .contracts import DecisionToolError


def event_financial_context(bundle: Mapping[str, object]) -> dict:
    documents = bundle["snapshot"].get("documents", [])
    financial = [d for d in documents if d.get("document_type") == "financial_statement"]
    if not financial:
        return {}
    try:
        FundamentalSnapshotTools(bundle["snapshot"])
    except FundamentalToolError as error:
        raise DecisionToolError("事件財報輸入驗證失敗：" + str(error)) from error
    chosen = {}
    for document in financial:
        statement = document["financial_statement"]
        key = (document["symbol"], statement["statement_type"])
        rank = (statement["fiscal_year"], statement["fiscal_quarter"],
                parse_aware_time(document["available_at"], "available_at", error=DecisionToolError), str(document["document_id"]))
        if key not in chosen or rank > chosen[key][0]:
            chosen[key] = (rank, document)
    result = {}
    for (symbol, _), (_, document) in sorted(chosen.items()):
        statement = document["financial_statement"]
        result.setdefault(symbol, []).append({
            "evidence_id": document["source_evidence_id"], "source": document["source"],
            "available_at": document["available_at"],
            **{key: statement[key] for key in ("statement_type", "fiscal_year", "fiscal_quarter", "period_start", "period_end", "period_kind", "reporting_scope", "currency", "unit_multiplier")},
            "facts": [{"fact_key": key, "value": decimal_string(parse_decimal(fact["value"], key, error=DecisionToolError)),
                       "currency": fact["currency"], "unit_multiplier": fact["unit_multiplier"],
                       "source_field": fact["source_field"]}
                      for key, fact in sorted(statement["facts"].items()) if fact["value_status"] == "provided"],
        })
    return result
