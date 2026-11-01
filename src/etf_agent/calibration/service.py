"""記錄已封存的決策並在價格到齊後對答案。純旁觀：不寫入 Decision run、不影響任何決策。"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from etf_agent.core import parse_aware_time
from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.core.run_layout import list_run_dirs

from .contracts import (
    CALENDAR_BASIS,
    CONVICTION_SCORE,
    HORIZONS,
    MIN_DAY_COVERAGE,
    MIN_DECISION_DAYS,
    SCHEMA_VERSION,
    SIGNAL_INTENTS,
    STRENGTH_SCORE,
    CalibrationError,
    require_mapping,
    require_sequence,
)
from .prices import OfficialPriceBook
from .repository import CalibrationRepository
from .tools import forward_return, format_decimal, group_summary, mean, rank_ic


def _read(path: Path) -> Mapping[str, object]:
    try:
        return require_mapping(json.loads(path.read_text(encoding="utf-8")), path.name)
    except (OSError, json.JSONDecodeError) as error:
        raise CalibrationError("無法讀取 %s" % path) from error


def build_observation(decision_run_dir: Path) -> Mapping[str, object]:
    """讀取（並先驗證）一份已封存的 Decision run，整理成觀測。唯讀。"""
    run_dir = Path(decision_run_dir)
    manifest = _read(run_dir / "manifest.json")
    ImmutableRunStore(run_dir.parent, schema_version=str(manifest.get("schema_version")), error=CalibrationError).verify(run_dir.name)
    intent = _read(run_dir / "intent.json")
    debate = _read(run_dir / "debate.json")
    if intent.get("status") != "completed":
        raise CalibrationError("交易意圖尚未完成：%s" % run_dir.name)
    strengths: Dict[str, Dict[str, str]] = {}
    for packet in require_sequence(debate.get("packets"), "debate.packets"):
        packet = require_mapping(packet, "debate.packets[]")
        role = packet.get("role")
        if role not in ("bull", "bear"):
            raise CalibrationError("debate packet 角色必須是 bull 或 bear")
        for item in require_sequence(packet.get("items"), "debate.packets[].items"):
            item = require_mapping(item, "debate.packets[].items[]")
            strengths.setdefault(str(item["symbol"]), {})[str(role)] = str(item["strength"])
    items = []
    for item in require_sequence(intent.get("items"), "intent.items"):
        item = require_mapping(item, "intent.items[]")
        symbol = str(item["symbol"])
        sides = strengths.get(symbol, {})
        if set(sides) != {"bull", "bear"} or any(value not in STRENGTH_SCORE for value in sides.values()):
            raise CalibrationError("%s 缺少完整的多空強度" % symbol)
        conviction = item.get("conviction")
        if conviction is not None and conviction not in CONVICTION_SCORE:
            raise CalibrationError("%s 的 conviction 不合法" % symbol)
        items.append({
            "symbol": symbol, "intent": str(item["intent"]), "conviction": conviction,
            "bull_strength": sides["bull"], "bear_strength": sides["bear"],
        })
    if not items:
        raise CalibrationError("交易意圖沒有任何股票")
    return {
        "schema_version": SCHEMA_VERSION,
        "observation_id": "calibration-observation:%s" % run_dir.name,
        "decision_run_id": run_dir.name,
        "decision_id": intent["decision_id"],
        "snapshot_id": intent["snapshot_id"],
        "decision_cutoff": intent["decision_cutoff"],
        "decision_manifest_sha256": manifest["manifest_sha256"],
        "items": sorted(items, key=lambda entry: entry["symbol"]),
    }


def record_day(decision_run_dir: Path, repository: CalibrationRepository) -> Path:
    observation = build_observation(decision_run_dir)
    return repository.save("calibration-%s" % Path(decision_run_dir).name, {"observation": observation})


def record_all(decisions_root: Path, repository: CalibrationRepository) -> Dict[str, List[object]]:
    """記錄所有可記錄的 Decision run；不合格的只回報原因，不中斷。"""
    recorded: List[str] = []
    skipped: List[Dict[str, str]] = []
    for run_dir in list_run_dirs(Path(decisions_root)):
        try:
            record_day(run_dir, repository)
            recorded.append(run_dir.name)
        except CalibrationError as error:
            skipped.append({"decision_run_id": run_dir.name, "reason": str(error)})
    return {"recorded": recorded, "skipped": skipped}


def latest_per_entry_day(observations, book: OfficialPriceBook):
    """同一個成交日只留 cutoff 最晚的一份，避免被取代的舊決策重複計算。

    回傳 ``(chosen, unresolved, superseded)``：``chosen`` 為成交日 → 觀測；``unresolved`` 是 cutoff 後還沒有
    官方交易日的 run；``superseded`` 是成交日 → 被較晚 cutoff 取代的 run_id 清單（依 cutoff 排序）。
    """
    chosen: Dict[str, Mapping[str, object]] = {}
    unresolved: List[str] = []
    grouped: Dict[str, List[Mapping[str, object]]] = {}
    for observation in observations:
        entry_day = book.execution_date(str(observation["decision_cutoff"]))
        if entry_day is None:
            unresolved.append(str(observation["decision_run_id"]))
            continue
        grouped.setdefault(entry_day, []).append(observation)
    superseded: Dict[str, List[str]] = {}

    def cutoff_of(item: Mapping[str, object]):
        return parse_aware_time(item["decision_cutoff"], "decision_cutoff", error=CalibrationError)

    for entry_day, group in grouped.items():
        latest = group[0]
        for item in group[1:]:
            if cutoff_of(item) > cutoff_of(latest):  # 同時刻的保留先讀到的那份
                latest = item
        chosen[entry_day] = latest
        superseded[entry_day] = [str(item["decision_run_id"]) for item in sorted(group, key=cutoff_of) if item is not latest]
    return chosen, unresolved, superseded


def _latest_per_entry_day(observations, book: OfficialPriceBook):
    """舊名；只回傳 (chosen, unresolved)。"""
    chosen, unresolved, _ = latest_per_entry_day(observations, book)
    return chosen, unresolved


def day_returns(observation, entry_day: str, horizon: int, book: OfficialPriceBook) -> Dict[str, object]:
    """單一決策日在某個 horizon 的逐檔報酬與交易池平均。

    回傳 ``{"status", "rows", "counts", "exit_day", "pool_mean"}``：價格未到齊、互相衝突或覆蓋不足時 ``rows`` 為 None、
    ``status`` 說明原因；完整時每列含 ``return`` 與對交易池平均的 ``excess``。
    """
    result: Dict[str, object] = {"status": None, "rows": None, "counts": {}, "exit_day": None, "pool_mean": None}
    days = book.trading_days(entry_day)
    if not days or days[0] != entry_day:
        return dict(result, status="entry_price_pending")
    if len(days) <= horizon:
        return dict(result, status="exit_price_pending")
    exit_day = days[horizon]
    result["exit_day"] = exit_day
    entry, exit_ = book.vwap(entry_day), book.vwap(exit_day)
    if entry is None or exit_ is None:
        return dict(result, status="price_conflict")
    skipped = book.ex_rights_symbols(entry_day, exit_day)
    rows, counts = [], {"no_price": 0, "ex_rights": 0}
    for item in observation["items"]:
        symbol = str(item["symbol"]).upper()
        if symbol in skipped:
            counts["ex_rights"] += 1
        elif entry.get(symbol) is None or exit_.get(symbol) is None:
            counts["no_price"] += 1
        else:
            rows.append({**item, "return": forward_return(entry[symbol], exit_[symbol])})
    result["counts"] = counts
    if Decimal(len(rows)) / Decimal(len(observation["items"])) < MIN_DAY_COVERAGE:
        return dict(result, status="coverage_too_low")
    pool = mean([row["return"] for row in rows])
    for row in rows:
        row["excess"] = row["return"] - pool
    return dict(result, status="complete", rows=rows, pool_mean=pool)


def _day_rows(observation, entry_day: str, horizon: int, book: OfficialPriceBook):
    """舊名；回傳 (rows 或 None, 狀態, 排除計數)。"""
    result = day_returns(observation, entry_day, horizon, book)
    return result["rows"], result["status"], result["counts"]


def _daily_ic(rows) -> Dict[str, Optional[object]]:
    signal = [row for row in rows if row["intent"] in SIGNAL_INTENTS and row["conviction"] is not None]
    return {
        "conviction": rank_ic([(Decimal(CONVICTION_SCORE[row["conviction"]]), row["excess"]) for row in signal]),
        "net_strength": rank_ic([
            (Decimal(STRENGTH_SCORE[row["bull_strength"]] - STRENGTH_SCORE[row["bear_strength"]]), row["excess"]) for row in signal
        ]),
    }


def evaluate(repository: CalibrationRepository, database: Path, as_of: str) -> Dict[str, object]:
    """以 ``as_of`` 前已抓到的官方均價對答案。完整決策日不足時只回報天數與筆數，不給任何平均或 IC。"""
    book = OfficialPriceBook(database, as_of)
    observations = repository.observations()
    chosen, unresolved = _latest_per_entry_day(observations, book)
    report: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION, "as_of": book.as_of, "calendar_basis": CALENDAR_BASIS,
        "price_basis": "official_vwap", "required_decision_days": MIN_DECISION_DAYS,
        "observation_count": len(observations), "decision_days": len(chosen),
        "entry_day_unresolved": sorted(unresolved), "horizons": {},
    }
    for horizon in HORIZONS:
        complete_rows: List[Mapping[str, object]] = []
        daily_ic: List[Dict[str, Optional[object]]] = []
        pending: Dict[str, str] = {}
        excluded = {"no_price": 0, "ex_rights": 0}
        complete_days = 0
        for entry_day, observation in sorted(chosen.items()):
            rows, status, counts = _day_rows(observation, entry_day, horizon, book)
            for key, value in counts.items():
                excluded[key] += value
            if rows is None:
                pending[entry_day] = status
                continue
            complete_days += 1
            complete_rows.extend(rows)
            daily_ic.append(_daily_ic(rows))
        entry: Dict[str, object] = {
            "complete_days": complete_days, "n_obs": len(complete_rows), "pending_days": pending, "excluded_obs": excluded,
        }
        if complete_days < MIN_DECISION_DAYS:
            entry["status"] = "insufficient_sample"
        else:
            entry["status"] = "ok"
            signal_rows = [row for row in complete_rows if row["intent"] in SIGNAL_INTENTS and row["conviction"] is not None]
            entry["by_conviction"] = group_summary(signal_rows, "conviction")
            entry["by_intent"] = group_summary(complete_rows, "intent")
            entry["rank_ic"] = {
                name: {
                    "mean": format_decimal(mean([item[name] for item in daily_ic if item[name] is not None])),
                    "n_days": sum(item[name] is not None for item in daily_ic),
                }
                for name in ("conviction", "net_strength")
            }
        report["horizons"][str(horizon)] = entry
    report["price_conflict_days"] = sorted(set(book.conflict_days))
    return report
