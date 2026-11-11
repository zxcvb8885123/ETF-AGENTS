"""連跌深度事實與抄底梯度狀態（``position_sizing.agent_swap.quality_dip`` v4 的 ``ladder``）。

設計見 ``docs/agent_led_offense_plan.md`` 4.13、7.3。本模組只做兩件事，都是確定性程式、不讀任何 Agent 輸出的數字、不用模型記憶：

1. ``build_decline_depth_facts``：交易池的連跌深度**事實**（連跌天數、累計跌幅、離 60 日高點回檔、20 日新低比例），
   加本次行情視窗內的**對照樣本**（連跌滿 3 天、回檔達目前深度的日子，之後 3／5 個交易日的報酬分布與最差值、樣本數、
   ``small_sample`` 與 ``evidence_grade: replay_reference_only``）。全部是 ``citable=false`` 的背景，沒有 evidence_id，缺資料不補值；
   整份存為 ``team_inputs.decline_depth_facts``（選配鍵，與 v4 閘門雙向同時存在），Finalizer 以 bundle 與 Policy 整份重算。
2. ``build_ladder_state``：梯度（人填的 ``ladder``）對今天的判定（``open``／``closed`` 與原因、剩餘抄底曝險、已進幾批…）。
   由 ``apply_trade_decision`` 綁進 Policy，配置、風險 Agent 的前置檢查都只讀綁定值；Finalizer 以同一份輸入重算比對。
   **缺任何資料一律 ``closed``（fail-closed），不放行。**

口徑（版本內固定，不是旋鈕；數字上限全由人填的 ``ladder``）：

- 交易池日報酬＝與 ``turn_facts`` 相同的等權簡單平均（當日與前一個交易日都有收盤的股票），另要求涵蓋至少 ``MIN_COVERAGE`` 的交易池股票，
  不足當日報酬為 None；任一需要的日報酬為 None 即該項不可用。交易池指數水位＝日報酬逐日複利。
- 離 60 日高點回檔＝1 − 最新水位 ÷ 最近 60 個交易日內的最高水位；需要至少 60 個交易日，不足不可用。
- **兩個起點**：視窗高點日（最近 60 個交易日內交易池水位最高的那天，並列取最早）與「本輪連跌」的起始日（最近一段連續下跌滿
  ``STREAK_REFERENCE_DAYS`` 天的連跌的第一天，含目前進行中的；找不到時退回視窗高點日）。
  **總抄底曝險與批數從兩者中較晚的那天起算**（``anchor_date``）：只用高點日的話，帳本在高點之後才建倉的帳戶（例如比賽帳戶重開）
  所有部位都算新建倉、梯度永遠關閉；只用連跌起始日的話，連跌中的一個反彈日就會讓計數歸零（所以取較晚者而不是只看連跌起點，
  反彈日不重置，只有出現新的一輪滿 3 天連跌才換起點）。**批次間隔與暫停線的「上一批」則從視窗高點日起算**（較早者），
  起點換新一輪時不會忘記上一批。
- 新建倉部位＝``exposure_rotation.held_since_by_symbol``（由帳本綁定、Finalizer 以帳本重算）的連續持有起點，換成台北日期後不早於起點日；
  總抄底曝險＝這些部位目前的持股權重合計（與配置引擎同一個 NAV 口徑）；批數＝這些部位的不同起點日數；上一批＝最晚的起點日。
  注意「新建倉」預設包含所有目的的新買進，不只 purpose=quality_dip 的配對（計畫 4.13 的定義）；``ladder.exposure_basis`` 填
  ``quality_dip_fills``（選配，已實作、尚未開啟）時改為只計 quality_dip 成交：總抄底曝險、批數與「上一批」都只讀 pipeline 綁定的
  ``quality_dip.ladder_entries``（``scan_quality_dip_entries`` 從決策庫取得；其餘公式不變），清單讀不到或與持股、帳本持有起點不一致即以
  ``entries_unavailable`` 關閉。缺席時逐位元維持上述舊口徑。
- 距上一批的交易日數＝交易池交易日中不早於上一批日期、且不晚於最近交易日的天數（決策在開盤前執行，上一批的成交日也算一天）。
- 暫停：上一批的參考水位＝上一批日期之前最後一個交易日的收盤水位（約等於成交價）；上一批之後第一個累計報酬 ≤ −``pause_after_further_drop``
  的交易日為觸發日；觸發日之後出現任一交易池上漲日才解除。缺資料（沒有參考日、日報酬缺漏）一律視為暫停中。
"""

from __future__ import annotations

import json
from datetime import timedelta, timezone
from pathlib import Path
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Set

from etf_agent.core import canonical_sha256, parse_aware_time
from etf_agent.core.run_layout import list_run_dirs

from .exposure import holding_weights
from .exposure_facts import _check_bars
from .fields import DecisionToolError, decimal_value, reject_unknown_fields
from .quality_dip_swap import (
    LADDER_CLOSED_REASONS, PURPOSE_QUALITY_DIP, ladder_counts_quality_dip_fills_only, quality_dip_ladder_gate, quality_dip_swap_gate,
)
from .turn_facts import NEW_LOW_WINDOW, TURN_ASSESSMENTS, _Pool

DECLINE_DEPTH_FACTS_KEY = "decline_depth_facts"  # team_inputs 的選配鍵：與 quality_dip v4 閘門雙向同時存在
DECLINE_DEPTH_SCHEMA_VERSION = "1.0"
DECLINE_DEPTH_VERSION = "v1"
WINDOW_DAYS = 60
STREAK_REFERENCE_DAYS = 3
FORWARD_HORIZONS = (3, 5)
MIN_COVERAGE = Decimal("0.8")
SMALL_SAMPLE_MIN = 10
EVIDENCE_GRADE = "replay_reference_only"
QUANTUM = Decimal("0.000001")
TAIPEI = timezone(timedelta(hours=8))
FACTS_FIELDS = {
    "schema_version", "version", "facts_id", "as_of", "citable", "data_status", "note", "pool", "reference", "data_gaps",
}
FACTS_NOTE = (
    "這些是程式算的交易池連跌深度事實與本次行情視窗內的對照樣本（citable=false、沒有 evidence_id，不可引用，理由也不要寫出 "
    "decline_depth_facts 這個鍵名）：不是訊號，不保證之後的走勢；對照樣本來自同一個行情視窗、彼此重疊、樣本很少（small_sample），"
    "證據等級 replay_reference_only，只能說明「這個視窗裡發生過什麼」，不能當成預測。不要補入未提供的歷史案例或後續報酬。"
)


def _q(value: Decimal) -> str:
    return str(value.quantize(QUANTUM, rounding=ROUND_HALF_UP))


class _Series:
    """交易池日報酬與複利水位（只依 bundle 的 price_series；任何 bar 晚於最近交易日即拒絕）。"""

    def __init__(self, bundle: Mapping[str, object], trade_date: str):
        self.pool = _Pool(bundle, trade_date)
        self.days = self.pool.days
        self.trade_date = trade_date
        self.returns: List[Optional[Decimal]] = [None] * len(self.days)
        for index in range(1, len(self.days)):
            self.returns[index] = self._day_return(index)

    def _day_return(self, index: int) -> Optional[Decimal]:
        from bisect import bisect_right

        today, before = self.days[index], self.days[index - 1]
        values: List[Decimal] = []
        for symbol, dates in self.pool.dates_by_symbol.items():
            closes = self.pool.closes_by_symbol[symbol]
            position = bisect_right(dates, today)
            if position < 2 or dates[position - 1] != today or dates[position - 2] != before or closes[position - 2] <= 0:
                continue
            values.append(closes[position - 1] / closes[position - 2] - Decimal(1))
        if not values or not self.pool.size or Decimal(len(values)) / Decimal(self.pool.size) < MIN_COVERAGE:
            return None
        return sum(values, Decimal(0)) / Decimal(len(values))

    def cumulative(self, start: int, end: int) -> Optional[Decimal]:
        """從 ``start`` 日收盤到 ``end`` 日收盤的累計報酬；區間內任一日報酬缺漏回 None。"""
        if start < 0 or end >= len(self.days) or start > end:
            return None
        level = Decimal(1)
        for index in range(start + 1, end + 1):
            value = self.returns[index]
            if value is None:
                return None
            level *= Decimal(1) + value
        return level - Decimal(1)

    def window_levels(self, end: int) -> Optional[List[Decimal]]:
        """以 ``end`` 結尾、長 ``WINDOW_DAYS`` 的視窗內逐日水位（視窗第一天為 1）；不足或缺漏回 None。"""
        start = end - WINDOW_DAYS + 1
        if start < 0:
            return None
        levels: List[Decimal] = [Decimal(1)]
        for index in range(start + 1, end + 1):
            value = self.returns[index]
            if value is None:
                return None
            levels.append(levels[-1] * (Decimal(1) + value))
        return levels

    def drawdown(self, end: int) -> Optional[Dict[str, object]]:
        levels = self.window_levels(end)
        if levels is None:
            return None
        peak_offset = max(range(len(levels)), key=lambda offset: (levels[offset], -offset))  # 並列取最早
        return {"drawdown": Decimal(1) - levels[-1] / levels[peak_offset], "peak_index": end - WINDOW_DAYS + 1 + peak_offset}


def _stats(values: Sequence[Decimal]) -> Dict[str, object]:
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return {"n": 0, "negative": 0, "worst": None, "median": None, "best": None, "small_sample": True}
    middle = count // 2
    median = ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) / Decimal(2)
    return {
        "n": count, "negative": sum(1 for value in ordered if value < 0), "worst": _q(ordered[0]), "median": _q(median),
        "best": _q(ordered[-1]), "small_sample": count < SMALL_SAMPLE_MIN,
    }


def _forward(series: _Series, indexes: Sequence[int]) -> Dict[str, object]:
    """各事件日之後 h 個交易日的報酬分布；前瞻視窗必須結束在最近交易日（含）之前，越過的事件不計入、也不補值。"""
    last = len(series.days) - 1
    result: Dict[str, object] = {}
    for horizon in FORWARD_HORIZONS:
        values: List[Decimal] = []
        for index in indexes:
            if index + horizon > last:
                continue
            value = series.cumulative(index, index + horizon)
            if value is not None:
                values.append(value)
        result[str(horizon)] = _stats(values)
    return result


def _gate_or_raise(policy: Mapping[str, object]) -> Mapping[str, object]:
    ladder = quality_dip_ladder_gate(policy.get("position_sizing"))
    if ladder is None:
        raise DecisionToolError("DecisionPolicy 沒有 position_sizing.agent_swap.quality_dip v4 的 ladder，不應建立連跌深度事實")
    return ladder


def build_decline_depth_facts(bundle: Mapping[str, object], policy: Mapping[str, object]) -> Dict[str, object]:
    """完整事實（team_inputs.decline_depth_facts）：只依 bundle（行情視窗），Policy 只用來確認 v4 閘門存在；不讀任何 Agent 輸出。"""
    _gate_or_raise(policy)
    snapshot = bundle.get("snapshot")
    trade_date = snapshot.get("latest_trade_date") if isinstance(snapshot, Mapping) else None
    if not isinstance(trade_date, str):
        raise DecisionToolError("decline_depth_facts 需要 snapshot.latest_trade_date")
    if not isinstance(bundle.get("price_series"), list):
        raise DecisionToolError("decline_depth_facts 需要 price_series")
    cutoff = parse_aware_time(bundle.get("decision_cutoff"), "decision_cutoff", error=DecisionToolError)
    _check_bars(bundle, trade_date, cutoff)
    series = _Series(bundle, trade_date)
    gaps: List[str] = []
    last = len(series.days) - 1
    if last < 0:
        gaps.append("price_series 沒有任何行情，連跌深度事實無法計算")
    elif series.days[-1] != trade_date:
        gaps.append("行情最後一個交易日 %s 不是 snapshot.latest_trade_date %s" % (series.days[-1], trade_date))

    streak = 0
    streak_ok = last >= 1
    for index in range(last, 0, -1):
        value = series.returns[index]
        if value is None:
            streak_ok = False
            gaps.append("連續下跌日數遇到無法計算的日報酬（覆蓋不足），天數不可用")
            break
        if value >= 0:
            break
        streak += 1
    streak_return = series.cumulative(last - streak, last) if streak_ok and streak > 0 else None
    if streak_ok and streak > 0 and streak_return is None:
        gaps.append("本輪連跌的累計跌幅無法計算")
    depth = series.drawdown(last) if last >= 0 else None
    if depth is None:
        gaps.append("離 %d 日高點的回檔無法計算（交易日不足 %d 個或日報酬覆蓋不足）" % (WINDOW_DAYS, WINDOW_DAYS))
    new_low = series.pool.share(series.days[-1], NEW_LOW_WINDOW, lambda c: c[-1] <= min(c[-NEW_LOW_WINDOW:])) if last >= 0 else None
    if new_low is None:
        gaps.append("new_low_20d_share 沒有足夠筆數的股票")

    # 最近一輪「連跌滿 STREAK_REFERENCE_DAYS 天」的連跌（含目前正在進行的）：起始日與長度；找不到回 None（梯度改以視窗高點日為起點）。
    round_start: Optional[int] = None
    round_length = 0
    run = 0
    for index in range(last, 0, -1):
        value = series.returns[index]
        if value is not None and value < 0:
            run += 1
            continue
        if run >= STREAK_REFERENCE_DAYS:
            round_start, round_length = index + 1, run
            break
        run = 0
    else:
        if run >= STREAK_REFERENCE_DAYS:
            round_start, round_length = 1, run
    pool_block: Dict[str, object] = {
        "pool_size": series.pool.size,
        "latest_trade_date": series.days[-1] if last >= 0 else None,
        "down_streak_days": streak if streak_ok else None,
        "streak_start_date": series.days[last - streak + 1] if streak_ok and streak > 0 else None,
        "streak_return": None if streak_return is None else _q(streak_return),
        "round_start_date": None if round_start is None else series.days[round_start],
        "round_length_days": round_length if round_start is not None else None,
        "window_days": WINDOW_DAYS,
        "drawdown_from_window_high": None if depth is None else _q(depth["drawdown"]),  # type: ignore[arg-type]
        "window_high_date": None if depth is None else series.days[int(depth["peak_index"])],
        "new_low_20d_share": new_low,
        "min_coverage": str(MIN_COVERAGE),
    }

    # 對照樣本：整個行情視窗內的事件日（不含最近交易日之後，前瞻視窗不得越過最近交易日）。
    streak_events: List[int] = []
    for index in range(4, last + 1):
        window = [series.returns[index - offset] for offset in range(STREAK_REFERENCE_DAYS)]
        before = series.returns[index - STREAK_REFERENCE_DAYS]
        if all(value is not None and value < 0 for value in window) and before is not None and before >= 0:
            streak_events.append(index)
    depth_events: List[int] = []
    if depth is not None and Decimal(str(depth["drawdown"])) > 0:
        target = Decimal(str(depth["drawdown"]))
        for index in range(WINDOW_DAYS - 1, last):
            other = series.drawdown(index)
            if other is not None and Decimal(str(other["drawdown"])) >= target:
                depth_events.append(index)
    reference = {
        "evidence_grade": EVIDENCE_GRADE,
        "note": "樣本取自本次行情視窗，重疊、彼此不獨立；n 很小，不是預測，也不是驗證。",
        "streak_days": STREAK_REFERENCE_DAYS,
        "forward_horizons": list(FORWARD_HORIZONS),
        "small_sample_below": SMALL_SAMPLE_MIN,
        "after_streak": {"event_dates": [series.days[i] for i in streak_events], **_forward(series, streak_events)},
        "after_same_depth": {
            "min_drawdown": pool_block["drawdown_from_window_high"],
            "event_dates": [series.days[i] for i in depth_events], **_forward(series, depth_events),
        },
    }
    status = "available" if depth is not None and streak_ok and new_low is not None and not (
        streak > 0 and streak_return is None) else "unavailable"
    body: Dict[str, object] = {
        "schema_version": DECLINE_DEPTH_SCHEMA_VERSION,
        "version": DECLINE_DEPTH_VERSION,
        "as_of": trade_date,
        "citable": False,
        "data_status": status,
        "note": FACTS_NOTE,
        "pool": pool_block,
        "reference": reference,
        "data_gaps": gaps,
    }
    body["facts_id"] = "decline-depth-facts:" + canonical_sha256(body)[:20]
    return body


def validate_decline_depth_facts(
    bundle: Mapping[str, object], policy: Mapping[str, object], team: Mapping[str, object], errors: List[str],
) -> None:
    """以 bundle 與 Policy 整份重算 ``team_inputs.decline_depth_facts``；缺少、多餘欄位或不一致即拒絕。"""
    prefix = "team_inputs.decline_depth_facts"
    facts = team.get(DECLINE_DEPTH_FACTS_KEY)
    if not isinstance(facts, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    if quality_dip_ladder_gate(policy.get("position_sizing")) is None:
        errors.append("%s 只能用於啟用 position_sizing.agent_swap.quality_dip v4（ladder）的 Policy" % prefix)
        return
    reject_unknown_fields(facts, FACTS_FIELDS, prefix, errors)
    try:
        expected = build_decline_depth_facts(bundle, policy)
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        errors.append("%s 無法以共同輸入重算：%s" % (prefix, error))
        return
    if dict(facts) != expected:
        errors.append("%s 與重算結果不同，拒絕改寫" % prefix)


def decline_depth_reference_tokens(facts: Optional[Mapping[str, object]]) -> List[str]:
    """背景識別碼：文字欄位出現即拒絕（它不是證據）。"""
    tokens = [DECLINE_DEPTH_FACTS_KEY]
    if isinstance(facts, Mapping) and isinstance(facts.get("facts_id"), str):
        tokens.append(str(facts["facts_id"]))
    return tokens


# ---------------------------------------------------------------- 梯度狀態

def _held_since_dates(policy: Mapping[str, object], bundle: Mapping[str, object]) -> Optional[Dict[str, str]]:
    """每檔連續持有起點的台北日期；Policy 沒有綁定或與帳戶持股不一致回 None（fail-closed，不猜）。"""
    sizing = policy.get("position_sizing")
    rotation = sizing.get("exposure_rotation") if isinstance(sizing, Mapping) else None
    raw = rotation.get("held_since_by_symbol") if isinstance(rotation, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    held = sorted(str(p["symbol"]).upper() for p in bundle["account_snapshot"]["positions"] if int(p.get("shares", 0)) > 0)  # type: ignore[index]
    if sorted(str(symbol).upper() for symbol in raw) != held:
        return None
    result: Dict[str, str] = {}
    for symbol, since in raw.items():
        try:
            moment = parse_aware_time(since, "held_since_by_symbol.%s" % symbol, error=DecisionToolError)
        except DecisionToolError:
            return None
        result[str(symbol).upper()] = moment.astimezone(TAIPEI).date().isoformat()
    return result


AGENT_SWAP_FILL_FLAG = "AGENT_SWAP_FILL"  # 與 allocation.AGENT_SWAP_FILL_CODE 相同（測試核對；這裡不匯入 allocation）


def _ladder_entry_problems(
    policy: Mapping[str, object], since: Mapping[str, str],
) -> Optional[List[str]]:
    """綁定的 ``ladder_entries`` 與帳本持有起點的不一致清單；清單是 null／缺席回 None（呼叫端另處理），一致回空清單。
    一致＝每筆 symbol 目前仍持有（在 held_since 內）、不重複、entry_date 等於帳本持有起點的台北日期。"""
    gate = quality_dip_swap_gate(policy.get("position_sizing"))
    entries = gate.get("ladder_entries") if gate is not None else None
    if not isinstance(entries, list):
        return None
    problems: List[str] = []
    seen: Set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            problems.append("ladder_entries 含非物件項目")
            continue
        symbol = str(entry.get("symbol", "")).upper()
        if symbol in seen:
            problems.append("%s 重複" % symbol)
        seen.add(symbol)
        if symbol not in since:
            problems.append("%s 不在帳本的持有起點內（已不持有）" % symbol)
        elif entry.get("entry_date") != since[symbol]:
            problems.append("%s 的 entry_date 與帳本持有起點 %s 不一致" % (symbol, since[symbol]))
    return problems


def _ladder_entry_dates(policy: Mapping[str, object], since: Mapping[str, str]) -> Optional[Dict[str, str]]:
    """quality_dip 成交的進場日（symbol → 台北日期）；清單缺席、null 或與持股／帳本不一致回 None（fail-closed）。"""
    problems = _ladder_entry_problems(policy, since)
    if problems is None or problems:
        return None
    gate = quality_dip_swap_gate(policy.get("position_sizing"))
    return {str(entry["symbol"]).upper(): str(entry["entry_date"]) for entry in gate["ladder_entries"]}  # type: ignore[index]


def scan_quality_dip_entries(
    repository_root: Optional[Path],
    bundle: Mapping[str, object],
    policy: Mapping[str, object],
    applied_run_ids: Optional[Sequence[str]],
) -> Optional[List[Dict[str, str]]]:
    """pipeline 用（來源口徑同 reentry_cooldown 的決策庫掃描，但只看帳本實際結算的 run）：回 quality_dip 成交清單，``None``＝讀不到（fail-closed）。

    已套用的前次 run（``applied_run_ids``，帳本 close 記錄的 Decision run）逐一以 manifest 驗證，且須是同帳戶、approved、cutoff 早於本次、
    ``decision.proposal_id`` 與 proposal.json 一致；該 run 的 Policy ``agent_swap.nominated_pairs`` 中 ``purpose=quality_dip`` 的換入者，
    若 proposal 同時有該股的買單與 ``AGENT_SWAP_FILL`` 旗標即為一筆成交。只留**目前仍持有**、且帳本持有起點的台北日期不早於該 run
    截止日（台北日期）的股票，entry_date 取帳本持有起點；同一股多個 run 符合時取最早的 run（建立該部位者）。任何讀取、驗證失敗回 None。"""
    if repository_root is None or applied_run_ids is None or not Path(repository_root).is_dir():
        return None
    since = _held_since_dates(policy, bundle)
    if since is None:
        return None
    try:
        from .finalization import DecisionRepository  # 延後匯入：finalization 也匯入本模組。

        account = (bundle.get("account_snapshot") or {}).get("account_id")  # type: ignore[union-attr]
        current_cutoff = parse_aware_time(bundle.get("decision_cutoff"), "decision_cutoff", error=DecisionToolError)
        known = {path.name: path for path in list_run_dirs(Path(repository_root))}
        entries: Dict[str, Dict[str, str]] = {}
        for run_id in applied_run_ids:
            run_dir = known.get(str(run_id))
            if run_dir is None:
                return None
            DecisionRepository(Path(repository_root)).verify(run_dir.name)
            docs = {}
            for name in ("decision", "decision_input", "policy", "proposal"):
                payload = json.loads((run_dir / ("%s.json" % name)).read_text(encoding="utf-8"))
                if not isinstance(payload, Mapping):
                    return None
                docs[name] = payload
            decision, prior_bundle, prior_policy, proposal = docs["decision"], docs["decision_input"], docs["policy"], docs["proposal"]
            if decision.get("status") != "approved":
                continue
            prior_cutoff = parse_aware_time(decision.get("decision_cutoff"), "decision_cutoff", error=DecisionToolError)
            if prior_cutoff >= current_cutoff:
                continue
            if (prior_bundle.get("account_snapshot") or {}).get("account_id") != account:
                continue
            if decision.get("proposal_id") != proposal.get("proposal_id"):
                return None
            swap = (prior_policy.get("position_sizing") or {}).get("agent_swap")  # type: ignore[union-attr]
            pairs = swap.get("nominated_pairs") if isinstance(swap, Mapping) else None
            buys = {
                str(pair["buy_symbol"]).upper() for pair in (pairs or [])
                if isinstance(pair, Mapping) and pair.get("purpose") == PURPOSE_QUALITY_DIP
            }
            if not buys:
                continue
            flagged = {
                str(flag.get("symbol", "")).upper() for flag in proposal.get("constraint_flags", [])
                if isinstance(flag, Mapping) and flag.get("code") == AGENT_SWAP_FILL_FLAG
            }
            ordered = {
                str(order.get("symbol", "")).upper() for order in (proposal.get("order_proposal") or {}).get("orders", [])  # type: ignore[union-attr]
                if isinstance(order, Mapping) and order.get("side") == "buy"
            }
            run_day = prior_cutoff.astimezone(TAIPEI).date().isoformat()
            for symbol in sorted(buys & flagged & ordered):
                held_day = since.get(symbol)
                if held_day is not None and held_day >= run_day and symbol not in entries:
                    entries[symbol] = {"symbol": symbol, "entry_date": held_day, "run_id": run_dir.name}
    except (DecisionToolError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return sorted(entries.values(), key=lambda entry: (entry["entry_date"], entry["symbol"]))


def build_ladder_state(
    bundle: Mapping[str, object],
    policy: Mapping[str, object],
    turn_assessment: str,
    facts: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """梯度對今天的判定。``policy`` 需有 v4 的 ``ladder`` 與（已綁定的）``exposure_rotation.held_since_by_symbol``；
    ``turn_assessment`` 是風險 Agent 本次的列舉。任何缺資料回 ``closed``（原因見 ``LADDER_CLOSED_REASONS``），不放行。"""
    ladder = _gate_or_raise(policy)
    if turn_assessment not in TURN_ASSESSMENTS:
        raise DecisionToolError("梯度狀態需要風險 Agent 的 turn_assessment（%s）" % "／".join(TURN_ASSESSMENTS))
    facts = dict(facts) if facts is not None else build_decline_depth_facts(bundle, policy)
    min_depth = decimal_value(ladder["min_pool_drawdown"], "ladder.min_pool_drawdown")
    max_exposure = decimal_value(ladder["max_dip_exposure"], "ladder.max_dip_exposure")
    max_tranches = int(ladder["max_tranches"])  # type: ignore[arg-type]
    min_gap = int(ladder["min_days_between_tranches"])  # type: ignore[arg-type]
    pause_drop = decimal_value(ladder["pause_after_further_drop"], "ladder.pause_after_further_drop")
    state: Dict[str, object] = {
        "status": "closed", "reason": None, "turn_assessment": turn_assessment, "facts_id": facts["facts_id"],
        "anchor_date": None, "pool_drawdown": None, "exposure_used": None, "exposure_remaining": _q(Decimal(0)),
        "tranches": None, "last_tranche_date": None, "days_since_last_tranche": None, "further_drop": None, "paused": None,
    }

    fills_only = ladder_counts_quality_dip_fills_only(policy.get("position_sizing"))
    if fills_only:
        state["exposure_basis"] = ladder["exposure_basis"]  # 只有新模式才有此鍵（簡報據此說明口徑）；缺席時 state 與舊版逐鍵相同

    def close(reason: str) -> Dict[str, object]:
        assert reason in LADDER_CLOSED_REASONS
        state["status"], state["reason"] = "closed", reason
        return state

    pool = facts["pool"]  # type: ignore[index]
    if facts.get("data_status") != "available" or pool["window_high_date"] is None:  # type: ignore[index]
        return close("facts_unavailable")
    high_day, depth = str(pool["window_high_date"]), Decimal(str(pool["drawdown_from_window_high"]))  # type: ignore[index]
    round_day = pool.get("round_start_date")  # type: ignore[union-attr]
    anchor = max(high_day, str(round_day)) if round_day is not None else high_day
    state["anchor_date"], state["pool_drawdown"] = anchor, pool["drawdown_from_window_high"]  # type: ignore[index]
    since = _held_since_dates(policy, bundle)
    if since is None:
        return close("held_since_unavailable")
    # exposure_basis=quality_dip_fills：只計 quality_dip 成交（綁定清單）；缺席時照舊計所有新建倉。
    counted = since
    if fills_only:
        counted = _ladder_entry_dates(policy, since)  # type: ignore[assignment]
        if counted is None:
            return close("entries_unavailable")
    weights, _, _ = holding_weights(bundle)
    new_positions = [symbol for symbol, day in counted.items() if day >= anchor]
    used = sum((weights.get(symbol, Decimal(0)) for symbol in new_positions), Decimal(0))
    tranche_days = sorted({counted[symbol] for symbol in new_positions})
    # 批次間隔與暫停線的「上一批」：從視窗高點日（較早的起點）起算，換新一輪時不會忘記上一批。
    episode_days = sorted({day for day in counted.values() if day >= high_day})
    last_day = episode_days[-1] if episode_days else None
    state.update(exposure_used=_q(used), tranches=len(tranche_days), last_tranche_date=last_day)
    trade_date = str(bundle["snapshot"]["latest_trade_date"])  # type: ignore[index]
    series = _Series(bundle, trade_date)
    days_since: Optional[int] = None
    if last_day is not None:
        days_since = sum(1 for day in series.days if last_day <= day <= trade_date)
        state["days_since_last_tranche"] = days_since
    remaining = max(max_exposure - used, Decimal(0))

    # 暫停：只有上一批之後才有；資料缺漏視為暫停中。
    paused: Optional[bool] = False
    further: Optional[Decimal] = None
    if last_day is not None:
        reference = [index for index, day in enumerate(series.days) if day < last_day]
        if not reference:
            paused = None
        else:
            ref, last = reference[-1], len(series.days) - 1
            further_value = series.cumulative(ref, last)
            further = None if further_value is None else -further_value
            trigger: Optional[int] = None
            unknown = further_value is None
            for index in range(ref + 1, last + 1):
                value = series.cumulative(ref, index)
                if value is None:
                    unknown = True
                    break
                if value <= -pause_drop:
                    trigger = index
                    break
            if unknown:
                paused = None
            elif trigger is None:
                paused = False
            else:
                later = [series.returns[index] for index in range(trigger + 1, last + 1)]
                paused = None if any(value is None for value in later) else not any(value > 0 for value in later)  # type: ignore[operator]
        state["further_drop"] = None if further is None else _q(further)
    state["paused"] = paused

    if turn_assessment == TURN_ASSESSMENTS[0] and depth < min_depth:
        return close("depth_below_gate")
    if used >= max_exposure:
        return close("exposure_full")
    if len(tranche_days) >= max_tranches:
        return close("tranches_full")
    if days_since is not None and days_since < min_gap:
        return close("tranche_interval")
    if paused is None:
        return close("pause_unavailable")
    if paused:
        return close("paused_after_drop")
    state["status"] = "open"
    state["exposure_remaining"] = _q(remaining)
    return state


def validate_ladder_state(
    bundle: Mapping[str, object], policy: Mapping[str, object], team: Optional[Mapping[str, object]], errors: List[str],
) -> None:
    """Finalizer：Policy 綁定的 ``ladder_state`` 必須等於以 bundle、Policy 與（本次綁定的）turn_assessment 重算的結果。"""
    prefix = "DecisionPolicy.position_sizing.agent_swap.quality_dip.ladder_state"
    gate = quality_dip_swap_gate(policy.get("position_sizing"))
    if gate is None or gate.get("version") != "v4":
        return
    state = gate.get("ladder_state")
    if not isinstance(state, Mapping):
        errors.append("%s 尚未綁定（梯度狀態必須由程式綁定）" % prefix)
        return
    turn = gate.get("turn_assessment")
    if ladder_counts_quality_dip_fills_only(policy.get("position_sizing")):
        # exposure_basis=quality_dip_fills：綁定的成交清單必須與帳本驗證過的持有起點相容（不重掃決策庫，信任層級同 reentry_cooldown）。
        if "ladder_entries" not in gate:
            errors.append("%s.ladder_entries 尚未綁定（quality_dip_fills 模式的成交清單必須由程式綁定）" % prefix.rsplit(".ladder_state", 1)[0])
        else:
            since = _held_since_dates(policy, bundle)
            problems = _ladder_entry_problems(policy, since) if since is not None else None
            if problems:
                errors.append("%s.ladder_entries 與持股或帳本持有起點不一致：%s" % (prefix.rsplit(".ladder_state", 1)[0], "；".join(problems)))
    facts = team.get(DECLINE_DEPTH_FACTS_KEY) if isinstance(team, Mapping) else None
    try:
        expected = build_ladder_state(bundle, policy, str(turn), facts if isinstance(facts, Mapping) else None)
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        errors.append("%s 無法重算：%s" % (prefix, error))
        return
    if dict(state) != expected:
        errors.append("%s 與重算結果不一致，拒絕改寫" % prefix)


def decline_depth_brief(facts: Mapping[str, object], state: Optional[Mapping[str, object]] = None,
                        strict_state: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    """給 Agent 的背景區塊（citable=false、沒有 evidence_id）。``state`` 給交易 Agent 的提名簡報用（讓它知道梯度今天開不開、還剩多少
    曝險）；風險 Agent 的裁決簡報不附（不附任何上限數字）。"""
    brief: Dict[str, object] = {key: facts[key] for key in ("citable", "as_of", "data_status", "note", "pool", "reference", "data_gaps")}
    if state is not None:
        brief["ladder_note"] = (
            "ladder 是抄底梯度今天的判定（上限由人填、狀態由程式算，不是你的判斷也不可引用）：status 為 closed 時 purpose=quality_dip 的配對不會成交（原因在 reason）；"
            "提名時風險 Agent 尚未判斷轉向，這裡以「有轉向跡象」的假設計算，所以不含回檔深度門檻：depth_needs_turn 為 true 表示交易池回檔"
            "尚未達人設的深度門檻，這時配對只有在風險 Agent 判斷有轉向跡象時才會成交，沒有就不成交（不是一定關閉，也不是一定開著）。你不得輸出任何數字。"
            + (
                "梯度的總抄底曝險、批數與批次間隔現在只計 purpose=quality_dip 的成交（thesis、exposure 與一般買進不占梯度額度，也不會讓隔天的間隔關閉）；"
                "exposure_remaining 是還能再抄底的曝險餘額。"
                if state.get("exposure_basis") is not None else ""
            )
        )
        brief["depth_needs_turn"] = bool(strict_state is not None and strict_state.get("reason") == "depth_below_gate")
        brief["ladder"] = {
            key: state[key] for key in (
                "status", "reason", "pool_drawdown", "exposure_remaining", "tranches", "last_tranche_date",
                "days_since_last_tranche", "paused", "turn_assessment",
            )
        }
    return brief


__all__ = [
    "DECLINE_DEPTH_FACTS_KEY", "DECLINE_DEPTH_VERSION", "build_decline_depth_facts", "build_ladder_state",
    "decline_depth_brief", "decline_depth_reference_tokens", "scan_quality_dip_entries", "validate_decline_depth_facts", "validate_ladder_state",
]
