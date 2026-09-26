"""Point-in-time contracts shared by portfolio decision agents."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from etf_agent.core import canonical_sha256, content_sha256, parse_aware_time, parse_decimal
from etf_agent.perception import (
    MarketPerceptionResultValidator,
    PerceptionDataTools,
    PerceptionToolError,
)
from etf_agent.research import ResearchResultValidator, ResearchToolError
from etf_agent.data.trading_status import TradingStatusBundleValidator


DECISION_SCHEMA_VERSION = "1.0"
INTENTS = {
    "buy",
    "add",
    "hold",
    "trim",
    "exit",
    "forced_exit",
    "exclude",
    "no_trade",
}


class DecisionToolError(ValueError):
    """Raised when a decision artifact cannot be used safely."""


def parse_time(value: object, field: str) -> datetime:
    return parse_aware_time(value, field, error=DecisionToolError)


def required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DecisionToolError("缺少必要字串欄位：%s" % field)
    return value.strip()


def string_list(payload: Mapping[str, object], field: str) -> List[str]:
    value = payload.get(field)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise DecisionToolError("%s 必須是字串陣列" % field)
    return [item.strip() for item in value]


def decimal_value(value: object, field: str) -> Decimal:
    return parse_decimal(value, field, error=DecisionToolError, parse_message="%(field)s 無法解析為數值")


def decision_bundle_sha256(bundle: Mapping[str, object]) -> str:
    payload = dict(bundle)
    payload.pop("bundle_sha256", None)
    return canonical_sha256(payload)


def decision_rules_sha256(rules: Mapping[str, object]) -> str:
    payload = dict(rules)
    payload.pop("config_sha256", None)
    return canonical_sha256(payload)


def artifact_content_sha256(artifact: Mapping[str, object]) -> str:
    return content_sha256(artifact)


def reject_unknown_fields(
    payload: Mapping[str, object],
    allowed: Set[str],
    prefix: str,
    errors: List[str],
) -> None:
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        errors.append("%s 含未允許欄位：%s" % (prefix, ", ".join(unknown)))


class DecisionInputValidator:
    """Validate the immutable input shared by every decision sub-agent."""

    def __init__(self, bundle: Mapping[str, object]):
        self.bundle = dict(bundle)
        self.snapshot = (
            dict(bundle.get("snapshot", {}))
            if isinstance(bundle.get("snapshot"), Mapping)
            else {}
        )
        self.cutoff: Optional[datetime] = None

    def validate(self) -> List[str]:
        errors: List[str] = []
        reject_unknown_fields(
            self.bundle,
            {
                "schema_version",
                "bundle_id",
                "snapshot_id",
                "decision_cutoff",
                "bundle_sha256",
                "snapshot_sha256",
                "snapshot",
                "account_snapshot",
                "rules",
                "benchmarks",
                "price_series",
                "research_results",
                "perception_inputs",
                "trading_status_bundle",
                "tradability_assessment",
            },
            "DecisionInputBundle",
            errors,
        )
        for field in (
            "schema_version",
            "bundle_id",
            "snapshot_id",
            "decision_cutoff",
            "bundle_sha256",
        ):
            try:
                required_string(self.bundle, field)
            except DecisionToolError as error:
                errors.append(str(error))
        if self.bundle.get("schema_version") != DECISION_SCHEMA_VERSION:
            errors.append("schema_version 必須為 %s" % DECISION_SCHEMA_VERSION)
        if self.bundle.get("bundle_sha256") != decision_bundle_sha256(self.bundle):
            errors.append("bundle_sha256 與 DecisionInputBundle 內容不一致")
        try:
            self.cutoff = parse_time(self.bundle.get("decision_cutoff"), "decision_cutoff")
        except DecisionToolError as error:
            errors.append(str(error))

        self._validate_snapshot(errors)
        self._validate_trading_status(errors)
        self._validate_rules(errors)
        universe, price_evidence = self._universe_and_price_evidence(errors)
        self._validate_account(universe, errors)
        self._validate_benchmarks(errors)
        self._validate_price_series(universe, price_evidence, errors)
        self._validate_research_results(errors)
        self._validate_perception_inputs(errors)
        self._validate_global_evidence_ids(errors)
        return errors

    def _validate_trading_status(self, errors: List[str]) -> None:
        bundle = self.bundle.get("trading_status_bundle")
        assessment = self.bundle.get("tradability_assessment")
        if bundle is None and assessment is None:
            # 舊 fixture 仍可用明確的股票名單測試；研究 Snapshot 的整數計數
            # 不得被風控當作集合，正式輸入須提供成對狀態包。
            for field in ("tradable_symbols", "not_tradable_symbols"):
                value = self.snapshot.get(field)
                if value is not None and not isinstance(value, list):
                    errors.append("snapshot.%s 是計數；正式決策需提供 trading_status_bundle" % field)
            return
        if not isinstance(bundle, Mapping) or not isinstance(assessment, Mapping):
            errors.append("trading_status_bundle 與 tradability_assessment 必須成對存在")
            return
        status_errors = TradingStatusBundleValidator().validate(bundle, assessment)
        errors.extend("trading_status：%s" % error for error in status_errors)
        if bundle.get("snapshot_id") != self.bundle.get("snapshot_id"):
            errors.append("trading_status_bundle.snapshot_id 與 DecisionInputBundle 不一致")
        try:
            if parse_time(bundle.get("decision_cutoff"), "trading_status_bundle.decision_cutoff") != self.cutoff:
                errors.append("trading_status_bundle.decision_cutoff 不一致")
            if parse_time(assessment.get("decision_cutoff"), "tradability_assessment.decision_cutoff") != self.cutoff:
                errors.append("tradability_assessment.decision_cutoff 不一致")
        except DecisionToolError as error:
            errors.append(str(error))

    def _validate_snapshot(self, errors: List[str]) -> None:
        if not self.snapshot:
            errors.append("snapshot 必須是物件")
            return
        reject_unknown_fields(
            self.snapshot,
            {
                "snapshot_id",
                "decision_cutoff",
                "universe_version",
                "created_at",
                "usable",
                "quality_flags",
                "latest_trade_date",
                "universe_size",
                "latest_price_symbols",
                "universe_validation_id",
                "universe_validation_status",
                "tradable_symbols",
                "not_tradable_symbols",
                "universe_mismatches",
                "latest_prices",
                "source_evidence",
                "documents",
            },
            "snapshot",
            errors,
        )
        if self.snapshot.get("usable") is not True:
            errors.append("Snapshot 不可用")
        if self.snapshot.get("snapshot_id") != self.bundle.get("snapshot_id"):
            errors.append("snapshot_id 與內嵌 Snapshot 不一致")
        try:
            snapshot_cutoff = parse_time(
                self.snapshot.get("decision_cutoff"), "snapshot.decision_cutoff"
            )
            if self.cutoff is not None and snapshot_cutoff != self.cutoff:
                errors.append("decision_cutoff 與內嵌 Snapshot 不一致")
        except DecisionToolError as error:
            errors.append(str(error))
        expected_hash = canonical_sha256(self.snapshot)
        if self.bundle.get("snapshot_sha256") != expected_hash:
            errors.append("snapshot_sha256 與內嵌 Snapshot 內容不一致")
        evidence = self.snapshot.get("source_evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append("Snapshot 缺少 source_evidence")
        elif len(
            {
                str(item.get("evidence_id"))
                for item in evidence
                if isinstance(item, Mapping) and item.get("evidence_id")
            }
        ) != len(evidence):
            errors.append("Snapshot source_evidence 的 evidence_id 必須存在且唯一")
        if isinstance(evidence, list):
            for index, item in enumerate(evidence):
                if isinstance(item, Mapping):
                    reject_unknown_fields(
                        item,
                        {
                            "evidence_id",
                            "source",
                            "authority",
                            "data_type",
                            "url",
                            "content_as_of",
                            "fetched_at",
                            "content_sha256",
                            "raw_payload_id",
                            "published_at",
                            "archive_url",
                        },
                        "snapshot.source_evidence[%d]" % index,
                        errors,
                    )
        documents = self.snapshot.get("documents", [])
        if not isinstance(documents, list):
            errors.append("snapshot.documents 必須是陣列")
        else:
            for index, item in enumerate(documents):
                if isinstance(item, Mapping):
                    reject_unknown_fields(
                        item,
                        {
                            "document_id",
                            "source",
                            "external_id",
                            "version",
                            "document_type",
                            "symbol",
                            "title",
                            "body",
                            "source_url",
                            "published_at",
                            "available_at",
                            "content_sha256",
                            "source_evidence_id",
                            "monthly_revenue",
                            "financial_statement",
                        },
                        "snapshot.documents[%d]" % index,
                        errors,
                    )

    def _universe_and_price_evidence(
        self, errors: List[str]
    ) -> Tuple[Set[str], Dict[str, str]]:
        prices = self.snapshot.get("latest_prices")
        if not isinstance(prices, list) or not prices:
            errors.append("Snapshot 缺少 latest_prices")
            return set(), {}
        universe: Set[str] = set()
        evidence: Dict[str, str] = {}
        known_evidence = {
            str(item.get("evidence_id"))
            for item in self.snapshot.get("source_evidence", [])
            if isinstance(item, Mapping) and item.get("evidence_id")
        }
        for index, row in enumerate(prices):
            if not isinstance(row, Mapping):
                errors.append("snapshot.latest_prices[%d] 必須是物件" % index)
                continue
            reject_unknown_fields(
                row,
                {
                    "symbol",
                    "code",
                    "trade_date",
                    "open_price",
                    "high_price",
                    "low_price",
                    "close_price",
                    "analysis_close_price",
                    "adjusted_close_price",
                    "price_change",
                    "volume_shares",
                    "trade_value",
                    "transactions",
                    "source",
                    "source_evidence_id",
                },
                "snapshot.latest_prices[%d]" % index,
                errors,
            )
            symbol = str(row.get("symbol", "")).upper()
            evidence_id = str(row.get("source_evidence_id", ""))
            if not symbol or not evidence_id:
                errors.append("snapshot.latest_prices[%d] 缺少 symbol 或證據" % index)
                continue
            if evidence_id not in known_evidence:
                errors.append(
                    "snapshot.latest_prices[%d] 引用不存在：%s" % (index, evidence_id)
                )
            if symbol in universe:
                errors.append("Snapshot 最新行情股票不得重複：%s" % symbol)
            universe.add(symbol)
            evidence[symbol] = evidence_id
        size = self.snapshot.get("universe_size")
        if not isinstance(size, int) or size != len(universe):
            errors.append("Snapshot universe_size 與最新行情股票數不一致")
        return universe, evidence

    def _validate_account(self, universe: Set[str], errors: List[str]) -> None:
        account = self.bundle.get("account_snapshot")
        if not isinstance(account, Mapping):
            errors.append("account_snapshot 必須是物件")
            return
        reject_unknown_fields(
            account,
            {
                "account_id",
                "available_at",
                "valuation_at",
                "source_evidence_id",
                "cash",
                "settled_cash",
                "unsettled_cash",
                "nav",
                "positions",
            },
            "account_snapshot",
            errors,
        )
        for field in ("account_id", "available_at", "valuation_at", "source_evidence_id"):
            try:
                required_string(account, field)
            except DecisionToolError as error:
                errors.append("account_snapshot.%s" % error)
        try:
            available = parse_time(account.get("available_at"), "account_snapshot.available_at")
            if self.cutoff is not None and available > self.cutoff:
                errors.append("帳戶快照晚於 decision_cutoff")
            valuation = parse_time(account.get("valuation_at"), "account_snapshot.valuation_at")
            if self.cutoff is not None and valuation > self.cutoff:
                errors.append("帳戶估值時間晚於 decision_cutoff")
        except DecisionToolError as error:
            errors.append(str(error))
        try:
            cash = decimal_value(account.get("cash"), "account_snapshot.cash")
            settled = decimal_value(account.get("settled_cash"), "account_snapshot.settled_cash")
            unsettled = decimal_value(account.get("unsettled_cash"), "account_snapshot.unsettled_cash")
            nav = decimal_value(account.get("nav"), "account_snapshot.nav")
            if cash < 0 or settled < 0 or unsettled < 0:
                errors.append("account_snapshot 現金欄位不得為負")
            if cash != settled + unsettled:
                errors.append("account_snapshot.cash 必須等於 settled_cash 加 unsettled_cash")
            if nav <= 0:
                errors.append("account_snapshot.nav 必須大於 0")
            if cash > nav:
                errors.append("account_snapshot.cash 不得大於 nav")
        except DecisionToolError as error:
            errors.append(str(error))
        positions = account.get("positions")
        if not isinstance(positions, list):
            errors.append("account_snapshot.positions 必須是陣列")
            return
        seen: Set[str] = set()
        for index, position in enumerate(positions):
            prefix = "account_snapshot.positions[%d]" % index
            if not isinstance(position, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            reject_unknown_fields(
                position,
                {"symbol", "shares", "average_cost"},
                prefix,
                errors,
            )
            symbol = str(position.get("symbol", "")).upper()
            if not symbol:
                errors.append("%s.symbol 缺少必要字串" % prefix)
            elif symbol in seen:
                errors.append("%s.symbol 不得重複" % prefix)
            elif universe and symbol not in universe:
                errors.append("%s.symbol 不在 Snapshot 交易池" % prefix)
            seen.add(symbol)
            shares = position.get("shares")
            if not isinstance(shares, int) or isinstance(shares, bool) or shares <= 0:
                errors.append("%s.shares 必須是正整數" % prefix)
            try:
                if decimal_value(position.get("average_cost"), "%s.average_cost" % prefix) <= 0:
                    errors.append("%s.average_cost 必須大於 0" % prefix)
            except DecisionToolError as error:
                errors.append(str(error))
        try:
            latest = {
                str(row.get("symbol", "")).upper(): decimal_value(
                    row.get("analysis_close_price"), "snapshot.analysis_close_price"
                )
                for row in self.snapshot.get("latest_prices", [])
                if isinstance(row, Mapping)
            }
            marked_nav = decimal_value(account.get("cash"), "account_snapshot.cash") + sum(
                decimal_value(position.get("shares"), "position.shares")
                * latest[str(position.get("symbol", "")).upper()]
                for position in positions
                if isinstance(position, Mapping)
                and str(position.get("symbol", "")).upper() in latest
            )
            stated_nav = decimal_value(account.get("nav"), "account_snapshot.nav")
            tolerance = decimal_value(
                self.bundle.get("rules", {}).get("max_nav_drift_rate"),
                "rules.max_nav_drift_rate",
            )
            drift = abs(stated_nav - marked_nav) / marked_nav if marked_nav else Decimal("1")
            if drift > tolerance:
                errors.append("account_snapshot.nav 與 cutoff 行情重算值差異超過允許範圍")
        except (DecisionToolError, KeyError, TypeError):
            pass

    def _validate_rules(self, errors: List[str]) -> None:
        rules = self.bundle.get("rules")
        if not isinstance(rules, Mapping):
            errors.append("rules 必須是物件")
            return
        allowed = {
            "version", "source_url", "published_at", "available_at", "config_sha256",
            "required_benchmark_ids", "lot_size", "commission_rate", "sell_tax_rate",
            "minimum_commission", "max_stock_weight", "special_weight_limits",
            "max_sector_weight", "min_positions", "max_positions",
            "cash_weight_must_be_below", "minimum_active_share", "reuse_sell_proceeds",
            "max_nav_drift_rate",
        }
        reject_unknown_fields(
            rules,
            allowed,
            "rules",
            errors,
        )
        for field in ("version", "source_url", "published_at", "available_at", "config_sha256"):
            try:
                required_string(rules, field)
            except DecisionToolError as error:
                errors.append("rules.%s" % error)
        try:
            published = parse_time(rules.get("published_at"), "rules.published_at")
            available = parse_time(rules.get("available_at"), "rules.available_at")
            if self.cutoff is not None and available > self.cutoff:
                errors.append("競賽規則版本晚於 decision_cutoff")
            if published > available:
                errors.append("rules.published_at 不得晚於 available_at")
        except DecisionToolError as error:
            errors.append(str(error))
        if rules.get("config_sha256") != decision_rules_sha256(rules):
            errors.append("rules.config_sha256 與規則內容不一致")
        try:
            required = string_list(rules, "required_benchmark_ids")
            if len(required) != len(set(required)):
                errors.append("rules.required_benchmark_ids 不得重複")
            if not required and decimal_value(
                rules.get("minimum_active_share"), "rules.minimum_active_share"
            ) != 0:
                errors.append("沒有必備 ETF 基準時，rules.minimum_active_share 必須為 0")
        except DecisionToolError as error:
            errors.append(str(error))
        for field in ("lot_size", "min_positions", "max_positions"):
            value = rules.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append("rules.%s 必須是非負整數" % field)
        if rules.get("lot_size") != 1000:
            errors.append("rules.lot_size 必須固定為 1000")
        if isinstance(rules.get("min_positions"), int) and isinstance(
            rules.get("max_positions"), int
        ) and rules["min_positions"] > rules["max_positions"]:
            errors.append("rules.min_positions 不得大於 max_positions")
        for field in (
            "commission_rate", "sell_tax_rate", "max_stock_weight", "max_sector_weight",
            "cash_weight_must_be_below", "minimum_active_share", "max_nav_drift_rate",
        ):
            try:
                value = decimal_value(rules.get(field), "rules.%s" % field)
                if value < 0 or value > 1:
                    errors.append("rules.%s 必須介於 0 與 1" % field)
            except DecisionToolError as error:
                errors.append(str(error))
        try:
            if decimal_value(rules.get("minimum_commission"), "rules.minimum_commission") < 0:
                errors.append("rules.minimum_commission 不得為負")
        except DecisionToolError as error:
            errors.append(str(error))
        if rules.get("reuse_sell_proceeds") not in {True, False}:
            errors.append("rules.reuse_sell_proceeds 必須是布林值")
        limits = rules.get("special_weight_limits")
        if not isinstance(limits, Mapping):
            errors.append("rules.special_weight_limits 必須是物件")
        else:
            for symbol, raw in limits.items():
                try:
                    value = decimal_value(raw, "rules.special_weight_limits.%s" % symbol)
                    if value <= 0 or value > 1:
                        errors.append("rules.special_weight_limits.%s 超出範圍" % symbol)
                except DecisionToolError as error:
                    errors.append(str(error))

    def _validate_benchmarks(self, errors: List[str]) -> None:
        benchmarks = self.bundle.get("benchmarks")
        if not isinstance(benchmarks, list):
            errors.append("benchmarks 必須是陣列")
            return
        seen: Set[str] = set()
        for index, benchmark in enumerate(benchmarks):
            prefix = "benchmarks[%d]" % index
            if not isinstance(benchmark, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            reject_unknown_fields(
                benchmark,
                {"benchmark_id", "version", "available_at", "weights"},
                prefix,
                errors,
            )
            try:
                benchmark_id = required_string(benchmark, "benchmark_id")
                required_string(benchmark, "version")
            except DecisionToolError as error:
                errors.append("%s.%s" % (prefix, error))
                benchmark_id = ""
            if benchmark_id in seen:
                errors.append("%s.benchmark_id 不得重複" % prefix)
            seen.add(benchmark_id)
            try:
                available = parse_time(benchmark.get("available_at"), "%s.available_at" % prefix)
                if self.cutoff is not None and available > self.cutoff:
                    errors.append("%s 晚於 decision_cutoff" % prefix)
            except DecisionToolError as error:
                errors.append(str(error))
            weights = benchmark.get("weights")
            if not isinstance(weights, Mapping) or not weights:
                errors.append("%s.weights 必須是非空物件" % prefix)
                continue
            total = Decimal("0")
            for symbol, value in weights.items():
                if not isinstance(symbol, str) or not symbol.strip():
                    errors.append("%s.weights 股票代碼無效" % prefix)
                    continue
                try:
                    weight = decimal_value(value, "%s.weights.%s" % (prefix, symbol))
                    if weight < 0 or weight > 1:
                        errors.append("%s.weights.%s 必須介於 0 與 1" % (prefix, symbol))
                    total += weight
                except DecisionToolError as error:
                    errors.append(str(error))
            if total > Decimal("1.000001"):
                errors.append("%s.weights 總和不得大於 1" % prefix)
        rules = self.bundle.get("rules", {})
        required = set(rules.get("required_benchmark_ids", [])) if isinstance(rules, Mapping) else set()
        missing = sorted(required - seen)
        if missing:
            errors.append("benchmarks 缺少規則指定基準：%s" % ", ".join(missing))

    def _validate_price_series(
        self,
        universe: Set[str],
        price_evidence: Mapping[str, str],
        errors: List[str],
    ) -> None:
        series = self.bundle.get("price_series")
        if not isinstance(series, list):
            errors.append("price_series 必須是陣列")
            return
        seen: Set[str] = set()
        latest_by_symbol = {
            str(row.get("symbol", "")).upper(): row
            for row in self.snapshot.get("latest_prices", [])
            if isinstance(row, Mapping)
        }
        for index, item in enumerate(series):
            prefix = "price_series[%d]" % index
            if not isinstance(item, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            reject_unknown_fields(
                item,
                {"symbol", "evidence_id", "series_sha256", "bars"},
                prefix,
                errors,
            )
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                errors.append("%s.symbol 缺少必要字串" % prefix)
                continue
            if symbol in seen:
                errors.append("%s.symbol 不得重複" % prefix)
            seen.add(symbol)
            if universe and symbol not in universe:
                errors.append("%s.symbol 不在 Snapshot 交易池" % prefix)
            if item.get("evidence_id") != price_evidence.get(symbol):
                errors.append("%s.evidence_id 與 Snapshot 最新行情不一致" % prefix)
            bars = item.get("bars")
            if not isinstance(bars, list) or len(bars) < 2:
                errors.append("%s.bars 至少需要兩筆行情" % prefix)
                continue
            if item.get("series_sha256") != canonical_sha256(bars):
                errors.append("%s.series_sha256 與歷史行情內容不一致" % prefix)
            previous_date = ""
            for bar_index, bar in enumerate(bars):
                bar_prefix = "%s.bars[%d]" % (prefix, bar_index)
                if not isinstance(bar, Mapping):
                    errors.append("%s 必須是物件" % bar_prefix)
                    continue
                reject_unknown_fields(
                    bar,
                    {"trade_date", "available_at", "open", "high", "low", "close", "volume"},
                    bar_prefix,
                    errors,
                )
                trade_date = str(bar.get("trade_date", ""))
                try:
                    datetime.fromisoformat(trade_date)
                except ValueError:
                    errors.append("%s.trade_date 格式錯誤" % bar_prefix)
                if previous_date and trade_date <= previous_date:
                    errors.append("%s.trade_date 必須嚴格遞增" % bar_prefix)
                previous_date = trade_date
                try:
                    available = parse_time(bar.get("available_at"), "%s.available_at" % bar_prefix)
                    if self.cutoff is not None and available > self.cutoff:
                        errors.append("%s 使用 decision_cutoff 後行情" % bar_prefix)
                except DecisionToolError as error:
                    errors.append(str(error))
                values: Dict[str, Decimal] = {}
                for field in ("open", "high", "low", "close"):
                    try:
                        values[field] = decimal_value(bar.get(field), "%s.%s" % (bar_prefix, field))
                        if values[field] <= 0:
                            errors.append("%s.%s 必須大於 0" % (bar_prefix, field))
                    except DecisionToolError as error:
                        errors.append(str(error))
                if len(values) == 4 and (
                    values["high"] < max(values["open"], values["close"], values["low"])
                    or values["low"] > min(values["open"], values["close"], values["high"])
                ):
                    errors.append("%s OHLC 關係無效" % bar_prefix)
                try:
                    volume = int(bar.get("volume"))
                    if volume < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("%s.volume 必須是非負整數" % bar_prefix)
            latest = latest_by_symbol.get(symbol, {})
            if bars:
                last = bars[-1]
                if last.get("trade_date") != latest.get("trade_date"):
                    errors.append("%s 最後交易日與 Snapshot 不一致" % prefix)
                try:
                    if decimal_value(last.get("close"), "last.close") != decimal_value(
                        latest.get("analysis_close_price"), "snapshot.analysis_close_price"
                    ):
                        errors.append("%s 最後收盤價與 Snapshot 不一致" % prefix)
                except DecisionToolError as error:
                    errors.append("%s.%s" % (prefix, error))
        if universe and seen != universe:
            missing = sorted(universe - seen)
            extra = sorted(seen - universe)
            if missing:
                errors.append("price_series 缺少交易池股票：%s" % ", ".join(missing))
            if extra:
                errors.append("price_series 含交易池外股票：%s" % ", ".join(extra))

    def _validate_research_results(self, errors: List[str]) -> None:
        results = self.bundle.get("research_results", [])
        if not isinstance(results, list):
            errors.append("research_results 必須是陣列")
            return
        for index, result in enumerate(results):
            if not isinstance(result, Mapping):
                errors.append("research_results[%d] 必須是物件" % index)
                continue
            try:
                result_errors = ResearchResultValidator(self.snapshot).validate(result)
            except ResearchToolError as error:
                result_errors = [str(error)]
            errors.extend(
                "research_results[%d]：%s" % (index, error)
                for error in result_errors
            )

    def _validate_perception_inputs(self, errors: List[str]) -> None:
        inputs = self.bundle.get("perception_inputs", [])
        if not isinstance(inputs, list):
            errors.append("perception_inputs 必須是陣列")
            return
        for index, item in enumerate(inputs):
            if not isinstance(item, Mapping):
                errors.append("perception_inputs[%d] 必須是物件" % index)
                continue
            data_bundle = item.get("bundle")
            result = item.get("result")
            if not isinstance(data_bundle, Mapping) or not isinstance(result, Mapping):
                errors.append("perception_inputs[%d] 必須包含 bundle 與 result" % index)
                continue
            if data_bundle.get("snapshot_id") != self.bundle.get("snapshot_id"):
                errors.append("perception_inputs[%d] snapshot_id 不一致" % index)
            try:
                if parse_time(
                    data_bundle.get("decision_cutoff"),
                    "perception_inputs[%d].bundle.decision_cutoff" % index,
                ) != self.cutoff:
                    errors.append("perception_inputs[%d] decision_cutoff 不一致" % index)
                if parse_time(
                    result.get("decision_cutoff"),
                    "perception_inputs[%d].result.decision_cutoff" % index,
                ) != self.cutoff:
                    errors.append("perception_inputs[%d] result decision_cutoff 不一致" % index)
            except DecisionToolError as error:
                errors.append(str(error))
            try:
                tools = PerceptionDataTools(data_bundle)
                result_errors = MarketPerceptionResultValidator(tools).validate(result)
            except PerceptionToolError as error:
                result_errors = [str(error)]
            errors.extend(
                "perception_inputs[%d]：%s" % (index, error)
                for error in result_errors
            )

    def _validate_global_evidence_ids(self, errors: List[str]) -> None:
        locations: Dict[str, str] = {}

        def register(evidence_id: object, location: str) -> None:
            if not isinstance(evidence_id, str) or not evidence_id.strip():
                errors.append("%s 缺少 evidence_id" % location)
                return
            normalized = evidence_id.strip()
            previous = locations.get(normalized)
            if previous is not None:
                errors.append(
                    "evidence_id 跨輸入來源不得重複：%s（%s、%s）"
                    % (normalized, previous, location)
                )
            else:
                locations[normalized] = location

        for index, evidence in enumerate(self.snapshot.get("source_evidence", [])):
            if isinstance(evidence, Mapping):
                register(evidence.get("evidence_id"), "snapshot.source_evidence[%d]" % index)
        account = self.bundle.get("account_snapshot", {})
        if isinstance(account, Mapping):
            register(account.get("source_evidence_id"), "account_snapshot")
        for input_index, perception in enumerate(
            self.bundle.get("perception_inputs", [])
        ):
            if not isinstance(perception, Mapping):
                continue
            data_bundle = perception.get("bundle", {})
            if not isinstance(data_bundle, Mapping):
                continue
            for evidence_index, evidence in enumerate(
                data_bundle.get("source_evidence", [])
            ):
                if isinstance(evidence, Mapping):
                    register(
                        evidence.get("evidence_id"),
                        "perception_inputs[%d].bundle.source_evidence[%d]"
                        % (input_index, evidence_index),
                    )
        status_bundle = self.bundle.get("trading_status_bundle")
        if isinstance(status_bundle, Mapping):
            for evidence_index, evidence in enumerate(status_bundle.get("source_evidence", [])):
                if isinstance(evidence, Mapping):
                    register(
                        evidence.get("evidence_id"),
                        "trading_status_bundle.source_evidence[%d]" % evidence_index,
                    )


class DecisionContext:
    """Validated lookup view over one DecisionInputBundle."""

    def __init__(self, bundle: Mapping[str, object]):
        errors = DecisionInputValidator(bundle).validate()
        if errors:
            raise DecisionToolError("DecisionInputBundle 驗證失敗：" + "；".join(errors))
        self.bundle = dict(bundle)
        self.snapshot = dict(bundle["snapshot"])
        self.bundle_id = str(bundle["bundle_id"])
        self.snapshot_id = str(bundle["snapshot_id"])
        self.decision_cutoff = str(bundle["decision_cutoff"])
        self.bundle_hash = str(bundle["bundle_sha256"])
        self.price_series = {
            str(item["symbol"]).upper(): dict(item)
            for item in bundle["price_series"]
        }
        self.universe = set(self.price_series)
        account = dict(bundle["account_snapshot"])
        self.account = account
        self.held_symbols = {
            str(item["symbol"]).upper()
            for item in account.get("positions", [])
            if isinstance(item, Mapping) and int(item.get("shares", 0)) > 0
        }
        self.evidence_symbols = self._build_evidence_symbols()

    def _build_evidence_symbols(self) -> Dict[str, Optional[str]]:
        result: Dict[str, Optional[str]] = {}
        for collection in ("documents", "latest_prices"):
            for item in self.snapshot.get(collection, []):
                if isinstance(item, Mapping) and item.get("source_evidence_id"):
                    evidence_id = str(item["source_evidence_id"])
                    symbol = str(item.get("symbol", "")).upper() or None
                    previous = result.get(evidence_id)
                    if previous is not None and previous != symbol:
                        raise DecisionToolError("evidence_id 對應多個股票：%s" % evidence_id)
                    result[evidence_id] = symbol
        for perception in self.bundle.get("perception_inputs", []):
            if not isinstance(perception, Mapping):
                continue
            data_bundle = perception.get("bundle", {})
            if not isinstance(data_bundle, Mapping):
                continue
            for evidence in data_bundle.get("source_evidence", []):
                if isinstance(evidence, Mapping) and evidence.get("evidence_id"):
                    evidence_id = str(evidence["evidence_id"])
                    if evidence_id in result:
                        raise DecisionToolError("evidence_id 跨輸入來源重複：%s" % evidence_id)
                    result[evidence_id] = str(evidence.get("symbol", "")).upper() or None
        status_bundle = self.bundle.get("trading_status_bundle", {})
        if isinstance(status_bundle, Mapping):
            for record in status_bundle.get("records", []):
                if isinstance(record, Mapping) and record.get("evidence_id"):
                    evidence_id = str(record["evidence_id"])
                    if evidence_id in result:
                        raise DecisionToolError("evidence_id 跨輸入來源重複：%s" % evidence_id)
                    result[evidence_id] = str(record.get("symbol", "")).upper() or None
        account_evidence_id = str(self.account["source_evidence_id"])
        if account_evidence_id in result:
            raise DecisionToolError(
                "account evidence_id 與研究證據重複：%s" % account_evidence_id
            )
        result[account_evidence_id] = None
        return result

    def validate_evidence(
        self, evidence_ids: Sequence[str], symbol: str, prefix: str, errors: List[str]
    ) -> None:
        for evidence_id in evidence_ids:
            linked_symbol = self.evidence_symbols.get(evidence_id)
            if evidence_id not in self.evidence_symbols:
                errors.append("%s 引用不存在：%s" % (prefix, evidence_id))
            elif (
                evidence_id == str(self.account["source_evidence_id"])
                and symbol.upper() not in self.held_symbols
            ):
                errors.append("%s 帳戶證據只能引用於目前持股：%s" % (prefix, evidence_id))
            elif linked_symbol is not None and linked_symbol != symbol.upper():
                errors.append("%s 引用不屬於 %s：%s" % (prefix, symbol.upper(), evidence_id))
