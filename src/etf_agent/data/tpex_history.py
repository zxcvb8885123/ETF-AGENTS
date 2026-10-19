"""按指定成交日補取 TPEx 官方全市場歷史價量，供虛擬帳本結算。"""

import json
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from etf_agent.core import parse_decimal

from .database import MarketDataDatabase
from .twse import DailyPrice


SOURCE = "TPEX_DAILY_QUOTES_HISTORY"
ENDPOINT = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
FIELDS = ("代號", "收盤", "成交股數", "成交金額(元)", "成交筆數")


def parse_quotes(payload: str, trade_date: date, symbols: set[str]) -> list[DailyPrice]:
    data = json.loads(payload)
    expected = trade_date.strftime("%Y%m%d")
    if not isinstance(data, dict) or str(data.get("date")) != expected:
        raise ValueError("TPEx 歷史行情日期與要求成交日不一致")
    found = {}
    if not isinstance(data.get("tables"), list):
        raise ValueError("TPEx 歷史行情沒有表格")
    for table in data["tables"]:
        if not isinstance(table, dict) or not isinstance(table.get("fields"), list) or not all(field in table["fields"] for field in FIELDS):
            continue
        columns = {field: table["fields"].index(field) for field in FIELDS}
        for row in table.get("data", []):
            if not isinstance(row, list) or len(row) <= max(columns.values()):
                raise ValueError("TPEx 歷史行情欄位不完整")
            symbol = str(row[columns["代號"]]).strip() + ".TWO"
            if symbol not in symbols:
                continue
            if symbol in found:
                raise ValueError("TPEx 歷史行情重複股票：" + symbol)
            def number(field):
                return parse_decimal(str(row[columns[field]]).replace(",", ""), field)
            close = number("收盤")
            shares = number("成交股數")
            amount = number("成交金額(元)")
            transactions = number("成交筆數")
            if close <= 0 or any(value < 0 or value != value.to_integral_value() for value in (shares, amount, transactions)):
                raise ValueError("TPEx 歷史行情價量不合法：" + symbol)
            found[symbol] = DailyPrice(symbol, symbol[:-4], "", trade_date.isoformat(), None, None, None,
                                        close, None, int(shares), int(amount), int(transactions))
    missing = symbols - found.keys()
    if missing:
        raise ValueError("TPEx 官方歷史行情缺少持股：" + "、".join(sorted(missing)))
    return [found[symbol] for symbol in sorted(found)]


def collect_quotes(database: Path, trade_date: date, symbols: set[str], *, opener=None) -> dict:
    if not symbols or any(not symbol.endswith(".TWO") for symbol in symbols):
        raise ValueError("補抓範圍必須是上櫃股票")
    if trade_date > datetime.now(timezone.utc).date():
        raise ValueError("不得補抓未來交易日")
    url = ENDPOINT + "?" + urllib.parse.urlencode({"date": trade_date.strftime("%Y/%m/%d"), "response": "json"})
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ETF-Agent-AICUP-2026/0.1"})
    with (opener or urllib.request.urlopen)(request, timeout=30) as response:
        payload = response.read().decode("utf-8-sig")
    fetched_at = datetime.now(timezone.utc).isoformat()
    prices = parse_quotes(payload, trade_date, symbols)
    store = MarketDataDatabase(database)
    store.initialize()
    run_id = "tpex-history-" + uuid.uuid4().hex
    with store.connect() as connection:
        connection.execute("INSERT INTO collection_runs(run_id, source, started_at, finished_at, status, fetched_rows, stored_rows, warning_count) VALUES (?, ?, ?, ?, 'success', ?, ?, 0)",
                           (run_id, SOURCE, fetched_at, fetched_at, len(prices), len(prices)))
        payload_id = store.insert_raw_payload(connection, run_id, SOURCE, url, fetched_at, payload)
        store.upsert_prices(connection, prices, SOURCE, fetched_at, run_id, payload_id)
    return {"run_id": run_id, "source": SOURCE, "trade_date": trade_date.isoformat(), "stored_rows": len(prices), "symbols": sorted(symbols)}
