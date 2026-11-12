"""程式兜底的條件式硬停損（``position_sizing.stop_exit`` v1，版本閘門，只由人設定；見 ``docs/plans/stop_loss_take_profit_plan.md`` 6.3）。

停損停利的判斷仍交給交易 Agent（持股檢視警示）；程式只兜底一種情況：個股自進場起虧損很深，而且明顯輸給交易池。
四個條件**全部成立**才強制出場（人填的數字，程式不給預設）：

- 自進場虧損 ≥ ``loss``（``return_since_entry`` ≤ −loss）；
- 同期輸交易池等權 ≥ ``lag_to_pool``（``relative_to_pool_since_entry`` ≤ −lag_to_pool），所以大盤一起跌時不會整批賣出；
- 持有交易日數 ≥ ``min_holding_days``（進場日之後已有收盤的交易日數，避免剛進場就被洗出）；
- 當日不可交易的持股不強制（留 ``RISK_STOP_EXIT_CANCELLED``，Guard 的 TRADABLE 不會因此否決）。

事實全部取自 ``position_review`` v2 的自進場事實（同一份 ``build_position_review``、不依賴帳戶 average_cost；持有起點取自 Policy
綁定的 ``exposure_rotation.held_since_by_symbol``，Finalizer 以帳本重算）。事實算不出來（``entry_gap``、交易池沒有同期報酬）時
**不強制賣出**、不補值，留 ``STOP_EXIT_UNAVAILABLE``，該檔仍有持股檢視警示、由交易 Agent 判斷（fail-closed 的意思是「缺資料時程式不自動下單」）。
單日最多 ``max_exits_per_run`` 檔：超過的依虧損由大到小（同值依代號）取前幾檔，其餘留 ``RISK_STOP_EXIT_CANCELLED``。

本模組只做純函式的觸發判定；出場的執行、旗標與硬規則（min_positions／現金上限／換手上限留住）、換股換出者、回撤減碼的互動在
``allocation.AllocationOrderEngine``。沒有閘門的 Policy 完全不呼叫持股檢視的計算（逐位元維持舊行為）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, NamedTuple, Optional, Set

from .fields import DecisionToolError, decimal_value, reject_unknown_fields
from .position_review import POLICY_VERSION_V2, build_position_review

STOP_EXIT_VERSION = "v1"
STOP_EXIT_FIELDS = {"version", "loss", "lag_to_pool", "min_holding_days", "max_exits_per_run"}
STOP_EXIT_CODE = "RISK_STOP_EXIT"  # 程式強制出場（訂單意圖標 exit，交易 Agent 的意圖不改寫）
STOP_EXIT_CANCELLED_CODE = "RISK_STOP_EXIT_CANCELLED"  # 條件成立但沒有強制出場（不可交易、超過單日上限、硬規則留住）
STOP_EXIT_UNAVAILABLE_CODE = "STOP_EXIT_UNAVAILABLE"  # 進場事實算不出來，程式不強制出場


def stop_exit_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """stop_exit 啟用時回傳閘門物件；沒有閘門回 None。"""
    gate = sizing.get("stop_exit") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def _ratio(value: object, label: str, errors: List[str]) -> None:
    try:
        number = decimal_value(value, label)
    except DecisionToolError as error:
        errors.append(str(error))
        return
    if not Decimal(0) < number < Decimal(1):
        errors.append("%s 必須大於 0 且小於 1" % label)


def validate_stop_exit_gate(policy: Mapping[str, object], errors: List[str]) -> None:
    """形狀與值域（全部必填、不給預設）加頂層前提：``position_review`` 必須是 v2（事實同一份）。
    ``exposure_rotation``、``reentry_cooldown`` 的存在由 ``sizing.GATE_PREREQUISITES`` 檢查。"""
    sizing = policy.get("position_sizing")
    gate = stop_exit_gate(sizing)
    prefix = "position_sizing.stop_exit"
    if gate is None:
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, STOP_EXIT_FIELDS, prefix, errors)
    if gate.get("version") != STOP_EXIT_VERSION:
        errors.append("%s.version 只允許 %s" % (prefix, STOP_EXIT_VERSION))
    for field in ("loss", "lag_to_pool"):
        if field not in gate:
            errors.append("%s 缺少 %s（由人填，程式不給預設）" % (prefix, field))
        else:
            _ratio(gate[field], "%s.%s" % (prefix, field), errors)
    for field in ("min_holding_days", "max_exits_per_run"):
        value = gate.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            errors.append("%s.%s 必須是不小於 1 的整數（由人填，程式不給預設）" % (prefix, field))
    review = policy.get("position_review")
    if not (isinstance(review, Mapping) and review.get("version") == POLICY_VERSION_V2):
        errors.append("%s 必須與 position_review v2 同時存在（自進場事實與警示取自同一份持股檢視）" % prefix)


class StopExitPlan(NamedTuple):
    """觸發判定結果：``triggered`` 是實際要強制出場的持股（已排除不可交易並套單日上限，依虧損由大到小）；``details`` 是各檔的旗標說明；
    ``notes`` 是不強制出場的旗標（缺資料、不可交易、超過單日上限），每筆是 constraint_flags 的一列。"""

    triggered: List[str]
    details: Dict[str, str]
    notes: List[Dict[str, object]]


EMPTY_PLAN = StopExitPlan([], {}, [])


def _pct(value: Decimal) -> str:
    return format(value, ".2%")


def build_stop_exit_plan(
    bundle: Mapping[str, object], policy: Mapping[str, object], untradable: Set[str],
) -> StopExitPlan:
    """逐檔持股判定停損是否觸發；只讀 bundle 與 Policy（含綁定的持有起點），不讀任何 Agent 輸出。"""
    gate = stop_exit_gate(policy.get("position_sizing"))
    if gate is None:
        return EMPTY_PLAN
    loss = decimal_value(gate["loss"], "stop_exit.loss")
    lag = decimal_value(gate["lag_to_pool"], "stop_exit.lag_to_pool")
    min_days = int(gate["min_holding_days"])
    cap = int(gate["max_exits_per_run"])
    review = build_position_review(bundle, policy)
    notes: List[Dict[str, object]] = []
    hits: List[tuple] = []
    for item in review["items"]:  # type: ignore[union-attr]
        symbol = str(item["symbol"])
        gap = item.get("entry_gap")
        own, relative, days = item.get("return_since_entry"), item.get("relative_to_pool_since_entry"), item.get("holding_trading_days")
        if gap is not None or own is None or relative is None or days is None:
            notes.append({
                "symbol": symbol, "code": STOP_EXIT_UNAVAILABLE_CODE,
                "detail": "自進場起算的事實不可用（%s），程式不強制出場、不補值；仍由持股檢視警示與交易 Agent 判斷" % (gap or "pool_unavailable"),
            })
            continue
        own_value, relative_value = Decimal(str(own)), Decimal(str(relative))
        if int(days) >= min_days and own_value <= -loss and relative_value <= -lag:
            hits.append((own_value, symbol, relative_value, int(days)))
    hits.sort(key=lambda row: (row[0], row[1]))  # 虧損由大到小、同值依代號
    triggered: List[str] = []
    details: Dict[str, str] = {}
    for own_value, symbol, relative_value, days in hits:
        if symbol in untradable:
            notes.append({
                "symbol": symbol, "code": STOP_EXIT_CANCELLED_CODE,
                "detail": "停損條件成立但非可交易狀態，程式不強制出場（交易 Agent 的意圖未被改寫）",
            })
            continue
        if len(triggered) >= cap:
            notes.append({
                "symbol": symbol, "code": STOP_EXIT_CANCELLED_CODE,
                "detail": "停損條件成立但本次強制出場已達單日上限 %d 檔，依虧損由大到小取前幾檔，其餘不強制（交易 Agent 的意圖未被改寫）" % cap,
            })
            continue
        triggered.append(symbol)
        details[symbol] = (
            "自進場虧損 %s（門檻 %s）且輸交易池 %s（門檻 %s），持有 %d 個交易日（門檻 %d）：程式兜底強制出場"
            "（交易 Agent 的意圖未被改寫，訂單意圖標為 exit）"
            % (_pct(-own_value), _pct(loss), _pct(-relative_value), _pct(lag), days, min_days)
        )
    return StopExitPlan(triggered, details, notes)


__all__ = [
    "EMPTY_PLAN", "STOP_EXIT_CANCELLED_CODE", "STOP_EXIT_CODE", "STOP_EXIT_FIELDS", "STOP_EXIT_UNAVAILABLE_CODE", "STOP_EXIT_VERSION",
    "StopExitPlan", "build_stop_exit_plan", "stop_exit_gate", "validate_stop_exit_gate",
]
