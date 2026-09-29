"""回測重放報告：淨值曲線、比賽規則檢查、基準比較與限制揭露。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

from etf_agent.virtual_account.daily import OfficialCloseMarket, average_price

from .contracts import AVAILABILITY_BASIS, EVIDENCE_STATUS, ReplayRequest

LIMITATIONS = [
    "行情可得時間是重建值（交易日 18:00 台北時間），不是當時的即時抓取紀錄（basis=%s）。" % AVAILABILITY_BASIS,
    "8 月沒有版本化的歷史處置／暫停等交易狀態，全部交易池視為可交易（backtest_assumed）。",
    "歷史重大訊息、月營收與財報申報時間無法證明早於各決策日，未進入 Snapshot；事件與基本面分析在此期間只有價量資料。",
    "以目前 150 檔名單重播過去，存在存活者／交易池選擇偏差；區間結束日前尚未有官方成交資料的股票（見 reconstruction.excluded_symbols_not_yet_trading）已自回測交易池移除。",
    "Yahoo 還原價含事後資訊，技術指標只應視為近似；官方未還原價用於成交與估值。",
    "子 Agent 使用的模型可能已知回測期間的市場事件，無法用程式排除。",
    "單次執行、單一市況；LLM 每次輸出不同，結果不代表策略有效。",
    "情緒／分析師共識無歷史核准來源，維持 unavailable。",
]


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def nav_series(states: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
    """依估值日整理淨值：成交後收盤狀態優先，其餘日期取決策前快照的前收盤估值。"""
    by_date: Dict[str, Mapping[str, object]] = {}
    for state in states:
        if state["type"] == "close":
            by_date[str(state["trade_date"])] = state
    for state in states:
        if state["type"] == "prepared" and str(state["trade_date"]) not in by_date:
            by_date[str(state["trade_date"])] = state
    genesis = [item for item in states if item["type"] == "genesis"]
    series = [{"date": key, "nav": str(_decimal(value["nav"])), "cash_weight": value["cash_weight"],
               "position_count": value["position_count"], "source": value["type"]}
              for key, value in sorted(by_date.items())]
    if genesis and not by_date:
        series.insert(0, {"date": str(genesis[0]["as_of"])[:10], "nav": str(_decimal(genesis[0]["nav"])),
                          "cash_weight": genesis[0]["cash_weight"], "position_count": 0, "source": "genesis"})
    return series


def max_drawdown(series: Sequence[Mapping[str, object]]) -> Decimal:
    peak = Decimal("0")
    worst = Decimal("0")
    for point in series:
        nav = _decimal(point["nav"])
        peak = max(peak, nav)
        if peak > 0:
            worst = min(worst, nav / peak - Decimal("1"))
    return worst


def equal_weight_benchmark(database: Path, first_day: str, last_day: str) -> Dict[str, object]:
    """全交易池等權：首日以官方均價買進、末日以收盤估值持有，不計費稅與整張。"""
    market = OfficialCloseMarket(database)
    first, last = market.quotes(first_day), market.quotes(last_day)
    returns = []
    for symbol, row in first.items():
        price = average_price(row["trade_value"], row["volume_shares"])
        if price is None or symbol not in last:
            continue
        returns.append(_decimal(last[symbol]["close_price"]) / price - Decimal("1"))
    if not returns:
        return {"status": "unavailable", "reason": "首日或末日缺官方行情"}
    return {
        "status": "computed",
        "definition": "全交易池等權買進持有（首日官方均價買進、末日收盤估值，未計費稅）",
        "symbols": len(returns),
        "return": str((sum(returns) / len(returns)).quantize(Decimal("0.000001"))),
    }


def build_report(
    request: ReplayRequest,
    days: Sequence,
    entries: Sequence[Mapping[str, object]],
    final_settle: Mapping[str, object],
    final_state: Mapping[str, object],
    states: Sequence[Mapping[str, object]],
    database: Path,
    manifest: Mapping[str, object],
    rules: Mapping[str, object],
) -> Dict[str, object]:
    series = nav_series(states)
    initial = _decimal(series[0]["nav"])
    final_nav = _decimal(final_state["nav"])
    cash_limit = _decimal(rules["cash_weight_must_be_below"])
    minimum, maximum = int(rules["min_positions"]), int(rules["max_positions"])
    breaches: List[Dict[str, object]] = []
    for state in states:
        if state["type"] != "close":
            continue
        if _decimal(state["cash_weight"]) >= cash_limit:
            breaches.append({"date": state["trade_date"], "rule": "cash_weight_after_fill",
                             "actual": state["cash_weight"], "must_be_below": str(cash_limit)})
        if not minimum <= int(state["position_count"]) <= maximum:
            breaches.append({"date": state["trade_date"], "rule": "position_count_after_fill",
                             "actual": state["position_count"], "allowed": [minimum, maximum]})
    for entry in entries:
        if entry.get("status") != "completed" or entry.get("decision_status") not in ("approved", "no_trade"):
            breaches.append({"date": entry["date"], "rule": "no_valid_decision",
                             "detail": entry.get("decision_status") or entry.get("errors")})
    benchmark = equal_weight_benchmark(database, days[0].isoformat(), days[-1].isoformat())
    return {
        "evidence_status": EVIDENCE_STATUS,
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "trading_days": [item.isoformat() for item in days],
        "initial_nav": str(initial),
        "final_nav": str(final_nav),
        "total_return": str((final_nav / initial - Decimal("1")).quantize(Decimal("0.000001"))),
        "max_drawdown": str(max_drawdown(series).quantize(Decimal("0.000001"))),
        "nav_series": series,
        "final_settle": dict(final_settle),
        "rule_breaches": breaches,
        "days": [dict(item) for item in entries],
        "agent_calls": sum(int(item.get("agent_calls") or 0) for item in entries),
        "benchmark": benchmark,
        "reconstruction": manifest.get("reconstruction"),
        "limitations": LIMITATIONS,
    }


def _pct(value: object) -> str:
    return "%.2f%%" % (float(value) * 100)


def render_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# 回測重放報告 %s ～ %s" % (report["start"], report["end"]),
        "",
        "> 證據狀態：`%s`。這是架構可行性驗證，不是策略有效性證明，也不是實盤或送件依據。" % report["evidence_status"],
        "",
    ]
    if report["rule_breaches"]:
        lines += ["## 違規與失敗日", ""]
        for item in report["rule_breaches"]:
            lines.append("- %s：`%s` %s" % (item["date"], item["rule"], {k: v for k, v in item.items() if k not in ("date", "rule")}))
        lines.append("")
    lines += [
        "## 績效", "",
        "| 項目 | 數值 |", "| --- | ---: |",
        "| 期初 NAV | %s |" % report["initial_nav"],
        "| 期末 NAV | %s |" % report["final_nav"],
        "| 區間報酬 | %s |" % _pct(report["total_return"]),
        "| 最大回撤 | %s |" % _pct(report["max_drawdown"]),
        "| Agent 呼叫次數 | %s |" % report["agent_calls"],
    ]
    benchmark = report["benchmark"]
    if benchmark.get("status") == "computed":
        lines.append("| 基準：%s | %s |" % (benchmark["definition"], _pct(benchmark["return"])))
    else:
        lines.append("| 基準 | 無法計算：%s |" % benchmark.get("reason"))
    lines += ["", "0050、加權指數尚未接入，未列入比較。", "", "## 每日淨值", "",
              "| 估值日 | NAV | 現金比例 | 持股檔數 | 來源 |", "| --- | ---: | ---: | ---: | --- |"]
    for point in report["nav_series"]:
        lines.append("| %s | %s | %s | %s | %s |" % (point["date"], point["nav"], _pct(point["cash_weight"]), point["position_count"], point["source"]))
    lines += ["", "## 每日決策", "", "| 決策日 | 狀態 | 決策 | 委託 | 現金姿態 | Agent 呼叫 |", "| --- | --- | --- | ---: | --- | ---: |"]
    for item in report["days"]:
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            item["date"], item.get("status"), item.get("decision_status") or "-", item.get("order_count", "-"),
            item.get("cash_stance") or "-", item.get("agent_calls", "-")))
    lines += ["", "## 限制（必須一併閱讀）", ""] + ["- %s" % text for text in report["limitations"]] + [""]
    return "\n".join(lines)
