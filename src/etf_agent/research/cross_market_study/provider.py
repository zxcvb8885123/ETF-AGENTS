"""抓兩年的歷史：Yahoo 日線（指數、ETF、台股加權指數 OHLC）與 FinMind 台指期盤後；只保存原始值，不解讀。"""
import importlib
import json
import os
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal

from etf_agent.core import decimal_string
from .contracts import NIGHT_FUTURES_ID, CrossMarketStudyError

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


def yahoo_ohlc(symbol, start: date, end: date):
    """日線未調整 OHLC；任一價非正或非有限的列略過。"""
    try:
        yf = importlib.import_module("yfinance")
    except ImportError as error:
        raise CrossMarketStudyError("缺少 yfinance；請先安裝 requirements.txt") from error
    frame = yf.Ticker(symbol).history(start=start.isoformat(), end=end.isoformat(), auto_adjust=False, actions=False)
    rows = []
    for index, row in frame.iterrows():
        values = {name: Decimal(str(row[name])) for name in ("Open", "High", "Low", "Close")}
        if all(value.is_finite() and value > 0 for value in values.values()):
            rows.append({"date": index.date().isoformat(),
                         **{name.lower(): decimal_string(value.quantize(Decimal("0.0001"))) for name, value in values.items()}})
    if not rows:
        raise CrossMarketStudyError("%s 沒有任何日線" % symbol)
    return sorted(rows, key=lambda item: item["date"])


def finmind_night_rows(start: date, end: date, token=None):
    """FinMind TaiwanFuturesDaily 的 TX 盤後列（after_market）與日盤結算列（position）；只留月合約。"""
    token = token if token is not None else os.environ.get("FINMIND_TOKEN", "")
    if not token:
        raise CrossMarketStudyError("缺少 FINMIND_TOKEN 環境變數")
    query = urllib.parse.urlencode({"dataset": "TaiwanFuturesDaily", "data_id": NIGHT_FUTURES_ID,
                                    "start_date": start.isoformat(), "end_date": end.isoformat()})
    request = urllib.request.Request(FINMIND_URL + "?" + query, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != 200 or not isinstance(payload.get("data"), list):
        raise CrossMarketStudyError("FinMind 回應異常：%s" % payload.get("msg"))
    keep = ("date", "contract_date", "close", "spread", "volume", "settlement_price", "trading_session")
    return [{name: str(row.get(name)) for name in keep} for row in payload["data"]
            if str(row.get("contract_date", "")).isdigit() and len(str(row["contract_date"])) == 6
            and row.get("trading_session") in ("after_market", "position")]


def pool_history(database, symbols, start: date, end: date, as_of: str):
    """交易池每日 OHLC（Yahoo，未調整）與官方成交均價（官方成交金額 ÷ 股數，只有資料庫有官方列的日子）。

    只讀 SQLite；同一股票同一天有多筆時取最後抓到的。除權息標記一併保留，分析時排除該股該日。"""
    import sqlite3
    from pathlib import Path
    from etf_agent.calibration.prices import OfficialPriceBook

    symbols = sorted({symbol.upper() for symbol in symbols})
    marks = ",".join("?" * len(symbols))
    with sqlite3.connect("file:%s?mode=ro" % Path(database), uri=True) as connection:
        rows = connection.execute(
            "SELECT symbol, trade_date, open_price, high_price, low_price, close_price, ex_rights_marker FROM daily_prices "
            "WHERE source = 'YAHOO_FINANCE' AND symbol IN (%s) AND trade_date >= ? AND trade_date <= ? ORDER BY fetched_at" % marks,
            (*symbols, start.isoformat(), end.isoformat())).fetchall()
    yahoo = {}
    for symbol, day, open_, high, low, close, marker in rows:
        values = [open_, high, low, close]
        if any(value is None for value in values):
            continue
        yahoo.setdefault(day, {})[symbol] = {"open": str(open_), "high": str(high), "low": str(low), "close": str(close),
                                             "ex": bool(marker)}
    book = OfficialPriceBook(database, as_of)
    official = {}
    for day in book.trading_days(start.isoformat()):
        table = book.vwap(day)
        if table:
            official[day] = {symbol: decimal_string(price) for symbol, price in table.items()
                             if symbol in symbols and price is not None}
    return {"symbols": symbols, "yahoo": yahoo, "official_vwap": official}
