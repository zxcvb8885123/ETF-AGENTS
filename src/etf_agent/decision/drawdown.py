"""回撤控制：從帳本 NAV 歷史確定性計算峰值回撤，對應風險 Agent 現金姿態的下限。

策略文件的目標是「控制回撤與集中風險」。回撤由程式計算（峰值 NAV 對目前 NAV 的跌幅），
門檻放在 Policy 的 ``drawdown_control``；風險 Agent 只看到結果與下限，不能自行計算或放寬。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.core import canonical_sha256, parse_aware_time, parse_decimal

from .contracts import DecisionToolError, reject_unknown_fields


CASH_STANCE_RANK = {"aggressive": 0, "neutral": 1, "defensive": 2}
THRESHOLD_FIELDS = ("neutral_at", "defensive_at")
# 2026-11-03 後新增（與 position_sizing.drawdown_derisk 閘門成對）：解除門檻（hysteresis）。
# 進入 neutral 後回撤要回到 release_neutral_below 以下才解除、defensive 要回到 release_defensive_below 以下才降回 neutral，
# 避免回撤在門檻附近來回時姿態日日切換。姿態下限改為沿帳本 NAV 歷史逐筆推進的狀態機（stance_floor_path），
# Validator 有帳本歷史時整段重算；沒有這兩個欄位的舊 Policy 維持無記憶的 stance_floor。
RELEASE_FIELDS = ("release_neutral_below", "release_defensive_below")
OBSERVED_FIELDS = ("status", "peak_nav", "peak_as_of", "current_nav", "drawdown", "history_length", "stance_floor",
                   "history_ref")
DRAWDOWN_STATUSES = ("available", "unavailable")
_PRECISION = Decimal("0.000001")


def _decimal(value: object, field: str) -> Decimal:
    return parse_decimal(value, field, error=DecisionToolError, reject_bool=True)


def stance_floor(control: Mapping[str, object], drawdown: Decimal) -> str:
    """回撤達 defensive_at 時至少 defensive，達 neutral_at 時至少 neutral，否則不設限。"""
    if drawdown >= _decimal(control["defensive_at"], "drawdown_control.defensive_at"):
        return "defensive"
    if drawdown >= _decimal(control["neutral_at"], "drawdown_control.neutral_at"):
        return "neutral"
    return "aggressive"


def hysteresis_enabled(control: Mapping[str, object]) -> bool:
    return all(field in control for field in RELEASE_FIELDS)


def stance_floor_path(control: Mapping[str, object], navs: Sequence[Decimal]) -> str:
    """沿 NAV 序列（帳本歷史＋目前 NAV）逐筆推進的姿態下限狀態機；序列為空時為 aggressive。

    每一筆以「截至該筆的峰值」算回撤：達 defensive_at 進 defensive；達 neutral_at 至少 neutral；
    在 defensive 時回撤低於 release_defensive_below 才降回 neutral；在 neutral 時低於 release_neutral_below 才解除。
    """
    neutral_at = _decimal(control["neutral_at"], "drawdown_control.neutral_at")
    defensive_at = _decimal(control["defensive_at"], "drawdown_control.defensive_at")
    release_neutral = _decimal(control["release_neutral_below"], "drawdown_control.release_neutral_below")
    release_defensive = _decimal(control["release_defensive_below"], "drawdown_control.release_defensive_below")
    state = "aggressive"
    peak = Decimal(0)
    for index, nav in enumerate(navs):
        if nav <= 0:
            raise DecisionToolError("NAV 序列第 %d 筆必須大於 0" % index)
        peak = max(peak, nav)
        drawdown = ((peak - nav) / peak).quantize(_PRECISION, rounding=ROUND_HALF_UP)
        if drawdown >= defensive_at:
            state = "defensive"
        elif state == "defensive":
            if drawdown < release_defensive:
                state = "neutral" if drawdown >= release_neutral else "aggressive"
        elif drawdown >= neutral_at:
            state = "neutral"
        elif state == "neutral" and drawdown < release_neutral:
            state = "aggressive"
    return state


def stance_meets_floor(level: object, floor: object) -> bool:
    if floor is None:
        return True
    return CASH_STANCE_RANK.get(str(level), -1) >= CASH_STANCE_RANK.get(str(floor), 99)


def measure_drawdown(current_nav: Decimal, nav_history: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """峰值取帳本歷史與目前 NAV 的最大值；回撤 = (峰值 − 目前) ÷ 峰值，四捨五入到小數 6 位。"""
    peak_nav, peak_as_of = current_nav, None
    for index, row in enumerate(nav_history):
        nav = _decimal(row.get("nav"), "nav_history[%d].nav" % index)
        if nav <= 0:
            raise DecisionToolError("nav_history[%d].nav 必須大於 0" % index)
        if nav > peak_nav:
            peak_nav, peak_as_of = nav, str(row.get("as_of"))
    if peak_nav <= 0:
        raise DecisionToolError("目前 NAV 必須大於 0")
    drawdown = ((peak_nav - current_nav) / peak_nav).quantize(_PRECISION, rounding=ROUND_HALF_UP)
    return {"peak_nav": str(peak_nav), "peak_as_of": peak_as_of, "current_nav": str(current_nav),
            "drawdown": str(max(drawdown, Decimal(0))), "history_length": len(nav_history)}


def history_reference(nav_history: Sequence[Mapping[str, object]]) -> Optional[Dict[str, object]]:
    """帳本 NAV 歷史的可驗證參照：最新一筆的 run_id、筆數與（run_id、as_of、nav）序列的 canonical sha256。

    歷史列沒有 run_id（例如回放的記憶體序列）或為空時回傳 None，維持沒有此欄位的舊行為。"""
    if not nav_history or any(not isinstance(row, Mapping) or not row.get("run_id") for row in nav_history):
        return None
    rows = [{"run_id": str(row["run_id"]), "as_of": str(row.get("as_of")), "nav": str(row.get("nav"))}
            for row in nav_history]
    return {"head_run_id": rows[-1]["run_id"], "length": len(rows), "sha256": canonical_sha256(rows)}


def verify_history_reference(
    control: Mapping[str, object], current_nav: Decimal, nav_history: Sequence[Mapping[str, object]],
    errors: List[str], bundle_cutoff=None,
) -> None:
    """以可讀的帳本歷史（須止於 history_ref.head_run_id）重算參照、峰值、峰值時間與筆數並逐項比對。"""
    prefix = "DecisionPolicy.drawdown_control"
    ref = control.get("history_ref")
    if not isinstance(ref, Mapping):
        errors.append("%s 有帳本歷史可驗證時必須含 history_ref" % prefix)
        return
    expected_ref = history_reference(nav_history)
    if expected_ref is None or dict(ref) != expected_ref:
        errors.append("%s.history_ref 與帳本 NAV 歷史不一致" % prefix)
        return
    cutoff = bundle_cutoff
    if cutoff is not None:
        for index, row in enumerate(nav_history):
            try:
                as_of = parse_aware_time(row.get("as_of"), "nav_history[%d].as_of" % index, error=DecisionToolError)
            except DecisionToolError as error:
                errors.append(str(error))
                return
            if as_of > cutoff:
                errors.append("%s 帳本 NAV 歷史第 %d 列的時間晚於 decision_cutoff" % (prefix, index))
                return
    try:
        measured = measure_drawdown(current_nav, nav_history)
    except DecisionToolError as error:
        errors.append(str(error))
        return
    for field in ("peak_nav", "peak_as_of", "history_length"):
        if control.get(field) != measured[field]:
            errors.append("%s.%s 與帳本 NAV 歷史重算不一致" % (prefix, field))


def bind_drawdown(
    policy: Mapping[str, object], bundle: Mapping[str, object],
    nav_history: Optional[Sequence[Mapping[str, object]]],
) -> Dict[str, object]:
    """把本次觀察到的回撤與姿態下限綁進 policy 的 drawdown_control；樣板沒有啟用時原樣回傳。

    ``nav_history`` 為 None（沒有帳本歷史可讀）時記為 unavailable、不設下限。呼叫端須重算 content_sha256。
    """
    result = dict(policy)
    control = policy.get("drawdown_control")
    if not isinstance(control, Mapping):
        return result
    thresholds = {field: control[field] for field in THRESHOLD_FIELDS + RELEASE_FIELDS if field in control}
    current = _decimal(bundle["account_snapshot"]["nav"], "account_snapshot.nav")
    if nav_history is None:
        observed: Dict[str, object] = {"status": "unavailable", "peak_nav": None, "peak_as_of": None,
                                       "current_nav": str(current), "drawdown": None, "history_length": 0,
                                       "stance_floor": None}
    else:
        observed = {"status": "available", **measure_drawdown(current, nav_history)}
        if hysteresis_enabled(thresholds):
            navs = [_decimal(row.get("nav"), "nav_history[%d].nav" % index) for index, row in enumerate(nav_history)]
            observed["stance_floor"] = stance_floor_path(thresholds, navs + [current])
        else:
            observed["stance_floor"] = stance_floor(thresholds, Decimal(observed["drawdown"]))
        reference = history_reference(nav_history)
        if reference is not None:
            observed["history_ref"] = reference
    result["drawdown_control"] = {**thresholds, **observed}
    result.pop("content_sha256", None)
    return result


def validate_drawdown_control(
    policy: Mapping[str, object], bundle: Mapping[str, object], errors: List[str],
    nav_history: Optional[Sequence[Mapping[str, object]]] = None,
) -> None:
    """門檻合理、觀察值可由 policy 內的峰值與共同輸入的 NAV 重算，且已綁定的現金姿態不低於下限。

    policy 帶 ``history_ref`` 且呼叫端提供帳本歷史（``nav_history``，止於 head_run_id）時，另由歷史重算
    參照與峰值；未提供帳本歷史（舊 run 重建）時只做結構檢查，行為與舊版相同。"""
    control = policy.get("drawdown_control")
    prefix = "DecisionPolicy.drawdown_control"
    if not isinstance(control, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(control, set(THRESHOLD_FIELDS) | set(RELEASE_FIELDS) | set(OBSERVED_FIELDS), prefix, errors)
    try:
        neutral = _decimal(control.get("neutral_at"), prefix + ".neutral_at")
        defensive = _decimal(control.get("defensive_at"), prefix + ".defensive_at")
    except DecisionToolError as error:
        errors.append(str(error))
        return
    if not Decimal(0) < neutral < defensive < Decimal(1):
        errors.append("%s 門檻必須滿足 0 < neutral_at < defensive_at < 1" % prefix)
        return
    sizing = policy.get("position_sizing")
    bound_stance = sizing.get("cash_stance") if isinstance(sizing, Mapping) else None
    # 解除門檻（hysteresis）與 position_sizing.drawdown_derisk 閘門成對：只有一邊即拒絕，舊 Policy 兩邊都沒有。
    derisk_gate = isinstance(sizing, Mapping) and "drawdown_derisk" in sizing
    hysteresis = hysteresis_enabled(control)
    if any(field in control for field in RELEASE_FIELDS) and not hysteresis:
        errors.append("%s 的 release_neutral_below 與 release_defensive_below 必須同時出現" % prefix)
        return
    if hysteresis != derisk_gate:
        errors.append("%s 的解除門檻必須與 position_sizing.drawdown_derisk 閘門同時存在" % prefix)
        return
    if hysteresis:
        try:
            release_neutral = _decimal(control.get("release_neutral_below"), prefix + ".release_neutral_below")
            release_defensive = _decimal(control.get("release_defensive_below"), prefix + ".release_defensive_below")
        except DecisionToolError as error:
            errors.append(str(error))
            return
        if not (Decimal(0) < release_neutral < neutral and release_neutral < release_defensive < defensive):
            errors.append("%s 解除門檻必須滿足 0 < release_neutral_below < neutral_at 且 release_neutral_below < release_defensive_below < defensive_at" % prefix)
            return
    if "status" not in control:
        if bound_stance is not None:
            errors.append("%s 綁定現金姿態前必須先記錄本次回撤" % prefix)
        return
    status = control.get("status")
    if status not in DRAWDOWN_STATUSES:
        errors.append("%s.status 必須是 %s" % (prefix, "／".join(DRAWDOWN_STATUSES)))
        return
    try:
        current = _decimal(bundle["account_snapshot"]["nav"], "account_snapshot.nav")
        if _decimal(control.get("current_nav"), prefix + ".current_nav") != current:
            errors.append("%s.current_nav 與 AccountSnapshot NAV 不一致" % prefix)
        if status == "unavailable":
            if control.get("stance_floor") is not None or control.get("drawdown") is not None:
                errors.append("%s 無歷史時不得有回撤或姿態下限" % prefix)
            # 呼叫端提供了可驗證的帳本歷史，就不能宣稱無歷史：否則清掉 drawdown／stance_floor 即可躲過下限。
            if nav_history is not None and history_reference(nav_history) is not None:
                errors.append("%s 帳本有可驗證的 NAV 歷史，status 不得為 unavailable" % prefix)
            return
        peak = _decimal(control.get("peak_nav"), prefix + ".peak_nav")
        if peak < current or peak <= 0:
            errors.append("%s.peak_nav 不得低於目前 NAV" % prefix)
            return
        ref = control.get("history_ref")
        length = control.get("history_length")
        # 嚴格的時間／筆數檢查只套用在帶 history_ref 的新 policy；舊 policy 逐位元維持原行為。
        if ref is not None:
            # 目前 NAV 就是峰值時 peak_as_of 為 None（沒有更高的歷史點）；有值時必須含時區且不晚於 cutoff。
            if control.get("peak_as_of") is not None:
                peak_as_of = parse_aware_time(control.get("peak_as_of"), prefix + ".peak_as_of", error=DecisionToolError)
                if peak_as_of > parse_aware_time(bundle.get("decision_cutoff"), "decision_cutoff", error=DecisionToolError):
                    errors.append("%s.peak_as_of 晚於 decision_cutoff" % prefix)
            if not isinstance(length, int) or isinstance(length, bool) or length < 0:
                errors.append("%s.history_length 必須是非負整數" % prefix)
        if ref is not None:
            if not isinstance(ref, Mapping) or set(ref) != {"head_run_id", "length", "sha256"} or ref.get("length") != length:
                errors.append("%s.history_ref 必須含 head_run_id、length（等於 history_length）、sha256" % prefix)
            elif nav_history is not None:
                verify_history_reference(control, current, nav_history, errors,
                                         parse_aware_time(bundle.get("decision_cutoff"), "decision_cutoff", error=DecisionToolError))
        elif nav_history is not None and history_reference(nav_history) is not None:
            # 呼叫端提供帳本歷史即要求嚴格驗證：新 policy 不得靠省略 history_ref 躲過重算。
            errors.append("%s 有帳本歷史可驗證時必須含 history_ref" % prefix)
        expected = measure_drawdown(current, [{"nav": str(peak), "as_of": control.get("peak_as_of")}])["drawdown"]
        if control.get("drawdown") != expected:
            errors.append("%s.drawdown 與峰值／目前 NAV 重算不一致" % prefix)
        floor = stance_floor(control, Decimal(expected))
        if not hysteresis:
            if control.get("stance_floor") != floor:
                errors.append("%s.stance_floor 應為 %s" % (prefix, floor))
        elif nav_history is not None:
            # 有帳本歷史時整段重算狀態機（歷史本身已由 history_ref 核對）。
            navs = [_decimal(row.get("nav"), "nav_history[%d].nav" % index) for index, row in enumerate(nav_history)]
            path_floor = stance_floor_path(control, navs + [current])
            if control.get("stance_floor") != path_floor:
                errors.append("%s.stance_floor 依帳本歷史重算應為 %s" % (prefix, path_floor))
        elif not stance_meets_floor(control.get("stance_floor"), floor) or control.get("stance_floor") not in CASH_STANCE_RANK:
            # 沒有歷史可讀時只能確認：解除門檻只會讓下限更保守，不得低於無記憶的下限。
            errors.append("%s.stance_floor 不得低於無記憶門檻 %s" % (prefix, floor))
    except DecisionToolError as error:
        errors.append(str(error))
        return
    if bound_stance is not None and not stance_meets_floor(bound_stance, control.get("stance_floor")):
        errors.append("現金姿態 %s 低於回撤下限 %s" % (bound_stance, control.get("stance_floor")))
