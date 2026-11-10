"""反彈參與閘門（``position_sizing.rebound_participation`` v1 版本閘門，只由人設定、策略反思不得改）。

動機與限制見 ``docs/rebound_participation_plan.md``：防守姿態下 beta 很低的持股，在市場快速反彈時幾乎沒有參與；
本閘門在「程式狀態機」與「風險 Agent 的既有列舉」都同意轉向時，放寬曝險換股（防守姿態改用 beta 停止線、
換股檔數上調、持有天數下調）。整條鏈全部是確定性程式，Agent 不輸出任何數字、不新增欄位：

- **程式狀態 turn_state ∈ none／candidate／confirmed**：只在 regime 為 bear 時有意義，其餘恆 none。訊號全用
  ``turn_facts`` 既有欄位（池等權日報酬、站回 MA5 比例、20 日新低比例；regime 呼叫 ``MomentumEngine._regime``），
  沿 bundle 的 ``price_series`` 逐日推進（無記憶重算：不跨 run 存狀態，Finalizer 可整段重算）。
  缺資料永不升級：任何欄位算不出、覆蓋不足、regime 不可用一律回 none 並標 ``TURN_STATE_UNAVAILABLE``。
- **雙鑰匙** ``effective_state = min(程式 turn_state, 風險 Agent turn_assessment 對映)``：程式單獨不能升、Agent 單獨也不能升。
  ``drawdown_control.stance_floor`` 為 defensive 時恆 none（回撤下限優先）；減碼日（drawdown_derisk）由配置整個略過曝險換股。
- 對映寫在 Policy 的 ``by_state``，只會放寬（beta 停止線取較大、換股檔數取較大、持有天數取較小），不會收緊現行設定。

v2（``docs/rebound_participation_v2_plan.md``）：不再有狀態機與轉向門檻，程式只整理事實與資料品質（``data_status``），
``effective_state`` 只由風險 Agent 的 ``turn_assessment`` 對映，資料缺漏或回撤下限為 defensive 時壓回 none；v1 函式本體保持不動。

誰判斷方向：程式只算事實與狀態；有效狀態只是在既有曝險換股規則上調整三個參數，賣單仍標 ``exit``、不改寫交易 Agent 意圖、
不只賣不買，``turnover_fit`` 仍是最終上限。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Mapping, Optional, Tuple

from .fields import DecisionToolError, decimal_value, reject_unknown_fields
from .turn_facts import TURN_ASSESSMENTS, _Pool

REBOUND_PARTICIPATION = "v1"
# v2：程式不再用固定門檻推進狀態機，只算事實（最近幾日的池報酬、站回 MA5 比例、新低比例、regime 與資料品質）交給風險 Agent；
# 有效狀態只由 Agent 的 turn_assessment 對映決定，資料缺漏時恆 none。v1 完全保留（舊 Policy／舊 run 逐位元重建）。
REBOUND_PARTICIPATION_V2 = "v2"
REBOUND_VERSIONS = (REBOUND_PARTICIPATION, REBOUND_PARTICIPATION_V2)
TURN_STATE_VERSION = "1.0"
TURN_STATE_VERSION_V2 = "2.0"
TURN_STATES = ("none", "candidate", "confirmed")
# 風險 Agent 既有列舉 → 狀態上限（列舉本身不含數字）；名稱與 turn_facts.TURN_ASSESSMENTS 同。
AGENT_STATE_BY_ASSESSMENT = {
    "no_turn_evidence": "none",
    "turn_unconfirmed": "candidate",
    "turn_confirmed": "confirmed",
}
TURN_STATE_UNAVAILABLE = "TURN_STATE_UNAVAILABLE"
GATE_REQUIRED_FIELDS = {
    "version", "candidate_return", "candidate_ma5_share", "confirm_return", "confirm_ma5_share",
    "confirm_new_low_share_max", "candidate_expiry_days", "retreat_return", "retreat_window_days",
    "retreat_ma5_share", "min_coverage", "by_state",
}
# effective_state 由 apply_trade_decision 綁定（不得預填），重建時 Finalizer 以 team_inputs.turn_state 與風險 Agent 的列舉重算。
# cash_loosening 也由程式綁定（防守姿態的現金比例被放寬時記下前後值，供稽核與 Finalizer 重算）。
GATE_FIELDS = GATE_REQUIRED_FIELDS | {"effective_state", "cash_loosening"}
# v2 只留資料品質數字 min_coverage（不是判斷）與 by_state 對映；沒有任何轉向門檻。
GATE_REQUIRED_FIELDS_V2 = {"version", "min_coverage", "by_state"}
GATE_FIELDS_V2 = GATE_REQUIRED_FIELDS_V2 | {"effective_state", "cash_loosening"}
STATE_REQUIRED_FIELDS = {"defensive_target", "max_swaps_per_run", "min_holding_days"}
# 選填：defensive_cash_buffer（兩態皆可）＝防守姿態的現金比例放寬目標；max_turnover_rate 只有 confirmed 可填＝總換手上限。
# 兩者都放寬風控，只由人設定；省略即不放寬。
STATE_FIELDS = {
    "candidate": STATE_REQUIRED_FIELDS | {"defensive_cash_buffer"},
    "confirmed": STATE_REQUIRED_FIELDS | {"neutral_target", "defensive_cash_buffer", "max_turnover_rate"},
}
CASH_LOOSENING_FIELDS = {"from", "to"}
TURN_STATE_FIELDS = {
    "schema_version", "version", "as_of", "state", "regime", "candidate_since", "days_since_candidate",
    "cumulative_return_since_candidate", "signals", "flags", "recent_path", "note",
}
SIGNAL_FIELDS = {"pool_return_latest", "share_above_ma5", "new_low_20d_share", "return_window", "coverage"}
TURN_STATE_FIELDS_V2 = {
    "schema_version", "version", "as_of", "data_status", "regime", "signals", "flags", "recent_path", "note",
}
SIGNAL_FIELDS_V2 = {"pool_return_latest", "share_above_ma5", "new_low_20d_share", "coverage", "return_coverage"}
DATA_STATUSES = ("available", "unavailable")
RECENT_PATH_DAYS = 5
QUANTUM = Decimal("0.000001")
TURN_STATE_NOTE = (
    "turn_state 是程式依固定規則逐日推進的狀態機（none／candidate／confirmed，regime 不是 bear 時恆為 none），"
    "只是背景：沒有證據編號、不可引用。有效狀態取程式狀態與你的 turn_assessment 對映中較保守者，"
    "所以兩邊都同意轉向時才會放寬防守姿態下的曝險換股；它不改現金姿態、持股廣度或任何硬性規則。"
    "反彈可能失敗，缺資料時程式一律回 none。"
)
TURN_STATE_NOTE_V2 = (
    "turn_state（v2）只是程式整理的事實與資料品質，沒有狀態、沒有門檻、沒有證據編號、不可引用。"
    "是否轉向由你的 turn_assessment 單獨決定：turn_unconfirmed 對映候選、turn_confirmed 對映確認，"
    "程式只在資料缺漏（data_status 為 unavailable）或回撤下限為 defensive 時把有效狀態壓回 none；regime 不是 bear 不擋。"
    "你的 turn_confirmed 會放寬防守姿態下的曝險換股、現金比例與換手上限（依 Policy），反彈可能失敗，缺資料時不要升級。"
)


def rebound_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """rebound_participation 啟用時回傳閘門物件；沒有閘門回 None（欄位白名單與舊版完全相同）。"""
    gate = sizing.get("rebound_participation") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def _q(value: Decimal) -> str:
    return str(value.quantize(QUANTUM, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- 閘門形狀

def _number(gate: Mapping[str, object], name: str, errors: List[str], low: Decimal, high: Decimal, prefix: str) -> Optional[Decimal]:
    if name not in gate:
        return None
    label = "%s.%s" % (prefix, name)
    value = gate[name]
    if isinstance(value, bool) or not isinstance(value, str):
        errors.append("%s 必須是數字字串" % label)
        return None
    try:
        number = decimal_value(value, label)
    except DecisionToolError as error:
        errors.append(str(error))
        return None
    if not low <= number <= high:
        errors.append("%s 必須介於 %s 與 %s（含）" % (label, low, high))
        return None
    return number


def _integer(value: object, label: str, errors: List[str], low: int, high: int) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        errors.append("%s 必須是 %d～%d 的整數" % (label, low, high))
        return None
    return value


def validate_rebound_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """閘門形狀、值域與前提（position_sizing 內的前提；bundle 層的 typed_evidence_v1 在建 turn_state 時檢查）。"""
    gate = sizing["rebound_participation"]
    prefix = "position_sizing.rebound_participation"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    v2 = gate.get("version") == REBOUND_PARTICIPATION_V2
    # v1／v2 欄位集不同：v2 帶 v1 的轉向門檻、或 v1 缺門檻都拒絕（不混用）。
    reject_unknown_fields(gate, GATE_FIELDS_V2 if v2 else GATE_FIELDS, prefix, errors)
    missing = sorted((GATE_REQUIRED_FIELDS_V2 if v2 else GATE_REQUIRED_FIELDS) - set(gate))
    if missing:
        errors.append("%s 缺少必要欄位（由人設定，程式不提供預設值）：%s" % (prefix, "、".join(missing)))
    if gate.get("version") not in REBOUND_VERSIONS:
        errors.append("%s.version 只允許 %s" % (prefix, "／".join(REBOUND_VERSIONS)))
    if not v2:
        _number(gate, "candidate_return", errors, Decimal("0.005"), Decimal("0.2"), prefix)
        _number(gate, "candidate_ma5_share", errors, Decimal(0), Decimal(1), prefix)
        _number(gate, "confirm_return", errors, Decimal("-0.1"), Decimal("0.2"), prefix)
        _number(gate, "confirm_ma5_share", errors, Decimal(0), Decimal(1), prefix)
        _number(gate, "confirm_new_low_share_max", errors, Decimal(0), Decimal(1), prefix)
        _number(gate, "retreat_return", errors, Decimal("-0.3"), Decimal("-0.005"), prefix)
        _number(gate, "retreat_ma5_share", errors, Decimal(0), Decimal(1), prefix)
        if "candidate_expiry_days" in gate:
            _integer(gate["candidate_expiry_days"], "%s.candidate_expiry_days" % prefix, errors, 2, 10)
        if "retreat_window_days" in gate:
            _integer(gate["retreat_window_days"], "%s.retreat_window_days" % prefix, errors, 2, 10)
    _number(gate, "min_coverage", errors, Decimal("0.5"), Decimal(1), prefix)
    _validate_by_state(gate, sizing, prefix, errors)
    if "effective_state" in gate and gate["effective_state"] not in TURN_STATES:
        errors.append("%s.effective_state 必須是 %s（只能由程式綁定）" % (prefix, "／".join(TURN_STATES)))
    if "cash_loosening" in gate:
        loosening = gate["cash_loosening"]
        if not isinstance(loosening, Mapping) or set(loosening) != CASH_LOOSENING_FIELDS or not all(
            isinstance(v, str) for v in loosening.values()
        ):
            errors.append("%s.cash_loosening 必須剛好包含 from／to 字串（只能由程式綁定）" % prefix)
    # 前提的「存在」由 sizing.GATE_PREREQUISITES 檢查；這裡只檢查版本（人設定的值，不放寬）。
    exposure = sizing.get("exposure_rotation")
    if isinstance(exposure, Mapping) and exposure.get("version") != "v3":
        errors.append("%s 只允許用於 exposure_rotation.version=v3（防守姿態可填 beta 停止線）" % prefix)
    turn_gate = sizing.get("cash_stance_turn_facts")
    if isinstance(turn_gate, Mapping) and turn_gate.get("version") != "v1":
        errors.append("%s 需要 cash_stance_turn_facts.version=v1（雙鑰匙的 Agent 列舉）" % prefix)
    derisk = sizing.get("drawdown_derisk")
    if isinstance(derisk, Mapping) and derisk.get("version") not in ("v4", "v5"):  # v5 只改減碼的先後，數字規則同 v4
        errors.append("%s 必須與 drawdown_derisk.version=v4（或 v5）同時使用" % prefix)


def _neutral_cash_floor(sizing: Mapping[str, object]) -> Optional[Decimal]:
    """中性姿態最低現金比例：cash_ratio_range 啟用取 grid.neutral.lower，否則取 cash_buffer_by_stance.neutral；讀不到回 None。"""
    try:
        range_gate = sizing.get("cash_ratio_range")
        if isinstance(range_gate, Mapping):
            return decimal_value(range_gate["grid"]["neutral"]["lower"], "grid.neutral.lower")
        return decimal_value(sizing["cash_buffer_by_stance"]["neutral"], "cash_buffer_by_stance.neutral")
    except (KeyError, TypeError, DecisionToolError):
        return None


def _validate_by_state(gate: Mapping[str, object], sizing: Mapping[str, object], prefix: str, errors: List[str]) -> None:
    if "by_state" not in gate:
        return
    label = "%s.by_state" % prefix
    table = gate["by_state"]
    if not isinstance(table, Mapping) or set(table) != set(STATE_FIELDS):
        errors.append("%s 必須剛好包含 candidate／confirmed" % label)
        return
    rotation = sizing.get("rotation")
    cap = rotation.get("max_swaps_per_run") if isinstance(rotation, Mapping) else None
    parsed: Dict[str, Dict[str, object]] = {}
    for state, fields in STATE_FIELDS.items():
        row = table[state]
        row_label = "%s.%s" % (label, state)
        if not isinstance(row, Mapping):
            errors.append("%s 必須是物件" % row_label)
            return
        reject_unknown_fields(row, fields, row_label, errors)
        missing = sorted(STATE_REQUIRED_FIELDS - set(row))
        if missing:
            errors.append("%s 缺少必要欄位：%s" % (row_label, "、".join(missing)))
            return
        entry: Dict[str, object] = {}
        for name in ("defensive_target", "neutral_target"):
            if name in row:
                entry[name] = _number(row, name, errors, Decimal("0.01"), Decimal(2), row_label)
        for name, high in (("defensive_cash_buffer", Decimal("0.25")), ("max_turnover_rate", Decimal("0.6"))):
            if name in row:
                entry[name] = _number(row, name, errors, Decimal("0.03") if name == "defensive_cash_buffer" else Decimal("0.05"), high, row_label)
        entry["swaps"] = _integer(row["max_swaps_per_run"], "%s.max_swaps_per_run" % row_label, errors, 1, 10)
        entry["hold"] = _integer(row["min_holding_days"], "%s.min_holding_days" % row_label, errors, 0, 60)
        if isinstance(entry["swaps"], int) and isinstance(cap, int) and entry["swaps"] > cap:
            errors.append(
                "%s.max_swaps_per_run 不得高於 rotation.max_swaps_per_run（%d）；超過的換股對會在名額計算被靜默丟掉" % (row_label, cap)
            )
        parsed[state] = entry
    exposure = sizing.get("exposure_rotation")
    targets = exposure.get("beta_target_by_stance") if isinstance(exposure, Mapping) else None
    aggressive = None
    if isinstance(targets, Mapping) and targets.get("aggressive") is not None:
        try:
            aggressive = decimal_value(targets["aggressive"], "exposure_rotation.beta_target_by_stance.aggressive")
        except DecisionToolError:
            aggressive = None
    for state, entry in parsed.items():
        defensive, neutral = entry.get("defensive_target"), entry.get("neutral_target")
        if isinstance(defensive, Decimal) and isinstance(neutral, Decimal) and neutral < defensive:
            errors.append("%s.%s.neutral_target 不得低於 defensive_target" % (label, state))
        for value in (defensive, neutral):
            if isinstance(value, Decimal) and aggressive is not None and value > aggressive:
                errors.append("%s.%s 的 beta 停止線不得高於 exposure_rotation 的進攻目標（%s）" % (label, state, aggressive))
    cand, conf = parsed.get("candidate", {}), parsed.get("confirmed", {})
    # 現金放寬只放寬：不得低於中性姿態最低格（防守姿態放寬後比中性還鬆沒有道理），確認後不得比候選更保守（現金比例不升）。
    floor_ratio = _neutral_cash_floor(sizing)
    for state, entry in parsed.items():
        value = entry.get("defensive_cash_buffer")
        if isinstance(value, Decimal) and floor_ratio is not None and value < floor_ratio:
            errors.append("%s.%s.defensive_cash_buffer 不得低於中性姿態最低現金比例（%s）" % (label, state, floor_ratio))
    if isinstance(cand.get("defensive_cash_buffer"), Decimal) and isinstance(conf.get("defensive_cash_buffer"), Decimal) \
            and conf["defensive_cash_buffer"] > cand["defensive_cash_buffer"]:
        errors.append("%s 必須 confirmed.defensive_cash_buffer ≤ candidate.defensive_cash_buffer" % label)
    # 確認後不得比候選更保守：停止線不降、檔數不減、持有天數不增。
    if isinstance(cand.get("defensive_target"), Decimal) and isinstance(conf.get("defensive_target"), Decimal) \
            and conf["defensive_target"] < cand["defensive_target"]:
        errors.append("%s 必須 confirmed.defensive_target ≥ candidate.defensive_target" % label)
    if isinstance(cand.get("swaps"), int) and isinstance(conf.get("swaps"), int) and conf["swaps"] < cand["swaps"]:
        errors.append("%s 必須 confirmed.max_swaps_per_run ≥ candidate.max_swaps_per_run" % label)
    if isinstance(cand.get("hold"), int) and isinstance(conf.get("hold"), int) and conf["hold"] > cand["hold"]:
        errors.append("%s 必須 confirmed.min_holding_days ≤ candidate.min_holding_days" % label)


# ---------------------------------------------------------------- 逐日狀態機

def _gate_decimal(gate: Mapping[str, object], name: str) -> Decimal:
    return decimal_value(gate[name], "rebound_participation.%s" % name)


def _day_record(
    pool: _Pool, index: int, gate: Mapping[str, object], date_sets: Mapping[str, set],
) -> Dict[str, object]:
    """某個交易日的原始訊號；任一項算不出就在 ``unavailable`` 寫原因（缺資料永不升級）。"""
    day = pool.days[index]
    ret = pool.pool_return(index)
    with_bar = sum(1 for dates in date_sets.values() if day in dates)
    coverage = Decimal(with_bar) / Decimal(pool.size) if pool.size else Decimal(0)
    record: Dict[str, object] = {"date": day, "ret": ret, "coverage": coverage, "unavailable": None,
                                 "regime": None, "ma5": None, "new_low": None}
    reasons: List[str] = []
    if ret is None:
        reasons.append("pool_return")
    if coverage < _gate_decimal(gate, "min_coverage"):
        reasons.append("coverage")
    regime = pool.regime(day)
    if regime["status"] != "available":
        reasons.append("regime")
    else:
        record["regime"] = regime["regime"]
    ma5 = pool.share(day, 5, lambda c: c[-1] >= sum(c[-5:]) / Decimal(5))
    new_low = pool.share(day, 20, lambda c: c[-1] <= min(c[-20:]))
    if ma5 is None:
        reasons.append("share_above_ma5")
    else:
        record["ma5"] = Decimal(ma5)
    if new_low is None:
        reasons.append("new_low_20d_share")
    else:
        record["new_low"] = Decimal(new_low)
    record["unavailable"] = "、".join(reasons) if reasons else None
    return record


def state_path(pool: _Pool, gate: Mapping[str, object]) -> List[Dict[str, object]]:
    """沿交易日逐日推進狀態機（無記憶：只依 ``pool`` 的序列與 ``gate`` 的門檻）。回傳每日一筆紀錄，含當日狀態。

    定義（全部由 Policy 欄位決定數字）：
    - none → candidate：regime 為 bear，且當日池報酬 ≥ ``candidate_return`` 與站回 MA5 比例 ≥ ``candidate_ma5_share``；該日為第 1 日。
    - candidate → confirmed：第 2 日起（至第 ``candidate_expiry_days`` 日），自轉向日「之後」各日池報酬複利 ≥ ``confirm_return``
      （不含轉向日當天，否則條件形同虛設）、站回 MA5 比例 ≥ ``confirm_ma5_share``、20 日新低比例 ≤ ``confirm_new_low_share_max``；
      candidate 的第 ``candidate_expiry_days`` + 1 日仍未確認即回 none（期間再出現大漲不重設計數）。
    - 退回 none：近 ``retreat_window_days`` 日池報酬複利 ≤ ``retreat_return`` 且站回 MA5 比例 < ``retreat_ma5_share``（只對 candidate／
      confirmed 生效；單日下跌不退）；或 regime 不是 bear。
    - 當日任一訊號算不出、覆蓋 < ``min_coverage`` 或 regime 不可用：回 none（旗標 ``TURN_STATE_UNAVAILABLE``）。
    """
    candidate_return = _gate_decimal(gate, "candidate_return")
    candidate_ma5 = _gate_decimal(gate, "candidate_ma5_share")
    confirm_return = _gate_decimal(gate, "confirm_return")
    confirm_ma5 = _gate_decimal(gate, "confirm_ma5_share")
    confirm_low = _gate_decimal(gate, "confirm_new_low_share_max")
    retreat_return = _gate_decimal(gate, "retreat_return")
    retreat_ma5 = _gate_decimal(gate, "retreat_ma5_share")
    expiry = int(gate["candidate_expiry_days"])
    window = int(gate["retreat_window_days"])
    state, turn_index = "none", None
    records: List[Dict[str, object]] = []
    date_sets = {symbol: set(dates) for symbol, dates in pool.dates_by_symbol.items()}
    for index in range(len(pool.days)):
        record = _day_record(pool, index, gate, date_sets)
        records.append(record)
        record.update(candidate_since=None, days_since_candidate=None, cumulative=None, window_return=None)
        ret, ma5, low = record["ret"], record["ma5"], record["new_low"]
        if record["unavailable"] is not None or record["regime"] != "bear":
            state, turn_index = "none", None
            record["state"] = state
            continue
        recent = [records[j]["ret"] for j in range(index - window + 1, index + 1) if j >= 0]
        if len(recent) == window and all(isinstance(value, Decimal) for value in recent):
            growth = Decimal(1)
            for value in recent:
                growth *= Decimal(1) + value  # type: ignore[operator]
            record["window_return"] = growth - Decimal(1)
        if state != "none" and isinstance(record["window_return"], Decimal) \
                and record["window_return"] <= retreat_return and ma5 < retreat_ma5:  # type: ignore[operator]
            state, turn_index = "none", None
        if state == "candidate":
            days_since = index - turn_index  # type: ignore[operator]
            growth = Decimal(1)
            for j in range(turn_index + 1, index + 1):  # type: ignore[operator]
                growth *= Decimal(1) + records[j]["ret"]  # type: ignore[operator]
            cumulative = growth - Decimal(1)
            if days_since + 1 <= expiry and cumulative >= confirm_return and ma5 >= confirm_ma5 and low <= confirm_low:  # type: ignore[operator]
                state = "confirmed"
            elif days_since + 1 > expiry:
                state, turn_index = "none", None
        if state == "none" and ret >= candidate_return and ma5 >= candidate_ma5:  # type: ignore[operator]
            state, turn_index = "candidate", index
        record["state"] = state
        if state != "none" and turn_index is not None:
            record["candidate_since"] = pool.days[turn_index]
            record["days_since_candidate"] = index - turn_index
            growth = Decimal(1)
            for j in range(turn_index + 1, index + 1):
                growth *= Decimal(1) + records[j]["ret"]  # type: ignore[operator]
            record["cumulative"] = growth - Decimal(1)
    return records


def _unavailable_state(trade_date: object, gate: Mapping[str, object], reason: str) -> Dict[str, object]:
    return {
        "schema_version": TURN_STATE_VERSION, "version": gate["version"], "as_of": trade_date, "state": "none",
        "regime": None, "candidate_since": None, "days_since_candidate": None, "cumulative_return_since_candidate": None,
        "signals": {name: None for name in sorted(SIGNAL_FIELDS)}, "flags": [TURN_STATE_UNAVAILABLE, reason],
        "recent_path": [], "note": TURN_STATE_NOTE,
    }


def _unavailable_facts_v2(trade_date: object, gate: Mapping[str, object], reason: str) -> Dict[str, object]:
    return {
        "schema_version": TURN_STATE_VERSION_V2, "version": gate["version"], "as_of": trade_date, "data_status": "unavailable",
        "regime": None, "signals": {name: None for name in sorted(SIGNAL_FIELDS_V2)}, "flags": [TURN_STATE_UNAVAILABLE, reason],
        "recent_path": [], "note": TURN_STATE_NOTE_V2,
    }


def _return_coverage(pool: _Pool, index: int) -> Decimal:
    """池日報酬實際用到的股票占交易池的比例（當日與前一個交易日都有收盤者）。``coverage`` 只看當日有 bar，
    前一日缺很多檔時報酬只由少數檔算出，必須另外看這個比例（v2 專用，不改 turn_facts 的 pool_return）。"""
    if index < 1 or not pool.size:
        return Decimal(0)
    today, before = pool.days[index], pool.days[index - 1]
    used = sum(1 for dates in pool.dates_by_symbol.values() if today in dates and before in dates)
    return Decimal(used) / Decimal(pool.size)


def _turn_facts_v2(pool: _Pool, gate: Mapping[str, object], trade_date: str) -> Dict[str, object]:
    """v2：只整理最近 ``RECENT_PATH_DAYS`` 日的事實與資料品質（沒有門檻、沒有狀態）；缺資料標 unavailable，永不補假值。"""
    date_sets = {symbol: set(dates) for symbol, dates in pool.dates_by_symbol.items()}
    start = max(0, len(pool.days) - RECENT_PATH_DAYS)
    records = [_day_record(pool, index, gate, date_sets) for index in range(start, len(pool.days))]
    return_coverage = [_return_coverage(pool, index) for index in range(start, len(pool.days))]
    last = records[-1]
    reasons = [last["unavailable"]] if last["unavailable"] is not None else []
    if return_coverage[-1] < _gate_decimal(gate, "min_coverage"):
        reasons.append("pool_return_sample")  # 前一交易日缺很多檔：報酬只由少數檔算出，不可當事實
    unavailable = bool(reasons)
    flags: List[str] = [TURN_STATE_UNAVAILABLE, "無法計算：%s" % "、".join(reasons)] if unavailable else []

    def text(value: object) -> Optional[str]:
        return _q(value) if isinstance(value, Decimal) else None  # type: ignore[arg-type]

    return {
        "schema_version": TURN_STATE_VERSION_V2,
        "version": gate["version"],
        "as_of": trade_date,
        "data_status": "unavailable" if unavailable else "available",
        "regime": last["regime"],
        "signals": {
            "pool_return_latest": text(last["ret"]),
            "share_above_ma5": text(last["ma5"]),
            "new_low_20d_share": text(last["new_low"]),
            "coverage": _q(last["coverage"]),  # type: ignore[arg-type]
            "return_coverage": _q(return_coverage[-1]),
        },
        "flags": flags,
        "recent_path": [
            {"date": row["date"], "pool_return": text(row["ret"]), "share_above_ma5": text(row["ma5"]),
             "new_low_20d_share": text(row["new_low"]), "regime": row["regime"], "coverage": _q(row["coverage"]),  # type: ignore[arg-type]
             "return_coverage": _q(sample)}
            for row, sample in zip(records, return_coverage)
        ],
        "note": TURN_STATE_NOTE_V2,
    }


def build_turn_state(bundle: Mapping[str, object], gate: Mapping[str, object]) -> Dict[str, object]:
    """依 bundle 的 ``price_series`` 重算程式狀態（整份輸出會存進 team_inputs 2.8，Finalizer 整份重算比對）。

    前提（缺一 fail-closed 拒絕）：bundle 為 typed_evidence_v1。資料問題（bar 晚於最近交易日、行情缺漏、覆蓋不足）
    不拋例外而是回 none 並標 ``TURN_STATE_UNAVAILABLE``。"""
    if bundle.get("evidence_policy") != "typed_evidence_v1":
        raise DecisionToolError("position_sizing.rebound_participation 只能用於 evidence_policy=typed_evidence_v1 的決策輸入")
    snapshot = bundle.get("snapshot")
    trade_date = snapshot.get("latest_trade_date") if isinstance(snapshot, Mapping) else None
    if not isinstance(trade_date, str) or not isinstance(bundle.get("price_series"), list):
        raise DecisionToolError("turn_state 需要 snapshot.latest_trade_date 與 price_series")
    v2 = gate.get("version") == REBOUND_PARTICIPATION_V2
    unavailable = _unavailable_facts_v2 if v2 else _unavailable_state
    try:
        pool = _Pool(bundle, trade_date)
    except DecisionToolError as error:
        return unavailable(trade_date, gate, str(error))
    if not pool.days:
        return unavailable(trade_date, gate, "price_series 沒有任何行情")
    if pool.days[-1] != trade_date:
        return unavailable(trade_date, gate, "行情最後一個交易日 %s 不是 snapshot.latest_trade_date %s" % (pool.days[-1], trade_date))
    if v2:
        return _turn_facts_v2(pool, gate, trade_date)
    records = state_path(pool, gate)
    last = records[-1]
    flags: List[str] = []
    if last["unavailable"] is not None:
        flags = [TURN_STATE_UNAVAILABLE, "無法計算：%s" % last["unavailable"]]
    cumulative = last["cumulative"]
    return {
        "schema_version": TURN_STATE_VERSION,
        "version": gate["version"],
        "as_of": trade_date,
        "state": last["state"],
        "regime": last["regime"],
        "candidate_since": last["candidate_since"],
        "days_since_candidate": last["days_since_candidate"],
        "cumulative_return_since_candidate": _q(cumulative) if isinstance(cumulative, Decimal) else None,
        "signals": {
            "pool_return_latest": _q(last["ret"]) if isinstance(last["ret"], Decimal) else None,  # type: ignore[arg-type]
            "share_above_ma5": _q(last["ma5"]) if isinstance(last["ma5"], Decimal) else None,  # type: ignore[arg-type]
            "new_low_20d_share": _q(last["new_low"]) if isinstance(last["new_low"], Decimal) else None,  # type: ignore[arg-type]
            "return_window": _q(last["window_return"]) if isinstance(last["window_return"], Decimal) else None,  # type: ignore[arg-type]
            "coverage": _q(last["coverage"]),  # type: ignore[arg-type]
        },
        "flags": flags,
        "recent_path": [{"date": row["date"], "state": row["state"]} for row in records[-RECENT_PATH_DAYS:]],
        "note": TURN_STATE_NOTE,
    }


def validate_turn_state(
    bundle: Mapping[str, object], policy: Mapping[str, object], turn_state: object, errors: List[str],
) -> None:
    """以同一輸入整份重算 ``team_inputs.turn_state`` 並比對；缺少、欄位多餘或不一致即拒絕（fail-closed）。"""
    prefix = "team_inputs.turn_state"
    if not isinstance(turn_state, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    gate = rebound_gate(policy.get("position_sizing"))
    if gate is None:
        errors.append("%s 只能用於啟用 position_sizing.rebound_participation 的 Policy" % prefix)
        return
    reject_unknown_fields(
        turn_state, TURN_STATE_FIELDS_V2 if gate.get("version") == REBOUND_PARTICIPATION_V2 else TURN_STATE_FIELDS, prefix, errors,
    )
    try:
        expected = build_turn_state(bundle, gate)
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        errors.append("%s 無法以共同輸入重算：%s" % (prefix, error))
        return
    if dict(turn_state) != expected:
        errors.append("%s 與重算結果不同，拒絕改寫" % prefix)


# ---------------------------------------------------------------- 雙鑰匙與綁定

def effective_state(
    turn_state: Mapping[str, object], stance: Mapping[str, object], stance_floor: object,
) -> str:
    """``min(程式 turn_state, 風險 Agent turn_assessment 對映)``；回撤下限為 defensive 時恆 none（回撤下限優先）。

    stance 缺 turn_assessment 或值不在列舉內即拒絕（雙鑰匙缺一把就不給放寬，也不猜）。"""
    assessment = stance.get("turn_assessment")
    if turn_state.get("version") == REBOUND_PARTICIPATION_V2:
        # v2：Agent 的列舉是唯一的鑰匙；程式只在資料缺漏或回撤下限為 defensive 時壓回 none（regime 不是 bear 不擋）。
        if turn_state.get("data_status") not in DATA_STATUSES:
            raise DecisionToolError("turn_state.data_status 必須是 %s" % "／".join(DATA_STATUSES))
        if assessment not in TURN_ASSESSMENTS:
            raise DecisionToolError("現金姿態必須有 turn_assessment（%s），反彈參與閘門才能算有效狀態" % "／".join(TURN_ASSESSMENTS))
        if turn_state["data_status"] != "available" or stance_floor == "defensive":
            return "none"
        return AGENT_STATE_BY_ASSESSMENT[str(assessment)]
    state = turn_state.get("state")
    if state not in TURN_STATES:
        raise DecisionToolError("turn_state.state 必須是 %s" % "／".join(TURN_STATES))
    if assessment not in TURN_ASSESSMENTS:
        raise DecisionToolError("現金姿態必須有 turn_assessment（%s），反彈參與閘門才能算有效狀態" % "／".join(TURN_ASSESSMENTS))
    if stance_floor == "defensive":
        return "none"
    return min(str(state), AGENT_STATE_BY_ASSESSMENT[str(assessment)], key=TURN_STATES.index)


def validate_effective_state(policy: Mapping[str, object], team: Mapping[str, object], errors: List[str]) -> None:
    """Finalizer：Policy 綁定的 ``effective_state`` 必須等於 min(team_inputs.turn_state, 風險 Agent turn_assessment 對映)。
    ``turn_state`` 本身另由 ``validate_turn_state`` 整份重算，所以這裡讀它的 state 欄位是可信的。"""
    prefix = "DecisionPolicy.position_sizing.rebound_participation.effective_state"
    sizing = policy.get("position_sizing")
    gate = rebound_gate(sizing)
    if gate is None:
        return
    stance = team.get("cash_stance")
    control = policy.get("drawdown_control")
    floor = control.get("stance_floor") if isinstance(control, Mapping) else None
    turn_state = team.get("turn_state")
    try:
        expected = effective_state(turn_state if isinstance(turn_state, Mapping) else {}, stance if isinstance(stance, Mapping) else {}, floor)
    except DecisionToolError as error:
        errors.append("rebound_participation：%s" % error)
        return
    if gate.get("effective_state") != expected:
        errors.append("%s 與 turn_state／風險 Agent 列舉重算結果不一致" % prefix)


def bind_effective_state(
    policy_sizing: Mapping[str, object], bundle: Mapping[str, object], stance: Mapping[str, object], stance_floor: object,
) -> Dict[str, object]:
    """回傳綁定 ``effective_state`` 的 rebound_participation 閘門物件（新副本）。樣板預填 effective_state 即拒絕。"""
    gate = rebound_gate(policy_sizing)
    assert gate is not None
    if "effective_state" in gate:
        raise DecisionToolError("position_sizing.rebound_participation.effective_state 只能由程式綁定，樣板不得預填")
    state = build_turn_state(bundle, gate)
    return {**dict(gate), "effective_state": effective_state(state, stance, stance_floor)}


def apply_rebound(
    exposure_gate: Mapping[str, object], sizing: Mapping[str, object],
) -> Tuple[Mapping[str, object], str]:
    """有效狀態對映到曝險換股閘門：回傳（有效閘門, 有效狀態）。只放寬：停止線取較大、檔數取較大、持有天數取較小。

    沒有閘門回（原閘門, "none"）；有閘門卻沒綁有效狀態即拒絕（不補預設）。"""
    gate = rebound_gate(sizing)
    if gate is None:
        return exposure_gate, "none"
    state = gate.get("effective_state")
    if state not in TURN_STATES:
        raise DecisionToolError("position_sizing.rebound_participation 尚未綁定 effective_state")
    if state == "none":
        return exposure_gate, "none"
    row = gate["by_state"][state]  # type: ignore[index]
    effective = dict(exposure_gate)
    effective["max_swaps_per_run"] = max(int(exposure_gate["max_swaps_per_run"]), int(row["max_swaps_per_run"]))
    effective["min_holding_days"] = min(int(exposure_gate["min_holding_days"]), int(row["min_holding_days"]))
    targets = dict(exposure_gate["beta_target_by_stance"])  # type: ignore[arg-type]
    for stance, name in (("defensive", "defensive_target"), ("neutral", "neutral_target")):
        if name not in row:
            continue
        proposed = decimal_value(row[name], name)
        current = targets.get(stance)
        if current is None or proposed > decimal_value(current, "beta_target_by_stance.%s" % stance):
            targets[stance] = row[name]
    effective["beta_target_by_stance"] = targets
    return effective, str(state)


# ---------------------------------------------------------------- 現金放寬與換手上限（只放寬、人設定）

def loosened_cash_buffer(
    gate: Optional[Mapping[str, object]], level: object, base: object,
) -> Tuple[object, Optional[Dict[str, str]]]:
    """有效狀態為 candidate／confirmed 且現金姿態為 defensive 時，現金比例改用該態的 ``defensive_cash_buffer``
    （只放寬：目標不低於原比例才維持原值）。回傳（現金比例原字串, ``cash_loosening``：{from, to}；沒放寬回 None）。

    這是「雙鑰匙都同意轉向」才成立的程式覆寫：覆寫風險 Agent 給的 defensive 現金子等級；
    ``drawdown_control.stance_floor`` 為 defensive 時有效狀態恆 none，所以回撤下限優先；
    ``cash_ratio_range.max_daily_raise`` 只限制往上拉高、不受影響（放寬是往下）。"""
    if gate is None or level != "defensive":
        return base, None
    state = gate.get("effective_state")
    table = gate.get("by_state")
    if state not in ("candidate", "confirmed") or not isinstance(table, Mapping) or not isinstance(table.get(state), Mapping):
        return base, None
    target = table[state].get("defensive_cash_buffer")
    if target is None:
        return base, None
    if decimal_value(target, "defensive_cash_buffer") < decimal_value(base, "cash_buffer_rate"):
        return target, {"from": str(base), "to": str(target)}
    return base, None


def lifted_turnover_rate(policy: Mapping[str, object]) -> Optional[Decimal]:
    """有效狀態為 confirmed 且 by_state.confirmed 有 ``max_turnover_rate`` 時的總換手上限（取與 Policy 上限較大者，只放寬）；
    其餘回 None，呼叫端沿用 Policy 的 ``max_turnover_rate``。"""
    gate = rebound_gate(policy.get("position_sizing"))
    if gate is None or gate.get("effective_state") != "confirmed":
        return None
    table = gate.get("by_state")
    row = table.get("confirmed") if isinstance(table, Mapping) else None
    raw = row.get("max_turnover_rate") if isinstance(row, Mapping) else None
    if raw is None:
        return None
    base = decimal_value(policy["max_turnover_rate"], "max_turnover_rate")
    cap = decimal_value(raw, "rebound_participation.by_state.confirmed.max_turnover_rate")
    return cap if cap > base else None
