"""Deterministic scenarios, competition checks, and risk-review contracts."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Dict, List, Mapping, Optional, Sequence, Set

from .allocation import DecisionPolicyValidator, ProposalValidator
from .contracts import (
    DECISION_SCHEMA_VERSION,
    DecisionContext,
    DecisionToolError,
    artifact_content_sha256,
    canonical_sha256,
    decimal_value,
    reject_unknown_fields,
    required_string,
    string_list,
)


SCENARIO_ENGINE_VERSION = "1.0.0"
GUARD_ENGINE_VERSION = "1.0.0"
MONEY = Decimal("1")
RATE = Decimal("0.00000001")
RISK_DECISIONS = {"approve", "revise", "reject"}
REVISION_TYPES = {
    "remove_candidate",
    "increase_cash_buffer",
    "reduce_max_stock_weight",
    "reduce_turnover_limit",
}


def _money(value: Decimal) -> str:
    return str(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def _rate(value: Decimal) -> str:
    return str(value.quantize(RATE, rounding=ROUND_HALF_UP))


class ScenarioEngine:
    """Apply transparent price and liquidity assumptions to one proposal."""

    def __init__(self, bundle: Mapping[str, object], policy: Mapping[str, object]):
        self.context = DecisionContext(bundle)
        errors = DecisionPolicyValidator(bundle).validate(policy)
        if errors:
            raise DecisionToolError("DecisionPolicy 驗證失敗：" + "；".join(errors))
        self.policy = dict(policy)

    def run(self, proposal: Mapping[str, object]) -> Dict[str, object]:
        decline = decimal_value(self.policy["stress_price_decline_rate"], "stress_price_decline_rate")
        multiplier = decimal_value(
            self.policy["stress_slippage_multiplier"], "stress_slippage_multiplier"
        )
        stress_fill = decimal_value(self.policy["liquidity_fill_rate"], "liquidity_fill_rate")
        account = self.context.bundle["account_snapshot"]
        starting_cash = decimal_value(account["cash"], "account_snapshot.cash")
        settled_cash = decimal_value(account["settled_cash"], "account_snapshot.settled_cash")
        lot_size = int(self.policy["lot_size"])
        prices = {
            str(row["symbol"]).upper(): decimal_value(row["analysis_close_price"], "analysis_close_price")
            for row in self.context.snapshot["latest_prices"]
        }
        starting_positions = {
            str(row["symbol"]).upper(): int(row["shares"])
            for row in account["positions"]
        }
        scenarios = []
        for name, price_factor, liquidity_factor, fill_rate in (
            ("base", Decimal("1"), Decimal("1"), Decimal("1")),
            ("price_decline", Decimal("1") - decline, Decimal("1"), Decimal("1")),
            ("liquidity_stress", Decimal("1") - decline, multiplier, stress_fill),
        ):
            shares = dict(starting_positions)
            cash = starting_cash
            buying_power = settled_cash
            executions = []
            unfilled_orders = []
            total_commission = Decimal("0")
            total_tax = Decimal("0")
            total_buy_cost = Decimal("0")
            orders = proposal.get("order_proposal", {}).get("orders", [])
            for order in orders:
                requested_lots = int(order["lots"])
                filled_lots = int(
                    (Decimal(requested_lots) * fill_rate).to_integral_value(rounding=ROUND_FLOOR)
                )
                filled_shares = filled_lots * lot_size
                unfilled_lots = requested_lots - filled_lots
                if unfilled_lots:
                    unfilled_orders.append(
                        {
                            "symbol": order["symbol"],
                            "side": order["side"],
                            "intent": order["intent"],
                            "unfilled_lots": unfilled_lots,
                            "unfilled_shares": unfilled_lots * lot_size,
                        }
                    )
                if not filled_shares:
                    continue
                reference = decimal_value(order["reference_price"], "order.reference_price")
                bps = decimal_value(self.policy["slippage_bps"], "slippage_bps") * liquidity_factor / Decimal("10000")
                execution_price = reference * (
                    Decimal("1") + bps if order["side"] == "buy" else Decimal("1") - bps
                )
                execution_price = execution_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                gross = execution_price * Decimal(filled_shares)
                commission = max(
                    gross * decimal_value(self.policy["commission_rate"], "commission_rate"),
                    decimal_value(self.policy["minimum_commission"], "minimum_commission"),
                ).quantize(MONEY, rounding=ROUND_CEILING)
                tax = (
                    gross * decimal_value(self.policy["sell_tax_rate"], "sell_tax_rate")
                    if order["side"] == "sell" else Decimal("0")
                ).quantize(MONEY, rounding=ROUND_CEILING)
                impact = -(gross + commission) if order["side"] == "buy" else gross - commission - tax
                symbol = str(order["symbol"]).upper()
                shares[symbol] = shares.get(symbol, 0) + (filled_shares if order["side"] == "buy" else -filled_shares)
                if shares[symbol] < 0:
                    raise DecisionToolError("情境成交造成負持股：%s" % symbol)
                cash += impact
                if order["side"] == "buy":
                    total_buy_cost += -impact
                    buying_power += impact
                elif self.policy["reuse_sell_proceeds"]:
                    buying_power += impact
                total_commission += commission
                total_tax += tax
                executions.append(
                    {
                        "symbol": symbol,
                        "side": order["side"],
                        "intent": order["intent"],
                        "filled_lots": filled_lots,
                        "filled_shares": filled_shares,
                        "execution_price": str(execution_price),
                        "gross_amount": _money(gross),
                        "commission": _money(commission),
                        "tax": _money(tax),
                        "net_cash_impact": _money(impact),
                    }
                )
            shares = {symbol: value for symbol, value in shares.items() if value > 0}
            values = []
            position_value = Decimal("0")
            for symbol, position_shares in sorted(shares.items()):
                close_price = prices[symbol] * price_factor
                value = Decimal(position_shares) * close_price
                position_value += value
                values.append(
                    {
                        "symbol": symbol,
                        "lots": position_shares // lot_size,
                        "shares": position_shares,
                        "assumed_price_factor": _rate(price_factor),
                        "assumed_close_price": str(close_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                        "assumed_value": _money(value),
                    }
                )
            nav = position_value + cash
            scenarios.append(
                {
                    "name": name,
                    "assumption_type": "scenario",
                    "price_decline_rate": _rate(Decimal("1") - price_factor),
                    "slippage_multiplier": _rate(liquidity_factor),
                    "order_fill_rate": _rate(fill_rate),
                    "executions": executions,
                    "unfilled_orders": unfilled_orders,
                    "positions": values,
                    "cash": _money(cash),
                    "nav": _money(nav),
                    "cash_weight": _rate(cash / nav) if nav > 0 else None,
                    "buying_power": _money(buying_power),
                    "buying_power_shortfall": _money(max(Decimal("0"), -buying_power)),
                    "total_buy_cost": _money(total_buy_cost),
                    "total_commission": _money(total_commission),
                    "total_tax": _money(total_tax),
                }
            )
        body: Dict[str, object] = {
            "schema_version": DECISION_SCHEMA_VERSION,
            "scenario_id": "",
            "bundle_id": self.context.bundle_id,
            "snapshot_id": self.context.snapshot_id,
            "decision_cutoff": self.context.decision_cutoff,
            "bundle_hash": self.context.bundle_hash,
            "proposal_id": proposal.get("proposal_id"),
            "policy_id": self.policy["policy_id"],
            "policy_hash": self.policy["content_sha256"],
            "engine_version": SCENARIO_ENGINE_VERSION,
            "status": "completed",
            "scenarios": scenarios,
            "errors": [],
        }
        body["scenario_id"] = "scenario:" + canonical_sha256(body)[:20]
        body["content_sha256"] = artifact_content_sha256(body)
        return body


class ScenarioValidator:
    def __init__(self, bundle: Mapping[str, object], policy: Mapping[str, object]):
        self.engine = ScenarioEngine(bundle, policy)

    def validate(self, proposal: Mapping[str, object], result: Mapping[str, object]) -> List[str]:
        return [] if dict(result) == self.engine.run(proposal) else ["ScenarioResult 與確定性重算結果不一致"]


class CompetitionGuardV2:
    """Check the rounded proposal against every supplied benchmark and hard rule."""

    def __init__(self, bundle: Mapping[str, object], policy: Mapping[str, object]):
        self.context = DecisionContext(bundle)
        errors = DecisionPolicyValidator(bundle).validate(policy)
        if errors:
            raise DecisionToolError("DecisionPolicy 驗證失敗：" + "；".join(errors))
        self.policy = dict(policy)

    def run(
        self, proposal: Mapping[str, object], scenario_result: Mapping[str, object]
    ) -> Dict[str, object]:
        checks: List[Dict[str, object]] = []
        allocation = proposal["allocation_proposal"]
        positions = allocation["positions"]
        weights = {
            str(item["symbol"]).upper(): decimal_value(item["weight"], "weight")
            for item in positions
        }
        status_bundle = self.context.bundle.get("trading_status_bundle")
        assessment = self.context.bundle.get("tradability_assessment")
        status_by_symbol = {}
        if isinstance(status_bundle, Mapping) and isinstance(assessment, Mapping):
            status_by_symbol = {
                str(item.get("symbol", "")).upper(): str(item.get("state", "unknown"))
                for item in assessment.get("symbols", [])
                if isinstance(item, Mapping) and item.get("symbol")
            }
            tradable = {symbol for symbol, state in status_by_symbol.items() if state == "allowed"}
            not_tradable = {symbol for symbol, state in status_by_symbol.items() if state == "blocked"}
        else:
            # 相容既有 fixture；正式 Snapshot 的同名欄位是計數，不能進入此分支。
            tradable = {
                str(value).upper() for value in self.context.snapshot.get("tradable_symbols", [])
            }
            not_tradable = {
                str(value).upper() for value in self.context.snapshot.get("not_tradable_symbols", [])
            }
        universe = set(self.context.universe)
        self._check(checks, "UNIVERSE", set(weights).issubset(universe), sorted(set(weights) - universe))
        explicit_status = bool(status_by_symbol) or bool(tradable or not_tradable)
        self._check(checks, "TRADABILITY_AVAILABLE", explicit_status, [] if explicit_status else ["缺少明確可交易狀態"])
        if status_by_symbol:
            unknown = sorted(symbol for symbol in universe if status_by_symbol.get(symbol) == "unknown")
            missing = sorted(universe - set(status_by_symbol))
            self._check(
                checks,
                "TRADABILITY_COVERAGE",
                not unknown and not missing,
                {"unknown": unknown, "missing": missing},
            )
        order_symbols = {
            str(item.get("symbol", "")).upper()
            for item in proposal.get("order_proposal", {}).get("orders", [])
        }
        checked_symbols = order_symbols if status_by_symbol else set(weights) | order_symbols
        invalid_tradability = sorted(
            symbol for symbol in checked_symbols
            if symbol in not_tradable or (tradable and symbol not in tradable)
        )
        self._check(checks, "TRADABLE", not invalid_tradability, invalid_tradability)
        count = len(positions)
        minimum = int(self.policy["min_positions"])
        maximum = int(self.policy["max_positions"])
        self._check(checks, "POSITION_COUNT", minimum <= count <= maximum, {"actual": count, "min": minimum, "max": maximum})
        cash_weight = decimal_value(allocation["cash_weight"], "cash_weight")
        cash_ceiling = decimal_value(self.policy["cash_weight_ceiling"], "cash_weight_ceiling")
        self._check(checks, "CASH_WEIGHT", cash_weight < cash_ceiling, {"actual": _rate(cash_weight), "must_be_below": _rate(cash_ceiling)})
        default_limit = decimal_value(self.policy["max_stock_weight"], "max_stock_weight")
        special = self.policy.get("special_weight_limits", {})
        violations = []
        for symbol, weight in weights.items():
            limit = decimal_value(special.get(symbol, default_limit), "weight_limit") if isinstance(special, Mapping) else default_limit
            if weight > limit:
                violations.append({"symbol": symbol, "actual": _rate(weight), "limit": _rate(limit)})
        self._check(checks, "STOCK_WEIGHT", not violations, violations)
        sector_by_symbol = {
            str(symbol).upper(): str(sector)
            for symbol, sector in self.policy.get("sector_by_symbol", {}).items()
        }
        missing_sectors = sorted(set(weights) - set(sector_by_symbol))
        self._check(checks, "SECTOR_CLASSIFICATION", not missing_sectors, missing_sectors)
        sector_weights: Dict[str, Decimal] = {}
        for symbol, weight in weights.items():
            if symbol in sector_by_symbol:
                sector = sector_by_symbol[symbol]
                sector_weights[sector] = sector_weights.get(sector, Decimal("0")) + weight
        sector_limit = decimal_value(self.policy["max_sector_weight"], "max_sector_weight")
        sector_violations = [
            {"sector": sector, "actual": _rate(weight), "limit": _rate(sector_limit)}
            for sector, weight in sorted(sector_weights.items())
            if weight > sector_limit
        ]
        self._check(checks, "SECTOR_WEIGHT", not sector_violations, sector_violations)
        turnover = decimal_value(proposal["order_proposal"]["turnover_rate"], "turnover_rate")
        turnover_limit = decimal_value(self.policy["max_turnover_rate"], "max_turnover_rate")
        self._check(checks, "TURNOVER", turnover <= turnover_limit, {"actual": _rate(turnover), "limit": _rate(turnover_limit)})
        self._check(checks, "NON_NEGATIVE_CASH", decimal_value(allocation["cash"], "cash") >= 0, allocation["cash"])
        scenario_checks = []
        required_benchmarks = set(self.context.bundle["rules"]["required_benchmark_ids"])
        supplied_benchmarks = {
            str(item["benchmark_id"]): item for item in self.context.bundle["benchmarks"]
        }
        minimum_active = decimal_value(self.policy["minimum_active_share"], "minimum_active_share")
        for scenario in scenario_result.get("scenarios", []):
            nav = decimal_value(scenario["nav"], "scenario.nav")
            cash = decimal_value(scenario["cash"], "scenario.cash")
            scenario_weights = {
                str(item["symbol"]).upper(): decimal_value(item["assumed_value"], "assumed_value") / nav
                for item in scenario.get("positions", [])
            } if nav > 0 else {}
            position_count = len(scenario_weights)
            stock_violations = []
            for symbol, weight in scenario_weights.items():
                limit = decimal_value(special.get(symbol, default_limit), "weight_limit") if isinstance(special, Mapping) else default_limit
                if weight > limit:
                    stock_violations.append(symbol)
            scenario_sector: Dict[str, Decimal] = {}
            for symbol, weight in scenario_weights.items():
                if symbol in sector_by_symbol:
                    scenario_sector[sector_by_symbol[symbol]] = scenario_sector.get(sector_by_symbol[symbol], Decimal("0")) + weight
            active_results = []
            for benchmark_id in sorted(required_benchmarks):
                benchmark = supplied_benchmarks.get(benchmark_id)
                if benchmark is None:
                    active_results.append({"benchmark_id": benchmark_id, "passed": False, "error": "missing"})
                    continue
                benchmark_weights = {
                    str(symbol).upper(): decimal_value(value, "benchmark weight")
                    for symbol, value in benchmark["weights"].items()
                }
                symbols = set(scenario_weights) | set(benchmark_weights)
                active = Decimal("0.5") * sum(
                    abs(scenario_weights.get(symbol, Decimal("0")) - benchmark_weights.get(symbol, Decimal("0")))
                    for symbol in symbols
                )
                active_results.append({
                    "benchmark_id": benchmark_id,
                    "version": benchmark["version"],
                    "active_share": _rate(active),
                    "minimum": _rate(minimum_active),
                    "passed": active >= minimum_active,
                })
            scenario_checks.append({
                "name": scenario["name"],
                "non_negative_cash": cash >= 0,
                "positive_nav": nav > 0,
                "cash_weight_ok": nav > 0 and cash / nav < cash_ceiling,
                "buying_power_ok": decimal_value(scenario["buying_power_shortfall"], "buying_power_shortfall") == 0,
                "position_count_ok": minimum <= position_count <= maximum,
                "stock_weight_ok": not stock_violations,
                "sector_weight_ok": all(value <= sector_limit for value in scenario_sector.values()),
                "tradability_ok": not any(
                    symbol in not_tradable or (tradable and symbol not in tradable)
                    for symbol in scenario_weights
                ),
                "active_share_results": active_results,
                "passed": cash >= 0 and nav > 0 and cash / nav < cash_ceiling
                and decimal_value(scenario["buying_power_shortfall"], "buying_power_shortfall") == 0
                and minimum <= position_count <= maximum
                and not stock_violations
                and all(value <= sector_limit for value in scenario_sector.values())
                and not any(symbol in not_tradable or (tradable and symbol not in tradable) for symbol in scenario_weights)
                and bool(active_results) and all(item["passed"] for item in active_results),
            })
        self._check(
            checks,
            "SCENARIOS",
            len(scenario_checks) == 3 and all(item["passed"] for item in scenario_checks),
            scenario_checks,
        )
        unfilled_forced = [
            item
            for scenario in scenario_result.get("scenarios", [])
            for item in scenario.get("unfilled_orders", [])
            if item.get("intent") == "forced_exit" and int(item.get("unfilled_shares", 0)) > 0
        ]
        self._check(
            checks,
            "FORCED_EXIT_LIQUIDITY",
            not unfilled_forced,
            unfilled_forced,
        )

        benchmark_results = []
        for benchmark_id in sorted(required_benchmarks):
            benchmark = supplied_benchmarks.get(benchmark_id)
            if benchmark is None:
                benchmark_results.append({"benchmark_id": benchmark_id, "passed": False, "error": "missing"})
                continue
            benchmark_weights = {
                str(symbol).upper(): decimal_value(value, "benchmark weight")
                for symbol, value in benchmark["weights"].items()
            }
            symbols = set(weights) | set(benchmark_weights)
            active = Decimal("0.5") * sum(
                abs(weights.get(symbol, Decimal("0")) - benchmark_weights.get(symbol, Decimal("0")))
                for symbol in symbols
            )
            benchmark_results.append(
                {
                    "benchmark_id": benchmark["benchmark_id"],
                    "version": benchmark["version"],
                    "active_share": _rate(active),
                    "minimum": _rate(minimum_active),
                    "passed": active >= minimum_active,
                }
            )
        self._check(
            checks,
            "ACTIVE_SHARE_ALL",
            len(benchmark_results) == len(required_benchmarks)
            and bool(benchmark_results)
            and all(item["passed"] for item in benchmark_results),
            benchmark_results,
        )
        passed = all(bool(check["passed"]) for check in checks)
        body: Dict[str, object] = {
            "schema_version": DECISION_SCHEMA_VERSION,
            "guard_id": "",
            "bundle_id": self.context.bundle_id,
            "snapshot_id": self.context.snapshot_id,
            "decision_cutoff": self.context.decision_cutoff,
            "bundle_hash": self.context.bundle_hash,
            "proposal_id": proposal["proposal_id"],
            "scenario_id": scenario_result["scenario_id"],
            "policy_id": self.policy["policy_id"],
            "policy_hash": self.policy["content_sha256"],
            "engine_version": GUARD_ENGINE_VERSION,
            "status": "passed" if passed else "failed",
            "passed": passed,
            "checks": checks,
            "benchmark_results": benchmark_results,
        }
        body["guard_id"] = "guard:" + canonical_sha256(body)[:20]
        body["content_sha256"] = artifact_content_sha256(body)
        return body

    @staticmethod
    def _check(checks: List[Dict[str, object]], rule_id: str, passed: bool, details: object) -> None:
        checks.append({"rule_id": rule_id, "passed": passed, "details": details})


class GuardValidator:
    def __init__(self, bundle: Mapping[str, object], policy: Mapping[str, object]):
        self.engine = CompetitionGuardV2(bundle, policy)

    def validate(
        self,
        proposal: Mapping[str, object],
        scenario: Mapping[str, object],
        result: Mapping[str, object],
    ) -> List[str]:
        return [] if dict(result) == self.engine.run(proposal, scenario) else ["GuardResult 與確定性重算結果不一致"]


class RiskReviewValidator:
    """Keep the Portfolio Risk Agent inside a structured, bounded review contract."""

    ALLOWED = {
        "schema_version",
        "review_id",
        "bundle_id",
        "snapshot_id",
        "decision_cutoff",
        "bundle_hash",
        "proposal_id",
        "scenario_id",
        "guard_id",
        "revision_index",
        "decision",
        "rationale",
        "evidence_ids",
        "risk_flags",
        "revision_actions",
        "unresolved_questions",
        "status",
        "errors",
        "content_sha256",
    }

    def __init__(self, bundle: Mapping[str, object], policy: Mapping[str, object]):
        self.context = DecisionContext(bundle)
        self.policy = dict(policy)

    def validate(
        self,
        proposal: Mapping[str, object],
        scenario: Mapping[str, object],
        guard: Mapping[str, object],
        review: Mapping[str, object],
    ) -> List[str]:
        errors: List[str] = []
        reject_unknown_fields(review, self.ALLOWED, "RiskReview", errors)
        for field in (
            "schema_version", "review_id", "bundle_id", "snapshot_id", "decision_cutoff",
            "bundle_hash", "proposal_id", "scenario_id", "guard_id", "decision",
            "rationale", "status", "content_sha256",
        ):
            try:
                required_string(review, field)
            except DecisionToolError as error:
                errors.append(str(error))
        expected = {
            "bundle_id": self.context.bundle_id,
            "snapshot_id": self.context.snapshot_id,
            "decision_cutoff": self.context.decision_cutoff,
            "bundle_hash": self.context.bundle_hash,
            "proposal_id": proposal.get("proposal_id"),
            "scenario_id": scenario.get("scenario_id"),
            "guard_id": guard.get("guard_id"),
        }
        for field, value in expected.items():
            if review.get(field) != value:
                errors.append("RiskReview.%s 不一致" % field)
        if review.get("schema_version") != DECISION_SCHEMA_VERSION:
            errors.append("RiskReview.schema_version 必須為 %s" % DECISION_SCHEMA_VERSION)
        if review.get("content_sha256") != artifact_content_sha256(review):
            errors.append("RiskReview.content_sha256 與內容不一致")
        if review.get("status") != "completed":
            errors.append("只有 status=completed 的 RiskReview 可進入決策")
        if review.get("decision") not in RISK_DECISIONS:
            errors.append("RiskReview.decision 必須是 approve、revise 或 reject")
        if guard.get("passed") is not True and review.get("decision") != "reject":
            errors.append("CompetitionGuard 硬性規則失敗時 RiskReview 只能 reject")
        index = review.get("revision_index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index > int(self.policy["max_revisions"]):
            errors.append("RiskReview.revision_index 超過允許範圍")
        elif index != proposal.get("revision_count") and review.get("decision") != "revise":
            errors.append("非 revise 審查的 revision_index 必須等於提案版本")
        elif review.get("decision") == "revise" and index != int(proposal.get("revision_count", 0)) + 1:
            errors.append("revise 的 revision_index 必須是下一個連續版本")
        for field in ("evidence_ids", "risk_flags", "unresolved_questions", "errors"):
            try:
                values = string_list(review, field)
                if field == "evidence_ids":
                    if not values:
                        errors.append("RiskReview.evidence_ids 不得為空")
                    unknown = sorted(set(values) - set(self.context.evidence_symbols))
                    if unknown:
                        errors.append("RiskReview.evidence_ids 引用不存在：%s" % ", ".join(unknown))
            except DecisionToolError as error:
                errors.append(str(error))
        if review.get("status") == "completed" and review.get("errors"):
            errors.append("completed RiskReview 不得同時包含 errors")
        actions = review.get("revision_actions")
        if not isinstance(actions, list):
            errors.append("RiskReview.revision_actions 必須是陣列")
            actions = []
        if review.get("decision") == "revise" and not actions:
            errors.append("revise 必須包含 revision_actions")
        if review.get("decision") != "revise" and actions:
            errors.append("只有 revise 可以包含 revision_actions")
        for action_index, action in enumerate(actions):
            self._validate_action(action, action_index, proposal, errors)
        return errors

    def _validate_action(
        self,
        action: object,
        index: int,
        proposal: Mapping[str, object],
        errors: List[str],
    ) -> None:
        prefix = "revision_actions[%d]" % index
        if not isinstance(action, Mapping):
            errors.append("%s 必須是物件" % prefix)
            return
        reject_unknown_fields(action, {"type", "symbol", "value", "reason"}, prefix, errors)
        action_type = action.get("type")
        if action_type not in REVISION_TYPES:
            errors.append("%s.type 不在允許清單" % prefix)
            return
        try:
            required_string(action, "reason")
        except DecisionToolError as error:
            errors.append("%s.%s" % (prefix, error))
        if action_type == "remove_candidate":
            symbol = str(action.get("symbol", "")).upper()
            order_symbols = {
                str(item["symbol"]).upper()
                for item in proposal.get("order_proposal", {}).get("orders", [])
                if item.get("side") == "buy"
            }
            if symbol not in order_symbols:
                errors.append("%s 只能移除本提案的買進候選" % prefix)
        else:
            try:
                value = decimal_value(action.get("value"), "%s.value" % prefix)
                if value < 0 or value > 1:
                    errors.append("%s.value 必須介於 0 與 1" % prefix)
                effective = proposal.get("effective_policy", {})
                field_by_type = {
                    "increase_cash_buffer": "cash_buffer_rate",
                    "reduce_max_stock_weight": "max_stock_weight",
                    "reduce_turnover_limit": "max_turnover_rate",
                }
                field = field_by_type[action_type]
                current = decimal_value(effective.get(field), "effective_policy.%s" % field)
                if action_type == "increase_cash_buffer" and value < current:
                    errors.append("%s 不得降低現金緩衝" % prefix)
                if action_type != "increase_cash_buffer" and value > current:
                    errors.append("%s 不得提高風險上限" % prefix)
            except DecisionToolError as error:
                errors.append(str(error))


def revision_effects(
    review: Mapping[str, object], previous_proposal: Mapping[str, object]
) -> Dict[str, object]:
    """Convert one validated review into deterministic proposal inputs."""
    excluded: Set[str] = {
        str(value).upper() for value in previous_proposal.get("excluded_symbols", [])
    }
    effective = previous_proposal.get("effective_policy", {})
    overrides = {
        key: effective[key]
        for key in (
            "cash_buffer_rate", "default_target_weight", "trim_fraction",
            "max_turnover_rate", "max_stock_weight", "max_positions",
        )
        if isinstance(effective, Mapping) and key in effective
    }
    for action in review.get("revision_actions", []):
        action_type = action["type"]
        if action_type == "remove_candidate":
            excluded.add(str(action["symbol"]).upper())
        elif action_type == "increase_cash_buffer":
            old = decimal_value(overrides["cash_buffer_rate"], "cash_buffer_rate")
            value = decimal_value(action["value"], "value")
            if value < old:
                raise DecisionToolError("increase_cash_buffer 不得降低現金緩衝")
            overrides["cash_buffer_rate"] = str(value)
        elif action_type == "reduce_max_stock_weight":
            old = decimal_value(overrides["max_stock_weight"], "max_stock_weight")
            value = decimal_value(action["value"], "value")
            if value > old:
                raise DecisionToolError("reduce_max_stock_weight 不得提高上限")
            overrides["max_stock_weight"] = str(value)
        elif action_type == "reduce_turnover_limit":
            old = decimal_value(overrides["max_turnover_rate"], "max_turnover_rate")
            value = decimal_value(action["value"], "value")
            if value > old:
                raise DecisionToolError("reduce_turnover_limit 不得提高換手上限")
            overrides["max_turnover_rate"] = str(value)
    return {"excluded_symbols": excluded, "overrides": overrides}
