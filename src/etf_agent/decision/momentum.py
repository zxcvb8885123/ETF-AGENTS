"""Deterministic momentum and market-regime calculations."""

from __future__ import annotations

import math
import statistics
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence

from .contracts import DecisionContext, DecisionToolError, canonical_sha256, decimal_value


MOMENTUM_SCHEMA_VERSION = "1.0"
MOMENTUM_ENGINE_VERSION = "1.0.0"
MIN_OBSERVATIONS = 61
MIN_REGIME_COVERAGE = 0.80


def _rounded(value: Optional[float]) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return round(value, 8)


class MomentumEngine:
    """Calculate reproducible features from the bundle's point-in-time bars."""

    def __init__(self, bundle: Mapping[str, object]):
        self.context = DecisionContext(bundle)

    def run(self) -> Dict[str, object]:
        items = [
            self._calculate_symbol(symbol, self.context.price_series[symbol])
            for symbol in sorted(self.context.universe)
        ]
        available = [item for item in items if item["status"] == "available"]
        coverage = len(available) / len(items) if items else 0.0
        errors = [
            "%s 歷史行情不足 %d 筆" % (item["symbol"], MIN_OBSERVATIONS)
            for item in items
            if item["status"] != "available"
        ]
        regime = self._regime(available, coverage)
        body: Dict[str, object] = {
            "schema_version": MOMENTUM_SCHEMA_VERSION,
            "bundle_id": self.context.bundle_id,
            "snapshot_id": self.context.snapshot_id,
            "decision_cutoff": self.context.decision_cutoff,
            "bundle_hash": self.context.bundle_hash,
            "engine_version": MOMENTUM_ENGINE_VERSION,
            "status": "completed" if not errors else "degraded",
            "regime_assessment": regime,
            "items": items,
            "errors": errors,
        }
        body["result_id"] = "momentum:" + canonical_sha256(body)[:20]
        return body

    @staticmethod
    def _calculate_symbol(
        symbol: str, series: Mapping[str, object]
    ) -> Dict[str, object]:
        bars = [dict(item) for item in series["bars"]]
        evidence_ids = [str(series["evidence_id"])]
        base: Dict[str, object] = {
            "symbol": symbol,
            "status": "insufficient" if len(bars) < MIN_OBSERVATIONS else "available",
            "as_of": bars[-1]["trade_date"],
            "observation_count": len(bars),
            "evidence_ids": evidence_ids,
        }
        feature_names = (
            "close",
            "return_5d",
            "return_20d",
            "return_60d",
            "ma20",
            "ma60",
            "above_ma20",
            "above_ma60",
            "atr_14",
            "atr_14_pct",
            "downside_volatility_20d",
            "average_volume_20d",
            "average_traded_value_20d",
        )
        if len(bars) < MIN_OBSERVATIONS:
            return {**base, **{name: None for name in feature_names}}

        closes = [decimal_value(item["close"], "close") for item in bars]
        highs = [decimal_value(item["high"], "high") for item in bars]
        lows = [decimal_value(item["low"], "low") for item in bars]
        volumes = [int(item["volume"]) for item in bars]
        latest = closes[-1]

        def period_return(days: int) -> float:
            return float(latest / closes[-days - 1] - Decimal("1"))

        ma20 = sum(closes[-20:]) / Decimal("20")
        ma60 = sum(closes[-60:]) / Decimal("60")
        returns = [
            float(closes[index] / closes[index - 1] - Decimal("1"))
            for index in range(1, len(closes))
        ]
        downside = [value for value in returns[-20:] if value < 0]
        downside_volatility = (
            statistics.pstdev(downside) if len(downside) >= 2 else 0.0
        )
        true_ranges: List[float] = []
        for index in range(len(bars) - 14, len(bars)):
            previous = closes[index - 1]
            true_ranges.append(
                float(
                    max(
                        highs[index] - lows[index],
                        abs(highs[index] - previous),
                        abs(lows[index] - previous),
                    )
                )
            )
        atr = statistics.mean(true_ranges)
        average_volume = statistics.mean(volumes[-20:])
        traded_values = [
            float(closes[index]) * volumes[index]
            for index in range(len(bars) - 20, len(bars))
        ]
        return {
            **base,
            "close": str(latest),
            "return_5d": _rounded(period_return(5)),
            "return_20d": _rounded(period_return(20)),
            "return_60d": _rounded(period_return(60)),
            "ma20": str(ma20.normalize()),
            "ma60": str(ma60.normalize()),
            "above_ma20": latest >= ma20,
            "above_ma60": latest >= ma60,
            "atr_14": _rounded(atr),
            "atr_14_pct": _rounded(atr / float(latest)),
            "downside_volatility_20d": _rounded(downside_volatility),
            "average_volume_20d": _rounded(float(average_volume)),
            "average_traded_value_20d": _rounded(statistics.mean(traded_values)),
        }

    @staticmethod
    def _regime(
        available: Sequence[Mapping[str, object]], coverage: float
    ) -> Dict[str, object]:
        if not available or coverage < MIN_REGIME_COVERAGE:
            return {
                "status": "unavailable",
                "regime": None,
                "coverage_ratio": _rounded(coverage),
                "breadth_above_ma20": None,
                "breadth_above_ma60": None,
                "bull_threshold": 0.60,
                "bear_threshold": 0.40,
                "reason": "可用歷史行情覆蓋不足 80%，不得判斷市場狀態。",
            }
        breadth20 = sum(bool(item["above_ma20"]) for item in available) / len(available)
        breadth60 = sum(bool(item["above_ma60"]) for item in available) / len(available)
        if breadth20 >= 0.60 and breadth60 >= 0.60:
            regime = "bull"
        elif breadth20 <= 0.40 and breadth60 <= 0.40:
            regime = "bear"
        else:
            regime = "neutral"
        return {
            "status": "available",
            "regime": regime,
            "coverage_ratio": _rounded(coverage),
            "breadth_above_ma20": _rounded(breadth20),
            "breadth_above_ma60": _rounded(breadth60),
            "bull_threshold": 0.60,
            "bear_threshold": 0.40,
            "reason": "依交易池中位於 MA20 與 MA60 之上的股票比例確定性分類。",
        }


class MomentumResultValidator:
    """Recompute the complete result instead of trusting agent-supplied numbers."""

    def __init__(self, bundle: Mapping[str, object]):
        self.bundle = dict(bundle)

    def validate(self, result: Mapping[str, object]) -> List[str]:
        try:
            expected = MomentumEngine(self.bundle).run()
        except DecisionToolError as error:
            return [str(error)]
        if dict(result) != expected:
            return ["MomentumResult 與確定性重算結果不一致"]
        return []
