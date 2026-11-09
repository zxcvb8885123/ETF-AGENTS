"""市場轉向時的持股曝險調整（``position_sizing.exposure_rotation`` v1／v2 版本閘門，只由人設定）。

整條鏈全部是確定性程式，Agent 不輸出任何數字：

- 每檔持股對交易池等權的 beta／上行捕捉／下行捕捉（``BetaModel``），以及組合層級 ``portfolio_beta``（含現金）、
  ``invested_beta``、``low_beta_share``（``build_exposure_review``，不輸出任何市場狀態或轉向判斷）；
- 持有天數（``bind_holding_days``）：由帳本逐狀態的持股重算「連續持有起點」，綁進 Policy，Finalizer 以帳本獨立重算；
- 換股配對（``exposure_rotation_plan``）：純函式，``AllocationOrderEngine`` 與測試共用；Finalizer 以整份配置重建驗證。

誰判斷方向：維持風險 Agent 的現金姿態列舉；本模組只把姿態對映成「含現金整體 beta 下限」並做有上限、有死區的換股。
v1 的下限減 ``release_margin`` 才是觸發與停止線；v2 直接寫停止線 ``beta_target_by_stance``，且沒有 ``max_rotation_fraction``
（換手由 ``turnover_fit`` 把關）與自己的 ``min_new_conviction``（沿用 ``rotation`` 的）。
交易 Agent 的意圖不被改寫：換出者在訂單意圖標 ``exit``，原因只留在旗標欄位。沒有 ``exposure_rotation`` 欄位的舊 Policy
完全不經過本模組。
"""

from __future__ import annotations

import copy
from datetime import timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from etf_agent.core import parse_aware_time

from .fields import DecisionToolError, decimal_value, reject_unknown_fields, snapshot_execution_price

EXPOSURE_ROTATION = "v1"
# v2（docs/exposure_rotation_v2_plan.md）：刪 max_rotation_fraction 與 min_new_conviction（沿用 rotation 的）、
# release_margin 併進停止線 beta_target_by_stance；v1 路徑逐位元保留，沒有 v2 的舊 Policy／舊 run 行為不變。
EXPOSURE_ROTATION_V2 = "v2"
EXPOSURE_ROTATION_VERSIONS = (EXPOSURE_ROTATION, EXPOSURE_ROTATION_V2)
HOLD_CONVICTION = "v1"
CONVICTION_GAP_SWAP = "v1"
EXPOSURE_REVIEW_VERSION = "1.0"
CONVICTION_LEVELS = ("high", "medium", "low")
STANCES = ("aggressive", "neutral", "defensive")
# 人與策略反思都不得改：欄位白名單。held_since_by_symbol 由每日 pipeline 綁定（不得預填）。
GATE_REQUIRED_FIELDS = {
    "version", "beta_window_days", "beta_floor_by_stance", "release_margin", "max_swaps_per_run",
    "max_rotation_fraction", "min_holding_days", "min_new_conviction", "hold_conviction",
}
GATE_FIELDS = GATE_REQUIRED_FIELDS | {"conviction_gap_swap", "held_since_by_symbol"}
GATE_REQUIRED_FIELDS_V2 = {
    "version", "beta_window_days", "beta_target_by_stance", "max_swaps_per_run", "min_holding_days", "hold_conviction",
}
GATE_FIELDS_V2 = GATE_REQUIRED_FIELDS_V2 | {"conviction_gap_swap", "held_since_by_symbol"}
# 前提：position_sizing 內的欄位由 sizing.GATE_PREREQUISITES 檢查；position_review 在 Policy 頂層，由 DecisionPolicyValidator 檢查。
POLICY_PREREQUISITE_FIELDS = ("position_review",)
LOW_BETA_THRESHOLD = Decimal("0.3")  # 統計用事實門檻（low_beta_share），不是策略旋鈕
QUANTUM = Decimal("0.000001")
TAIPEI = timezone(timedelta(hours=8))
REVIEW_FIELDS = {"schema_version", "policy", "note", "pool", "portfolio", "items"}
ITEM_FIELDS = {"symbol", "weight", "beta", "up_capture", "down_capture"}
PORTFOLIO_FIELDS = {"portfolio_beta", "invested_beta", "low_beta_share", "low_beta_threshold", "cash_weight", "uncovered_symbols"}
REVIEW_NOTE = (
    "beta 與上行／下行捕捉都是對交易池等權的歷史事實（缺資料為 null，不補值），不是市場方向判斷，也不是賣出指令；"
    "市場參與度低本身不是賣出理由。續抱時請依你對這檔的信心填 hold 的 conviction（定義見 data-rules.md），"
    "這個背景沒有 evidence_id，不可引用。"
)
SKIP_PORTFOLIO_SYMBOL = "PORTFOLIO"  # 組合層級旗標的 symbol 欄位（旗標一律有 symbol，渲染端依它分組）


def exposure_rotation_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """exposure_rotation 啟用時回傳閘門物件；沒有閘門回 None（欄位白名單與舊版完全相同）。"""
    gate = sizing.get("exposure_rotation") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def gate_is_v2(gate: Optional[Mapping[str, object]]) -> bool:
    return gate is not None and gate.get("version") == EXPOSURE_ROTATION_V2


def hold_conviction_enabled(policy_or_sizing: object) -> bool:
    """交易 Agent 對 hold 也必填 conviction。接受整份 Policy 或 position_sizing。"""
    if not isinstance(policy_or_sizing, Mapping):
        return False
    sizing = policy_or_sizing.get("position_sizing") if "position_sizing" in policy_or_sizing else policy_or_sizing
    gate = exposure_rotation_gate(sizing)
    return gate is not None and gate.get("hold_conviction") == HOLD_CONVICTION


def conviction_gap_swap_enabled(gate: Optional[Mapping[str, object]]) -> bool:
    return gate is not None and gate.get("conviction_gap_swap") == CONVICTION_GAP_SWAP


def _quantize(value: Decimal) -> str:
    return str(value.quantize(QUANTUM, rounding=ROUND_HALF_UP))


def _q(value: Decimal) -> Decimal:
    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- 閘門形狀

def _positive_int(value: object, label: str, errors: List[str], minimum: int = 1, maximum: int = 1000) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        errors.append("%s 必須是 %d～%d 的整數" % (label, minimum, maximum))


def _decimal_in(value: object, label: str, errors: List[str], low: Decimal, high: Decimal) -> Optional[Decimal]:
    if isinstance(value, bool) or not isinstance(value, str):
        errors.append("%s 必須是數字字串" % label)
        return None
    try:
        number = decimal_value(value, label)
    except DecisionToolError as error:
        errors.append(str(error))
        return None
    if not low < number <= high:
        errors.append("%s 必須大於 %s 且不超過 %s" % (label, low, high))
        return None
    return number


def _validate_beta_targets(gate: Mapping[str, object], prefix: str, errors: List[str]) -> None:
    """v2 的停止線 ``beta_target_by_stance``：觸發與停止都是「含現金整體 beta ≥ 目標」；防守只能 null；進攻 ≥ 中性。"""
    if "beta_target_by_stance" not in gate:
        return
    targets = gate["beta_target_by_stance"]
    label = "%s.beta_target_by_stance" % prefix
    if not isinstance(targets, Mapping) or set(targets) != set(STANCES):
        errors.append("%s 必須剛好包含 aggressive／neutral／defensive（不觸發請寫 null）" % label)
        return
    if targets["defensive"] is not None:
        errors.append("%s.defensive 只能是 null（防守姿態不觸發曝險換股）" % label)
    values: Dict[str, Decimal] = {}
    for stance in ("aggressive", "neutral"):
        if targets[stance] is not None:
            number = _decimal_in(targets[stance], "%s.%s" % (label, stance), errors, Decimal(0), Decimal("2"))
            if number is not None:
                values[stance] = number
    if "aggressive" in values and "neutral" in values and values["aggressive"] < values["neutral"]:
        errors.append("%s 必須 aggressive ≥ neutral（越進攻目標越高）" % label)


def _validate_v1_fields(gate: Mapping[str, object], sizing: Mapping[str, object], prefix: str, errors: List[str]) -> None:
    if "release_margin" in gate:
        _decimal_in(gate["release_margin"], "%s.release_margin" % prefix, errors, Decimal(0), Decimal("0.5"))
    if "max_rotation_fraction" in gate:
        _decimal_in(gate["max_rotation_fraction"], "%s.max_rotation_fraction" % prefix, errors, Decimal(0), Decimal("0.2"))
    if "min_new_conviction" in gate:
        if gate["min_new_conviction"] not in CONVICTION_LEVELS:
            errors.append("%s.min_new_conviction 必須是 %s" % (prefix, "／".join(CONVICTION_LEVELS)))
        else:
            rotation = sizing.get("rotation")
            rotation_min = rotation.get("min_new_conviction") if isinstance(rotation, Mapping) else None
            if rotation_min in CONVICTION_LEVELS and (
                CONVICTION_LEVELS.index(gate["min_new_conviction"]) > CONVICTION_LEVELS.index(rotation_min)
            ):
                errors.append(
                    "%s.min_new_conviction 必須不低於 rotation.min_new_conviction（%s）；"
                    "否則換入者會被 ROTATION_CONVICTION 擋下而變成只賣不買" % (prefix, rotation_min)
                )
    if "beta_floor_by_stance" in gate:
        floors = gate["beta_floor_by_stance"]
        label = "%s.beta_floor_by_stance" % prefix
        if not isinstance(floors, Mapping) or set(floors) != set(STANCES):
            errors.append("%s 必須剛好包含 aggressive／neutral／defensive（不觸發請寫 null）" % label)
        else:
            if floors["defensive"] is not None:
                errors.append("%s.defensive 只能是 null（防守姿態不觸發曝險換股）" % label)
            values: Dict[str, Decimal] = {}
            for stance in ("aggressive", "neutral"):
                if floors[stance] is not None:
                    number = _decimal_in(floors[stance], "%s.%s" % (label, stance), errors, Decimal(0), Decimal("2"))
                    if number is not None:
                        values[stance] = number
            if "aggressive" in values and "neutral" in values and values["aggressive"] < values["neutral"]:
                errors.append("%s 必須 aggressive ≥ neutral（越進攻下限越高）" % label)
            margin = gate.get("release_margin")
            try:
                margin_value = decimal_value(margin, "release_margin") if isinstance(margin, str) else None
            except DecisionToolError:
                margin_value = None
            if margin_value is not None and any(value <= margin_value for value in values.values()):
                errors.append("%s 的下限必須大於 release_margin（補到「下限 − margin」才停）" % label)


def validate_exposure_rotation_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """閘門形狀；其他閘門的前提由 ``GATE_PREREQUISITES``、position_review 由 DecisionPolicyValidator 檢查。"""
    gate = sizing["exposure_rotation"]
    prefix = "position_sizing.exposure_rotation"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    v2 = gate_is_v2(gate)
    reject_unknown_fields(gate, GATE_FIELDS_V2 if v2 else GATE_FIELDS, prefix, errors)
    missing = sorted((GATE_REQUIRED_FIELDS_V2 if v2 else GATE_REQUIRED_FIELDS) - set(gate))
    if missing:
        errors.append("%s 缺少必要欄位（由人設定，程式不提供預設值）：%s" % (prefix, "、".join(missing)))
    if gate.get("version") not in EXPOSURE_ROTATION_VERSIONS:
        errors.append("%s.version 只允許 %s" % (prefix, "、".join(EXPOSURE_ROTATION_VERSIONS)))
    if "hold_conviction" in gate and gate["hold_conviction"] != HOLD_CONVICTION:
        errors.append("%s.hold_conviction 必須與閘門同時出現且只允許 %s" % (prefix, HOLD_CONVICTION))
    if "conviction_gap_swap" in gate and gate["conviction_gap_swap"] != CONVICTION_GAP_SWAP:
        errors.append("%s.conviction_gap_swap 只允許省略或 %s" % (prefix, CONVICTION_GAP_SWAP))
    if "beta_window_days" in gate:
        _positive_int(gate["beta_window_days"], "%s.beta_window_days" % prefix, errors, 20, 250)
    if "max_swaps_per_run" in gate:
        _positive_int(gate["max_swaps_per_run"], "%s.max_swaps_per_run" % prefix, errors, 1, 10)
    if "min_holding_days" in gate:
        _positive_int(gate["min_holding_days"], "%s.min_holding_days" % prefix, errors, 0, 60)
    if v2:
        _validate_beta_targets(gate, prefix, errors)
    else:
        _validate_v1_fields(gate, sizing, prefix, errors)
    if "held_since_by_symbol" in gate:
        held = gate["held_since_by_symbol"]
        label = "%s.held_since_by_symbol" % prefix
        if not isinstance(held, Mapping):
            errors.append("%s 必須是物件（symbol → 連續持有起點）" % label)
        else:
            symbols = list(held)
            if symbols != sorted(symbols) or any(not isinstance(s, str) or s != s.upper() or not s for s in symbols):
                errors.append("%s 必須是依代號排序的大寫代號" % label)
            for symbol, value in held.items():
                try:
                    parse_aware_time(value, "%s.%s" % (label, symbol), error=DecisionToolError)
                except DecisionToolError as error:
                    errors.append(str(error))


# ---------------------------------------------------------------- beta 模型

def _decimal_mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


class BetaModel:
    """每檔對交易池等權日報酬的 beta／上行捕捉／下行捕捉。

    - 日期格：所有交易池行情的交易日聯集取最後 ``window + 1`` 天；少於 ``window + 1`` 天，全部不可用。
    - 個股日報酬需要該日與前一日都有收盤（調整價口徑，與持股檢視相同）；窗內每一天都有才算有覆蓋，缺一天即 null、不補值。
    - 交易池日報酬是當日有報酬的全部股票的等權平均；池報酬變異為 0 時全部不可用。
    - 所有輸出量化到小數 6 位；比較一律用量化後的值，審查（review）與配置（plan）才會逐位一致。
    """

    def __init__(self, bundle: Mapping[str, object], window: int):
        self.window = window
        closes: Dict[str, Dict[str, Decimal]] = {}
        for row in bundle.get("price_series", []):
            if not isinstance(row, Mapping):
                continue
            symbol = str(row["symbol"]).upper()
            closes[symbol] = {str(bar["trade_date"]): decimal_value(bar["close"], "%s.close" % symbol) for bar in row["bars"]}
        dates = sorted({date for bars in closes.values() for date in bars})
        self.dates: List[str] = dates[-(window + 1):] if len(dates) >= window + 1 else []
        self.pool_size = len(closes)
        self._returns: Dict[str, List[Optional[Decimal]]] = {}
        self.pool_returns: List[Decimal] = []
        self.available = False
        if not self.dates:
            return
        for symbol, bars in closes.items():
            row: List[Optional[Decimal]] = []
            for previous, current in zip(self.dates, self.dates[1:]):
                start, end = bars.get(previous), bars.get(current)
                row.append(None if start is None or end is None or start <= 0 else end / start - Decimal(1))
            self._returns[symbol] = row
        for index in range(window):
            day = [row[index] for row in self._returns.values() if row[index] is not None]
            if not day:
                self.pool_returns = []
                return
            self.pool_returns.append(_decimal_mean(day))
        mean = _decimal_mean(self.pool_returns)
        self._variance_sum = sum(((value - mean) ** 2 for value in self.pool_returns), Decimal(0))
        self._pool_mean = mean
        self.available = self._variance_sum > 0
        self._cache: Dict[str, Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]] = {}

    def stats(self, symbol: str) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
        """(beta, up_capture, down_capture)；覆蓋不足或無法計算者為 None。"""
        symbol = symbol.upper()
        if not self.available:
            return None, None, None
        if symbol in self._cache:
            return self._cache[symbol]
        row = self._returns.get(symbol)
        if row is None or any(value is None for value in row):
            result: Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]] = (None, None, None)
        else:
            own = [value for value in row if value is not None]
            own_mean = _decimal_mean(own)
            covariance = sum(
                ((a - own_mean) * (b - self._pool_mean) for a, b in zip(own, self.pool_returns)), Decimal(0)
            )
            beta = _q(covariance / self._variance_sum)
            result = (beta, self._capture(own, positive=True), self._capture(own, positive=False))
        self._cache[symbol] = result
        return result

    def beta(self, symbol: str) -> Optional[Decimal]:
        return self.stats(symbol)[0]

    def _capture(self, own: Sequence[Decimal], positive: bool) -> Optional[Decimal]:
        pairs = [(a, b) for a, b in zip(own, self.pool_returns) if (b > 0 if positive else b < 0)]
        if not pairs:
            return None
        pool_mean = _decimal_mean([b for _, b in pairs])
        return _q(_decimal_mean([a for a, _ in pairs]) / pool_mean)


def beta_model(bundle: Mapping[str, object], gate: Mapping[str, object]) -> BetaModel:
    return BetaModel(bundle, int(gate["beta_window_days"]))


# ---------------------------------------------------------------- 持股權重與組合 beta

def holding_weights(bundle: Mapping[str, object]) -> Tuple[Dict[str, Decimal], Decimal, Decimal]:
    """(持股權重, 標價 NAV, 現金權重)。公式與 ``AllocationOrderEngine`` 相同：NAV = 帳戶現金 + Σ 股數 × 決策用價。"""
    account = bundle["account_snapshot"]
    latest = {str(row["symbol"]).upper(): row for row in bundle["snapshot"]["latest_prices"]}
    cash = decimal_value(account["cash"], "account_snapshot.cash")
    values: Dict[str, Decimal] = {}
    for position in account["positions"]:
        symbol = str(position["symbol"]).upper()
        shares = int(position.get("shares", 0))
        if shares <= 0:
            continue
        if symbol not in latest:
            raise DecisionToolError("持股 %s 缺少同 Snapshot 的行情，無法計算曝險" % symbol)
        values[symbol] = Decimal(shares) * snapshot_execution_price(latest[symbol], symbol)
    nav = cash + sum(values.values(), Decimal(0))
    if nav <= 0:
        raise DecisionToolError("依 cutoff 行情重算的 NAV 必須大於 0")
    return {symbol: value / nav for symbol, value in values.items()}, nav, cash / nav


def portfolio_beta(weights: Mapping[str, Decimal], betas: Mapping[str, Optional[Decimal]]) -> Optional[Decimal]:
    """含現金的整體 beta（現金 beta 為 0）；任一持股 beta 缺值回 None，絕不當成 0。"""
    total = Decimal(0)
    for symbol, weight in weights.items():
        beta = betas.get(symbol)
        if beta is None:
            return None
        total += weight * beta
    return _q(total)


def build_exposure_review(bundle: Mapping[str, object], policy: Mapping[str, object]) -> Dict[str, object]:
    """逐檔持股 beta／捕捉與組合層級 beta；只讀 policy 的 ``beta_window_days``，基礎與最終 policy 建出的結果相同。"""
    gate = exposure_rotation_gate(policy.get("position_sizing"))
    if gate is None:
        raise DecisionToolError("DecisionPolicy 沒有 position_sizing.exposure_rotation，不應建立曝險檢視")
    errors: List[str] = []
    validate_exposure_rotation_gate(policy["position_sizing"], errors)  # type: ignore[index]
    if errors:
        raise DecisionToolError("；".join(errors))
    model = beta_model(bundle, gate)
    weights, _, cash_weight = holding_weights(bundle)
    stats = {symbol: model.stats(symbol) for symbol in weights}
    betas = {symbol: value[0] for symbol, value in stats.items()}
    total = portfolio_beta(weights, betas)
    uncovered = sorted(symbol for symbol, beta in betas.items() if beta is None)
    invested = Decimal(1) - cash_weight
    portfolio: Dict[str, object] = {
        "portfolio_beta": None if total is None else _quantize(total),
        "invested_beta": None if total is None or invested <= 0 else _quantize(total / invested),
        "low_beta_share": None if total is None else _quantize(
            sum((weights[s] for s, b in betas.items() if b is not None and b < LOW_BETA_THRESHOLD), Decimal(0))
        ),
        "low_beta_threshold": str(LOW_BETA_THRESHOLD),
        "cash_weight": _quantize(cash_weight),
        "uncovered_symbols": uncovered,
    }
    return {
        "schema_version": EXPOSURE_REVIEW_VERSION,
        "policy": {"version": gate["version"], "beta_window_days": gate["beta_window_days"]},
        "note": REVIEW_NOTE,
        "pool": {
            "size": model.pool_size,
            "return_days": len(model.pool_returns),
            "first_date": model.dates[0] if model.dates else None,
            "last_date": model.dates[-1] if model.dates else None,
        },
        "portfolio": portfolio,
        "items": [
            {
                "symbol": symbol,
                "weight": _quantize(weights[symbol]),
                "beta": None if stats[symbol][0] is None else _quantize(stats[symbol][0]),
                "up_capture": None if stats[symbol][1] is None else _quantize(stats[symbol][1]),
                "down_capture": None if stats[symbol][2] is None else _quantize(stats[symbol][2]),
            }
            for symbol in sorted(weights)
        ],
    }


def validate_exposure_review(
    bundle: Mapping[str, object], policy: Mapping[str, object], review: object, errors: List[str],
) -> None:
    """以同一輸入重算整份曝險檢視並逐欄比對；不一致即拒絕（fail-closed）。"""
    prefix = "team_inputs.exposure_review"
    if not isinstance(review, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(review, REVIEW_FIELDS, prefix, errors)
    try:
        expected = build_exposure_review(bundle, policy)
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        errors.append("%s 無法以共同輸入重算：%s" % (prefix, error))
        return
    if dict(review) != expected:
        errors.append("%s 與重算結果不同，拒絕改寫" % prefix)


def exposure_review_brief(review: Mapping[str, object]) -> Dict[str, object]:
    """給交易 Agent 的背景：整體說明加逐股數字；沒有 evidence_id（``citable=false``），不可引用。"""
    return {
        "background": {
            "citable": False, "policy": review["policy"], "note": review["note"], "pool": review["pool"],
            "portfolio": review["portfolio"],
        },
        "by_symbol": {
            str(item["symbol"]): {key: item[key] for key in ("weight", "beta", "up_capture", "down_capture")}
            for item in review["items"]
        },
    }


# ---------------------------------------------------------------- 持有天數（帳本）

def held_since_from_history(
    rows: Sequence[Mapping[str, object]], held_symbols: Sequence[str],
) -> Dict[str, str]:
    """由帳本逐狀態的持股（舊到新）算每檔「連續持有起點」：從最新狀態往回走，到該檔第一次不在持股為止。

    最新一筆的持股必須剛好等於帳戶持股；任何不符（帳本與帳戶不同步）一律拒絕，不猜起點。"""
    if not rows:
        raise DecisionToolError("帳本持股歷史是空的，無法計算持有天數")
    held = sorted({str(symbol).upper() for symbol in held_symbols})
    head_symbols = sorted({str(symbol).upper() for symbol in rows[-1].get("symbols", [])})
    if head_symbols != held:
        raise DecisionToolError("帳本最新狀態的持股與帳戶持股不一致，無法計算持有天數")
    result: Dict[str, str] = {}
    for symbol in held:
        start: Optional[str] = None
        for row in reversed(rows):
            if symbol not in {str(s).upper() for s in row.get("symbols", [])}:
                break
            start = str(row["as_of"])
        if start is None:
            raise DecisionToolError("帳本找不到 %s 的持有起點" % symbol)
        result[symbol] = start
    return result


def _history_head(policy: Mapping[str, object]) -> str:
    control = policy.get("drawdown_control")
    ref = control.get("history_ref") if isinstance(control, Mapping) else None
    head = ref.get("head_run_id") if isinstance(ref, Mapping) else None
    if not isinstance(head, str) or not head:
        raise DecisionToolError(
            "position_sizing.exposure_rotation 需要 drawdown_control.history_ref.head_run_id（帳本歷史的錨點），但 Policy 沒有"
        )
    return head


def bind_holding_days(
    policy: Mapping[str, object], bundle: Mapping[str, object],
    holdings_history: Optional[Sequence[Mapping[str, object]]],
    nav_history: Optional[Sequence[Mapping[str, object]]] = None,
) -> Dict[str, object]:
    """樣板啟用 ``exposure_rotation`` 時把每檔連續持有起點綁進 Policy；沒有閘門原樣回傳（呼叫端須重算 content_sha256）。

    樣板預填 ``held_since_by_symbol`` 即拒絕（只能由程式綁定）；沒有帳本歷史、帳本與帳戶不同步或最新一筆不是
    drawdown_control.history_ref.head_run_id 一律拒絕（fail-closed，不綁空名單）。"""
    bound = copy.deepcopy(dict(policy))
    sizing = bound.get("position_sizing")
    gate = sizing.get("exposure_rotation") if isinstance(sizing, dict) else None
    if not isinstance(gate, dict):
        return bound
    if "held_since_by_symbol" in gate:
        raise DecisionToolError("position_sizing.exposure_rotation.held_since_by_symbol 只能由程式綁定，樣板不得預填")
    if holdings_history is None:
        raise DecisionToolError("position_sizing.exposure_rotation 需要帳本持股歷史計算持有天數，但呼叫端未提供")
    rows = [dict(row) for row in holdings_history]
    if not rows or rows[-1].get("run_id") != _history_head(bound):
        raise DecisionToolError("帳本持股歷史的最新一筆不是 drawdown_control.history_ref.head_run_id")
    _check_history_times(rows, bundle)
    _check_same_chain(rows, nav_history)
    held = [str(p["symbol"]).upper() for p in bundle["account_snapshot"]["positions"] if int(p.get("shares", 0)) > 0]
    gate["held_since_by_symbol"] = held_since_from_history(rows, held)
    return bound


def _check_same_chain(rows: Sequence[Mapping[str, object]], nav_rows: Optional[Sequence[Mapping[str, object]]]) -> None:
    """持股歷史必須與 NAV 歷史是同一條帳本鏈：逐列比對 run_id 與 as_of（筆數、順序都要一致）。"""
    if nav_rows is None:
        return
    held = [(str(r.get("run_id")), str(r.get("as_of"))) for r in rows]
    nav = [(str(r.get("run_id")), str(r.get("as_of"))) for r in nav_rows]
    if held != nav:
        raise DecisionToolError("帳本持股歷史與 NAV 歷史不是同一條鏈（run_id／as_of／筆數不一致）")


def _check_history_times(rows: Sequence[Mapping[str, object]], bundle: Mapping[str, object]) -> None:
    cutoff = parse_aware_time(bundle.get("decision_cutoff"), "decision_cutoff", error=DecisionToolError)
    for index, row in enumerate(rows):
        as_of = parse_aware_time(row.get("as_of"), "holdings_history[%d].as_of" % index, error=DecisionToolError)
        if as_of > cutoff:
            raise DecisionToolError("帳本持股歷史第 %d 列的時間晚於 decision_cutoff" % index)


def validate_held_since(
    policy: Mapping[str, object], bundle: Mapping[str, object],
    provider: Optional[Callable[[str], Sequence[Mapping[str, object]]]], errors: List[str],
    nav_provider: Optional[Callable[[str], Sequence[Mapping[str, object]]]] = None,
) -> None:
    """Finalizer：以帳本獨立重算 ``held_since_by_symbol``；缺帳本來源、讀不到、歷史不符或與重算不同一律拒絕。"""
    gate = exposure_rotation_gate(policy.get("position_sizing"))
    if gate is None:
        return
    prefix = "DecisionPolicy.position_sizing.exposure_rotation.held_since_by_symbol"
    if "held_since_by_symbol" not in gate:
        errors.append("%s 尚未綁定（持有天數必須由程式依帳本綁定）" % prefix)
        return
    try:
        head = _history_head(policy)
    except DecisionToolError as error:
        errors.append(str(error))
        return
    if provider is None:
        errors.append("%s 需要帳本持股歷史才能重算，但呼叫端未提供帳本來源" % prefix)
        return
    try:
        rows = [dict(row) for row in provider(head)]
        if not rows or rows[-1].get("run_id") != head:
            raise DecisionToolError("帳本持股歷史的最新一筆不是 head_run_id=%s" % head)
        _check_history_times(rows, bundle)
        _check_same_chain(rows, list(nav_provider(head)) if nav_provider is not None else None)
        held = [str(p["symbol"]).upper() for p in bundle["account_snapshot"]["positions"] if int(p.get("shares", 0)) > 0]
        expected = held_since_from_history(rows, held)
    except Exception as error:  # 帳本缺 head、雜湊鏈損毀、與帳戶不同步一律拒絕
        errors.append("%s 無法由帳本重算（head_run_id=%s）：%s" % (prefix, head, error))
        return
    if dict(gate["held_since_by_symbol"]) != expected:
        errors.append("%s 與帳本重算結果不一致" % prefix)


def holding_days_by_symbol(policy: Mapping[str, object], bundle: Mapping[str, object]) -> Dict[str, int]:
    """每檔持有的日曆天數：台北日期的 decision_cutoff 減去連續持有起點；Policy 未綁定或與帳戶持股不符即拒絕。"""
    gate = exposure_rotation_gate(policy.get("position_sizing"))
    if gate is None or "held_since_by_symbol" not in gate:
        raise DecisionToolError("position_sizing.exposure_rotation 尚未綁定 held_since_by_symbol（持有天數）")
    held = sorted(str(p["symbol"]).upper() for p in bundle["account_snapshot"]["positions"] if int(p.get("shares", 0)) > 0)
    if sorted(gate["held_since_by_symbol"]) != held:
        raise DecisionToolError("held_since_by_symbol 與帳戶持股不一致")
    cutoff = parse_aware_time(bundle.get("decision_cutoff"), "decision_cutoff", error=DecisionToolError)
    today = cutoff.astimezone(TAIPEI).date()
    days: Dict[str, int] = {}
    for symbol, since in gate["held_since_by_symbol"].items():
        moment = parse_aware_time(since, "held_since_by_symbol.%s" % symbol, error=DecisionToolError)
        days[symbol] = (today - moment.astimezone(TAIPEI).date()).days
    return days


# ---------------------------------------------------------------- 換股配對（純函式）

class Victim(NamedTuple):
    """候選換出者（持股、交易 Agent 標 hold、可交易、持有夠久、未被留住）。"""
    symbol: str
    conviction: str
    beta: Optional[Decimal]
    weight: Decimal
    value: Decimal  # 預估賣出市值


class Fill(NamedTuple):
    """候選換入者（本次 buy、未持有、被名額擋下）；呼叫端依既有入選排序給順序。"""
    symbol: str
    conviction: str
    beta: Optional[Decimal]


def exposure_rotation_plan(
    gate: Mapping[str, object],
    stance: object,
    portfolio_beta_value: Optional[Decimal],
    nav: Decimal,
    victims: Sequence[Victim],
    fills: Sequence[Fill],
    afford: Optional[Callable[[str, str], bool]] = None,
    min_new_conviction: Optional[str] = None,
) -> Dict[str, object]:
    """換股配對（純函式）。回傳 ``{"swaps": [{"kind", "victim", "fill"}...], "skipped": {"exposure": 原因|None, "gap": 原因|None}}``。

    曝險換股（全部成立才換）：姿態有非 null 下限且 ``portfolio_beta`` 低於它；beta 低於「下限 − release_margin」才補；
    換入者 beta ≥ 組合 beta + release_margin、信心 ≥ min_new_conviction；換出者非 high 信心且 beta 低於組合 beta；
    順序：信心 low 優先、beta 由低到高、代號；換入沿用呼叫端給的入選排序。逐對配對直到：達 ``max_swaps_per_run``、
    累計賣出市值再加一對就超過 ``max_rotation_fraction × NAV``（嚴格讀法：不加會超過的那一對）、候選用完、
    預估換後 beta 達「下限 − release_margin」。預估換後 beta = 目前 + 換出者權重 × (換入 beta − 換出 beta)，換入沿用換出者的賣出金額。

    信心差換股（``conviction_gap_swap`` 有開才做，不看姿態與 beta）：新股 high 對持股 low，沿用同一組上限（與曝險換股共用
    ``max_swaps_per_run`` 與賣出市值上限），曝險換股先。``afford(victim, fill)`` 是呼叫端的現金可行性檢查（有狀態，回 True 即已預扣）。
    """
    v2 = gate_is_v2(gate)
    max_swaps = int(gate["max_swaps_per_run"])
    if v2:
        # v2：沒有賣出市值比例上限（換手由 turnover_fit 把關）、沒有 release_margin；換入信心門檻沿用 rotation 的，由呼叫端傳入。
        if min_new_conviction not in CONVICTION_LEVELS:
            raise DecisionToolError("exposure_rotation v2 需要呼叫端傳入 rotation.min_new_conviction")
        cap: Optional[Decimal] = None
        margin = Decimal(0)
        min_tier = CONVICTION_LEVELS.index(str(min_new_conviction))
    else:
        cap = decimal_value(gate["max_rotation_fraction"], "max_rotation_fraction") * nav
        margin = decimal_value(gate["release_margin"], "release_margin")
        min_tier = CONVICTION_LEVELS.index(str(gate["min_new_conviction"]))
    swaps: List[Dict[str, str]] = []
    skipped: Dict[str, Optional[str]] = {"exposure": None, "gap": None}
    used_victims: Set[str] = set()
    used_fills: Set[str] = set()
    sold = Decimal(0)

    def take(kind: str, victim_pool: List[Victim], fill_pool: List[Fill], stop: Callable[[], bool]) -> Optional[str]:
        """依序配對；回傳沒有配到任何一對時的原因（至少配到一對回 None）。"""
        nonlocal sold
        made = 0
        attempted_fills: Set[str] = set()
        reason = "no_eligible_victim" if not victim_pool else "no_eligible_fill" if not fill_pool else "cash_infeasible"
        while not stop():
            if len(swaps) >= max_swaps:
                reason = "max_swaps" if not made else reason
                break
            fill = next((f for f in fill_pool if f.symbol not in used_fills | attempted_fills), None)
            if fill is None:
                if not attempted_fills and not made:
                    reason = "no_eligible_fill"
                break
            chosen: Optional[Victim] = None
            blocked = "no_eligible_victim"
            for victim in victim_pool:
                if victim.symbol in used_victims:
                    continue
                if cap is not None and sold + victim.value > cap:
                    blocked = "rotation_fraction"
                    continue
                if afford is not None and not afford(victim.symbol, fill.symbol):
                    blocked = "cash_infeasible"
                    continue
                chosen = victim
                break
            if chosen is None:
                reason = blocked if not made else reason
                attempted_fills.add(fill.symbol)
                continue
            used_victims.add(chosen.symbol)
            used_fills.add(fill.symbol)
            sold += chosen.value
            swaps.append({"kind": kind, "victim": chosen.symbol, "fill": fill.symbol})
            made += 1
            if kind == "exposure" and chosen.beta is not None and fill.beta is not None:
                state["beta"] = state["beta"] + chosen.weight * (fill.beta - chosen.beta)
        return None if made else reason

    state: Dict[str, Decimal] = {"beta": portfolio_beta_value if portfolio_beta_value is not None else Decimal(0)}
    floors = gate["beta_target_by_stance"] if v2 else gate["beta_floor_by_stance"]
    floor_raw = floors.get(str(stance)) if isinstance(floors, Mapping) else None  # type: ignore[union-attr]
    if floor_raw is None:
        skipped["exposure"] = "stance_not_triggered"
    elif portfolio_beta_value is None:
        skipped["exposure"] = "beta_unavailable"
    else:
        floor = decimal_value(floor_raw, "beta_target_by_stance" if v2 else "beta_floor_by_stance")
        target = floor if v2 else floor - margin
        if portfolio_beta_value >= floor:
            skipped["exposure"] = "beta_at_target"
        elif portfolio_beta_value >= target:
            skipped["exposure"] = "beta_within_release_margin"
        else:
            victim_pool = sorted(
                (v for v in victims if v.conviction != "high" and v.beta is not None and v.beta < portfolio_beta_value),
                key=lambda v: (0 if v.conviction == "low" else 1, v.beta, v.symbol),
            )
            fill_pool = [
                f for f in fills
                if f.beta is not None and (f.beta > portfolio_beta_value if v2 else f.beta >= portfolio_beta_value + margin)
                and CONVICTION_LEVELS.index(f.conviction) <= min_tier
            ]
            skipped["exposure"] = take("exposure", victim_pool, fill_pool, lambda: state["beta"] >= target)
    if conviction_gap_swap_enabled(gate):
        victim_pool = sorted((v for v in victims if v.conviction == "low"), key=lambda v: (v.symbol,))
        fill_pool = [f for f in fills if f.conviction == "high"]
        skipped["gap"] = take("gap", victim_pool, fill_pool, lambda: False)
    return {"swaps": swaps, "skipped": skipped}
