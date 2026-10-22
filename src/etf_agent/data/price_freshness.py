"""判斷資料庫是否已有某交易日「收盤後定案」的行情，讓 start.sh 可略過重複抓取。

只用於收盤行情這類固定時間定案的資料。重大訊息、月營收、財報、新聞等發布時間不固定，
不在此判斷，永遠照常收集。判斷不出來時一律視為「不是最新」，由呼叫端重抓（fail-closed）。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Dict, Iterable, Mapping, Tuple

from etf_agent.core import parse_aware_time

from .evidence import TAIPEI_TIMEZONE

# 交易日 15:00 後當日行情才算齊全；Yahoo 盤中就會回傳當日那根日線，數值尚未定案。
DATA_READY_TIME = time(15, 0)

# 每個行情步驟寫入哪個來源（依股票後綴）；對應 start.sh 的三個行情步驟。
STEP_SOURCES: Mapping[str, Mapping[str, str]] = {
    "official_latest": {".TW": "TWSE_STOCK_DAY_ALL", ".TWO": "TPEX_MAINBOARD_QUOTES"},
    "yahoo": {".TW": "YAHOO_FINANCE", ".TWO": "YAHOO_FINANCE"},
    "official_history": {".TW": "TWSE_STOCK_DAY", ".TWO": "TPEX_TRADING_STOCK"},
}


def prices_current(
    connection, symbols: Iterable[str], step: str, trade_date: date
) -> Tuple[bool, str]:
    """交易池每檔在 trade_date 都有該步驟來源的行情，且都在當日 DATA_READY_TIME 之後抓取才算最新。"""
    if step not in STEP_SOURCES:
        raise ValueError("未知的行情步驟：%s" % step)
    ready_at = datetime.combine(trade_date, DATA_READY_TIME, tzinfo=TAIPEI_TIMEZONE)
    by_source: Dict[str, list] = {}
    for symbol in symbols:
        suffix = ".TWO" if symbol.upper().endswith(".TWO") else ".TW"
        by_source.setdefault(STEP_SOURCES[step][suffix], []).append(symbol)
    if not by_source:
        return False, "交易池是空的"
    for source, expected in sorted(by_source.items()):
        fetched = {
            str(row["symbol"]): row["fetched_at"]
            for row in connection.execute(
                "SELECT symbol, MIN(fetched_at) AS fetched_at FROM daily_prices "
                "WHERE source = ? AND trade_date = ? GROUP BY symbol",
                (source, trade_date.isoformat()),
            ).fetchall()
        }
        missing = sorted(set(expected) - set(fetched))
        if missing:
            return False, "%s 在 %s 缺 %d 檔" % (source, trade_date.isoformat(), len(missing))
        for symbol in expected:
            try:
                fetched_at = parse_aware_time(fetched[symbol], "fetched_at")
            except ValueError:
                return False, "%s %s 的抓取時間無法判讀" % (source, symbol)
            if fetched_at < ready_at:
                return False, "%s 的 %s 是 %s 收盤前抓的，可能尚未定案" % (source, symbol, trade_date.isoformat())
    return True, "資料庫已有 %s 收盤後抓取的完整行情" % trade_date.isoformat()
