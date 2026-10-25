"""從決策封存建立同源閱讀資料，不生成新投資判斷。"""

from copy import deepcopy
from pathlib import Path
from etf_agent.core import canonical_sha256
from etf_agent.decision import DecisionRepository
from .contracts import VERSION
from .provider import verify_inputs
from .tools import trade_date


def build_report(artifacts, run_id):
    verify_inputs(artifacts)
    a = deepcopy(artifacts)
    b, decision = a["decision_input"], a["decision"]
    accepted = decision["status"] in ("approved", "no_trade")
    before = {p["symbol"]: p for p in b["account_snapshot"]["positions"]}
    after = {p["symbol"]: p for p in (decision["portfolio"] or {}).get("positions", [])}
    orders = {o["symbol"]: o for o in decision["orders"]}
    rows = []
    intents = {item["symbol"]: item for item in a["intent"]["items"]}
    for symbol in sorted(set(before) | set(after) | set(intents) | set(orders)):
        item = intents.get(symbol)
        order = orders.get(symbol)
        rows.append({"symbol": symbol, "before": before.get(symbol), "after": after.get(symbol) if accepted else None,
            "intent": item, "order": order,
            "final_action": (order["side"] if order else "no_order") if accepted else "rejected"})
    report = {
        "schema_version": VERSION,
        "provenance": {"decision_run_id": run_id, "snapshot_id": b["snapshot_id"],
            "decision_manifest_sha256": DecisionRepository(Path(".")).build_manifest(run_id, a)["manifest_sha256"],
            "decision_cutoff": b["decision_cutoff"], "trade_date": trade_date(b),
            "input_hashes": {k: canonical_sha256(v) for k, v in sorted(a.items())},
            "builder_version": VERSION},
        "decision_status": decision["status"], "account_before": b["account_snapshot"],
        "portfolio_after_estimate": decision["portfolio"], "orders": decision["orders"],
        "order_totals": {k: v for k, v in a["proposal"]["order_proposal"].items() if k != "orders"} if accepted else None,
        "decision_rows": rows,
        "risk_summary": {"cash_stance": a["team_inputs"]["cash_stance"], "review": a["risk_review"],
            "guard": a["guard"], "scenario": a["scenario"],
            "revisions": [{"proposal_id": entry["proposal"]["proposal_id"], "review": entry["risk_review"]}
                for entry in a["revision_history"]["entries"]],
            "constraint_flags": a["proposal"].get("constraint_flags", []),
            "revision_count": decision["revision_count"]},
        "analyst_reports": a["team_inputs"]["analyst_reports"],
        "research_claims": a["debate"],
        # 已核准的個股新聞不在 Snapshot 內，另附在引用清單後；舊輸入沒有這個鍵，清單不變。
        "evidence_index": [*b["snapshot"]["source_evidence"], *(
            {"evidence_id": item["evidence_id"], "source": item["candidate"]["source"], "symbol": item["symbol"],
             "publisher": item["candidate"]["publisher"], "title": item["candidate"]["title"],
             "link": item["candidate"]["link"], "available_at": item["available_at"],
             "content_sha256": item["content_sha256"]}
            for item in b.get("company_news_input", {}).get("items", []))],
        "reference_prices": b["snapshot"]["latest_prices"],
        "errors": decision["errors"],
    }
    report["report_id"] = "report:" + canonical_sha256(report)[:24]
    report["content_sha256"] = canonical_sha256(report)
    return report
