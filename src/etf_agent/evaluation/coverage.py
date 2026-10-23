"""資料抓齊率：官方來源最新一次回應裡屬於交易池的項目，有多少已存進資料庫。

不連網：只重新解析資料庫裡已保存的原始回應，和已存的文件比對。行情另算每個來源
最新交易日覆蓋交易池幾檔。數字全部由程式計算，只讀資料庫。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

from etf_agent.data.corporate_provider import OfficialCorporateProviderFactory
from etf_agent.data.universe import Instrument

from .contracts import COVERAGE_SCHEMA_VERSION, EvaluationError


_MISSING_LIMIT = 20


def _connect(database_path: Path) -> sqlite3.Connection:
    path = Path(database_path).resolve()
    if not path.exists():
        raise EvaluationError("資料庫不存在：%s" % path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _ratio(found: int, expected: int):
    return round(found / expected, 4) if expected else None


def document_recall(connection: sqlite3.Connection, sources_config: Mapping[str, object],
                    symbols: Sequence[str]) -> List[Dict[str, object]]:
    """每個官方公司資料來源：最新原始回應解析出的交易池項目，資料庫有幾項、內容是否為最新版。"""
    wanted = set(symbols)
    results = []
    for provider in OfficialCorporateProviderFactory().create(sources_config):
        raw = connection.execute(
            "SELECT id, fetched_at, payload_json FROM raw_payloads WHERE source = ? ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (provider.source,),
        ).fetchone()
        row: Dict[str, object] = {"source": provider.source, "document_type": provider.document_type}
        if raw is None:
            results.append({**row, "status": "no_payload", "expected": 0, "found": 0, "current": 0, "recall": None})
            continue
        try:
            records, _, _ = provider.parse(raw["payload_json"], raw["fetched_at"])
        except Exception as error:  # noqa: BLE001 — 解析失敗本身就是要回報的抓取問題
            results.append({**row, "status": "parse_failed", "error": str(error)[:200], "fetched_at": raw["fetched_at"],
                            "expected": 0, "found": 0, "current": 0, "recall": None})
            continue
        expected = [record.document for record in records if record.document.symbol in wanted]
        missing, stale, found, current = [], [], 0, 0
        for document in expected:
            stored = connection.execute(
                "SELECT body FROM source_documents WHERE source = ? AND external_id = ? ORDER BY version DESC LIMIT 1",
                (document.source, document.external_id),
            ).fetchone()
            if stored is None:
                missing.append(document.external_id)
                continue
            found += 1
            if stored["body"] == document.body:
                current += 1
            else:
                stale.append(document.external_id)
        results.append({
            **row, "status": "checked", "fetched_at": raw["fetched_at"], "expected": len(expected),
            "symbols": sorted({document.symbol for document in expected}),
            "found": found, "current": current, "recall": _ratio(found, len(expected)),
            "missing": sorted(missing)[:_MISSING_LIMIT], "stale": sorted(stale)[:_MISSING_LIMIT],
        })
    return results


def universe_coverage(documents: Sequence[Mapping[str, object]], symbols: Sequence[str]) -> List[Dict[str, object]]:
    """月營收與財報：交易池每檔應該都有；列出所有官方來源最新回應都沒有的股票。重大訊息不是每檔都有，不算。"""
    results = []
    for document_type in ("monthly_revenue", "financial_statement"):
        covered = set()
        for row in documents:
            if row.get("document_type") == document_type:
                covered |= set(row.get("symbols", []))
        missing = sorted(set(symbols) - covered)
        results.append({"document_type": document_type, "expected": len(symbols), "found": len(symbols) - len(missing),
                        "recall": _ratio(len(symbols) - len(missing), len(symbols)), "missing": missing[:_MISSING_LIMIT]})
    return results


def price_coverage(connection: sqlite3.Connection, universe: Sequence[Instrument]) -> List[Dict[str, object]]:
    """每個行情來源最新交易日覆蓋交易池幾檔；官方來源只比對同市場（上市 TWSE、上櫃 TPEx）。"""
    by_market: Dict[str, set] = {}
    for item in universe:
        by_market.setdefault(item.market, set()).add(item.symbol)
    everyone = {item.symbol for item in universe}
    results = []
    for source_row in connection.execute("SELECT DISTINCT source FROM daily_prices ORDER BY source").fetchall():
        source = source_row["source"]
        market = "TWSE" if source.startswith("TWSE") else "TPEX" if source.startswith("TPEX") else None
        expected = by_market.get(market, set()) if market else everyone
        if not expected:
            continue
        latest = connection.execute("SELECT MAX(trade_date) FROM daily_prices WHERE source = ?", (source,)).fetchone()[0]
        have = {r[0] for r in connection.execute(
            "SELECT DISTINCT symbol FROM daily_prices WHERE source = ? AND trade_date = ?", (source, latest))}
        missing = sorted(expected - have)
        results.append({"source": source, "latest_trade_date": latest, "expected": len(expected),
                        "found": len(expected) - len(missing), "recall": _ratio(len(expected) - len(missing), len(expected)),
                        "missing": missing[:_MISSING_LIMIT]})
    return results


def data_coverage(database_path: Path, universe: Sequence[Instrument], data_sources: Mapping[str, object]) -> Dict[str, object]:
    if not universe:
        raise EvaluationError("交易池是空的")
    symbols = [item.symbol for item in universe]
    with _connect(database_path) as connection:
        documents = []
        for key in ("corporate_data", "financial_statements"):
            if isinstance(data_sources.get(key), Mapping):
                documents.extend(document_recall(connection, data_sources[key], symbols))
        prices = price_coverage(connection, universe)
        latest = max((row["latest_trade_date"] for row in prices if row["latest_trade_date"]), default=None)
        for row in prices:
            # 只補抓過一次的歷史來源不會每天更新，不算漏抓，另外標示。
            row["current"] = row["latest_trade_date"] == latest
        cash_flow = connection.execute(
            "SELECT COUNT(DISTINCT external_id) FROM source_documents WHERE source = 'FINMIND_VERIFIED_CASH_FLOW'"
        ).fetchone()[0]
    by_universe = universe_coverage(documents, symbols)
    incomplete = [row["source"] for row in documents if row.get("status") == "parse_failed"
                  or (row.get("recall") is not None and row["recall"] < 1)]
    incomplete += [row["source"] for row in prices if row["current"] and row["recall"] < 1]
    incomplete += [row["document_type"] for row in by_universe if row["recall"] < 1]
    return {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database_path": str(Path(database_path).resolve()),
        "universe_size": len(symbols),
        "documents": documents,
        "universe_coverage": by_universe,
        "prices": prices,
        "latest_trade_date": latest,
        "cash_flow_documents": cash_flow,
        "incomplete_sources": sorted(incomplete),
        "status": "complete" if not incomplete else "incomplete",
    }
