"""以封存的原始回應整份重建並比對；任何欄位被改即拒絕。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from etf_agent.core import canonical_sha256

from .contracts import ValuationError
from .reconcile import build_run
from .repository import valuation_store


def _load(run_dir: Path, name: str) -> Mapping[str, object]:
    return json.loads((run_dir / ("%s.json" % name)).read_text(encoding="utf-8"))


def validate_valuation_run(root: Path, run_id: str) -> Mapping[str, object]:
    store = valuation_store(root)
    run_dir = store.verify(run_id)
    official, finmind = _load(run_dir, "official_raw"), _load(run_dir, "finmind_raw")
    if canonical_sha256(str(official["raw"])) != official.get("raw_sha256"):
        raise ValuationError("官方原始回應雜湊不符")
    for code, response in finmind.get("responses", {}).items():
        if "raw" in response and canonical_sha256(response["raw"]) != response.get("raw_sha256"):
            raise ValuationError("FinMind 原始回應雜湊不符：%s" % code)
    codes = finmind["codes"]
    records, history, audit = build_run(official, finmind, codes)
    for name, rebuilt in (("records", records), ("history", history), ("audit", audit)):
        if _load(run_dir, name) != rebuilt:
            raise ValuationError("%s 與原始回應重建結果不一致" % name)
    return audit
