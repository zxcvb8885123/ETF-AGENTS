"""估值收集服務（V1）：抓官方與 FinMind、逐檔核對、不可變保存。只收集與稽核，不進決策與簡報。

每日排程只能「盡力而為」呼叫：呼叫端須自己捕捉例外，估值失敗不得讓 D-Plan 失敗。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

from etf_agent.core import canonical_sha256, parse_aware_time

from .contracts import (
    DEFAULT_HISTORY_DAYS, FINMIND_DATASET, FINMIND_SOURCE, OFFICIAL_ENDPOINT, OFFICIAL_SOURCE, ValuationError,
)
from .provider import FinMindValuationProvider, OfficialValuationProvider
from .reconcile import build_run, parse_official
from .repository import valuation_store


def listed_codes(universe) -> list:
    """只收上市股；上櫃端點確認前一律不收。"""
    return [item.code for item in universe if item.symbol.endswith(".TW")]


class ValuationCollector:
    def __init__(self, root: Path, official: OfficialValuationProvider, finmind: FinMindValuationProvider,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.root, self.official, self.finmind, self.clock = root, official, finmind, clock

    def collect(self, codes: Sequence[str], history_start: Optional[date] = None) -> Dict[str, object]:
        if not codes or len(set(codes)) != len(codes):
            raise ValuationError("股票代號清單必須非空且不重複")
        fetched = self.clock()
        fetched_at = parse_aware_time(fetched.isoformat(), "fetched_at").isoformat()
        raw = self.official.fetch()
        official_raw = {"source": OFFICIAL_SOURCE, "endpoint": OFFICIAL_ENDPOINT, "fetched_at": fetched_at,
                        "raw": raw, "raw_sha256": canonical_sha256(raw)}
        data_date, _ = parse_official(raw, fetched_at)  # 欄位名變更、日期不一致即整份拒絕、不保存
        start = history_start or data_date - timedelta(days=DEFAULT_HISTORY_DAYS)
        if start > data_date:
            raise ValuationError("history_start 不得晚於資料日")
        responses: Dict[str, Dict[str, object]] = {}
        unavailable = None
        for code in codes:
            if unavailable is not None:  # 額度或連線錯誤後不再硬打，其餘逐檔記為不可用
                responses[code] = {"error": unavailable}
                continue
            try:
                text = self.finmind.fetch_per(code, start, data_date)
            except (RuntimeError, ValueError) as error:
                unavailable = str(error)
                responses[code] = {"error": unavailable}
                continue
            responses[code] = {"raw": text, "raw_sha256": canonical_sha256(text)}
        finmind_raw = {"source": FINMIND_SOURCE, "dataset": FINMIND_DATASET, "fetched_at": fetched_at,
                       "history_start": start.isoformat(), "history_end": data_date.isoformat(),
                       "codes": list(codes), "responses": responses}
        records, history, audit = build_run(official_raw, finmind_raw, codes)
        run_id = "valuation-%s" % fetched.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = valuation_store(self.root).save(
            run_id, {"official_raw": official_raw, "finmind_raw": finmind_raw, "records": records,
                     "history": history, "audit": audit},
            day=fetched.astimezone(timezone(timedelta(hours=8))).date().isoformat())
        return {"run_id": run_id, "output": str(output), "fetched_at": fetched_at, **audit}
