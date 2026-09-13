from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class Position:
    """A long-only stock position valued with the intended execution price."""

    symbol: str
    shares: int
    price: float

    @property
    def market_value(self) -> float:
        return self.shares * self.price


@dataclass(frozen=True)
class Portfolio:
    positions: Tuple[Position, ...]
    cash: float

    @property
    def net_asset_value(self) -> float:
        return sum(position.market_value for position in self.positions) + self.cash

    def weights(self) -> Dict[str, float]:
        nav = self.net_asset_value
        if nav <= 0:
            return {}
        return {position.symbol.upper(): position.market_value / nav for position in self.positions}

    @property
    def cash_weight(self) -> float:
        nav = self.net_asset_value
        return self.cash / nav if nav > 0 else 0.0
