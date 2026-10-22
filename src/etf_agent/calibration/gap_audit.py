"""唯讀審計：決策用的 T-1 官方收盤，與 T 日實際成交均價、開盤差多少。

比賽以 T 日成交均價成交，決策時只有 T-1 收盤，兩者的差距就是 D-Plan 現金比檢核與
單股權重估算需要預留的緩衝。本模組只讀 SQLite，不被決策路徑匯入。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from etf_agent.core import parse_aware_time
from etf_agent.virtual_account.daily import OFFICIAL_SOURCES, average_price

from .contracts import CALENDAR_BASIS, SCHEMA_VERSION, CalibrationError
from .tools import format_decimal, percentile

MAX_CALENDAR_GAP = 5  # 相鄰兩個資料日相隔超過這麼多日曆天，視為資料缺口或長假，不當作「前一個交易日」。
LIMIT_GAP = Decimal("0.095")  # 台股單日漲跌幅 10%；開盤或收盤逼近 9.5% 視為觸及漲跌停。
THRESHOLDS = (Decimal("0.03"), Decimal("0.05"), Decimal("0.09"))
PERCENTILES = (("p1", Decimal("0.01")), ("p5", Decimal("0.05")), ("p50", Decimal("0.5")), ("p95", Decimal("0.95")), ("p99", Decimal("0.99")))


def _distribution(values: List[Decimal]) -> Dict[str, Optional[str]]:
    ordered = sorted(values)
    return {name: format_decimal(percentile(ordered, fraction)) for name, fraction in PERCENTILES}


def execution_gap_report(database: Path, as_of: str, days: int = 60) -> Dict[str, object]:
    if days < 1:
        raise CalibrationError("days 必須為正整數")
    observed = parse_aware_time(as_of, "as_of", error=CalibrationError).isoformat()
    sources = {source: (market, rank) for market, group in OFFICIAL_SOURCES.items() for rank, source in enumerate(group)}
    connection = sqlite3.connect("file:%s?mode=ro" % Path(database), uri=True)
    try:
        rows = connection.execute(
            """
            SELECT daily_prices.symbol, trade_date, open_price, high_price, low_price, close_price,
                   volume_shares, trade_value, source, fetched_at, COALESCE(ex_rights_marker, 0)
            FROM daily_prices JOIN instruments ON instruments.symbol = daily_prices.symbol
            WHERE instruments.in_competition_universe = 1 AND fetched_at <= ? AND source IN (%s)
            """ % ",".join("?" * len(sources)),
            (observed, *sources),
        ).fetchall()
    finally:
        connection.close()
    best: Dict[tuple, tuple] = {}  # (symbol, 日期) → (來源優先序, 資料列)
    for row in rows:
        symbol, trade_date, source = str(row[0]).upper(), str(row[1]), str(row[8])
        market, rank = sources[source]
        if market != ("TPEX" if symbol.endswith(".TWO") else "TWSE"):
            continue
        key = (symbol, trade_date)
        current = best.get(key)
        # 優先序小者勝；同優先序取最晚抓取的版本。
        if current is None or rank < current[0] or (rank == current[0] and str(row[9]) > str(current[1][9])):
            best[key] = (rank, row)
    per_day: Dict[str, int] = {}
    for _, trade_date in best:
        per_day[trade_date] = per_day.get(trade_date, 0) + 1
    # 只用股票覆蓋足夠的日子；歷史補抓留下的零星日期不當作交易日。
    floor = max(per_day.values(), default=0) // 2
    dates = sorted(day for day, total in per_day.items() if total >= floor)
    window = dates[-(days + 1):]
    previous = {day: window[index - 1] for index, day in enumerate(window) if index > 0}
    vwap_gaps: List[Decimal] = []
    open_gaps: List[Decimal] = []
    missing_vwap: List[Dict[str, str]] = []
    open_limit = {"up": 0, "down": 0}
    locked: List[Dict[str, str]] = []
    skipped_ex_rights = 0
    skipped_calendar_gap = 0
    used_days = set()
    for (symbol, trade_date), (_, row) in sorted(best.items()):
        if trade_date not in previous:
            continue
        before = best.get((symbol, previous[trade_date]))
        if before is None:
            continue
        if (date.fromisoformat(trade_date) - date.fromisoformat(previous[trade_date])).days > MAX_CALENDAR_GAP:
            skipped_calendar_gap += 1
            continue
        if row[10] or before[1][10]:
            skipped_ex_rights += 1
            continue
        prev_close = Decimal(str(before[1][5]))
        vwap = average_price(row[7], row[6])
        if vwap is None:
            missing_vwap.append({"symbol": symbol, "trade_date": trade_date})
            continue
        vwap_gaps.append(vwap / prev_close - 1)
        used_days.add(trade_date)
        if row[2] is not None:
            open_gap = Decimal(str(row[2])) / prev_close - 1
            open_gaps.append(open_gap)
            if open_gap >= LIMIT_GAP:
                open_limit["up"] += 1
            elif open_gap <= -LIMIT_GAP:
                open_limit["down"] += 1
        if row[3] is not None and row[3] == row[4] and abs(Decimal(str(row[5])) / prev_close - 1) >= LIMIT_GAP:
            locked.append({"symbol": symbol, "trade_date": trade_date})
    count = len(vwap_gaps)
    return {
        "schema_version": SCHEMA_VERSION, "as_of": observed, "calendar_basis": CALENDAR_BASIS,
        "window": {"first_day": min(used_days, default=None), "last_day": max(used_days, default=None), "trading_days": len(used_days)},
        "pairs": count, "skipped_ex_rights_pairs": skipped_ex_rights, "skipped_calendar_gap_pairs": skipped_calendar_gap,
        "vwap_vs_prev_close": _distribution(vwap_gaps),
        "open_vs_prev_close": _distribution(open_gaps),
        "vwap_gap_share_above": {
            str(threshold): format_decimal(Decimal(sum(abs(gap) > threshold for gap in vwap_gaps)) / count) if count else None
            for threshold in THRESHOLDS
        },
        "open_limit_pairs": open_limit, "locked_at_limit": locked, "missing_vwap": missing_vwap,
    }
