"""Rebuild and compare fixture strategies on identical market observations."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from etf_agent.core import canonical_sha256, content_sha256

from .contracts import BacktestToolError, HistoricalClock, decimal_value


REQUIRED_STRATEGIES = (
    "equal_weight_25", "momentum", "rule_event_momentum",
    "llm_fixed_event_momentum", "agent_tool_event_momentum",
    "agent_tool_event_momentum_defensive",
)


def _ratio(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001")))


def _metrics(run: Mapping[str, object]) -> dict:
    days = run["days"]
    navs = [decimal_value(day["ledger"]["nav"], "ledger.nav") for day in days]
    if any(nav <= 0 for nav in navs):
        raise BacktestToolError("比較所需的每日 NAV 必須大於零")
    peak = navs[0]
    drawdown = Decimal("0")
    for nav in navs:
        peak = max(peak, nav)
        drawdown = max(drawdown, (peak - nav) / peak)
    fills = [fill for day in days for fill in day["execution"]["fills"]]
    windows = [
        {"start_date": days[i]["trade_date"], "end_date": days[i + 24]["trade_date"],
         "return": _ratio(navs[i + 24] / navs[i] - 1)}
        for i in range(0, len(days) - 24, 24)
    ]
    return {
        "first_close_nav": str(navs[0]), "last_close_nav": str(navs[-1]),
        "first_close_to_last_close_return": _ratio(navs[-1] / navs[0] - 1),
        "max_drawdown_from_first_close": _ratio(drawdown),
        "total_commission": str(sum((decimal_value(fill["commission"], "commission") for fill in fills), Decimal("0"))),
        "total_tax": str(sum((decimal_value(fill["tax"], "tax") for fill in fills), Decimal("0"))),
        "gross_traded": str(sum((decimal_value(fill["gross_amount"], "gross_amount") for fill in fills), Decimal("0"))),
        "filled_orders": len(fills),
        "unfilled_orders": sum(len(day["execution"]["unfilled_orders"]) for day in days),
        "nonoverlapping_24_session_windows": windows,
    }


def compare_fixture(request: Mapping[str, object], strategies: Mapping[str, object], baseline_id: str) -> dict:
    """Replay each strategy; never accept supplied performance numbers."""
    from .service import BacktestService

    if not isinstance(strategies, Mapping) or len(strategies) < 2:
        raise BacktestToolError("策略比較至少需要兩組逐日輸入")
    if not isinstance(baseline_id, str) or baseline_id not in strategies:
        raise BacktestToolError("基準策略必須存在於比較輸入")
    if any(not isinstance(key, str) or not key.strip() for key in strategies):
        raise BacktestToolError("策略 ID 必須是非空字串")
    sessions = list(HistoricalClock(request))
    service = BacktestService()
    shared_hash = None
    results = {}
    for strategy_id in sorted(strategies):
        inputs = strategies[strategy_id]
        if not isinstance(inputs, Mapping):
            raise BacktestToolError("%s 逐日輸入必須是物件" % strategy_id)
        market_days = []
        for session in sessions:
            trade_date = session["trade_date"]
            item = inputs.get(trade_date)
            if not isinstance(item, Mapping):
                raise BacktestToolError("%s 缺少交易日輸入：%s" % (strategy_id, trade_date))
            market_days.append({
                "trade_date": trade_date,
                "execution_market": item.get("execution_market"),
                "close_market": item.get("close_market"),
                "corporate_actions": item.get("corporate_actions", []),
            })
        market_hash = canonical_sha256(market_days)
        if shared_hash is None:
            shared_hash = market_hash
        elif market_hash != shared_hash:
            raise BacktestToolError("策略比較的市場價格或公司行動不一致")
        run = service.run_fixture(request, inputs)
        results[strategy_id] = {
            "backtest_id": run["backtest_id"],
            "strategy_inputs_sha256": canonical_sha256(inputs),
            "metrics": _metrics(run),
        }
    baseline = results[baseline_id]["metrics"]
    baseline_return = decimal_value(baseline["first_close_to_last_close_return"], "baseline.return")
    baseline_windows = baseline["nonoverlapping_24_session_windows"]
    for item in results.values():
        metrics = item["metrics"]
        metrics["excess_return_vs_baseline"] = _ratio(
            decimal_value(metrics["first_close_to_last_close_return"], "strategy.return") - baseline_return
        )
        metrics["nonoverlapping_24_session_excess"] = [
            _ratio(decimal_value(window["return"], "window.return") - decimal_value(base["return"], "baseline.window.return"))
            for window, base in zip(metrics["nonoverlapping_24_session_windows"], baseline_windows)
        ]
    missing = sorted(set(REQUIRED_STRATEGIES) - set(strategies))
    body = {
        "schema_version": "1.0", "comparison_id": "", "request_hash": request["content_sha256"],
        "market_inputs_sha256": shared_hash, "baseline_id": baseline_id,
        "first_trade_date": sessions[0]["trade_date"], "last_trade_date": sessions[-1]["trade_date"],
        "session_count": len(sessions), "strategies": results,
        "missing_planned_strategies": missing,
        "evidence_status": "insufficient",
        "limitations": [
            "報酬與回撤由首日收盤 NAV 起算，不含首日交易前報酬；成本包含完整比較期間成交。",
            "24 交易日視窗不重疊；沒有樣本外驗證或不確定性區間，不可據此宣稱策略有效。",
            "策略 ID 僅為輸入標籤；比較器不證明 equal_weight_25 真正持有 25 檔，也不證明各研究策略使用相同可查資料範圍。",
        ],
    }
    body["comparison_id"] = "comparison:" + canonical_sha256(body)[:20]
    body["content_sha256"] = content_sha256(body)
    return body
