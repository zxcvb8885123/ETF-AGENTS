"""技術分析用的整段歷史價格與逐日均線；不產生趨勢標籤。"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping

from etf_agent.core import canonical_sha256, decimal_string, parse_decimal

from .contracts import DecisionContext, DecisionToolError


class TechnicalTrendHistoryTools:
    """只使用已通過 cutoff、版本與引用檢查的行情，完整保留輸入觀測。"""

    def __init__(self, bundle: Mapping[str, object]):
        self.context = DecisionContext(bundle)

    def for_symbol(self, symbol: str) -> Dict[str, object]:
        symbol = symbol.upper()
        if symbol not in self.context.price_series:
            raise DecisionToolError("交易池沒有此股票的歷史行情：" + symbol)
        series = self.context.price_series[symbol]
        bars = series["bars"]
        closes = [parse_decimal(bar["close"], "close", error=DecisionToolError) for bar in bars]
        rows: List[Dict[str, object]] = []
        for index, bar in enumerate(bars):
            row = {
                "trade_date": bar["trade_date"],
                "available_at": bar["available_at"],
                **{field: decimal_string(parse_decimal(bar[field], field, error=DecisionToolError))
                   for field in ("close", "high", "low")},
                "volume": bar["volume"],
            }
            for window in (20, 60):
                # 暖機不足保留 null，不使用之後的價格補足歷史均線。
                mean = None if index + 1 < window else sum(closes[index + 1 - window:index + 1]) / Decimal(window)
                row["ma%d" % window] = None if mean is None else decimal_string(mean.quantize(Decimal("0.000001")))
            rows.append(row)
        body = {
            "schema_version": "1.0",
            "symbol": symbol,
            "snapshot_id": self.context.snapshot_id,
            "decision_cutoff": self.context.decision_cutoff,
            "bundle_hash": self.context.bundle_hash,
            "source_series_sha256": series["series_sha256"],
            "evidence_ids": [series["evidence_id"]],
            "start_date": bars[0]["trade_date"],
            "end_date": bars[-1]["trade_date"],
            "observation_count": len(rows),
            "ma_decimal_places": 6,
            "rows": rows,
            "limitations": ["只涵蓋輸入封存的歷史視窗，不代表全部上市歷史；均線暖機不足為 null。"],
        }
        body["content_sha256"] = canonical_sha256(body)
        return body

    def summary_for_symbol(self, symbol: str) -> Dict[str, object]:
        """固定歷史錨點、極值與近五筆；來源完整序列仍由 DecisionInput 保存。"""
        full = self.for_symbol(symbol)
        rows = full["rows"]
        count = len(rows)
        indices = {index * (count - 1) // 11 for index in range(12)}
        indices.update(range(max(0, count - 5), count))
        indices.add(min(range(count), key=lambda index: parse_decimal(rows[index]["close"], "close", error=DecisionToolError)))
        indices.add(max(range(count), key=lambda index: parse_decimal(rows[index]["close"], "close", error=DecisionToolError)))
        indices.add(max(range(count), key=lambda index: int(rows[index]["volume"])))
        summary = {key: value for key, value in full.items() if key not in {"rows", "content_sha256"}}
        summary["schema_version"] = "1.1-summary"
        summary["rows"] = [rows[index] for index in sorted(indices)]
        summary["sampling"] = "12_fixed_anchors_extrema_recent_5_v1"
        summary["limitations"] = ["僅供 Agent 判斷的抽樣；非完整逐日行情。完整序列由同一 DecisionInput 原始價格與 Validator 重建。", *full["limitations"]]
        summary["content_sha256"] = canonical_sha256(summary)
        return summary


class TechnicalTrendHistoryValidator:
    """從相同原始行情重建全部歷史，不只相信輸出的內容雜湊。"""

    def __init__(self, bundle: Mapping[str, object]):
        self.bundle = bundle

    def validate(self, history: Mapping[str, object], symbol: str) -> List[str]:
        try:
            expected = TechnicalTrendHistoryTools(self.bundle).for_symbol(symbol)
        except DecisionToolError as error:
            return [str(error)]
        return [] if dict(history) == expected else ["TechnicalTrendHistory 與原始行情重建結果不一致"]

    def validate_summary(self, summary: Mapping[str, object], symbol: str) -> List[str]:
        try:
            expected = TechnicalTrendHistoryTools(self.bundle).summary_for_symbol(symbol)
        except DecisionToolError as error:
            return [str(error)]
        return [] if dict(summary) == expected else ["TechnicalTrendHistory 摘要與原始行情重建結果不一致"]
