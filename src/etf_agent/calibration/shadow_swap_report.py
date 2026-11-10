"""唯讀統計：`agent_swap` v1 影子期封存的換股提名，以及「若開 v2 大約會成交哪幾對」的離線估算。

逐份讀已封存 Decision run（先驗證 manifest 與雜湊），只處理 `position_sizing.agent_swap.version == "v1"` 且
`team_inputs`（2.9）帶 `swap_nomination` 的影子 run，輸出：

- 每日與合計的提名狀態次數（completed／not_needed／exhausted／unavailable）、提名配對數、換入與換出股及其信心；
- 依 v2 配置規則（`allocation.py` 的 `_agent_swap`：依 Agent 先後、減碼日略過、換入者不換股即有名額則不需要、
  換出者不可用、`max_swaps_per_run` 上限）對每一對估算結果與被取消的原因分佈；
- 與同日實際訂單對照（影子期實際配置不讀提名，訂單與沒有閘門時相同）。

估算的限制（報告內也會寫明）：影子 run 的實際配置就是「不換股」的基準，所以只能用它的訂單與旗標推論；
現金、單股權重、產業、換手、Active Share 與風險審查修正等動態取消無法離線重現，因此「估計可成交」是上限，不是承諾。
只描述當時封存了什麼，不是績效，也不是對 LLM 判斷的前瞻驗證（證據等級 replay_reference_only）。

不寫入任何 artifact、不改 Policy，也不被 decision、automation 匯入。任何一份 run 缺檔、雜湊不符、欄位不符、
snapshot／cutoff 或交易決策識別不一致即整批拒絕，不輸出部分結果、不補假值；沒有任何影子 run 時明確回報 `no_shadow_data`。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.core.run_layout import list_run_dirs
from etf_agent.decision.contracts import artifact_content_sha256
from etf_agent.decision.swap_nomination import STATUS_COMPLETED, STATUSES

from .contracts import REPLAY_EVIDENCE_GRADE, REPLAY_NOTICE, CalibrationError, require_mapping, require_sequence

REPORT_VERSION = "shadow_swap_report.v1"
NO_SHADOW_DATA = "no_shadow_data"
OK = "ok"
REQUIRED_ARTIFACTS = ("intent", "proposal", "policy", "team_inputs")
SWAP_NOMINATION_KEY = "swap_nomination"
SHADOW_VERSION = "v1"
DERISK_FLAG = "RISK_DRAWDOWN"  # 減碼日的近似判定：配置在該日留下的旗標（allocation 的 derisk_day 看的是同一類減碼旗標）
SLOT_FLAG = "MAX_POSITIONS"
# 與 allocation.AGENT_SWAP_UNFILLED_REASONS 同義：換入者沒有買單時，依其縮減旗標判定原因（離線只能看影子基準的旗標）。
UNFILLED_REASONS = {
    "RISK_CASH_FIT": "cash_fit", "CASH_BUFFER": "cash_fit", "RISK_CASH_SOFT_FLOOR": "cash_fit",
    "RISK_STOCK_WEIGHT": "stock_weight", "RISK_SECTOR_WEIGHT": "sector_fit", "TURNOVER_LIMIT": "turnover",
}
WOULD_FILL = "would_fill"  # 估計可成交（上限）
UNNEEDED = "unneeded"  # 換入者在影子基準已有買單：v2 不需要這一對
SKIPPED_PREFIX = "skipped:"
CANCELLED_PREFIX = "cancelled:"
LIMITATION = (
    "影子 run 的實際配置不讀提名，只能以它的訂單與旗標推論 v2 的結果；現金、單股權重、產業、換手、Active Share 與風險審查修正"
    "造成的動態取消無法離線重現，estimated_fills 是上限。減碼日以影子基準出現 %s 旗標近似判定。" % DERISK_FLAG
)


def _read(path: Path) -> Mapping[str, object]:
    try:
        return require_mapping(json.loads(path.read_text(encoding="utf-8")), path.name)
    except (OSError, json.JSONDecodeError) as error:
        raise CalibrationError("無法讀取 %s" % path) from error


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CalibrationError("%s 必須是非空字串" % label)
    return value


def _symbol(value: object, label: str) -> str:
    return _text(value, label).upper()


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CalibrationError("%s 必須是非負整數" % label)
    return value


def _load_shadow_run(run_dir: Path) -> Optional[Dict[str, object]]:
    """驗證並整理一份 run；不是 v1 影子 run 回傳 None（舊 run 或 v2 正式 run），其餘任何異常都拋 CalibrationError。"""
    label = run_dir.name
    manifest = _read(run_dir / "manifest.json")
    store = ImmutableRunStore(run_dir.parent, schema_version=str(manifest.get("schema_version")), error=CalibrationError)
    store.verify(label)
    listed = require_mapping(manifest.get("artifacts"), "%s manifest.artifacts" % label)
    # 沒有 policy 或 team_inputs 的舊 run 不可能是影子 run（影子閘門要求 team_inputs 2.9）；雜湊上面已驗過。
    if "policy" not in listed or "team_inputs" not in listed:
        return None
    policy = _read(run_dir / "policy.json")
    sizing = policy.get("position_sizing")
    gate = sizing.get("agent_swap") if isinstance(sizing, Mapping) else None
    team = _read(run_dir / "team_inputs.json")
    has_nomination = SWAP_NOMINATION_KEY in team
    if gate is None:
        if has_nomination:
            raise CalibrationError("%s 的 team_inputs 有 swap_nomination，但 Policy 沒有 agent_swap 閘門" % label)
        return None
    gate = require_mapping(gate, "%s policy.agent_swap" % label)
    if gate.get("version") != SHADOW_VERSION:
        return None  # v2 正式 run：配置已使用提名，不屬於影子期
    if not has_nomination:
        raise CalibrationError("%s 的 Policy 開了 agent_swap v1，team_inputs 卻沒有 swap_nomination" % label)
    for name in REQUIRED_ARTIFACTS:
        if name not in listed:
            raise CalibrationError("%s 的 manifest 缺少 %s" % (label, name))
    nomination = require_mapping(team[SWAP_NOMINATION_KEY], "%s swap_nomination" % label)
    intent = _read(run_dir / "intent.json")
    proposal = _read(run_dir / "proposal.json")
    if intent.get("status") != "completed":
        raise CalibrationError("%s 的交易意圖尚未完成" % label)
    if proposal.get("status") != "completed":
        raise CalibrationError("%s 的配置提案不是 completed，沒有可對照的訂單" % label)
    for field in ("snapshot_id", "decision_cutoff"):
        if not (intent.get(field) == proposal.get(field) == nomination.get(field)) or not intent.get(field):
            raise CalibrationError("%s 的 intent、proposal 與 swap_nomination %s 不一致" % (label, field))
    if intent.get("decision_id") != proposal.get("trade_intent_result_id") or nomination.get("trade_decision_id") != intent.get("decision_id"):
        raise CalibrationError("%s 的 proposal 或 swap_nomination 不是由這份 intent 產生" % label)
    if nomination.get("content_sha256") != artifact_content_sha256(nomination):
        raise CalibrationError("%s 的 swap_nomination content_sha256 不符" % label)

    status = nomination.get("status")
    if status not in STATUSES:
        raise CalibrationError("%s 的提名狀態不合法：%r" % (label, status))
    raw_pairs = require_sequence(nomination.get("nominations"), "%s swap_nomination.nominations" % label)
    if status != STATUS_COMPLETED and raw_pairs:
        raise CalibrationError("%s 的 %s 提名不得有配對" % (label, status))

    items: Dict[str, Mapping[str, object]] = {}
    for item in require_sequence(intent.get("items"), "%s intent.items" % label):
        item = require_mapping(item, "%s intent.items[]" % label)
        symbol = _symbol(item.get("symbol"), "%s intent.items[].symbol" % label)
        _text(item.get("intent"), "%s %s intent" % (label, symbol))
        if symbol in items:
            raise CalibrationError("%s 的 intent 重複出現 %s" % (label, symbol))
        items[symbol] = item
    if not items:
        raise CalibrationError("%s 的交易意圖沒有任何股票" % label)

    pairs: List[Dict[str, object]] = []
    seen: set = set()
    for index, raw in enumerate(raw_pairs):
        raw = require_mapping(raw, "%s nominations[%d]" % (label, index))
        buy = _symbol(raw.get("buy_symbol"), "%s nominations[%d].buy_symbol" % (label, index))
        sell = _symbol(raw.get("sell_symbol"), "%s nominations[%d].sell_symbol" % (label, index))
        if buy == sell or buy in seen or sell in seen:
            raise CalibrationError("%s 的提名配對重複或自我配對：%s／%s" % (label, buy, sell))
        seen.update((buy, sell))
        if buy not in items or items[buy]["intent"] != "buy":
            raise CalibrationError("%s 提名的換入股 %s 不是第一輪 buy" % (label, buy))
        if sell not in items or items[sell]["intent"] != "hold":
            raise CalibrationError("%s 提名的換出股 %s 不是第一輪 hold" % (label, sell))
        pairs.append({
            "buy_symbol": buy, "buy_conviction": items[buy].get("conviction"),
            "sell_symbol": sell, "sell_conviction": items[sell].get("conviction"),
        })

    order_proposal = require_mapping(proposal.get("order_proposal"), "%s order_proposal" % label)
    buys: Dict[str, str] = {}
    sells: Dict[str, str] = {}
    for order in require_sequence(order_proposal.get("orders"), "%s order_proposal.orders" % label):
        order = require_mapping(order, "%s orders[]" % label)
        symbol = _symbol(order.get("symbol"), "%s orders[].symbol" % label)
        side = order.get("side")
        if side not in ("buy", "sell"):
            raise CalibrationError("%s 的 %s 訂單方向不合法：%r" % (label, symbol, side))
        target = buys if side == "buy" else sells
        if symbol in target:
            raise CalibrationError("%s 的 %s 出現重複的%s單" % (label, symbol, side))
        target[symbol] = str(order.get("intent"))

    flags: Dict[str, List[str]] = {}
    flag_counts: Counter = Counter()
    raw_flags = proposal.get("constraint_flags")
    for flag in require_sequence(raw_flags if raw_flags is not None else [], "%s constraint_flags" % label):
        flag = require_mapping(flag, "%s constraint_flags[]" % label)
        code = _text(flag.get("code"), "%s constraint_flags[].code" % label)
        flag_counts[code] += 1
        if flag.get("symbol") is not None:
            flags.setdefault(_symbol(flag["symbol"], "%s constraint_flags[].symbol" % label), []).append(code)
    if any(code.startswith("AGENT_SWAP") for code in flag_counts):
        raise CalibrationError("%s 是 v1 影子 run，配置卻出現 AGENT_SWAP 旗標（影子期配置不得讀提名）" % label)

    limit = _positive_int(gate.get("max_swaps_per_run"), "%s agent_swap.max_swaps_per_run" % label)
    return {
        "run_id": label, "date": run_dir.parent.name, "status": status, "pairs": pairs, "limit": limit,
        "buys": buys, "sells": sells, "flags": flags, "flag_counts": flag_counts,
        "derisk_day": flag_counts.get(DERISK_FLAG, 0) > 0,
    }


def _estimate(run: Mapping[str, object]) -> List[Dict[str, object]]:
    """依 v2 配置規則（allocation._agent_swap 的前置判定）逐對估算結果；順序與 Agent 的先後一致。"""
    status, buys, sells, flags = run["status"], run["buys"], run["sells"], run["flags"]
    results: List[Dict[str, object]] = []
    needed: List[Dict[str, object]] = []
    for pair in run["pairs"]:
        buy, sell = pair["buy_symbol"], pair["sell_symbol"]
        entry = dict(pair)
        buy_flags = flags.get(buy, [])
        if run["derisk_day"]:
            outcome = SKIPPED_PREFIX + "derisk_day"
        elif buy in buys:
            outcome = UNNEEDED
        elif sells.get(sell) == "exit":
            outcome = CANCELLED_PREFIX + "victim_unavailable"
        elif SLOT_FLAG not in buy_flags:
            reason = "buy_unavailable"
            for code in buy_flags:
                reason = UNFILLED_REASONS.get(code, reason)
            outcome = CANCELLED_PREFIX + reason
        else:
            outcome = WOULD_FILL
        entry["estimated_outcome"] = outcome
        entry["baseline_buy_order"] = buy in buys
        entry["baseline_sell_order_intent"] = sells.get(sell)
        entry["baseline_buy_flags"] = sorted(set(buy_flags))
        results.append(entry)
        if outcome == WOULD_FILL:
            needed.append(entry)
    for entry in needed[run["limit"]:]:
        entry["estimated_outcome"] = CANCELLED_PREFIX + "swap_cap"
    return results


def _summarize_run(run: Mapping[str, object]) -> Dict[str, object]:
    pairs = _estimate(run)
    outcomes = Counter(str(pair["estimated_outcome"]) for pair in pairs)
    return {
        "date": run["date"], "run_id": run["run_id"], "nomination_status": run["status"],
        "nominated_pairs": len(pairs), "derisk_day_estimate": run["derisk_day"], "max_swaps_per_run": run["limit"],
        "pairs": pairs,
        "estimated_fills": outcomes.get(WOULD_FILL, 0),
        "estimated_outcomes": dict(sorted(outcomes.items())),
        "actual_orders": len(run["buys"]) + len(run["sells"]), "actual_buy_orders": len(run["buys"]),
        "actual_sell_orders": len(run["sells"]), "actual_max_positions_flags": run["flag_counts"].get(SLOT_FLAG, 0),
    }


def _empty_report(decisions_root: Path, since: Optional[str], skipped: int) -> Dict[str, object]:
    return {
        "report_version": REPORT_VERSION, "evidence_grade": REPLAY_EVIDENCE_GRADE, "notice": REPLAY_NOTICE,
        "status": NO_SHADOW_DATA, "message": "無影子資料：決策庫內沒有 agent_swap v1 影子 run，沒有可統計的提名。",
        "since": since, "shadow_run_count": 0, "skipped_non_shadow_runs": skipped, "limitation": LIMITATION,
    }


def shadow_swap_report(decisions_root: Path, since: Optional[str] = None) -> Dict[str, object]:
    """統計決策庫內 agent_swap v1 影子 run 的提名（`since` 為 YYYY-MM-DD，只算該日含以後）。

    唯讀；每份 run 都先驗證 manifest 與雜湊，任何一份不合格即拒絕整批。沒有影子 run 時回報 `status=no_shadow_data`。"""
    if since is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise CalibrationError("since 格式必須是 YYYY-MM-DD")
    root = Path(decisions_root)
    run_dirs = [run for run in list_run_dirs(root) if since is None or run.parent.name >= since] if root.is_dir() else []
    runs: List[Dict[str, object]] = []
    skipped = 0
    for run_dir in run_dirs:
        run = _load_shadow_run(run_dir)
        if run is None:
            skipped += 1
        else:
            runs.append(run)
    if not runs:
        return _empty_report(root, since, skipped)
    days = [_summarize_run(run) for run in runs]
    statuses: Counter = Counter({status: 0 for status in STATUSES})
    outcomes: Counter = Counter()
    buy_convictions: Counter = Counter()
    sell_convictions: Counter = Counter()
    buys_nominated: Counter = Counter()
    sells_nominated: Counter = Counter()
    for day in days:
        statuses[day["nomination_status"]] += 1
        outcomes.update(day["estimated_outcomes"])
        for pair in day["pairs"]:
            buy_convictions[str(pair["buy_conviction"])] += 1
            sell_convictions[str(pair["sell_conviction"])] += 1
            buys_nominated[pair["buy_symbol"]] += 1
            sells_nominated[pair["sell_symbol"]] += 1
    cancelled = {key[len(CANCELLED_PREFIX):]: count for key, count in sorted(outcomes.items()) if key.startswith(CANCELLED_PREFIX)}
    skipped_reasons = {key[len(SKIPPED_PREFIX):]: count for key, count in sorted(outcomes.items()) if key.startswith(SKIPPED_PREFIX)}
    return {
        "report_version": REPORT_VERSION,
        "evidence_grade": REPLAY_EVIDENCE_GRADE,
        "notice": REPLAY_NOTICE,
        "status": OK,
        "limitation": LIMITATION,
        "since": since,
        "shadow_run_count": len(days),
        "skipped_non_shadow_runs": skipped,
        "totals": {
            "nomination_status_days": {status: statuses[status] for status in STATUSES},
            "nominated_pairs": sum(day["nominated_pairs"] for day in days),
            "buy_conviction": dict(sorted(buy_convictions.items())),
            "sell_conviction": dict(sorted(sell_convictions.items())),
            "buy_symbols": dict(sorted(buys_nominated.items())),
            "sell_symbols": dict(sorted(sells_nominated.items())),
            "estimated_fills": outcomes.get(WOULD_FILL, 0),
            "estimated_unneeded": outcomes.get(UNNEEDED, 0),
            "estimated_skipped": skipped_reasons,
            "estimated_cancelled": cancelled,
            "actual_orders": sum(day["actual_orders"] for day in days),
            "actual_max_positions_flags": sum(day["actual_max_positions_flags"] for day in days),
        },
        "days": days,
    }
