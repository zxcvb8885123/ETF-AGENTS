"""Market data collection and persistence."""

from .collector import CollectionResult, DailyPriceCollector
from .corporate import (
    CorporateCollectionResult,
    CorporateDataCollector,
    CorporateRecord,
    MonthlyRevenue,
    OfficialCorporateProvider,
    OfficialCorporateProviderFactory,
    SourceDocument,
)
from .database import MarketDataDatabase
from .historical import HistoricalPriceProvider, HistoricalResponse, month_starts, tpex_ssl_context
from .historical_collector import HistoricalCollectionResult, HistoricalPriceCollector
from .latest import (
    LatestPriceCollector,
    LatestPriceCollectionResult,
    LatestPriceProviderFactory,
    collect_latest_prices,
    latest_price_providers,
)
from .tpex import TpexDailyProvider
from .twse import DailyPrice, TwseDailyProvider
from .universe import Instrument, UniverseLoader, load_universe
from .yfinance_history import (
    HistoryRefreshBatch,
    YFinanceCollectionResult,
    YFinanceHistoryCollector,
    YFinanceRefreshResult,
    plan_history_refresh,
    rolling_start,
)
from .snapshot import (
    DataAgentService,
    ResearchSnapshot,
    SnapshotBuilder,
    SnapshotDocument,
    SnapshotMonthlyRevenue,
    SnapshotPrice,
    SnapshotQualityPolicy,
)
from .snapshot_repository import SnapshotRepository
from .source_health import (
    SourceHealthProbe,
    SourceProbeDefinition,
    UniverseValidator,
    compute_universe_version,
    load_probe_definitions,
    write_json_artifact,
)
from .status import (
    CollectionRunStatus,
    DataAgentStatus,
    DataAgentStatusRepository,
    DocumentStatus,
    PriceSourceStatus,
    PriceStatus,
    SourceReportStatus,
    UniverseValidationStatus,
)

__all__ = [
    "CollectionResult",
    "CorporateCollectionResult",
    "CorporateDataCollector",
    "CorporateRecord",
    "CollectionRunStatus",
    "DataAgentStatus",
    "DataAgentStatusRepository",
    "DataAgentService",
    "DailyPrice",
    "DailyPriceCollector",
    "DocumentStatus",
    "PriceSourceStatus",
    "PriceStatus",
    "HistoricalCollectionResult",
    "HistoricalPriceCollector",
    "HistoricalPriceProvider",
    "HistoricalResponse",
    "HistoryRefreshBatch",
    "Instrument",
    "LatestPriceCollectionResult",
    "LatestPriceCollector",
    "LatestPriceProviderFactory",
    "MarketDataDatabase",
    "MonthlyRevenue",
    "OfficialCorporateProvider",
    "OfficialCorporateProviderFactory",
    "ResearchSnapshot",
    "SnapshotBuilder",
    "SnapshotDocument",
    "SnapshotMonthlyRevenue",
    "SnapshotPrice",
    "SnapshotQualityPolicy",
    "SnapshotRepository",
    "SourceHealthProbe",
    "SourceProbeDefinition",
    "SourceDocument",
    "SourceReportStatus",
    "TwseDailyProvider",
    "TpexDailyProvider",
    "YFinanceCollectionResult",
    "YFinanceHistoryCollector",
    "YFinanceRefreshResult",
    "UniverseValidator",
    "UniverseLoader",
    "UniverseValidationStatus",
    "compute_universe_version",
    "collect_latest_prices",
    "latest_price_providers",
    "load_probe_definitions",
    "load_universe",
    "month_starts",
    "plan_history_refresh",
    "rolling_start",
    "tpex_ssl_context",
    "write_json_artifact",
]
