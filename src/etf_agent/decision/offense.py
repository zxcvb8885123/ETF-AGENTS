"""轉向後進攻：``position_sizing.offense`` v1（版本閘門，只由人設定；策略反思不得改，``offense`` 前綴列在禁區）。

設計見 ``docs/plans/agent_led_offense_plan.md`` 4.1～4.10、第 6 節階段 2、7.1 節。起因：2026-11-11 的 run，風險 Agent 只有「維持防守」與「回補」兩個選擇，
防守姿態的現金目標把買單砍成 0，轉向確認前組合 beta 停在 0.13。本閘門新增一級 ``probe``（預測型、小量先試），並讓風險 Agent 的
``exposure_view`` 對映一列人填的上限：

- ``defensive_cash_buffer``：防守姿態下現金目標改為 ``min(格點值, 該級值)``（只放寬、不收緊；neutral／aggressive 不動）。
- ``max_exposure_pairs``：當天最多取用幾組 ``purpose: exposure`` 配對（與 ``agent_swap.exposure.max_pairs_by_view`` 同名鍵相等，單一出處）。
- ``max_size``：曝險配對的份量格上限（交易 Agent 填的 ``size`` 高於上限時由程式縮到上限）。
- ``beta_stop``：組合 beta 停止線，依清單先後試算，預估成交後已達此線即停止取用其後的曝險配對；是「不再加」的上限、不是目標，
  程式不為了達線挑股、不排序、不補提名。
- ``offense_off_drawdown``：帳戶回撤達此值，當天有效等級壓回 ``keep_defensive``。

Agent 只輸出列舉、代號與文字：``exposure_view``（多一個 ``probe``）、probe 時的 ``offense_basis``（事實類別列舉，至少兩類且各類事實 available）、
``failure_condition``（文字，含時間條件）、防守姿態下非 keep_defensive 時的 ``defensive_reconciliation``。所有數字都是 Policy 裡人填的欄位。

程式算的事實 ``offense_facts``（背景、``citable=false``、沒有 evidence_id、不是訊號）：賣壓竭盡、廣度回升、量能、情緒極端、指數站回五類與一組對照樣本；
只用 cutoff 之前可得的資料，缺資料該類別 ``unavailable``、不補值；存為 ``team_inputs.offense_facts``（選配鍵，與閘門雙向同時存在、須 2.9 以上），
Finalizer 以 bundle 整份重算。**缺資料永不升級**：有效等級 ``effective_view`` 由 ``apply_trade_decision`` 綁進閘門，事實不可用、回撤下限為 defensive、
帳本回撤歷史不可用、回撤達停用線時一律壓回 ``keep_defensive``。

沒有 ``offense`` 的舊 Policy／舊 bundle／舊 run 逐位元維持原重建行為；回放略過本閘門（notes 標 ``offense: ignored_in_replay``）。
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from etf_agent.core import canonical_sha256, parse_aware_time

from .decline_depth import FORWARD_HORIZONS, MIN_COVERAGE, WINDOW_DAYS, _forward, _Series
from .exposure_facts import EXPOSURE_VIEWS_WITH_PROBE, UP_DAY_LOOKBACK, _check_bars
from .fields import DecisionToolError, decimal_value, reject_unknown_fields
from .quality_dip_swap import SIZE_GRADES
from .time_condition import time_condition_error
from .turn_facts import B20_LOOKBACK_DAYS, NEW_LOW_WINDOW, TURN_ASSESSMENTS

OFFENSE_V1 = "v1"
OFFENSE_FACTS_KEY = "offense_facts"  # team_inputs 的選配鍵與簡報區塊鍵：與 Policy 閘門雙向同時存在
OFFENSE_FACTS_SCHEMA_VERSION = "1.0"
OFFENSE_VIEWS = ("probe", "start_rebuild", "rebuild")  # by_view 的鍵，由保守到進攻
KEEP_DEFENSIVE = "keep_defensive"
PROBE = "probe"
OFFENSE_CATEGORIES = ("washout", "breadth_thrust", "volume_confirmation", "sentiment_extreme", "index_reclaim")
MIN_BASIS_CATEGORIES = 2
VIEW_ROW_FIELDS = {"defensive_cash_buffer", "max_exposure_pairs", "max_size", "beta_stop"}
OFFENSE_FIELDS = {"version", "by_view", "offense_off_drawdown"}
# pipeline 在 apply_trade_decision 綁定（樣板不得預填；Finalizer 以 team_inputs 與帳本重算）：
# view＝風險 Agent 的 exposure_view、effective_view＝程式壓回後的有效等級、suppressed_reason＝壓回原因（沒壓回為 null）、
# cash_loosening＝防守姿態的現金目標被放寬時的前後值（沒放寬為 null）。
BOUND_FIELDS = {"view", "effective_view", "suppressed_reason", "cash_loosening"}
# 選配子欄位（都只有版本、沒有人填數字；沒有欄位就逐位元不變）：
# thesis_size_cap＝放寬現金期間 thesis 配對的份量格也套該級 max_size；cash_glide＝offense 放寬現金之後，防守姿態的現金回升受 cash_ratio_range.max_daily_raise 約束。
THESIS_SIZE_CAP_FIELD = "thesis_size_cap"
CASH_GLIDE_FIELD = "cash_glide"
OPTIONAL_VERSION_FIELDS = (THESIS_SIZE_CAP_FIELD, CASH_GLIDE_FIELD)
OPTIONAL_FIELD_VERSION = "v1"
# cash_glide 開啟時 pipeline 多綁一個欄位：現金回升被限制時的前後值（沒限制為 null）。
CASH_RAISE_LIMITED_FIELD = "cash_raise_limited"
SUPPRESSED_REASONS = ("facts_unavailable", "stance_floor", "drawdown_unavailable", "drawdown_limit")
CASH_LOOSENING_FIELDS = {"from", "to"}
MAX_EXPOSURE_PAIRS_RANGE = (0, 10)
CASH_BUFFER_RANGE = (Decimal("0.03"), Decimal("0.25"))
BETA_STOP_RANGE = (Decimal("0.05"), Decimal("3"))
OFF_DRAWDOWN_RANGE = (Decimal("0.01"), Decimal("0.5"))
BIG_UP_DAY_FRACTION = Decimal("0.10")
VOLUME_WINDOW = 20
QUANTUM = Decimal("0.000001")
TWO = Decimal("0.01")
FACTS_FIELDS = {
    "schema_version", "version", "facts_id", "as_of", "citable", "data_status", "note", "categories", "reference", "data_gaps",
}
FACTS_NOTE = (
    "這些是程式算的交易池與加權指數事實（citable=false、沒有 evidence_id，不可引用，理由也不要寫出 offense_facts 這個鍵名）：不是訊號，"
    "程式沒有設定任何轉向門檻；缺資料的類別標 unavailable、不補值。對照樣本取自本次行情視窗，重疊、彼此不獨立、樣本很少（small_sample），"
    "證據等級 replay_reference_only，只說明「這個視窗裡發生過什麼」，不是預測；不要補入未提供的歷史案例或後續報酬。"
)
OFFENSE_NOTE = (
    "probe 是預測型的小量先試：轉向還看不出來或尚未確認時，先用人設的小份量與停止線補一點市場參與度；start_rebuild／rebuild 是保守型，等訊號再補。"
    "每一級對映的上限（防守姿態的現金目標、曝險配對數、份量格上限、組合 beta 停止線）是人填的數字，見 levels；你只選等級、不輸出任何數字。"
    "追高被套與錯過反彈都有成本：太早進攻，可能在反彈失敗時買在高點；太晚進攻，組合 beta 留在低檔而吃不到上漲。"
    "probe 必須填 offense_basis（至少兩類程式事實，各類須 available）與 failure_condition（之後看得到、會讓這次試單算失敗的事，含時間條件）；"
    "防守姿態下非 keep_defensive 必須填 defensive_reconciliation。缺資料時程式會把有效等級壓回 keep_defensive，不會升級。"
)
SENTIMENT_CHANGE_DAYS = 5


def offense_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """``position_sizing.offense``；沒有（或 sizing 不是物件）回 None，簡報、Schema、白名單、配置與舊版逐位元相同。"""
    gate = sizing.get("offense") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def offense_bound(gate: Optional[Mapping[str, object]]) -> bool:
    """閘門已由 apply_trade_decision 綁定本次的有效等級。"""
    return isinstance(gate, Mapping) and "effective_view" in gate


def thesis_size_cap_enabled(gate: Optional[Mapping[str, object]]) -> bool:
    """``offense.thesis_size_cap`` 欄位存在（樣板層級，簡報與提示用；是否生效見 ``thesis_size_cap_active``）。"""
    return isinstance(gate, Mapping) and THESIS_SIZE_CAP_FIELD in gate


def cash_glide_enabled(gate: Optional[Mapping[str, object]]) -> bool:
    """``offense.cash_glide`` 欄位存在。"""
    return isinstance(gate, Mapping) and CASH_GLIDE_FIELD in gate


def bound_field_names(gate: Optional[Mapping[str, object]]) -> Set[str]:
    """pipeline 綁定的欄位集合：基本四個，``cash_glide`` 開啟時多 ``cash_raise_limited``。"""
    return set(BOUND_FIELDS) | ({CASH_RAISE_LIMITED_FIELD} if cash_glide_enabled(gate) else set())


def thesis_size_cap_active(gate: Optional[Mapping[str, object]]) -> bool:
    """thesis 配對的份量格上限生效：欄位開啟、已綁定、現金目標被放寬（``cash_loosening`` 不是 null）且有效等級在 ``by_view`` 內。"""
    if not thesis_size_cap_enabled(gate) or not offense_bound(gate):
        return False
    assert gate is not None
    table = gate.get("by_view")
    return gate.get("cash_loosening") is not None and isinstance(table, Mapping) and gate.get("effective_view") in table


def _q(value: Decimal) -> str:
    return str(value.quantize(QUANTUM, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- 閘門形狀與前提

def _decimal_field(raw: object, label: str, errors: List[str], low: Decimal, high: Decimal) -> Optional[Decimal]:
    if isinstance(raw, bool) or not isinstance(raw, str):
        errors.append("%s 必須是數字字串" % label)
        return None
    try:
        value = decimal_value(raw, label)
    except DecisionToolError as error:
        errors.append(str(error))
        return None
    if not low <= value <= high:
        errors.append("%s 必須介於 %s 與 %s（含）" % (label, low, high))
        return None
    return value


def _neutral_cash_floor(sizing: Mapping[str, object]) -> Optional[Decimal]:
    from .rebound import _neutral_cash_floor as floor

    return floor(sizing)


def validate_offense_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """形狀、值域、單調性與只看 position_sizing 的前提／互斥；前提的「存在」由 GATE_PREREQUISITES 檢查。全部 fail-closed。

    ``offense_off_drawdown`` 低於 ``drawdown_control.defensive_at`` 需要 Policy，由 ``validate_offense_limits`` 檢查。"""
    gate = sizing["offense"]
    prefix = "position_sizing.offense"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, OFFENSE_FIELDS | set(OPTIONAL_VERSION_FIELDS) | bound_field_names(gate), prefix, errors)
    missing = sorted(OFFENSE_FIELDS - set(gate))
    if missing:
        errors.append("%s 缺少必要欄位（由人設定，程式不提供預設值）：%s" % (prefix, "、".join(missing)))
    if gate.get("version") != OFFENSE_V1:
        errors.append("%s.version 只允許 %s" % (prefix, OFFENSE_V1))
    swap = sizing.get("agent_swap") if isinstance(sizing.get("agent_swap"), Mapping) else {}
    exposure = swap.get("exposure") if isinstance(swap.get("exposure"), Mapping) else {}  # type: ignore[union-attr]
    view_gate = sizing.get("exposure_view") if isinstance(sizing.get("exposure_view"), Mapping) else {}
    if swap.get("version") != "v2":  # type: ignore[union-attr]
        errors.append("%s 需要 agent_swap v2（配置才讀提名）" % prefix)
    if exposure.get("version") != "v2":  # type: ignore[union-attr]
        errors.append("%s 需要 agent_swap.exposure v2（風險 Agent 的 exposure_view 才決定曝險配對）" % prefix)
    if view_gate.get("version") != "v2":  # type: ignore[union-attr]
        errors.append("%s 需要 exposure_view v2（簡報與提示文字須與 exposure_view 的實際作用一致）" % prefix)
    rebound = sizing.get("rebound_participation")
    by_state = rebound.get("by_state") if isinstance(rebound, Mapping) else None
    if isinstance(by_state, Mapping) and any(isinstance(row, Mapping) and "defensive_cash_buffer" in row for row in by_state.values()):
        errors.append("%s 不得與 rebound_participation.by_state.*.defensive_cash_buffer 同時存在（防守姿態的現金放寬只能有一個出處）" % prefix)
    table = gate.get("by_view")
    if "by_view" in gate and (not isinstance(table, Mapping) or set(table) != set(OFFENSE_VIEWS)):
        errors.append("%s.by_view 必須剛好有 %s" % (prefix, "、".join(OFFENSE_VIEWS)))
        table = None
    parsed: Dict[str, Dict[str, object]] = {}
    floor_ratio = _neutral_cash_floor(sizing)
    aggressive = None
    targets = (sizing.get("exposure_rotation") or {}).get("beta_target_by_stance") if isinstance(sizing.get("exposure_rotation"), Mapping) else None  # type: ignore[union-attr]
    if isinstance(targets, Mapping) and targets.get("aggressive") is not None:
        try:
            aggressive = decimal_value(targets["aggressive"], "exposure_rotation.beta_target_by_stance.aggressive")
        except DecisionToolError:
            aggressive = None
    caps = exposure.get("max_pairs_by_view") if isinstance(exposure.get("max_pairs_by_view"), Mapping) else {}  # type: ignore[union-attr]
    limit = swap.get("max_swaps_per_run")  # type: ignore[union-attr]
    if isinstance(table, Mapping):
        for view in OFFENSE_VIEWS:
            row = table[view]
            label = "%s.by_view.%s" % (prefix, view)
            if not isinstance(row, Mapping):
                errors.append("%s 必須是物件" % label)
                continue
            reject_unknown_fields(row, VIEW_ROW_FIELDS, label, errors)
            absent = sorted(VIEW_ROW_FIELDS - set(row))
            if absent:
                errors.append("%s 缺少必要欄位（由人設定，程式不提供預設值）：%s" % (label, "、".join(absent)))
                continue
            entry: Dict[str, object] = {}
            entry["buffer"] = _decimal_field(row["defensive_cash_buffer"], "%s.defensive_cash_buffer" % label, errors, *CASH_BUFFER_RANGE)
            entry["beta"] = _decimal_field(row["beta_stop"], "%s.beta_stop" % label, errors, *BETA_STOP_RANGE)
            pairs = row["max_exposure_pairs"]
            if isinstance(pairs, bool) or not isinstance(pairs, int) or not MAX_EXPOSURE_PAIRS_RANGE[0] <= pairs <= MAX_EXPOSURE_PAIRS_RANGE[1]:
                errors.append("%s.max_exposure_pairs 必須是 %d～%d 的整數" % (label, *MAX_EXPOSURE_PAIRS_RANGE))
            else:
                entry["pairs"] = pairs
                if isinstance(limit, int) and not isinstance(limit, bool) and pairs > limit:
                    errors.append("%s.max_exposure_pairs 不得高於 agent_swap.max_swaps_per_run（只決定誰先、不放寬現行上限）" % label)
                if caps.get(view) != pairs:
                    errors.append("%s.max_exposure_pairs 必須與 agent_swap.exposure.max_pairs_by_view.%s 相等（對數上限單一出處）" % (label, view))
            if row["max_size"] not in SIZE_GRADES:
                errors.append("%s.max_size 必須是 %s" % (label, "／".join(SIZE_GRADES)))
            else:
                entry["size"] = SIZE_GRADES.index(str(row["max_size"]))
            buffer = entry.get("buffer")
            if isinstance(buffer, Decimal) and floor_ratio is not None and buffer < floor_ratio:
                errors.append("%s.defensive_cash_buffer 不得低於中性姿態最低現金比例（%s）" % (label, floor_ratio))
            stop = entry.get("beta")
            if isinstance(stop, Decimal) and aggressive is not None and stop > aggressive:
                errors.append("%s.beta_stop 不得高於 exposure_rotation.beta_target_by_stance.aggressive（%s）" % (label, aggressive))
            parsed[view] = entry
        for lower, higher in zip(OFFENSE_VIEWS, OFFENSE_VIEWS[1:]):
            a, b = parsed.get(lower, {}), parsed.get(higher, {})
            if isinstance(a.get("buffer"), Decimal) and isinstance(b.get("buffer"), Decimal) and b["buffer"] > a["buffer"]:  # type: ignore[operator]
                errors.append("%s.by_view 的 defensive_cash_buffer 必須由保守到進攻不升（%s 不得高於 %s）" % (prefix, higher, lower))
            for key, label in (("pairs", "max_exposure_pairs"), ("size", "max_size"), ("beta", "beta_stop")):
                if key in a and key in b and b[key] < a[key]:  # type: ignore[operator]
                    errors.append("%s.by_view 的 %s 必須由保守到進攻不降（%s 不得低於 %s）" % (prefix, label, higher, lower))
    if "offense_off_drawdown" in gate:
        _decimal_field(gate["offense_off_drawdown"], "%s.offense_off_drawdown" % prefix, errors, *OFF_DRAWDOWN_RANGE)
    for name in OPTIONAL_VERSION_FIELDS:
        if name in gate and (not isinstance(gate[name], Mapping) or dict(gate[name]) != {"version": OPTIONAL_FIELD_VERSION}):
            errors.append('%s.%s 必須剛好是 {"version": "%s"}（沒有人填數字）' % (prefix, name, OPTIONAL_FIELD_VERSION))
    if cash_glide_enabled(gate) and not isinstance(sizing.get("cash_ratio_range"), Mapping):
        errors.append("%s.cash_glide 需要 cash_ratio_range（沿用其 max_daily_raise 與前次現金比例 anchor）" % prefix)
    bound = bound_field_names(gate) & set(gate)
    if bound:
        _validate_bound(gate, prefix, errors, bound)


def _validate_bound(gate: Mapping[str, object], prefix: str, errors: List[str], present: Set[str]) -> None:
    """綁定欄位（只能由程式綁定）：四個一起出現、值域固定、彼此一致。"""
    if present != bound_field_names(gate):
        errors.append("%s 的 view／effective_view／suppressed_reason／cash_loosening%s 必須一起出現（只能由 pipeline 綁定）" % (
            prefix, "／cash_raise_limited" if cash_glide_enabled(gate) else ""))
        return
    view, effective, reason, loosening = gate["view"], gate["effective_view"], gate["suppressed_reason"], gate["cash_loosening"]
    if view not in EXPOSURE_VIEWS_WITH_PROBE or effective not in EXPOSURE_VIEWS_WITH_PROBE:
        errors.append("%s.view／effective_view 必須是 %s" % (prefix, "／".join(EXPOSURE_VIEWS_WITH_PROBE)))
        return
    if reason is not None and reason not in SUPPRESSED_REASONS:
        errors.append("%s.suppressed_reason 只允許 %s 或 null" % (prefix, "／".join(SUPPRESSED_REASONS)))
    if (reason is not None) != (effective != view):
        errors.append("%s.effective_view 與 view 不同時必須有 suppressed_reason，相同時不得有" % prefix)
    elif reason is not None and effective != KEEP_DEFENSIVE:
        errors.append("%s 壓回時有效等級只能是 keep_defensive" % prefix)
    if loosening is not None and (
        not isinstance(loosening, Mapping) or set(loosening) != CASH_LOOSENING_FIELDS or not all(isinstance(v, str) for v in loosening.values())
    ):
        errors.append("%s.cash_loosening 必須是 null 或剛好包含 from／to 字串（只能由程式綁定）" % prefix)
    elif loosening is not None and effective == KEEP_DEFENSIVE:
        errors.append("%s.cash_loosening 只允許有效等級不是 keep_defensive" % prefix)
    if cash_glide_enabled(gate):
        limited = gate[CASH_RAISE_LIMITED_FIELD]
        if limited is not None and (
            not isinstance(limited, Mapping) or set(limited) != CASH_LOOSENING_FIELDS or not all(isinstance(v, str) for v in limited.values())
        ):
            errors.append("%s.cash_raise_limited 必須是 null 或剛好包含 from／to 字串（只能由程式綁定）" % prefix)


def validate_offense_limits(policy: Mapping[str, object], errors: List[str]) -> None:
    """需要 Policy 的檢查：``offense_off_drawdown`` 必須低於 ``drawdown_control.defensive_at``（否則停用線在強制防守線之後才生效）。"""
    sizing = policy.get("position_sizing")
    gate = offense_gate(sizing)
    if gate is None or not isinstance(gate.get("offense_off_drawdown"), str):
        return
    control = policy.get("drawdown_control")
    if not isinstance(control, Mapping) or "defensive_at" not in control:
        errors.append("position_sizing.offense 需要 drawdown_control.defensive_at（停用線須低於強制防守線）")
        return
    try:
        off = decimal_value(gate["offense_off_drawdown"], "offense.offense_off_drawdown")
        defensive = decimal_value(control["defensive_at"], "drawdown_control.defensive_at")
    except DecisionToolError:
        return
    if off >= defensive:
        errors.append("position_sizing.offense.offense_off_drawdown 必須低於 drawdown_control.defensive_at（%s）" % defensive)


# ---------------------------------------------------------------- 事實

def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _category(status: str, **fields: object) -> Dict[str, object]:
    return {"status": status, **fields}


def _washout(series: _Series, last: int, gaps: List[str]) -> Dict[str, object]:
    pool = series.pool
    if last < 0:
        return _category("unavailable")
    shares: List[Decimal] = []
    for index in range(max(last - B20_LOOKBACK_DAYS + 1, 0), last + 1):
        raw = pool.share(series.days[index], NEW_LOW_WINDOW, lambda c: c[-1] <= min(c[-NEW_LOW_WINDOW:]))
        if raw is None:
            gaps.append("washout：%s 的 20 日新低比例沒有足夠筆數的股票" % series.days[index])
            return _category("unavailable")
        shares.append(Decimal(raw))
    depth = series.drawdown(last)
    if depth is None:
        gaps.append("washout：離 %d 日高點的回檔無法計算（交易日不足 %d 個或日報酬覆蓋不足）" % (WINDOW_DAYS, WINDOW_DAYS))
        return _category("unavailable")
    streak = 0
    for index in range(last, 0, -1):
        value = series.returns[index]
        if value is None:
            gaps.append("washout：連續下跌日數遇到無法計算的日報酬（覆蓋不足）")
            return _category("unavailable")
        if value >= 0:
            break
        streak += 1
    return _category(
        "available", new_low_20d_share_latest=_q(shares[-1]), new_low_20d_share_max_5d=_q(max(shares)),
        new_low_lookback_days=len(shares), drawdown_from_window_high=_q(depth["drawdown"]),  # type: ignore[arg-type]
        down_streak_days=streak,
    )


def _up_share(series: _Series, last: int) -> Optional[Decimal]:
    from bisect import bisect_right

    today, before = series.days[last], series.days[last - 1]
    values: List[Decimal] = []
    for symbol, dates in series.pool.dates_by_symbol.items():
        closes = series.pool.closes_by_symbol[symbol]
        position = bisect_right(dates, today)
        if position < 2 or dates[position - 1] != today or dates[position - 2] != before or closes[position - 2] <= 0:
            continue
        values.append(closes[position - 1] / closes[position - 2] - Decimal(1))
    if not values or Decimal(len(values)) / Decimal(series.pool.size) < MIN_COVERAGE:
        return None
    return Decimal(sum(1 for value in values if value > 0)) / Decimal(len(values))


def _breadth_thrust(series: _Series, last: int, gaps: List[str]) -> Dict[str, object]:
    if last < 5:
        gaps.append("breadth_thrust：交易日不足，無法計算站上 MA5 比例的 5 日變化")
        return _category("unavailable")
    pool = series.pool

    def share(index: int) -> Optional[Decimal]:
        raw = pool.share(series.days[index], 5, lambda c: c[-1] >= sum(c[-5:]) / Decimal(5))
        return None if raw is None else Decimal(raw)

    latest = share(last)
    earlier = {days: share(last - days) for days in (1, 3, 5)}
    up = _up_share(series, last)
    if latest is None or any(value is None for value in earlier.values()) or up is None:
        gaps.append("breadth_thrust：站上 MA5 比例或上漲檔數占比沒有足夠筆數的股票")
        return _category("unavailable")
    return _category(
        "available", share_above_ma5_latest=_q(latest),
        **{"share_above_ma5_change_%dd" % days: _q(latest - value) for days, value in earlier.items()},  # type: ignore[operator]
        up_share_latest=_q(up),
    )


def _volume_confirmation(bundle: Mapping[str, object], series: _Series, last: int, gaps: List[str]) -> Dict[str, object]:
    found: Optional[int] = None
    for index in range(last, max(last - UP_DAY_LOOKBACK, 0), -1):
        value = series.returns[index]
        if value is not None and value > 0:
            found = index
            break
    if found is None:
        gaps.append("volume_confirmation：最近 %d 個交易日內交易池沒有上漲日" % UP_DAY_LOOKBACK)
        return _category("unavailable")
    day = series.days[found]
    ratios: List[Decimal] = []
    for row in bundle["price_series"]:  # type: ignore[union-attr]
        bars = sorted(row["bars"], key=lambda bar: str(bar["trade_date"]))
        dates = [str(bar["trade_date"]) for bar in bars]
        if day not in dates:
            continue
        position = dates.index(day)
        if position < VOLUME_WINDOW:
            continue
        volumes = [Decimal(str(bar["volume"])) for bar in bars[position - VOLUME_WINDOW:position + 1]]
        average = sum(volumes[:-1], Decimal(0)) / Decimal(VOLUME_WINDOW)
        if average > 0:
            ratios.append(volumes[-1] / average)
    if not ratios or Decimal(len(ratios)) / Decimal(series.pool.size) < MIN_COVERAGE:
        gaps.append("volume_confirmation：上漲日 %s 有足夠 %d 日均量的股票不足交易池的 %s" % (day, VOLUME_WINDOW, MIN_COVERAGE))
        return _category("unavailable")
    return _category(
        "available", up_day_date=day, is_latest_trade_date=found == last, trading_days_ago=last - found,
        median_volume_ratio_20d=_q(_median(ratios)), covered_symbols=len(ratios), pool_size=series.pool.size,
    )


def _sentiment_extreme(
    bundle: Mapping[str, object], trade_date: str, gaps: List[str], trade_days: Sequence[str] = (),
) -> Dict[str, object]:
    pair = bundle.get("market_fear_greed_input")
    result = pair.get("result") if isinstance(pair, Mapping) else None
    if not isinstance(result, Mapping):
        gaps.append("sentiment_extreme：決策輸入沒有恐懼貪婪結果")
        return _category("unavailable")
    for component in result.get("components") or []:
        if isinstance(component, Mapping) and component.get("as_of") and str(component["as_of"]) > trade_date:
            # 資料日晚於最近交易日：不能當截止日的事實用（也不回填），整類標 unavailable 並記缺口，不中斷整個 run。
            gaps.append("sentiment_extreme：恐懼貪婪成分 %s 的資料日 %s 晚於台股最近交易日 %s，不納入（缺資料不升級）"
                        % (component.get("source"), component["as_of"], trade_date))
            return _category("unavailable")
    if result.get("status") != "available" or not isinstance(result.get("score"), str):
        gaps.append("sentiment_extreme：恐懼貪婪指標不可用（已核准來源不足）")
        return _category("unavailable")
    return _category(
        "available", score=str(result["score"]), band=result.get("band"),
        score_change_5d=_score_change_5d(pair, str(result["score"]), trade_date, trade_days, gaps),
    )


def _score_change_5d(
    pair: Mapping[str, object], score: str, trade_date: str, trade_days: Sequence[str], gaps: List[str],
) -> Optional[str]:
    """由資料包原始回應重算「5 個交易日前」的正式分數（只用內容日不晚於該日的列），回傳 ``score - 前分數``；任何一步缺資料回 None 並記缺口。

    當日分數必須與結果的分數一致（不一致代表窗口或來源有落差）才算；不補值、不回填未來。"""
    from etf_agent.perception.market_fear_greed.contracts import FearGreedError
    from etf_agent.perception.market_fear_greed.tools import score_as_of

    pack = pair.get("bundle")
    if not isinstance(pack, Mapping):
        gaps.append("sentiment_extreme：決策輸入沒有恐懼貪婪資料包，無法計算 5 日變化")
        return None
    days = [day for day in trade_days if day <= trade_date]
    if len(days) < SENTIMENT_CHANGE_DAYS + 1 or days[-1] != trade_date:
        gaps.append("sentiment_extreme：交易日不足 %d 個或最後一日不是最近交易日，無法計算 5 日變化" % (SENTIMENT_CHANGE_DAYS + 1))
        return None
    ref_day = days[-1 - SENTIMENT_CHANGE_DAYS]
    try:
        now, before = score_as_of(dict(pack), trade_date), score_as_of(dict(pack), ref_day)
    except FearGreedError as error:
        gaps.append("sentiment_extreme：重算歷史分數失敗（%s），不計算 5 日變化" % error)
        return None
    if now != score:
        gaps.append("sentiment_extreme：以原始回應重算的截止日分數 %s 與結果分數 %s 不同，不計算 5 日變化" % (now, score))
        return None
    if before is None:
        gaps.append("sentiment_extreme：%s 的歷史分數不可用（已核准成分不足或資料不足），不計算 5 日變化" % ref_day)
        return None
    return str((Decimal(score) - Decimal(before)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _index_reclaim(bundle: Mapping[str, object], trade_date: str, gaps: List[str]) -> Dict[str, object]:
    from .market_index import _series

    local: List[str] = []
    series = _series(bundle, local)
    gaps.extend("index_reclaim：%s" % gap for gap in local)
    later = sorted(day for day in series if day > trade_date)
    if later:
        gaps.append("index_reclaim：加權指數 %s 晚於 snapshot.latest_trade_date %s，不納入（缺資料不升級）" % (later[0], trade_date))
        return _category("unavailable")
    days = sorted(series)
    if not days:
        return _category("unavailable")
    if days[-1] != trade_date:
        gaps.append("index_reclaim：加權指數最後一個交易日 %s 不是 snapshot.latest_trade_date %s" % (days[-1], trade_date))
        return _category("unavailable")
    closes = [series[day] for day in days]
    if len(closes) < 20:
        gaps.append("index_reclaim：加權指數歷史只有 %d 日，無法計算 MA20" % len(closes))
        return _category("unavailable")
    latest = closes[-1]
    ma5, ma20 = sum(closes[-5:]) / Decimal(5), sum(closes[-20:]) / Decimal(20)
    fields: Dict[str, object] = {
        "latest_date": days[-1], "close": str(latest.quantize(TWO, rounding=ROUND_HALF_UP)),
        "above_ma5": latest >= ma5, "above_ma20": latest >= ma20,
        "close_vs_ma5_pct": str(((latest / ma5 - 1) * 100).quantize(TWO, rounding=ROUND_HALF_UP)),
        "close_vs_ma20_pct": str(((latest / ma20 - 1) * 100).quantize(TWO, rounding=ROUND_HALF_UP)),
    }
    if len(closes) >= 60:
        fields["drawdown_from_60d_high_pct"] = str(((latest / max(closes[-60:]) - 1) * 100).quantize(TWO, rounding=ROUND_HALF_UP))
    else:
        fields["drawdown_from_60d_high_pct"] = None
        gaps.append("index_reclaim：加權指數歷史只有 %d 日，無法計算 60 日高點回檔" % len(closes))
    return _category("available", **fields)


def _reference(series: _Series, last: int) -> Dict[str, object]:
    """對照樣本：本次行情視窗內交易池單日漲幅落在前 10% 的日子，之後 3／5 個交易日的報酬分布。前瞻視窗不得越過最近交易日。"""
    candidates = [(series.returns[index], index) for index in range(1, last + 1) if series.returns[index] is not None]
    events: List[int] = []
    threshold: Optional[Decimal] = None
    if candidates:
        take = max(1, math.ceil(len(candidates) * BIG_UP_DAY_FRACTION))
        ordered = sorted((value for value, _ in candidates), reverse=True)  # type: ignore[type-var]
        threshold = ordered[take - 1]
        events = [index for value, index in candidates if value >= threshold and value > 0]  # type: ignore[operator]
    return {
        "evidence_grade": "replay_reference_only",
        "note": "樣本取自本次行情視窗，重疊、彼此不獨立；n 很小，不是預測，也不是驗證。",
        "event": "pool_up_day_top_decile", "top_fraction": str(BIG_UP_DAY_FRACTION), "sample_days": len(candidates),
        "min_return": None if threshold is None else _q(threshold), "forward_horizons": list(FORWARD_HORIZONS),
        "event_dates": [series.days[i] for i in events], **_forward(series, events),
    }


def build_offense_facts(bundle: Mapping[str, object], policy: Mapping[str, object]) -> Dict[str, object]:
    """完整事實（``team_inputs.offense_facts``）：只依 bundle，Policy 只用來確認 offense 閘門存在；不讀任何 Agent 輸出。

    bar 晚於最近交易日、可得時間缺時區或晚於 ``decision_cutoff`` 一律拒絕；缺資料只讓該類別 ``unavailable``，不補值。"""
    if offense_gate(policy.get("position_sizing")) is None:
        raise DecisionToolError("DecisionPolicy 沒有 position_sizing.offense，不應建立進攻事實")
    snapshot = bundle.get("snapshot")
    trade_date = snapshot.get("latest_trade_date") if isinstance(snapshot, Mapping) else None
    if not isinstance(trade_date, str):
        raise DecisionToolError("offense_facts 需要 snapshot.latest_trade_date")
    if not isinstance(bundle.get("price_series"), list):
        raise DecisionToolError("offense_facts 需要 price_series")
    cutoff = parse_aware_time(bundle.get("decision_cutoff"), "decision_cutoff", error=DecisionToolError)
    _check_bars(bundle, trade_date, cutoff)
    series = _Series(bundle, trade_date)
    last = len(series.days) - 1
    gaps: List[str] = []
    if last < 0:
        gaps.append("price_series 沒有任何行情，行情類事實無法計算")
    elif series.days[-1] != trade_date:
        gaps.append("行情最後一個交易日 %s 不是 snapshot.latest_trade_date %s" % (series.days[-1], trade_date))
    stale = last >= 0 and series.days[-1] != trade_date
    unavailable = _category("unavailable")
    categories = {
        "washout": unavailable if last < 0 or stale else _washout(series, last, gaps),
        "breadth_thrust": unavailable if last < 0 or stale else _breadth_thrust(series, last, gaps),
        "volume_confirmation": unavailable if last < 0 or stale else _volume_confirmation(bundle, series, last, gaps),
        "sentiment_extreme": _sentiment_extreme(bundle, trade_date, gaps, series.days if last >= 0 else []),
        "index_reclaim": _index_reclaim(bundle, trade_date, gaps),
    }
    body: Dict[str, object] = {
        "schema_version": OFFENSE_FACTS_SCHEMA_VERSION,
        "version": OFFENSE_V1,
        "as_of": trade_date,
        "citable": False,
        "data_status": "available" if any(row["status"] == "available" for row in categories.values()) else "unavailable",
        "note": FACTS_NOTE,
        "categories": categories,
        "reference": _reference(series, last) if last >= 0 and not stale else {"evidence_grade": "replay_reference_only", "event_dates": []},
        "data_gaps": gaps,
    }
    body["facts_id"] = "offense-facts:" + canonical_sha256(body)[:20]
    return body


def validate_offense_facts(
    bundle: Mapping[str, object], policy: Mapping[str, object], team: Mapping[str, object], errors: List[str],
) -> None:
    """以 bundle 與 Policy 整份重算 ``team_inputs.offense_facts``；缺少、多餘欄位或不一致即拒絕。"""
    prefix = "team_inputs.offense_facts"
    facts = team.get(OFFENSE_FACTS_KEY)
    if not isinstance(facts, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    if offense_gate(policy.get("position_sizing")) is None:
        errors.append("%s 只能用於啟用 position_sizing.offense 的 Policy" % prefix)
        return
    reject_unknown_fields(facts, FACTS_FIELDS, prefix, errors)
    try:
        expected = build_offense_facts(bundle, policy)
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        errors.append("%s 無法以共同輸入重算：%s" % (prefix, error))
        return
    if dict(facts) != expected:
        errors.append("%s 與重算結果不同，拒絕改寫" % prefix)


def offense_reference_tokens(facts: Optional[Mapping[str, object]]) -> List[str]:
    """文字欄位不得出現的識別碼（背景不是證據）：簡報區塊鍵名，以及有事實時的 facts_id。"""
    tokens = [OFFENSE_FACTS_KEY]
    if isinstance(facts, Mapping) and isinstance(facts.get("facts_id"), str) and facts["facts_id"].strip():
        tokens.append(str(facts["facts_id"]))
    return tokens


def offense_levels(gate: Mapping[str, object]) -> Dict[str, Dict[str, object]]:
    """各等級對映的人填上限（給風險 Agent 簡報；都是 Policy 的欄位，不是程式的判斷）。"""
    return {view: dict(gate["by_view"][view]) for view in OFFENSE_VIEWS}  # type: ignore[index]


def offense_brief(facts: Mapping[str, object], gate: Mapping[str, object], include_reference: bool = True) -> Dict[str, object]:
    """風險 Agent 的背景區塊（citable=false、沒有 evidence_id）；附各等級對映的人填上限，但不附交易 Agent 的任何意圖或信心統計。
    ``include_reference=False``（cash_stance_brief.offense_reference=omit）時不附 ``reference`` 對照樣本；預設行為不變。"""
    brief = {
        "citable": False,
        "note": OFFENSE_NOTE,
        "as_of": facts["as_of"],
        "data_status": facts["data_status"],
        "categories": facts["categories"],
        "reference": facts["reference"],
        "levels": offense_levels(gate),
        "offense_off_drawdown": gate["offense_off_drawdown"],
        "data_gaps": facts["data_gaps"],
    }
    if not include_reference:
        brief.pop("reference")
    return brief


# ---------------------------------------------------------------- 風險 Agent 的列舉驗證

def non_news_market_ids(bundle: Mapping[str, object], overnight_ids: Set[str]) -> Set[str]:
    """市場層級證據中「不是新聞」的識別碼：恐懼貪婪成分、隔夜市場、市場狀態編號（新聞是不受信任的文字，不能單獨支持進攻）。"""
    from .input_context import MARKET_REGIME_EVIDENCE_ID

    ids: Set[str] = {MARKET_REGIME_EVIDENCE_ID, *overnight_ids}
    pair = bundle.get("market_fear_greed_input")
    result = pair.get("result") if isinstance(pair, Mapping) else None
    for component in (result.get("components") or []) if isinstance(result, Mapping) else []:
        if isinstance(component, Mapping) and component.get("evidence_id"):
            ids.add(str(component["evidence_id"]))
    return ids


FIELD_RESET_REMINDER = (
    "提醒：改等級時請清空不適用的欄位（keep_defensive 不得填 defensive_reconciliation、offense_basis、failure_condition；"
    "start_rebuild／rebuild 不得填 offense_basis、failure_condition）"
)


def normalize_offense_stance(output: Mapping[str, object]) -> Dict[str, object]:
    """Codex strict Schema 要求欄位全列入 required：不適用的以空陣列／空字串送出，封存時還原成缺席（其餘欄位原樣保留）。"""
    result = dict(output)
    for key in ("offense_basis", "failure_condition", "defensive_reconciliation"):
        value = result.get(key)
        if value == [] or (isinstance(value, str) and not value.strip()):
            result.pop(key)
    return result


def validate_offense_stance(
    stance: Mapping[str, object],
    evidence: Sequence[str],
    market_evidence_ids: object,
    non_news_ids: Set[str],
    facts: Optional[Mapping[str, object]],
    reference_tokens: Sequence[str],
    prefix: str,
    errors: List[str],
) -> None:
    """offense 閘門啟用時的現金姿態驗證（exposure_view 的值域另由 ``validate_exposure_view`` 檢查）：

    - 與 ``turn_assessment`` 的一致性（計畫 4.1）：probe 配 no_turn_evidence／turn_unconfirmed、start_rebuild 至少 turn_unconfirmed、rebuild 須 turn_confirmed；
      不一致退回重答，不由程式夾到邊界。
    - 非 keep_defensive：evidence_ids 至少兩個市場層級證據，且至少一個不是新聞。
    - probe：``offense_basis``（至少兩個不同類別、各類事實 available）與 ``failure_condition`` 必填；其他等級不得填。
    - 姿態為 defensive：非 keep_defensive 須填 ``defensive_reconciliation``；keep_defensive 或其他姿態不得填。
    - 文字不得寫出背景識別碼；全部只有列舉與文字，沒有數字欄位。"""
    before = len(errors)
    _validate_offense_stance(stance, evidence, market_evidence_ids, non_news_ids, facts, reference_tokens, prefix, errors)
    if len(errors) > before:
        errors.append(FIELD_RESET_REMINDER)


def _validate_offense_stance(
    stance: Mapping[str, object],
    evidence: Sequence[str],
    market_evidence_ids: object,
    non_news_ids: Set[str],
    facts: Optional[Mapping[str, object]],
    reference_tokens: Sequence[str],
    prefix: str,
    errors: List[str],
) -> None:
    view = stance.get("exposure_view")
    if view not in EXPOSURE_VIEWS_WITH_PROBE:
        return  # 值域錯誤已由 validate_exposure_view 回報
    offensive = view != KEEP_DEFENSIVE
    turn = stance.get("turn_assessment")
    if offensive and turn in TURN_ASSESSMENTS:
        index = TURN_ASSESSMENTS.index(str(turn))
        allowed = {PROBE: (0, 1), "start_rebuild": (1, 2), "rebuild": (2, 2)}[str(view)]
        if not allowed[0] <= index <= allowed[1]:
            errors.append(
                "%s.exposure_view=%s 與 turn_assessment=%s 不一致（probe 配 no_turn_evidence／turn_unconfirmed、start_rebuild 至少 turn_unconfirmed、"
                "rebuild 須 turn_confirmed）；請改選相符的等級或重新判斷轉向，程式不會替你調整" % (prefix, view, turn)
            )
    if offensive:
        market = set(evidence) & set(market_evidence_ids or ())  # type: ignore[arg-type]
        if len(market) < 2 or not market & non_news_ids:
            errors.append(
                "%s.exposure_view=%s 時 evidence_ids 至少要引用兩個市場層級證據，其中至少一個不是新聞（恐懼貪婪成分、隔夜市場或市場狀態）；"
                "新聞是不受信任的文字，不能單獨支持進攻" % (prefix, view)
            )
    basis, failure, recon = stance.get("offense_basis"), stance.get("failure_condition"), stance.get("defensive_reconciliation")
    texts: List[Tuple[str, object]] = [("rationale", stance.get("rationale"))]
    if view == PROBE:
        categories = (facts or {}).get("categories") if isinstance(facts, Mapping) else None
        if not isinstance(basis, list) or not all(isinstance(item, str) for item in basis):
            errors.append("%s.offense_basis 必須是類別列舉的陣列（%s）" % (prefix, "／".join(OFFENSE_CATEGORIES)))
        else:
            bad = sorted({item for item in basis if item not in OFFENSE_CATEGORIES})
            if bad:
                errors.append("%s.offense_basis 含不在列舉內的類別：%s（%s）" % (prefix, "、".join(bad), "／".join(OFFENSE_CATEGORIES)))
            distinct = {item for item in basis if item in OFFENSE_CATEGORIES}
            if len(distinct) < MIN_BASIS_CATEGORIES or len(distinct) != len(basis):
                errors.append("%s.offense_basis 必須至少 %d 個不同類別、不得重複" % (prefix, MIN_BASIS_CATEGORIES))
            if not isinstance(categories, Mapping):
                errors.append("%s：沒有 offense_facts，probe 無法核對依據類別" % prefix)
            else:
                dead = sorted(item for item in distinct if not isinstance(categories.get(item), Mapping) or categories[item].get("status") != "available")  # type: ignore[union-attr]
                if dead:
                    errors.append("%s.offense_basis 的類別事實不是 available：%s；請只選程式有算出事實的類別，或改填 keep_defensive" % (prefix, "、".join(dead)))
        if not isinstance(failure, str) or not failure.strip():
            errors.append("%s.failure_condition 需要非空文字（之後看得到、會讓這次試單算失敗的事，含時間條件）；請補寫，或改選較保守的等級，擇一即可" % prefix)
        else:
            problem = time_condition_error(failure)
            if problem:
                errors.append("%s.failure_condition %s" % (prefix, problem))
            texts.append(("failure_condition", failure))
    else:
        for field in ("offense_basis", "failure_condition"):
            if field in stance:
                errors.append("%s.%s 只能在 exposure_view=probe 時填寫" % (prefix, field))
    defensive_level = stance.get("level") == "defensive"
    if offensive and defensive_level:
        if not isinstance(recon, str) or not recon.strip():
            errors.append("%s.defensive_reconciliation 必填：防守姿態下非 keep_defensive，須說明防守的現金判斷與進攻為什麼不矛盾" % prefix)
        else:
            texts.append(("defensive_reconciliation", recon))
    elif "defensive_reconciliation" in stance:
        errors.append("%s.defensive_reconciliation 只能在姿態為 defensive 且 exposure_view 不是 keep_defensive 時填寫" % prefix)
    for name, text in texts:
        if not isinstance(text, str):
            continue
        cited = sorted(token for token in reference_tokens if token and token in text)
        if cited:
            errors.append("%s.%s 不得寫出背景識別碼（%s）；它不是證據、也不是前次決策的延續" % (prefix, name, ", ".join(cited)))


# ---------------------------------------------------------------- 有效等級與綁定

def effective_view(
    gate: Mapping[str, object], stance: Mapping[str, object], control: object, facts: Optional[Mapping[str, object]],
) -> Tuple[str, Optional[str]]:
    """風險 Agent 的 exposure_view 經程式壓回後的有效等級：``(有效等級, 壓回原因)``。缺資料永不升級。

    keep_defensive 原樣；其餘在事實不可用、回撤下限為 defensive、帳本回撤歷史不可用、回撤達 ``offense_off_drawdown`` 時壓回 keep_defensive。"""
    view = stance.get("exposure_view")
    if view not in EXPOSURE_VIEWS_WITH_PROBE:
        raise DecisionToolError("Policy 啟用 offense，現金姿態必須給 exposure_view（%s）" % "／".join(EXPOSURE_VIEWS_WITH_PROBE))
    if view == KEEP_DEFENSIVE:
        return KEEP_DEFENSIVE, None
    if not isinstance(facts, Mapping) or facts.get("data_status") != "available":
        return KEEP_DEFENSIVE, "facts_unavailable"
    if not isinstance(control, Mapping) or control.get("status") != "available" or control.get("drawdown") is None:
        return KEEP_DEFENSIVE, "drawdown_unavailable"
    if control.get("stance_floor") == "defensive":
        return KEEP_DEFENSIVE, "stance_floor"
    try:
        off = decimal_value(gate["offense_off_drawdown"], "offense.offense_off_drawdown")
        drawdown = decimal_value(control["drawdown"], "drawdown_control.drawdown")
    except DecisionToolError:
        return KEEP_DEFENSIVE, "drawdown_unavailable"
    if drawdown >= off:
        return KEEP_DEFENSIVE, "drawdown_limit"
    return str(view), None


def offense_cash_target(
    gate: Mapping[str, object], level: object, base: object,
) -> Tuple[object, Optional[Dict[str, str]]]:
    """有效等級非 keep_defensive 且現金姿態為 defensive 時，現金目標改為 ``min(格點值, 該級 defensive_cash_buffer)``（只放寬）。
    回傳（現金比例原字串, ``cash_loosening``；沒放寬回 None）。gate 必須已綁定有效等級。"""
    if level != "defensive":
        return base, None
    effective = gate.get("effective_view")
    if effective not in OFFENSE_VIEWS:
        return base, None
    target = gate["by_view"][effective]["defensive_cash_buffer"]  # type: ignore[index]
    if decimal_value(target, "defensive_cash_buffer") < decimal_value(base, "cash_buffer_rate"):
        return target, {"from": str(base), "to": str(target)}
    return base, None


def _ratio_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def drawdown_allows_glide(gate: Mapping[str, object], control: object) -> bool:
    """cash_glide 的回撤條件：回撤下限不是 defensive、帳本回撤可用且低於 ``offense_off_drawdown``；任一不成立回 False（方向是多留現金，不滑行）。

    與 ``effective_view`` 分開算：風險 Agent 選 keep_defensive 時 effective_view 不看回撤，但滑行仍須回撤條件。"""
    if not isinstance(control, Mapping) or control.get("status") != "available" or control.get("drawdown") is None:
        return False
    if control.get("stance_floor") == "defensive":
        return False
    try:
        off = decimal_value(gate["offense_off_drawdown"], "offense.offense_off_drawdown")
        drawdown = decimal_value(control["drawdown"], "drawdown_control.drawdown")
    except (DecisionToolError, KeyError):
        return False
    return drawdown < off


def cash_glide_ceiling(sizing: Mapping[str, object], gate: Mapping[str, object], control: object, level: object) -> Optional[Decimal]:
    """cash_glide 生效時今天現金目標的上限 ``anchor + max_daily_raise``；任一條件不成立回 None（照格點，不滑行）。

    條件（全部成立）：``cash_glide`` 開啟、今日姿態 defensive、anchor 是 prior_decision 且前次 run 有放寬記錄
    （anchor 的 ``prior_offense_loosened``，由 ``cash_ratio`` 讀前次 policy 綁定）、回撤條件成立。"""
    if not cash_glide_enabled(gate) or level != "defensive":
        return None
    range_gate = sizing.get("cash_ratio_range")
    anchor = range_gate.get("anchor") if isinstance(range_gate, Mapping) else None
    if not isinstance(anchor, Mapping) or anchor.get("status") != "prior_decision" or anchor.get("prior_offense_loosened") is not True:
        return None
    if not drawdown_allows_glide(gate, control):
        return None
    try:
        return decimal_value(anchor["ratio"], "anchor.ratio") + decimal_value(range_gate["max_daily_raise"], "max_daily_raise")  # type: ignore[index]
    except (DecisionToolError, KeyError):
        return None


def cash_raise_limited(
    sizing: Mapping[str, object], gate: Mapping[str, object], control: object, level: object, target: object,
) -> Optional[Dict[str, str]]:
    """``target`` 是 offense 放寬之後的現金目標；高於 ``cash_glide_ceiling`` 時回 ``{"from", "to"}``（只往下壓、不放寬），否則 None。"""
    ceiling = cash_glide_ceiling(sizing, gate, control, level)
    if ceiling is None:
        return None
    if decimal_value(target, "cash_buffer_rate") > ceiling:
        return {"from": str(target), "to": _ratio_text(ceiling)}
    return None


GLIDE_BRIEF_NOTE = (
    "前次決策放寬過防守姿態的現金目標。今天若選 defensive，現金目標最多回升到 max_cash_target_today（前次比例加每日回升上限），"
    "不會一天回到完整格點；之後每日再回升一格。回撤下限為 defensive、帳本回撤不可用或回撤達停用線時不滑行（照格點）。你不輸出數字。"
)


def cash_glide_brief(gate: Mapping[str, object], cash_ratio_range: object) -> Optional[Dict[str, object]]:
    """風險 Agent 簡報的背景區塊（``citable=false``）：``cash_glide`` 開啟且 anchor 是前次放寬過現金的 prior_decision 時，說明今天防守姿態的現金目標
    最多回升到多少；其他情形回 None（簡報不附，與沒有此欄位時逐鍵相同）。"""
    anchor = cash_ratio_range.get("anchor") if isinstance(cash_ratio_range, Mapping) else None
    if not cash_glide_enabled(gate) or not isinstance(anchor, Mapping) or anchor.get("status") != "prior_decision":
        return None
    if anchor.get("prior_offense_loosened") is not True:
        return None
    ceiling = decimal_value(anchor["ratio"], "anchor.ratio") + decimal_value(cash_ratio_range["max_daily_raise"], "max_daily_raise")  # type: ignore[index]
    return {"citable": False, "note": GLIDE_BRIEF_NOTE, "max_cash_target_today": _ratio_text(ceiling)}


def bind_offense(
    sizing: Mapping[str, object], cash_stance: Mapping[str, object], control: object, facts: Optional[Mapping[str, object]],
) -> Dict[str, object]:
    """``apply_trade_decision`` 用：回傳綁定本次有效等級的 offense 閘門（新副本，現金放寬記錄另由呼叫端算好再寫入）。樣板預填綁定欄位即拒絕。"""
    gate = offense_gate(sizing)
    assert gate is not None
    prefilled = sorted(bound_field_names(gate) & set(gate))
    if prefilled:
        raise DecisionToolError("position_sizing.offense.%s 只能由程式綁定，樣板不得預填" % "、".join(prefilled))
    effective, reason = effective_view(gate, cash_stance, control, facts)
    bound = {**dict(gate), "view": str(cash_stance["exposure_view"]), "effective_view": effective, "suppressed_reason": reason, "cash_loosening": None}
    if cash_glide_enabled(gate):
        bound[CASH_RAISE_LIMITED_FIELD] = None  # 現金回升限制記錄由呼叫端算好再寫入
    return bound


def validate_offense_binding(policy: Mapping[str, object], team: Mapping[str, object], errors: List[str]) -> None:
    """Finalizer：Policy 綁定的 view／effective_view／suppressed_reason／cash_loosening 必須等於以 team_inputs 的現金姿態、
    已重算驗證的 offense_facts 與 Policy 的回撤下限（另由 validate_drawdown_control 與帳本獨立驗證）重算的結果。"""
    prefix = "DecisionPolicy.position_sizing.offense"
    sizing = policy.get("position_sizing")
    gate = offense_gate(sizing)
    if gate is None:
        return
    stance = team.get("cash_stance")
    facts = team.get(OFFENSE_FACTS_KEY)
    if not isinstance(stance, Mapping):
        errors.append("%s 無法重算：team_inputs.cash_stance 缺少" % prefix)
        return
    try:
        effective, reason = effective_view(gate, stance, policy.get("drawdown_control"), facts if isinstance(facts, Mapping) else None)
        level = stance.get("level")
        target, loosening = offense_cash_target({**dict(gate), "effective_view": effective}, level, _base_cash(policy, gate, level))
    except (DecisionToolError, KeyError, TypeError) as error:
        errors.append("%s 無法重算：%s" % (prefix, error))
        return
    expected = {"view": stance.get("exposure_view"), "effective_view": effective, "suppressed_reason": reason, "cash_loosening": loosening}
    if cash_glide_enabled(gate):
        try:
            expected[CASH_RAISE_LIMITED_FIELD] = cash_raise_limited(sizing, gate, policy.get("drawdown_control"), level, target)  # type: ignore[arg-type]
        except (DecisionToolError, KeyError, TypeError) as error:
            errors.append("%s 無法重算 cash_raise_limited：%s" % (prefix, error))
            return
    for field, value in expected.items():
        if gate.get(field) != value:
            errors.append("%s.%s 與現金姿態、offense_facts 與回撤下限重算的結果不一致（%s）" % (prefix, field, value))


def _base_cash(policy: Mapping[str, object], gate: Mapping[str, object], level: object) -> object:
    """放寬前的現金比例（cash_ratio_range 格點值）：以 Policy 綁定的 cash_grade 對應 grid。"""
    sizing = policy["position_sizing"]
    range_gate = sizing["cash_ratio_range"]  # type: ignore[index]
    return range_gate["grid"][str(level)][str(sizing.get("cash_grade"))]  # type: ignore[index,union-attr]


def expected_cash_buffer(
    sizing: Mapping[str, object], stance: object, base: object, errors: List[str],
) -> object:
    """``validate_position_sizing`` 用：offense 的條件式現金放寬預期值（沒放寬回原值），並核對閘門綁定的 ``cash_loosening``。"""
    gate = offense_gate(sizing)
    if gate is None or not offense_bound(gate):
        return base
    try:
        expected, loosening = offense_cash_target(gate, stance, base)
    except DecisionToolError as error:
        errors.append(str(error))
        return base
    if gate.get("cash_loosening") != loosening:
        errors.append("offense.cash_loosening 必須與重算結果一致（%s）" % (loosening,))
    if cash_glide_enabled(gate):
        # 現金回升限制：完整條件由 validate_offense_binding（需要帳本回撤與 anchor）重算；這裡核對現金比例等於綁定的 to、from 等於放寬後的目標。
        limited = gate.get(CASH_RAISE_LIMITED_FIELD)
        if isinstance(limited, Mapping) and set(limited) == CASH_LOOSENING_FIELDS:
            try:
                if decimal_value(limited["from"], "cash_raise_limited.from") != decimal_value(expected, "cash_buffer_rate"):
                    errors.append("offense.cash_raise_limited.from 必須等於 offense 放寬之後的現金目標（%s）" % (expected,))
                expected = limited["to"]
            except DecisionToolError as error:
                errors.append(str(error))
    return expected


def capped_size(size: str, max_size: str) -> str:
    """交易 Agent 的份量格與該級 ``max_size`` 取較小的一格（只縮不放）。"""
    return SIZE_GRADES[min(SIZE_GRADES.index(size), SIZE_GRADES.index(max_size))]


__all__ = [
    "BOUND_FIELDS", "OFFENSE_CATEGORIES", "OFFENSE_FACTS_KEY", "OFFENSE_NOTE", "OFFENSE_V1", "OFFENSE_VIEWS", "SUPPRESSED_REASONS",
    "bind_offense", "build_offense_facts", "capped_size", "effective_view", "expected_cash_buffer", "non_news_market_ids",
    "normalize_offense_stance", "offense_bound", "offense_brief", "offense_cash_target", "CASH_GLIDE_FIELD", "CASH_RAISE_LIMITED_FIELD", "THESIS_SIZE_CAP_FIELD",
    "bound_field_names", "cash_glide_brief", "cash_glide_ceiling", "cash_glide_enabled", "cash_raise_limited", "drawdown_allows_glide", "thesis_size_cap_active",
    "thesis_size_cap_enabled", "offense_gate", "offense_levels",
    "offense_reference_tokens", "validate_offense_binding", "validate_offense_facts", "validate_offense_gate", "validate_offense_limits",
    "validate_offense_stance",
]
