"""唯讀候選表的資料取得狀態；不把候選升級成投資證據。"""

import re
import sqlite3
from pathlib import Path

from etf_agent.core import parse_aware_time


def analyst_availability(database_path: Path, symbols, decision_cutoff: str) -> dict:
    cutoff = parse_aware_time(decision_cutoff, "decision_cutoff")
    path = database_path.resolve()
    if not path.is_file():
        raise ValueError("資料庫不存在，不能宣稱已檢查資料取得狀態")
    codes = {}
    for symbol in symbols:
        if not re.fullmatch(r"[0-9A-Z]{4,6}\.(TW|TWO)", symbol):
            raise ValueError("資料查詢股票格式錯誤")
        codes[symbol] = symbol.split(".")[0]
    result = {"decision_cutoff": decision_cutoff, "database_path": str(path), "symbols": []}
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for symbol, code in codes.items():
            entry = {"symbol": symbol, "channels": []}
            for table, condition, args, label in (
                ("supplemental_facts", "stock_id=? AND dataset=?", (code, "TaiwanStockCashFlowsStatement"), "現金流資料"),
                ("finmind_news_candidates", "stock_id=?", (code,), "新聞候選"),
            ):
                if table not in tables:
                    entry["channels"].append({"name": label, "status": "狀態未確認，資料庫沒有對應候選表"})
                    continue
                rows = list(connection.execute("SELECT id,available_at,content_sha256,raw_payload_id FROM " + table + " WHERE " + condition, args))
                eligible = [r for r in rows if parse_aware_time(r["available_at"], "available_at") <= cutoff]
                entry["channels"].append({
                    "name": label, "stored_rows": len(rows), "available_by_cutoff_rows": len(eligible),
                    "after_cutoff_rows": len(rows) - len(eligible),
                    "status": "已取得；期間、單位及官方核對尚未接入" if table == "supplemental_facts" and rows else
                              "已取得；新聞來源使用權及發布時間待確認，尚未進入情緒計算" if rows else "尚未取得本股票候選資料",
                    "earliest_available_at": min((r["available_at"] for r in rows), default=None),
                    "latest_available_at": max((r["available_at"] for r in rows), default=None),
                    "candidate_references": [{"row_id": r["id"], "content_sha256": r["content_sha256"], "raw_payload_id": r["raw_payload_id"]} for r in rows],
                })
            result["symbols"].append(entry)
    return result
