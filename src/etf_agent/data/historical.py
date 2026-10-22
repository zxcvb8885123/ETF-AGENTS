"""Historical monthly price provider for TWSE listed stocks."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .twse import DailyPrice, parse_decimal, parse_integer, parse_roc_date
from .universe import Instrument


@dataclass(frozen=True)
class HistoricalResponse:
    prices: Tuple[DailyPrice, ...]
    payload: str
    endpoint: str
    fetched_at: str
    source: str
    warnings: Tuple[str, ...]


@dataclass(frozen=True)
class HistoryRefreshBatch:
    """一組共用起始日的增量歷史行情請求。"""

    start_date: date
    instruments: Tuple[Instrument, ...]


def rolling_start(end: date, years: int = 2) -> date:
    if years < 1:
        raise ValueError("歷史回看年數必須大於 0")
    try:
        return end.replace(year=end.year - years)
    except ValueError:
        return end.replace(year=end.year - years, day=28)


def plan_history_refresh(
    universe: Sequence[Instrument],
    last_dates: Mapping[str, date],
    end: date,
    lookback_years: int = 2,
    overlap_days: int = 7,
) -> List[HistoryRefreshBatch]:
    """為新標的回補、為既有標的重抓重疊區間。

    停抓超過重疊天數的標的整段重抓：Yahoo 在停抓期間改寫事件前原價時，
    重疊區間已看不到被改寫的舊列，只有整段重抓才能回到同一基準。
    """

    if overlap_days < 0:
        raise ValueError("歷史行情重疊天數不得小於 0")
    floor = rolling_start(end, lookback_years)
    grouped: Dict[date, List[Instrument]] = {}
    for instrument in universe:
        last_date = last_dates.get(instrument.symbol)
        if last_date is None or (end - last_date).days > overlap_days:
            start = floor
        else:
            start = max(floor, last_date - timedelta(days=overlap_days))
        grouped.setdefault(start, []).append(instrument)
    return [
        HistoryRefreshBatch(start_date=start, instruments=tuple(instruments))
        for start, instruments in sorted(grouped.items())
    ]


def month_starts(start: date, end: date) -> List[date]:
    if start > end:
        raise ValueError("歷史資料起日不得晚於迄日")
    months = []
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while cursor <= last:
        months.append(cursor)
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return months


# TWSE 限流時會回 308 轉址循環、403 封鎖，或 429／503；這些都代表要放慢，不是資料不存在。
THROTTLE_STATUS_CODES = frozenset({308, 403, 429, 503})


class SourceThrottledError(RuntimeError):
    """來源回應限流訊號；應等待後重試，不消耗一般錯誤的重試次數。"""


class SourceBlockedError(RuntimeError):
    """退避用盡仍被限流；停止送出後續請求，避免延長封鎖。"""


class RequestGate:
    """跨執行緒共用的請求節流與退避閘門。

    每次請求開始至少間隔 ``min_interval_seconds``。收到限流訊號時，所有執行緒
    一起暫停 ``backoff_seconds`` 中的下一段；退避用盡仍被限流即熔斷，之後的請求
    直接失敗而不送出。暫停前已送出的請求若也被限流，不重複累加退避。
    """

    def __init__(
        self,
        min_interval_seconds: float = 2.0,
        backoff_seconds: Sequence[float] = (30.0, 120.0, 300.0),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if min_interval_seconds < 0 or any(value < 0 for value in backoff_seconds):
            raise ValueError("節流間隔與退避秒數不得小於 0")
        self.min_interval_seconds = float(min_interval_seconds)
        self.backoff_seconds = tuple(float(value) for value in backoff_seconds)
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._next_at: Optional[float] = None
        self._epoch = 0
        self._consecutive = 0
        self.blocked = False
        self.throttle_events = 0

    def acquire(self) -> int:
        """等到可送出請求的時間，回傳目前退避世代供限流回報使用。"""
        with self._lock:
            self._raise_if_blocked()
            now = self.clock()
            start = now if self._next_at is None else max(now, self._next_at)
            self._next_at = start + self.min_interval_seconds
            epoch = self._epoch
        if start > now:
            self.sleep(start - now)
        with self._lock:
            self._raise_if_blocked()
        return epoch

    def success(self) -> None:
        with self._lock:
            self._consecutive = 0

    def throttled(self, epoch: int) -> None:
        with self._lock:
            self.throttle_events += 1
            if epoch != self._epoch:
                return
            self._epoch += 1
            self._consecutive += 1
            if self._consecutive > len(self.backoff_seconds):
                self.blocked = True
                return
            pause = self.backoff_seconds[self._consecutive - 1]
            self._next_at = max(self._next_at or 0.0, self.clock() + pause)

    def _raise_if_blocked(self) -> None:
        if self.blocked:
            raise SourceBlockedError(
                "TWSE 連續限流 %d 次，已停止送出請求；請稍後再補抓" % self._consecutive
            )


class HistoricalPriceProvider:
    twse_source = "TWSE_STOCK_DAY"
    tpex_source = "TPEX_TRADING_STOCK"

    def __init__(
        self,
        twse_url: str,
        timeout_seconds: int = 30,
        user_agent: str = "ETF-Agent-AICUP-2026/0.1",
        opener: Callable[..., object] = urllib.request.urlopen,
        gate: Optional[RequestGate] = None,
        tpex_url: Optional[str] = None,
    ):
        self.twse_url = twse_url
        self.tpex_url = tpex_url
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.opener = opener
        self.gate = gate if gate is not None else RequestGate()

    def source_for(self, instrument: Instrument) -> str:
        market = instrument.market.upper()
        if market == "TWSE":
            return self.twse_source
        if market == "TPEX" and self.tpex_url:
            return self.tpex_source
        raise ValueError("官方個股月補抓不支援 %s（上櫃需設定 tpex_url）" % instrument.market)

    def fetch(self, instrument: Instrument, month: date) -> HistoricalResponse:
        source = self.source_for(instrument)
        if source == self.tpex_source:
            query = urllib.parse.urlencode(
                {"code": instrument.code, "date": month.strftime("%Y/%m/01"), "id": "", "response": "json"}
            )
            endpoint = str(self.tpex_url) + "?" + query
        else:
            query = urllib.parse.urlencode(
                {"response": "json", "date": month.strftime("%Y%m01"), "stockNo": instrument.code}
            )
            endpoint = self.twse_url + "?" + query

        request = urllib.request.Request(
            endpoint,
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
        )
        epoch = self.gate.acquire()
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                payload = response.read().decode("utf-8-sig")
        except urllib.error.HTTPError as error:
            if error.code in THROTTLE_STATUS_CODES:
                self.gate.throttled(epoch)
                raise SourceThrottledError("%s HTTP %d 限流" % (source, error.code)) from None
            raise
        self.gate.success()
        fetched_at = datetime.now(timezone.utc).isoformat()
        if source == self.tpex_source:
            prices, warnings = self.parse_tpex(payload, instrument)
        else:
            prices, warnings = self.parse_twse(payload, instrument)
        return HistoricalResponse(
            prices=tuple(prices),
            payload=payload,
            endpoint=endpoint,
            fetched_at=fetched_at,
            source=source,
            warnings=tuple(warnings),
        )

    @staticmethod
    def parse_tpex(payload: str, instrument: Instrument) -> Tuple[List[DailyPrice], List[str]]:
        """解析 TPEx 個股日成交資訊（月）。

        成交張數、成交仟元換算為股與元，與 TWSE 列同單位。TPEx 漲跌欄沒有 X 標記，
        除權息日以「參考價（收盤 − 漲跌）不等於前一交易日收盤」判定；月內第一列沒有
        前一日可比，標記保持未知（None），不猜測。
        """
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ValueError("TPEx 歷史行情不是有效 JSON") from error
        tables = raw.get("tables") if isinstance(raw, dict) else None
        if not isinstance(tables, list) or not tables or not isinstance(tables[0], dict):
            raise ValueError("TPEx 歷史行情格式錯誤：缺少 tables")
        rows = tables[0].get("data") or []
        if not rows:
            return [], ["%s TPEx 無資料" % instrument.symbol]
        prices: List[DailyPrice] = []
        warnings: List[str] = []
        previous_close: Optional[Decimal] = None
        for index, row in enumerate(rows, start=1):
            try:
                close = parse_decimal(row[6])
                if close is None:
                    warnings.append("%s 第 %d 列無收盤價" % (instrument.symbol, index))
                    continue
                change_text = str(row[7]).strip()
                change = parse_decimal(change_text.replace("+", ""))
                ex_rights: Optional[bool] = None
                if previous_close is not None and change is not None:
                    ex_rights = abs((close - change) - previous_close) > Decimal("0.005")
                prices.append(
                    DailyPrice(
                        symbol=instrument.symbol,
                        code=instrument.code,
                        name=instrument.name,
                        trade_date=parse_roc_date(row[0]),
                        volume_shares=parse_integer(row[1]) * 1000,
                        trade_value=parse_integer(row[2]) * 1000,
                        open_price=parse_decimal(row[3]),
                        high_price=parse_decimal(row[4]),
                        low_price=parse_decimal(row[5]),
                        close_price=close,
                        change=change,
                        transactions=parse_integer(row[8]),
                        ex_rights=ex_rights,
                    )
                )
                previous_close = close
            except (IndexError, TypeError, ValueError) as error:
                warnings.append("%s 第 %d 列略過：%s" % (instrument.symbol, index, error))
        return prices, warnings

    @staticmethod
    def parse_twse(payload: str, instrument: Instrument) -> Tuple[List[DailyPrice], List[str]]:
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ValueError("TWSE 歷史行情不是有效 JSON") from error
        if raw.get("stat") != "OK":
            return [], ["%s TWSE 無資料：%s" % (instrument.symbol, raw.get("stat", "未知狀態"))]
        prices: List[DailyPrice] = []
        warnings: List[str] = []
        for index, row in enumerate(raw.get("data", []), start=1):
            try:
                close = parse_decimal(row[6])
                if close is None:
                    warnings.append("%s 第 %d 列無收盤價" % (instrument.symbol, index))
                    continue
                prices.append(
                    DailyPrice(
                        symbol=instrument.symbol,
                        code=instrument.code,
                        name=instrument.name,
                        trade_date=parse_roc_date(row[0]),
                        volume_shares=parse_integer(row[1]),
                        trade_value=parse_integer(row[2]),
                        open_price=parse_decimal(row[3]),
                        high_price=parse_decimal(row[4]),
                        low_price=parse_decimal(row[5]),
                        close_price=close,
                        change=parse_decimal(str(row[7]).replace("X", "")),
                        transactions=parse_integer(row[8]),
                        # 漲跌價差欄的 X 代表當日除權息，參考價已扣除權息。
                        ex_rights="X" in str(row[7]).upper(),
                    )
                )
            except (IndexError, TypeError, ValueError) as error:
                warnings.append("%s 第 %d 列略過：%s" % (instrument.symbol, index, error))
        return prices, warnings
