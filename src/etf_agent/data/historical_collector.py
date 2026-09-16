"""Batch and persist monthly historical responses with bounded concurrency."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, List, Optional, Sequence, Tuple

from .database import MarketDataDatabase
from .historical import HistoricalPriceProvider, HistoricalResponse, month_starts
from .universe import Instrument


@dataclass(frozen=True)
class HistoricalCollectionResult:
    run_id: str
    start_date: str
    end_date: str
    requested_months: int
    fetched_payloads: int
    stored_rows: int
    warnings: Tuple[str, ...]


class HistoricalPriceCollector:
    def __init__(
        self,
        database: MarketDataDatabase,
        provider: HistoricalPriceProvider,
        max_workers: int = 4,
        retries: int = 3,
        retry_delay_seconds: float = 0.5,
        progress: Optional[Callable[[str], None]] = None,
    ):
        if max_workers < 1 or max_workers > 8:
            raise ValueError("max_workers 必須介於 1 與 8")
        self.database = database
        self.provider = provider
        self.max_workers = max_workers
        self.retries = retries
        self.retry_delay_seconds = retry_delay_seconds
        self.progress = progress

    def collect(
        self, universe: Sequence[Instrument], start: date, end: date
    ) -> HistoricalCollectionResult:
        if not universe:
            raise ValueError("官方交易池是空的")
        months = month_starts(start, end)
        run_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()
        source = "TWSE_TPEX_HISTORICAL"
        self.database.initialize()
        with self.database.connect() as connection:
            self.database.upsert_instruments(connection, universe, True)
            connection.execute(
                "INSERT INTO collection_runs(run_id, source, started_at, status) VALUES (?, ?, ?, 'running')",
                (run_id, source, started_at),
            )

        fetched_payloads = 0
        stored_rows = 0
        warnings: List[str] = []
        failures: List[str] = []
        try:
            for month in months:
                responses: List[HistoricalResponse] = []
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    future_map = {
                        executor.submit(self._fetch_with_retry, instrument, month): instrument
                        for instrument in universe
                    }
                    for future in as_completed(future_map):
                        instrument = future_map[future]
                        try:
                            responses.append(future.result())
                        except Exception as error:
                            failures.append(
                                "%s %s：%s" % (instrument.symbol, month.strftime("%Y-%m"), error)
                            )

                with self.database.connect() as connection:
                    for response in responses:
                        fetched_payloads += 1
                        warnings.extend(response.warnings)
                        selected = [
                            price
                            for price in response.prices
                            if start.isoformat() <= price.trade_date <= end.isoformat()
                        ]
                        payload_id = self.database.insert_raw_payload(
                            connection,
                            run_id,
                            response.source,
                            response.endpoint,
                            response.fetched_at,
                            response.payload,
                        )
                        stored_rows += self.database.upsert_prices(
                            connection,
                            selected,
                            response.source,
                            response.fetched_at,
                            run_id,
                            payload_id,
                        )
                if self.progress:
                    self.progress(
                        "%s 完成：累計 %d/%d 個回應，%d 筆日線"
                        % (
                            month.strftime("%Y-%m"),
                            fetched_payloads,
                            len(months) * len(universe),
                            stored_rows,
                        )
                    )

            if failures:
                raise RuntimeError(
                    "%d 個月份請求失敗；前 5 筆：%s"
                    % (len(failures), " | ".join(failures[:5]))
                )
            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE collection_runs
                    SET finished_at = ?, status = 'success', fetched_rows = ?,
                        stored_rows = ?, warning_count = ?
                    WHERE run_id = ?
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        fetched_payloads,
                        stored_rows,
                        len(warnings),
                        run_id,
                    ),
                )
        except BaseException as error:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE collection_runs
                    SET finished_at = ?, status = 'failed', fetched_rows = ?,
                        stored_rows = ?, warning_count = ?, error_message = ?
                    WHERE run_id = ?
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        fetched_payloads,
                        stored_rows,
                        len(warnings),
                        str(error),
                        run_id,
                    ),
                )
            raise
        return HistoricalCollectionResult(
            run_id=run_id,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            requested_months=len(months) * len(universe),
            fetched_payloads=fetched_payloads,
            stored_rows=stored_rows,
            warnings=tuple(warnings),
        )

    def _fetch_with_retry(self, instrument: Instrument, month: date) -> HistoricalResponse:
        last_error = None
        for attempt in range(self.retries + 1):
            try:
                return self.provider.fetch(instrument, month)
            except Exception as error:
                last_error = error
                if attempt < self.retries:
                    time.sleep(self.retry_delay_seconds * (2**attempt))
        raise RuntimeError(str(last_error))
