"""封存已列明授權候選的政府開放 CSV；不產生交易許可。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen


OGD_SOURCE_IDS = frozenset({
    "TPEX_SPECIAL_OGD", "TPEX_ATTENTION_OGD", "TPEX_DISPOSAL_OGD",
    "TPEX_HALTS_HISTORY_OGD", "TWSE_SPECIAL_OGD", "TWSE_DISPOSAL_OGD",
    "TWSE_HALTS_OGD", "TWSE_CALENDAR_OGD",
})
ALLOWED_HOSTS = frozenset({"www.tpex.org.tw", "www.twse.com.tw"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def capture_ogd_candidates(
    report: Mapping[str, Any],
    output_dir: Path,
    *,
    fetch: Callable[..., Any] = urlopen,
    now: Callable[[], str] = _utc_now,
) -> Dict[str, object]:
    """每次建立獨立封存目錄；任何失敗都不宣稱來源可用。"""

    sources = report.get("sources")
    if not isinstance(sources, list) or {item.get("source_id") for item in sources if isinstance(item, dict)} != OGD_SOURCE_IDS or len(sources) != len(OGD_SOURCE_IDS):
        raise ValueError("候選來源必須恰為已稽核的八份政府開放 CSV")
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("候選來源設定必須是物件")
        parsed = urlparse(str(source.get("url", "")))
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password:
            raise ValueError("候選來源 URL 不在官方 HTTPS 主機清單")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    results = []
    for source in sources:
        source_id = source["source_id"]
        request = Request(source["url"], headers={"User-Agent": "ETF-Agent-AICUP-2026/0.1"})
        started_at = now()
        try:
            with fetch(request, timeout=30) as response:
                raw = response.read()
                status = response.status
                headers = response.headers
            finished_at = now()
            raw_path = run_dir / (source_id + ".csv")
            raw_path.write_bytes(raw)
            if status != 200:
                raise ValueError("HTTP 狀態不是 200：%s" % status)
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError("CSV 欄位缺失或重複")
            rows = list(reader)
            if any(None in row or any(value is None for value in row.values()) for row in rows):
                raise ValueError("CSV 列欄位數不一致")
            results.append({
                "source_id": source_id,
                "status": "captured_candidate_only",
                "url": source["url"],
                "dataset_url": source.get("dataset_url"),
                "fetch_started_at": started_at,
                "fetch_finished_at": finished_at,
                "http_status": status,
                "response_date": headers.get("Date"),
                "last_modified": headers.get("Last-Modified"),
                "etag": headers.get("ETag"),
                "content_type": headers.get("Content-Type"),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "row_count": len(rows),
                "raw_path": str(raw_path),
                "available_at_verified": False,
            })
        except (OSError, UnicodeError, ValueError, csv.Error) as error:
            raw_path = run_dir / (source_id + ".csv")
            results.append({
                "source_id": source_id,
                "status": "failed",
                "url": source["url"],
                "fetch_started_at": started_at,
                "fetch_finished_at": now(),
                "reason": str(error),
                "raw_path": str(raw_path) if raw_path.exists() else None,
                "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest() if raw_path.exists() else None,
                "available_at_verified": False,
            })
    manifest = {
        "run_id": run_id,
        "status": "captured_candidate_only" if all(item["status"] == "captured_candidate_only" for item in results) else "partial_failure",
        "approved_sources": 0,
        "sources": results,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
