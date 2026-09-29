"""回測重放的常數、錯誤型別與請求解析。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import List

TAIPEI = timezone(timedelta(hours=8))
CUTOFF_TIME = time(8, 55)
# 官方日成交資訊在收盤後才可得；回測保守假設收盤當日 18:00 起可用。
PRICE_AVAILABLE_TIME = time(18, 0)
SESSION_OPEN = time(9, 0)
SESSION_CLOSE = time(13, 30)
AVAILABILITY_BASIS = "reconstructed_trade_date_1800_taipei"
ASSUMED_STATUS_SOURCE_ID = "BACKTEST_ASSUMED_NO_HISTORICAL_STATUS"
EVIDENCE_STATUS = "insufficient"


class ReplayError(ValueError):
    """回測重放的輸入、隔離或時間點條件不安全。"""


class ReplayInterrupted(ReplayError):
    """Agent 執行環境失敗（例如用量上限）；不是決策品質問題，必須停止並等待續跑。"""


# 這些訊息代表子 Agent 根本沒有跑成，而不是輸出被 Validator 拒絕。
INFRASTRUCTURE_MARKERS = ("Agent 未產生結構化輸出", "Agent 執行失敗", "Agent 輸出不是 JSON")


@dataclass(frozen=True)
class ReplayRequest:
    start: date
    end: date

    @classmethod
    def parse(cls, start: str, end: str) -> "ReplayRequest":
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError as error:
            raise ReplayError("日期必須是 YYYY-MM-DD") from error
        if first > last:
            raise ReplayError("start 不得晚於 end")
        return cls(first, last)


def cutoff_for(day: date) -> str:
    """決策日 08:55（台北）；此時只能看到前一個交易日以前的收盤資料。"""
    return datetime.combine(day, CUTOFF_TIME, TAIPEI).isoformat()


def available_at_for(trade_date: str) -> str:
    """重建的行情可得時間（UTC ISO），與資料庫 fetched_at 格式一致。"""
    moment = datetime.combine(date.fromisoformat(trade_date), PRICE_AVAILABLE_TIME, TAIPEI)
    return moment.astimezone(timezone.utc).isoformat()


def trading_days(calendar, request: ReplayRequest) -> List[date]:
    days: List[date] = []
    current = request.start
    while current <= request.end:
        if calendar.is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days
