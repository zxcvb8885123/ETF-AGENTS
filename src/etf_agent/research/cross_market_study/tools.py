"""由原始歷史重建每日訊號與隔日目標，並做事先登記的統計；全部確定性、Decimal。"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from etf_agent.calibration.tools import format_decimal, mean, rank_ic
from etf_agent.core import decimal_string
from etf_agent.perception.overnight_market.tools import historical_signals
from .contracts import (
    BUY_COST, CASH_BY_STANCE, CASH_RULES, SELL_COST,
    DECISION_HOUR, DECISION_MINUTE, EXPLORATORY_TARGETS, FORWARD_TARGETS, MIN_DAYS, MIN_GROUP_DAYS, PASS_T_STAT, PRIMARY_SIGNALS,
    PRIMARY_TARGET, SIGNALS, CrossMarketStudyError,
)

TAIPEI = ZoneInfo("Asia/Taipei")
HUNDRED = Decimal(100)
Z95 = Decimal("1.96")


def _pct(ratio: Decimal) -> Decimal:
    return ratio * HUNDRED


def night_changes(rows: Sequence[Mapping[str, str]]) -> Dict[str, Decimal]:
    """每個日期取盤後列中成交量最大的月合約；漲跌幅 = 最後價相對前一日結算價（= 收盤 − 漲跌）。"""
    best: Dict[str, tuple] = {}
    for row in rows:
        if row["trading_session"] != "after_market":
            continue
        try:
            close, spread, volume = Decimal(row["close"]), Decimal(row["spread"]), int(float(row["volume"]))
        except (ArithmeticError, ValueError):
            continue
        ref = close - spread
        if close <= 0 or ref <= 0 or volume <= 0:
            continue
        key = (-volume, row["contract_date"])
        if row["date"] not in best or key < best[row["date"]][0]:
            best[row["date"]] = (key, spread / ref * HUNDRED)
    return {day: value[1] for day, value in best.items()}


def build_dataset(raw: Mapping[str, object]) -> List[Dict[str, object]]:
    """每個台股交易日 t 一列：07:00 當下的隔夜訊號，與 t 日的開盤跳空、開→收、收→收、均價代理、振幅。"""
    target = raw["target"]
    if not isinstance(target, list) or len(target) < 3:
        raise CrossMarketStudyError("台股加權指數歷史不足")
    bars = {symbol: {item["date"]: Decimal(item["close"]) for item in rows}
            for symbol, rows in raw["yahoo"].items()}  # type: ignore[union-attr]
    nights = night_changes(raw["night"])  # type: ignore[arg-type]
    values = [{key: Decimal(item[key]) for key in ("open", "high", "low", "close")} | {"date": item["date"]} for item in target]
    rows: List[Dict[str, object]] = []
    for index in range(1, len(values)):
        today, previous = values[index], values[index - 1]
        decision_at = datetime.combine(datetime.strptime(today["date"], "%Y-%m-%d").date(),
                                       time(DECISION_HOUR, DECISION_MINUTE), tzinfo=TAIPEI)
        sig = historical_signals(bars, nights.get(today["date"]), previous["date"], decision_at)
        comps = sig["components"]
        average = lambda symbols: (sum(comps[s] for s in symbols) / len(symbols)) if all(s in comps for s in symbols) else None
        agreement = sig["agreement"]
        score = None
        if agreement and agreement["total"] >= 3:
            score = Decimal(agreement["up"] - agreement["down"]) / agreement["total"]
        mid = lambda row: (row["open"] + row["high"] + row["low"] + row["close"]) / 4
        forward = {"fill_to_close": _pct(today["close"] / mid(today) - 1)}
        if index + 1 < len(values):
            forward["fill_to_next_avg"] = _pct(mid(values[index + 1]) / mid(today) - 1)
        rows.append({
            "date": today["date"],
            "signals": {"us_main": average(("^SOX", "^GSPC")), "proxy": average(("EWT", "TSM")),
                        "asia": average(("^N225", "^KS11")), "night": comps.get("TXF_NIGHT"),
                        "agreement_score": score, "vix_change": comps.get("^VIX")},
            "outlook": sig["outlook"], "agreement_label": agreement["label"] if agreement else None,
            "targets": {"gap": _pct(today["open"] / previous["close"] - 1),
                        "open_to_close": _pct(today["close"] / today["open"] - 1),
                        "close_to_close": _pct(today["close"] / previous["close"] - 1),
                        "avg_price": _pct(mid(today) / mid(previous) - 1),
                        "range": _pct((today["high"] - today["low"]) / previous["close"]), **forward},
        })
    return rows


def t_statistic(ic: Optional[Decimal], n: int) -> Optional[Decimal]:
    if ic is None or n < 3 or abs(ic) >= 1:
        return None
    return ic * ((Decimal(n - 2)) / (1 - ic * ic)).sqrt()


def wilson(hits: int, n: int):
    if n == 0:
        return None
    p, z2 = Decimal(hits) / n, Z95 * Z95
    center = (p + z2 / (2 * n)) / (1 + z2 / n)
    margin = Z95 * (p * (1 - p) / n + z2 / (4 * n * n)).sqrt() / (1 + z2 / n)
    return [format_decimal(max(center - margin, Decimal(0))), format_decimal(min(center + margin, Decimal(1)))]


def _pairs(rows, signal, target, transform=None):
    return [((transform(row["signals"][signal]) if transform else row["signals"][signal]), row["targets"][target])
            for row in rows if row["signals"][signal] is not None and target in row["targets"]]


def _ic_block(pairs) -> Dict[str, object]:
    ic = rank_ic(pairs)
    t = t_statistic(ic, len(pairs))
    half = len(pairs) // 2
    first, second = rank_ic(pairs[:half]), rank_ic(pairs[half:])
    return {"n": len(pairs), "rank_ic": format_decimal(ic), "t_stat": format_decimal(t),
            "first_half_ic": format_decimal(first), "second_half_ic": format_decimal(second)}


def _verdict(block: Mapping[str, object]) -> str:
    if block["n"] < MIN_DAYS:  # type: ignore[operator]
        return "insufficient_sample"
    ic, t, first, second = (Decimal(block[k]) if block[k] is not None else None
                            for k in ("rank_ic", "t_stat", "first_half_ic", "second_half_ic"))
    if ic is None or t is None or first is None or second is None:
        return "not_supported"
    same = all((value > 0) == (ic > 0) for value in (first, second))
    return "supported" if abs(t) >= PASS_T_STAT and same else "not_supported"


def _hit_rate(rows, signal) -> Dict[str, object]:
    usable = [(row["signals"][signal], row["targets"][PRIMARY_TARGET]) for row in rows
              if row["signals"][signal] is not None and row["signals"][signal] != 0 and row["targets"][PRIMARY_TARGET] != 0]
    hits = sum(1 for s, g in usable if (s > 0) == (g > 0))
    return {"n": len(usable), "hit_rate": format_decimal(Decimal(hits) / len(usable)) if usable else None,
            "wilson_95": wilson(hits, len(usable))}


def _groups(rows, key) -> Dict[str, Dict[str, object]]:
    out: Dict[str, Dict[str, object]] = {}
    for label in sorted({row[key] for row in rows if row[key]}):
        members = [row for row in rows if row[key] == label]
        entry: Dict[str, object] = {"days": len(members)}
        if len(members) >= MIN_GROUP_DAYS:
            for target in ("gap", "close_to_close", "range"):
                entry["mean_" + target] = format_decimal(mean([row["targets"][target] for row in members]))
        else:
            entry["note"] = "樣本不足 %d 天，不報平均" % MIN_GROUP_DAYS
        out[label] = entry
    return out


def analyze(rows: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    primary = {}
    for signal in PRIMARY_SIGNALS:
        block = _ic_block(_pairs(rows, signal, PRIMARY_TARGET))
        block["verdict"] = _verdict(block)
        block["gap_sign_hits"] = _hit_rate(rows, signal)
        primary[signal] = block
    forward = {}
    for signal in PRIMARY_SIGNALS:
        forward[signal] = {}
        for target in FORWARD_TARGETS:
            block = _ic_block(_pairs(rows, signal, target))
            block["verdict"] = _verdict(block)
            forward[signal][target] = block
    exploratory = {signal: {target: _ic_block(_pairs(rows, signal, target)) for target in EXPLORATORY_TARGETS if target != "range"}
                   for signal in SIGNALS}
    magnitude = {signal: _ic_block(_pairs(rows, signal, "range", abs)) for signal in SIGNALS}
    return {
        "days": len(rows), "first_day": rows[0]["date"] if rows else None, "last_day": rows[-1]["date"] if rows else None,
        "primary": primary,
        "forward_from_fill_post_hoc": forward,
        "exploratory_signed": exploratory,
        "exploratory_magnitude_vs_range": magnitude,
        "by_outlook": _groups(rows, "outlook"), "by_agreement": _groups(rows, "agreement_label"),
        "criteria": {"min_days": MIN_DAYS, "primary_target": PRIMARY_TARGET, "primary_signals": list(PRIMARY_SIGNALS),
                     "pass_t_stat": str(PASS_T_STAT), "halves_must_share_sign": True,
                     "note": "只有 primary 判通過與否；exploratory 不判通過（多重檢定）。目標 gap 為 t 日開盤相對 t-1 收盤，其餘以 ^TWII 指數計，不是比賽用的成交均價。"},
    }


# ---------------------------------------------------------------- 交易池目標與現金姿態模擬
POOL_MIN_SYMBOLS = 50
POOL_TARGETS = ("pool_gap", "pool_fill_to_close", "pool_fill_to_next_avg", "pool_official_next")


def pool_series(pool: Mapping[str, object]) -> Dict[str, Dict[str, Decimal]]:
    """交易池等權報酬（%）。均價代理為 (O+H+L+C)/4；官方目標用官方成交金額 ÷ 股數。
    該股該日有除權息標記就整檔排除，涉及的任何一天有標記都不計。"""
    yahoo = pool["yahoo"]  # type: ignore[index]
    official = pool["official_vwap"]  # type: ignore[index]
    days = sorted(yahoo)
    num = lambda text: Decimal(text)

    def avg(row):
        return (num(row["open"]) + num(row["high"]) + num(row["low"]) + num(row["close"])) / 4

    def mean_pct(values):
        return (sum(values) / len(values) * HUNDRED) if len(values) >= POOL_MIN_SYMBOLS else None

    result: Dict[str, Dict[str, Decimal]] = {}
    for index in range(1, len(days)):
        day, prev = days[index], days[index - 1]
        today, before = yahoo[day], yahoo[prev]
        common = [s for s in today if s in before and not today[s]["ex"] and not before[s]["ex"]]
        entry: Dict[str, Decimal] = {}
        gap = mean_pct([num(today[s]["open"]) / num(before[s]["close"]) - 1 for s in common])
        close = mean_pct([num(today[s]["close"]) / avg(today[s]) - 1 for s in common])
        if gap is not None:
            entry["pool_gap"] = gap
        if close is not None:
            entry["pool_fill_to_close"] = close
        if index + 1 < len(days):
            nxt = days[index + 1]
            both = [s for s in today if s in yahoo[nxt] and not today[s]["ex"] and not yahoo[nxt][s]["ex"]]
            forward = mean_pct([avg(yahoo[nxt][s]) / avg(today[s]) - 1 for s in both])
            if forward is not None:
                entry["pool_fill_to_next_avg"] = forward
            if day in official and nxt in official:
                ok = [s for s in official[day] if s in official[nxt] and s in today and not today[s]["ex"]
                      and not yahoo[nxt].get(s, {"ex": True})["ex"]]
                real = mean_pct([num(official[nxt][s]) / num(official[day][s]) - 1 for s in ok])
                if real is not None:
                    entry["pool_official_next"] = real
        result[day] = entry
    return result


def simulate_cash(rows: Sequence[Mapping[str, object]], rule: Mapping[str, str],
                  target: str = "pool_fill_to_next_avg") -> Dict[str, object]:
    """每日依隔夜訊號決定現金姿態，在 t 日均價重配到目標曝險，賺 t 日均價到 t+1 日均價的交易池報酬；
    扣買賣成本。基準是全程 neutral。回傳規則與基準的累積報酬、最大回落與逐日差的 t 值。"""
    series = [row for row in rows if target in row["targets"]]
    if len(series) < 3:
        return {"status": "insufficient_sample", "days": len(series)}

    def run(stance_of):
        nav, peak, worst, previous, costs, daily = Decimal(1), Decimal(1), Decimal(0), None, Decimal(0), []
        for row in series:
            exposure = 1 - CASH_BY_STANCE[stance_of(row)]
            if previous is None:
                previous = exposure
            cost = (BUY_COST * (exposure - previous) if exposure > previous else SELL_COST * (previous - exposure))
            ret = exposure * row["targets"][target] / HUNDRED - cost
            nav *= 1 + ret
            peak = max(peak, nav)
            worst = min(worst, nav / peak - 1)
            costs += cost
            daily.append(ret)
            previous = exposure
        return {"nav": nav, "max_drawdown": worst, "costs": costs, "daily": daily}

    base = run(lambda row: "neutral")
    test = run(lambda row: rule.get(row["outlook"]) or rule.get(row["agreement_label"]) or "neutral")
    diffs = [a - b for a, b in zip(test["daily"], base["daily"])]
    n = len(diffs)
    average = sum(diffs) / n
    variance = sum(((d - average) ** 2 for d in diffs), Decimal(0)) / (n - 1)
    t = average / (variance / n).sqrt() if variance > 0 else None
    triggered = sum(1 for row in series if (rule.get(row["outlook"]) or rule.get(row["agreement_label"])))
    pct = lambda value: format_decimal(value * HUNDRED)
    return {"status": "ok", "days": n, "triggered_days": triggered,
            "baseline_total_return_pct": pct(base["nav"] - 1), "rule_total_return_pct": pct(test["nav"] - 1),
            "baseline_max_drawdown_pct": pct(base["max_drawdown"]), "rule_max_drawdown_pct": pct(test["max_drawdown"]),
            "baseline_costs_pct": pct(base["costs"]), "rule_costs_pct": pct(test["costs"]),
            "mean_daily_diff_pct": pct(average), "diff_t_stat": format_decimal(t)}


def analyze_pool(rows: Sequence[Mapping[str, object]], pool: Mapping[str, object]) -> Dict[str, object]:
    """把交易池目標接到每日列上做同一套檢定，並模擬現金姿態、核對均價代理與官方均價的差距。"""
    series = pool_series(pool)
    merged = [{**row, "targets": {**row["targets"], **series.get(row["date"], {})}} for row in rows]
    blocks: Dict[str, Dict[str, object]] = {}
    for signal in PRIMARY_SIGNALS:
        blocks[signal] = {}
        for target in POOL_TARGETS:
            block = _ic_block(_pairs(merged, signal, target))
            block["verdict"] = _verdict(block)
            blocks[signal][target] = block
    overlap = [(row["targets"]["pool_fill_to_next_avg"], row["targets"]["pool_official_next"]) for row in merged
               if "pool_fill_to_next_avg" in row["targets"] and "pool_official_next" in row["targets"]]
    proxy_check = {"days": len(overlap), "rank_ic_proxy_vs_official": format_decimal(rank_ic(overlap)),
                   "mean_abs_diff_pct": format_decimal(mean([abs(a - b) for a, b in overlap])) if overlap else None}
    return {"pool_symbols": len(pool["symbols"]),  # type: ignore[arg-type]
            "forward_targets_ic": blocks, "proxy_vs_official": proxy_check,
            "cash_simulation": {name: simulate_cash(merged, rule) for name, rule in CASH_RULES.items()},
            "note": "交易池為最新決策輸入的 150 檔（有存活者偏誤）；pool_official_next 只有資料庫內有官方列的日子（約三個月），樣本不足時不判通過。"}
