"""競賽 Active Share：主動型 ETF 前 15 大持股基準載入與前 N 大權重選取。

競賽辦法（十三）：投資組合前 15 大 vs. 各檔主動型 ETF 前 15 大，
Active Share = 1/2 × Σ |Wi(投資組合) − Wi(Benchmark)|，不得小於 15%。
權重採占淨值比例，不在前 15 大內重新正規化（兩種讀法中較嚴格者）。
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

from .contracts import DecisionToolError, decimal_value
from .fields import UNLISTED_BENCHMARK_PREFIX, parse_time

ACTIVE_SHARE_TOP_N = 15
HOLDINGS_FIELDS = ["etf_code", "rank", "stock_name", "symbol", "weight", "source_date", "available_at", "source"]


def top_weights(weights: Mapping[str, Decimal], count: int = ACTIVE_SHARE_TOP_N) -> Dict[str, Decimal]:
    """取權重最大的前 count 檔；同權重依代號排序，結果可重算。"""
    ranked = sorted(weights.items(), key=lambda item: (-item[1], item[0]))
    return dict(ranked[:count])


def active_share(portfolio: Mapping[str, Decimal], benchmark: Mapping[str, Decimal]) -> Decimal:
    symbols = set(portfolio) | set(benchmark)
    return Decimal("0.5") * sum(
        (abs(portfolio.get(symbol, Decimal("0")) - benchmark.get(symbol, Decimal("0"))) for symbol in symbols),
        Decimal("0"),
    )


def _approved_sources(approvals_path: Path, cutoff: datetime) -> Dict[str, Mapping[str, object]]:
    try:
        config = json.loads(Path(approvals_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DecisionToolError("無法讀取 Active Share 來源核准檔：%s" % approvals_path) from error
    approved: Dict[str, Mapping[str, object]] = {}
    for item in config.get("sources", []) if isinstance(config, Mapping) else []:
        if not isinstance(item, Mapping) or item.get("license_status") != "approved":
            continue
        if parse_time(item.get("reviewed_at"), "sources.reviewed_at") > cutoff:
            continue
        approved[str(item.get("source"))] = item
    return approved


def load_active_etf_benchmarks(
    holdings_path: Path,
    approvals_path: Path,
    required_ids: Sequence[str],
    decision_cutoff: str,
) -> List[Dict[str, object]]:
    """讀取主動型 ETF 前 15 大持股，轉成 DecisionInputBundle.benchmarks；任何缺漏或不一致都拒絕。"""
    cutoff = parse_time(decision_cutoff, "decision_cutoff")
    approved = _approved_sources(approvals_path, cutoff)
    try:
        raw = Path(holdings_path).read_bytes()
    except OSError as error:
        raise DecisionToolError("無法讀取主動型 ETF 持股檔：%s" % holdings_path) from error
    rows = list(csv.DictReader(raw.decode("utf-8").splitlines()))
    if not rows or list(rows[0].keys()) != HOLDINGS_FIELDS:
        raise DecisionToolError("主動型 ETF 持股檔欄位必須為：%s" % ",".join(HOLDINGS_FIELDS))
    version = "active-etf-holdings:%s" % hashlib.sha256(raw).hexdigest()[:12]
    grouped: Dict[str, List[Mapping[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["etf_code"].strip().upper(), []).append(row)
    benchmarks: List[Dict[str, object]] = []
    for etf_code in sorted({str(item).upper() for item in required_ids}):
        etf_rows = grouped.get(etf_code)
        if not etf_rows:
            raise DecisionToolError("主動型 ETF 持股檔缺少 %s" % etf_code)
        if len(etf_rows) > ACTIVE_SHARE_TOP_N:
            raise DecisionToolError("%s 持股超過前 %d 大" % (etf_code, ACTIVE_SHARE_TOP_N))
        ranks = sorted(int(row["rank"]) for row in etf_rows)
        if ranks != list(range(1, len(etf_rows) + 1)):
            raise DecisionToolError("%s 持股排名必須自 1 起連續" % etf_code)
        weights: Dict[str, Decimal] = {}
        available = None
        available_text = ""
        for row in etf_rows:
            if row["source"] not in approved:
                raise DecisionToolError("%s 的持股來源 %s 未經核准" % (etf_code, row["source"]))
            row_available = parse_time(row["available_at"], "%s.available_at" % etf_code)
            if row_available > cutoff:
                raise DecisionToolError("%s 持股晚於 decision_cutoff 才可得" % etf_code)
            if available is None or row_available > available:
                available, available_text = row_available, row["available_at"].strip()
            symbol = row["symbol"].strip().upper()
            name = row["stock_name"].strip()
            if not symbol and not name:
                raise DecisionToolError("%s 持股缺少代號與名稱" % etf_code)
            key = symbol or UNLISTED_BENCHMARK_PREFIX + name
            if key in weights:
                raise DecisionToolError("%s 持股重複：%s" % (etf_code, key))
            weight = decimal_value(row["weight"], "%s.weight" % etf_code)
            if weight <= 0 or weight > 1:
                raise DecisionToolError("%s.%s 權重必須介於 0 與 1" % (etf_code, key))
            weights[key] = weight
        benchmarks.append({
            "benchmark_id": etf_code,
            "version": version,
            "available_at": available_text,
            "weights": {key: str(value) for key, value in weights.items()},
        })
    return benchmarks
