"""風險 Agent 現金姿態的「大盤指數事實」（market_index_facts v1 版本閘門）。

紀錄工具，不是規則：程式只依決策輸入裡 ``market_fear_greed_input`` 已封存的證交所加權指數（TWSE_TAIEX）原始回應，
重算最新收盤、當日漲跌、近 5／20 日報酬、離 60 日高點的回檔與收盤相對 MA20／MA60；全部是 Decimal 字串，
Agent 不算數字。沒有任何訊號決定現金姿態、現金下限或配置，也不是證據（``citable=false``）。
晚間 run 看不到隔夜美股與台指期夜盤，但當天大盤收盤已可得，這份事實補上「今天大盤怎麼走」。
來源未核准、行情日期晚於最近交易日、歷史不足都記入 ``data_gaps``（或拒絕），不補值。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Mapping, Optional

from .fields import DecisionToolError, reject_unknown_fields

MARKET_INDEX_FACTS = "v1"
MARKET_INDEX_FACTS_FIELDS = {"version"}
SOURCE = "TWSE_TAIEX"
MARKET_INDEX_NOTE = (
    "這是加權指數的事實描述，不是程式判定的訊號，也不決定現金姿態，也沒有 evidence_id、不可引用。"
    "單日大漲或大跌不代表趨勢反轉，大跌後的反彈也可能失敗；請連同近 5／20 日報酬與離高點的距離一起看。"
    "僅依本次截止前可得的資料判斷，不要補入未提供的歷史案例或後續報酬。"
)
_TWO = Decimal("0.01")


def market_index_facts_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """market_index_facts 啟用時回傳閘門物件；沒有閘門回 None（簡報與舊版逐鍵相同）。"""
    gate = sizing.get("market_index_facts") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def validate_market_index_facts_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    gate = sizing["market_index_facts"]
    prefix = "position_sizing.market_index_facts"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, MARKET_INDEX_FACTS_FIELDS, prefix, errors)
    if gate.get("version") != MARKET_INDEX_FACTS:
        errors.append("%s.version 只允許 %s" % (prefix, MARKET_INDEX_FACTS))


def _q(value: Decimal, quantum: Decimal) -> str:
    return str(value.quantize(quantum, rounding=ROUND_HALF_UP))


def _series(bundle: Mapping[str, object], gaps: List[str]) -> Dict[str, Decimal]:
    """已核准、已抓到的加權指數收盤（日期 → 收盤）；沒有或未核准回空並記缺口。"""
    from etf_agent.perception.market_fear_greed.tools import parse_twse_index

    pair = bundle.get("market_fear_greed_input")
    pack = pair.get("bundle") if isinstance(pair, Mapping) else None
    if not isinstance(pack, Mapping):
        gaps.append("決策輸入沒有恐懼貪婪資料包，無法取得加權指數")
        return {}
    approved = any(
        isinstance(row, Mapping) and row.get("source") == SOURCE and row.get("license_status") == "approved"
        for row in pack.get("source_approvals", [])
    )
    if not approved:
        gaps.append("加權指數來源未核准，不計入")
        return {}
    series: Dict[str, Decimal] = {}
    for capture in pack.get("captures", []):
        if not isinstance(capture, Mapping) or capture.get("source") != SOURCE or capture.get("status") != "fetched":
            continue
        try:
            series.update(parse_twse_index(capture["raw_text"]))
        except Exception as error:  # 回應改版或毀損：整份不用，記缺口
            raise DecisionToolError("market_index_facts：加權指數原始回應無法解析（%s）" % error) from error
    if not series:
        gaps.append("沒有成功抓取的加權指數月檔")
    return series


def market_index_facts(bundle: Mapping[str, object]) -> Dict[str, object]:
    """依 bundle 重算加權指數事實；資料不足記入 ``data_gaps``，不猜。有任何收盤日晚於最近交易日即拒絕。"""
    snapshot = bundle.get("snapshot")
    trade_date = snapshot.get("latest_trade_date") if isinstance(snapshot, Mapping) else None
    if not isinstance(trade_date, str):
        raise DecisionToolError("market_index_facts 需要 snapshot.latest_trade_date")
    gaps: List[str] = []
    series = _series(bundle, gaps)
    later = sorted(day for day in series if day > trade_date)
    if later:
        raise DecisionToolError("market_index_facts：加權指數 %s 晚於 snapshot.latest_trade_date %s" % (later[0], trade_date))
    result: Dict[str, object] = {
        "version": MARKET_INDEX_FACTS, "citable": False, "source": SOURCE, "as_of": trade_date, "note": MARKET_INDEX_NOTE,
    }
    days = sorted(series)
    if not days:
        result["data_gaps"] = gaps
        return result
    if days[-1] != trade_date:
        gaps.append("加權指數最後一個交易日 %s 不是 snapshot.latest_trade_date %s" % (days[-1], trade_date))
    closes = [series[day] for day in days]
    last = closes[-1]
    result["latest_date"] = days[-1]
    result["close"] = _q(last, _TWO)
    if len(closes) >= 2 and closes[-2] > 0:
        result["day_change_points"] = _q(last - closes[-2], _TWO)
        result["day_change_pct"] = _q((last / closes[-2] - 1) * 100, _TWO)
    else:
        gaps.append("沒有前一個交易日，無法計算當日漲跌")
    for window in (5, 20):
        if len(closes) > window and closes[-1 - window] > 0:
            result["return_%dd_pct" % window] = _q((last / closes[-1 - window] - 1) * 100, _TWO)
        else:
            gaps.append("歷史不足，無法計算近 %d 日報酬" % window)
    if len(closes) >= 60:
        high = max(closes[-60:])
        result["drawdown_from_60d_high_pct"] = _q((last / high - 1) * 100, _TWO)
        result["above_ma60"] = last >= sum(closes[-60:]) / Decimal(60)
    else:
        gaps.append("歷史只有 %d 日，無法計算 60 日高點回檔與 MA60" % len(closes))
    if len(closes) >= 20:
        result["above_ma20"] = last >= sum(closes[-20:]) / Decimal(20)
    else:
        gaps.append("歷史只有 %d 日，無法計算 MA20" % len(closes))
    result["data_gaps"] = gaps
    return result
