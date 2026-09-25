"""早期原型：不在正式決策路徑上。

正式競賽規則檢查使用 ``etf_agent.decision.risk.CompetitionGuardV2``；買賣決策使用
``etf_agent.decision`` 的共同輸入、獨立 Buy／Sell 與 Trade Adjudicator。此處保留
float 版 ``CompetitionGuard``、``Portfolio`` 與事件策略 V1，只供相容測試與
``scripts/run_strategy.py`` 對照，不得用來產生正式權重、訂單或 D-Plan。
"""

from .event_strategy import EventDrivenStrategy, StockResearch, StrategyInputError, StrategyProposal
from .guard import CompetitionGuard, GuardResult
from .models import Portfolio, Position

__all__ = [
    "CompetitionGuard",
    "EventDrivenStrategy",
    "GuardResult",
    "Portfolio",
    "Position",
    "StockResearch",
    "StrategyInputError",
    "StrategyProposal",
]
