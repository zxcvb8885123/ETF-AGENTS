"""出場後 N 日不得再買進的冷卻名單（``position_sizing.reentry_cooldown``，版本閘門，只由人設定）。

名單由每日 pipeline 從決策庫確定性取得並綁進 Policy（``blocked_symbols``＋``source_run_ids``），事後重建只讀 Policy、
不重掃決策庫（與 ``forced_swap.broken_symbols`` 相同做法）。來源條件：

- 同帳戶、狀態 ``approved``、目標交易時段（``trading_status_bundle.target_session.start``）嚴格早於本次，
  且與本次相隔不超過 ``days`` 個日曆天、cutoff 早於本次 cutoff 的 Decision run；
- 該 run 最終 ``decision.orders`` 中 ``side=sell`` 且訂單意圖為 ``exit``／``forced_exit``（含程式強制換股、風控減至零）的股票。

這是決策層的出場紀錄（不是帳本實際成交）；主辦方作廢整批時該股仍在冷卻名單內，屬保守方向。
沒有決策庫或沒有符合的 run 時綁空名單，不是錯誤（已驗證無歷史）；但 run 存在而 decision.json 損壞、時間無效或
manifest 驗證失敗時一律拒絕（歷史無法驗證不等於沒有出場紀錄，否則冷卻會被悄悄解除）。

v2（``agent_only=True``）：賣單 ``exit``／``forced_exit`` 之外，還要前次 run 的 ``intent.json``（交易 Agent 原意圖）對該股
也是 ``exit``／``forced_exit``；程式把 hold／trim 賣成 exit（回撤減碼、強制換股、曝險換股、單股上限）不計。
前次 run 的 intent.json 缺少、損壞或與 decision 不是同一組時拒絕，不當成「沒有出場」。
stop_exit（程式兜底的硬停損，``include_stop_exits=True``，只在 Policy 有 ``stop_exit`` 時）：停損出場的股票也列入名單——交易 Agent 原意圖不是 exit，
所以從前次 run 的 proposal.json 取帶 ``RISK_STOP_EXIT`` 旗標且賣單意圖為 exit 的股票，與上面的 Agent 出場取聯集；proposal.json 缺少、損壞或與 decision 不是
同一組時拒絕，不當成「沒有停損」。Policy 沒有 stop_exit（或從未停損）時名單與舊版逐位元相同。
v3 的名單口徑同 v2（同樣 ``agent_only=True``），但名單不擋買單、只是交易 Agent 簡報的提示（見 ``sizing.REENTRY_COOLDOWN_VERSION_V3``）。
"""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from etf_agent.core.run_layout import list_run_dirs

from .contracts import DecisionToolError, parse_time
from .prior_decision import _account_id, _read_run_json, _session_start
from .sizing import REENTRY_COOLDOWN_VERSION_V2, REENTRY_COOLDOWN_VERSION_V3, REENTRY_COOLDOWN_VERSIONS

EXIT_INTENTS = ("exit", "forced_exit")


def _agent_exit_symbols(run_dir: Path, decision: Mapping[str, object]) -> set:
    """前次 run 中交易 Agent 自己給 exit／forced_exit 的股票（v2）；intent.json 不可用一律拒絕。"""
    intent = _read_run_json(run_dir, "intent")
    if intent is None:
        raise DecisionToolError("再進場冷卻 v2 無法讀取決策庫 run 的 intent.json：%s" % run_dir.name)
    if decision.get("trade_intent_result_id") != intent.get("decision_id"):
        raise DecisionToolError("再進場冷卻 v2：決策庫 run %s 的 decision 與 intent 不是同一組" % run_dir.name)
    items = intent.get("items")
    if not isinstance(items, list):
        raise DecisionToolError("再進場冷卻 v2：決策庫 run %s 的 intent.items 必須是清單" % run_dir.name)
    return {
        str(item.get("symbol", "")).upper() for item in items
        if isinstance(item, Mapping) and item.get("intent") in EXIT_INTENTS and item.get("symbol")
    }


def _stop_exit_symbols(run_dir: Path, decision: Mapping[str, object]) -> set:
    """前次 run 中被程式兜底停損出場（RISK_STOP_EXIT）的股票；proposal.json 不可用或與 decision 不是同一組一律拒絕。"""
    proposal = _read_run_json(run_dir, "proposal")
    if proposal is None:
        raise DecisionToolError("再進場冷卻（stop_exit）無法讀取決策庫 run 的 proposal.json：%s" % run_dir.name)
    if decision.get("proposal_id") != proposal.get("proposal_id"):
        raise DecisionToolError("再進場冷卻（stop_exit）：決策庫 run %s 的 decision 與 proposal 不是同一組" % run_dir.name)
    flags = proposal.get("constraint_flags")
    if not isinstance(flags, list):
        raise DecisionToolError("再進場冷卻（stop_exit）：決策庫 run %s 的 proposal.constraint_flags 必須是清單" % run_dir.name)
    return {
        str(flag.get("symbol", "")).upper() for flag in flags
        if isinstance(flag, Mapping) and flag.get("code") == "RISK_STOP_EXIT" and flag.get("symbol")
    }


def recent_exit_symbols(
    repository_root: Optional[Path], bundle: Mapping[str, object], days: int, agent_only: bool = False,
    include_stop_exits: bool = False,
) -> Tuple[List[str], List[str]]:
    """最近 ``days`` 個日曆天內已出場的股票與來源 run_id（都排序去重）；``agent_only`` 為 v2：只計交易 Agent 自己的出場；
    ``include_stop_exits`` 再加上程式兜底停損（RISK_STOP_EXIT）出場的股票（只在 agent_only 時有意義；v1 本來就計所有出場）。"""
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
        if decision is None:
            if (run_dir / "decision.json").exists():
                raise DecisionToolError("再進場冷卻無法讀取決策庫 run 的 decision.json：%s" % run_dir.name)
            continue  # 尚未產出 decision 的未完成 run 不構成出場紀錄。
        if decision.get("status") != "approved":
            continue
        try:
            prior_cutoff = parse_time(decision.get("decision_cutoff"), "decision_cutoff")
        except DecisionToolError as error:
            raise DecisionToolError("再進場冷卻遇到 cutoff 無效的 approved run %s：%s" % (run_dir.name, error)) from error
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
        from .finalization import DecisionRepository  # 延後匯入：finalization 也匯入本模組鏈。

        DecisionRepository(Path(repository_root)).verify(run_dir.name)  # manifest 不符即拒絕，不默默略過。
        exited = {
            str(order.get("symbol", "")).upper()
            for order in decision.get("orders", [])
            if isinstance(order, Mapping) and order.get("side") == "sell" and order.get("intent") in EXIT_INTENTS
            and order.get("symbol")
        }
        if agent_only:
            agent_exited = exited & _agent_exit_symbols(run_dir, decision)
            # 只有出場賣單裡有交易 Agent 沒標 exit 的股票時才需要查停損旗標；沒有就不讀 proposal.json（名單與舊版逐位元相同）。
            stopped = (exited - agent_exited) & _stop_exit_symbols(run_dir, decision) if include_stop_exits and exited - agent_exited else set()
            exited = agent_exited | stopped
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
    version = cooldown.get("version")
    if version not in REENTRY_COOLDOWN_VERSIONS:
        raise DecisionToolError("position_sizing.reentry_cooldown.version 只允許 %s" % "、".join(REENTRY_COOLDOWN_VERSIONS))
    symbols, run_ids = recent_exit_symbols(
        repository_root, bundle, days, agent_only=version in (REENTRY_COOLDOWN_VERSION_V2, REENTRY_COOLDOWN_VERSION_V3),
        include_stop_exits=isinstance(sizing.get("stop_exit"), dict),
    )
    cooldown["blocked_symbols"] = symbols
    cooldown["source_run_ids"] = run_ids
    return bound


__all__ = ["bind_reentry_cooldown", "recent_exit_symbols"]
