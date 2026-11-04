"""Deterministic portfolio allocation, order, fee, and cash calculations."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .contracts import (
    DECISION_SCHEMA_VERSION,
    DecisionContext,
    DecisionToolError,
    artifact_content_sha256,
    canonical_sha256,
    decimal_value,
    snapshot_execution_price,
    intent_artifact_id,
    parse_time,
    reject_unknown_fields,
    required_string,
)
from .active_share import active_share, top_weights
from .drawdown import validate_drawdown_control
from .position_review import validate_position_review_policy
from .momentum import MomentumEngine
from .sizing import (
    BUY_GAP_RESERVE, CASH_BUFFER_FLOOR, CASH_CEILING_FIX, CASH_FIT_ORDER, CONVICTION_LEVELS, FORCED_SWAP_CONFIRMATION,
    LIMIT_HEADROOM, LIMIT_HEADROOM_BUY, SECTOR_FIT, SIZING_METHOD, STOCK_LIMIT_HEADROOM, TURNOVER_FIT, breadth_target,
    buy_gap_reserve_rate, cash_fit_min_fill_ratio, conviction_weights, drawdown_derisk_fraction, drawdown_derisk_v2, drawdown_derisk_v3, reentry_blocked_symbols,
    validate_position_sizing,
)


ALLOCATION_ENGINE_VERSION = "1.0.0"
# Active Share 低於門檻時，程式最多取消幾檔重疊的買進候選後重新配置。
ACTIVE_SHARE_MAX_DROPS = 3
MONEY = Decimal("1")
PRICE = Decimal("0.01")
RATE = Decimal("0.00000001")


def _money(value: Decimal) -> str:
    return str(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def _rate(value: Decimal) -> str:
    return str(value.quantize(RATE, rounding=ROUND_HALF_UP))


def _price(value: Decimal) -> str:
    return str(value.quantize(PRICE, rounding=ROUND_HALF_UP))


def decision_policy_sha256(policy: Mapping[str, object]) -> str:
    payload = dict(policy)
    payload.pop("content_sha256", None)
    return canonical_sha256(payload)


class DecisionPolicyValidator:
    """Validate the versioned strategy and execution assumptions."""

    FIELDS = {
        "schema_version",
        "policy_id",
        "available_at",
        "currency",
        "cash_buffer_rate",
        "default_target_weight",
        "trim_fraction",
        "commission_rate",
        "sell_tax_rate",
        "minimum_commission",
        "lot_size",
        "slippage_bps",
        "max_turnover_rate",
        "max_stock_weight",
        "special_weight_limits",
        "max_sector_weight",
        "sector_classification_version",
        "sector_by_symbol",
        "min_positions",
        "max_positions",
        "cash_weight_ceiling",
        "minimum_active_share",
        "stress_price_decline_rate",
        "stress_slippage_multiplier",
        "liquidity_fill_rate",
        "reuse_sell_proceeds",
        "max_revisions",
        "position_sizing",
        "drawdown_control",
        "position_review",
        "content_sha256",
    }

    RATE_FIELDS = {
        "cash_buffer_rate",
        "default_target_weight",
        "trim_fraction",
        "commission_rate",
        "sell_tax_rate",
        "max_turnover_rate",
        "max_stock_weight",
        "max_sector_weight",
        "cash_weight_ceiling",
        "minimum_active_share",
        "stress_price_decline_rate",
        "liquidity_fill_rate",
    }

    def __init__(self, bundle: Mapping[str, object]):
        self.context = DecisionContext(bundle)

    def validate(
        self, policy: Mapping[str, object], nav_history: Optional[Sequence[Mapping[str, object]]] = None,
    ) -> List[str]:
        errors: List[str] = []
        reject_unknown_fields(policy, self.FIELDS, "DecisionPolicy", errors)
        for field in (
            "schema_version",
            "policy_id",
            "available_at",
            "currency",
            "sector_classification_version",
            "content_sha256",
        ):
            try:
                required_string(policy, field)
            except DecisionToolError as error:
                errors.append(str(error))
        if policy.get("schema_version") != DECISION_SCHEMA_VERSION:
            errors.append("DecisionPolicy.schema_version 必須為 %s" % DECISION_SCHEMA_VERSION)
        if policy.get("currency") != "TWD":
            errors.append("DecisionPolicy.currency 目前只支援 TWD")
        if policy.get("content_sha256") != decision_policy_sha256(policy):
            errors.append("DecisionPolicy.content_sha256 與內容不一致")
        try:
            if parse_time(policy.get("available_at"), "DecisionPolicy.available_at") > parse_time(
                self.context.decision_cutoff, "decision_cutoff"
            ):
                errors.append("DecisionPolicy 晚於 decision_cutoff")
        except DecisionToolError as error:
            errors.append(str(error))
        values: Dict[str, Decimal] = {}
        for field in self.RATE_FIELDS:
            try:
                value = decimal_value(policy.get(field), "DecisionPolicy.%s" % field)
                values[field] = value
                if value < 0 or value > 1:
                    errors.append("DecisionPolicy.%s 必須介於 0 與 1" % field)
            except DecisionToolError as error:
                errors.append(str(error))
        for field in ("minimum_commission", "slippage_bps", "stress_slippage_multiplier"):
            try:
                if decimal_value(policy.get(field), "DecisionPolicy.%s" % field) < 0:
                    errors.append("DecisionPolicy.%s 不得為負" % field)
            except DecisionToolError as error:
                errors.append(str(error))
        for field in ("lot_size", "min_positions", "max_positions", "max_revisions"):
            value = policy.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append("DecisionPolicy.%s 必須是非負整數" % field)
        if policy.get("lot_size") != 1000:
            errors.append("DecisionPolicy.lot_size 必須固定為 1000 股（一張）")
        if isinstance(policy.get("max_revisions"), int) and policy.get("max_revisions") != 3:
            errors.append("DecisionPolicy.max_revisions 第一版必須為 3")
        if isinstance(policy.get("min_positions"), int) and isinstance(
            policy.get("max_positions"), int
        ) and policy["min_positions"] > policy["max_positions"]:
            errors.append("DecisionPolicy.min_positions 不得大於 max_positions")
        if policy.get("reuse_sell_proceeds") not in {True, False}:
            errors.append("DecisionPolicy.reuse_sell_proceeds 必須是布林值")
        limits = policy.get("special_weight_limits")
        if not isinstance(limits, Mapping):
            errors.append("DecisionPolicy.special_weight_limits 必須是物件")
        else:
            for symbol, raw in limits.items():
                if not isinstance(symbol, str) or symbol != symbol.upper():
                    errors.append("special_weight_limits 股票代碼必須使用大寫：%s" % symbol)
                try:
                    value = decimal_value(raw, "special_weight_limits.%s" % symbol)
                    if value <= 0 or value > 1:
                        errors.append("special_weight_limits.%s 必須大於 0 且不超過 1" % symbol)
                except DecisionToolError as error:
                    errors.append(str(error))
        sectors = policy.get("sector_by_symbol")
        if not isinstance(sectors, Mapping):
            errors.append("DecisionPolicy.sector_by_symbol 必須是物件")
        else:
            for symbol, sector in sectors.items():
                if not isinstance(symbol, str) or symbol != symbol.upper():
                    errors.append("sector_by_symbol 股票代碼必須使用大寫：%s" % symbol)
                if str(symbol).upper() not in self.context.universe:
                    errors.append("sector_by_symbol 含交易池外股票：%s" % symbol)
                if not isinstance(sector, str) or not sector.strip():
                    errors.append("sector_by_symbol.%s 必須是非空字串" % symbol)
        if "position_sizing" in policy:
            validate_position_sizing(policy, self.context.universe, errors)
        if "drawdown_control" in policy:
            validate_drawdown_control(policy, self.context.bundle, errors, nav_history)
        if "position_review" in policy:
            validate_position_review_policy(policy["position_review"], errors)
        if values.get("cash_buffer_rate", Decimal("0")) >= Decimal("1"):
            errors.append("DecisionPolicy.cash_buffer_rate 必須小於 1")
        rules = self.context.bundle.get("rules", {})
        hard_bindings = {
            "lot_size": "lot_size",
            "commission_rate": "commission_rate",
            "sell_tax_rate": "sell_tax_rate",
            "minimum_commission": "minimum_commission",
            "max_stock_weight": "max_stock_weight",
            "special_weight_limits": "special_weight_limits",
            "max_sector_weight": "max_sector_weight",
            "min_positions": "min_positions",
            "max_positions": "max_positions",
            "cash_weight_ceiling": "cash_weight_must_be_below",
            "minimum_active_share": "minimum_active_share",
            "reuse_sell_proceeds": "reuse_sell_proceeds",
        }
        if isinstance(rules, Mapping):
            for policy_field, rule_field in hard_bindings.items():
                left = policy.get(policy_field)
                right = rules.get(rule_field)
                if isinstance(left, Mapping) and isinstance(right, Mapping):
                    matches = dict(left) == dict(right)
                elif isinstance(left, bool) or isinstance(right, bool) or isinstance(left, int) or isinstance(right, int):
                    matches = type(left) is type(right) and left == right
                else:
                    try:
                        matches = decimal_value(left, policy_field) == decimal_value(right, rule_field)
                    except DecisionToolError:
                        matches = False
                if not matches:
                    errors.append(
                        "DecisionPolicy.%s 必須與 rules.%s 完全一致" % (policy_field, rule_field)
                    )
        return errors


class AllocationOrderEngine:
    """Turn validated trade intents into reproducible whole-share proposals."""

    def __init__(self, bundle: Mapping[str, object], policy: Mapping[str, object]):
        self.context = DecisionContext(bundle)
        errors = DecisionPolicyValidator(bundle).validate(policy)
        if errors:
            raise DecisionToolError("DecisionPolicy 驗證失敗：" + "；".join(errors))
        self.policy = dict(policy)
        self.prices = self._latest_prices()
        self._volatility: Optional[Dict[str, Decimal]] = None
        self._returns: Optional[Dict[str, Decimal]] = None

    def run(
        self,
        intent_result: Mapping[str, object],
        excluded_symbols: Optional[Set[str]] = None,
        overrides: Optional[Mapping[str, object]] = None,
        revision_count: int = 0,
        parent_proposal_id: Optional[str] = None,
    ) -> Dict[str, object]:
        """配置後任一必備主動型 ETF 的 Active Share 低於門檻時，由程式取消與最低那檔 ETF 重疊最多的
        buy／add 候選（重疊量 min(組合權重, ETF 權重) 由大到小、同值依代號），重新配置，最多 ACTIVE_SHARE_MAX_DROPS 次；
        每取消一檔記 RISK_ACTIVE_SHARE 旗標，不改寫交易 Agent 的意圖，仍不通過時由 Guard 拒絕。Validator 以同一方法重算。"""
        dropped: Dict[str, str] = {}
        while True:
            body = self._run_min_positions(
                intent_result, excluded_symbols, overrides, revision_count, parent_proposal_id, dropped
            )
            if len(dropped) >= ACTIVE_SHARE_MAX_DROPS:
                return body
            pick = self._active_share_drop(body, intent_result, dropped)
            if pick is None:
                return body
            dropped[pick[0]] = pick[1]

    def _active_share_drop(
        self, body: Mapping[str, object], intent_result: Mapping[str, object], dropped: Mapping[str, str]
    ) -> Optional[Tuple[str, str]]:
        required = set(self.context.bundle["rules"].get("required_benchmark_ids", []))
        minimum = decimal_value(self.policy["minimum_active_share"], "minimum_active_share")
        if not required or minimum <= 0:
            return None
        weights = top_weights({
            str(item["symbol"]).upper(): decimal_value(item["weight"], "weight")
            for item in body["allocation_proposal"]["positions"]
        })
        droppable = {
            str(item.get("symbol", "")).upper()
            for item in intent_result.get("items", [])
            if isinstance(item, Mapping) and item.get("intent") in {"buy", "add"}
        } - set(dropped)
        failing = []
        for benchmark in self.context.bundle.get("benchmarks", []):
            if benchmark["benchmark_id"] not in required:
                continue
            reference = top_weights({
                str(symbol).upper(): decimal_value(value, "benchmark weight")
                for symbol, value in benchmark["weights"].items()
            })
            share = active_share(weights, reference)
            if share < minimum:
                failing.append((share, str(benchmark["benchmark_id"]), reference))
        for share, benchmark_id, reference in sorted(failing, key=lambda item: (item[0], item[1])):
            overlaps = sorted(
                (
                    (min(weights[symbol], reference[symbol]), symbol)
                    for symbol in set(weights) & set(reference)
                    if symbol in droppable
                ),
                key=lambda item: (-item[0], item[1]),
            )
            if overlaps:
                symbol = overlaps[0][1]
                return symbol, "與 %s 的 Active Share %s 低於 %s，由程式取消重疊最多的買進候選（交易 Agent 的意圖未被改寫）" % (
                    benchmark_id, format(share, ".2%"), format(minimum, ".2%"))
        return None

    def _run_min_positions(
        self,
        intent_result: Mapping[str, object],
        excluded_symbols: Optional[Set[str]],
        overrides: Optional[Mapping[str, object]],
        revision_count: int,
        parent_proposal_id: Optional[str],
        dropped: Mapping[str, str],
    ) -> Dict[str, object]:
        """配置後持股低於 min_positions 時，由程式依 ATR14 由低到高（同值依代號）留住交易 Agent 標為
        exit 的持股續抱，直到達標或沒有可留的 exit；forced_exit 不留。每留一檔記 RISK_MIN_POSITIONS 旗標，
        不改寫交易 Agent 的意圖。Validator 以同一方法重算，結果確定。

        position_sizing.cash_ceiling_fix（版本閘門）啟用時，達標後再檢查 base 情境期末現金比例：達競賽上限
        （cash_weight_ceiling）就以同一套順序再多留一檔 exit（RISK_CASH_CEILING），直到低於上限或沒有可留的
        exit。兩個條件共用同一個 kept 集合，每輪至少多留一檔，持股有限所以必定收斂；省略閘門的舊 run 不檢查現金。"""
        kept: Dict[str, str] = {}
        minimum = int(self.policy["min_positions"])
        sizing = self.policy.get("position_sizing") or {}
        ceiling_fix = sizing.get("cash_ceiling_fix") == CASH_CEILING_FIX
        turnover_fit = sizing.get("turnover_fit") == TURNOVER_FIT
        ceiling = decimal_value(self.policy["cash_weight_ceiling"], "cash_weight_ceiling")
        while True:
            body = self._run(intent_result, excluded_symbols, overrides, revision_count, parent_proposal_id, kept, dropped)
            need = minimum - len(body["allocation_proposal"]["positions"])
            code = "RISK_MIN_POSITIONS"
            if need <= 0:
                if ceiling_fix and decimal_value(body["allocation_proposal"]["cash_weight"], "cash_weight") >= ceiling:
                    need, code = 1, "RISK_CASH_CEILING"
                elif turnover_fit and decimal_value(body["order_proposal"]["turnover_rate"], "turnover_rate") > decimal_value(
                    body["effective_policy"]["max_turnover_rate"], "max_turnover_rate"
                ):
                    # turnover_fit：買單已減到零仍超過換手上限，表示賣單本身就超過；再多留一檔 exit 續抱。
                    need, code = 1, "RISK_TURNOVER"
                else:
                    return body
            remaining = self._keepable_exits(intent_result, kept, body)
            if not remaining:
                return body
            kept.update({symbol: code for symbol in remaining[:need]})

    def _keepable_exits(
        self, intent_result: Mapping[str, object], kept: Mapping[str, str], body: Optional[Mapping[str, object]] = None,
    ) -> List[str]:
        """可由程式留住續抱的 exit 持股，依 ATR14 由低到高（同值依代號）；forced_exit、不可交易與已留住者不在內。

        forced_swap.confirmation 啟用時，本輪被程式強制換出的持股（RISK_FORCED_SWAP）也可被留住：
        換入的新股若因現金／換手被取消，持股數仍要達 min_positions。"""
        held = {str(item["symbol"]).upper() for item in self.context.bundle["account_snapshot"]["positions"]}
        untradable = self._untradable()
        atr = self._atr_pct()
        exits = {
            str(item.get("symbol", "")).upper()
            for item in intent_result.get("items", [])
            if isinstance(item, Mapping) and item.get("intent") == "exit"
        }
        if body is not None and self._forced_swap_confirmed():
            exits |= {
                str(flag["symbol"]).upper() for flag in body.get("constraint_flags", []) if flag.get("code") == "RISK_FORCED_SWAP"
            }
        remaining = sorted(exits, key=lambda symbol: (atr.get(symbol, Decimal("Infinity")), symbol))
        return [symbol for symbol in remaining if symbol in held and symbol not in kept and symbol not in untradable]

    def _forced_swap_confirmed(self) -> bool:
        rotation = (self.policy.get("position_sizing") or {}).get("rotation")
        forced = rotation.get("forced_swap") if isinstance(rotation, Mapping) else None
        return isinstance(forced, Mapping) and forced.get("confirmation") == FORCED_SWAP_CONFIRMATION

    def _untradable(self) -> Set[str]:
        """明確交易狀態包中非 allowed 的標的（與 CompetitionGuard 的 TRADABLE 規則同源）；沒有狀態包時回空集合。"""
        status_bundle = self.context.bundle.get("trading_status_bundle")
        assessment = self.context.bundle.get("tradability_assessment")
        if not isinstance(status_bundle, Mapping) or not isinstance(assessment, Mapping):
            return set()
        return {
            str(item.get("symbol", "")).upper()
            for item in assessment.get("symbols", [])
            if isinstance(item, Mapping) and item.get("symbol") and str(item.get("state", "unknown")) != "allowed"
        }

    def _run(
        self,
        intent_result: Mapping[str, object],
        excluded_symbols: Optional[Set[str]],
        overrides: Optional[Mapping[str, object]],
        revision_count: int,
        parent_proposal_id: Optional[str],
        kept_exits: Mapping[str, str],
        active_share_dropped: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, object]:
        """``kept_exits`` 是程式留住續抱的 exit 持股及其旗標代碼（RISK_MIN_POSITIONS／RISK_CASH_CEILING）。"""
        excluded = {value.upper() for value in (excluded_symbols or set())}
        effective = self._effective_policy(overrides or {})
        intents = {
            str(item.get("symbol", "")).upper(): str(item.get("intent", ""))
            for item in intent_result.get("items", [])
            if isinstance(item, Mapping)
        }
        account = self.context.bundle["account_snapshot"]
        current = {
            str(item["symbol"]).upper(): int(item["shares"])
            for item in account["positions"]
        }
        lot_size = int(effective["lot_size"])
        odd_lot_positions = sorted(
            symbol for symbol, shares in current.items() if shares % lot_size != 0
        )
        if odd_lot_positions:
            raise DecisionToolError(
                "帳戶持股不是整張，第一版禁止產生零股交易："
                + ", ".join(odd_lot_positions)
            )
        starting_cash = decimal_value(account["settled_cash"], "account_snapshot.settled_cash")
        starting_total_cash = decimal_value(account["cash"], "account_snapshot.cash")
        marked_nav = starting_total_cash + sum(
            Decimal(shares) * self.prices[symbol] for symbol, shares in current.items()
        )
        if marked_nav <= 0:
            raise DecisionToolError("依 cutoff 行情重算的 NAV 必須大於 0")
        target_shares = dict(current)
        trim_fraction = effective["trim_fraction"]
        default_weight = effective["default_target_weight"]
        max_positions = int(effective["max_positions"])
        constraint_flags: List[Dict[str, object]] = []
        untradable = self._untradable()

        # Exits and trims run before capital is assigned to buys.
        for symbol in sorted(current):
            intent = intents.get(symbol, "hold")
            if intent in {"exit", "forced_exit", "trim"} and symbol in untradable:
                # 受限持股無法賣出，否則 Guard 的 TRADABLE 會否決整份決策；由程式改為續抱。
                constraint_flags.append({
                    "symbol": symbol, "code": "RISK_NOT_TRADABLE",
                    "detail": "非可交易狀態，%s 無法執行，由程式續抱（交易 Agent 的意圖未被改寫）" % intent,
                })
                continue
            # forced_swap.confirmation：被程式強制換出的持股（交易 Agent 標 hold）也可被留住，同樣要記旗標。
            if symbol in kept_exits and (intent == "exit" or (intent == "hold" and self._forced_swap_confirmed())):
                if kept_exits[symbol] == "RISK_CASH_CEILING":
                    constraint_flags.append({
                        "symbol": symbol, "code": "RISK_CASH_CEILING",
                        "detail": "出場後現金比例將達 %s 的競賽上限，由程式依 ATR14 由低到高留住續抱（交易 Agent 的意圖未被改寫）"
                        % format(decimal_value(self.policy["cash_weight_ceiling"], "cash_weight_ceiling"), ".0%"),
                    })
                    continue
                if kept_exits[symbol] == "RISK_TURNOVER":
                    constraint_flags.append({
                        "symbol": symbol, "code": "RISK_TURNOVER",
                        "detail": "賣單本身已超過 %s 的換手上限，由程式依 ATR14 由低到高留住續抱（交易 Agent 的意圖未被改寫）"
                        % format(effective["max_turnover_rate"], ".0%"),
                    })
                    continue
                constraint_flags.append({
                    "symbol": symbol, "code": "RISK_MIN_POSITIONS",
                    "detail": "出場後持股將低於 %d 檔，由程式依 ATR14 由低到高留住續抱（交易 Agent 的意圖未被改寫）" % int(self.policy["min_positions"]),
                })
                continue
            if intent in {"exit", "forced_exit"}:
                target_shares[symbol] = 0
            elif intent == "trim":
                trim_shares = int(
                    (
                        Decimal(current[symbol])
                        * trim_fraction
                        / Decimal(lot_size)
                    ).to_integral_value(rounding=ROUND_FLOOR)
                ) * lot_size
                target_shares[symbol] = current[symbol] - trim_shares
                if trim_shares == 0:
                    constraint_flags.append(
                        {
                            "symbol": symbol,
                            "code": "LOT_SIZE",
                            "detail": "減碼比例不足一張，未建立零股賣單",
                        }
                    )

        active_symbols = {symbol for symbol, shares in target_shares.items() if shares > 0}
        order_intents = dict(intents)
        derisk_gate = drawdown_derisk_fraction(self.policy.get("position_sizing") or {}) is not None
        # drawdown_derisk（版本閘門）：防守姿態時由程式把持股減碼到姿態現金緩衝；不改寫交易 Agent 的意圖。
        derisk_flags = self._drawdown_derisk(
            current, target_shares, active_symbols, order_intents, intents, marked_nav, starting_total_cash,
            effective, untradable, kept_exits,
        )
        constraint_flags.extend(derisk_flags)
        candidates = [
            symbol
            for symbol in sorted(intents)
            if intents[symbol] in {"buy", "add"} and symbol not in excluded
        ]
        # drawdown_derisk v3：被減碼的持股不再是本次加碼候選，減碼後的股數就是上限；否則下面的
        # `desired = max(current, desired)` 會把交易 Agent 標 add 的同一檔加回原持股，留下減碼旗標卻沒有賣單。
        # 在名額與預算配置前就排除，減碼後的持股才會算進 held_outside、也不占換股名額。v1／v2 的舊 run 維持原重建行為。
        if drawdown_derisk_v3(self.policy.get("position_sizing") or {}):
            derisked = {str(flag["symbol"]) for flag in derisk_flags}
            for symbol in candidates:
                if symbol in derisked:
                    constraint_flags.append({
                        "symbol": symbol, "code": "RISK_DRAWDOWN",
                        "detail": "防守姿態回撤減碼後的持股不再是本次加碼候選：減碼後的股數為上限，由程式不建立買單"
                                  "（交易 Agent 的意圖未被改寫）",
                    })
            candidates = [symbol for symbol in candidates if symbol not in derisked]
        # 受限標的在分配資金前就排除，避免占用預算；否則 Guard 的 TRADABLE 會否決整份決策。
        for symbol in candidates:
            if symbol in untradable:
                constraint_flags.append(
                    {"symbol": symbol, "code": "RISK_NOT_TRADABLE", "detail": "非可交易狀態，由程式不建立買單（交易 Agent 的意圖未被改寫）"}
                )
        candidates = [symbol for symbol in candidates if symbol not in untradable]
        # reentry_cooldown（版本閘門）：最近出場的股票未持有時不得再買進；名單綁在 Policy，重建只讀 Policy。
        blocked = reentry_blocked_symbols(self.policy.get("position_sizing") or {})
        if blocked is not None:
            cooldown = self.policy["position_sizing"]["reentry_cooldown"]
            for symbol in candidates:
                if symbol in blocked and symbol not in active_symbols and current.get(symbol, 0) <= 0:
                    constraint_flags.append({
                        "symbol": symbol, "code": "RISK_REENTRY_COOLDOWN",
                        "detail": "出場後 %d 日內不得再買進（來源決策 %s），由程式不建立買單（交易 Agent 的意圖未被改寫）"
                        % (int(cooldown["days"]), "、".join(cooldown.get("source_run_ids") or []) or "無"),
                    })
            candidates = [
                symbol for symbol in candidates
                if not (symbol in blocked and symbol not in active_symbols and current.get(symbol, 0) <= 0)
            ]
        for symbol, detail in sorted((active_share_dropped or {}).items()):
            if symbol in candidates:
                constraint_flags.append({"symbol": symbol, "code": "RISK_ACTIVE_SHARE", "detail": detail})
        candidates = [symbol for symbol in candidates if symbol not in (active_share_dropped or {})]
        sized_weights: Optional[Dict[str, Decimal]] = None
        skipped: Dict[str, str] = {}
        rotation = (self.policy.get("position_sizing") or {}).get("rotation")
        budget: Optional[Decimal] = None
        confirmation = self._forced_swap_confirmed()
        adopted_bear = {
            str(item.get("symbol", "")).upper()
            for item in intent_result.get("items", [])
            if isinstance(item, Mapping)
            and any(str(claim_id).startswith("bear-") for claim_id in (item.get("adopted_claim_ids") or []))
        }
        victims, unconfirmed = self._forced_swap_victims(
            candidates, current, active_symbols, target_shares, intents, kept_exits, untradable, max_positions,
            adopted_bear, starting_cash, marked_nav, effective,
        )
        for symbol in victims:
            target_shares[symbol] = 0
            active_symbols.discard(symbol)
            order_intents[symbol] = "exit"
            constraint_flags.append({
                "symbol": symbol, "code": "RISK_FORCED_SWAP",
                "detail": (
                    "滿檔且有高信心新股沒有名額：由程式換出論點被推翻、且本次已採納該股空頭 claim 的續抱持股（依代號；"
                    "交易 Agent 的意圖未被改寫，訂單意圖標為 exit）"
                    if confirmation else
                    "滿檔且有高信心新股沒有名額：由程式換出 20 日報酬最低的續抱持股（交易 Agent 的意圖未被改寫，訂單意圖標為 exit）"
                ),
            })
        for symbol, reason in sorted(unconfirmed.items()):
            constraint_flags.append({"symbol": symbol, "code": "RISK_FORCED_SWAP_UNCONFIRMED", "detail": reason})
        if "position_sizing" in self.policy:
            candidates, sized_weights, skipped, budget = self._conviction_targets(
                candidates, active_symbols, target_shares, marked_nav, max_positions, effective,
                len(current),
            )
        for symbol in candidates:
            if symbol not in self.prices:
                constraint_flags.append(
                    {"symbol": symbol, "code": "MISSING_PRICE", "detail": "缺少 cutoff 行情"}
                )
                continue
            # 啟用 rotation 時名額已由 _conviction_targets 全權配對（換股名額可超過廣度目標），
            # 這裡只看是否入選，否則換股進來的新股會被下面的持股數上限誤擋成 MAX_POSITIONS。
            over_max = (
                rotation is None
                and symbol not in active_symbols
                and len(active_symbols) >= max_positions
            )
            if over_max or (sized_weights is not None and symbol not in sized_weights):
                constraint_flags.append(
                    {"symbol": symbol, **self._slot_flag(symbol, skipped, rotation)}
                )
                continue
            limit = self._weight_limit(symbol, effective)
            target_weight = (
                min(default_weight, limit) if sized_weights is None else sized_weights[symbol]
            )
            desired = self._shares_for_value(marked_nav * target_weight, self.prices[symbol])
            if symbol in current:
                desired = max(current[symbol], desired)
            if desired > 0:
                target_shares[symbol] = desired
                active_symbols.add(symbol)
            elif derisk_gate and budget is not None and budget <= 0:
                # drawdown_derisk 閘門：預算 ≤ 0 是現金低於姿態緩衝，不是整張門檻，旗標改為 CASH_BUFFER。
                constraint_flags.append(
                    {"symbol": symbol, "code": "CASH_BUFFER", "detail": "現金低於姿態緩衝，沒有配置預算，未建立買單"}
                )
            else:
                constraint_flags.append(
                    {"symbol": symbol, "code": "LOT_SIZE", "detail": "目標金額不足一個交易單位"}
                )

        orders = self._build_orders(current, target_shares, order_intents, effective)
        orders, projected_cash, cash_flags = self._fit_cash(
            orders, starting_cash, starting_total_cash, marked_nav, effective, current,
            floor_disabled=bool(derisk_flags),
        )
        constraint_flags.extend(cash_flags)
        orders, projected_cash, weight_flags = self._fit_stock_weights(
            current, orders, projected_cash, effective
        )
        constraint_flags.extend(weight_flags)
        orders, projected_cash, sector_flags = self._fit_sector_weights(
            current, orders, projected_cash, effective
        )
        constraint_flags.extend(sector_flags)
        orders, projected_cash, turnover_flags = self._fit_turnover(current, orders, projected_cash, marked_nav, effective)
        constraint_flags.extend(turnover_flags)
        final_shares = dict(current)
        for order in orders:
            sign = 1 if order["side"] == "buy" else -1
            final_shares[order["symbol"]] = final_shares.get(order["symbol"], 0) + sign * int(
                order["shares"]
            )
        final_shares = {symbol: shares for symbol, shares in final_shares.items() if shares > 0}
        # 與 _order 相同：先量化到 PRICE 再計算，Validator 才能由 position.price 精確重算。
        position_prices = {
            symbol: self.prices[symbol].quantize(PRICE, rounding=ROUND_HALF_UP)
            for symbol in final_shares
        }
        nav = projected_cash + sum(
            Decimal(shares) * position_prices[symbol] for symbol, shares in final_shares.items()
        )
        positions = [
            {
                "symbol": symbol,
                "lots": shares // lot_size,
                "shares": shares,
                "price": _price(position_prices[symbol]),
                "market_value": _money(Decimal(shares) * position_prices[symbol]),
                "weight": _rate(Decimal(shares) * position_prices[symbol] / nav),
                "intent": intents.get(symbol, "hold"),
            }
            for symbol, shares in sorted(final_shares.items())
        ]
        turnover = sum(decimal_value(order["gross_amount"], "gross_amount") for order in orders) / marked_nav
        body: Dict[str, object] = {
            "schema_version": DECISION_SCHEMA_VERSION,
            "bundle_id": self.context.bundle_id,
            "snapshot_id": self.context.snapshot_id,
            "decision_cutoff": self.context.decision_cutoff,
            "bundle_hash": self.context.bundle_hash,
            "trade_intent_result_id": intent_artifact_id(intent_result),
            "policy_id": self.policy["policy_id"],
            "policy_hash": self.policy["content_sha256"],
            "engine_version": ALLOCATION_ENGINE_VERSION,
            "revision_count": revision_count,
            "parent_proposal_id": parent_proposal_id,
            "excluded_symbols": sorted(excluded),
            "constraint_flags": constraint_flags,
            "effective_policy": {key: _rate(value) if isinstance(value, Decimal) else value for key, value in effective.items()},
            "status": "completed",
            "allocation_proposal": {
                "positions": positions,
                "cash": _money(projected_cash),
                "cash_weight": _rate(projected_cash / nav),
                "nav": _money(nav),
            },
            "order_proposal": {
                "orders": orders,
                "starting_cash": _money(starting_cash),
                "starting_total_cash": _money(starting_total_cash),
                "projected_cash": _money(projected_cash),
                "turnover_rate": _rate(turnover),
                "total_commission": _money(
                    sum(
                        (decimal_value(item["commission"], "commission") for item in orders),
                        Decimal("0"),
                    )
                ),
                "total_tax": _money(
                    sum(
                        (decimal_value(item["tax"], "tax") for item in orders),
                        Decimal("0"),
                    )
                ),
            },
            "errors": [],
        }
        if sized_weights is not None:
            sizing = self.policy["position_sizing"]
            body["position_sizing"] = {
                "method": sizing["method"],
                "sizing_plan_id": sizing["sizing_plan_id"],
                "target_weights": {
                    symbol: _rate(weight) for symbol, weight in sorted(sized_weights.items())
                },
            }
        body["proposal_id"] = "proposal:" + canonical_sha256(body)[:20]
        body["content_sha256"] = artifact_content_sha256(body)
        return body

    def _forced_swap_victims(
        self,
        candidates: Sequence[str],
        current: Mapping[str, int],
        active_symbols: Set[str],
        target_shares: Mapping[str, int],
        intents: Mapping[str, str],
        kept_exits: Mapping[str, str],
        untradable: Set[str],
        hard_max: int,
        adopted_bear: Set[str],
        starting_cash: Decimal,
        marked_nav: Decimal,
        effective: Mapping[str, object],
    ) -> Tuple[List[str], Dict[str, str]]:
        """滿檔又沒人出場時，信心夠高的未持有新股可換掉「論點被推翻」的續抱持股（rotation.forced_swap，版本閘門）。

        新股名額數量依 ``_conviction_targets`` 的同一套規則（廣度目標內的空名額＋本次出場釋出的名額）推算；
        不夠的部分才由程式換出「交易 Agent 明確標為 hold、且在 Policy 綁定的 broken_symbols 名單內」的持股（依代號）。
        沒有 forced_swap 設定、沒有 broken_symbols、或沒有高信心新股時一律不換；只讀 Policy，不看 20 日報酬。

        ``forced_swap.confirmation``（版本閘門）啟用時另要求：被換出者必須有本次交易 Agent 採納的該股空頭 claim
        （``adopted_bear``），否則不換並回傳於 unconfirmed；換出數不超過「出場後持股數 − min_positions」，也不超過
        現金（含本次賣出收入，依 reuse_sell_proceeds）能支應一張新股的檔數。回傳 (victims, unconfirmed)。"""
        sizing = self.policy.get("position_sizing") or {}
        rotation = sizing.get("rotation")
        forced = rotation.get("forced_swap") if isinstance(rotation, Mapping) else None
        tiers = sizing.get("conviction_by_symbol")
        if forced is None or tiers is None:
            return [], {}
        confirmation = forced.get("confirmation") == FORCED_SWAP_CONFIRMATION
        threshold = CONVICTION_LEVELS.index(str(forced["min_new_conviction"]))
        wanted = [
            symbol for symbol in candidates
            if symbol not in active_symbols and symbol in self.prices and symbol in tiers
            and CONVICTION_LEVELS.index(tiers[symbol]) <= threshold
        ]
        if confirmation:
            # 確認版的現金檢查必須和後續實際配置使用同一候選順序；否則代號較前但排名較後的昂貴股票
            # 會先占用換股現金，錯誤取消本來買得起且排名較前的候選。舊 forced_swap 路徑維持原代號順序。
            wanted = self._ordered_conviction_candidates(wanted, tiers, sizing, self._atr_pct())
        if not wanted:
            return [], {}
        target = breadth_target(sizing)
        cap = hard_max if target is None else min(hard_max, max(target, int(self.policy["min_positions"])))
        held_count = len(current)
        released = max(0, held_count - len(active_symbols))
        rotation_cap = int(rotation["max_swaps_per_run"])
        free = max(0, cap - held_count) + min(released, rotation_cap)
        # 換出會在後面的名額配對中被 rotation 的換股上限限制；換出數不得超過剩餘換股名額，否則變成只賣不換。
        need = min(len(wanted) - free, int(forced["max_swaps_per_run"]), max(0, rotation_cap - min(released, rotation_cap)))
        if confirmation:
            need = min(need, len(active_symbols) - int(self.policy["min_positions"]))
        if need <= 0:
            return [], {}
        broken = set(forced.get("broken_symbols") or [])
        pool = sorted(
            symbol for symbol in current
            if symbol in broken and intents.get(symbol, "hold") == "hold" and target_shares.get(symbol, 0) > 0
            and symbol not in untradable and symbol not in kept_exits
        )
        if not confirmation:
            return pool[:need], {}
        unconfirmed = {
            symbol: "論點被推翻但本次交易 Agent 未採納該股任何空頭 claim，沒有本次證據支持出場，由程式不換出（交易 Agent 的意圖未被改寫）"
            for symbol in pool if symbol not in adopted_bear
        }
        confirmed = [symbol for symbol in pool if symbol in adopted_bear]
        # 現金能支應的新股數：原有現金（扣姿態緩衝）加本次出場收入（reuse_sell_proceeds 時）要買得起每檔新股至少一張；
        # 換出者的賣出收入同樣只在 reuse_sell_proceeds 時可用。
        buffer = marked_nav * effective["cash_buffer_rate"]  # type: ignore[operator]
        if effective.get("cash_buffer_floor") == CASH_BUFFER_FLOOR:
            buffer = min(buffer, starting_cash)
        room = starting_cash - buffer
        reuse = bool(effective["reuse_sell_proceeds"])
        if reuse:
            for symbol in sorted(current):
                sold = current[symbol] - target_shares.get(symbol, 0)
                if sold > 0:
                    room += decimal_value(self._order(symbol, "sell", sold, "exit", effective)["net_cash_impact"], "net_cash_impact")
        lot = int(effective["lot_size"])
        new_costs = [-decimal_value(self._order(symbol, "buy", lot, "buy", effective)["net_cash_impact"], "net_cash_impact")
                     for symbol in wanted[free:]] if free < len(wanted) else []
        victims: List[str] = []
        for symbol in confirmed:
            if len(victims) >= need or len(victims) >= len(new_costs):
                break
            proceeds = decimal_value(
                self._order(symbol, "sell", target_shares[symbol], "exit", effective)["net_cash_impact"], "net_cash_impact"
            ) if reuse else Decimal("0")
            if room + proceeds < new_costs[len(victims)]:
                # victim 依代號決定優先順序，但前一檔賣款不足不代表後一檔也不足；沿用同一候選，不另找替補算法。
                continue
            room += proceeds - new_costs[len(victims)]
            victims.append(symbol)
        return victims, unconfirmed

    def _ordered_conviction_candidates(
        self,
        candidates: Sequence[str],
        tiers: Mapping[str, str],
        sizing: Mapping[str, object],
        volatility: Mapping[str, Decimal],
    ) -> List[str]:
        """回傳配置使用的唯一候選排序；forced_swap.confirmation 亦共用此規則。"""
        support = sizing.get("support_by_symbol", {})
        rank = sizing.get("rank_by_symbol")
        if rank is not None:
            return sorted(
                candidates,
                key=lambda symbol: (CONVICTION_LEVELS.index(tiers[symbol]), rank[symbol], symbol),
            )
        if breadth_target(sizing) is None:
            return sorted(
                candidates,
                key=lambda symbol: (CONVICTION_LEVELS.index(tiers[symbol]), -support.get(symbol, 0), symbol),
            )
        tiebreak = str(sizing.get("selection_tiebreak", "atr_low"))
        return sorted(
            candidates,
            key=lambda symbol: (
                CONVICTION_LEVELS.index(tiers[symbol]), -support.get(symbol, 0),
                *self._tiebreak_key(symbol, tiebreak, volatility), symbol,
            ),
        )

    def _latest_prices(self) -> Dict[str, Decimal]:
        result: Dict[str, Decimal] = {}
        for row in self.context.snapshot["latest_prices"]:
            symbol = str(row["symbol"]).upper()
            price = snapshot_execution_price(row, "latest_prices")
            if price <= 0:
                raise DecisionToolError("%s 決策用價必須大於 0" % symbol)
            result[symbol] = price
        return result

    def _effective_policy(self, overrides: Mapping[str, object]) -> Dict[str, object]:
        keys = {
            "cash_buffer_rate",
            "default_target_weight",
            "trim_fraction",
            "max_turnover_rate",
            "max_stock_weight",
            "max_positions",
        }
        unknown = set(overrides) - keys
        if unknown:
            raise DecisionToolError("修正包含未允許設定：%s" % ", ".join(sorted(unknown)))
        result: Dict[str, object] = {
            key: decimal_value(self.policy[key], "DecisionPolicy.%s" % key)
            for key in (
                "cash_buffer_rate",
                "default_target_weight",
                "trim_fraction",
                "max_turnover_rate",
                "max_stock_weight",
            )
        }
        result.update(
            {
                "max_positions": int(self.policy["max_positions"]),
                "lot_size": int(self.policy["lot_size"]),
                "commission_rate": decimal_value(self.policy["commission_rate"], "commission_rate"),
                "sell_tax_rate": decimal_value(self.policy["sell_tax_rate"], "sell_tax_rate"),
                "minimum_commission": decimal_value(self.policy["minimum_commission"], "minimum_commission"),
                "slippage_bps": decimal_value(self.policy["slippage_bps"], "slippage_bps"),
                "reuse_sell_proceeds": bool(self.policy["reuse_sell_proceeds"]),
            }
        )
        # 執行面版本閘門只在 Policy 有該欄位時才進 effective（並寫入 proposal.effective_policy）：
        # 省略欄位的舊 Decision run 的 effective_policy 必須逐鍵不變，否則重建比對會失敗。
        sizing = self.policy.get("position_sizing") or {}
        for key in ("cash_buffer_floor", "cash_fit_order", "cash_ceiling_fix", "stock_limit_headroom", "buy_gap_reserve",
                    "sector_fit", "turnover_fit"):
            if key in sizing:
                result[key] = sizing[key]
        if "cash_fit_order" in sizing:
            result["cash_fit_min_fill_ratio"] = cash_fit_min_fill_ratio(sizing)
        if "buy_gap_reserve" in sizing:
            result["buy_gap_reserve_rate"] = buy_gap_reserve_rate(sizing)
        for key, raw in overrides.items():
            result[key] = int(raw) if key == "max_positions" else decimal_value(raw, key)
        return result

    def _conviction_targets(
        self,
        candidates: List[str],
        active_symbols: Set[str],
        target_shares: Mapping[str, int],
        marked_nav: Decimal,
        max_positions: int,
        effective: Mapping[str, object],
        held_count: int,
    ) -> Tuple[List[str], Dict[str, Decimal], Dict[str, str], Decimal]:
        """依風控等級排序候選，取到持股廣度目標（舊 policy 為 max_positions），並以等級乘數／ATR 分配扣除現金緩衝後的資金。

        回傳 (ordered, weights, skipped, budget)；skipped 記錄被名額規則擋下的新股與對應旗標代碼，
        啟用 rotation 時才會有內容，舊 policy 一律為空並由 `_run` 沿用原本的旗標文案；budget 是扣除緩衝與
        未入選持股後可配置的權重預算（≤ 0 代表現金已低於姿態緩衝）。
        """
        hard_max = max_positions
        sizing = self.policy["position_sizing"]
        tiers = sizing.get("conviction_by_symbol")
        if tiers is None:
            raise DecisionToolError("DecisionPolicy 已啟用 position_sizing，但尚未綁定交易決策的信心等級")
        missing = [symbol for symbol in candidates if symbol not in tiers]
        if missing:
            raise DecisionToolError("交易決策缺少候選等級：" + ", ".join(missing))
        volatility = self._atr_pct()
        target = breadth_target(sizing)
        ordered = self._ordered_conviction_candidates(candidates, tiers, sizing, volatility)
        if target is not None:
            max_positions = min(max_positions, max(target, int(self.policy["min_positions"])))
        # held_add_priority：未持有的新股只能用賣出後空出的檔數；滿檔時不得排進名額，
        # 否則會擠掉已持有、買得進去的加碼。省略此欄位的舊 Decision run 維持原重建行為。
        held_add_priority = "held_add_priority" in sizing
        rotation = sizing.get("rotation")
        skipped: Dict[str, str] = {}
        if rotation is None:
            slots = max_positions - len(active_symbols - set(ordered))
            free_for_new = max_positions - len(active_symbols)
            growth_left = swap_left = new_cap = 0
            min_tier = 0
        else:
            # 廣度收窄只停止「淨增加」，不凍結既有持股的加碼與換股：cap 以目前持股數為底，
            # 否則持有 30 檔而廣度選 22 時 slots 會變負數，連 add 都一起被凍住。
            cap = max(max_positions, min(held_count, hard_max))
            slots = cap - len(active_symbols - set(ordered))
            # growth：廣度目標之下、出場前就存在的真正空名額，不設信心門檻。
            growth_left = max(0, max_positions - held_count)
            # swap：本次實際出場（被 RISK_MIN_POSITIONS 留住或不可交易而續抱者不計）釋出的名額。
            released = max(0, held_count - len(active_symbols))
            swap_left = min(released, int(rotation["max_swaps_per_run"]))
            new_cap = max(0, hard_max - len(active_symbols))
            min_tier = CONVICTION_LEVELS.index(str(rotation["min_new_conviction"]))
            free_for_new = 0
        eligible: List[str] = []
        for symbol in ordered:
            if symbol not in self.prices or len(eligible) >= slots:
                continue
            if rotation is None:
                if held_add_priority and symbol not in active_symbols:
                    if free_for_new <= 0:
                        continue
                    free_for_new -= 1
            elif symbol not in active_symbols:
                if new_cap <= 0 or (growth_left <= 0 and swap_left <= 0):
                    skipped[symbol] = "MAX_POSITIONS"
                    continue
                if growth_left > 0:
                    growth_left -= 1
                elif CONVICTION_LEVELS.index(tiers[symbol]) > min_tier:
                    # 換股釋出的名額只給信心足夠的新股，避免為了填名額而換進弱候選。
                    skipped[symbol] = "ROTATION_CONVICTION"
                    continue
                else:
                    swap_left -= 1
                new_cap -= 1
            if symbol not in volatility:
                raise DecisionToolError("%s 缺少可用 ATR，不能依波動度配置" % symbol)
            eligible.append(symbol)
        held_outside = sum(
            (
                Decimal(target_shares[symbol]) * self.prices[symbol] / marked_nav
                for symbol in active_symbols - set(eligible)
            ),
            Decimal("0"),
        )
        budget = Decimal("1") - effective["cash_buffer_rate"] - held_outside  # type: ignore[operator]
        weights = conviction_weights(
            eligible,
            tiers,
            {
                level: decimal_value(sizing["conviction_multipliers"][level], level)
                for level in CONVICTION_LEVELS
            },
            volatility,
            decimal_value(sizing["volatility_floor"], "volatility_floor"),
            # eligible 全是本次 buy／add 的股票：stock_limit_headroom 啟用時封頂留 3%（0.97），否則 0.995。
            {symbol: self._weight_limit(symbol, effective) * self._headroom(effective, buying=True) for symbol in eligible},
            budget,
            # 省略 volatility_exponent 的舊 policy 傳 None，conviction_weights 走 v1 原式。
            decimal_value(sizing["volatility_exponent"], "volatility_exponent")
            if "volatility_exponent" in sizing
            else None,
        )
        return ordered, weights, skipped, budget

    def _slot_flag(
        self,
        symbol: str,
        skipped: Mapping[str, str],
        rotation: Optional[Mapping[str, object]],
    ) -> Dict[str, str]:
        """名額不足時的旗標；rotation 省略時文案與舊 Decision run 逐字相同。"""
        if rotation is None:
            breadth = breadth_target(self.policy.get("position_sizing") or {})
            return {
                "code": "MAX_POSITIONS",
                "detail": "持股檔數已達上限" if breadth is None
                else "持股檔數已達風險 Agent 持股廣度對應的 %d 檔" % breadth,
            }
        if skipped.get(symbol) == "ROTATION_CONVICTION":
            return {
                "code": "ROTATION_CONVICTION",
                "detail": "出場釋出的名額只給信心不低於 %s 的新股（交易 Agent 的意圖未被改寫）"
                % rotation["min_new_conviction"],
            }
        return {
            "code": "MAX_POSITIONS",
            "detail": "持股已滿且本次沒有出場釋出名額（交易 Agent 的意圖未被改寫）",
        }

    def _tiebreak_key(
        self, symbol: str, tiebreak: str, volatility: Mapping[str, Decimal]
    ) -> Tuple[Decimal, ...]:
        """同等級同支持數的排序值（升冪）；缺值一律排到最後，不補假值。"""
        if tiebreak == "symbol":
            return ()
        if tiebreak == "momentum_high":
            value = self._returns_20d().get(symbol)
            # 近期報酬高者優先，所以取負值；缺 20 日報酬者排最後。
            return (Decimal("Infinity") if value is None else -value,)
        return (volatility.get(symbol, Decimal("Infinity")),)

    def _returns_20d(self) -> Dict[str, Decimal]:
        if self._returns is None:
            items = MomentumEngine(self.context.bundle).run()["items"]
            self._returns = {
                str(item["symbol"]).upper(): Decimal(str(item["return_20d"]))
                for item in items
                if item.get("status") == "available" and item.get("return_20d") is not None
            }
        return self._returns

    def _atr_pct(self) -> Dict[str, Decimal]:
        if self._volatility is None:
            items = MomentumEngine(self.context.bundle).run()["items"]
            self._volatility = {
                str(item["symbol"]).upper(): Decimal(str(item["atr_14_pct"]))
                for item in items
                if item.get("status") == "available" and item.get("atr_14_pct") is not None
            }
        return self._volatility

    def _weight_limit(self, symbol: str, effective: Mapping[str, object]) -> Decimal:
        special = self.policy.get("special_weight_limits", {})
        if symbol in special:
            return decimal_value(special[symbol], "special_weight_limits.%s" % symbol)
        return effective["max_stock_weight"]  # type: ignore[return-value]

    @staticmethod
    def _headroom(effective: Mapping[str, object], buying: bool) -> Decimal:
        """單股上限的封頂係數：stock_limit_headroom 啟用且該股當日有買單時留 3%，其餘（含舊 run）留 0.5%。

        比賽規則「因加碼導致超限當日即警告，價格波動超限才有 5 日寬限」：只有自己買上去的部位需要多留空間。"""
        if buying and effective.get("stock_limit_headroom") == STOCK_LIMIT_HEADROOM:
            return LIMIT_HEADROOM_BUY
        return LIMIT_HEADROOM

    def _shares_for_value(self, value: Decimal, price: Decimal) -> int:
        lot = int(self.policy["lot_size"])
        lots = (value / price / Decimal(lot)).to_integral_value(rounding=ROUND_FLOOR)
        return int(lots) * lot

    def _build_orders(
        self,
        current: Mapping[str, int],
        target: Mapping[str, int],
        intents: Mapping[str, str],
        effective: Mapping[str, object],
    ) -> List[Dict[str, object]]:
        orders: List[Dict[str, object]] = []
        for symbol in sorted(set(current) | set(target)):
            delta = int(target.get(symbol, 0)) - int(current.get(symbol, 0))
            if not delta:
                continue
            side = "buy" if delta > 0 else "sell"
            orders.append(self._order(symbol, side, abs(delta), intents.get(symbol, "hold"), effective))
        return sorted(orders, key=lambda item: (0 if item["side"] == "sell" else 1, item["symbol"]))

    def _fit_stock_weights(
        self,
        current: Mapping[str, int],
        orders: List[Dict[str, object]],
        cash: Decimal,
        effective: Mapping[str, object],
    ) -> Tuple[List[Dict[str, object]], Decimal, List[Dict[str, object]]]:
        """在 Guard 前按整張降低預計超重部位；費稅改變 NAV 時逐張重算。"""
        fitted = list(orders)
        shares = dict(current)
        for order in fitted:
            symbol = str(order["symbol"])
            shares[symbol] = shares.get(symbol, 0) + (1 if order["side"] == "buy" else -1) * int(order["shares"])
        original = dict(shares)
        lot = int(effective["lot_size"])
        # 沿用正式情境引擎的成交率、費稅與 NAV 計算，不自行近似壓力情境。
        from .risk import ScenarioEngine

        scenarios = ScenarioEngine(self.context.bundle, self.policy)
        while True:
            changed = False
            for symbol in sorted(shares):
                while shares[symbol] > 0:
                    # 封頂係數每輪重算：買單被整筆減光後該股只剩續抱，回到 0.995，不會因價格漂移被多賣。
                    buying = any(item["symbol"] == symbol and item["side"] == "buy" for item in fitted)
                    limit = self._weight_limit(symbol, effective) * self._headroom(effective, buying)
                    result = scenarios.run({"proposal_id": "risk-fit", "order_proposal": {"orders": fitted}})
                    overweight = False
                    for scenario in result["scenarios"]:
                        nav = decimal_value(scenario["nav"], "scenario.nav")
                        if nav <= 0:
                            raise DecisionToolError("風險限額調整後情境 NAV 必須大於 0")
                        value = next((decimal_value(row["assumed_value"], "assumed_value") for row in scenario["positions"] if row["symbol"] == symbol), Decimal("0"))
                        if value > nav * limit:
                            overweight = True
                            break
                    if not overweight:
                        break
                    existing = next((item for item in fitted if item["symbol"] == symbol), None)
                    if existing is not None and existing["side"] == "buy":
                        remaining = int(existing["shares"]) - lot
                        replacement = self._order(symbol, "buy", remaining, str(existing["intent"]), effective) if remaining else None
                    else:
                        remaining = int(existing["shares"]) + lot if existing is not None else lot
                        # 風控減碼的賣單不沿用 hold；依減碼後是否還有持股標為 trim 或 exit。
                        sell_intent = str(existing["intent"]) if existing else "hold"
                        if sell_intent == "hold":
                            sell_intent = "trim" if shares[symbol] - lot > 0 else "exit"
                        replacement = self._order(symbol, "sell", remaining, sell_intent, effective)
                    if existing is not None:
                        fitted.remove(existing)
                        cash -= decimal_value(existing["net_cash_impact"], "net_cash_impact")
                    if replacement is not None:
                        fitted.append(replacement)
                        cash += decimal_value(replacement["net_cash_impact"], "net_cash_impact")
                    shares[symbol] -= lot
                    changed = True
            if not changed:
                break
        flags: List[Dict[str, object]] = []
        for symbol in sorted(shares):
            if shares[symbol] != original[symbol]:
                flags.append({
                    "symbol": symbol,
                    "code": "RISK_STOCK_WEIGHT",
                    "detail": "單股硬性上限風險調整：預計股數由 %d 降為 %d（訂單意圖由程式標為 trim／exit，交易 Agent 的意圖未被改寫）" % (original[symbol], shares[symbol]),
                })
        fitted.sort(key=lambda item: (0 if item["side"] == "sell" else 1, item["symbol"]))
        return fitted, cash, flags

    def _fit_sector_weights(
        self,
        current: Mapping[str, int],
        orders: List[Dict[str, object]],
        cash: Decimal,
        effective: Mapping[str, object],
    ) -> Tuple[List[Dict[str, object]], Decimal, List[Dict[str, object]]]:
        """產業權重在任一情境超過上限時，在 Guard 前由程式按整張減碼。

        舊路徑：先減當日買進，再賣既有持股；同產業內先減信心等級低、情境市值大的股票。
        ``sector_fit``（版本閘門）啟用時只減當日 buy／add 的增量、不賣既有持股（產業上限是本隊自設的風控，
        不是比賽硬規則，不因價格漂移強賣交易 Agent 要續抱的股票）；減光買單仍超限的產業由 Guard 以
        「既有已超限且本次沒有買進」容忍（carried）。上限為 1 時不動作，舊 Decision run 的重建結果不變。
        """
        raw_limit = decimal_value(self.policy["max_sector_weight"], "max_sector_weight")
        if raw_limit >= 1:
            return orders, cash, []
        increment_only = effective.get("sector_fit") == SECTOR_FIT
        limit = raw_limit * LIMIT_HEADROOM
        sectors = {str(k).upper(): str(v) for k, v in self.policy.get("sector_by_symbol", {}).items()}
        tiers = (self.policy.get("position_sizing") or {}).get("conviction_by_symbol") or {}
        fitted = list(orders)
        shares = dict(current)
        for order in fitted:
            symbol = str(order["symbol"])
            shares[symbol] = shares.get(symbol, 0) + (1 if order["side"] == "buy" else -1) * int(order["shares"])
        original = dict(shares)
        lot = int(effective["lot_size"])
        from .risk import ScenarioEngine

        scenarios = ScenarioEngine(self.context.bundle, self.policy)
        # 每輪至少減一張，總張數有限，迴圈必定結束；上限只防程式錯誤造成無窮迴圈。
        for _ in range(10000):
            result = scenarios.run({"proposal_id": "risk-fit", "order_proposal": {"orders": fitted}})
            worst: Optional[Tuple[Decimal, str, Dict[str, Decimal]]] = None
            for scenario in result["scenarios"]:
                nav = decimal_value(scenario["nav"], "scenario.nav")
                if nav <= 0:
                    raise DecisionToolError("產業限額調整後情境 NAV 必須大於 0")
                values: Dict[str, Decimal] = {}
                by_symbol: Dict[str, Decimal] = {}
                for row in scenario["positions"]:
                    symbol = str(row["symbol"]).upper()
                    if symbol in sectors:
                        value = decimal_value(row["assumed_value"], "assumed_value")
                        by_symbol[symbol] = value
                        values[sectors[symbol]] = values.get(sectors[symbol], Decimal("0")) + value
                for sector, value in sorted(values.items()):
                    excess = value - nav * limit
                    if excess > 0 and (worst is None or excess > worst[0]):
                        worst = (excess, sector, by_symbol)
            if worst is None:
                break
            excess, sector, by_symbol = worst
            buying = {str(o["symbol"]) for o in fitted if o["side"] == "buy"}
            members = [
                s for s in shares
                if shares[s] > 0 and sectors.get(s) == sector and (not increment_only or s in buying)
            ]
            if not members:
                # 部分成交情境下已無可再減的部位（sector_fit：買單已減光、不賣持股）；交由 Guard 判定，不宣稱已修正。
                break
            symbol = min(members, key=lambda s: (
                0 if s in buying else 1,
                -CONVICTION_LEVELS.index(tiers[s]) if s in tiers else 0,
                -by_symbol.get(s, Decimal("0")), s,
            ))
            lots = int((excess / (self.prices[symbol] * lot)).to_integral_value(rounding=ROUND_CEILING))
            existing = next((item for item in fitted if item["symbol"] == symbol), None)
            if existing is not None and existing["side"] == "buy":
                cut = min(lots, int(existing["shares"]) // lot) * lot
                replacement = self._order(symbol, "buy", int(existing["shares"]) - cut, str(existing["intent"]), effective) if int(existing["shares"]) > cut else None
            else:
                cut = min(max(lots, 1), shares[symbol] // lot) * lot
                sold = (int(existing["shares"]) if existing is not None else 0) + cut
                sell_intent = str(existing["intent"]) if existing else "hold"
                if sell_intent == "hold":
                    sell_intent = "trim" if shares[symbol] - cut > 0 else "exit"
                replacement = self._order(symbol, "sell", sold, sell_intent, effective)
            if existing is not None:
                fitted.remove(existing)
                cash -= decimal_value(existing["net_cash_impact"], "net_cash_impact")
            if replacement is not None:
                fitted.append(replacement)
                cash += decimal_value(replacement["net_cash_impact"], "net_cash_impact")
            shares[symbol] -= cut
        else:
            raise DecisionToolError("產業限額調整未收斂")
        flags = [
            {
                "symbol": symbol,
                "code": "RISK_SECTOR_WEIGHT",
                "detail": (
                    "產業權重上限風險調整（%s）：買進股數由 %d 降為 %d（只減增量、不賣既有持股，交易 Agent 的意圖未被改寫）"
                    if increment_only else
                    "產業權重上限風險調整（%s）：預計股數由 %d 降為 %d（訂單意圖由程式標為 trim／exit，交易 Agent 的意圖未被改寫）"
                ) % (sectors.get(symbol, "UNCLASSIFIED"), original[symbol], shares[symbol]),
            }
            for symbol in sorted(shares) if shares[symbol] != original[symbol]
        ]
        fitted.sort(key=lambda item: (0 if item["side"] == "sell" else 1, item["symbol"]))
        return fitted, cash, flags

    def _fit_turnover(
        self,
        current: Mapping[str, int],
        orders: List[Dict[str, object]],
        cash: Decimal,
        marked_nav: Decimal,
        effective: Mapping[str, object],
    ) -> Tuple[List[Dict[str, object]], Decimal, List[Dict[str, object]]]:
        """``turnover_fit``（版本閘門）：總換手（買賣 gross 合計 ÷ NAV）超過 max_turnover_rate 時由程式縮減買單。

        依（信心低→高、ATR14 高→低、代號）挑最弱的買單：已持有的 add 只減增量（按整張）；未持有的新股減到
        低於目標股數 × cash_fit_min_fill_ratio 就整筆取消（換股數減少），不留塵埃持股。每筆記 TURNOVER_LIMIT。
        買單減光仍超過時表示賣單本身超過，交由 ``_run_min_positions`` 留住 exit（RISK_TURNOVER）。省略閘門的舊 run
        維持 ``_fit_cash`` 內依取得現金順序逐張減少的原行為。"""
        if effective.get("turnover_fit") != TURNOVER_FIT:
            return orders, cash, []
        budget = marked_nav * effective["max_turnover_rate"]  # type: ignore[operator]
        sizing = self.policy.get("position_sizing") or {}
        tiers = sizing.get("conviction_by_symbol") or {}
        atr = self._atr_pct()
        min_fill_ratio = cash_fit_min_fill_ratio(sizing)
        lot = int(effective["lot_size"])
        fitted = list(orders)
        requested = {str(o["symbol"]): int(o["shares"]) for o in orders if o["side"] == "buy"}
        flags: Dict[str, Dict[str, object]] = {}

        def total() -> Decimal:
            return sum((decimal_value(o["gross_amount"], "gross_amount") for o in fitted), Decimal("0"))

        while total() > budget:
            buys = [o for o in fitted if o["side"] == "buy"]
            if not buys:
                break
            weakest = min(
                buys,
                key=lambda o: (
                    -(CONVICTION_LEVELS.index(tiers[str(o["symbol"])]) if str(o["symbol"]) in tiers else len(CONVICTION_LEVELS)),
                    -atr.get(str(o["symbol"]), Decimal("Infinity")),
                    str(o["symbol"]),
                ),
            )
            symbol = str(weakest["symbol"])
            excess = total() - budget
            cut = min(max(int((excess / (self.prices[symbol] * lot)).to_integral_value(rounding=ROUND_CEILING)), 1),
                      int(weakest["shares"]) // lot) * lot
            remaining = int(weakest["shares"]) - cut
            if current.get(symbol, 0) <= 0 and Decimal(remaining) < Decimal(requested[symbol]) * min_fill_ratio:
                remaining = 0
            fitted.remove(weakest)
            cash -= decimal_value(weakest["net_cash_impact"], "net_cash_impact")
            if remaining > 0:
                replacement = self._order(symbol, "buy", remaining, str(weakest["intent"]), effective)
                fitted.append(replacement)
                cash += decimal_value(replacement["net_cash_impact"], "net_cash_impact")
            flags[symbol] = {
                "symbol": symbol, "code": "TURNOVER_LIMIT",
                "detail": "換手超過 %s 的上限：買進股數由 %d 降為 %d（依信心低→高、ATR 高→低縮減%s，交易 Agent 的意圖未被改寫）"
                % (format(effective["max_turnover_rate"], ".0%"), requested[symbol], remaining,
                   "，新股殘量不足整筆取消" if remaining == 0 and current.get(symbol, 0) <= 0 else ""),
            }
        fitted.sort(key=lambda item: (0 if item["side"] == "sell" else 1, item["symbol"]))
        return fitted, cash, [flags[symbol] for symbol in sorted(flags)]

    def _drawdown_derisk(
        self,
        current: Mapping[str, int],
        target_shares: Dict[str, int],
        active_symbols: Set[str],
        order_intents: Dict[str, str],
        intents: Mapping[str, str],
        marked_nav: Decimal,
        starting_total_cash: Decimal,
        effective: Mapping[str, object],
        untradable: Set[str],
        kept_exits: Mapping[str, str],
    ) -> List[Dict[str, object]]:
        """``drawdown_derisk``（版本閘門）：現金姿態為 defensive 時，交易 Agent 的賣單算進現金後仍低於姿態緩衝
        （cash_buffer_rate × NAV），由程式依（ATR14 高→低、信心低→高、代號）把持股按整張減碼到目標現金，一次最多賣出
        NAV × max_derisk_fraction；每檔記 RISK_DRAWDOWN，訂單意圖由程式標 trim／exit，不改寫交易 Agent 的意圖。
        不動不可交易、被留住的 exit、交易 Agent 已標 exit／trim 的持股；持股數不低於 min_positions（必要時留一張）。
        版本：v1 整張進位可超過單次上限；v2 進位不得超過；v3 另由 ``_run`` 把被減碼的持股排除在本次加碼候選外
        （減碼後的股數為上限，交易 Agent 標 add 也不加回）。省略閘門的舊 run 維持「防守姿態只停止買進、不降曝險」的原行為。"""
        sizing = self.policy.get("position_sizing") or {}
        fraction = drawdown_derisk_fraction(sizing)
        if fraction is None or sizing.get("cash_stance") != "defensive":
            return []
        lot = int(effective["lot_size"])
        projected_cash = starting_total_cash
        for symbol in sorted(current):
            sold = current[symbol] - target_shares.get(symbol, 0)
            if sold > 0:
                projected_cash += decimal_value(
                    self._order(symbol, "sell", sold, order_intents.get(symbol, "exit"), effective)["net_cash_impact"],
                    "net_cash_impact",
                )
        shortfall = marked_nav * effective["cash_buffer_rate"] - projected_cash  # type: ignore[operator]
        if shortfall <= 0:
            return []
        # 現金缺口可整張進位補足，但單次減碼總額（NAV × fraction）是硬上限：進位後不得超過剩餘預算。
        budget_capped = drawdown_derisk_v2(sizing)  # v1 的舊 run 維持進位可超過上限的原行為
        hard_budget = marked_nav * fraction
        remaining = min(shortfall, hard_budget)
        tiers = sizing.get("conviction_by_symbol") or {}
        atr = self._atr_pct()
        minimum = int(self.policy["min_positions"])
        count = len(active_symbols)
        order = sorted(
            (
                symbol for symbol in current
                if target_shares.get(symbol, 0) > 0 and symbol in self.prices and symbol not in untradable
                and symbol not in kept_exits and intents.get(symbol, "hold") not in {"exit", "forced_exit", "trim"}
            ),
            key=lambda symbol: (
                -atr.get(symbol, Decimal("-Infinity")),
                -(CONVICTION_LEVELS.index(tiers[symbol]) if symbol in tiers else len(CONVICTION_LEVELS)),
                symbol,
            ),
        )
        flags: List[Dict[str, object]] = []
        for symbol in order:
            if remaining <= 0:
                break
            held_lots = target_shares[symbol] // lot
            lot_value = self.prices[symbol] * lot
            lots = min(
                int((remaining / lot_value).to_integral_value(rounding=ROUND_CEILING)),
                int((hard_budget / lot_value).to_integral_value(rounding=ROUND_FLOOR)) if budget_capped else held_lots,
                held_lots,
            )
            if lots >= held_lots and count <= minimum:
                lots = held_lots - 1
            if lots <= 0:
                continue
            proceeds = decimal_value(self._order(symbol, "sell", lots * lot, "trim", effective)["net_cash_impact"], "net_cash_impact")
            before = target_shares[symbol]
            target_shares[symbol] = before - lots * lot
            if target_shares[symbol] == 0:
                active_symbols.discard(symbol)
                count -= 1
            order_intents[symbol] = "exit" if target_shares[symbol] == 0 else "trim"
            remaining -= proceeds
            hard_budget -= lots * lot_value
            flags.append({
                "symbol": symbol, "code": "RISK_DRAWDOWN",
                "detail": "防守姿態回撤減碼：現金低於 %s 的姿態緩衝，由程式依 ATR14 由高到低減碼，股數由 %d 降為 %d"
                          "（單次最多 NAV 的 %s；訂單意圖由程式標為 %s，交易 Agent 的意圖未被改寫）"
                % (format(effective["cash_buffer_rate"], ".0%"), before, target_shares[symbol], format(fraction, ".0%"),
                   order_intents[symbol]),
            })
        return flags

    def _order(
        self,
        symbol: str,
        side: str,
        shares: int,
        intent: str,
        effective: Mapping[str, object],
    ) -> Dict[str, object]:
        reference = self.prices[symbol]
        bps = effective["slippage_bps"] / Decimal("10000")  # type: ignore[operator]
        price = reference * (Decimal("1") + bps if side == "buy" else Decimal("1") - bps)
        price = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        gross = price * Decimal(shares)
        raw_fee = gross * effective["commission_rate"]  # type: ignore[operator]
        commission = max(raw_fee, effective["minimum_commission"]).quantize(  # type: ignore[arg-type]
            MONEY, rounding=ROUND_CEILING
        )
        tax = (
            gross * effective["sell_tax_rate"] if side == "sell" else Decimal("0")  # type: ignore[operator]
        ).quantize(MONEY, rounding=ROUND_CEILING)
        impact = -(gross + commission) if side == "buy" else gross - commission - tax
        return {
            "symbol": symbol,
            "side": side,
            "lots": shares // int(effective["lot_size"]),
            "shares": shares,
            "reference_price": _price(reference),
            "estimated_price": _price(price),
            "gross_amount": _money(gross),
            "commission": _money(commission),
            "tax": _money(tax),
            "net_cash_impact": _money(impact),
            "intent": intent,
        }

    def _fit_cash(
        self,
        orders: List[Dict[str, object]],
        starting_cash: Decimal,
        starting_total_cash: Decimal,
        nav: Decimal,
        effective: Mapping[str, object],
        current: Optional[Mapping[str, int]] = None,
        floor_disabled: bool = False,
    ) -> Tuple[List[Dict[str, object]], Decimal, List[Dict[str, object]]]:
        """依現金緩衝與換手上限逐筆縮減買單。

        ``floor_disabled``（drawdown_derisk 已減碼時為 True）：減碼收入是為了補足姿態緩衝，不得被 cash_buffer_floor
        放行拿去買替補，所以緩衝一律以 NAV × cash_buffer_rate 計。``turnover_fit`` 啟用時換手改由 ``_fit_turnover``
        依信心／ATR 縮減，這裡不再逐張擋換手。

        舊路徑：買單依代號順序取得現金、逐張減少。三個版本閘門（省略即不啟用，舊 run 逐位不變）：

        - ``cash_fit_order``：買單改依（信心、入選名次、代號）順序取得現金；因現金不足使未持有新股的殘量
          低於目標股數 × ``cash_fit_min_fill_ratio`` 時整筆取消並記 RISK_CASH_FIT，釋出的現金給後面的買單；
          已持有的 add 不受影響，只減少增量（維持 CASH_BUFFER 旗標）。
        - ``buy_gap_reserve``：決策用前日收盤、成交用當日均價，買單累計總額 × ``buy_gap_reserve_rate``
          另計入現金需求，避免跳空開高讓主辦方整批作廢。
        - ``cash_buffer_floor``：見下方註解。"""
        sells = [item for item in orders if item["side"] == "sell"]
        buys = [item for item in orders if item["side"] == "buy"]
        available = starting_cash
        if effective["reuse_sell_proceeds"]:
            available += sum(decimal_value(item["net_cash_impact"], "net_cash_impact") for item in sells)
        buffer = nav * effective["cash_buffer_rate"]  # type: ignore[operator]
        if effective.get("cash_buffer_floor") == CASH_BUFFER_FLOOR and not floor_disabled:
            # 現金已低於緩衝時，緩衝只擋「再動用原有現金」，不擋用本次賣出收入買的替補：
            # 否則出場釋出的名額會因現金永遠湊不到緩衝而白白流失。
            buffer = min(buffer, starting_cash)
        turnover_budget: Optional[Decimal] = nav * effective["max_turnover_rate"]  # type: ignore[operator]
        if effective.get("turnover_fit") == TURNOVER_FIT:
            turnover_budget = None
        turnover_used = sum(
            (decimal_value(item["gross_amount"], "gross_amount") for item in sells),
            Decimal("0"),
        )
        ordered_fit = effective.get("cash_fit_order") == CASH_FIT_ORDER
        reserve_rate = (
            effective["buy_gap_reserve_rate"] if effective.get("buy_gap_reserve") == BUY_GAP_RESERVE else None
        )
        if ordered_fit:
            sizing = self.policy.get("position_sizing") or {}
            tiers = sizing.get("conviction_by_symbol") or {}
            rank = sizing.get("rank_by_symbol") or {}
            # 信心高、名次前的買單先拿現金；沒有名次（舊排序或未入選排序）時同等級依代號。
            buys = sorted(
                buys,
                key=lambda item: (
                    CONVICTION_LEVELS.index(tiers[str(item["symbol"])]) if str(item["symbol"]) in tiers else len(CONVICTION_LEVELS),
                    rank.get(str(item["symbol"]), len(rank) + 1),
                    str(item["symbol"]),
                ),
            )
            min_fill_ratio: Decimal = effective["cash_fit_min_fill_ratio"]  # type: ignore[assignment]
        held = {symbol for symbol, shares in (current or {}).items() if int(shares) > 0}
        fitted = list(sells)
        flags: List[Dict[str, object]] = []
        buy_total = Decimal("0")
        for item in buys:
            candidate = dict(item)
            requested_shares = int(candidate["shares"])
            lot = int(effective["lot_size"])
            cash_clipped = False
            turnover_clipped = False
            while int(candidate["shares"]) > 0:
                cost = -decimal_value(candidate["net_cash_impact"], "net_cash_impact")
                gross = decimal_value(candidate["gross_amount"], "gross_amount")
                if reserve_rate is None:
                    cash_ok = available - cost >= buffer
                else:
                    cash_ok = available - cost - (buy_total + cost) * reserve_rate >= buffer
                turnover_ok = turnover_budget is None or turnover_used + gross <= turnover_budget
                if cash_ok and turnover_ok:
                    break
                cash_clipped = cash_clipped or not cash_ok
                turnover_clipped = turnover_clipped or not turnover_ok
                remaining = int(candidate["shares"]) - lot
                if remaining <= 0:
                    candidate = {}
                    break
                candidate = self._order(
                    str(item["symbol"]), "buy", remaining, str(item["intent"]), effective
                )
            dust_cancelled = False
            if (
                ordered_fit and candidate and cash_clipped and str(item["symbol"]) not in held
                and Decimal(int(candidate["shares"])) < Decimal(requested_shares) * min_fill_ratio
            ):
                # 新股只買得到零頭會白占一個名額：整筆取消，現金留給後面的買單。
                flags.append({
                    "symbol": item["symbol"],
                    "code": "RISK_CASH_FIT",
                    "detail": "現金不足：買進 %d 股只買得到 %d 股（低於目標的 %s），由程式整筆取消、不留塵埃持股（交易 Agent 的意圖未被改寫）"
                    % (requested_shares, int(candidate["shares"]), format(min_fill_ratio, ".0%")),
                })
                candidate = {}
                dust_cancelled = True
            if candidate:
                fitted.append(candidate)
                available += decimal_value(candidate["net_cash_impact"], "net_cash_impact")
                turnover_used += decimal_value(candidate["gross_amount"], "gross_amount")
                buy_total += -decimal_value(candidate["net_cash_impact"], "net_cash_impact")
            actual_shares = int(candidate["shares"]) if candidate else 0
            if dust_cancelled:
                continue
            if actual_shares != requested_shares and cash_clipped:
                flags.append(
                    {
                        "symbol": item["symbol"],
                        "code": "CASH_BUFFER",
                        "detail": "買進股數由 %d 降為 %d" % (requested_shares, actual_shares),
                    }
                )
            if actual_shares != requested_shares and turnover_clipped:
                flags.append(
                    {
                        "symbol": item["symbol"],
                        "code": "TURNOVER_LIMIT",
                        "detail": "買進股數由 %d 降為 %d" % (requested_shares, actual_shares),
                    }
                )
        if ordered_fit:
            # 取得現金的順序不等於輸出順序：訂單與旗標仍依（賣先買後、代號）排列，與舊路徑同形。
            fitted.sort(key=lambda item: (0 if item["side"] == "sell" else 1, item["symbol"]))
            flags.sort(key=lambda flag: (str(flag["symbol"]), str(flag["code"])))
        actual_cash = starting_total_cash + sum(
            decimal_value(item["net_cash_impact"], "net_cash_impact") for item in fitted
        )
        return fitted, actual_cash, flags


class ProposalValidator:
    """Rebuild an allocation/order proposal and compare the full artifact."""

    def __init__(
        self,
        bundle: Mapping[str, object],
        policy: Mapping[str, object],
        intent_result: Mapping[str, object],
    ):
        self.engine = AllocationOrderEngine(bundle, policy)
        self.intent_result = intent_result

    def validate(self, proposal: Mapping[str, object]) -> List[str]:
        invariant_errors = self._invariant_errors(proposal)
        try:
            effective = proposal.get("effective_policy", {})
            override_keys = {
                "cash_buffer_rate",
                "default_target_weight",
                "trim_fraction",
                "max_turnover_rate",
                "max_stock_weight",
                "max_positions",
            }
            overrides = {
                key: effective[key]
                for key in override_keys
                if isinstance(effective, Mapping) and key in effective
            }
            expected = self.engine.run(
                self.intent_result,
                excluded_symbols=set(proposal.get("excluded_symbols", [])),
                overrides=overrides,
                revision_count=int(proposal.get("revision_count", 0)),
                parent_proposal_id=proposal.get("parent_proposal_id"),
            )
        except (DecisionToolError, TypeError, ValueError) as error:
            return invariant_errors + [str(error)]
        if dict(proposal) != expected:
            invariant_errors.append("ProposalBundle 與確定性重算結果不一致")
        return invariant_errors

    def _invariant_errors(self, proposal: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        try:
            effective = proposal["effective_policy"]
            lot_size = int(effective["lot_size"])
            order_block = proposal["order_proposal"]
            orders = order_block["orders"]
            impacts = Decimal("0")
            for index, order in enumerate(orders):
                lots = order.get("lots")
                shares = order.get("shares")
                if not isinstance(lots, int) or isinstance(lots, bool) or lots <= 0:
                    errors.append("order[%d].lots 必須是正整數" % index)
                    continue
                if shares != lots * lot_size:
                    errors.append("order[%d] shares 必須等於 lots*lot_size" % index)
                gross = decimal_value(order["estimated_price"], "estimated_price") * Decimal(shares)
                if order["gross_amount"] != _money(gross):
                    errors.append("order[%d].gross_amount 無法由價格與股數重算" % index)
                commission = max(
                    gross * decimal_value(effective["commission_rate"], "commission_rate"),
                    decimal_value(effective["minimum_commission"], "minimum_commission"),
                ).quantize(MONEY, rounding=ROUND_CEILING)
                tax = (
                    gross * decimal_value(effective["sell_tax_rate"], "sell_tax_rate")
                    if order["side"] == "sell" else Decimal("0")
                ).quantize(MONEY, rounding=ROUND_CEILING)
                impact = -(gross + commission) if order["side"] == "buy" else gross - commission - tax
                if order["commission"] != _money(commission) or order["tax"] != _money(tax):
                    errors.append("order[%d] 費稅無法重算" % index)
                if order["net_cash_impact"] != _money(impact):
                    errors.append("order[%d].net_cash_impact 無法重算" % index)
                impacts += impact
            projected = decimal_value(order_block["starting_total_cash"], "starting_total_cash") + impacts
            if order_block["projected_cash"] != _money(projected):
                errors.append("order_proposal.projected_cash 無法由現金影響重算")
            allocation = proposal["allocation_proposal"]
            market_value = Decimal("0")
            for index, position in enumerate(allocation["positions"]):
                if position["shares"] != position["lots"] * lot_size:
                    errors.append("position[%d] shares 必須等於 lots*lot_size" % index)
                value = decimal_value(position["price"], "position.price") * Decimal(position["shares"])
                if position["market_value"] != _money(value):
                    errors.append("position[%d].market_value 無法重算" % index)
                market_value += value
            nav = decimal_value(allocation["cash"], "allocation.cash") + market_value
            if allocation["nav"] != _money(nav):
                errors.append("allocation_proposal.nav 無法重算")
        except (DecisionToolError, KeyError, TypeError, ValueError):
            errors.append("ProposalBundle 缺少可重算的整張帳務欄位")
        return errors
