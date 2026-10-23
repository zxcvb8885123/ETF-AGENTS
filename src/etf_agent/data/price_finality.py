"""收盤行情的定案時間：Snapshot、Yahoo 收集、新鮮度判斷與版本寫入共用同一組常數。

- ``MARKET_CLOSE_TIME``（13:30 台北）：台股收盤。某交易日的日線只可能在收盤後存在，
  Snapshot 以此決定 cutoff 最多能納入哪一天。
- ``PRICE_FINALIZED_TIME``（15:00 台北）：Yahoo 等非官方來源盤中就會回傳當日那根日線，
  收盤價與成交量尚未定案；只有在此之後抓到的列才算定案。官方來源（TWSE／TPEx）
  只在收盤後公布，抓到即定案，不受此限制。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Union

from etf_agent.core import parse_aware_time

from .evidence import TAIPEI_TIMEZONE

MARKET_CLOSE_TIME = time(13, 30)
PRICE_FINALIZED_TIME = time(15, 0)

# 盤中即回傳當日日線、收盤後才定案的來源。
UNOFFICIAL_PRICE_SOURCES = ("YAHOO_FINANCE",)


def requires_finalized_fetch(source: str) -> bool:
    return str(source).upper() in UNOFFICIAL_PRICE_SOURCES


def _as_date(trade_date: Union[date, str]) -> date:
    if isinstance(trade_date, date):
        return trade_date
    try:
        return date.fromisoformat(str(trade_date))
    except ValueError as error:
        raise ValueError("trade_date 無法解析：%s" % trade_date) from error


def market_close_at(trade_date: Union[date, str]) -> datetime:
    return datetime.combine(_as_date(trade_date), MARKET_CLOSE_TIME, tzinfo=TAIPEI_TIMEZONE)


def price_finalized_at(trade_date: Union[date, str]) -> datetime:
    """該交易日的非官方日線最早可視為定案的時間。"""
    return datetime.combine(_as_date(trade_date), PRICE_FINALIZED_TIME, tzinfo=TAIPEI_TIMEZONE)


def is_price_finalized(
    trade_date: Union[date, str], fetched_at: object, *, field: str = "fetched_at"
) -> bool:
    """fetched_at 必須含時區；缺時區直接拋出 ValueError，不猜測。"""
    return parse_aware_time(fetched_at, field) >= price_finalized_at(trade_date)


def latest_finalized_price_date(now: datetime) -> date:
    """``now`` 時非官方日線最晚可能已定案的日期：台北 15:00 後是當天，否則前一天。

    只處理時間門檻；該日是否為交易日、是否真的抓到，仍由覆蓋驗證判定。
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必須包含時區")
    local = now.astimezone(TAIPEI_TIMEZONE)
    if local.time() >= PRICE_FINALIZED_TIME:
        return local.date()
    return local.date() - timedelta(days=1)


def _finalized_utc_clock() -> str:
    moment = datetime.combine(date(2000, 1, 1), PRICE_FINALIZED_TIME, tzinfo=TAIPEI_TIMEZONE)
    return moment.astimezone(timezone.utc).time().isoformat()


# daily_prices.fetched_at 一律以 UTC ISO 字串保存（store_prices 與遷移都會正規化），
# 字串比較等同時間比較；trade_date 是台北日期，定案時間換成 UTC 後拼成同格式字串。
FINALIZED_FETCH_UTC_SUFFIX = "T%s+00:00" % _finalized_utc_clock()


def finalized_rows_sql(alias: str = "daily_prices") -> str:
    """SQL 條件：非官方來源的列必須在該交易日定案時間之後抓取；官方來源不受限。"""
    sources = ", ".join("'%s'" % item for item in UNOFFICIAL_PRICE_SOURCES)
    return "(%s.source NOT IN (%s) OR %s.fetched_at >= %s.trade_date || '%s')" % (
        alias,
        sources,
        alias,
        alias,
        FINALIZED_FETCH_UTC_SUFFIX,
    )
