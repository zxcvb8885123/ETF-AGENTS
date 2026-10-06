"""資料庫候選、原頁核對與全量標註的可重跑入口。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from etf_agent.core import canonical_sha256
from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.data.database import MarketDataDatabase
from etf_agent.data.news_content import save_news_captures
from .contracts import PerceptionToolError
from .news_preparation import prepare_news_candidates
from .news_diagnostic import build_news_diagnostic


def run_news_preparation(database_path: Path, captures_path: Path, output_root: Path, labels_path: Path | None = None) -> dict:
    payload = json.loads(captures_path.read_text(encoding="utf-8"))
    candidates, captures = payload["candidates"], payload["captures"]
    if not candidates or len(candidates) > 5000:
        raise PerceptionToolError("新聞候選數量必須介於 1 至 5000")
    ids = [row["id"] for row in candidates]
    if len(set(ids)) != len(ids):
        raise PerceptionToolError("資料庫候選識別字重複")
    with sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = {row["id"]: dict(row) for row in connection.execute(
            "SELECT * FROM finmind_news_candidates WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids)}
    if any(rows.get(row["id"]) != row for row in candidates):
        raise PerceptionToolError("候選與原資料庫版本不一致，拒絕覆寫或新增候選")
    prepared = prepare_news_candidates(candidates, captures, payload["decision_cutoff"])
    artifacts = {"capture_input": payload, "prepared": prepared}
    if labels_path is not None:
        labels = json.loads(labels_path.read_text(encoding="utf-8"))
        artifacts["labels"] = labels
        artifacts["diagnostic"] = build_news_diagnostic(prepared, labels["labels"])
    run_id = "news-" + canonical_sha256(artifacts)[:20]
    store = ImmutableRunStore(output_root, schema_version="news-preparation-run-1.0", error=PerceptionToolError)
    path = store.save(run_id, artifacts)
    store.verify(run_id)
    saved = save_news_captures(MarketDataDatabase(database_path), captures)
    return {"valid": True, "run_id": run_id, "run_dir": str(path), "new_capture_versions": saved,
            "candidate_rows": len(candidates), "unique_urls": len(prepared["items"]),
            "formal_sentiment_available": False}
