"""評測考卷的唯讀帳本快照：只沿封存 Policy 的 head 取歷史，不讀今日 latest。"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Dict, Mapping, Optional

from etf_agent.core import content_sha256, parse_aware_time, parse_decimal
from etf_agent.decision import PortfolioDecisionApplicationService
from etf_agent.decision.drawdown import history_reference
from etf_agent.decision.exposure import held_since_from_history

from .contracts import EvaluationError


FIELDS = {
    "schema_version", "account_id", "snapshot_id", "decision_cutoff", "history_ref",
    "nav_history", "holdings_history", "content_sha256",
}


def validate_ledger_context(context: object, bundle: Mapping[str, object]) -> Dict[str, object]:
    """重驗時間、帳戶、雜湊、同鏈錨點與持股；缺少或不符一律拒絕。"""
    if not isinstance(context, Mapping) or set(context) != FIELDS or context.get("schema_version") != "1.0":
        raise EvaluationError("評測帳本快照欄位或版本不符，請重建評測集")
    if context.get("content_sha256") != content_sha256(context):
        raise EvaluationError("評測帳本快照雜湊不符，請重建評測集")
    account = bundle["account_snapshot"]
    if (context.get("account_id") != account["account_id"] or context.get("snapshot_id") != bundle["snapshot_id"]
            or context.get("decision_cutoff") != bundle["decision_cutoff"]):
        raise EvaluationError("評測帳本快照與考卷帳戶、Snapshot 或 cutoff 不一致")
    nav, holdings = context.get("nav_history"), context.get("holdings_history")
    if not isinstance(nav, list) or not nav or not isinstance(holdings, list) or len(nav) != len(holdings):
        raise EvaluationError("評測帳本 NAV 與持股歷史必須是同一條非空鏈")
    cutoff = parse_aware_time(bundle["decision_cutoff"], "decision_cutoff", error=EvaluationError)
    seen = set()
    for index, (nrow, hrow) in enumerate(zip(nav, holdings)):
        if (not isinstance(nrow, Mapping) or set(nrow) != {"run_id", "as_of", "nav"}
                or not isinstance(hrow, Mapping) or set(hrow) != {"run_id", "as_of", "symbols"}):
            raise EvaluationError("評測帳本歷史第 %d 列欄位不符" % index)
        run_id = nrow["run_id"]
        if not isinstance(run_id, str) or not run_id or run_id in seen or run_id != hrow["run_id"] or nrow["as_of"] != hrow["as_of"]:
            raise EvaluationError("評測帳本 NAV 與持股歷史錨點不一致或重複")
        seen.add(run_id)
        moment = parse_aware_time(nrow["as_of"], "ledger_context.as_of", error=EvaluationError)
        # 帳本順序由已驗證的 parent 鏈與 history_ref 決定；rebase 的估值 as_of 可以早於前一狀態，不能擅自重排。
        if moment > cutoff:
            raise EvaluationError("評測帳本歷史時間晚於 cutoff")
        if parse_decimal(nrow["nav"], "ledger_context.nav", error=EvaluationError, reject_bool=True) <= 0:
            raise EvaluationError("評測帳本 NAV 必須大於零")
        symbols = hrow["symbols"]
        if (not isinstance(symbols, list) or any(not isinstance(s, str) or not s or s != s.upper() for s in symbols)
                or symbols != sorted(set(symbols))):
            raise EvaluationError("評測帳本持股必須是排序且不重複的大寫代號")
    if context.get("history_ref") != history_reference(nav):
        raise EvaluationError("評測帳本快照與封存 Policy 的 history_ref 不一致")
    held = [str(p["symbol"]).upper() for p in account["positions"] if int(p.get("shares", 0)) > 0]
    try:
        held_since_from_history(holdings, held)
    except ValueError as error:
        raise EvaluationError("評測帳本持股與考卷不同步：%s" % error) from error
    return copy.deepcopy(dict(context))


def capture_ledger_context(root: Path, reference_dir: Path, bundle: Mapping[str, object]) -> Optional[Dict[str, object]]:
    """建考卷時凍結已驗證帳本；舊 run 沒有 history_ref 時不猜 head、不回填今日歷史。"""
    path = reference_dir / "policy.json"
    if not path.is_file():
        return None
    try:
        policy = PortfolioDecisionApplicationService.read_json(path, " 評測參考 Policy")
        control = policy.get("drawdown_control")
        ref = control.get("history_ref") if isinstance(control, Mapping) else None
        if ref is None:
            return None
        if not isinstance(ref, Mapping) or not isinstance(ref.get("head_run_id"), str) or not ref["head_run_id"]:
            raise EvaluationError("評測參考 Policy 的帳本 history_ref 無效")
        from etf_agent.virtual_account import VirtualAccountRepository

        account_id = bundle["account_snapshot"]["account_id"]
        repository = VirtualAccountRepository(root / "artifacts" / "ledger", account_id)
        context = {
            "schema_version": "1.0", "account_id": account_id, "snapshot_id": bundle["snapshot_id"],
            "decision_cutoff": bundle["decision_cutoff"], "history_ref": dict(ref),
            "nav_history": repository.nav_history(ref["head_run_id"]),
            "holdings_history": repository.holdings_history(ref["head_run_id"]),
        }
        context["content_sha256"] = content_sha256(context)
        return validate_ledger_context(context, bundle)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise EvaluationError("無法封存考卷 %s 的帳本快照：%s" % (reference_dir.name, error)) from error
