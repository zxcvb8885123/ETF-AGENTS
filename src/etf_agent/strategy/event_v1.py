"""One-month event-driven stock selection and target-weight generation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


class StrategyInputError(ValueError):
    """Raised when a research snapshot cannot support a safe proposal."""


@dataclass(frozen=True)
class StockResearch:
    symbol: str
    industry: str
    published_at: str
    evidence_ids: Tuple[str, ...]
    days_to_catalyst: int
    fundamental_change: float
    persistence: float
    liquidity_percentile: float
    evidence_quality: float
    relative_return_1d: float
    close_location: float
    volume_ratio: float
    downside_volatility_percentile: float
    relative_strength_10d_percentile: float
    price_above_ma20: bool
    market_has_reacted: bool
    pre_event_runup_atr: float = 0.0
    post_event_move_atr: float = 0.0
    event_missed: bool = False
    one_off_gain: bool = False
    conflicting_evidence: bool = False


@dataclass(frozen=True)
class ScoredStock:
    symbol: str
    industry: str
    event_score: float
    defense_score: float
    confirmations: int
    market_has_reacted: bool
    selection_role: str
    target_weight: float
    evidence_ids: Tuple[str, ...]
    reasons: Tuple[str, ...]


@dataclass(frozen=True)
class StrategyProposal:
    as_of: str
    regime: str
    cash_weight: float
    positions: Tuple[ScoredStock, ...]
    warnings: Tuple[str, ...]

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _unit(value: float, field: str) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise StrategyInputError("%s 必須介於 0 與 1" % field)
    return number


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise StrategyInputError("無法解析時間：%s" % value) from error
    if parsed.tzinfo is None:
        raise StrategyInputError("時間必須包含時區：%s" % value)
    return parsed


class EventDrivenStrategy:
    def __init__(self, config_path: Path):
        self.config = json.loads(config_path.read_text(encoding="utf-8"))

    def run(
        self,
        *,
        as_of: str,
        breadth: float,
        equal_weight_above_ma20: bool,
        stocks: Iterable[StockResearch],
    ) -> StrategyProposal:
        cutoff = _parse_time(as_of)
        breadth = _unit(breadth, "breadth")
        rows = list(stocks)
        target_count = int(self.config["portfolio"]["target_positions"])
        if len({row.symbol.upper() for row in rows}) != len(rows):
            raise StrategyInputError("研究快照含有重複股票")
        if len(rows) < target_count:
            raise StrategyInputError("至少需要 %d 檔完整研究資料" % target_count)

        eligible_rows: List[StockResearch] = []
        unavailable: List[str] = []
        for row in rows:
            if not row.evidence_ids:
                unavailable.append(row.symbol.upper() + " 缺少 evidence_ids")
                continue
            if _parse_time(row.published_at) > cutoff:
                unavailable.append(row.symbol.upper() + " 資料晚於決策截止時間")
                continue
            self._validate_row(row)
            eligible_rows.append(row)
        if len(eligible_rows) < target_count:
            raise StrategyInputError(
                "截止時間前只有 %d 檔合格資料，無法建立 %d 檔組合"
                % (len(eligible_rows), target_count)
            )

        regime = self._regime(breadth, equal_weight_above_ma20)
        regime_config = self.config["regimes"][regime]
        scored = [self._score(row) for row in eligible_rows]
        event_config = self.config["event_score"]
        candidate_threshold = float(event_config["candidate_threshold"])
        max_attack_positions = min(15, target_count)

        attack = [
            item
            for item in scored
            if item[1] >= candidate_threshold
            and (item[0].market_has_reacted and item[3] >= 2 or not item[0].market_has_reacted)
        ]
        attack.sort(key=lambda item: (item[1], item[2]), reverse=True)
        attack = attack[:max_attack_positions]
        attack_symbols = {item[0].symbol.upper() for item in attack}

        defense = [item for item in scored if item[0].symbol.upper() not in attack_symbols]
        defense.sort(key=lambda item: item[2], reverse=True)
        defense = defense[: target_count - len(attack)]
        selected = attack + defense
        if len(selected) != target_count:
            raise StrategyInputError("合格候選不足，無法補足 %d 檔" % target_count)

        cash_weight = float(regime_config["cash_target"])
        stock_weight = 1.0 - cash_weight
        attack_budget = min(float(regime_config["attack_weight"]), stock_weight)
        defense_budget = stock_weight - attack_budget
        weights = self._allocate(selected, len(attack), attack_budget, defense_budget)

        positions: List[ScoredStock] = []
        for index, (row, event_score, defense_score, confirmations, reasons) in enumerate(selected):
            role = "attack" if index < len(attack) else "defense"
            weight = weights[row.symbol.upper()]
            # The starter cap applies only to an unreacted *event* position.
            # Defensive holdings are selected from price and risk features, so
            # they must remain fundable when the event candidate pool is empty.
            if role == "attack" and not row.market_has_reacted:
                starter_cap = float(self.config["portfolio"]["starter_position_max_weight"])
                weight = min(weight, starter_cap)
            positions.append(
                ScoredStock(
                    symbol=row.symbol.upper(),
                    industry=row.industry,
                    event_score=round(event_score, 2),
                    defense_score=round(defense_score, 2),
                    confirmations=confirmations,
                    market_has_reacted=row.market_has_reacted,
                    selection_role=role,
                    target_weight=round(weight, 6),
                    evidence_ids=row.evidence_ids,
                    reasons=reasons,
                )
            )

        # A capped starter leaves cash temporarily unassigned. Move it to reacted
        # positions while preserving all concentration limits.
        positions = self._redistribute_starter_cash(positions, stock_weight)
        warnings = tuple(unavailable)
        return StrategyProposal(as_of, regime, cash_weight, tuple(positions), warnings)

    @staticmethod
    def _validate_row(row: StockResearch) -> None:
        for field in (
            "fundamental_change",
            "persistence",
            "liquidity_percentile",
            "evidence_quality",
            "close_location",
            "downside_volatility_percentile",
            "relative_strength_10d_percentile",
        ):
            _unit(getattr(row, field), field)
        if row.volume_ratio < 0:
            raise StrategyInputError("volume_ratio 不得為負")

    def _regime(self, breadth: float, equal_weight_above_ma20: bool) -> str:
        if breadth >= float(self.config["regimes"]["bull"]["breadth_minimum"]):
            if equal_weight_above_ma20:
                return "bull"
        if breadth < float(self.config["regimes"]["bear"]["breadth_maximum"]):
            if not equal_weight_above_ma20:
                return "bear"
        return "neutral"

    def _score(
        self, row: StockResearch
    ) -> Tuple[StockResearch, float, float, int, Tuple[str, ...]]:
        event_config = self.config["event_score"]
        maximum_days = int(event_config["maximum_days_to_catalyst"])
        if -maximum_days <= row.days_to_catalyst <= maximum_days:
            timing = 1.0 - 0.4 * abs(row.days_to_catalyst) / maximum_days
        else:
            timing = 0.0

        confirmations = sum(
            (
                row.relative_return_1d > 0,
                row.close_location >= float(
                    self.config["price_confirmation"]["close_location_minimum"]
                ),
                row.volume_ratio
                >= float(self.config["price_confirmation"]["volume_vs_20d_median"]),
            )
        )
        confirmation_score = 10.0 * confirmations if row.market_has_reacted else 0.0
        score = (
            timing * float(event_config["event_timing"])
            + row.fundamental_change * float(event_config["fundamental_change"])
            + confirmation_score
            + row.persistence * float(event_config["persistence"])
            + row.liquidity_percentile * float(event_config["liquidity"])
            + row.evidence_quality * float(event_config["evidence_quality"])
        )
        penalty = 0.0
        penalty += min(row.pre_event_runup_atr / 3.0, 1.0) * 7.0
        chase_limit = float(self.config["price_confirmation"]["maximum_chase_atr_multiple"])
        if row.post_event_move_atr > chase_limit:
            penalty += min((row.post_event_move_atr - chase_limit) * 3.0, 6.0)
        penalty += 10.0 if row.event_missed else 0.0
        penalty += 5.0 if row.one_off_gain else 0.0
        penalty += 7.0 if row.conflicting_evidence else 0.0
        penalty = min(penalty, float(event_config["maximum_risk_penalty"]))
        score = max(0.0, score - penalty)

        defense_score = (
            (1.0 - row.downside_volatility_percentile) * 45.0
            + row.relative_strength_10d_percentile * 35.0
            + row.liquidity_percentile * 10.0
            + (10.0 if row.price_above_ma20 else 0.0)
        )
        reasons = (
            "事件分數 %.1f，風險扣分 %.1f" % (score, penalty),
            "價格確認 %d/3" % confirmations,
            "防守分數 %.1f" % defense_score,
        )
        return row, score, defense_score, confirmations, reasons

    def _symbol_cap(self, symbol: str) -> float:
        portfolio = self.config["portfolio"]
        if symbol.upper() == "2330.TW":
            return float(portfolio["tsmc_normal_max_weight"])
        return float(portfolio["default_max_weight"])

    def _allocate(
        self,
        selected: Sequence[Tuple[StockResearch, float, float, int, Tuple[str, ...]]],
        attack_count: int,
        attack_budget: float,
        defense_budget: float,
    ) -> Dict[str, float]:
        attack = selected[:attack_count]
        defense = selected[attack_count:]
        weights: Dict[str, float] = {item[0].symbol.upper(): 0.0 for item in selected}
        left = self._fill_bucket(weights, attack, attack_budget, use_event_score=True)
        left += self._fill_bucket(weights, defense, defense_budget, use_event_score=False)
        if left > 1e-9:
            left = self._fill_bucket(weights, selected, left, use_event_score=False)
        if left > 1e-7:
            raise StrategyInputError("個股權重上限使股票部位無法配置完成")
        return self._enforce_industry_caps(weights, selected)

    def _fill_bucket(
        self,
        weights: Dict[str, float],
        items: Sequence[Tuple[StockResearch, float, float, int, Tuple[str, ...]]],
        budget: float,
        *,
        use_event_score: bool,
    ) -> float:
        remaining = budget
        for _ in range(100):
            available = [item for item in items if weights[item[0].symbol.upper()] < self._symbol_cap(item[0].symbol) - 1e-12]
            if remaining <= 1e-10 or not available:
                break
            scores = [max(item[1] if use_event_score else item[2], 1.0) for item in available]
            total_score = sum(scores)
            spent = 0.0
            for item, score in zip(available, scores):
                symbol = item[0].symbol.upper()
                capacity = self._symbol_cap(symbol) - weights[symbol]
                addition = min(remaining * score / total_score, capacity)
                weights[symbol] += addition
                spent += addition
            if spent <= 1e-12:
                break
            remaining -= spent
        return max(remaining, 0.0)

    def _enforce_industry_caps(
        self,
        weights: Dict[str, float],
        selected: Sequence[Tuple[StockResearch, float, float, int, Tuple[str, ...]]],
    ) -> Dict[str, float]:
        industry_cap = float(self.config["portfolio"]["industry_target_max_weight"])
        industry_by_symbol = {item[0].symbol.upper(): item[0].industry for item in selected}
        released = 0.0
        industry_totals: Dict[str, float] = {}
        for symbol, weight in weights.items():
            industry = industry_by_symbol[symbol]
            industry_totals[industry] = industry_totals.get(industry, 0.0) + weight
        for industry, total in industry_totals.items():
            if total <= industry_cap:
                continue
            scale = industry_cap / total
            for symbol in weights:
                if industry_by_symbol[symbol] == industry:
                    old = weights[symbol]
                    weights[symbol] *= scale
                    released += old - weights[symbol]

        ranked = sorted(selected, key=lambda item: (item[2], item[1]), reverse=True)
        for _ in range(100):
            if released <= 1e-10:
                break
            moved = 0.0
            industry_totals = {}
            for symbol, weight in weights.items():
                industry = industry_by_symbol[symbol]
                industry_totals[industry] = industry_totals.get(industry, 0.0) + weight
            for item in ranked:
                symbol = item[0].symbol.upper()
                industry = item[0].industry
                capacity = min(
                    self._symbol_cap(symbol) - weights[symbol],
                    industry_cap - industry_totals[industry],
                )
                addition = min(max(capacity, 0.0), released)
                weights[symbol] += addition
                industry_totals[industry] += addition
                released -= addition
                moved += addition
                if released <= 1e-10:
                    break
            if moved <= 1e-12:
                break
        if released > 1e-7:
            raise StrategyInputError("產業權重上限使股票部位無法配置完成")
        return weights

    def _redistribute_starter_cash(
        self, positions: List[ScoredStock], target_stock_weight: float
    ) -> List[ScoredStock]:
        current = sum(position.target_weight for position in positions)
        remainder = target_stock_weight - current
        if remainder <= 1e-7:
            return positions
        mutable = [asdict(position) for position in positions]
        industry_cap = float(self.config["portfolio"]["industry_target_max_weight"])
        for _ in range(100):
            if remainder <= 1e-7:
                break
            industry_totals: Dict[str, float] = {}
            for item in mutable:
                industry_totals[item["industry"]] = industry_totals.get(item["industry"], 0.0) + item["target_weight"]
            eligible = [
                item
                for item in mutable
                if (
                    item["selection_role"] == "defense"
                    or (
                        item["confirmations"] >= 2
                        and item["market_has_reacted"]
                    )
                )
                and item["target_weight"] < self._symbol_cap(item["symbol"]) - 1e-9
                and industry_totals[item["industry"]] < industry_cap - 1e-9
            ]
            if not eligible:
                raise StrategyInputError("觀察部位上限造成資金無法合規配置")
            share = remainder / len(eligible)
            moved = 0.0
            for item in eligible:
                capacity = min(
                    self._symbol_cap(item["symbol"]) - item["target_weight"],
                    industry_cap - industry_totals[item["industry"]],
                )
                addition = min(share, capacity)
                item["target_weight"] += addition
                industry_totals[item["industry"]] += addition
                remainder -= addition
                moved += addition
            if moved <= 1e-12:
                break
        if remainder > 1e-7:
            raise StrategyInputError("觀察部位上限造成資金無法合規配置")
        return [ScoredStock(**item) for item in mutable]


def stock_from_dict(payload: Dict[str, object]) -> StockResearch:
    values = dict(payload)
    values["evidence_ids"] = tuple(str(value) for value in values.get("evidence_ids", []))
    return StockResearch(**values)
