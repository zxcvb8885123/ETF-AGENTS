"""每週診斷包（確定性）：程式從已封存的決策與官方成交價算出事實，Agent 只負責解讀。

只含數值與列舉：不收分析師 findings、多空 claims、新聞標題等自由文字，簡報因此不會被不受信任的內容誘導。
每個指標固定帶 ``status``：``ok`` 才會成為可引用的證據；樣本不足或資料缺漏時只回報天數與筆數，
不給任何平均或命中率。報酬口徑與校準相同（成交日 T 是 cutoff 後第一個官方交易日、官方均價、
窗口內除權息排除、超額報酬減去同日交易池平均），沿用 ``evaluation.answers`` 與 ``calibration``。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from etf_agent.calibration.contracts import MIN_DECISION_DAYS, CalibrationError
from etf_agent.calibration.prices import OfficialPriceBook
from etf_agent.calibration.service import _latest_per_entry_day
from etf_agent.calibration.tools import format_decimal, mean
from etf_agent.core import parse_aware_time
from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.core.run_layout import list_run_dirs
from etf_agent.evaluation.answers import answer_key, pool_scores, score_day

from .thesis import DEFAULT_TOLERANCE, build_thesis_tracking, numeric_view

# 診斷只看這兩個 horizon：20 日在決策日少時不可能完整，其餘數字會誤導。
DIAGNOSTIC_HORIZONS = ("1", "5")
# 賣出事後檢討至少要這麼多筆賣單才給比例；少了只是雜訊。
MIN_SELL_OBSERVATIONS = 10
SELL_INTENTS = {"trim", "exit", "forced_exit"}
ANALYSTS = ("technical", "fundamental", "event")
DIAGNOSTICS_VERSION = "1.0"
UNAVAILABLE_BENCH = "尚未取得基準比較（由 service.make_brief 補上）；主動型 ETF 基準沒有價格歷史與授權審核，不在比較範圍。"


def _read(path: Path) -> Optional[Mapping[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_run(run_dir: Path) -> Optional[Dict[str, object]]:
    """讀一份已封存 Decision run；manifest 驗證失敗或缺檔就略過（診斷是旁觀者，不中斷）。"""
    manifest = _read(run_dir / "manifest.json")
    if manifest is None:
        return None
    try:
        ImmutableRunStore(run_dir.parent, schema_version=str(manifest.get("schema_version")), error=CalibrationError).verify(run_dir.name)
    except CalibrationError:
        return None
    files = {name: _read(run_dir / ("%s.json" % name)) for name in ("intent", "team_inputs", "decision")}
    if any(value is None for value in files.values()) or files["intent"].get("status") != "completed":  # type: ignore[union-attr]
        return None
    return {"decision_run_id": run_dir.name, "run_dir": str(run_dir), "decision_cutoff": files["intent"]["decision_cutoff"], **files}  # type: ignore[index]


def load_runs(decisions: Path) -> List[Dict[str, object]]:
    runs = [_load_run(run_dir) for run_dir in list_run_dirs(Path(decisions))] if Path(decisions).exists() else []
    return [run for run in runs if run is not None]


def _outputs(run: Mapping[str, object]) -> Dict[str, Dict[str, str]]:
    """分析師 outlook、交易意圖（intent/信心）與委託方向，格式同 ``evaluation.answers._calls`` 的輸入。"""
    team, intent, decision = run["team_inputs"], run["intent"], run["decision"]
    outputs: Dict[str, Dict[str, str]] = {}
    for name in ANALYSTS:
        report = (team.get("analyst_reports") or {}).get(name)  # type: ignore[union-attr]
        if isinstance(report, Mapping):
            outputs[name] = {str(item["symbol"]).upper(): str(item.get("outlook")) for item in report.get("items", [])}
    outputs["intent"] = {
        str(item["symbol"]).upper(): "%s/%s" % (item["intent"], item.get("conviction"))
        for item in intent.get("items", [])  # type: ignore[union-attr]
    }
    outputs["orders"] = {str(order["symbol"]).upper(): str(order["side"]) for order in decision.get("orders", [])}  # type: ignore[union-attr]
    return outputs


def _scored(entry: Mapping[str, object]) -> Dict[str, object]:
    """``pool_scores`` 的單一項目整理成純數值／列舉；status=ok 才有命中率與平均。"""
    result: Dict[str, object] = {"status": entry["status"], "complete_days": entry["complete_days"], "n": entry["n"]}
    if entry["status"] == "ok":
        for key in ("hit_rate", "up_mean_excess", "down_mean_excess"):
            if entry.get(key) is not None:
                result[key] = entry[key]
    else:
        result["required_days"] = MIN_DECISION_DAYS
    return result


def _analyst_and_sell(runs: List[Mapping[str, object]], book: OfficialPriceBook) -> Dict[str, object]:
    chosen, _ = _latest_per_entry_day(runs, book)
    pooled: Dict[str, List[tuple]] = {horizon: [] for horizon in DIAGNOSTIC_HORIZONS}
    sells: Dict[str, List[Decimal]] = {horizon: [] for horizon in DIAGNOSTIC_HORIZONS}
    sell_days: Dict[str, set] = {horizon: set() for horizon in DIAGNOSTIC_HORIZONS}
    for entry_day, run in sorted(chosen.items()):
        outputs = _outputs(run)
        symbols = sorted(outputs["intent"])
        key = answer_key(book, str(run["decision_cutoff"]), symbols)
        for horizon in DIAGNOSTIC_HORIZONS:
            answer = key["horizons"][horizon]
            if answer["status"] != "complete":
                continue
            pooled[horizon].append((entry_day, score_day(outputs, answer["excess"])))
            sold = [s for s, side in outputs["orders"].items() if side == "sell" and s in answer["excess"]]
            if sold:
                sell_days[horizon].add(entry_day)
                sells[horizon].extend(answer["excess"][s] for s in sold)
    analyst: Dict[str, object] = {}
    for horizon in DIAGNOSTIC_HORIZONS:
        for name, entry in pool_scores(pooled[horizon]).items():
            analyst.setdefault(name, {})["h%s" % horizon] = _scored(entry)  # type: ignore[index]
    sell: Dict[str, object] = {}
    for horizon in DIAGNOSTIC_HORIZONS:
        values = sells[horizon]
        entry: Dict[str, object] = {"complete_days": len(pooled[horizon]), "n": len(values)}
        if len(pooled[horizon]) < MIN_DECISION_DAYS or len(values) < MIN_SELL_OBSERVATIONS:
            entry.update(status="insufficient_sample", required_days=MIN_DECISION_DAYS, required_sells=MIN_SELL_OBSERVATIONS)
        else:
            regret = sum(1 for value in values if value > 0)
            entry.update(status="ok", mean_excess_after_sell=format_decimal(mean(values)),
                         regret_rate=str(round(regret / len(values), 4)))
        sell["h%s" % horizon] = entry
    return {"analyst": analyst, "sell": sell, "decision_days": len(chosen)}


def _turnover(runs: List[Mapping[str, object]], book: OfficialPriceBook) -> Dict[str, object]:
    """換手與費稅拖累是事實統計，沒有樣本門檻：成交金額與費稅除以當日 NAV。"""
    chosen, _ = _latest_per_entry_day(runs, book)
    days = [run for _, run in sorted(chosen.items())]
    result: Dict[str, object] = {"days": len(days)}
    if not days:
        result["status"] = "unavailable"
        result["reason"] = "沒有可用的決策日"
        return result
    ratios, fees, swaps = [], [], 0
    for run in days:
        decision = run["decision"]
        if not isinstance(decision.get("portfolio"), Mapping):  # type: ignore[union-attr]
            continue  # rejected 的決策沒有 portfolio，沒有成交也沒有 NAV
        nav = Decimal(str(decision["portfolio"]["nav"]))  # type: ignore[index]
        if nav <= 0:
            continue
        orders = decision.get("orders", [])  # type: ignore[union-attr]
        ratios.append(sum((Decimal(str(o["gross_amount"])) for o in orders), Decimal(0)) / nav)
        fees.append(sum((Decimal(str(o["commission"])) + Decimal(str(o["tax"])) for o in orders), Decimal(0)) / nav)
        swaps += sum(1 for o in orders if o.get("side") == "sell")
    result.update(
        status="ok", mean_turnover_rate=format_decimal(mean(ratios)), mean_fee_drag=format_decimal(mean(fees)),
        sell_orders=swaps,
    )
    return result


def _review_and_stance(runs: List[Mapping[str, object]], book: OfficialPriceBook) -> Dict[str, object]:
    """停損／停利警示的出現次數與交易 Agent 的回應（team_inputs 2.4 以後才有）；現金姿態與廣度的分布。"""
    chosen, _ = _latest_per_entry_day(runs, book)
    alerts: Dict[str, Dict[str, int]] = {}
    versions = 0
    stance: Dict[str, int] = {}
    breadth: Dict[str, int] = {}
    for _, run in sorted(chosen.items()):
        team = run["team_inputs"]
        level = (team.get("cash_stance") or {}).get("level")  # type: ignore[union-attr]
        if level:
            stance[str(level)] = stance.get(str(level), 0) + 1
        if (team.get("cash_stance") or {}).get("breadth"):  # type: ignore[union-attr]
            name = str(team["cash_stance"]["breadth"])  # type: ignore[index]
            breadth[name] = breadth.get(name, 0) + 1
        review = team.get("position_review")  # type: ignore[union-attr]
        if not isinstance(review, Mapping):
            continue
        versions += 1
        intents = {str(item["symbol"]).upper(): str(item["intent"]) for item in run["intent"]["items"]}  # type: ignore[index]
        for item in review.get("items", []):
            for flag in item.get("flags", []):
                response = intents.get(str(item["symbol"]).upper(), "unknown")
                bucket = alerts.setdefault(str(flag["flag"]), {})
                bucket[response] = bucket.get(response, 0) + 1
    review_entry: Dict[str, object] = {"days_with_review": versions}
    if versions == 0:
        review_entry.update(status="unavailable", reason="尚無 team_inputs 2.4 以後的決策日")
    else:
        review_entry["status"] = "ok"
        review_entry["alerts"] = {flag: dict(sorted(counts.items())) for flag, counts in sorted(alerts.items())}
        review_entry["alert_total"] = sum(sum(counts.values()) for counts in alerts.values())
    return {
        "review": review_entry,
        "stance": {"status": "ok", "days": dict(sorted(stance.items()))} if stance else {"status": "unavailable", "reason": "沒有現金姿態紀錄"},
        "breadth": {"status": "ok", "days": dict(sorted(breadth.items()))} if breadth else {"status": "unavailable", "reason": "沒有持股廣度紀錄"},
    }


def week_activity(runs: List[Mapping[str, object]], book: OfficialPriceBook, days: int = 5) -> Dict[str, object]:
    """最近 ``days`` 個成交日的買賣筆數與換手（一週為單位的事實，沒有樣本門檻）。"""
    chosen, _ = _latest_per_entry_day(runs, book)
    recent = [run for _, run in sorted(chosen.items())[-days:]]
    if not recent:
        return {"status": "unavailable", "reason": "沒有可用的決策日"}
    orders = [o for run in recent for o in run["decision"].get("orders", [])]  # type: ignore[union-attr]
    return {"status": "ok", "days": len(recent), "buy_orders": sum(1 for o in orders if o.get("side") == "buy"),
            "sell_orders": sum(1 for o in orders if o.get("side") == "sell")}


def build_diagnostics(decisions: Path, database: Path, as_of: str, tolerance: Decimal = DEFAULT_TOLERANCE) -> Dict[str, object]:
    """全部指標一次算好；沒有任何決策 run 時每項都是 unavailable，不丟錯。"""
    # 時間一致：只看 as_of 當時已存在的決策（cutoff 不晚於 as_of），重建過去的簡報也不會混入後來的委託與姿態。
    as_of_time = parse_aware_time(as_of, "as_of", error=CalibrationError)
    runs = [run for run in load_runs(decisions)
            if parse_aware_time(run["decision_cutoff"], "decision_cutoff", error=CalibrationError) <= as_of_time]
    book = OfficialPriceBook(Path(database), as_of)
    body: Dict[str, object] = {"schema_version": DIAGNOSTICS_VERSION, "run_count": len(runs)}
    scored = _analyst_and_sell(runs, book) if runs else {"analyst": {}, "sell": {}, "decision_days": 0}
    body["decision_days"] = scored["decision_days"]
    body["analyst"] = scored["analyst"]
    body["sell"] = scored["sell"]
    body["turnover"] = _turnover(runs, book)
    body.update(_review_and_stance(runs, book))
    body["benchmark"] = {"status": "unavailable", "reason": UNAVAILABLE_BENCH}
    body["week"] = week_activity(runs, book) if runs else {"status": "unavailable", "reason": "沒有可用的決策日"}
    chosen = _latest_per_entry_day(runs, book)[0] if runs else {}
    body["thesis"] = numeric_view(build_thesis_tracking(chosen, book, tolerance))
    return body
