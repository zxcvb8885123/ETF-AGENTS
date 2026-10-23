"""考卷的答案：決策之後真正的官方成交均價。答案隨時間自己長出來，價格沒到齊前標 pending。

沿用 ``calibration`` 的口徑：成交日 T 是 cutoff 後第一個有官方收盤的交易日，報酬是 T+h 日均價
÷ T 日均價 − 1，超額報酬減去同日交易池平均；窗口內除權息的股票不計；完整日少於 20 天只給天數
與筆數，不給命中率或平均。單日結果另列，標明不具統計意義。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.calibration.contracts import HORIZONS, MIN_DAY_COVERAGE, MIN_DECISION_DAYS
from etf_agent.calibration.prices import OfficialPriceBook
from etf_agent.calibration.tools import format_decimal, forward_return, mean


BULLISH_INTENTS = {"buy", "add"}
BEARISH_INTENTS = {"trim", "exit", "forced_exit"}


def answer_key(book: OfficialPriceBook, decision_cutoff: str, symbols: Sequence[str]) -> Dict[str, object]:
    """每個 horizon 的逐檔超額報酬；價格沒到齊、互相衝突或覆蓋太低時標出原因。"""
    entry_day = book.execution_date(decision_cutoff)
    if entry_day is None:
        return {"entry_day": None, "horizons": {str(h): {"status": "entry_price_pending"} for h in HORIZONS}}
    days = book.trading_days(entry_day)
    horizons: Dict[str, object] = {}
    for horizon in HORIZONS:
        if not days or days[0] != entry_day:
            horizons[str(horizon)] = {"status": "entry_price_pending"}
            continue
        if len(days) <= horizon:
            horizons[str(horizon)] = {"status": "exit_price_pending"}
            continue
        exit_day = days[horizon]
        entry, exit_ = book.vwap(entry_day), book.vwap(exit_day)
        if entry is None or exit_ is None:
            horizons[str(horizon)] = {"status": "price_conflict"}
            continue
        skipped = book.ex_rights_symbols(entry_day, exit_day)
        returns = {symbol: forward_return(entry[symbol], exit_[symbol]) for symbol in symbols
                   if symbol not in skipped and entry.get(symbol) is not None and exit_.get(symbol) is not None}
        if not symbols or Decimal(len(returns)) / Decimal(len(symbols)) < MIN_DAY_COVERAGE:
            horizons[str(horizon)] = {"status": "coverage_too_low", "priced": len(returns)}
            continue
        pool = mean(list(returns.values()))
        horizons[str(horizon)] = {"status": "complete", "exit_day": exit_day,
                                  "excess": {symbol: value - pool for symbol, value in returns.items()}}
    return {"entry_day": entry_day, "horizons": horizons}


def _calls(outputs: Mapping[str, object]) -> Dict[str, Dict[str, str]]:
    """把結論轉成「看好／看壞」：分析師 positive／negative；交易 buy、add／trim、exit；委託買／賣。"""
    calls: Dict[str, Dict[str, str]] = {}
    for name in ("technical", "fundamental", "event"):
        if outputs.get(name) is not None:
            calls[name] = {symbol: "up" if view == "positive" else "down"
                           for symbol, view in outputs[name].items() if view in {"positive", "negative"}}
    if outputs.get("intent") is not None:
        calls["intent"] = {}
        for symbol, value in outputs["intent"].items():
            intent = value.split("/", 1)[0]
            if intent in BULLISH_INTENTS:
                calls["intent"][symbol] = "up"
            elif intent in BEARISH_INTENTS:
                calls["intent"][symbol] = "down"
    if outputs.get("orders") is not None:
        calls["orders"] = {symbol: "up" if side == "buy" else "down" for symbol, side in outputs["orders"].items()}
    return calls


def score_day(outputs: Mapping[str, object], excess: Mapping[str, Decimal]) -> Dict[str, Dict[str, object]]:
    """單日：看好的有沒有贏交易池平均、看壞的有沒有輸；只算有答案的股票。"""
    result = {}
    for name, calls in _calls(outputs).items():
        hits = [1 if (excess[s] > 0) == (call == "up") else 0 for s, call in calls.items() if s in excess and excess[s] != 0]
        up = [excess[s] for s, call in calls.items() if call == "up" and s in excess]
        down = [excess[s] for s, call in calls.items() if call == "down" and s in excess]
        result[name] = {"n": len(hits), "hits": sum(hits), "up_excess": [str(v) for v in up], "down_excess": [str(v) for v in down]}
    return result


def pool_scores(days: Sequence[tuple]) -> Dict[str, object]:
    """把多個完整決策日合起來；``days`` 是 (考卷, 單日分數)。同一天重跑多次只算一天，完整日少於 20 天只回報天數與筆數。"""
    result: Dict[str, object] = {}
    names = sorted({name for _, day in days for name in day})
    for name in names:
        rows = [day[name] for _, day in days if name in day]
        distinct = len({case_id for case_id, day in days if name in day})
        n, hits = sum(row["n"] for row in rows), sum(row["hits"] for row in rows)
        entry: Dict[str, object] = {"complete_days": distinct, "n": n}
        if distinct < MIN_DECISION_DAYS:
            entry["status"] = "insufficient_sample"
        else:
            up = [Decimal(v) for row in rows for v in row["up_excess"]]
            down = [Decimal(v) for row in rows for v in row["down_excess"]]
            entry.update(status="ok", hit_rate=round(hits / n, 4) if n else None,
                         up_mean_excess=format_decimal(mean(up)), down_mean_excess=format_decimal(mean(down)))
        result[name] = entry
    return result


def score_runs(database: Path, as_of: str, cases: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """``cases``：每份考卷的 cutoff、交易池與各次結論（``reference`` 與 ``reruns``）。回傳逐卷與合計分數。"""
    book = OfficialPriceBook(Path(database), as_of)
    per_case, pooled = [], {str(h): {"reference": [], "reruns": []} for h in HORIZONS}
    for case in cases:
        key = answer_key(book, str(case["decision_cutoff"]), case["symbols"])
        horizons = {}
        for horizon, answer in key["horizons"].items():
            if answer["status"] != "complete":
                horizons[horizon] = {"status": answer["status"]}
                continue
            reference = score_day(case["reference"], answer["excess"])
            reruns = [score_day(outputs, answer["excess"]) for outputs in case["reruns"]]
            pooled[horizon]["reference"].append((case["case_id"], reference))
            pooled[horizon]["reruns"].extend((case["case_id"], item) for item in reruns)
            horizons[horizon] = {"status": "complete", "exit_day": answer["exit_day"],
                                 "reference": _single_day(reference), "reruns": [_single_day(item) for item in reruns]}
        per_case.append({"case_id": case["case_id"], "entry_day": key["entry_day"], "horizons": horizons})
    return {
        "as_of": book.as_of, "price_basis": "official_vwap", "required_decision_days": MIN_DECISION_DAYS,
        "cases": per_case,
        "overall": {h: {"reference": pool_scores(groups["reference"]), "reruns": pool_scores(groups["reruns"])}
                    for h, groups in pooled.items()},
    }


def _single_day(scores: Mapping[str, Mapping[str, object]]) -> Dict[str, Optional[object]]:
    """單日命中率，只供參考（不具統計意義）。"""
    return {name: {"n": row["n"], "hits": row["hits"]} for name, row in scores.items()}
