"""從已驗證的虛擬帳本 run 鏈彙整本金、NAV、報酬率與持倉損益。

只讀取 VirtualAccountRepository 已封存且通過 manifest／雜湊驗證的狀態，
不寫入帳本、不補假值；任何斷鏈或驗證失敗都直接拋出錯誤。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Dict, List, Mapping, Optional

from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository


class PerformanceError(ValueError):
    """帳本無法彙整成績效摘要。"""


def _dec(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise PerformanceError("%s 不是有效數值" % label) from error
    if not result.is_finite():
        raise PerformanceError("%s 不是有限數值" % label)
    return result


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _rate(numerator: Decimal, denominator: Decimal) -> Optional[str]:
    if denominator <= 0:
        return None
    return str((numerator / denominator).quantize(Decimal("0.000001")))


def _read_json(path) -> Mapping[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _chain(repository: VirtualAccountRepository) -> List[Dict[str, object]]:
    """由 latest 沿 parent_run_id 走回 genesis，回傳由舊到新的 run。"""
    current = repository.latest()
    runs: List[Dict[str, object]] = []
    run_id: Optional[str] = str(current["run_id"])
    expected_parent: Optional[str] = None
    while run_id is not None:
        run_dir = repository.verify(run_id)
        state = _read_json(run_dir / "state.json")
        if expected_parent is not None and state.get("state_id") != expected_parent:
            raise PerformanceError("帳本父子狀態斷鏈：%s" % run_id)
        transition_path = run_dir / "transition.json"
        runs.append({
            "run_id": run_id, "state": state,
            "transition": _read_json(transition_path) if transition_path.exists() else None,
        })
        provenance = state.get("provenance") or {}
        expected_parent = state.get("parent_state_id")
        run_id = provenance.get("parent_run_id")
        if run_id is None and expected_parent is not None:
            raise PerformanceError("狀態有父狀態但缺少 parent_run_id")
    runs.reverse()
    if runs[0]["state"].get("provenance", {}).get("type") != "genesis":
        raise PerformanceError("帳本鏈起點不是 genesis")
    return runs


def _positions(state: Mapping[str, object]) -> List[Dict[str, object]]:
    rows = []
    for row in state.get("positions", []):
        shares = int(row["shares"])
        cost = _dec(row["cost_basis"], "%s.cost_basis" % row["symbol"])
        price = _dec(row["close_price"], "%s.close_price" % row["symbol"]) if row.get("close_price") is not None else None
        market_value = _dec(row["market_value"], "%s.market_value" % row["symbol"]) if row.get("market_value") is not None else None
        cost_value = cost * shares
        pnl = market_value - cost_value if market_value is not None else None
        rows.append({
            "symbol": row["symbol"], "shares": shares, "lots": shares // 1000,
            "average_cost": _money(cost), "price": _money(price) if price is not None else None,
            "cost_value": _money(cost_value),
            "market_value": _money(market_value) if market_value is not None else None,
            "unrealized_pnl": _money(pnl) if pnl is not None else None,
            "unrealized_return": _rate(pnl, cost_value) if pnl is not None else None,
        })
    return rows


def build_performance_summary(repository: VirtualAccountRepository) -> Dict[str, object]:
    """彙整本金、目前 NAV、今日／累積報酬、費稅與持倉。"""
    try:
        if repository.latest(optional=True) is None:
            return {"status": "uninitialized", "account_id": repository.root.name}
        runs = _chain(repository)
        genesis = _read_json(repository.verify("genesis") / "genesis.json")
    except (VirtualAccountError, OSError, json.JSONDecodeError, KeyError) as error:
        raise PerformanceError("帳本驗證失敗：%s" % error) from error

    initial = _dec(genesis["initial_capital"], "initial_capital")
    # 每個估值交易日一點：close 以執行日收盤價估值，prepared 以 Snapshot 最新收盤價估值；
    # 同一交易日保留最後狀態，早於開帳日的估值不另列（開帳前不可能有交易）。
    points: List[Dict[str, object]] = []
    commission = Decimal(0)
    tax = Decimal(0)
    trade_count = 0
    for item in runs:
        state = item["state"]
        provenance = state["provenance"]
        kind = provenance["type"]
        if kind == "genesis":
            valuation_date = str(state["as_of"])[:10]
        else:
            valuation_date = provenance.get("trade_date") or (item["transition"] or {}).get("close_ledger", {}).get("trade_date")
        point = {"run_id": item["run_id"], "as_of": state["as_of"], "trade_date": valuation_date, "type": kind, "nav": _dec(state["nav"], "nav")}
        if not points:
            points.append(point)
        elif valuation_date and valuation_date > points[-1]["trade_date"]:
            points.append(point)
        elif points[-1]["type"] != "genesis" and valuation_date == points[-1]["trade_date"]:
            point["type"] = "close" if "close" in {kind, points[-1]["type"]} else kind
            points[-1] = point
        transition = item["transition"]
        if transition:
            for fill in transition.get("execution", {}).get("fills", []):
                commission += _dec(fill.get("commission", 0), "commission")
                tax += _dec(fill.get("tax", 0), "tax")
                trade_count += 1

    latest = runs[-1]["state"]
    nav = _dec(latest["nav"], "nav")
    settled = _dec(latest["settled_cash"], "settled_cash")
    unsettled = _dec(latest["unsettled_cash"], "unsettled_cash")
    positions = _positions(latest)
    market_value = sum((_dec(row["market_value"], "market_value") for row in positions if row["market_value"] is not None), Decimal(0))

    history = []
    previous: Optional[Decimal] = None
    for point in points:
        change = point["nav"] - previous if previous is not None else None
        history.append({
            "run_id": point["run_id"], "as_of": point["as_of"], "trade_date": point["trade_date"], "type": point["type"],
            "nav": _money(point["nav"]),
            "daily_pnl": _money(change) if change is not None else None,
            "daily_return": _rate(change, previous) if change is not None else None,
            "cumulative_return": _rate(point["nav"] - initial, initial),
        })
        previous = point["nav"]

    today = history[-1] if len(history) > 1 else None
    return {
        "status": "ok",
        "account_id": repository.root.name,
        "currency": genesis.get("currency", "TWD"),
        "started_at": genesis.get("started_at"),
        "latest_run_id": runs[-1]["run_id"],
        "latest_state_type": latest["provenance"]["type"],
        "as_of": latest["as_of"],
        "initial_capital": _money(initial),
        "nav": _money(nav),
        "cash": _money(settled + unsettled),
        "settled_cash": _money(settled),
        "unsettled_cash": _money(unsettled),
        "market_value": _money(market_value),
        "cash_weight": _rate(settled + unsettled, nav),
        "total_pnl": _money(nav - initial),
        "total_return": _rate(nav - initial, initial),
        "today": {
            "as_of": today["as_of"], "trade_date": today["trade_date"],
            "pnl": today["daily_pnl"], "return": today["daily_return"],
        } if today else None,
        "fees": {"commission": _money(commission), "tax": _money(tax), "fill_count": trade_count},
        "positions": positions,
        "history": history,
    }
