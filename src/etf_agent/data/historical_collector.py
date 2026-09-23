"""Batch and persist monthly historical responses with bounded concurrency."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .database import MarketDataDatabase
from .historical import (
    HistoricalPriceProvider,
    HistoricalResponse,
    HistoryRefreshBatch,
    month_starts,
    plan_history_refresh,
)
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


class HistoricalCollectionError(RuntimeError):
    """保留已寫入的 collection run，讓呼叫端能報告部分失敗。"""

    def __init__(self, run_id: str, message: str):
        super().__init__(message)
        self.run_id = run_id


@dataclass(frozen=True)
class OfficialHistoryBatchResult:
    run_id: str
    start_date: str
    end_date: str
    symbols: Tuple[str, ...]
    status: str
    stored_rows: int
    warning_count: int
    error_message: Optional[str] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "run_id": self.run_id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "symbols": list(self.symbols),
            "status": self.status,
            "stored_rows": self.stored_rows,
            "warning_count": self.warning_count,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class OfficialHistoryCoverage:
    symbol: str
    source: str
    requested_start_date: str
    requested_end_date: str
    stored_start_date: Optional[str]
    stored_end_date: Optional[str]
    stored_rows: int
    end_coverage: str
    range_coverage: str
    warnings: Tuple[str, ...]

    def as_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "source": self.source,
            "requested_start_date": self.requested_start_date,
            "requested_end_date": self.requested_end_date,
            "stored_start_date": self.stored_start_date,
            "stored_end_date": self.stored_end_date,
            "stored_rows": self.stored_rows,
            "end_coverage": self.end_coverage,
            "range_coverage": self.range_coverage,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class OfficialHistoryRefreshResult:
    status: str
    safe_end_date: str
    start_date: str
    end_date: str
    requested_symbols: int
    stored_rows: int
    batches: Tuple[OfficialHistoryBatchResult, ...]
    coverage: Tuple[OfficialHistoryCoverage, ...]
    warnings: Tuple[str, ...]

    @property
    def exit_code(self) -> int:
        if any(batch.status == "failed" for batch in self.batches):
            return 1
        if self.status == "degraded":
            return 2
        return 0

    def as_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "safe_end_date": self.safe_end_date,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "requested_symbols": self.requested_symbols,
            "stored_rows": self.stored_rows,
            "batches": [batch.as_dict() for batch in self.batches],
            "coverage": [item.as_dict() for item in self.coverage],
            "warnings": list(self.warnings),
            "limitations": [
                "未接入版本化官方交易日曆；range_coverage 只報告已保存區間，不能證明期間內每個應有交易日均完整。",
                "本結果只驗證官方歷史行情收集與終止日覆蓋，不能作為正式歷史回測或策略績效驗證。",
            ],
        }


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
        except Exception as error:
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
            raise HistoricalCollectionError(run_id, str(error)) from error
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


class OfficialHistoricalRefreshService:
    """以官方 TWSE／TPEx 月行情更新並稽核指定交易池的歷史日線。"""

    latest_sources = {
        "TWSE": "TWSE_STOCK_DAY_ALL",
        "TPEX": "TPEX_MAINBOARD_QUOTES",
    }

    def __init__(
        self,
        database: MarketDataDatabase,
        provider: HistoricalPriceProvider,
        max_workers: int = 4,
        retries: int = 3,
        progress: Optional[Callable[[str], None]] = None,
    ):
        self.database = database
        self.provider = provider
        self.max_workers = max_workers
        self.retries = retries
        self.progress = progress

    def refresh(
        self,
        universe: Sequence[Instrument],
        end: Optional[date] = None,
        start: Optional[date] = None,
        lookback_years: int = 2,
        overlap_days: int = 7,
    ) -> OfficialHistoryRefreshResult:
        if not universe:
            raise ValueError("官方交易池是空的")
        self.database.initialize()
        safe_end = self._safe_end_date(universe)
        target_end = safe_end if end is None else end
        if target_end > safe_end:
            raise ValueError(
                "歷史行情迄日 %s 晚於最近完整官方交易日 %s"
                % (target_end.isoformat(), safe_end.isoformat())
            )
        if start is not None and start > target_end:
            raise ValueError("歷史資料起日不得晚於迄日")

        if start is not None:
            batches = [HistoryRefreshBatch(start, tuple(universe))]
        else:
            last_dates = {
                symbol: date.fromisoformat(value)
                for symbol, value in self.database.latest_trade_dates_by_symbol(
                    self._historical_sources(universe)
                ).items()
            }
            batches = plan_history_refresh(
                universe,
                last_dates,
                target_end,
                lookback_years=lookback_years,
                overlap_days=overlap_days,
            )

        requested_starts = {
            instrument.symbol: batch.start_date
            for batch in batches
            for instrument in batch.instruments
        }
        batch_results: List[OfficialHistoryBatchResult] = []
        warnings: List[str] = []
        stored_rows = 0
        for batch in batches:
            collector = HistoricalPriceCollector(
                self.database,
                self.provider,
                max_workers=self.max_workers,
                retries=self.retries,
                progress=self.progress,
            )
            symbols = tuple(instrument.symbol for instrument in batch.instruments)
            try:
                result = collector.collect(batch.instruments, batch.start_date, target_end)
            except HistoricalCollectionError as error:
                warnings.append(
                    "%s 至 %s 的 %d 檔請求失敗：%s"
                    % (
                        batch.start_date.isoformat(),
                        target_end.isoformat(),
                        len(symbols),
                        error,
                    )
                )
                batch_results.append(
                    OfficialHistoryBatchResult(
                        run_id=error.run_id,
                        start_date=batch.start_date.isoformat(),
                        end_date=target_end.isoformat(),
                        symbols=symbols,
                        status="failed",
                        stored_rows=0,
                        warning_count=0,
                        error_message=str(error),
                    )
                )
                continue
            stored_rows += result.stored_rows
            warnings.extend(result.warnings)
            batch_results.append(
                OfficialHistoryBatchResult(
                    run_id=result.run_id,
                    start_date=result.start_date,
                    end_date=result.end_date,
                    symbols=symbols,
                    status="success",
                    stored_rows=result.stored_rows,
                    warning_count=len(result.warnings),
                )
            )

        coverage = self._coverage(universe, requested_starts, target_end)
        has_failed_batch = any(item.status == "failed" for item in batch_results)
        has_uncovered_symbol = any(
            item.end_coverage != "present" for item in coverage
        )
        status = "failed" if has_failed_batch else (
            "degraded" if has_uncovered_symbol else "completed"
        )
        return OfficialHistoryRefreshResult(
            status=status,
            safe_end_date=safe_end.isoformat(),
            start_date=min(requested_starts.values()).isoformat(),
            end_date=target_end.isoformat(),
            requested_symbols=len(universe),
            stored_rows=stored_rows,
            batches=tuple(batch_results),
            coverage=tuple(coverage),
            warnings=tuple(warnings),
        )

    def _safe_end_date(self, universe: Sequence[Instrument]) -> date:
        latest_sources = tuple(
            dict.fromkeys(
                self.latest_sources[self._market_key(instrument)]
                for instrument in universe
            )
        )
        latest = self.database.latest_complete_trade_date(latest_sources)
        if latest is None:
            raise ValueError(
                "缺少 TWSE／TPEx 最新官方行情；請先執行 collect_latest_prices.py"
            )
        return date.fromisoformat(latest)

    def _coverage(
        self,
        universe: Sequence[Instrument],
        requested_starts: Dict[str, date],
        end: date,
    ) -> List[OfficialHistoryCoverage]:
        ranges = self.database.history_price_ranges(self._historical_sources(universe))
        coverage: List[OfficialHistoryCoverage] = []
        for instrument in universe:
            expected_source = self._historical_source(instrument)
            row = ranges.get((instrument.symbol, expected_source))
            stored_start = stored_end = None
            stored_rows = 0
            warnings: List[str] = []
            if row is not None:
                stored_start, stored_end, stored_rows = row
            else:
                warnings.append("尚未保存 %s 的官方歷史行情" % expected_source)
            if stored_end == end.isoformat():
                end_coverage = "present"
            else:
                end_coverage = "missing"
                warnings.append(
                    "終止日 %s 未有官方日線；目前最後日為 %s"
                    % (end.isoformat(), stored_end or "無")
                )
            coverage.append(
                OfficialHistoryCoverage(
                    symbol=instrument.symbol,
                    source=expected_source,
                    requested_start_date=requested_starts[instrument.symbol].isoformat(),
                    requested_end_date=end.isoformat(),
                    stored_start_date=stored_start,
                    stored_end_date=stored_end,
                    stored_rows=stored_rows,
                    end_coverage=end_coverage,
                    range_coverage="unverified_without_official_calendar",
                    warnings=tuple(warnings),
                )
            )
        return coverage

    def _historical_sources(self, universe: Sequence[Instrument]) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(self._historical_source(item) for item in universe))

    def _historical_source(self, instrument: Instrument) -> str:
        return (
            self.provider.tpex_source
            if self._market_key(instrument) == "TPEX"
            else self.provider.twse_source
        )

    @staticmethod
    def _market_key(instrument: Instrument) -> str:
        return "TPEX" if instrument.market.upper() in {"TPEX", "OTC", "上櫃"} else "TWSE"
