import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Protocol, Sequence, Tuple

from .database import MarketDataDatabase
from .twse import DailyPrice
from .universe import Instrument


@dataclass(frozen=True)
class CollectionResult:
    run_id: str
    fetched_rows: int
    stored_rows: int
    trade_date: str
    warnings: List[str]
    source: str = ""
    market: str = ""


class DailyPriceProvider(Protocol):
    source_name: str
    market: str
    url: str

    def fetch(self) -> Tuple[str, str]:
        ...

    def parse(self, payload: str) -> Tuple[List[DailyPrice], List[str]]:
        ...


class DailyPriceCollector:
    def __init__(self, database: MarketDataDatabase, provider: DailyPriceProvider):
        self.database = database
        self.provider = provider

    def collect(
        self,
        universe: Sequence[Instrument],
        include_all_listed: bool = False,
    ) -> CollectionResult:
        if not universe and not include_all_listed:
            raise ValueError("官方交易池是空的；請先填入 data/official_universe.csv")

        run_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()
        self.database.initialize()

        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO collection_runs(run_id, source, started_at, status)
                VALUES (?, ?, ?, 'running')
                """,
                (run_id, self.provider.source_name, started_at),
            )

        try:
            payload, fetched_at = self.provider.fetch()
            all_prices, warnings = self.provider.parse(payload)
            selected = self._select(
                all_prices,
                universe,
                include_all_listed,
                warnings,
                self.provider.market,
            )
            if not selected:
                raise ValueError(
                    "%s 回應中找不到交易池內的 %s 股票"
                    % (self.provider.source_name, self.provider.market)
                )
            trade_dates = {price.trade_date for price in selected}
            if len(trade_dates) != 1:
                raise ValueError(
                    "單次 %s 回應包含多個交易日：%s"
                    % (self.provider.source_name, sorted(trade_dates))
                )

            observed = [
                Instrument(
                    symbol=price.symbol,
                    code=price.code,
                    name=price.name,
                    market=self.provider.market,
                    source_date=price.trade_date,
                )
                for price in selected
            ]
            with self.database.connect() as connection:
                self.database.upsert_instruments(connection, universe, True)
                self.database.upsert_instruments(connection, observed, False)
                payload_id = self.database.insert_raw_payload(
                    connection,
                    run_id,
                    self.provider.source_name,
                    self.provider.url,
                    fetched_at,
                    payload,
                )
                stored_rows = self.database.upsert_prices(
                    connection,
                    selected,
                    self.provider.source_name,
                    fetched_at,
                    run_id,
                    payload_id,
                )
                connection.execute(
                    """
                    UPDATE collection_runs
                    SET finished_at = ?, status = 'success', fetched_rows = ?,
                        stored_rows = ?, warning_count = ?
                    WHERE run_id = ?
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        len(all_prices),
                        stored_rows,
                        len(warnings),
                        run_id,
                    ),
                )
            return CollectionResult(
                run_id=run_id,
                fetched_rows=len(all_prices),
                stored_rows=stored_rows,
                trade_date=next(iter(trade_dates)),
                warnings=warnings,
                source=self.provider.source_name,
                market=self.provider.market,
            )
        except Exception as error:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE collection_runs
                    SET finished_at = ?, status = 'failed', error_message = ?
                    WHERE run_id = ?
                    """,
                    (datetime.now(timezone.utc).isoformat(), str(error), run_id),
                )
            raise

    @staticmethod
    def _select(
        prices: Sequence[DailyPrice],
        universe: Sequence[Instrument],
        include_all_listed: bool,
        warnings: List[str],
        market: str = "TWSE",
    ) -> List[DailyPrice]:
        if include_all_listed:
            return list(prices)
        market_aliases = {
            "TWSE": {"TWSE", "上市"},
            "TPEX": {"TPEX", "TPEx", "上櫃"},
        }
        allowed_markets = market_aliases.get(market.upper(), {market})
        allowed = {item.symbol for item in universe if item.market in allowed_markets}
        selected = [price for price in prices if price.symbol in allowed]
        missing = sorted(allowed - {price.symbol for price in selected})
        if missing:
            warnings.append(
                "%s 最新資料缺少 %d 檔交易池股票" % (market.upper(), len(missing))
            )
        return selected
