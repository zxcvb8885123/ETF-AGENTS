"""Combined latest-price collection for the official TWSE and TPEx universe."""

from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence

from .collector import CollectionResult, DailyPriceCollector, DailyPriceProvider
from .database import MarketDataDatabase
from .tpex import TpexDailyProvider
from .twse import TwseDailyProvider
from .universe import Instrument


@dataclass(frozen=True)
class LatestPriceCollectionResult:
    collections: List[CollectionResult]

    @property
    def fetched_rows(self) -> int:
        return sum(item.fetched_rows for item in self.collections)

    @property
    def stored_rows(self) -> int:
        return sum(item.stored_rows for item in self.collections)

    @property
    def warnings(self) -> List[str]:
        return [warning for item in self.collections for warning in item.warnings]


class LatestPriceProviderFactory:
    """從 allowlist 設定建立官方最新行情 provider 物件。"""

    def create(self, config: Mapping[str, object]) -> List[DailyPriceProvider]:
        twse = self._source_config(config, "twse_daily")
        tpex = self._source_config(config, "tpex_daily")
        return [
            TwseDailyProvider(
                url=str(twse["url"]),
                timeout_seconds=int(twse["timeout_seconds"]),
                user_agent=str(twse["user_agent"]),
            ),
            TpexDailyProvider(
                url=str(tpex["url"]),
                timeout_seconds=int(tpex["timeout_seconds"]),
                user_agent=str(tpex["user_agent"]),
            ),
        ]

    @staticmethod
    def _source_config(
        config: Mapping[str, object], key: str
    ) -> Dict[str, object]:
        value = config.get(key)
        if not isinstance(value, dict):
            raise ValueError("缺少資料來源設定：%s" % key)
        for field in ("url", "timeout_seconds", "user_agent"):
            if field not in value:
                raise ValueError("%s 缺少欄位：%s" % (key, field))
        return value


class LatestPriceCollector:
    """協調多個市場 provider，完成一次官方最新行情更新。"""

    def __init__(
        self,
        database: MarketDataDatabase,
        providers: Sequence[DailyPriceProvider],
    ):
        self.database = database
        self.providers = providers

    def collect(
        self, universe: Sequence[Instrument]
    ) -> LatestPriceCollectionResult:
        if not universe:
            raise ValueError("官方交易池是空的；請先填入 data/official_universe.csv")
        return LatestPriceCollectionResult(
            collections=[
                DailyPriceCollector(self.database, provider).collect(universe)
                for provider in self.providers
            ]
        )


def latest_price_providers(config: Mapping[str, object]) -> List[DailyPriceProvider]:
    """相容舊呼叫端；新程式應使用 LatestPriceProviderFactory。"""

    return LatestPriceProviderFactory().create(config)


def collect_latest_prices(
    database: MarketDataDatabase,
    universe: Sequence[Instrument],
    providers: Sequence[DailyPriceProvider],
) -> LatestPriceCollectionResult:
    """相容舊呼叫端；新程式應使用 LatestPriceCollector。"""

    return LatestPriceCollector(database, providers).collect(universe)
