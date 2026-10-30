"""買進論點追蹤（確定性）：每檔持股，自當初買進（buy／add）以來有沒有照理由在走。

關心的不是命中率，而是「這檔股票是不是照當初買的理由在走；短期波動可以接受」。程式只算**事實**：
買進日與預估成交價、至今報酬、期間最大回落、之後交易 Agent 的意圖與持股檢視警示；再依容忍度（預設取
policy 的 ``position_review.loss_alert``）分成三種狀態：

- ``on_track``：至今報酬不為負。
- ``within_tolerance``：目前虧損但未達容忍度、也沒觸發虧損警示——短期波動，可接受。
- ``needs_review``：虧損達容忍度或曾觸發虧損警示——請人（或交易 Agent）回頭檢查當初理由是否還成立。

「理由本身是否還成立」是判斷，不由程式下結論：原買進理由與重估條件（交易 Agent 當時寫的文字）只放在
週報給人閱讀（``text``），**不進**診斷包與 Agent 簡報，反思 Agent 因此不會被這些文字影響。
窗口內有除權息時價格不連續，只列事實、不算報酬（``ex_rights``）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.calibration.prices import OfficialPriceBook
from etf_agent.calibration.tools import format_decimal

DEFAULT_TOLERANCE = Decimal("0.10")
BUY_INTENTS = {"buy", "add"}
LOSS_FLAG = "loss_alert"


def _entry_for(symbol: str, ordered: Sequence[tuple]) -> Optional[tuple]:
    """最近一次「有委託買進」的 buy／add：回傳 (成交日, run, intent 項目, 委託)。"""
    for entry_day, run in reversed(ordered):
        item = next((i for i in run["intent"]["items"] if str(i["symbol"]).upper() == symbol and i.get("intent") in BUY_INTENTS), None)
        order = next((o for o in run["decision"].get("orders", []) if str(o["symbol"]).upper() == symbol and o.get("side") == "buy"), None)
        if item is not None and order is not None:
            return entry_day, run, item, order
    return None


def _later(symbol: str, entry_day: str, ordered: Sequence[tuple]) -> Dict[str, object]:
    intents: Dict[str, int] = {}
    flags: Dict[str, int] = {}
    for day, run in ordered:
        if day <= entry_day:
            continue
        item = next((i for i in run["intent"]["items"] if str(i["symbol"]).upper() == symbol), None)
        if item is not None:
            intents[str(item["intent"])] = intents.get(str(item["intent"]), 0) + 1
        review = run["team_inputs"].get("position_review")
        if isinstance(review, Mapping):
            for row in review.get("items", []):
                if str(row["symbol"]).upper() == symbol:
                    for flag in row.get("flags", []):
                        flags[str(flag["flag"])] = flags.get(str(flag["flag"]), 0) + 1
    return {"intents_since": dict(sorted(intents.items())), "alerts_since": dict(sorted(flags.items()))}


def _path(symbol: str, entry_day: str, book: OfficialPriceBook) -> List[Decimal]:
    prices: List[Decimal] = []
    for day in book.trading_days(entry_day):
        table = book.vwap(day) or {}
        price = table.get(symbol)
        if price is not None:
            prices.append(price)
    return prices


def build_thesis_tracking(
    chosen: Mapping[str, Mapping[str, object]], book: OfficialPriceBook, tolerance: Decimal = DEFAULT_TOLERANCE,
) -> List[Dict[str, object]]:
    """每檔「目前持有」的股票一筆。``chosen`` 是成交日 → 當日採用的 Decision run（同 ``_latest_per_entry_day``）。

    沒有找到買進紀錄的持股（例如開帳就持有）不列入，因為沒有「當初的理由」可對照。"""
    ordered = sorted(chosen.items())
    if not ordered:
        return []
    # rejected 的決策沒有 portfolio；持股以最近一個有 portfolio 的決策日為準。
    portfolios = [run["decision"].get("portfolio") for _, run in ordered if isinstance(run["decision"].get("portfolio"), Mapping)]
    if not portfolios:
        return []
    held = {str(p["symbol"]).upper() for p in portfolios[-1].get("positions", [])}
    rows: List[Dict[str, object]] = []
    for symbol in sorted(held):
        found = _entry_for(symbol, ordered)
        if found is None:
            continue
        entry_day, _, item, order = found
        entry_price = Decimal(str(order["estimated_price"]))
        later = _later(symbol, entry_day, ordered)
        row: Dict[str, object] = {
            "symbol": symbol, "entry_day": entry_day, "entry_intent": item["intent"], "entry_conviction": item.get("conviction"),
            "entry_price": format_decimal(entry_price), **later,
            "text": {"rationale": item.get("rationale"), "invalidation_conditions": item.get("invalidation_conditions", []),
                     "unresolved_questions": item.get("unresolved_questions", [])},
        }
        prices = _path(symbol, entry_day, book)
        last_day = ordered[-1][0]
        if book.ex_rights_symbols(entry_day, book.trading_days(entry_day)[-1] if book.trading_days(entry_day) else last_day) & {symbol}:
            row.update(status="ex_rights", note="買進後有除權息，價格不連續，不計算報酬")
        elif not prices or entry_price <= 0:
            row.update(status="unavailable", note="買進日之後尚無官方成交均價")
        else:
            latest = prices[-1]
            peak, worst = entry_price, Decimal(0)
            for price in prices:
                peak = max(peak, price)
                worst = min(worst, price / peak - 1)
            ret = latest / entry_price - 1
            alerted = LOSS_FLAG in later["alerts_since"]  # type: ignore[operator]
            state = "needs_review" if (alerted or ret <= -tolerance) else ("on_track" if ret >= 0 else "within_tolerance")
            row.update(status=state, days_held=len(prices), return_since_entry=format_decimal(ret),
                       max_drawdown_since_entry=format_decimal(worst), tolerance=format_decimal(tolerance))
        rows.append(row)
    return rows


def numeric_view(rows: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """診斷包用的版本：去掉所有文字欄位，只留數值與列舉（反思 Agent 讀得到的部分）。"""
    counts: Dict[str, int] = {}
    for row in rows:
        counts[str(row["status"])] = counts.get(str(row["status"]), 0) + 1
    if not rows:
        return {"status": "unavailable", "reason": "目前沒有可對照買進理由的持股"}
    return {"status": "ok", "holdings": len(rows), "by_state": dict(sorted(counts.items())),
            "items": [{k: v for k, v in row.items() if k != "text"} for row in rows]}
