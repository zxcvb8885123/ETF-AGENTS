"""由官方開休市日期表建立的交易日與交割日曆。

TWSE「開休市日期」政府開放資料只列出例外日期：國定假日為休市且不交割；
「市場無交易，僅辦理結算交割作業」為休市但仍交割；「開始交易」「最後交易」
只是提示，照常交易。未列出的週一至週五為交易日，週末一律不交易不交割。

日期表逐年公布，只有表內出現過的年度視為已涵蓋；查詢未涵蓋年度一律拒絕，
不退回週一至週五近似。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, FrozenSet, Iterable, Mapping

from etf_agent.core import canonical_sha256, parse_aware_time
from .evidence import TAIPEI_TIMEZONE


CALENDAR_BASIS = "twse_ogd_calendar"
SESSION_OPEN = time(9, 0)
SESSION_CLOSE = time(13, 30)
_SETTLEMENT_ONLY_MARK = "僅辦理結算交割"
_TRADING_MARKS = ("開始交易", "最後交易")
_SEARCH_LIMIT_DAYS = 40


class TradingCalendarError(ValueError):
    """日曆資料不足或查詢超出涵蓋範圍。"""


@dataclass(frozen=True)
class TradingCalendar:
    """休市日（不交易、不交割）與僅交割日（不交易、仍交割）；``covered_years`` 外的日期拒絕查詢。"""

    holidays: FrozenSet[str]
    settlement_only: FrozenSet[str]
    covered_years: FrozenSet[int]
    source_sha256: str

    @classmethod
    def from_ogd_facts(cls, facts: Iterable[Mapping[str, object]], source_sha256: str) -> "TradingCalendar":
        holidays = set()
        settlement_only = set()
        years = set()
        for fact in facts:
            if fact.get("kind") != "calendar_event":
                continue
            day = date.fromisoformat(str(fact["event_date"]))
            years.add(day.year)
            name = str(fact.get("name", ""))
            if any(mark in name for mark in _TRADING_MARKS):
                continue
            (settlement_only if _SETTLEMENT_ONLY_MARK in name else holidays).add(day.isoformat())
        if not years:
            raise TradingCalendarError("開休市日期表沒有任何日期，不能建立交易日曆")
        return cls(frozenset(holidays), frozenset(settlement_only), frozenset(years), source_sha256)

    def _require_covered(self, day: date) -> None:
        if day.year not in self.covered_years:
            raise TradingCalendarError(
                "開休市日曆未涵蓋 %d 年（已涵蓋：%s），不能判定 %s"
                % (day.year, "、".join(str(year) for year in sorted(self.covered_years)), day.isoformat())
            )

    def is_trading_day(self, day: date) -> bool:
        self._require_covered(day)
        iso = day.isoformat()
        return day.weekday() < 5 and iso not in self.holidays and iso not in self.settlement_only

    def is_settlement_day(self, day: date) -> bool:
        self._require_covered(day)
        return day.weekday() < 5 and day.isoformat() not in self.holidays

    def next_trading_day(self, day: date) -> date:
        """``day`` 之後（不含當日）第一個交易日。"""
        current = day
        for _ in range(_SEARCH_LIMIT_DAYS):
            current += timedelta(days=1)
            if self.is_trading_day(current):
                return current
        raise TradingCalendarError("%s 後 %d 天內找不到交易日，行事曆資料可能錯誤" % (day.isoformat(), _SEARCH_LIMIT_DAYS))

    def add_settlement_days(self, trade_date: date, count: int) -> date:
        """T+N 交割日：只計入可辦理交割的營業日（含僅交割日）。"""
        if count < 0:
            raise TradingCalendarError("交割天數不得為負")
        current = trade_date
        remaining = count
        for _ in range(_SEARCH_LIMIT_DAYS * max(count, 1)):
            if remaining == 0:
                return current
            current += timedelta(days=1)
            if self.is_settlement_day(current):
                remaining -= 1
        raise TradingCalendarError("%s 起找不到 T+%d 交割日，行事曆資料可能錯誤" % (trade_date.isoformat(), count))

    def next_trading_session(self, decision_cutoff: str) -> Dict[str, str]:
        """cutoff 後第一個交易日的 09:00–13:30（台北）；cutoff 在盤中或盤後都排到下一個交易日。"""
        cutoff = parse_aware_time(decision_cutoff, "decision_cutoff", error=TradingCalendarError).astimezone(TAIPEI_TIMEZONE)
        day = self.next_trading_day(cutoff.date())
        return {
            "start": datetime.combine(day, SESSION_OPEN, TAIPEI_TIMEZONE).isoformat(),
            "end": datetime.combine(day, SESSION_CLOSE, TAIPEI_TIMEZONE).isoformat(),
        }

    def provenance(self) -> Dict[str, object]:
        return {
            "basis": CALENDAR_BASIS,
            "source_sha256": self.source_sha256,
            "covered_years": sorted(self.covered_years),
            "calendar_sha256": canonical_sha256(
                {"holidays": sorted(self.holidays), "settlement_only": sorted(self.settlement_only),
                 "covered_years": sorted(self.covered_years)}
            ),
        }
