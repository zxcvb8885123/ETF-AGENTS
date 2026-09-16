"""Historical monthly price providers for TWSE and Taipei Exchange stocks."""

from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, List, Optional, Tuple

from .twse import DailyPrice, parse_decimal, parse_integer, parse_roc_date
from .universe import Instrument


TWCA_INTERMEDIATE_URL = "https://sslserver.twca.com.tw/cacert/Cyber_SSL_2023.crt"


@dataclass(frozen=True)
class HistoricalResponse:
    prices: Tuple[DailyPrice, ...]
    payload: str
    endpoint: str
    fetched_at: str
    source: str
    warnings: Tuple[str, ...]


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


def tpex_ssl_context(
    opener: Callable[..., object] = urllib.request.urlopen,
) -> ssl.SSLContext:
    """Build a verified context when TPEx omits its public intermediate cert."""

    with opener(TWCA_INTERMEDIATE_URL, timeout=30) as response:
        certificate_der = response.read()
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(certificate_der))
    return context


class HistoricalPriceProvider:
    twse_source = "TWSE_STOCK_DAY"
    tpex_source = "TPEX_TRADING_STOCK"

    def __init__(
        self,
        twse_url: str,
        tpex_url: str,
        timeout_seconds: int = 30,
        user_agent: str = "ETF-Agent-AICUP-2026/0.1",
        opener: Callable[..., object] = urllib.request.urlopen,
        tpex_context: Optional[ssl.SSLContext] = None,
    ):
        self.twse_url = twse_url
        self.tpex_url = tpex_url
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.opener = opener
        self.tpex_context = tpex_context

    def fetch(self, instrument: Instrument, month: date) -> HistoricalResponse:
        is_tpex = instrument.market.upper() in {"TPEX", "OTC", "上櫃"}
        if is_tpex:
            query = urllib.parse.urlencode(
                {
                    "code": instrument.code,
                    "date": month.strftime("%Y/%m/01"),
                    "id": "",
                    "response": "json",
                }
            )
            endpoint = self.tpex_url + "?" + query
        else:
            query = urllib.parse.urlencode(
                {
                    "response": "json",
                    "date": month.strftime("%Y%m01"),
                    "stockNo": instrument.code,
                }
            )
            endpoint = self.twse_url + "?" + query

        request = urllib.request.Request(
            endpoint,
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
        )
        kwargs = {"timeout": self.timeout_seconds}
        if is_tpex and self.tpex_context is not None:
            kwargs["context"] = self.tpex_context
        with self.opener(request, **kwargs) as response:
            payload = response.read().decode("utf-8-sig")
        fetched_at = datetime.now(timezone.utc).isoformat()
        if is_tpex:
            prices, warnings = self.parse_tpex(payload, instrument)
            source = self.tpex_source
        else:
            prices, warnings = self.parse_twse(payload, instrument)
            source = self.twse_source
        return HistoricalResponse(
            prices=tuple(prices),
            payload=payload,
            endpoint=endpoint,
            fetched_at=fetched_at,
            source=source,
            warnings=tuple(warnings),
        )

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
                    )
                )
            except (IndexError, TypeError, ValueError) as error:
                warnings.append("%s 第 %d 列略過：%s" % (instrument.symbol, index, error))
        return prices, warnings

    @staticmethod
    def parse_tpex(payload: str, instrument: Instrument) -> Tuple[List[DailyPrice], List[str]]:
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ValueError("TPEx 歷史行情不是有效 JSON") from error
        if str(raw.get("stat", "")).lower() != "ok" or not raw.get("tables"):
            return [], ["%s TPEx 無資料：%s" % (instrument.symbol, raw.get("stat", "未知狀態"))]
        table = raw["tables"][0]
        prices: List[DailyPrice] = []
        warnings: List[str] = []
        for index, row in enumerate(table.get("data", []), start=1):
            try:
                close = parse_decimal(row[6])
                if close is None:
                    warnings.append("%s 第 %d 列無收盤價" % (instrument.symbol, index))
                    continue
                # TPEx reports volume in lots and value in thousands of NTD.
                volume_lots = parse_integer(row[1])
                value_thousands = parse_integer(row[2])
                prices.append(
                    DailyPrice(
                        symbol=instrument.symbol,
                        code=instrument.code,
                        name=instrument.name,
                        trade_date=parse_roc_date(row[0]),
                        volume_shares=volume_lots * 1000,
                        trade_value=value_thousands * 1000,
                        open_price=parse_decimal(row[3]),
                        high_price=parse_decimal(row[4]),
                        low_price=parse_decimal(row[5]),
                        close_price=close,
                        change=parse_decimal(str(row[7]).replace("X", "")),
                        transactions=parse_integer(row[8]),
                    )
                )
            except (IndexError, TypeError, ValueError) as error:
                warnings.append("%s 第 %d 列略過：%s" % (instrument.symbol, index, error))
        return prices, warnings
