"""以 cutoff 前可得的歷史行情計算事件研究用價格特徵。"""

from __future__ import annotations

import math
import statistics
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from etf_agent.data import MarketDataDatabase
from .contracts import ResearchToolError, _decimal, _parse_time


class HistoricalPriceFeatureRepository:
    """Read point-in-time prices and calculate reproducible research features."""

    def __init__(self, database: MarketDataDatabase):
        self.database = database

    def get_features(
        self,
        symbol: str,
        decision_cutoff: str,
        latest_trade_date: Optional[str],
        event_published_at: Optional[str] = None,
    ) -> Dict[str, object]:
        if not self.database.path.exists():
            raise ResearchToolError("行情資料庫不存在：%s" % self.database.path)
        cutoff = _parse_time(decision_cutoff, "decision_cutoff").isoformat()
        if not latest_trade_date:
            raise ResearchToolError("Snapshot 缺少 latest_trade_date")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                WITH eligible AS (
                    SELECT daily_prices.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY symbol, trade_date
                               ORDER BY CASE source
                                   WHEN 'TPEX_TRADING_STOCK' THEN 1
                                   WHEN 'TWSE_STOCK_DAY' THEN 1
                                   WHEN 'TWSE_STOCK_DAY_ALL' THEN 2
                                   WHEN 'TPEX_MAINBOARD_QUOTES' THEN 2
                                   WHEN 'YAHOO_FINANCE' THEN 3
                                   ELSE 9
                               END
                           ) AS source_rank
                    FROM daily_prices
                    WHERE symbol = ?
                      AND trade_date <= ?
                      AND fetched_at <= ?
                )
                SELECT * FROM eligible
                WHERE source_rank = 1
                ORDER BY trade_date DESC
                LIMIT 80
                """,
                (symbol.upper(), latest_trade_date, cutoff),
            ).fetchall()
        if not rows:
            raise ResearchToolError("找不到截止時間前的行情：%s" % symbol.upper())
        ordered = list(reversed(rows))
        closes = [_decimal(row["adjusted_close_price"] or row["close_price"]) for row in ordered]
        volumes = [int(row["volume_shares"]) for row in ordered]
        returns = [
            float(closes[index] / closes[index - 1] - Decimal("1"))
            for index in range(1, len(closes))
            if closes[index - 1] != 0
        ]
        latest = ordered[-1]
        latest_close = closes[-1]
        one_day = returns[-1] if returns else None
        ten_day = (
            float(latest_close / closes[-11] - Decimal("1"))
            if len(closes) >= 11 and closes[-11] != 0
            else None
        )
        high = _decimal(latest["high_price"] or latest["close_price"])
        low = _decimal(latest["low_price"] or latest["close_price"])
        close_location = (
            float((latest_close - low) / (high - low)) if high > low else None
        )
        prior_volumes = volumes[-21:-1]
        median_volume = statistics.median(prior_volumes) if prior_volumes else None
        volume_ratio = (
            volumes[-1] / median_volume if median_volume and median_volume > 0 else None
        )
        downside = [value for value in returns[-20:] if value < 0]
        downside_volatility = (
            statistics.pstdev(downside) if len(downside) >= 2 else None
        )
        true_ranges: List[float] = []
        for index in range(max(1, len(ordered) - 14), len(ordered)):
            row = ordered[index]
            row_high = _decimal(row["high_price"] or row["close_price"])
            row_low = _decimal(row["low_price"] or row["close_price"])
            previous = closes[index - 1]
            true_ranges.append(
                float(max(row_high - row_low, abs(row_high - previous), abs(row_low - previous)))
            )
        atr_14 = statistics.mean(true_ranges) if true_ranges else None
        result = {
            "symbol": symbol.upper(),
            "as_of": str(latest["trade_date"]),
            "source": str(latest["source"]),
            "observations": len(ordered),
            "analysis_close_price": str(latest_close),
            "return_1d": _round_optional(one_day),
            "return_10d": _round_optional(ten_day),
            "close_location": _round_optional(close_location),
            "volume_ratio_20d_median": _round_optional(volume_ratio),
            "atr_14": _round_optional(atr_14),
            "atr_14_pct": _round_optional(
                atr_14 / float(latest_close) if atr_14 is not None and latest_close else None
            ),
            "downside_volatility_20d": _round_optional(downside_volatility),
            "decision_cutoff": cutoff,
        }
        if event_published_at:
            result.update(
                self._event_relative_features(ordered, closes, volumes, event_published_at)
            )
        return result

    @staticmethod
    def _event_relative_features(
        rows: Sequence[Mapping[str, object]],
        closes: Sequence[Decimal],
        volumes: Sequence[int],
        event_published_at: str,
    ) -> Dict[str, object]:
        published = _parse_time(event_published_at, "event_published_at").astimezone(
            ZoneInfo("Asia/Taipei")
        )
        same_day_eligible = published.hour < 13 or (
            published.hour == 13 and published.minute < 30
        )
        event_index: Optional[int] = None
        for index, row in enumerate(rows):
            trade_date = str(row["trade_date"])
            if trade_date > published.date().isoformat() or (
                same_day_eligible and trade_date == published.date().isoformat()
            ):
                event_index = index
                break
        if event_index is None:
            return {
                "event_trade_date": None,
                "pre_event_return_5d": None,
                "event_day_return": None,
                "post_event_return_to_cutoff": None,
                "event_volume_ratio_20d_median": None,
            }
        previous_index = event_index - 1
        pre_index = event_index - 6
        pre_event_return = (
            float(closes[previous_index] / closes[pre_index] - Decimal("1"))
            if previous_index >= 0 and pre_index >= 0 and closes[pre_index] != 0
            else None
        )
        event_day_return = (
            float(closes[event_index] / closes[previous_index] - Decimal("1"))
            if previous_index >= 0 and closes[previous_index] != 0
            else None
        )
        post_event_return = (
            float(closes[-1] / closes[previous_index] - Decimal("1"))
            if previous_index >= 0 and closes[previous_index] != 0
            else None
        )
        prior_volumes = list(volumes[max(0, event_index - 20):event_index])
        median_volume = statistics.median(prior_volumes) if prior_volumes else None
        event_volume_ratio = (
            volumes[event_index] / median_volume
            if median_volume and median_volume > 0
            else None
        )
        return {
            "event_trade_date": str(rows[event_index]["trade_date"]),
            "pre_event_return_5d": _round_optional(pre_event_return),
            "event_day_return": _round_optional(event_day_return),
            "post_event_return_to_cutoff": _round_optional(post_event_return),
            "event_volume_ratio_20d_median": _round_optional(event_volume_ratio),
        }


def _round_optional(value: Optional[float]) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return round(value, 8)
