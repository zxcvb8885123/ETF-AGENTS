"""以 yfinance 讀取 Yahoo Finance 日線；只保存原始收盤，不解讀、不核准來源。"""
import importlib
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from etf_agent.core import canonical_sha256, decimal_string
from .contracts import LOOKBACK_CALENDAR_DAYS, SOURCE_ID, SYMBOLS, YAHOO_CHART_PREFIX, OvernightMarketError


def chart_url(symbol):
    """識別該標的 Yahoo 圖表端點；實際傳輸由 yfinance 負責，此網址用於稽核與白名單檢查。"""
    return YAHOO_CHART_PREFIX + urllib.parse.quote(symbol, safe="")


def raw_text(symbol, bars):
    return json.dumps({"symbol": symbol, "bars": bars}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def yfinance_bars(symbol, start, end):
    """預設抓法：日線未調整收盤，回傳 [{date, close}]；NaN 與非正值略過。"""
    try:
        yf = importlib.import_module("yfinance")
    except ImportError as error:
        raise OvernightMarketError("缺少 yfinance；請先安裝 requirements.txt") from error
    frame = yf.Ticker(symbol).history(start=start.isoformat(), end=end.isoformat(), auto_adjust=False, actions=False)
    bars = []
    for index, value in frame["Close"].items():
        close = Decimal(str(value))
        if close.is_finite() and close > 0:
            bars.append({"date": index.date().isoformat(), "close": decimal_string(close.quantize(Decimal("0.0001")))})
    return bars


class OvernightProvider:
    def __init__(self, fetcher=None):
        self.fetcher = fetcher or yfinance_bars

    def capture(self, symbol, now):
        """抓一個標的；失敗時保留原因，不拋出，讓其他標的照常計算。"""
        text, error, status = "", "", "fetched"
        try:
            start = (now - timedelta(days=LOOKBACK_CALENDAR_DAYS)).date()
            bars = self.fetcher(symbol, start, (now + timedelta(days=1)).date())
            if not bars:
                raise OvernightMarketError("原始回應為空")
            text = raw_text(symbol, sorted(bars, key=lambda bar: bar["date"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            status, error, text = "failed", str(exc) or type(exc).__name__, ""
        return {"source": SOURCE_ID, "symbol": symbol, "url": chart_url(symbol), "raw_text": text,
                "raw_sha256": canonical_sha256(text), "available_at": now.astimezone(timezone.utc).isoformat(),
                "status": status, "error": error}

    def capture_all(self, now=None):
        now = now or datetime.now(timezone.utc)
        return [self.capture(symbol, now) for symbol in SYMBOLS]
