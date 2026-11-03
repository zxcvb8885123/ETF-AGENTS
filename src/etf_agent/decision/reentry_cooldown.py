"""出場後 N 日不得再買進的冷卻名單（``position_sizing.reentry_cooldown``，版本閘門，只由人設定）。

名單由每日 pipeline 從決策庫確定性取得並綁進 Policy（``blocked_symbols``＋``source_run_ids``），事後重建只讀 Policy、
不重掃決策庫（與 ``forced_swap.broken_symbols`` 相同做法）。來源條件：

- 同帳戶、狀態 ``approved``、目標交易時段（``trading_status_bundle.target_session.start``）嚴格早於本次，
  且與本次相隔不超過 ``days`` 個日曆天、cutoff 早於本次 cutoff 的 Decision run；
- 該 run 最終 ``decision.orders`` 中 ``side=sell`` 且訂單意圖為 ``exit``／``forced_exit``（含程式強制換股、風控減至零）的股票。

這是決策層的出場紀錄（不是帳本實際成交）；主辦方作廢整批時該股仍在冷卻名單內，屬保守方向。
找不到決策庫或沒有符合的 run 時綁空名單，不是錯誤。
"""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from etf_agent.core.run_layout import list_run_dirs

from .contracts import DecisionToolError, parse_time
from .prior_decision import _account_id, _read_run_json, _session_start

EXIT_INTENTS = ("exit", "forced_exit")


def recent_exit_symbols(repository_root: Optional[Path], bundle: Mapping[str, object], days: int) -> Tuple[List[str], List[str]]:
    """最近 ``days`` 個日曆天內已出場的股票與來源 run_id（都排序去重）。"""
    if repository_root is None or not Path(repository_root).is_dir():
        return [], []
    cutoff = parse_time(bundle.get("decision_cutoff"), "decision_cutoff")
    try:
        session_start = _session_start(bundle, "本次 bundle")
    except DecisionToolError:
        return [], []
    account_id = _account_id(bundle)
    symbols: set = set()
    run_ids: set = set()
    for run_dir in list_run_dirs(Path(repository_root)):
        decision = _read_run_json(run_dir, "decision")
        if decision is None or decision.get("status") != "approved":
            continue
        try:
            prior_cutoff = parse_time(decision.get("decision_cutoff"), "decision_cutoff")
        except DecisionToolError:
            continue
        if prior_cutoff >= cutoff:
            continue
        prior_bundle = _read_run_json(run_dir, "decision_input")
        if prior_bundle is None or _account_id(prior_bundle) != account_id:
            continue
        if prior_bundle.get("decision_cutoff") != decision.get("decision_cutoff"):
            continue
        try:
            prior_session = _session_start(prior_bundle, "前次 bundle")
        except DecisionToolError:
            continue
        if not (prior_session < session_start and session_start - prior_session <= timedelta(days=days)):
            continue
        exited = {
            str(order.get("symbol", "")).upper()
            for order in decision.get("orders", [])
            if isinstance(order, Mapping) and order.get("side") == "sell" and order.get("intent") in EXIT_INTENTS
            and order.get("symbol")
        }
        if exited:
            symbols |= exited
            run_ids.add(run_dir.name)
    return sorted(symbols), sorted(run_ids)


def bind_reentry_cooldown(
    policy: Mapping[str, object], repository_root: Optional[Path], bundle: Mapping[str, object],
) -> Dict[str, object]:
    """樣板啟用 ``reentry_cooldown`` 時把冷卻名單綁進 Policy；沒有閘門原樣回傳。呼叫端須重算 content_sha256。"""
    bound = copy.deepcopy(dict(policy))
    sizing = bound.get("position_sizing")
    cooldown = sizing.get("reentry_cooldown") if isinstance(sizing, dict) else None
    if not isinstance(cooldown, dict):
        return bound
    days = cooldown.get("days")
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise DecisionToolError("position_sizing.reentry_cooldown.days 必須是正整數")
    symbols, run_ids = recent_exit_symbols(repository_root, bundle, days)
    cooldown["blocked_symbols"] = symbols
    cooldown["source_run_ids"] = run_ids
    return bound


__all__ = ["bind_reentry_cooldown", "recent_exit_symbols"]
