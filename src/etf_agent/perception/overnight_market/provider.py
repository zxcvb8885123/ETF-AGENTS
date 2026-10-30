"""以 yfinance 讀取 Yahoo Finance 日線；只保存原始收盤，不解讀、不核准來源。"""
import importlib
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from etf_agent.core import canonical_sha256, decimal_string
from .contracts import (
    LOOKBACK_CALENDAR_DAYS, NIGHT_SYMBOL, SOURCE_ID, SYMBOLS_V11, TAIFEX_QUOTE_URL, TAIFEX_SOURCE_ID, YAHOO_CHART_PREFIX,
    OvernightMarketError,
)


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


_NIGHT_FIELDS = ("SymbolID", "CDate", "CTime", "CLastPrice", "CRefPrice", "CTotalVolume")
_QUOTE_REQUEST = {"MarketType": "1", "SymbolType": "F", "KindID": "1", "CID": "TXF", "ExpireMonth": "",
                  "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"}


def taifex_night_rows():
    """期交所官網即時行情的臺指期月合約報價（盤後時段結束後即為夜盤最後報價）；只留需要的欄位。"""
    request = urllib.request.Request(
        TAIFEX_QUOTE_URL, data=json.dumps(_QUOTE_REQUEST, ensure_ascii=False).encode("utf-8"),
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload["RtData"]["QuoteList"]
    return [{field: str(row.get(field, "")) for field in _NIGHT_FIELDS}
            for row in rows if re.fullmatch(r"TXF[A-L]\d-M", str(row.get("SymbolID", "")))]


def night_raw_text(rows):
    return json.dumps({"rows": sorted(rows, key=lambda row: row["SymbolID"])}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


class OvernightProvider:
    def __init__(self, fetcher=None, night_fetcher=None):
        self.fetcher = fetcher or yfinance_bars
        self.night_fetcher = night_fetcher or taifex_night_rows

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

    def capture_night(self, now):
        """抓台指期夜盤報價；失敗時保留原因，不拋出。"""
        text, error, status = "", "", "fetched"
        try:
            rows = self.night_fetcher()
            if not rows:
                raise OvernightMarketError("原始回應為空")
            text = night_raw_text(rows)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            status, error, text = "failed", str(exc) or type(exc).__name__, ""
        return {"source": TAIFEX_SOURCE_ID, "symbol": NIGHT_SYMBOL, "url": TAIFEX_QUOTE_URL, "raw_text": text,
                "raw_sha256": canonical_sha256(text), "available_at": now.astimezone(timezone.utc).isoformat(),
                "status": status, "error": error}

    def capture_all(self, now=None):
        now = now or datetime.now(timezone.utc)
        return [self.capture(symbol, now) for symbol in SYMBOLS_V11] + [self.capture_night(now)]
