"""Deterministic portfolio allocation, order, fee, and cash calculations."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Dict, List, Mapping, Optional, Set, Tuple

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
from .momentum import MomentumEngine
from .sizing import (
    CONVICTION_LEVELS, LIMIT_HEADROOM, SIZING_METHOD, breadth_target, conviction_weights, validate_position_sizing,
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

    def validate(self, policy: Mapping[str, object]) -> List[str]:
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
            validate_drawdown_control(policy, self.context.bundle, errors)
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
        不改寫交易 Agent 的意圖。Validator 以同一方法重算，結果確定。"""
        kept: Set[str] = set()
        minimum = int(self.policy["min_positions"])
        while True:
            body = self._run(intent_result, excluded_symbols, overrides, revision_count, parent_proposal_id, kept, dropped)
            need = minimum - len(body["allocation_proposal"]["positions"])
            if need <= 0:
                return body
            held = {str(item["symbol"]).upper() for item in self.context.bundle["account_snapshot"]["positions"]}
            untradable = self._untradable()
            atr = self._atr_pct()
            remaining = sorted(
                (
                    str(item.get("symbol", "")).upper()
                    for item in intent_result.get("items", [])
                    if isinstance(item, Mapping) and item.get("intent") == "exit"
                ),
                key=lambda symbol: (atr.get(symbol, Decimal("Infinity")), symbol),
            )
            remaining = [symbol for symbol in remaining if symbol in held and symbol not in kept and symbol not in untradable]
            if not remaining:
                return body
            kept.update(remaining[:need])

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
        kept_exits: Set[str],
        active_share_dropped: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, object]:
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
            if intent == "exit" and symbol in kept_exits:
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
        candidates = [
            symbol
            for symbol in sorted(intents)
            if intents[symbol] in {"buy", "add"} and symbol not in excluded
        ]
        # 受限標的在分配資金前就排除，避免占用預算；否則 Guard 的 TRADABLE 會否決整份決策。
        for symbol in candidates:
            if symbol in untradable:
                constraint_flags.append(
                    {"symbol": symbol, "code": "RISK_NOT_TRADABLE", "detail": "非可交易狀態，由程式不建立買單（交易 Agent 的意圖未被改寫）"}
                )
        candidates = [symbol for symbol in candidates if symbol not in untradable]
        for symbol, detail in sorted((active_share_dropped or {}).items()):
            if symbol in candidates:
                constraint_flags.append({"symbol": symbol, "code": "RISK_ACTIVE_SHARE", "detail": detail})
        candidates = [symbol for symbol in candidates if symbol not in (active_share_dropped or {})]
        sized_weights: Optional[Dict[str, Decimal]] = None
        if "position_sizing" in self.policy:
            candidates, sized_weights = self._conviction_targets(
                candidates, active_symbols, target_shares, marked_nav, max_positions, effective
            )
        for symbol in candidates:
            if symbol not in self.prices:
                constraint_flags.append(
                    {"symbol": symbol, "code": "MISSING_PRICE", "detail": "缺少 cutoff 行情"}
                )
                continue
            if (symbol not in active_symbols and len(active_symbols) >= max_positions) or (
                sized_weights is not None and symbol not in sized_weights
            ):
                breadth = breadth_target(self.policy.get("position_sizing") or {})
                constraint_flags.append(
                    {"symbol": symbol, "code": "MAX_POSITIONS",
                     "detail": "持股檔數已達上限" if breadth is None else "持股檔數已達風險 Agent 持股廣度對應的 %d 檔" % breadth}
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
            else:
                constraint_flags.append(
                    {"symbol": symbol, "code": "LOT_SIZE", "detail": "目標金額不足一個交易單位"}
                )

        orders = self._build_orders(current, target_shares, intents, effective)
        orders, projected_cash, cash_flags = self._fit_cash(
            orders, starting_cash, starting_total_cash, marked_nav, effective
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
    ) -> Tuple[List[str], Dict[str, Decimal]]:
        """依風控等級排序候選，取到持股廣度目標（舊 policy 為 max_positions），並以等級乘數／ATR 分配扣除現金緩衝後的資金。"""
        sizing = self.policy["position_sizing"]
        tiers = sizing.get("conviction_by_symbol")
        if tiers is None:
            raise DecisionToolError("DecisionPolicy 已啟用 position_sizing，但尚未綁定交易決策的信心等級")
        missing = [symbol for symbol in candidates if symbol not in tiers]
        if missing:
            raise DecisionToolError("交易決策缺少候選等級：" + ", ".join(missing))
        support = sizing.get("support_by_symbol", {})
        volatility = self._atr_pct()
        target = breadth_target(sizing)
        if target is None:
            ordered = sorted(
                candidates,
                key=lambda symbol: (CONVICTION_LEVELS.index(tiers[symbol]), -support.get(symbol, 0), symbol),
            )
        else:
            # 持股廣度啟用後，同等級同支持數改由 ATR 低者優先，不再只靠股票代號決定誰入選。
            ordered = sorted(
                candidates,
                key=lambda symbol: (
                    CONVICTION_LEVELS.index(tiers[symbol]), -support.get(symbol, 0),
                    volatility.get(symbol, Decimal("Infinity")), symbol,
                ),
            )
            max_positions = min(max_positions, max(target, int(self.policy["min_positions"])))
        slots = max_positions - len(active_symbols - set(ordered))
        # 未持有的新股只能用賣出後空出的檔數；滿檔時不得排進名額，否則會擠掉已持有、買得進去的加碼。
        free_for_new = max_positions - len(active_symbols)
        eligible: List[str] = []
        for symbol in ordered:
            if symbol not in self.prices or len(eligible) >= slots:
                continue
            if symbol not in active_symbols:
                if free_for_new <= 0:
                    continue
                free_for_new -= 1
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
        weights = conviction_weights(
            eligible,
            tiers,
            {
                level: decimal_value(sizing["conviction_multipliers"][level], level)
                for level in CONVICTION_LEVELS
            },
            volatility,
            decimal_value(sizing["volatility_floor"], "volatility_floor"),
            {symbol: self._weight_limit(symbol, effective) * LIMIT_HEADROOM for symbol in eligible},
            Decimal("1") - effective["cash_buffer_rate"] - held_outside,  # type: ignore[operator]
        )
        return ordered, weights

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
                limit = self._weight_limit(symbol, effective) * LIMIT_HEADROOM
                while shares[symbol] > 0:
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

        先減當日買進，再賣既有持股；同產業內先減信心等級低、情境市值大的股票。
        上限為 1 時不動作，舊 Decision run 的重建結果不變。
        """
        raw_limit = decimal_value(self.policy["max_sector_weight"], "max_sector_weight")
        if raw_limit >= 1:
            return orders, cash, []
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
            members = [s for s in shares if shares[s] > 0 and sectors.get(s) == sector]
            if not members:
                # 部分成交情境下已無可再減的部位；交由 Guard 判定，不宣稱已修正。
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
                "detail": "產業權重上限風險調整（%s）：預計股數由 %d 降為 %d（訂單意圖由程式標為 trim／exit，交易 Agent 的意圖未被改寫）"
                % (sectors.get(symbol, "UNCLASSIFIED"), original[symbol], shares[symbol]),
            }
            for symbol in sorted(shares) if shares[symbol] != original[symbol]
        ]
        fitted.sort(key=lambda item: (0 if item["side"] == "sell" else 1, item["symbol"]))
        return fitted, cash, flags

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
    ) -> Tuple[List[Dict[str, object]], Decimal, List[Dict[str, object]]]:
        sells = [item for item in orders if item["side"] == "sell"]
        buys = [item for item in orders if item["side"] == "buy"]
        available = starting_cash
        if effective["reuse_sell_proceeds"]:
            available += sum(decimal_value(item["net_cash_impact"], "net_cash_impact") for item in sells)
        buffer = nav * effective["cash_buffer_rate"]  # type: ignore[operator]
        turnover_budget = nav * effective["max_turnover_rate"]  # type: ignore[operator]
        turnover_used = sum(
            (decimal_value(item["gross_amount"], "gross_amount") for item in sells),
            Decimal("0"),
        )
        fitted = list(sells)
        flags: List[Dict[str, object]] = []
        for item in buys:
            candidate = dict(item)
            requested_shares = int(candidate["shares"])
            lot = int(effective["lot_size"])
            cash_clipped = False
            turnover_clipped = False
            while int(candidate["shares"]) > 0:
                cost = -decimal_value(candidate["net_cash_impact"], "net_cash_impact")
                gross = decimal_value(candidate["gross_amount"], "gross_amount")
                cash_ok = available - cost >= buffer
                turnover_ok = turnover_used + gross <= turnover_budget
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
            if candidate:
                fitted.append(candidate)
                available += decimal_value(candidate["net_cash_impact"], "net_cash_impact")
                turnover_used += decimal_value(candidate["gross_amount"], "gross_amount")
            actual_shares = int(candidate["shares"]) if candidate else 0
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
