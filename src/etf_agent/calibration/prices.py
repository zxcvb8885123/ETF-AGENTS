"""官方成交均價查詢：只讀 SQLite，只用官方未還原資料，只看 ``as_of`` 前已抓到的列。"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Set

from etf_agent.core import parse_aware_time
from etf_agent.virtual_account.daily import OFFICIAL_SOURCES, OfficialCloseMarket, average_price
from etf_agent.virtual_account.service import VirtualAccountError

from .contracts import CalibrationError


class OfficialPriceBook:
    """以官方成交金額除以成交股數取得 T 日均價。

    YAHOO 等非官方來源不在 ``OFFICIAL_SOURCES``，不會被採用。同一股票同一天的
    來源優先序、抓取時間與衝突判斷全部沿用 ``OfficialCloseMarket.quotes``。
    """

    def __init__(self, database: Path, as_of: str):
        self.database = Path(database)
        self.as_of = parse_aware_time(as_of, "as_of", error=CalibrationError).isoformat()
        self.market = OfficialCloseMarket(self.database)
        self._vwap: Dict[str, Optional[Dict[str, Optional[Decimal]]]] = {}
        self.conflict_days: List[str] = []

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect("file:%s?mode=ro" % self.database, uri=True)

    def execution_date(self, cutoff: str) -> Optional[str]:
        """決策 cutoff 後第一個已有官方收盤資料的交易日（與帳本同一口徑）。"""
        try:
            return self.market.execution_date(cutoff)
        except VirtualAccountError as error:
            raise CalibrationError(str(error)) from error

    def trading_days(self, start: str) -> List[str]:
        """自 ``start`` 起、``as_of`` 前已有官方列的交易日，由舊到新。"""
        sources = [item for group in OFFICIAL_SOURCES.values() for item in group]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT trade_date FROM daily_prices WHERE source IN (%s) AND trade_date >= ? AND fetched_at <= ? ORDER BY trade_date"
                % ",".join("?" * len(sources)),
                (*sources, start, self.as_of),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def vwap(self, trade_date: str) -> Optional[Dict[str, Optional[Decimal]]]:
        """symbol → 均價；成交量或金額為 0 時為 None。同日資料互相衝突時整日回傳 None。"""
        if trade_date not in self._vwap:
            try:
                quotes = self.market.quotes(trade_date, self.as_of)
            except VirtualAccountError:
                self.conflict_days.append(trade_date)
                self._vwap[trade_date] = None
            else:
                self._vwap[trade_date] = {
                    symbol: average_price(row["trade_value"], row["volume_shares"]) for symbol, row in quotes.items()
                }
        return self._vwap[trade_date]

    def ex_rights_symbols(self, after: str, through: str) -> Set[str]:
        """(after, through] 內有除權息標記的股票；窗口內價格不連續，不拿來算報酬。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT symbol FROM daily_prices WHERE ex_rights_marker = 1 AND trade_date > ? AND trade_date <= ?",
                (after, through),
            ).fetchall()
        return {str(row[0]).upper() for row in rows}
