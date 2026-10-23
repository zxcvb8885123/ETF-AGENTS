"""虛擬帳戶績效對基準（0050 買入持有、交易池等權買入持有）的比較。

口徑：
- 起點：帳戶開帳（genesis）當時全為現金；基準在開帳後第一個可成交的官方交易日
  （開帳時間在交件期限 08:55 前算當天，否則下一個交易日）以官方成交均價整張買進，
  手續費與帳本相同（``config/decision_rules.json``），剩餘現金留著，不再交易、不計股利。
- 估值：每個交易日以官方未還原收盤價估值；交易日集合取自交易池在 as-of 時點已有的
  官方日線。只採用 ``fetched_at ≤ as_of`` 且收盤時間 ≤ as_of 的版本，不前視。
- 指標：累積報酬、超額報酬（帳戶 − 基準，取雙方都有估值的最後一個交易日）、最大回撤、
  相對回撤（帳戶 NAV ÷ 基準 NAV 序列的最大回撤）。資料不足時該項 unavailable，不補假值。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.core import parse_aware_time, parse_decimal
from etf_agent.dashboard.performance import PerformanceError, build_performance_summary
from etf_agent.data.benchmarks import (
    BENCHMARK_KIND_EQUAL_WEIGHT,
    BENCHMARK_KIND_SINGLE,
    BenchmarkConfigError,
    BenchmarkDefinition,
    load_benchmarks,
)
from etf_agent.data.trading_calendar import DECISION_DEADLINE
from etf_agent.data.universe import UniverseLoader
from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository

from .contracts import EXECUTION_PRICE_RULE, PRICE_BASIS, TAIPEI, BenchmarkError, FeeSchedule
from .repository import BenchmarkPriceRepository, PriceTable
from .tools import buy_and_hold, cumulative_return, max_drawdown, money, rate, relative_drawdown, valuation

METHOD = {
    "price_basis": PRICE_BASIS,
    "execution_price_rule": EXECUTION_PRICE_RULE,
    "start_rule": "開帳時間在交件期限（台北 08:55）前算當天、否則下一個官方交易日，以該日官方成交均價整張買進",
    "valuation": "現金＋持股 × 當日官方未還原收盤價；現金股利與配股不計（帳本亦只在提供公司行動時入帳）",
    "as_of_rule": "只用 fetched_at ≤ as_of 且收盤時間 ≤ as_of 的官方日線版本",
    "excess_return": "帳戶累積報酬 − 基準累積報酬，取雙方都有估值的最後一個交易日",
    "relative_drawdown": "帳戶 NAV ÷ 基準 NAV（同日對齊）序列的最大回撤",
}


def _unavailable(definition: BenchmarkDefinition, reason: str, **extra: object) -> Dict[str, object]:
    body: Dict[str, object] = {**definition.as_dict(), "status": "unavailable", "reason": reason}
    body.update(extra)
    return body


def _first_execution_date(started_at: datetime, trading_dates: Sequence[str]) -> Optional[str]:
    local = started_at.astimezone(TAIPEI)
    for trade_date in trading_dates:
        if trade_date > local.date().isoformat():
            return trade_date
        if trade_date == local.date().isoformat() and local.time() < DECISION_DEADLINE:
            return trade_date
    return None


def _series_with_metrics(
    genesis_point: Mapping[str, object],
    capital: Decimal,
    cash: Decimal,
    positions: Mapping[str, int],
    trading_dates: Sequence[str],
    prices: PriceTable,
) -> Dict[str, object]:
    series: List[Dict[str, object]] = [dict(genesis_point)]
    navs: List[Decimal] = [capital]
    for trade_date in trading_dates:
        closes = {symbol: row.close_price for symbol, row in prices.get(trade_date, {}).items()}
        nav, missing = valuation(cash, positions, closes)
        if nav is None:
            series.append({"trade_date": trade_date, "nav": None, "cumulative_return": None,
                           "reason": "缺官方收盤價：%s" % "、".join(missing[:10]) + ("…" if len(missing) > 10 else "")})
            continue
        navs.append(nav)
        series.append({"trade_date": trade_date, "nav": money(nav), "cumulative_return": rate(cumulative_return(nav, capital))})
    latest = next((point for point in reversed(series) if point["nav"] is not None), None)
    return {
        "series": series,
        "latest": {"trade_date": latest["trade_date"], "nav": latest["nav"], "cumulative_return": latest["cumulative_return"]} if latest else None,
        "max_drawdown": rate(max_drawdown(navs)),
        "valued_days": len(navs) - 1,
        "unvalued_days": sum(1 for point in series if point["nav"] is None),
    }


def _compare(account_points: Mapping[str, Decimal], benchmark_series: Sequence[Mapping[str, object]], capital: Decimal) -> Dict[str, object]:
    aligned: List[str] = []
    account_navs: List[Decimal] = []
    benchmark_navs: List[Decimal] = []
    for point in benchmark_series:
        trade_date = str(point["trade_date"])
        if point["nav"] is None or trade_date not in account_points:
            continue
        aligned.append(trade_date)
        account_navs.append(account_points[trade_date])
        benchmark_navs.append(Decimal(str(point["nav"])))
    if len(aligned) < 2:
        # 只有開帳點對齊（雙方都是全現金）時沒有可比較的內容。
        return {"status": "unavailable", "reason": "帳戶與基準在開帳後沒有同一天都有估值的交易日", "aligned_days": len(aligned)}
    account_cum = cumulative_return(account_navs[-1], capital)
    benchmark_cum = cumulative_return(benchmark_navs[-1], capital)
    return {
        "status": "ok",
        "from_trade_date": aligned[0],
        "trade_date": aligned[-1],
        "aligned_days": len(aligned),
        "account_nav": money(account_navs[-1]),
        "benchmark_nav": money(benchmark_navs[-1]),
        "account_cumulative_return": rate(account_cum),
        "benchmark_cumulative_return": rate(benchmark_cum),
        "excess_return": rate(account_cum - benchmark_cum),
        "relative_drawdown": rate(relative_drawdown(account_navs, benchmark_navs)),
    }


def _build_single(definition: BenchmarkDefinition, repository: BenchmarkPriceRepository, capital: Decimal,
                  fees: FeeSchedule, start_date: str, trading_dates: Sequence[str], as_of: datetime,
                  genesis_point: Mapping[str, object], end_date: str) -> Dict[str, object]:
    symbol = definition.instrument.symbol
    prices = repository.official_prices([symbol], start_date, end_date, as_of)
    start_row = prices.get(start_date, {}).get(symbol)
    if start_row is None:
        return _unavailable(definition, "%s 在起始日 %s 沒有 as-of 時已可得的官方行情；請先用 cli/benchmark.py collect 補抓" % (symbol, start_date), start_date=start_date)
    average = start_row.average_price()
    if average is None:
        return _unavailable(definition, "%s 在起始日 %s 無成交，沒有成交均價" % (symbol, start_date), start_date=start_date)
    portfolio = buy_and_hold(capital, {symbol: capital}, {symbol: average}, fees)
    if not portfolio["positions"]:
        return _unavailable(definition, "本金不足以買進一張 %s" % symbol, start_date=start_date)
    body = {**definition.as_dict(), "status": "ok", "start_date": start_date,
            "execution": {"trade_date": start_date, "price": str(average), "source": start_row.source,
                          "fetched_at": start_row.fetched_at.isoformat(), "fills": portfolio["fills"]},
            "cash": money(portfolio["cash"]), "commission": money(portfolio["commission"]),
            "positions": {symbol: shares for symbol, shares in sorted(portfolio["positions"].items())}}
    body.update(_series_with_metrics(genesis_point, capital, portfolio["cash"], portfolio["positions"], trading_dates, prices))
    return body


def _build_equal_weight(definition: BenchmarkDefinition, universe_prices: PriceTable, symbols: Sequence[str],
                        capital: Decimal, fees: FeeSchedule, start_date: str, trading_dates: Sequence[str],
                        genesis_point: Mapping[str, object]) -> Dict[str, object]:
    start_rows = universe_prices.get(start_date, {})
    missing = sorted(symbol for symbol in symbols if symbol not in start_rows)
    if missing:
        return _unavailable(definition, "起始日 %s 有 %d 檔交易池股票缺官方行情：%s" % (start_date, len(missing), "、".join(missing[:10]) + ("…" if len(missing) > 10 else "")), start_date=start_date)
    execution_prices: Dict[str, Decimal] = {}
    untraded: List[str] = []
    for symbol in symbols:
        average = start_rows[symbol].average_price()
        if average is None:
            untraded.append(symbol)
        else:
            execution_prices[symbol] = average
    if untraded:
        return _unavailable(definition, "起始日 %s 有 %d 檔無成交、沒有成交均價：%s" % (start_date, len(untraded), "、".join(untraded[:10]) + ("…" if len(untraded) > 10 else "")), start_date=start_date)
    budget = capital / Decimal(len(symbols))
    portfolio = buy_and_hold(capital, {symbol: budget for symbol in symbols}, execution_prices, fees)
    unaffordable = sorted(set(symbols) - set(portfolio["positions"]))
    body = {**definition.as_dict(), "status": "ok", "start_date": start_date, "symbols": len(symbols),
            "budget_per_symbol": money(budget),
            "execution": {"trade_date": start_date, "fills": len(portfolio["fills"]), "unaffordable_symbols": unaffordable,
                          "sources": sorted({row.source for row in start_rows.values()})},
            "cash": money(portfolio["cash"]), "commission": money(portfolio["commission"]),
            "positions": {symbol: shares for symbol, shares in sorted(portfolio["positions"].items())}}
    body.update(_series_with_metrics(genesis_point, capital, portfolio["cash"], portfolio["positions"], trading_dates, universe_prices))
    return body


def build_benchmark_comparison(
    repository: VirtualAccountRepository,
    database: Path,
    rules_path: Path,
    benchmarks_path: Path,
    root: Path,
    as_of: Optional[str] = None,
) -> Dict[str, object]:
    """帳戶 NAV 序列對各基準的比較；帳本未開帳回 uninitialized，基準資料不足時該基準 unavailable。"""
    try:
        if repository.latest(optional=True) is None:
            return {"status": "uninitialized", "account_id": repository.root.name}
        summary = build_performance_summary(repository)
    except (PerformanceError, VirtualAccountError) as error:
        raise BenchmarkError("帳本驗證失敗：%s" % error) from error
    started_at = parse_aware_time(summary.get("started_at"), "started_at", error=BenchmarkError)
    as_of_time = parse_aware_time(as_of or summary["as_of"], "as_of", error=BenchmarkError)
    if as_of_time < started_at:
        raise BenchmarkError("as_of 不得早於開帳時間")
    capital = parse_decimal(summary["initial_capital"], "initial_capital", error=BenchmarkError)
    if capital <= 0:
        raise BenchmarkError("初始資金必須為正數")
    fees = FeeSchedule.from_rules(rules_path)
    try:
        definitions = load_benchmarks(benchmarks_path)
    except BenchmarkConfigError as error:
        raise BenchmarkError(str(error)) from error

    history = summary["history"]
    account_points = {str(point["trade_date"]): Decimal(str(point["nav"])) for point in history if point.get("trade_date")}
    genesis_point = {"trade_date": history[0]["trade_date"], "nav": money(capital), "cumulative_return": rate(Decimal(0)), "type": "genesis"}
    account_navs = [Decimal(str(point["nav"])) for point in history]
    account = {
        "latest_trade_date": history[-1]["trade_date"], "nav": summary["nav"],
        "cumulative_return": summary["total_return"], "max_drawdown": rate(max_drawdown(account_navs)),
        "series": [{"trade_date": point["trade_date"], "nav": point["nav"], "cumulative_return": point["cumulative_return"], "type": point["type"]} for point in history],
    }

    price_repository = BenchmarkPriceRepository(database)
    universe_definition = next((item for item in definitions if item.kind == BENCHMARK_KIND_EQUAL_WEIGHT), None)
    universe_path = Path(root) / (universe_definition.universe_path if universe_definition else "data/official_universe.csv")
    try:
        symbols = [item.symbol for item in UniverseLoader().load(universe_path)]
    except (OSError, ValueError) as error:
        raise BenchmarkError("無法載入交易池：%s" % error) from error
    end_date = as_of_time.astimezone(TAIPEI).date().isoformat()
    start_floor = started_at.astimezone(TAIPEI).date().isoformat()
    universe_prices = price_repository.official_prices(symbols, start_floor, end_date, as_of_time)
    # universe_prices 只含 as-of 時已收盤、已可得的交易日；起始日再依交件期限規則決定。
    trading_dates = sorted(universe_prices)
    start_date = _first_execution_date(started_at, trading_dates)
    trading_dates = [trade_date for trade_date in trading_dates if start_date is not None and trade_date >= start_date]

    benchmarks: List[Dict[str, object]] = []
    for definition in definitions:
        if not definition.approved:
            benchmarks.append(_unavailable(definition, "license_status=%s，未核准的來源不進正式計算" % definition.license_status))
            continue
        if start_date is None:
            benchmarks.append(_unavailable(definition, "開帳後交易池尚無 as-of %s 時已可得的官方交易日行情" % as_of_time.isoformat()))
            continue
        if definition.kind == BENCHMARK_KIND_SINGLE:
            result = _build_single(definition, price_repository, capital, fees, start_date, trading_dates, as_of_time, genesis_point, end_date)
        else:
            result = _build_equal_weight(definition, universe_prices, symbols, capital, fees, start_date, trading_dates, genesis_point)
        if result["status"] == "ok":
            result["comparison"] = _compare(account_points, result["series"], capital)
        benchmarks.append(result)

    return {
        "status": "ok",
        "account_id": repository.root.name,
        "currency": summary.get("currency", "TWD"),
        "started_at": summary.get("started_at"),
        "as_of": as_of_time.isoformat(),
        "initial_capital": summary["initial_capital"],
        "start_date": start_date,
        "trading_days": len(trading_dates),
        "fees": fees.as_dict(),
        "method": dict(METHOD),
        "account": account,
        "benchmarks": benchmarks,
    }
