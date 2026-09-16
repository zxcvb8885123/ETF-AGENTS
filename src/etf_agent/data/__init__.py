"""Market data collection and persistence."""

from .collector import CollectionResult, DailyPriceCollector
from .corporate import (
    CorporateCollectionResult,
    CorporateDataCollector,
    CorporateRecord,
    MonthlyRevenue,
    OfficialCorporateProvider,
    SourceDocument,
)
from .database import MarketDataDatabase
from .historical import HistoricalPriceProvider, HistoricalResponse, month_starts, tpex_ssl_context
from .historical_collector import HistoricalCollectionResult, HistoricalPriceCollector
from .twse import DailyPrice, TwseDailyProvider
from .universe import Instrument, load_universe
from .yfinance_history import YFinanceCollectionResult, YFinanceHistoryCollector
from .snapshot import DataAgentService, ResearchSnapshot

__all__ = [
    "CollectionResult",
    "CorporateCollectionResult",
    "CorporateDataCollector",
    "CorporateRecord",
    "DataAgentService",
    "DailyPrice",
    "DailyPriceCollector",
    "HistoricalCollectionResult",
    "HistoricalPriceCollector",
    "HistoricalPriceProvider",
    "HistoricalResponse",
    "Instrument",
    "MarketDataDatabase",
    "MonthlyRevenue",
    "OfficialCorporateProvider",
    "ResearchSnapshot",
    "SourceDocument",
    "TwseDailyProvider",
    "YFinanceCollectionResult",
    "YFinanceHistoryCollector",
    "load_universe",
    "month_starts",
    "tpex_ssl_context",
]
