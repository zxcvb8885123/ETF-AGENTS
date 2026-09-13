"""Fail-closed competition checks used before a decision can be submitted."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from .models import Portfolio


@dataclass(frozen=True)
class GuardResult:
    passed: bool
    errors: List[str]
    warnings: List[str]
    active_share: Optional[float]


class CompetitionGuard:
    def __init__(self, rules_path: Path, allowed_symbols: Iterable[str]):
        with rules_path.open(encoding="utf-8") as handle:
            self.rules: Dict[str, object] = json.load(handle)
        self.allowed_symbols: Set[str] = {symbol.upper() for symbol in allowed_symbols}

    def validate(
        self,
        portfolio: Portfolio,
        benchmark_weights: Optional[Dict[str, float]] = None,
    ) -> GuardResult:
        errors: List[str] = []
        warnings: List[str] = []
        positions = portfolio.positions
        weights = portfolio.weights()

        if portfolio.net_asset_value <= 0:
            errors.append("NAV 必須大於 0")
        if portfolio.cash < 0:
            errors.append("現金部位不得為負")
        if portfolio.cash_weight >= float(self.rules["cash_weight_must_be_below"]):
            errors.append("現金部位必須低於 NAV 的 25%")
        elif portfolio.cash_weight > float(self.rules["recommended_cash_weight_ceiling"]):
            warnings.append("現金部位高於建議安全緩衝 22%")

        symbols = [position.symbol.upper() for position in positions]
        if len(symbols) != len(set(symbols)):
            errors.append("同一股票不得拆成多筆部位；請先合併後再檢查")
        if not int(self.rules["min_positions"]) <= len(positions) <= int(self.rules["max_positions"]):
            errors.append("每日持股檔數必須介於 20 至 30 檔")
        if not self.allowed_symbols:
            errors.append("官方 150 檔交易池尚未載入，禁止送件")
        else:
            disallowed = sorted(set(symbols) - self.allowed_symbols)
            if disallowed:
                errors.append("交易標的不在官方交易池：" + ", ".join(disallowed))

        tsmc_symbol = str(self.rules["tsmc_symbol"]).upper()
        for position in positions:
            if position.shares <= 0 or position.price <= 0:
                errors.append("所有部位必須為正整股數與正價格")
                break
            weight = weights.get(position.symbol.upper(), 0.0)
            max_weight = (
                float(self.rules["tsmc_max_stock_weight"])
                if position.symbol.upper() == tsmc_symbol
                else float(self.rules["default_max_stock_weight"])
            )
            if weight > max_weight:
                errors.append(
                    "%s 權重 %.2f%% 超過上限 %.2f%%"
                    % (position.symbol, weight * 100, max_weight * 100)
                )

        active_share: Optional[float] = None
        if benchmark_weights is None:
            errors.append("缺少主動式 ETF 前十大基準資料，無法驗證 Active Share")
        else:
            active_share = self.calculate_active_share(weights, benchmark_weights)
            if active_share < float(self.rules["minimum_active_share"]):
                errors.append("Active Share %.2f%% 低於 20%%" % (active_share * 100))

        return GuardResult(not errors, errors, warnings, active_share)

    @staticmethod
    def calculate_active_share(
        portfolio_weights: Dict[str, float], benchmark_weights: Dict[str, float]
    ) -> float:
        portfolio_weights = {
            symbol.upper(): weight for symbol, weight in portfolio_weights.items()
        }
        benchmark_weights = {
            symbol.upper(): weight for symbol, weight in benchmark_weights.items()
        }
        symbols = set(portfolio_weights) | set(benchmark_weights)
        return 0.5 * sum(
            abs(portfolio_weights.get(symbol, 0.0) - benchmark_weights.get(symbol, 0.0))
            for symbol in symbols
        )
