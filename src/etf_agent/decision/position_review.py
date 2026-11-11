"""持股檢視（停損／停利的判斷依據，確定性工具）。

程式只負責算事實：每檔持股的帳面報酬、從近期高點的回落，並依 ``policy["position_review"]`` 的門檻
標出「需要交易 Agent 正視」的警示。門檻預設全部關閉（``null``）：停損停利由交易 Agent 依這些事實自行判斷，不設死數字；人若想加一道提醒，
可在 policy 設門檻。警示不是賣出指令；賣不賣、減碼或出場仍由交易 Agent 判斷，
並以 ``review_id`` 放進 ``adopted_claim_ids``（與多空 claim 一樣必須剛好處理一次，且不可否決）。

數值全部由 bundle 與 policy 重算，Agent 不輸出任何數字；Finalizer 以相同輸入重算整份比對，
任何改寫一律拒絕。持股檢視讀的是帳戶與同 Snapshot 行情，因此可引用 ``account`` 與該檔行情證據。
"""

from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Tuple

from etf_agent.core import parse_aware_time

from .fields import DecisionToolError, decimal_value, reject_unknown_fields, snapshot_execution_price


POSITION_REVIEW_VERSION = "1.0"
POLICY_VERSION = "v1"
POLICY_VERSION_V2 = "v2"  # 自進場起算的三個警示（版本閘門，只由人設定；沒有 v2 的 Policy 逐位元維持 v1 行為）
POLICY_VERSIONS = (POLICY_VERSION, POLICY_VERSION_V2)
POLICY_FIELDS = {"version", "loss_alert", "retrace_alert_20d", "gain_alert", "context"}
POLICY_FIELDS_V2 = POLICY_FIELDS | {"trail_alert", "relative_loss_alert", "stale_alert"}
# v2 的巢狀門檻物件欄位（全部必填；整個物件為 null 代表不啟用該警示）。
TRAIL_ALERT_FIELDS = ("arm_gain", "giveback")
RELATIVE_LOSS_ALERT_FIELDS = ("loss", "lag")
STALE_ALERT_FIELDS = ("days",)
TAIPEI = timezone(timedelta(hours=8))
CONTEXT_VERSION = "v1"  # policy 設 context=v1 才在每檔附上相對交易池的報酬與基本面資料日期
REVIEW_FIELDS = {"schema_version", "policy", "cost_basis_note", "items"}
ITEM_FIELDS = {
    "symbol", "position_id", "average_cost", "price", "unrealized_return", "retrace_from_high_5d", "retrace_from_high_20d",
    "flags", "evidence_ids",
}
CONTEXT_ITEM_FIELDS = {
    "return_5d", "pool_return_5d", "relative_to_pool_5d", "return_20d", "pool_return_20d", "relative_to_pool_20d",
    "pool_size", "latest_revenue_available_at", "latest_financial_available_at",
}
# v2 另算的自進場事實（持有起點取自 Policy 綁定的 exposure_rotation.held_since_by_symbol；不依賴帳戶 average_cost）。
ENTRY_ITEM_FIELDS = {
    "entry_date", "holding_trading_days", "return_since_entry", "peak_return_since_entry", "retrace_from_entry_high",
    "pool_return_since_entry", "relative_to_pool_since_entry", "entry_gap",
}
FLAG_FIELDS = {"review_id", "flag"}
FLAGS = ("loss_alert", "retrace_alert", "gain_alert")
FLAGS_V2 = FLAGS + ("trail_alert", "relative_loss_alert", "stale_alert")
RETRACE_WINDOW = 20
SHORT_RETRACE_WINDOW = 5  # 短線判斷用：近 5 日高點回落
QUANTUM = Decimal("0.000001")
COST_BASIS_NOTE = (
    "賺賠從帳戶成本起算；比賽開帳或重建日的成本是當日結算收盤價，不是真實買價。"
    "警示只代表需要正視，賣不賣、減碼或出場由交易 Agent 判斷；數值由程式計算，不可自行改寫。"
)
COST_BASIS_NOTE_V2 = COST_BASIS_NOTE + (
    "另有自進場起算的事實（entry_date、return_since_entry 等）：以持有起點當日收盤為基準、不依賴帳戶成本，"
    "帳本重建日也不會歸零；進場事實缺漏時 entry_gap 說明原因、不產生該類警示。"
)


def review_id(symbol: str, flag: str) -> str:
    """警示識別碼；刻意避開 bull-／bear- 前綴，不會與多空 claim 混淆。"""
    return "review:%s:%s" % (symbol.upper(), flag)


def position_id(symbol: str) -> str:
    """每檔持股都有的「持股事實」編號（選用）：交易 Agent 可用自己對報酬與回落的判斷當減碼／出場依據，但不強制處理。"""
    return "review:%s:position" % symbol.upper()


def _check_ratio(value: object, label: str, errors: List[str]) -> None:
    try:
        number = decimal_value(value, label)
    except DecisionToolError as error:
        errors.append(str(error))
        return
    if not Decimal(0) < number < Decimal(1):
        errors.append("%s 必須大於 0 且小於 1" % label)


def _check_nested(settings: Mapping[str, object], field: str, names: Tuple[str, ...], prefix: str, errors: List[str]) -> None:
    """v2 巢狀門檻物件：必須出現（不啟用寫 null）；有值時欄位全必填、不得多餘，數值落在 0～1 之間（不含）。"""
    if field not in settings:
        errors.append("%s 缺少 %s（不啟用請明確寫 null）" % (prefix, field))
        return
    value = settings[field]
    if value is None:
        return
    label = "%s.%s" % (prefix, field)
    if not isinstance(value, Mapping):
        errors.append("%s 必須是物件或 null" % label)
        return
    reject_unknown_fields(value, set(names), label, errors)
    for name in names:
        if name not in value:
            errors.append("%s 缺少 %s" % (label, name))
            continue
        if field == "stale_alert":
            days = value[name]
            if isinstance(days, bool) or not isinstance(days, int) or days < 1:
                errors.append("%s.%s 必須是不小於 1 的整數" % (label, name))
        else:
            _check_ratio(value[name], "%s.%s" % (label, name), errors)


def validate_position_review_policy(policy: object, errors: List[str]) -> None:
    """``policy["position_review"]`` 的門檻驗證：版本 v1 或 v2，門檻必須落在 0～1 之間（不含）。

    v2 另有 ``trail_alert``／``relative_loss_alert``／``stale_alert`` 三個巢狀門檻（必填，不啟用寫 null）；v1 出現它們即拒絕。"""
    prefix = "position_review"
    if not isinstance(policy, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    version = policy.get("version")
    v2 = version == POLICY_VERSION_V2
    reject_unknown_fields(policy, POLICY_FIELDS_V2 if v2 else POLICY_FIELDS, prefix, errors)
    if version not in POLICY_VERSIONS:
        errors.append("%s.version 必須為 %s 或 %s" % (prefix, POLICY_VERSION, POLICY_VERSION_V2))
    if policy.get("context") not in (None, CONTEXT_VERSION):
        errors.append("%s.context 只允許省略或 %s" % (prefix, CONTEXT_VERSION))
    for field in ("loss_alert", "retrace_alert_20d", "gain_alert"):
        if field not in policy:
            errors.append("%s 缺少 %s（不啟用請明確寫 null）" % (prefix, field))
            continue
        if policy[field] is None:
            continue
        _check_ratio(policy[field], "%s.%s" % (prefix, field), errors)
    if v2:
        _check_nested(policy, "trail_alert", TRAIL_ALERT_FIELDS, prefix, errors)
        _check_nested(policy, "relative_loss_alert", RELATIVE_LOSS_ALERT_FIELDS, prefix, errors)
        _check_nested(policy, "stale_alert", STALE_ALERT_FIELDS, prefix, errors)


def position_review_version(policy: Mapping[str, object]) -> str:
    """Policy 的 position_review 版本；沒有欄位（舊 Policy）或不是物件回 v1，行為與舊版相同。"""
    settings = policy.get("position_review") if isinstance(policy, Mapping) else None
    return POLICY_VERSION_V2 if isinstance(settings, Mapping) and settings.get("version") == POLICY_VERSION_V2 else POLICY_VERSION


def _threshold(policy: Mapping[str, object], field: str) -> Optional[Decimal]:
    value = policy.get(field)
    return None if value is None else decimal_value(value, "position_review.%s" % field)


def _quantize(value: Decimal) -> str:
    return str(value.quantize(QUANTUM))


def _window_return(bars: List[Mapping[str, object]], window: int) -> Optional[Decimal]:
    """最近 ``window`` 個交易日的報酬（調整價收盤）；根數不足不補假值。"""
    if len(bars) < window + 1:
        return None
    start = decimal_value(bars[-1 - window]["close"], "close")
    end = decimal_value(bars[-1]["close"], "close")
    return None if start <= 0 else end / start - Decimal(1)


def _pool_return(series: Mapping[str, Mapping[str, object]], window: int) -> tuple:
    """交易池（同 Snapshot 全部有足夠行情的股票）等權平均報酬與檔數。"""
    values = [value for row in series.values() for value in [_window_return(row["bars"], window)] if value is not None]
    return (sum(values) / len(values), len(values)) if values else (None, 0)


def _latest_available(bundle: Mapping[str, object], symbol: str, document_type: str) -> Optional[str]:
    """該檔最新文件的可得時間（UTC ISO）。用時間而不是字串比大小（不同時區偏移的字串不能直接比）；
    晚於 ``decision_cutoff`` 的文件不算，缺時區或無法解析就拒絕，不猜。"""
    cutoff = parse_aware_time(bundle["decision_cutoff"], "decision_cutoff", error=DecisionToolError)
    times = []
    for doc in bundle["snapshot"].get("documents", []):
        if isinstance(doc, Mapping) and doc.get("document_type") == document_type \
                and str(doc.get("symbol", "")).upper() == symbol and doc.get("available_at"):
            moment = parse_aware_time(doc["available_at"], "%s.available_at" % document_type, error=DecisionToolError)
            if moment <= cutoff:
                times.append(moment)
    return max(times).astimezone(timezone.utc).isoformat() if times else None


def _held_since_dates(policy: Mapping[str, object]) -> Mapping[str, object]:
    """Policy 綁定的每檔連續持有起點（exposure_rotation.held_since_by_symbol）；沒有綁定回空字典。"""
    sizing = policy.get("position_sizing")
    rotation = sizing.get("exposure_rotation") if isinstance(sizing, Mapping) else None
    raw = rotation.get("held_since_by_symbol") if isinstance(rotation, Mapping) else None
    return raw if isinstance(raw, Mapping) else {}


def _check_bars_visible(bundle: Mapping[str, object]) -> None:
    """v2 自進場事實讀的行情必須都在 decision_cutoff 之前可得：晚於最近交易日、可得時間缺時區或晚於 cutoff 一律拒絕（fail-closed）。"""
    cutoff = parse_aware_time(bundle["decision_cutoff"], "decision_cutoff", error=DecisionToolError)
    latest_day = str(bundle["snapshot"].get("latest_trade_date", ""))
    for row in bundle["price_series"]:
        for bar in row["bars"]:
            day = str(bar["trade_date"])
            if latest_day and day > latest_day:
                raise DecisionToolError("持股檢視 v2：%s 的行情 %s 晚於 snapshot.latest_trade_date %s" % (row["symbol"], day, latest_day))
            if bar.get("available_at") is not None:
                moment = parse_aware_time(bar["available_at"], "%s.bars.available_at" % row["symbol"], error=DecisionToolError)
                if moment > cutoff:
                    raise DecisionToolError("持股檢視 v2：%s 的行情 %s 可得時間晚於 decision_cutoff" % (row["symbol"], day))


def _close_on(bars: List[Mapping[str, object]], trade_date: str) -> Optional[Decimal]:
    for bar in bars:
        if str(bar["trade_date"]) == trade_date:
            value = decimal_value(bar["close"], "close")
            return value if value > 0 else None
    return None


def _entry_facts(
    held_since: Mapping[str, object], symbol: str, bars: List[Mapping[str, object]],
    series: Mapping[str, Mapping[str, object]],
) -> Dict[str, object]:
    """單檔自進場起算的事實，全部取自持有起點當日之後的調整收盤（不用 average_cost）。缺資料時事實為 None、``entry_gap`` 寫原因，不補值。

    - 進場基準＝持有起點（台北日期）當日的收盤；該日沒有行情或收盤無效即 ``entry_bar_missing``；
    - 持有交易日數＝進場日之後已有收盤的交易日數（進場日本身不算）；
    - 進場後最高收盤含進場日收盤，所以最高報酬不為負、回落不為正；
    - 相對交易池＝個股報酬減同期交易池等權報酬（有進場日與最新日收盤的股票，與 ``context`` 的交易池同口徑）。
    """
    empty: Dict[str, object] = {key: None for key in ENTRY_ITEM_FIELDS}

    def gap(reason: str, entry_date: Optional[str] = None) -> Dict[str, object]:
        return {**empty, "entry_date": entry_date, "entry_gap": reason}

    raw = held_since.get(symbol)
    if raw is None:
        return gap("held_since_unavailable")
    try:
        entry_date = parse_aware_time(raw, "held_since_by_symbol.%s" % symbol, error=DecisionToolError).astimezone(TAIPEI).date().isoformat()
    except DecisionToolError:
        return gap("held_since_invalid")
    index = next((i for i, bar in enumerate(bars) if str(bar["trade_date"]) == entry_date), None)
    entry_close = None if index is None else _close_on(bars[index:index + 1], entry_date)
    if index is None or entry_close is None:
        return gap("entry_bar_missing", entry_date)
    since = bars[index:]
    closes = [decimal_value(bar["close"], "%s.close" % symbol) for bar in since]
    last = closes[-1]
    peak = max(closes)
    if last <= 0 or peak <= 0:
        return gap("entry_bar_missing", entry_date)
    own = last / entry_close - Decimal(1)
    last_date = str(since[-1]["trade_date"])
    pool_values = []
    for row in series.values():
        start = _close_on(row["bars"], entry_date)
        end = _close_on(row["bars"], last_date)
        if start is not None and end is not None:
            pool_values.append(end / start - Decimal(1))
    pool = sum(pool_values) / len(pool_values) if pool_values else None
    return {
        "entry_date": entry_date,
        "holding_trading_days": len(since) - 1,
        "return_since_entry": _quantize(own),
        "peak_return_since_entry": _quantize(peak / entry_close - Decimal(1)),
        "retrace_from_entry_high": _quantize(last / peak - Decimal(1)),
        "pool_return_since_entry": None if pool is None else _quantize(pool),
        "relative_to_pool_since_entry": None if pool is None else _quantize(own - pool),
        "entry_gap": None,
    }


def _entry_flags(settings: Mapping[str, object], facts: Mapping[str, object]) -> List[str]:
    """v2 三個警示；用已量化的事實比較（與 Finalizer 重算、簡報看到的數字同一份）。缺資料的事實不產生警示。"""
    flags: List[str] = []

    def number(key: str) -> Optional[Decimal]:
        value = facts.get(key)
        return None if value is None else Decimal(str(value))

    own, peak, retrace, relative = (
        number("return_since_entry"), number("peak_return_since_entry"), number("retrace_from_entry_high"), number("relative_to_pool_since_entry"))
    trail = settings.get("trail_alert")
    if isinstance(trail, Mapping) and peak is not None and retrace is not None \
            and peak >= decimal_value(trail["arm_gain"], "trail_alert.arm_gain") and retrace <= -decimal_value(trail["giveback"], "trail_alert.giveback"):
        flags.append("trail_alert")
    relative_loss = settings.get("relative_loss_alert")
    if isinstance(relative_loss, Mapping) and own is not None and relative is not None \
            and own <= -decimal_value(relative_loss["loss"], "relative_loss_alert.loss") \
            and relative <= -decimal_value(relative_loss["lag"], "relative_loss_alert.lag"):
        flags.append("relative_loss_alert")
    stale = settings.get("stale_alert")
    days = facts.get("holding_trading_days")
    if isinstance(stale, Mapping) and own is not None and isinstance(days, int) and days >= int(stale["days"]) and own < 0:
        flags.append("stale_alert")
    return flags


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
    with_context = settings.get("context") == CONTEXT_VERSION
    v2 = settings.get("version") == POLICY_VERSION_V2
    held_since = _held_since_dates(policy) if v2 else {}
    if v2:
        _check_bars_visible(bundle)
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
        entry: Dict[str, object] = {}
        if v2:
            entry = _entry_facts(held_since, symbol, bars, series)
            flags.extend({"review_id": review_id(symbol, flag), "flag": flag} for flag in _entry_flags(settings, entry))
        context: Dict[str, object] = {}
        if with_context:
            for window in (SHORT_RETRACE_WINDOW, RETRACE_WINDOW):
                own = _window_return(bars, window)
                pool, size = _pool_return(series, window)
                tag = "%dd" % window
                context["return_" + tag] = None if own is None else _quantize(own)
                context["pool_return_" + tag] = None if pool is None else _quantize(pool)
                context["relative_to_pool_" + tag] = None if own is None or pool is None else _quantize(own - pool)
                if window == RETRACE_WINDOW:
                    context["pool_size"] = size
            context["latest_revenue_available_at"] = _latest_available(bundle, symbol, "monthly_revenue")
            context["latest_financial_available_at"] = _latest_available(bundle, symbol, "financial_statement")
        items.append({
            **context,
            **entry,
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
        "policy": {key: settings.get(key) for key in sorted(POLICY_FIELDS_V2 if v2 else POLICY_FIELDS) if key != "context" or "context" in settings},
        "cost_basis_note": COST_BASIS_NOTE_V2 if v2 else COST_BASIS_NOTE,
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
    v2 = review["policy"].get("version") == POLICY_VERSION_V2
    brief: Dict[str, object] = {
        "policy": review["policy"],
        "note": review["cost_basis_note"],
        "by_symbol": {
            str(item["symbol"]): {key: item[key] for key in (
                "position_id", "average_cost", "price", "unrealized_return", "retrace_from_high_5d", "retrace_from_high_20d", "flags", "evidence_ids",
                *sorted(CONTEXT_ITEM_FIELDS & set(item)),
                *sorted(ENTRY_ITEM_FIELDS & set(item)),
            )}
            for item in review["items"]
        },
    }
    if v2:
        # v2：警示多時把「必須逐一處理」的清單集中放在背景最前面（symbol → review_id 陣列，只列有警示的持股）；
        # 內容與逐股 flags 完全相同，只是方便對照，沒有新資訊。
        brief["alerts_to_handle"] = {
            symbol: [flag["review_id"] for flag in row["flags"]] for symbol, row in brief["by_symbol"].items() if row["flags"]  # type: ignore[union-attr]
        }
    return brief
