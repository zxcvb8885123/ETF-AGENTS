"""由官方行情缺口推斷臨時休市日（例如颱風假），並保存稽核紀錄。

官方開休市日期表可能來不及列入臨時休市。本模組只在證據很強時才推斷：
日曆認定為交易日的某一天，資料庫「完全沒有」該日的任何行情，而且上海市場與
櫃買市場都在該日之前與之後各有官方行情（表示日線歷史跨過這一天，卻只缺這一天）。

同一天晚上不推斷：全市場彙總端點更新較晚，當日晚間仍可能回傳前一日資料，
會把正常交易日誤判為休市。要等後一個交易日的行情進來才確認。

來源抓取失敗、只有單一市場、或該日有任何行情時，一律不推斷（維持原本的停止行為）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Mapping

from etf_agent.core import canonical_sha256
from .evidence import TAIPEI_TIMEZONE
from .trading_calendar import TradingCalendar, TradingCalendarError


INFERRED_CLOSED_FILE = "inferred_closed_days.json"
# 與行情資料庫同放 var/：清空 artifacts/ 或開新帳戶都不會弄丟，之後也不必重新推斷。
DEFAULT_STATE_DIR = Path(__file__).resolve().parents[3] / "var"
INFERENCE_BASIS = "official_price_gap_confirmed_by_later_day"
# 只看官方行情來源；Yahoo 等補充來源不作為「有沒有開市」的證據。
OFFICIAL_PRICE_SOURCES: Mapping[str, tuple] = {
    "TWSE": ("TWSE_STOCK_DAY", "TWSE_STOCK_DAY_ALL"),
    "TPEX": ("TPEX_TRADING_STOCK", "TPEX_DAILY_QUOTES_HISTORY", "TPEX_MAINBOARD_QUOTES"),
}
LOOKBACK_DAYS = 14


def inferred_closed_path(state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    return Path(state_dir) / INFERRED_CLOSED_FILE


def load_inferred_closed_days(state_dir: Path = DEFAULT_STATE_DIR) -> Dict[str, Mapping[str, object]]:
    """讀取已保存的臨時休市日；檔案不存在回空；格式錯誤即拒絕，不忽略。"""
    path = inferred_closed_path(state_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        days = payload["closed_days"]
        if not isinstance(days, dict):
            raise ValueError("closed_days 必須是物件")
        for key, record in days.items():
            if date.fromisoformat(key).weekday() >= 5 or not isinstance(record, dict):
                raise ValueError("%s 不是有效的臨時休市紀錄" % key)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise TradingCalendarError("臨時休市日紀錄無法讀取：%s（%s）" % (path, error)) from error
    return days


def _market_dates(connection: sqlite3.Connection, market: str) -> Dict[str, int]:
    sources = OFFICIAL_PRICE_SOURCES[market]
    rows = connection.execute(
        "SELECT trade_date, COUNT(DISTINCT symbol) AS symbols FROM daily_prices WHERE source IN (%s) GROUP BY trade_date"
        % ",".join("?" * len(sources)),
        sources,
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def infer_closed_days(database: Path, calendar: TradingCalendar, today: date) -> Dict[str, Dict[str, object]]:
    """回傳 ``today`` 前 LOOKBACK_DAYS 天內、依行情缺口推斷的臨時休市日與證據。"""
    inferred: Dict[str, Dict[str, object]] = {}
    with sqlite3.connect("file:%s?mode=ro" % database, uri=True) as connection:
        market_dates = {market: _market_dates(connection, market) for market in OFFICIAL_PRICE_SOURCES}
        any_dates = {str(row[0]) for row in connection.execute("SELECT DISTINCT trade_date FROM daily_prices")}
    for offset in range(1, LOOKBACK_DAYS + 1):
        day = today - timedelta(days=offset)
        iso = day.isoformat()
        try:
            if not calendar.is_trading_day(day):
                continue
        except TradingCalendarError:
            continue
        if iso in any_dates:
            continue
        evidence: Dict[str, object] = {}
        for market, dates in market_dates.items():
            before = max((item for item in dates if item < iso), default=None)
            after = min((item for item in dates if item > iso and item <= today.isoformat()), default=None)
            if before is None or after is None:
                break
            evidence[market] = {"last_date_before": before, "first_date_after": after,
                                "symbols_on_first_date_after": dates[after]}
        else:
            inferred[iso] = {"basis": INFERENCE_BASIS, "database_rows_on_day": 0, "markets": evidence}
    return inferred


def update_inferred_closed_days(
    database: Path, state_dir: Path, calendar: TradingCalendar, now: datetime
) -> List[str]:
    """推斷並保存新的臨時休市日；回傳這次新增的日期。已保存的紀錄不改寫、不刪除。"""
    existing = dict(load_inferred_closed_days(state_dir))
    found = infer_closed_days(database, calendar.with_inferred_closed(existing), now.astimezone(TAIPEI_TIMEZONE).date())
    added = sorted(day for day in found if day not in existing)
    if not added:
        return []
    for day in added:
        record = dict(found[day], inferred_at=now.isoformat())
        record["record_sha256"] = canonical_sha256(record)
        existing[day] = record
    path = inferred_closed_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"version": 1, "closed_days": existing}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return added
