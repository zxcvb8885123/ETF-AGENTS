"""Two-year OHLCV collection through yfinance for TWSE and TPEx symbols."""

from __future__ import annotations

import importlib
import json
import math
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .database import MarketDataDatabase
from .historical import HistoryRefreshBatch, plan_history_refresh, rolling_start
from .twse import DailyPrice
from .universe import Instrument


@dataclass(frozen=True)
class YFinanceCollectionResult:
    run_id: str
    start_date: str
    end_date: str
    requested_symbols: int
    stored_rows: int
    missing_symbols: Tuple[str, ...]
    warnings: Tuple[str, ...]


@dataclass(frozen=True)
class YFinanceRefreshResult:
    run_ids: Tuple[str, ...]
    start_date: str
    end_date: str
    requested_symbols: int
    stored_rows: int
    missing_symbols: Tuple[str, ...]
    warnings: Tuple[str, ...]
    rebased_symbols: Tuple[str, ...] = ()


# Yahoo 還原價四捨五入至 0.0001；比值差距在此相對容忍度內視為同一還原基準。
# 台股最小配息率仍遠大於 0.01%，不會把真正的除權息誤判為誤差。
BASIS_TOLERANCE = Decimal("0.0001")


def stale_basis_symbols(connection, source: str, symbols: Sequence[str]) -> List[str]:
    """找出已存 Yahoo 序列混用不同還原基準的股票。

    Yahoo 的還原係數（Adj Close ÷ Close）以最新資料為基準往回累乘除權息，
    日期越早只會越小或相等。增量重抓只覆寫重疊區間，除權息後較舊列仍是舊基準，
    會出現「較早日期比值大於較晚日期」；出現時整檔須重抓才能回到同一基準。
    """
    stale: List[str] = []
    for symbol in symbols:
        rows = connection.execute(
            """
            SELECT close_price, adjusted_close_price FROM daily_prices
            WHERE symbol = ? AND source = ? AND adjusted_close_price IS NOT NULL
            ORDER BY trade_date
            """,
            (symbol, source),
        ).fetchall()
        highest: Optional[Decimal] = None
        for row in rows:
            close = Decimal(str(row["close_price"]))
            if close <= 0:
                continue
            ratio = Decimal(str(row["adjusted_close_price"])) / close
            if highest is not None and ratio < highest * (1 - BASIS_TOLERANCE):
                stale.append(symbol)
                break
            highest = ratio if highest is None else max(highest, ratio)
    return stale


def stored_closes(
    connection, source: str, symbols: Sequence[str], start: date
) -> Dict[Tuple[str, str], Decimal]:
    """讀取已存的 Yahoo 原始 Close，供重抓前後比對 Yahoo 是否改了原價基準。"""
    result: Dict[Tuple[str, str], Decimal] = {}
    for symbol in symbols:
        for row in connection.execute(
            "SELECT trade_date, close_price FROM daily_prices "
            "WHERE symbol = ? AND source = ? AND trade_date >= ?",
            (symbol, source, start.isoformat()),
        ).fetchall():
            result[(symbol, str(row["trade_date"]))] = Decimal(str(row["close_price"]))
    return result


def close_rebased_symbols(
    before: Mapping[Tuple[str, str], Decimal], after: Mapping[Tuple[str, str], Decimal]
) -> List[str]:
    """重抓前後同日 Close 相對差超過容忍度的股票。

    配股、分割、減資時 Yahoo 會把事件前的 Close 與 Adj Close 同比例改寫，
    Adj÷Close 不變，stale_basis_symbols 看不到；只有比對同日 Close 才抓得到。
    """
    changed = set()
    for key, old in before.items():
        new = after.get(key)
        if new is None or old <= 0:
            continue
        if abs(new - old) > BASIS_TOLERANCE * max(old, new):
            changed.add(key[0])
    return sorted(changed)


def _decimal(value: object) -> Optional[Decimal]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    # Yahoo commonly exposes float32 artifacts such as 64.5999984741211.
    # Four decimals preserve adjusted-price usefulness while removing noise.
    return Decimal("%.4f" % number)


class YFinanceHistoryCollector:
    source = "YAHOO_FINANCE"

    def __init__(
        self,
        database: MarketDataDatabase,
        batch_size: int = 30,
        retries: int = 2,
        progress: Optional[Callable[[str], None]] = None,
    ):
        if batch_size < 1 or batch_size > 100:
            raise ValueError("batch_size 必須介於 1 與 100")
        self.database = database
        self.batch_size = batch_size
        self.retries = retries
        self.progress = progress

    def refresh(
        self,
        universe: Sequence[Instrument],
        end: date,
        lookback_years: int = 2,
        overlap_days: int = 7,
    ) -> YFinanceRefreshResult:
        if not universe:
            raise ValueError("官方交易池是空的")
        self.database.initialize()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, MAX(trade_date) AS last_date
                FROM daily_prices
                WHERE source = ?
                GROUP BY symbol
                """,
                (self.source,),
            ).fetchall()
        last_dates = {
            str(row["symbol"]): date.fromisoformat(str(row["last_date"]))
            for row in rows
            if row["last_date"]
        }
        batches = plan_history_refresh(
            universe,
            last_dates,
            end,
            lookback_years=lookback_years,
            overlap_days=overlap_days,
        )
        results: List[YFinanceCollectionResult] = []
        price_changed: List[str] = []
        for batch in batches:
            if self.progress:
                self.progress(
                    "增量區間 %s 至 %s：%d 檔"
                    % (batch.start_date.isoformat(), end.isoformat(), len(batch.instruments))
                )
            symbols = [item.symbol for item in batch.instruments]
            with self.database.connect() as connection:
                before = stored_closes(connection, self.source, symbols, batch.start_date)
            results.append(self.collect(batch.instruments, batch.start_date, end))
            with self.database.connect() as connection:
                after = stored_closes(connection, self.source, symbols, batch.start_date)
            price_changed.extend(close_rebased_symbols(before, after))

        rebased, rebase_results, warnings = self._rebase_stale_series(
            universe, end, lookback_years, sorted(set(price_changed))
        )
        results.extend(rebase_results)
        starts = [batch.start_date for batch in batches] + [
            date.fromisoformat(item.start_date) for item in rebase_results
        ]
        return YFinanceRefreshResult(
            run_ids=tuple(item.run_id for item in results),
            start_date=min(starts).isoformat(),
            end_date=end.isoformat(),
            requested_symbols=len(universe),
            stored_rows=sum(item.stored_rows for item in results),
            missing_symbols=tuple(
                sorted({symbol for item in results for symbol in item.missing_symbols})
            ),
            warnings=tuple(warning for item in results for warning in item.warnings) + warnings,
            rebased_symbols=tuple(rebased),
        )

    def _rebase_stale_series(
        self,
        universe: Sequence[Instrument],
        end: date,
        lookback_years: int,
        price_changed: Sequence[str] = (),
    ) -> Tuple[List[str], List[YFinanceCollectionResult], Tuple[str, ...]]:
        """Yahoo 改了還原或原價基準後，整檔重抓已存區間回到同一基準。

        price_changed 是增量重抓時同日 Close 被改寫的股票（配股、分割、減資）。
        """
        by_symbol = {item.symbol: item for item in universe}
        with self.database.connect() as connection:
            stale = sorted(
                set(stale_basis_symbols(connection, self.source, list(by_symbol)))
                | {symbol for symbol in price_changed if symbol in by_symbol}
            )
            first_dates = {
                str(row["symbol"]): date.fromisoformat(str(row["first_date"]))
                for row in connection.execute(
                    "SELECT symbol, MIN(trade_date) AS first_date FROM daily_prices "
                    "WHERE source = ? GROUP BY symbol",
                    (self.source,),
                ).fetchall()
            }
        if not stale:
            return [], [], ()
        floor = rolling_start(end, lookback_years)
        grouped: Dict[date, List[Instrument]] = {}
        for symbol in stale:
            start = min(floor, first_dates.get(symbol, floor))
            grouped.setdefault(start, []).append(by_symbol[symbol])
        results: List[YFinanceCollectionResult] = []
        for start, instruments in sorted(grouped.items()):
            if self.progress:
                self.progress(
                    "還原基準已變動，整段重抓 %s 至 %s：%s"
                    % (start.isoformat(), end.isoformat(), ", ".join(item.symbol for item in instruments))
                )
            results.append(self.collect(instruments, start, end))
        with self.database.connect() as connection:
            remaining = stale_basis_symbols(connection, self.source, stale)
        warnings = tuple(
            "%s 整段重抓後 Yahoo 還原基準仍不一致" % symbol for symbol in remaining
        )
        return stale, results, warnings

    def collect(
        self, universe: Sequence[Instrument], start: date, end: date
    ) -> YFinanceCollectionResult:
        if not universe:
            raise ValueError("官方交易池是空的")
        if start > end:
            raise ValueError("歷史資料起日不得晚於迄日")
        try:
            yf = importlib.import_module("yfinance")
        except ImportError as error:
            raise RuntimeError("缺少 yfinance；請先安裝 requirements.txt") from error

        run_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()
        self.database.initialize()
        with self.database.connect() as connection:
            self.database.upsert_instruments(connection, universe, True)
            connection.execute(
                "INSERT INTO collection_runs(run_id, source, started_at, status) VALUES (?, ?, ?, 'running')",
                (run_id, self.source, started_at),
            )

        by_symbol = {item.symbol.upper(): item for item in universe}
        frames: Dict[str, object] = {}
        warnings: List[str] = []
        try:
            symbols = list(by_symbol)
            for offset in range(0, len(symbols), self.batch_size):
                batch = symbols[offset : offset + self.batch_size]
                data = yf.download(
                    batch,
                    start=start.isoformat(),
                    end=(end + timedelta(days=1)).isoformat(),
                    auto_adjust=False,
                    actions=False,
                    progress=False,
                    threads=True,
                    group_by="ticker",
                    timeout=30,
                )
                for symbol in batch:
                    frame = self._ticker_frame(data, symbol)
                    if frame is not None and not frame.dropna(how="all").empty:
                        frames[symbol] = frame
                if self.progress:
                    self.progress(
                        "批次 %d/%d：目前取得 %d/%d 檔"
                        % (
                            min(offset + len(batch), len(symbols)),
                            len(symbols),
                            len(frames),
                            len(symbols),
                        )
                    )

            missing = [symbol for symbol in symbols if symbol not in frames]
            for attempt in range(self.retries):
                if not missing:
                    break
                time.sleep(1.0 * (attempt + 1))
                for symbol in list(missing):
                    data = yf.download(
                        symbol,
                        start=start.isoformat(),
                        end=(end + timedelta(days=1)).isoformat(),
                        auto_adjust=False,
                        actions=False,
                        progress=False,
                        threads=False,
                        group_by="ticker",
                        timeout=30,
                    )
                    frame = self._ticker_frame(data, symbol)
                    if frame is not None and not frame.dropna(how="all").empty:
                        frames[symbol] = frame
                        missing.remove(symbol)

            stored_rows = 0
            with self.database.connect() as connection:
                for symbol, frame in frames.items():
                    prices, payload, row_warnings = self._convert_frame(
                        by_symbol[symbol], frame, start, end
                    )
                    warnings.extend(row_warnings)
                    payload_id = self.database.insert_raw_payload(
                        connection,
                        run_id,
                        self.source,
                        "yfinance://download/%s?start=%s&end=%s"
                        % (symbol, start.isoformat(), end.isoformat()),
                        datetime.now(timezone.utc).isoformat(),
                        payload,
                    )
                    stored_rows += self.database.upsert_prices(
                        connection,
                        prices,
                        self.source,
                        datetime.now(timezone.utc).isoformat(),
                        run_id,
                        payload_id,
                    )
                connection.execute(
                    """
                    UPDATE collection_runs
                    SET finished_at = ?, status = 'success', fetched_rows = ?,
                        stored_rows = ?, warning_count = ?, error_message = ?
                    WHERE run_id = ?
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        len(frames),
                        stored_rows,
                        len(warnings) + len(missing),
                        ("缺少：" + ", ".join(missing)) if missing else None,
                        run_id,
                    ),
                )
        except BaseException as error:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE collection_runs SET finished_at=?, status='failed', error_message=? WHERE run_id=?",
                    (datetime.now(timezone.utc).isoformat(), str(error), run_id),
                )
            raise

        return YFinanceCollectionResult(
            run_id=run_id,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            requested_symbols=len(universe),
            stored_rows=stored_rows,
            missing_symbols=tuple(missing),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _ticker_frame(data, symbol: str):
        columns = getattr(data, "columns", None)
        if columns is None:
            return None
        if getattr(columns, "nlevels", 1) == 1:
            return data
        level_zero = set(columns.get_level_values(0))
        if symbol in level_zero:
            return data[symbol]
        level_one = set(columns.get_level_values(1))
        if symbol in level_one:
            return data.xs(symbol, axis=1, level=1)
        return None

    @staticmethod
    def _convert_frame(
        instrument: Instrument, frame, start: date, end: date
    ) -> Tuple[List[DailyPrice], str, List[str]]:
        prices: List[DailyPrice] = []
        records: List[Dict[str, object]] = []
        warnings: List[str] = []
        previous_close: Optional[Decimal] = None
        for timestamp, row in frame.sort_index().iterrows():
            trade_date = timestamp.date()
            if trade_date < start or trade_date > end:
                continue
            close = _decimal(row.get("Close"))
            if close is None:
                # Multi-symbol downloads share one date index. A stock that was
                # not yet listed or did not trade has an expected all-NaN row.
                continue
            adjusted = _decimal(row.get("Adj Close"))
            volume_decimal = _decimal(row.get("Volume"))
            volume = int(volume_decimal) if volume_decimal is not None else 0
            change = close - previous_close if previous_close is not None else None
            previous_close = close
            price = DailyPrice(
                symbol=instrument.symbol,
                code=instrument.code,
                name=instrument.name,
                trade_date=trade_date.isoformat(),
                open_price=_decimal(row.get("Open")),
                high_price=_decimal(row.get("High")),
                low_price=_decimal(row.get("Low")),
                close_price=close,
                change=change,
                volume_shares=volume,
                trade_value=0,
                transactions=0,
                adjusted_close=adjusted,
            )
            prices.append(price)
            records.append(
                {
                    "date": price.trade_date,
                    "open": str(price.open_price) if price.open_price is not None else None,
                    "high": str(price.high_price) if price.high_price is not None else None,
                    "low": str(price.low_price) if price.low_price is not None else None,
                    "close": str(price.close_price),
                    "adjusted_close": str(price.adjusted_close) if price.adjusted_close is not None else None,
                    "volume": price.volume_shares,
                }
            )
        return prices, json.dumps(records, ensure_ascii=False), warnings
