"""回撤控制：從帳本 NAV 歷史確定性計算峰值回撤，對應風險 Agent 現金姿態的下限。

策略文件的目標是「控制回撤與集中風險」。回撤由程式計算（峰值 NAV 對目前 NAV 的跌幅），
門檻放在 Policy 的 ``drawdown_control``；風險 Agent 只看到結果與下限，不能自行計算或放寬。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.core import parse_decimal

from .contracts import DecisionToolError, reject_unknown_fields


CASH_STANCE_RANK = {"aggressive": 0, "neutral": 1, "defensive": 2}
THRESHOLD_FIELDS = ("neutral_at", "defensive_at")
OBSERVED_FIELDS = ("status", "peak_nav", "peak_as_of", "current_nav", "drawdown", "history_length", "stance_floor")
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
    thresholds = {field: control[field] for field in THRESHOLD_FIELDS}
    current = _decimal(bundle["account_snapshot"]["nav"], "account_snapshot.nav")
    if nav_history is None:
        observed: Dict[str, object] = {"status": "unavailable", "peak_nav": None, "peak_as_of": None,
                                       "current_nav": str(current), "drawdown": None, "history_length": 0,
                                       "stance_floor": None}
    else:
        observed = {"status": "available", **measure_drawdown(current, nav_history)}
        observed["stance_floor"] = stance_floor(thresholds, Decimal(observed["drawdown"]))
    result["drawdown_control"] = {**thresholds, **observed}
    result.pop("content_sha256", None)
    return result


def validate_drawdown_control(policy: Mapping[str, object], bundle: Mapping[str, object], errors: List[str]) -> None:
    """門檻合理、觀察值可由 policy 內的峰值與共同輸入的 NAV 重算，且已綁定的現金姿態不低於下限。"""
    control = policy.get("drawdown_control")
    prefix = "DecisionPolicy.drawdown_control"
    if not isinstance(control, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(control, set(THRESHOLD_FIELDS) | set(OBSERVED_FIELDS), prefix, errors)
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
            return
        peak = _decimal(control.get("peak_nav"), prefix + ".peak_nav")
        if peak < current or peak <= 0:
            errors.append("%s.peak_nav 不得低於目前 NAV" % prefix)
            return
        expected = measure_drawdown(current, [{"nav": str(peak), "as_of": control.get("peak_as_of")}])["drawdown"]
        if control.get("drawdown") != expected:
            errors.append("%s.drawdown 與峰值／目前 NAV 重算不一致" % prefix)
        floor = stance_floor(control, Decimal(expected))
        if control.get("stance_floor") != floor:
            errors.append("%s.stance_floor 應為 %s" % (prefix, floor))
    except DecisionToolError as error:
        errors.append(str(error))
        return
    if bound_stance is not None and not stance_meets_floor(bound_stance, control.get("stance_floor")):
        errors.append("現金姿態 %s 低於回撤下限 %s" % (bound_stance, control.get("stance_floor")))
