"""建立隔離的回測資料庫：以重建的可得時間取代事後補抓的 fetched_at。

原始資料庫不會被修改。複本內每一筆被改寫的時間都記錄在
``backtest_reconstruction``，原始抓取時間不遺失；報告必須揭露這是推導值。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict

from etf_agent.core import parse_aware_time

from .contracts import AVAILABILITY_BASIS, ReplayError, available_at_for

_TABLE = """
CREATE TABLE IF NOT EXISTS backtest_reconstruction (
    table_name TEXT NOT NULL,
    row_key INTEGER NOT NULL,
    original_fetched_at TEXT NOT NULL,
    reconstructed_fetched_at TEXT NOT NULL,
    basis TEXT NOT NULL,
    PRIMARY KEY (table_name, row_key)
)
"""


def prepare_backtest_database(source: Path, target: Path, end: str) -> Dict[str, object]:
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise ReplayError("回測資料庫不得與正式資料庫相同")
    if target.exists():
        raise ReplayError("回測資料庫已存在，拒絕覆寫：%s" % target)
    if not source.is_file():
        raise ReplayError("找不到正式資料庫：%s" % source)
    target.parent.mkdir(parents=True, exist_ok=True)
    origin = sqlite3.connect("file:%s?mode=ro" % source, uri=True)
    copy = sqlite3.connect(target)
    try:
        origin.backup(copy)
    finally:
        origin.close()
    try:
        copy.execute(_TABLE)
        rewritten_prices = 0
        payload_times: Dict[int, str] = {}
        rows = copy.execute(
            "SELECT rowid, trade_date, fetched_at, raw_payload_id FROM daily_prices"
        ).fetchall()
        for rowid, trade_date, fetched_at, payload_id in rows:
            reconstructed = available_at_for(trade_date)
            if parse_aware_time(fetched_at, "fetched_at") <= parse_aware_time(reconstructed, "reconstructed"):
                continue
            copy.execute(
                "INSERT INTO backtest_reconstruction VALUES ('daily_prices', ?, ?, ?, ?)",
                (rowid, fetched_at, reconstructed, AVAILABILITY_BASIS),
            )
            copy.execute("UPDATE daily_prices SET fetched_at = ? WHERE rowid = ?", (reconstructed, rowid))
            rewritten_prices += 1
            if payload_id is not None:
                # 一份原始回應可含多個交易日；它最早只能在其中最早一天的資料可得時才存在。
                previous = payload_times.get(payload_id)
                if previous is None or reconstructed < previous:
                    payload_times[payload_id] = reconstructed
        rewritten_payloads = 0
        for payload_id, reconstructed in payload_times.items():
            original = copy.execute("SELECT fetched_at FROM raw_payloads WHERE id = ?", (payload_id,)).fetchone()
            if original is None or parse_aware_time(original[0], "fetched_at") <= parse_aware_time(reconstructed, "reconstructed"):
                continue
            copy.execute(
                "INSERT INTO backtest_reconstruction VALUES ('raw_payloads', ?, ?, ?, ?)",
                (payload_id, original[0], reconstructed, AVAILABILITY_BASIS),
            )
            copy.execute("UPDATE raw_payloads SET fetched_at = ? WHERE id = ?", (reconstructed, payload_id))
            rewritten_payloads += 1
        # 區間結束日前還沒有任何官方成交資料的股票（例如區間後才掛牌）在當時不存在，
        # 從回測交易池移除；否則 Snapshot 會因缺一檔行情而永遠不可用。
        excluded = [
            row[0] for row in copy.execute(
                """SELECT symbol FROM instruments WHERE in_competition_universe = 1 AND symbol NOT IN (
                       SELECT symbol FROM daily_prices WHERE trade_date <= ?) ORDER BY symbol""",
                (end,),
            )
        ]
        for symbol in excluded:
            copy.execute("UPDATE instruments SET in_competition_universe = 0 WHERE symbol = ?", (symbol,))
        copy.commit()
    finally:
        copy.close()
    return {
        "basis": AVAILABILITY_BASIS,
        "rewritten_daily_prices": rewritten_prices,
        "rewritten_raw_payloads": rewritten_payloads,
        "excluded_symbols_not_yet_trading": excluded,
    }
