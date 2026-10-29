"""持股檢視（停損／停利的判斷依據，確定性工具）。

程式只負責算事實：每檔持股的帳面報酬、從近期高點的回落，並依 ``policy["position_review"]`` 的門檻
標出「需要交易 Agent 正視」的警示。門檻預設全部關閉（``null``）：停損停利由交易 Agent 依這些事實自行判斷，不設死數字；人若想加一道提醒，
可在 policy 設門檻。警示不是賣出指令；賣不賣、減碼或出場仍由交易 Agent 判斷，
並以 ``review_id`` 放進 ``adopted_claim_ids``（與多空 claim 一樣必須剛好處理一次，且不可否決）。

數值全部由 bundle 與 policy 重算，Agent 不輸出任何數字；Finalizer 以相同輸入重算整份比對，
任何改寫一律拒絕。持股檢視讀的是帳戶與同 Snapshot 行情，因此可引用 ``account`` 與該檔行情證據。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, Optional

from .fields import DecisionToolError, decimal_value, reject_unknown_fields, snapshot_execution_price


POSITION_REVIEW_VERSION = "1.0"
POLICY_VERSION = "v1"
POLICY_FIELDS = {"version", "loss_alert", "retrace_alert_20d", "gain_alert"}
REVIEW_FIELDS = {"schema_version", "policy", "cost_basis_note", "items"}
ITEM_FIELDS = {
    "symbol", "position_id", "average_cost", "price", "unrealized_return", "retrace_from_high_5d", "retrace_from_high_20d",
    "flags", "evidence_ids",
}
FLAG_FIELDS = {"review_id", "flag"}
FLAGS = ("loss_alert", "retrace_alert", "gain_alert")
RETRACE_WINDOW = 20
SHORT_RETRACE_WINDOW = 5  # 短線判斷用：近 5 日高點回落
QUANTUM = Decimal("0.000001")
COST_BASIS_NOTE = (
    "賺賠從帳戶成本起算；比賽開帳或重建日的成本是當日結算收盤價，不是真實買價。"
    "警示只代表需要正視，賣不賣、減碼或出場由交易 Agent 判斷；數值由程式計算，不可自行改寫。"
)


def review_id(symbol: str, flag: str) -> str:
    """警示識別碼；刻意避開 bull-／bear- 前綴，不會與多空 claim 混淆。"""
    return "review:%s:%s" % (symbol.upper(), flag)


def position_id(symbol: str) -> str:
    """每檔持股都有的「持股事實」編號（選用）：交易 Agent 可用自己對報酬與回落的判斷當減碼／出場依據，但不強制處理。"""
    return "review:%s:position" % symbol.upper()


def validate_position_review_policy(policy: object, errors: List[str]) -> None:
    """``policy["position_review"]`` 的門檻驗證：版本只允許 v1，門檻必須落在 0～1 之間（不含）。"""
    prefix = "position_review"
    if not isinstance(policy, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(policy, POLICY_FIELDS, prefix, errors)
    if policy.get("version") != POLICY_VERSION:
        errors.append("%s.version 必須為 %s" % (prefix, POLICY_VERSION))
    for field in ("loss_alert", "retrace_alert_20d", "gain_alert"):
        if field not in policy:
            errors.append("%s 缺少 %s（不啟用請明確寫 null）" % (prefix, field))
            continue
        if policy[field] is None:
            continue
        try:
            value = decimal_value(policy[field], "%s.%s" % (prefix, field))
        except DecisionToolError as error:
            errors.append(str(error))
            continue
        if not Decimal(0) < value < Decimal(1):
            errors.append("%s.%s 必須大於 0 且小於 1" % (prefix, field))


def _threshold(policy: Mapping[str, object], field: str) -> Optional[Decimal]:
    value = policy.get(field)
    return None if value is None else decimal_value(value, "position_review.%s" % field)


def _quantize(value: Decimal) -> str:
    return str(value.quantize(QUANTUM))


def build_position_review(bundle: Mapping[str, object], policy: Mapping[str, object]) -> Dict[str, object]:
    """逐檔持股的帳面報酬、近 20 日高點回落與警示；policy 沒有 ``position_review`` 時呼叫端不應建立。

    只讀 ``apply_trade_decision`` 不會改動的 policy 欄位，所以基礎 policy 與最終 policy 建出的結果相同。
    """
    settings = policy.get("position_review")
    errors: List[str] = []
    validate_position_review_policy(settings, errors)
    if errors:
        raise DecisionToolError("; ".join(errors))
    assert isinstance(settings, Mapping)
    loss_limit = _threshold(settings, "loss_alert")
    retrace_limit = _threshold(settings, "retrace_alert_20d")
    gain_limit = _threshold(settings, "gain_alert")
    account = bundle["account_snapshot"]
    latest = {str(row["symbol"]).upper(): row for row in bundle["snapshot"]["latest_prices"]}
    series = {str(row["symbol"]).upper(): row for row in bundle["price_series"]}
    items: List[Dict[str, object]] = []
    for position in sorted(account["positions"], key=lambda row: str(row["symbol"]).upper()):
        symbol = str(position["symbol"]).upper()
        if int(position.get("shares", 0)) <= 0:
            continue
        if symbol not in latest or symbol not in series:
            raise DecisionToolError("持股 %s 缺少同 Snapshot 的行情，無法建立持股檢視" % symbol)
        cost = decimal_value(position["average_cost"], "%s.average_cost" % symbol)
        price = snapshot_execution_price(latest[symbol], symbol)
        if cost <= 0 or price <= 0:
            raise DecisionToolError("持股 %s 的成本與現價必須大於 0" % symbol)
        unrealized = price / cost - Decimal(1)
        # 高點與最新收盤同為調整價口徑，避免與未調整的成本混算；視窗不足 20 根時不補假值。
        bars = series[symbol]["bars"]
        retrace: Optional[Decimal] = None
        short_retrace: Optional[Decimal] = None
        for window in (RETRACE_WINDOW, SHORT_RETRACE_WINDOW):
            if len(bars) < window:
                continue
            recent = bars[-window:]
            high = max(decimal_value(bar["high"], "%s.high" % symbol) for bar in recent)
            close = decimal_value(recent[-1]["close"], "%s.close" % symbol)
            if high > 0:
                value = close / high - Decimal(1)
                if window == RETRACE_WINDOW:
                    retrace = value
                else:
                    short_retrace = value
        flags: List[Dict[str, str]] = []
        if loss_limit is not None and unrealized <= -loss_limit:
            flags.append({"review_id": review_id(symbol, "loss_alert"), "flag": "loss_alert"})
        if retrace_limit is not None and retrace is not None and retrace <= -retrace_limit:
            flags.append({"review_id": review_id(symbol, "retrace_alert"), "flag": "retrace_alert"})
        if gain_limit is not None and unrealized >= gain_limit:
            flags.append({"review_id": review_id(symbol, "gain_alert"), "flag": "gain_alert"})
        items.append({
            "symbol": symbol,
            "position_id": position_id(symbol),
            "average_cost": _quantize(cost),
            "price": _quantize(price),
            "unrealized_return": _quantize(unrealized),
            "retrace_from_high_5d": None if short_retrace is None else _quantize(short_retrace),
            "retrace_from_high_20d": None if retrace is None else _quantize(retrace),
            "flags": flags,
            "evidence_ids": [str(account["source_evidence_id"]), str(latest[symbol]["source_evidence_id"])],
        })
    return {
        "schema_version": POSITION_REVIEW_VERSION,
        "policy": {key: settings.get(key) for key in sorted(POLICY_FIELDS)},
        "cost_basis_note": COST_BASIS_NOTE,
        "items": items,
    }


def validate_position_review(
    bundle: Mapping[str, object],
    policy: Mapping[str, object],
    review: object,
    errors: List[str],
) -> None:
    """以同一輸入重算整份持股檢視並逐欄比對；不一致即拒絕（fail-closed）。"""
    prefix = "team_inputs.position_review"
    if not isinstance(review, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(review, REVIEW_FIELDS, prefix, errors)
    try:
        expected = build_position_review(bundle, policy)
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        errors.append("%s 無法以共同輸入重算：%s" % (prefix, error))
        return
    if dict(review) != expected:
        errors.append("%s 與重算結果不同，拒絕改寫" % prefix)


def review_ids_by_symbol(review: Optional[Mapping[str, object]]) -> Dict[str, Dict[str, str]]:
    """每檔需要處理的警示：review_id → flag。沒有檢視（舊 run）回空字典。"""
    result: Dict[str, Dict[str, str]] = {}
    if not isinstance(review, Mapping):
        return result
    for item in review.get("items", []):
        if not isinstance(item, Mapping):
            continue
        for flag in item.get("flags", []):
            result.setdefault(str(item["symbol"]).upper(), {})[str(flag["review_id"])] = str(flag["flag"])
    return result


def position_ids_by_symbol(review: Optional[Mapping[str, object]]) -> Dict[str, str]:
    """每檔持股的選用編號（symbol → position_id）；沒有檢視回空字典。"""
    if not isinstance(review, Mapping):
        return {}
    return {str(item["symbol"]).upper(): str(item["position_id"]) for item in review.get("items", []) if isinstance(item, Mapping) and item.get("position_id")}


def position_review_brief(review: Mapping[str, object]) -> Dict[str, object]:
    """給交易 Agent 的精簡背景：整體說明加逐股數字；數字只供判斷，不可改寫。"""
    return {
        "policy": review["policy"],
        "note": review["cost_basis_note"],
        "by_symbol": {
            str(item["symbol"]): {key: item[key] for key in (
                "position_id", "average_cost", "price", "unrealized_return", "retrace_from_high_5d", "retrace_from_high_20d", "flags", "evidence_ids",
            )}
            for item in review["items"]
        },
    }
