"""風險 Agent 現金姿態的「轉向事實」（cash_stance_turn_facts v1 版本閘門）。

這是紀錄工具，不是規則：程式只依 bundle 的 ``price_series`` 重算一組交易池層級的數字（全部 Decimal 字串、
沒有任何文字判斷），放進風險 Agent 簡報；Agent 多填一個列舉 ``turn_assessment`` 記錄它怎麼看。
列舉與姿態之間不設對應、沒有任何訊號會決定現金下限或姿態，也不改 ``MomentumEngine._regime``
（MA20／MA60 之上比例與 regime 直接呼叫它，維持同一公式）。資料不足記入 ``data_gaps``，不猜。
"""

from __future__ import annotations

from bisect import bisect_right
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Mapping, Optional

from .fields import DecisionToolError, reject_unknown_fields
from .momentum import MIN_OBSERVATIONS, MomentumEngine

CASH_STANCE_TURN_FACTS = "v1"
CASH_STANCE_TURN_FACTS_FIELDS = {"version"}
TURN_ASSESSMENTS = ("no_turn_evidence", "turn_unconfirmed", "turn_confirmed")
STREAK_WINDOW = 20
B20_LOOKBACK_DAYS = 5
NEW_LOW_WINDOW = 20
TURN_FACTS_NOTE = (
    "這些是交易池的事實描述，不是程式判定的訊號，也不決定現金姿態。短期反彈可能是假反彈，不保證趨勢反轉；"
    "regime 是均線廣度的累積結果，可能落後短期價格變化。僅依本次截止前可得的證據判斷，"
    "不要補入未提供的歷史案例或後續報酬。turn_assessment 只是紀錄你怎麼看，不要求與姿態一致。"
)
_FOUR = Decimal("0.0001")
_SIX = Decimal("0.000001")


def cash_stance_turn_facts_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """cash_stance_turn_facts 啟用時回傳閘門物件；沒有閘門回 None（欄位白名單與舊版完全相同）。"""
    gate = sizing.get("cash_stance_turn_facts") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def validate_cash_stance_turn_facts_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """閘門形狀：{version: v1}；前提（typed_evidence_v1）在重建現金姿態時檢查。"""
    gate = sizing["cash_stance_turn_facts"]
    prefix = "position_sizing.cash_stance_turn_facts"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, CASH_STANCE_TURN_FACTS_FIELDS, prefix, errors)
    if gate.get("version") != CASH_STANCE_TURN_FACTS:
        errors.append("%s.version 只允許 %s" % (prefix, CASH_STANCE_TURN_FACTS))


def _q4(value: Decimal) -> str:
    return str(value.quantize(_FOUR, rounding=ROUND_HALF_UP))


def _ratio(count: int, total: int) -> Optional[str]:
    return _q4(Decimal(count) / Decimal(total)) if total else None


def _breadth_text(value: object) -> str:
    return _q4(Decimal(str(value)))


class _Pool:
    """交易池逐檔收盤序列；所有逐日計算都以「截至某個交易日」的序列為準。"""

    def __init__(self, bundle: Mapping[str, object], trade_date: str):
        self.dates_by_symbol: Dict[str, List[str]] = {}
        self.closes_by_symbol: Dict[str, List[Decimal]] = {}
        for row in bundle["price_series"]:  # type: ignore[union-attr]
            symbol = str(row["symbol"]).upper()
            bars = sorted(row["bars"], key=lambda bar: str(bar["trade_date"]))
            for bar in bars:
                if str(bar["trade_date"]) > trade_date:
                    raise DecisionToolError(
                        "turn_facts：%s 的行情 %s 晚於 snapshot.latest_trade_date %s" % (symbol, bar["trade_date"], trade_date)
                    )
            self.dates_by_symbol[symbol] = [str(bar["trade_date"]) for bar in bars]
            self.closes_by_symbol[symbol] = [Decimal(str(bar["close"])) for bar in bars]
        self.days = sorted({date for dates in self.dates_by_symbol.values() for date in dates})
        self.size = len(self.dates_by_symbol)

    def history(self, symbol: str, day: str) -> List[Decimal]:
        return self.closes_by_symbol[symbol][: bisect_right(self.dates_by_symbol[symbol], day)]

    def pool_return(self, index: int) -> Optional[Decimal]:
        """池等權日報酬：當日與前一個交易日都有收盤的股票，簡單平均。"""
        if index < 1:
            return None
        today, before = self.days[index], self.days[index - 1]
        returns: List[Decimal] = []
        for symbol, dates in self.dates_by_symbol.items():
            closes = self.closes_by_symbol[symbol]
            position = bisect_right(dates, today)
            if position < 2 or dates[position - 1] != today or dates[position - 2] != before or closes[position - 2] <= 0:
                continue
            returns.append(closes[position - 1] / closes[position - 2] - Decimal(1))
        return sum(returns, Decimal(0)) / Decimal(len(returns)) if returns else None

    def regime(self, day: str) -> Dict[str, object]:
        """與 MomentumEngine 同一公式：有 61 筆以上者才算可用，breadth 與 regime 交給 ``MomentumEngine._regime``。"""
        available: List[Dict[str, object]] = []
        for symbol in self.dates_by_symbol:
            closes = self.history(symbol, day)
            if len(closes) < MIN_OBSERVATIONS:
                continue
            latest = closes[-1]
            available.append({
                "above_ma20": latest >= sum(closes[-20:]) / Decimal("20"),
                "above_ma60": latest >= sum(closes[-60:]) / Decimal("60"),
            })
        coverage = len(available) / self.size if self.size else 0.0
        return MomentumEngine._regime(available, coverage)


def turn_facts(bundle: Mapping[str, object]) -> Dict[str, object]:
    """依 bundle 重算交易池層級的轉向事實；資料不足記入 ``data_gaps``，不猜。任何 bar 晚於最近交易日即拒絕。"""
    snapshot = bundle.get("snapshot")
    trade_date = snapshot.get("latest_trade_date") if isinstance(snapshot, Mapping) else None
    if not isinstance(trade_date, str):
        raise DecisionToolError("turn_facts 需要 snapshot.latest_trade_date")
    if not isinstance(bundle.get("price_series"), list):
        raise DecisionToolError("turn_facts 需要 price_series")
    pool = _Pool(bundle, trade_date)
    gaps: List[str] = []
    result: Dict[str, object] = {
        "version": CASH_STANCE_TURN_FACTS,
        "as_of": trade_date,
        "pool_size": pool.size,
    }
    if not pool.days:
        gaps.append("price_series 沒有任何行情，所有轉向事實都無法計算")
    elif pool.days[-1] != trade_date:
        gaps.append("行情最後一個交易日 %s 不是 snapshot.latest_trade_date %s" % (pool.days[-1], trade_date))
    last = len(pool.days) - 1

    def day_return(index: int, label: str) -> Optional[str]:
        value = pool.pool_return(index) if 0 <= index <= last else None
        if value is None:
            gaps.append("%s 無法計算（交易日數或可比較的收盤不足）" % label)
            return None
        return str(value.quantize(_SIX, rounding=ROUND_HALF_UP))

    result["pool_return_latest"] = day_return(last, "pool_return_latest")
    result["pool_return_prior"] = day_return(last - 1, "pool_return_prior")

    streak = 0
    for index in range(last, max(last - STREAK_WINDOW, 0), -1):
        value = pool.pool_return(index)
        if value is None or value <= 0:
            break
        streak += 1
    result["up_streak_days"] = streak if last >= 1 else None
    result["up_streak_window_days"] = STREAK_WINDOW

    def share(window: int, test) -> Optional[str]:
        count = total = 0
        for symbol in pool.dates_by_symbol:
            closes = pool.history(symbol, pool.days[-1]) if pool.days else []
            if len(closes) < window:
                continue
            total += 1
            count += 1 if test(closes) else 0
        return _ratio(count, total)

    result["share_above_ma5"] = share(5, lambda c: c[-1] >= sum(c[-5:]) / Decimal(5))
    result["share_above_ma20"] = share(20, lambda c: c[-1] >= sum(c[-20:]) / Decimal(20))
    result["new_low_20d_share"] = share(NEW_LOW_WINDOW, lambda c: c[-1] <= min(c[-NEW_LOW_WINDOW:]))
    for name in ("share_above_ma5", "share_above_ma20", "new_low_20d_share"):
        if result[name] is None:
            gaps.append("%s 沒有足夠筆數的股票" % name)

    now = pool.regime(pool.days[-1]) if pool.days else None
    if now is None or now["status"] != "available":
        gaps.append("regime 無法計算（可用歷史行情不足 %d 筆或覆蓋不足）" % MIN_OBSERVATIONS)
        result.update(regime=None, breadth_above_ma20=None, breadth_above_ma60=None)
    else:
        result.update(
            regime=now["regime"],
            breadth_above_ma20=_breadth_text(now["breadth_above_ma20"]),
            breadth_above_ma60=_breadth_text(now["breadth_above_ma60"]),
        )
    before = pool.regime(pool.days[last - B20_LOOKBACK_DAYS]) if last >= B20_LOOKBACK_DAYS else None
    if before is None or before["status"] != "available":
        gaps.append("%d 個交易日前的 breadth 無法計算（歷史不足 %d 筆）" % (B20_LOOKBACK_DAYS, MIN_OBSERVATIONS))
        result.update(breadth_above_ma20_5d_ago=None, b20_change_5d=None)
    else:
        result["breadth_above_ma20_5d_ago"] = _breadth_text(before["breadth_above_ma20"])
        if now is not None and now["status"] == "available":
            change = Decimal(str(now["breadth_above_ma20"])) - Decimal(str(before["breadth_above_ma20"]))
            result["b20_change_5d"] = _q4(change)
        else:
            result["b20_change_5d"] = None

    bear_days: Optional[int] = None
    truncated = False
    if now is not None and now["status"] == "available":
        bear_days = 0
        for index in range(last, -1, -1):
            state = now if index == last else pool.regime(pool.days[index])
            if state["status"] != "available":
                truncated = bear_days > 0
                if truncated:
                    gaps.append("regime 連續 bear 天數被歷史長度截斷（更早的交易日不足 %d 筆），實際天數不少於此值" % MIN_OBSERVATIONS)
                break
            if state["regime"] != "bear":
                break
            bear_days += 1
    result["bear_streak_days"] = bear_days
    result["bear_streak_truncated"] = truncated
    result["data_gaps"] = gaps
    result["note"] = TURN_FACTS_NOTE
    return result


def validate_turn_assessment(stance: Mapping[str, object], prefix: str, errors: List[str]) -> None:
    """列舉必填且在值域內。不驗與姿態的對應，也不驗文字品質（現金姿態不由訊號決定）。"""
    value = stance.get("turn_assessment")
    if not isinstance(value, str) or value not in TURN_ASSESSMENTS:
        errors.append("%s.turn_assessment 必須是 %s" % (prefix, "／".join(TURN_ASSESSMENTS)))
