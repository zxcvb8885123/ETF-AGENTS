"""便宜版回測：多日滾動回放，零 token。

依時間順序逐日讀已封存 Decision run 的 Agent 輸出（交易意圖／信心、現金姿態、多空辯論），用指定的
策略樣板以確定性程式重算 policy、權重、張數與訂單，再依帳本口徑（下一交易日官方成交均價、費稅、
整張、T+2 交割）成交並結轉到下一天。回放用自己的模擬帳本：換 Policy 後持股會和實際不同，不能讀實際帳本。

- 價格只用各日 cutoff 前可得、已定案的版本（封存 Snapshot 內的決策用價）；成交與日終估值只用 ``as_of``
  前抓到的官方未還原列，成交日資料在該日收盤前就抓到視為前視價格並拒絕。
- 封存驗證失敗、缺日（有官方交易日卻沒有決策 run）、缺價（成交日沒有官方列）一律停止或明確標示，不補假值。
- 風險 Agent 的修正循環（revise）需要 Agent，回放不重現：Guard 未通過的日子標為 rejected、不下單。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core import canonical_sha256, content_sha256, parse_aware_time
from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.core.run_layout import list_run_dirs
from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionInputValidator,
    DecisionPolicyValidator,
    DecisionRepository,
    DecisionToolError,
    MomentumEngine,
    ScenarioEngine,
    TradeDecisionValidator,
    apply_trade_decision,
    bind_drawdown,
    build_decision_policy,
    decision_bundle_sha256,
    decision_policy_sha256,
    stance_meets_floor,
)
from etf_agent.decision.allocation import ALLOCATION_ENGINE_VERSION
from etf_agent.decision.finalization import DECISION_ENGINE_VERSION
from etf_agent.virtual_account.daily import OfficialCloseMarket

from .contracts import REPLAY_SCHEMA_VERSION, ReplayError
from .replay_market import TAIPEI, ReplayLedger, ReplayMarket
from .replay_tools import code_version, nav_metrics, policy_template_sha256, tree_sha256
from .service import EVALUATION_ROOT


REPLAY_ROOT = EVALUATION_ROOT / "replays"
# 每日決策鏈以 Decimal 預設精度（28 位）計算並封存；本套件的 answers 經 calibration.tools 會把全域精度改成 40，
# 回放必須鎖回 28 位，動能與配置的重算才會和封存結果逐位一致。
DECISION_DECIMAL_PRECISION = 28
ENGINE_VERSIONS = {"decision": DECISION_ENGINE_VERSION, "allocation": ALLOCATION_ENGINE_VERSION, "replay": REPLAY_SCHEMA_VERSION}
ARCHIVED_FILES = ("decision_input", "intent", "debate", "momentum", "policy", "team_inputs", "decision")
# 報告中這些欄位不進 content_sha256，也不參與重算比對。
VOLATILE_FIELDS = ("replay_id", "generated_at", "content_sha256")
Log = Callable[[str], None]


class ReplayRepository(ImmutableRunStore):
    """回放結果的不可變保存：``artifacts/evaluation/replays/<replay_id>/``。"""

    def __init__(self, root: Path):
        super().__init__(root, schema_version=REPLAY_SCHEMA_VERSION, error=ReplayError)


def _read_json(path: Path, label: str) -> Dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReplayError("%s 無法讀取：%s" % (label, path)) from error
    if not isinstance(value, dict):
        raise ReplayError("%s 必須是 JSON 物件" % label)
    return value


def _time(value: object, field: str) -> datetime:
    return parse_aware_time(value, field, error=ReplayError)


def replay_body_sha256(body: Mapping[str, object]) -> str:
    payload = {key: value for key, value in body.items() if key not in VOLATILE_FIELDS}
    return canonical_sha256(payload)


# ---------------------------------------------------------------- run selection
def list_candidates(decision_root: Path, market: ReplayMarket) -> List[Dict[str, object]]:
    """決策庫中有封存輸入與交易意圖的 run，附 cutoff 與目標時段日（沒有目標時段時取資料庫 cutoff 後第一個官方交易日）。"""
    candidates = []
    for run_dir in list_run_dirs(Path(decision_root)):
        if not (run_dir / "decision_input.json").is_file() or not (run_dir / "intent.json").is_file():
            continue
        bundle = _read_json(run_dir / "decision_input.json", "DecisionInputBundle")
        cutoff = bundle.get("decision_cutoff")
        if not isinstance(cutoff, str):
            raise ReplayError("%s 的 DecisionInputBundle 缺少 decision_cutoff" % run_dir.name)
        session_day = market.session_day(bundle)
        basis = "target_session"
        if session_day is None:
            session_day = market.first_trading_day_after(cutoff)
            basis = "first_official_trading_day"
        if session_day is None:
            session_day = _time(cutoff, "decision_cutoff").astimezone(TAIPEI).date().isoformat()
            basis = "cutoff_date"
        candidates.append({"run_id": run_dir.name, "run_dir": run_dir, "decision_cutoff": cutoff,
                           "session_day": session_day, "session_day_basis": basis})
    return candidates


def select_runs(
    decision_root: Path, market: ReplayMarket, start: Optional[str] = None, end: Optional[str] = None,
    run_ids: Optional[Sequence[str]] = None,
) -> List[Dict[str, object]]:
    """依日期範圍（目標時段日）或指定 run 挑出回放序列；同一天多個 run 取 cutoff 最新者並記錄被略過的。"""
    candidates = list_candidates(decision_root, market)
    if run_ids:
        wanted = list(dict.fromkeys(run_ids))
        by_id = {item["run_id"]: item for item in candidates}
        missing = [run_id for run_id in wanted if run_id not in by_id]
        if missing:
            raise ReplayError("找不到可回放的 Decision run：%s" % "、".join(missing))
        chosen = [dict(by_id[run_id], superseded_run_ids=[]) for run_id in wanted]
    else:
        in_range = [item for item in candidates
                    if (start is None or item["session_day"] >= start) and (end is None or item["session_day"] <= end)]
        by_day: Dict[str, List[Dict[str, object]]] = {}
        for item in in_range:
            by_day.setdefault(str(item["session_day"]), []).append(item)
        chosen = []
        for day in sorted(by_day):
            ordered = sorted(by_day[day], key=lambda item: _time(item["decision_cutoff"], "decision_cutoff"))
            latest = dict(ordered[-1])
            latest["superseded_run_ids"] = [item["run_id"] for item in ordered[:-1]]
            chosen.append(latest)
    if not chosen:
        raise ReplayError("範圍內沒有可回放的 Decision run")
    chosen.sort(key=lambda item: _time(item["decision_cutoff"], "decision_cutoff"))
    for previous, current in zip(chosen, chosen[1:]):
        if _time(current["decision_cutoff"], "decision_cutoff") <= _time(previous["decision_cutoff"], "decision_cutoff"):
            raise ReplayError("回放序列的 cutoff 必須嚴格遞增：%s、%s" % (previous["run_id"], current["run_id"]))
        if current["session_day"] <= previous["session_day"]:
            raise ReplayError("回放序列的目標時段日必須遞增：%s（%s）、%s（%s）" % (
                previous["run_id"], previous["session_day"], current["run_id"], current["session_day"]))
    return chosen


def load_archived_run(decision_root: Path, run_id: str) -> Dict[str, object]:
    """讀一份封存 run：manifest 雜湊、動能重算、交易意圖對封存輸入的重建驗證與 team_inputs 綁定都要通過。"""
    repository = DecisionRepository(Path(decision_root))
    try:
        run_dir = repository.verify(run_id)
    except DecisionToolError as error:
        raise ReplayError("Decision run %s 封存驗證失敗：%s" % (run_id, error)) from error
    files = {}
    for name in ARCHIVED_FILES:
        path = run_dir / ("%s.json" % name)
        if not path.is_file():
            raise ReplayError("Decision run %s 缺少 %s.json" % (run_id, name))
        files[name] = _read_json(path, name)
    bundle = files["decision_input"]
    errors = DecisionInputValidator(bundle).validate()
    if errors:
        raise ReplayError("Decision run %s 的封存輸入驗證失敗：%s" % (run_id, "；".join(errors[:5])))
    try:
        momentum = MomentumEngine(bundle).run()
    except DecisionToolError as error:
        raise ReplayError("Decision run %s 動能重算失敗：%s" % (run_id, error)) from error
    if momentum != files["momentum"]:
        raise ReplayError("Decision run %s 的 MomentumResult 與重算不一致" % run_id)
    team = files["team_inputs"]
    if files["decision"].get("team_inputs_sha256") != canonical_sha256(team):
        raise ReplayError("Decision run %s 的 team_inputs 與 DecisionResult 綁定不一致" % run_id)
    stance = team.get("cash_stance")
    if not isinstance(stance, Mapping) or stance.get("level") is None:
        raise ReplayError("Decision run %s 的 team_inputs 缺少現金姿態" % run_id)
    prior = team.get("prior_decision_context") if isinstance(team.get("prior_decision_context"), Mapping) else None
    try:
        errors = TradeDecisionValidator(bundle, momentum, files["debate"], prior_context=prior).validate(files["intent"])
    except DecisionToolError as error:
        errors = [str(error)]
    if errors:
        raise ReplayError("Decision run %s 的 TradeDecision 重建驗證失敗：%s" % (run_id, "；".join(errors[:5])))
    manifest = _read_json(run_dir / "manifest.json", "manifest")
    return {"run_id": run_id, "bundle": bundle, "intent": files["intent"], "debate": files["debate"],
            "momentum": momentum, "policy": files["policy"], "stance": dict(stance),
            "manifest_sha256": manifest.get("manifest_sha256")}


# ---------------------------------------------------------------- policy binding
def bind_replay_policy(
    template: Mapping[str, object], bundle: Mapping[str, object], archived_policy: Mapping[str, object],
    intent: Mapping[str, object], debate: Mapping[str, object], stance: Mapping[str, object],
    nav_history: Sequence[Mapping[str, object]],
) -> Tuple[Dict[str, object], Dict[str, object]]:
    """以指定樣板重建當日 policy：產業分類沿用封存 policy（當時已驗證），回撤由回放 NAV 歷史計算，
    封存的現金姿態低於回撤下限時由程式提高到下限並標示。"""
    cutoff = str(bundle["decision_cutoff"])
    working = dict(template)
    overridden = False
    if _time(working.get("available_at"), "policy_template.available_at") > _time(cutoff, "decision_cutoff"):
        working["available_at"] = cutoff
        overridden = True
    sectors = {"version": archived_policy.get("sector_classification_version"), "available_at": cutoff,
               "sector_by_symbol": archived_policy.get("sector_by_symbol")}
    try:
        base = build_decision_policy(working, bundle, sectors)
        base = bind_drawdown(base, bundle, list(nav_history))
        base["content_sha256"] = decision_policy_sha256(base)
        floor = (base.get("drawdown_control") or {}).get("stance_floor") if isinstance(base.get("drawdown_control"), Mapping) else None
        level = str(stance.get("level"))
        raised = not stance_meets_floor(level, floor)
        bound_stance = dict(stance, level=str(floor)) if raised else dict(stance)
        policy = apply_trade_decision(base, intent, bound_stance, bundle, debate)
        policy["content_sha256"] = decision_policy_sha256(policy)
    except DecisionToolError as error:
        raise ReplayError("回放 policy 建立失敗：%s" % error) from error
    errors = DecisionPolicyValidator(bundle).validate(policy)
    if errors:
        raise ReplayError("回放 policy 驗證失敗：" + "；".join(errors[:5]))
    notes = {
        "policy_id": policy["policy_id"], "content_sha256": policy["content_sha256"],
        "available_at_overridden": overridden, "archived_cash_stance": level, "cash_stance": bound_stance["level"],
        "stance_floor": floor, "stance_raised_to_floor": raised, "cash_buffer_rate": policy["cash_buffer_rate"],
        "drawdown": (base.get("drawdown_control") or {}).get("drawdown"),
    }
    return policy, notes


# ---------------------------------------------------------------- one day
def _compact_order(order: Mapping[str, object]) -> Dict[str, object]:
    return {key: order.get(key) for key in ("symbol", "side", "shares", "intent", "estimated_price", "net_cash_impact")}


def _account_summary(account: Mapping[str, object]) -> Dict[str, object]:
    return {"cash": account["cash"], "nav": account["nav"], "positions": len(account["positions"]),
            "symbols": [row["symbol"] for row in account["positions"]]}


def replay_day(
    index: int, archived: Mapping[str, object], template: Mapping[str, object], ledger: ReplayLedger,
    nav_history: Sequence[Mapping[str, object]], market: ReplayMarket, trade_date: Optional[str],
) -> Tuple[Dict[str, object], Dict[str, Mapping[str, object]], Optional[Dict[str, object]]]:
    """回放一天：換成模擬帳戶 → 重建 policy → 配置、情境、Guard → 下一交易日官方均價成交 → 日終估值。

    回傳 (日紀錄, 要封存的 artifacts, 日終帳本快照或 None（成交價尚未可得）)。
    """
    run_id = str(archived["run_id"])
    source = archived["bundle"]
    cutoff = str(source["decision_cutoff"])
    account = ledger.prepare(source)
    bundle = dict(source)
    bundle["account_snapshot"] = account
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    errors = DecisionInputValidator(bundle).validate()
    if errors:
        raise ReplayError("%s 換成回放帳戶後輸入驗證失敗：%s" % (run_id, "；".join(errors[:5])))
    policy, policy_notes = bind_replay_policy(
        template, bundle, archived["policy"], archived["intent"], archived["debate"], archived["stance"], nav_history)
    try:
        proposal = AllocationOrderEngine(bundle, policy).run(archived["intent"])
        scenario = ScenarioEngine(bundle, policy).run(proposal)
        guard = CompetitionGuardV2(bundle, policy).run(proposal, scenario)
    except DecisionToolError as error:
        raise ReplayError("%s 回放配置失敗：%s" % (run_id, error)) from error
    orders = list(proposal["order_proposal"]["orders"])
    passed = guard.get("passed") is True
    status = "approved" if passed and orders else ("no_trade" if passed else "rejected")
    decision: Dict[str, object] = {
        "schema_version": REPLAY_SCHEMA_VERSION, "kind": "replay_decision", "source_run_id": run_id,
        "decision_cutoff": cutoff, "bundle_hash": bundle["bundle_sha256"], "policy_hash": policy["content_sha256"],
        "proposal_id": proposal["proposal_id"], "guard_id": guard.get("guard_id"), "status": status,
        "orders": orders if status == "approved" else [],
    }
    decision["content_sha256"] = content_sha256(decision)
    failed_rules = [str(item.get("rule_id")) for item in guard.get("checks", []) if not item.get("passed")]
    record: Dict[str, object] = {
        "index": index, "run_id": run_id, "decision_cutoff": cutoff, "trade_date": trade_date, "status": "pending_execution",
        "decision_status": status, "policy": policy_notes, "account_before": _account_summary(account),
        "proposal": {"proposal_id": proposal["proposal_id"], "content_sha256": proposal["content_sha256"],
                     "orders": len(orders), "turnover_rate": proposal["order_proposal"]["turnover_rate"],
                     "cash_weight": proposal["allocation_proposal"]["cash_weight"],
                     "constraint_flags": list(proposal.get("constraint_flags", []))},
        "guard": {"passed": passed, "failed_rules": failed_rules},
        "orders": [_compact_order(order) for order in decision["orders"]],
        "execution": None, "close": None, "price_evidence": None,
    }
    prefix = "d%02d_" % index
    artifacts: Dict[str, Mapping[str, object]] = {
        prefix + "account_before": account, prefix + "policy": policy, prefix + "proposal": proposal,
        prefix + "scenario": scenario, prefix + "guard": guard, prefix + "decision": decision,
    }
    if trade_date is None:
        record["pending_reason"] = "資料庫在 as_of 前沒有 cutoff 後的官方交易日"
        return record, artifacts, None
    if OfficialCloseMarket.close_at(trade_date) <= _time(cutoff, "decision_cutoff"):
        raise ReplayError("%s 的成交日 %s 不晚於 decision_cutoff" % (run_id, trade_date))
    symbols = sorted(set(ledger.held_symbols()) | {str(order["symbol"]).upper() for order in decision["orders"]})
    markets = market.build(trade_date, symbols)
    if markets is None:
        record["pending_reason"] = "%s 的官方成交均價／收盤價在 as_of 前尚未到齊" % trade_date
        return record, artifacts, None
    execution_market, close_market = markets
    settlement = market.settlement_date(trade_date)
    execution = ledger.execute(decision, execution_market, bundle["rules"], settlement)
    close = ledger.close(close_market)
    fills = execution["fills"]
    record.update({
        "status": "executed",
        "execution": {
            "execution_at": execution_market["execution_at"], "settlement_date": settlement,
            "price_rule": execution_market["price_rule"], "fills": fills, "unfilled_orders": execution["unfilled_orders"],
            "gross_traded": str(sum((Decimal(fill["gross_amount"]) for fill in fills), Decimal(0))),
            "commission": str(sum((Decimal(fill["commission"]) for fill in fills), Decimal(0))),
            "tax": str(sum((Decimal(fill["tax"]) for fill in fills), Decimal(0))),
            "content_sha256": execution["content_sha256"],
        },
        "close": {"trade_date": close["trade_date"], "nav": close["nav"], "cash": close["cash"],
                  "positions": len(close["positions"]), "available_at": close_market["available_at"]},
        "price_evidence": {symbol: {key: row[key] for key in ("source", "fetched_at", "raw_sha256")}
                           for symbol, row in sorted(close_market["quotes"].items()) if symbol in symbols},
    })
    artifacts.update({prefix + "execution_market": execution_market, prefix + "close_market": close_market,
                      prefix + "execution": execution, prefix + "close": close})
    return record, artifacts, close


def _trade_date_for(market: ReplayMarket, bundle: Mapping[str, object], run_id: str) -> Optional[str]:
    """目標時段日為成交日；資料庫 cutoff 後第一個官方交易日必須等於它，否則就是缺價或缺日。"""
    cutoff = str(bundle["decision_cutoff"])
    first = market.first_trading_day_after(cutoff)
    session = market.session_day(bundle)
    if session is None:
        return first
    if first is not None and first != session:
        raise ReplayError("%s 的目標時段日 %s 與資料庫 cutoff 後第一個官方交易日 %s 不一致（缺價或缺日）" % (run_id, session, first))
    return session if first is not None else None


# ---------------------------------------------------------------- whole replay
def build_replay(
    root: Path, policy_template_path: Path, *, decision_root: Path, database: Path, as_of: str,
    start: Optional[str] = None, end: Optional[str] = None, run_ids: Optional[Sequence[str]] = None,
    allow_gaps: bool = False, initial_cash: Optional[str] = None, log: Log = lambda _: None,
) -> Tuple[Dict[str, object], Dict[str, Mapping[str, object]]]:
    """執行整段回放，回傳 (報告主體, 每日 artifacts)；主體不含 replay_id／generated_at。不寫檔、不呼叫 Agent。"""
    with localcontext() as context:
        context.prec = DECISION_DECIMAL_PRECISION
        return _build_replay(root, policy_template_path, decision_root=decision_root, database=database, as_of=as_of,
                             start=start, end=end, run_ids=run_ids, allow_gaps=allow_gaps, initial_cash=initial_cash, log=log)


def _build_replay(
    root: Path, policy_template_path: Path, *, decision_root: Path, database: Path, as_of: str,
    start: Optional[str], end: Optional[str], run_ids: Optional[Sequence[str]], allow_gaps: bool,
    initial_cash: Optional[str], log: Log,
) -> Tuple[Dict[str, object], Dict[str, Mapping[str, object]]]:
    root, decision_root, database = Path(root), Path(decision_root), Path(database)
    template = _read_json(policy_template_path, "策略樣板")
    market = ReplayMarket(database, as_of)
    selection = select_runs(decision_root, market, start, end, run_ids)
    log("[回放] %d 個 Decision run，策略樣板 %s，as_of %s" % (len(selection), template.get("policy_id"), market.as_of))
    archived_first = load_archived_run(decision_root, str(selection[0]["run_id"]))
    if initial_cash is not None:
        ledger = ReplayLedger.all_cash(initial_cash)
        start_account = {"basis": "initial_cash", "cash": str(Decimal(str(initial_cash))), "positions": []}
    else:
        source_account = archived_first["bundle"]["account_snapshot"]
        ledger = ReplayLedger.from_account_snapshot(source_account)
        start_account = {"basis": "archived_account_snapshot", "run_id": archived_first["run_id"],
                         "cash": source_account["cash"], "positions": list(source_account["positions"])}
    days: List[Dict[str, object]] = []
    artifacts: Dict[str, Mapping[str, object]] = {}
    nav_series: List[Dict[str, object]] = []
    nav_history: List[Dict[str, object]] = []
    gross, commission, tax, fills = Decimal(0), Decimal(0), Decimal(0), 0
    status, not_replayed = "completed", []
    previous_trade_date: Optional[str] = None
    for index, item in enumerate(selection, start=1):
        run_id = str(item["run_id"])
        archived = archived_first if index == 1 else load_archived_run(decision_root, run_id)
        bundle = archived["bundle"]
        cutoff = str(bundle["decision_cutoff"])
        trade_date = _trade_date_for(market, bundle, run_id)
        if previous_trade_date is not None:
            if _time(cutoff, "decision_cutoff") <= OfficialCloseMarket.close_at(previous_trade_date):
                raise ReplayError("%s 的 cutoff 早於前一天成交日 %s 收盤，無法結轉" % (run_id, previous_trade_date))
            if trade_date is not None:
                for gap_day in market.official_days(previous_trade_date, trade_date):
                    if not allow_gaps:
                        raise ReplayError("缺日：%s 有官方交易日卻沒有決策 run（在 %s 與 %s 之間）；確定要略過請用 allow_gaps" % (
                            gap_day, previous_trade_date, trade_date))
                    gap_markets = market.build(gap_day, ledger.held_symbols())
                    if gap_markets is None:
                        raise ReplayError("缺日 %s 的官方收盤價在 as_of 前尚未到齊，無法估值" % gap_day)
                    close = ledger.close(gap_markets[1])
                    days.append({"index": len(days) + 1, "run_id": None, "decision_cutoff": None, "trade_date": gap_day,
                                 "status": "gap", "decision_status": None, "close": {"trade_date": gap_day, "nav": close["nav"],
                                                                                   "cash": close["cash"], "positions": len(close["positions"]),
                                                                                   "available_at": gap_markets[1]["available_at"]}})
                    nav_series.append({"trade_date": gap_day, "nav": close["nav"], "basis": "gap_official_close", "run_id": None})
                    nav_history.append({"nav": close["nav"], "as_of": gap_markets[1]["available_at"]})
                    log("[回放] 缺日 %s：沒有決策，持股以官方收盤估值 NAV %s" % (gap_day, close["nav"]))
        log("[回放] %d/%d %s cutoff %s → 成交日 %s" % (index, len(selection), run_id, cutoff, trade_date or "待官方價格"))
        record, day_artifacts, close = replay_day(len(days) + 1, archived, template, ledger, nav_history, market, trade_date)
        record["session_day"] = item["session_day"]
        if index == 1:
            nav_series.append({"trade_date": str(bundle["snapshot"].get("latest_trade_date")), "nav": record["account_before"]["nav"],
                               "basis": "start_marked_at_cutoff_prices", "run_id": run_id})
        days.append(record)
        artifacts.update(day_artifacts)
        if close is None:
            status = "stopped_pending_execution"
            not_replayed = [str(other["run_id"]) for other in selection[index:]]
            log("[回放] %s 成交價尚未可得：%s；之後 %d 個 run 未回放" % (run_id, record["pending_reason"], len(not_replayed)))
            break
        execution = record["execution"]
        gross += Decimal(execution["gross_traded"])
        commission += Decimal(execution["commission"])
        tax += Decimal(execution["tax"])
        fills += len(execution["fills"])
        nav_series.append({"trade_date": close["trade_date"], "nav": close["nav"], "basis": "official_close", "run_id": run_id})
        nav_history.append({"nav": close["nav"], "as_of": record["close"]["available_at"]})
        previous_trade_date = close["trade_date"]
        log("[回放]   決策 %s、訂單 %d、成交 %d、未成交 %d，日終 NAV %s" % (
            record["decision_status"], len(record["orders"]), len(execution["fills"]), len(execution["unfilled_orders"]), close["nav"]))
    body: Dict[str, object] = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "status": status,
        "inputs": {
            "policy_template_path": str(policy_template_path), "decision_root": str(decision_root), "database": str(database),
            "as_of": market.as_of, "start": start, "end": end, "run_ids": [str(item["run_id"]) for item in selection],
            "allow_gaps": bool(allow_gaps), "initial_cash": None if initial_cash is None else str(Decimal(str(initial_cash))),
        },
        "provenance": {
            "policy_template": {"policy_id": template.get("policy_id"), "available_at": template.get("available_at"),
                                "content_sha256": policy_template_sha256(template)},
            "runs": [{"run_id": str(item["run_id"]), "decision_cutoff": item["decision_cutoff"], "session_day": item["session_day"],
                      "session_day_basis": item["session_day_basis"], "superseded_run_ids": item["superseded_run_ids"],
                      "manifest_sha256": _read_json(Path(item["run_dir"]) / "manifest.json", "manifest").get("manifest_sha256")}
                     for item in selection],
            "execution_price_rule": "official_average_price",
            "settlement_calendar_basis": "weekday_approximation",
            "tradeagent": tree_sha256(root / "tradeagent") if (root / "tradeagent").is_dir() else None,
            "code_version": code_version(root, ENGINE_VERSIONS),
        },
        "start_account": start_account,
        "days": days,
        "not_replayed_run_ids": not_replayed,
        "nav_series": nav_series,
        "metrics": nav_metrics(nav_series, gross, commission, tax, fills),
        "limitations": [
            "回放不重現風險 Agent 的修正循環：Guard 未通過的日子標為 rejected、不下單。",
            "交割日以週一至週五近似，不含國定假日；公司行動（除權息、分割）不套用，除權息日 NAV 會含價格跳空。",
            "以單一樣本（少於 20 個交易日）得到的 Sharpe 為 unavailable，少於 60 個交易日的年化只供參考。",
        ],
    }
    body["content_sha256"] = replay_body_sha256(body)
    return body, artifacts


def save_replay(
    root: Path, body: Mapping[str, object], artifacts: Mapping[str, Mapping[str, object]], replay_id: Optional[str] = None,
) -> Tuple[str, Path]:
    """封存報告與每日 artifacts（不可變 run），並在旁邊寫同源 Markdown。"""
    from .replay_report import render_replay_report

    replay_id = replay_id or "replay-%s" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = dict(body)
    report["replay_id"] = replay_id
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    repository = ReplayRepository(Path(root) / REPLAY_ROOT)
    if repository.run_dir(replay_id) is not None:
        raise ReplayError("回放 %s 已存在，不覆寫" % replay_id)
    run_dir = repository.save(replay_id, {"report": report, **artifacts})
    (run_dir.parent / ("%s.md" % replay_id)).write_text(render_replay_report(report), encoding="utf-8")
    return replay_id, run_dir


def read_replay(root: Path, replay_id: str) -> Tuple[Dict[str, object], Path]:
    repository = ReplayRepository(Path(root) / REPLAY_ROOT)
    run_dir = repository.run_dir(replay_id)
    if run_dir is None:
        raise ReplayError("找不到回放：%s" % replay_id)
    return _read_json(run_dir / "report.json", "回放報告"), run_dir
