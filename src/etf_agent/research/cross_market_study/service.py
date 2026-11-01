"""抓歷史、不可變保存、重建與分析；不碰決策鏈。"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from etf_agent.core import canonical_sha256
from etf_agent.core.artifact_store import ImmutableRunStore
from .contracts import (
    DATA_SCHEMA_VERSION, DEFAULT_HISTORY_DAYS, SCHEMA_VERSION, TARGET_INDEX, YAHOO_SYMBOLS, CrossMarketStudyError,
)
from .provider import finmind_night_rows, pool_history, yahoo_ohlc
from .tools import analyze, analyze_pool, build_dataset


def _store(root, schema):
    return ImmutableRunStore(Path(root), schema_version=schema, error=CrossMarketStudyError, dated=True)


def fetch_history(root, days=DEFAULT_HISTORY_DAYS, now=None, ohlc=None, night=None):
    """抓 Yahoo 日線與 FinMind 夜盤並不可變保存；回傳 run 路徑。"""
    now = now or datetime.now(timezone.utc)
    end, start = (now + timedelta(days=1)).date(), (now - timedelta(days=days)).date()
    ohlc, night = ohlc or yahoo_ohlc, night or finmind_night_rows
    yahoo = {symbol: [{"date": row["date"], "close": row["close"]} for row in ohlc(symbol, start, end)]
             for symbol in YAHOO_SYMBOLS}
    raw = {"fetched_at": now.astimezone(timezone.utc).isoformat(), "start": start.isoformat(), "end": end.isoformat(),
           "target": ohlc(TARGET_INDEX, start, end), "yahoo": yahoo, "night": night(start, end)}
    run_id = "history-%s-%s" % (now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), canonical_sha256(raw)[:12])
    store = _store(Path(root) / "data", DATA_SCHEMA_VERSION)
    path = store.save(run_id, {"raw": raw})
    store.verify(run_id)
    return {"run_id": run_id, "path": str(path), "target_days": len(raw["target"]), "night_rows": len(raw["night"])}


def fetch_pool(root, database, symbols, days=DEFAULT_HISTORY_DAYS, now=None, history=None):
    """讀本機資料庫的交易池日線與官方成交均價並不可變保存（資料庫是可變的，所以先凍結再分析）。"""
    now = now or datetime.now(timezone.utc)
    start, end = (now - timedelta(days=days)).date(), now.date()
    raw = (history or pool_history)(database, symbols, start, end, now.astimezone(timezone.utc).isoformat())
    raw["fetched_at"] = now.astimezone(timezone.utc).isoformat()
    run_id = "pool-%s-%s" % (now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), canonical_sha256(raw)[:12])
    store = _store(Path(root) / "pool", DATA_SCHEMA_VERSION)
    path = store.save(run_id, {"raw": raw})
    store.verify(run_id)
    return {"run_id": run_id, "path": str(path), "symbols": len(raw["symbols"]), "yahoo_days": len(raw["yahoo"]),
            "official_days": len(raw["official_vwap"])}


def latest_pool(root):
    base = Path(root) / "pool"
    manifests = sorted(base.glob("*/pool-*/manifest.json"), key=lambda path: path.parent.name)
    if not manifests:
        return None, None
    run_id = manifests[-1].parent.name
    _store(base, DATA_SCHEMA_VERSION).verify(run_id)
    return run_id, json.loads((manifests[-1].parent / "raw.json").read_text(encoding="utf-8"))


def latest_raw(root):
    base = Path(root) / "data"
    manifests = sorted(base.glob("*/history-*/manifest.json"), key=lambda path: path.parent.name)
    if not manifests:
        raise CrossMarketStudyError("沒有任何歷史資料；請先執行 fetch")
    run_id = manifests[-1].parent.name
    _store(base, DATA_SCHEMA_VERSION).verify(run_id)
    return run_id, json.loads((manifests[-1].parent / "raw.json").read_text(encoding="utf-8"))


def run_analysis(root):
    """重建每日資料列並做事先登記的統計，不可變保存結果。"""
    run_id, raw = latest_raw(root)
    rows = build_dataset(raw)
    result = {"schema_version": SCHEMA_VERSION, "data_run_id": run_id, "data_sha256": canonical_sha256(raw),
              "analysis": analyze(rows)}
    pool_id, pool = latest_pool(root)
    if pool is not None:
        result["pool_run_id"], result["pool_sha256"] = pool_id, canonical_sha256(pool)
        result["pool_analysis"] = analyze_pool(rows, pool)
    study_id = "study-%s" % canonical_sha256(result)[:16]
    store = _store(Path(root) / "studies", SCHEMA_VERSION)
    path = store.save(study_id, {"result": _jsonable(result)}, day=date.today().isoformat())
    store.verify(study_id)
    return dict(_jsonable(result), study_id=study_id, path=str(path))


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))
