"""唯讀讀取 as-of 時點已可得、已定案的官方日線版本。

- 只讀 SQLite（`mode=ro`），不會觸發 schema 遷移。
- 只取 `fetched_at ≤ as_of` 的列，同一（symbol, trade_date）依官方來源優先序、再依
  `fetched_at` 取最新版本；交易日收盤時間必須不晚於 as_of。Yahoo 等非官方來源不採用。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from etf_agent.core import parse_aware_time
from etf_agent.data.price_finality import market_close_at

from .contracts import OFFICIAL_SOURCE_PRIORITY, BenchmarkError, OfficialPriceRow

PriceTable = Dict[str, Dict[str, OfficialPriceRow]]  # {trade_date: {symbol: row}}


class BenchmarkPriceRepository:
    def __init__(self, database: Path):
        self.database = Path(database)

    def _connect(self) -> sqlite3.Connection:
        if not self.database.exists():
            raise BenchmarkError("找不到行情資料庫：%s" % self.database)
        connection = sqlite3.connect("file:%s?mode=ro" % self.database, uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def official_prices(
        self, symbols: Iterable[str], start_date: str, end_date: str, as_of: datetime
    ) -> PriceTable:
        """回傳 start_date 至 end_date 內、as_of 時已可得的官方日線（每檔每日一個版本）。"""
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise BenchmarkError("as_of 必須包含時區")
        wanted = sorted({str(symbol).upper() for symbol in symbols})
        if not wanted:
            return {}
        sources = sorted(OFFICIAL_SOURCE_PRIORITY)
        table: PriceTable = {}
        with self._connect() as connection:
            if not connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'daily_prices'"
            ).fetchone():
                raise BenchmarkError("資料庫沒有 daily_prices 資料表")
            for chunk in _chunks(wanted, 400):
                rows = connection.execute(
                    """
                    SELECT symbol, trade_date, close_price, trade_value, volume_shares, source, fetched_at
                    FROM daily_prices
                    WHERE symbol IN (%s) AND source IN (%s) AND trade_date >= ? AND trade_date <= ?
                    """
                    % (", ".join("?" * len(chunk)), ", ".join("?" * len(sources))),
                    (*chunk, *sources, start_date, end_date),
                ).fetchall()
                for row in rows:
                    trade_date = str(row["trade_date"])
                    if market_close_at(trade_date) > as_of:
                        continue
                    fetched = parse_aware_time(row["fetched_at"], "daily_prices.fetched_at", error=BenchmarkError)
                    if fetched > as_of:
                        continue
                    try:
                        close = Decimal(str(row["close_price"]))
                    except InvalidOperation as error:
                        raise BenchmarkError("%s %s 的 close_price 無法解析" % (row["symbol"], trade_date)) from error
                    if not close.is_finite() or close <= 0:
                        raise BenchmarkError("%s %s 的官方收盤價必須為正數" % (row["symbol"], trade_date))
                    candidate = OfficialPriceRow(
                        symbol=str(row["symbol"]).upper(),
                        trade_date=trade_date,
                        close_price=close,
                        trade_value=int(row["trade_value"]),
                        volume_shares=int(row["volume_shares"]),
                        source=str(row["source"]),
                        fetched_at=fetched,
                    )
                    current = table.setdefault(trade_date, {}).get(candidate.symbol)
                    if current is None or _rank(candidate) < _rank(current):
                        table[trade_date][candidate.symbol] = candidate
        return table


def _rank(row: OfficialPriceRow):
    # 來源優先序小者優先；同來源以較晚抓取的版本優先（取負值讓排序一致）。
    return (OFFICIAL_SOURCE_PRIORITY[row.source], -row.fetched_at.timestamp())


def _chunks(items: Sequence[str], size: int) -> List[Sequence[str]]:
    return [items[offset : offset + size] for offset in range(0, len(items), size)]
