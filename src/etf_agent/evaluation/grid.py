"""回測格子：每筆決策一列（run × symbol），按評級分組統計兩種超額報酬，零 token。

- 只讀已封存、manifest 驗證通過的 Decision run（經 ``calibration.build_observation``）與官方成交均價
  （``calibration.prices.OfficialPriceBook``，只用 ``fetched_at`` 不晚於 ``as_of`` 的官方列），不呼叫 Agent、
  不碰決策庫、帳本或 D-Plan。
- 口徑沿用 ``calibration``：成交日 T 是 cutoff 後第一個有官方收盤的交易日，報酬 = T+h 均價 ÷ T 均價 − 1；
  同一成交日只採 cutoff 最晚的 run；窗口 (T, T+h] 內除權息的股票不計；有效報酬不到八成的決策日不給答案。
- alpha 並列兩種、不合成：``excess_vs_pool``（減同日交易池平均）與 ``excess_vs_benchmark``（減基準 ETF，預設
  0050.TW，同一 T／T+h 均價）。基準在 T 或 T+h 缺官方列（含抓取時間晚於 ``as_of``）時該日
  ``benchmark_status=unavailable``、窗口內除權息時 ``ex_rights``，池 alpha 照算、基準欄留空，不補值。
- 完整決策日少於 ``MIN_DECISION_DAYS`` 的 horizon 只給天數與筆數（``insufficient_sample``），不給平均與勝率；
  超額剛好為 0 的不算贏也不算輸。
- ``provenance_class`` 本輪一律 ``archived``：只收已封存 run，不往更早歷史重建（見 README 的下一步與風險）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.calibration.contracts import (
    CALENDAR_BASIS,
    HORIZONS,
    MIN_DAY_COVERAGE,
    MIN_DECISION_DAYS,
    SCHEMA_VERSION as CALIBRATION_SCHEMA_VERSION,
    SIGNAL_INTENTS,
    STRENGTH_SCORE,
    CalibrationError,
)
from etf_agent.calibration.prices import OfficialPriceBook
from etf_agent.calibration.service import build_observation, day_returns, latest_per_entry_day
from etf_agent.calibration.tools import format_decimal, forward_return, mean
from etf_agent.core import canonical_sha256, parse_aware_time
from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.core.run_layout import find_run_dir, list_run_dirs

from .contracts import DEFAULT_BENCHMARK_SYMBOL, GRID_SCHEMA_VERSION, REPLAY_EVIDENCE_GRADE, REPLAY_NOTICE, GridError
from .replay_tools import code_version
from .service import EVALUATION_ROOT


GRID_ROOT = EVALUATION_ROOT / "grids"
# 這些欄位不進 content_sha256，也不參與重算比對。
VOLATILE_FIELDS = ("grid_id", "generated_at", "content_sha256")
ENGINE_VERSIONS = {"grid": GRID_SCHEMA_VERSION, "calibration": CALIBRATION_SCHEMA_VERSION}
# 分組鍵：by_conviction 只算 buy／add 且有信心等級（與 calibration 的 by_conviction 同口徑），其餘含全部股票。
GROUP_KEYS = (
    ("by_conviction", "conviction"), ("by_intent", "intent"), ("by_bull_strength", "bull_strength"),
    ("by_bear_strength", "bear_strength"), ("by_net_strength", "net_strength"),
)
_DAY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
Log = Callable[[str], None]


class GridRepository(ImmutableRunStore):
    """格子的不可變保存：``artifacts/evaluation/grids/<grid_id>/``。"""

    def __init__(self, root: Path):
        super().__init__(Path(root), schema_version=GRID_SCHEMA_VERSION, error=GridError)


def _read_json(path: Path, label: str) -> Dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GridError("%s 無法讀取：%s" % (label, path)) from error
    if not isinstance(value, dict):
        raise GridError("%s 必須是 JSON 物件" % label)
    return value


def grid_body_sha256(body: Mapping[str, object]) -> str:
    return canonical_sha256({key: value for key, value in body.items() if key not in VOLATILE_FIELDS})


def _day(value: Optional[str], field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _DAY_PATTERN.fullmatch(value):
        raise GridError("%s 必須是 YYYY-MM-DD" % field)
    return value


# ---------------------------------------------------------------- observations
def collect_observations(decision_root: Path, run_ids: Optional[Sequence[str]] = None) -> Tuple[List[Mapping[str, object]], List[Dict[str, str]]]:
    """讀決策庫中的 Decision run 成觀測（含 manifest 驗證）。指定 run 時任何一份不合格即停止；
    未指定時不合格的只記錄原因、不納入。"""
    decision_root = Path(decision_root)
    if run_ids:
        directories = []
        for run_id in dict.fromkeys(str(item) for item in run_ids):
            found = find_run_dir(decision_root, run_id, GridError)
            if found is None:
                raise GridError("找不到 Decision run：%s" % run_id)
            directories.append(found)
    else:
        directories = list_run_dirs(decision_root)
    observations: List[Mapping[str, object]] = []
    skipped: List[Dict[str, str]] = []
    for run_dir in directories:
        try:
            if not (run_dir / "intent.json").is_file() or not (run_dir / "debate.json").is_file():
                raise CalibrationError("缺少 intent.json 或 debate.json")
            observations.append(build_observation(run_dir))
        except CalibrationError as error:
            if run_ids:
                raise GridError("Decision run %s 無法納入格子：%s" % (run_dir.name, error)) from error
            skipped.append({"run_id": run_dir.name, "reason": str(error)})
    if not observations:
        raise GridError("沒有可納入格子的 Decision run")
    return observations, skipped


def benchmark_return(book: OfficialPriceBook, symbol: str, entry_day: str, exit_day: str) -> Tuple[Optional[Decimal], str]:
    """基準 ETF 在 (T, T+h] 的報酬與狀態；缺 T 或 T+h 官方列（含抓取時間晚於 as_of）→ unavailable，窗口內除權息 → ex_rights。"""
    if symbol in book.ex_rights_symbols(entry_day, exit_day):
        return None, "ex_rights"
    entry, exit_ = book.vwap(entry_day), book.vwap(exit_day)
    if entry is None or exit_ is None:
        return None, "unavailable"
    start, end = entry.get(symbol), exit_.get(symbol)
    if start is None or end is None:
        return None, "unavailable"
    return forward_return(start, end), "available"


# ---------------------------------------------------------------- aggregation
def _beat(values: Sequence[Decimal]) -> Dict[str, object]:
    """贏過的比例：只算超額不為 0 的筆數，剛好 0 另記 ties。"""
    scored = [value for value in values if value != 0]
    wins = sum(1 for value in scored if value > 0)
    return {"n": len(scored), "wins": wins, "ties": len(values) - len(scored),
            "rate": format_decimal(Decimal(wins) / Decimal(len(scored))) if scored else None}


def _group_sort_key(name: str):
    return (0, int(name)) if re.fullmatch(r"-?\d+", name) else (1, name)


def group_alpha(rows: Sequence[Mapping[str, object]], key: str) -> Dict[str, Dict[str, object]]:
    """依 key 分組：筆數、平均報酬、對交易池與對基準的平均超額與贏過比例。基準缺列的筆只進池欄。"""
    groups: Dict[str, List[Mapping[str, object]]] = {}
    for row in rows:
        groups.setdefault(str(row[key]), []).append(row)
    result: Dict[str, Dict[str, object]] = {}
    for name in sorted(groups, key=_group_sort_key):
        items = groups[name]
        pool = [item["excess_vs_pool"] for item in items]
        bench = [item["excess_vs_benchmark"] for item in items if item["excess_vs_benchmark"] is not None]
        result[name] = {
            "n_obs": len(items),
            "mean_return": format_decimal(mean([item["return"] for item in items])),
            "mean_excess_vs_pool": format_decimal(mean(pool)),
            "beat_pool": _beat(pool),
            "n_obs_benchmark": len(bench),
            "mean_excess_vs_benchmark": format_decimal(mean(bench)),
            "beat_benchmark": _beat(bench),
        }
    return result


def _cell(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else format_decimal(value)


# ---------------------------------------------------------------- whole grid
def build_grid(
    root: Path, *, decision_root: Path, database: Path, as_of: str, start: Optional[str] = None, end: Optional[str] = None,
    run_ids: Optional[Sequence[str]] = None, benchmark: str = DEFAULT_BENCHMARK_SYMBOL, log: Log = lambda _: None,
) -> Dict[str, object]:
    """建立格子主體（不含 grid_id／generated_at）。不寫檔、不呼叫 Agent。

    ``start``／``end`` 篩的是成交日 T。``inputs.run_ids`` 記錄實際納入（含同日被取代）的 run，重算驗證以它重建，
    決策庫之後新增 run 不影響既有格子。
    """
    root, decision_root, database = Path(root), Path(decision_root), Path(database)
    start, end = _day(start, "start"), _day(end, "end")
    if start is not None and end is not None and start > end:
        raise GridError("start 不得晚於 end")
    if not isinstance(benchmark, str) or not benchmark.strip():
        raise GridError("benchmark 必須是股票代號")
    benchmark = benchmark.strip().upper()
    try:
        book = OfficialPriceBook(database, as_of)
        observations, skipped = collect_observations(decision_root, run_ids)
        chosen, unresolved, superseded = latest_per_entry_day(observations, book)
    except CalibrationError as error:
        raise GridError(str(error)) from error
    selected = {day: chosen[day] for day in sorted(chosen) if (start is None or day >= start) and (end is None or day <= end)}
    if not selected:
        raise GridError("範圍內沒有可納入格子的決策日")
    out_of_range = sorted(set(chosen) - set(selected))
    log("[格子] %d 個決策日（%s ～ %s），基準 %s，as_of %s" % (len(selected), min(selected), max(selected), benchmark, book.as_of))

    rows: List[Dict[str, object]] = []
    days: List[Dict[str, object]] = []
    per_horizon: Dict[int, Dict[str, object]] = {
        horizon: {"complete_rows": [], "complete_days": 0, "pending": {}, "excluded": {"no_price": 0, "ex_rights": 0},
                  "benchmark_days": {"available": 0, "unavailable": 0, "ex_rights": 0}}
        for horizon in HORIZONS
    }
    for entry_day, observation in selected.items():
        run_id = str(observation["decision_run_id"])
        day_record: Dict[str, object] = {
            "entry_day": entry_day, "run_id": run_id, "decision_cutoff": observation["decision_cutoff"],
            "superseded_run_ids": superseded.get(entry_day, []), "n_items": len(observation["items"]), "horizons": {},
        }
        base_rows: Dict[str, Dict[str, object]] = {}
        for item in observation["items"]:
            symbol = str(item["symbol"]).upper()
            base_rows[symbol] = {
                "entry_day": entry_day, "run_id": run_id, "symbol": symbol, "intent": item["intent"], "conviction": item["conviction"],
                "bull_strength": item["bull_strength"], "bear_strength": item["bear_strength"],
                "net_strength": STRENGTH_SCORE[item["bull_strength"]] - STRENGTH_SCORE[item["bear_strength"]], "horizons": {},
            }
        for horizon in HORIZONS:
            bucket = per_horizon[horizon]
            try:
                answer = day_returns(observation, entry_day, horizon, book)
            except CalibrationError as error:
                raise GridError(str(error)) from error
            status = str(answer["status"])
            for key, value in answer["counts"].items():
                bucket["excluded"][key] += value
            day_cell: Dict[str, object] = {"status": status, "exit_day": answer["exit_day"], "pool_mean": _cell(answer["pool_mean"]),
                                           "benchmark_status": None, "benchmark_return": None}
            if status != "complete":
                bucket["pending"][entry_day] = status
                for row in base_rows.values():
                    row["horizons"][str(horizon)] = {"status": status}
                day_record["horizons"][str(horizon)] = day_cell
                continue
            exit_day = str(answer["exit_day"])
            bench_value, bench_status = benchmark_return(book, benchmark, entry_day, exit_day)
            bucket["benchmark_days"][bench_status] += 1
            bucket["complete_days"] += 1
            day_cell.update(benchmark_status=bench_status, benchmark_return=_cell(bench_value))
            ex_rights = book.ex_rights_symbols(entry_day, exit_day)
            priced = {str(row["symbol"]).upper(): row for row in answer["rows"]}
            for symbol, row in base_rows.items():
                scored = priced.get(symbol)
                if scored is None:
                    row["horizons"][str(horizon)] = {"status": "ex_rights" if symbol in ex_rights else "no_price"}
                    continue
                excess_bench = None if bench_value is None else scored["return"] - bench_value
                row["horizons"][str(horizon)] = {
                    "status": "complete", "exit_day": exit_day, "return": _cell(scored["return"]),
                    "excess_vs_pool": _cell(scored["excess"]), "excess_vs_benchmark": _cell(excess_bench), "benchmark_status": bench_status,
                }
                bucket["complete_rows"].append({**{key: row[key] for key in ("entry_day", "run_id", "symbol", "intent", "conviction",
                                                                             "bull_strength", "bear_strength", "net_strength")},
                                                "return": scored["return"], "excess_vs_pool": scored["excess"], "excess_vs_benchmark": excess_bench})
            day_record["horizons"][str(horizon)] = day_cell
        rows.extend(base_rows[symbol] for symbol in sorted(base_rows))
        days.append(day_record)

    horizons: Dict[str, object] = {}
    for horizon in HORIZONS:
        bucket = per_horizon[horizon]
        complete_rows = bucket["complete_rows"]
        entry: Dict[str, object] = {
            "complete_days": bucket["complete_days"], "n_obs": len(complete_rows), "pending_days": bucket["pending"],
            "excluded_obs": bucket["excluded"], "benchmark_days": bucket["benchmark_days"],
        }
        if bucket["complete_days"] < MIN_DECISION_DAYS:
            entry["status"] = "insufficient_sample"
        else:
            entry["status"] = "ok"
            for name, key in GROUP_KEYS:
                scope = ([row for row in complete_rows if row["intent"] in SIGNAL_INTENTS and row["conviction"] is not None]
                         if key == "conviction" else complete_rows)
                entry[name] = group_alpha(scope, key)
        horizons[str(horizon)] = entry

    # 實際納入的 run（含同日被取代的），依 cutoff 排序；重算驗證以此重建，決策庫之後新增 run 不影響既有格子。
    cutoff_by_run = {str(obs["decision_run_id"]): parse_aware_time(obs["decision_cutoff"], "decision_cutoff", error=GridError)
                     for obs in observations}
    pinned_all = sorted({str(selected[day]["decision_run_id"]) for day in selected}
                        | {run_id for day in selected for run_id in superseded.get(day, [])}, key=lambda run_id: cutoff_by_run[run_id])
    manifest_by_run = {str(obs["decision_run_id"]): obs["decision_manifest_sha256"] for obs in observations}
    body: Dict[str, object] = {
        "schema_version": GRID_SCHEMA_VERSION,
        "status": "completed",
        # 格子只是把已封存決策拿來對之後的價格，不是對 LLM 判斷的前瞻驗證（見 contracts）。
        "evidence_grade": REPLAY_EVIDENCE_GRADE,
        "notice": REPLAY_NOTICE,
        "inputs": {
            "decision_root": str(decision_root), "database": str(database), "as_of": book.as_of, "benchmark": benchmark,
            "run_ids": pinned_all,
            "requested": {"start": start, "end": end, "run_ids": None if not run_ids else [str(item) for item in run_ids]},
        },
        "provenance": {
            "provenance_class": "archived",
            "price_basis": "official_vwap", "calendar_basis": CALENDAR_BASIS,
            "required_decision_days": MIN_DECISION_DAYS, "min_day_coverage": str(MIN_DAY_COVERAGE),
            "runs": [{"run_id": str(selected[day]["decision_run_id"]), "decision_cutoff": selected[day]["decision_cutoff"], "entry_day": day,
                      "manifest_sha256": manifest_by_run[str(selected[day]["decision_run_id"])], "superseded_run_ids": superseded.get(day, [])}
                     for day in selected],
            "selection": {"entry_day_unresolved": sorted(unresolved), "out_of_range_entry_days": out_of_range, "skipped": skipped},
            "code_version": code_version(root, ENGINE_VERSIONS),
        },
        "days": days,
        "rows": rows,
        "horizons": horizons,
        "price_conflict_days": sorted(set(book.conflict_days)),
        "limitations": [
            "每格只是一筆決策之後的實際報酬，不是歸因；贏過比例高不代表可重複。",
            "5 日與 20 日窗口在相鄰決策日高度重疊，等效獨立樣本遠少於筆數；不要對平均做一般 t 檢定。",
            "基準欄只在基準 ETF 於 T 與 T+h 都有 as_of 前抓到的官方列時才有值；缺列或除權息的日子留空，不補值。",
            "只收已封存、manifest 驗證通過的 Decision run（provenance_class=archived）；不往更早歷史重建。",
        ],
    }
    body["content_sha256"] = grid_body_sha256(body)
    return body


# ---------------------------------------------------------------- persistence
def save_grid(root: Path, body: Mapping[str, object], grid_id: Optional[str] = None) -> Tuple[str, Path]:
    """封存格子（不可變 run），並在旁邊寫同源 HTML（衍生品，不進 manifest）。"""
    from .html_report import render_grid_html

    grid_id = grid_id or "grid-%s" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = dict(body)
    report["grid_id"] = grid_id
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    repository = GridRepository(Path(root) / GRID_ROOT)
    if repository.run_dir(grid_id) is not None:
        raise GridError("格子 %s 已存在，不覆寫" % grid_id)
    page = render_grid_html(report)  # 先渲染再封存：渲染失敗時不留下封存好的 run，同一個 ID 才能重跑
    run_dir = repository.save(grid_id, {"grid": report})
    (run_dir.parent / ("%s.html" % grid_id)).write_text(page, encoding="utf-8")
    return grid_id, run_dir


def read_grid(root: Path, grid_id: str) -> Tuple[Dict[str, object], Path]:
    repository = GridRepository(Path(root) / GRID_ROOT)
    run_dir = repository.run_dir(grid_id)
    if run_dir is None:
        raise GridError("找不到格子：%s" % grid_id)
    return _read_json(run_dir / "grid.json", "格子"), run_dir


# 重算必須完全一致的欄位；inputs 不比路徑與原始要求，provenance 的程式版本只警告。
REBUILT_FIELDS = ("schema_version", "status", "inputs", "days", "rows", "horizons", "price_conflict_days", "limitations")
REBUILT_PROVENANCE = ("provenance_class", "price_basis", "calendar_basis", "required_decision_days", "min_day_coverage", "runs")
_PATH_INPUTS = {"decision_root", "database", "requested"}


def verify_grid(
    root: Path, grid_id: str, *, decision_root: Optional[Path] = None, database: Optional[Path] = None, log: Log = lambda _: None,
) -> Dict[str, object]:
    """回傳 {valid, errors, warnings}：封存雜湊、content_sha256，再以記錄的輸入重建逐欄比對。任何差異視為竄改或輸入改變。"""
    errors: List[str] = []
    warnings: List[str] = []
    try:
        GridRepository(Path(root) / GRID_ROOT).verify(grid_id)
    except GridError as error:
        return {"valid": False, "errors": ["格子封存驗證失敗：%s" % error], "warnings": warnings}
    grid, _ = read_grid(root, grid_id)
    if grid.get("content_sha256") != grid_body_sha256(grid):
        return {"valid": False, "errors": ["格子 content_sha256 與內容不一致"], "warnings": warnings}
    inputs = grid.get("inputs")
    if not isinstance(inputs, Mapping) or not isinstance(grid.get("provenance"), Mapping):
        return {"valid": False, "errors": ["格子缺少 inputs 或 provenance"], "warnings": warnings}
    try:
        rebuilt = build_grid(
            root, decision_root=Path(decision_root or inputs["decision_root"]), database=Path(database or inputs["database"]),
            as_of=str(inputs["as_of"]), run_ids=list(inputs["run_ids"]), benchmark=str(inputs["benchmark"]), log=log,
        )
    except (GridError, KeyError, TypeError) as error:
        return {"valid": False, "errors": ["重算失敗：%s" % error], "warnings": warnings}
    for field in REBUILT_FIELDS:
        left, right = grid.get(field), rebuilt.get(field)
        if field == "inputs":
            left = {k: v for k, v in dict(left).items() if k not in _PATH_INPUTS}
            right = {k: v for k, v in dict(right).items() if k not in _PATH_INPUTS}
        if canonical_sha256(left) != canonical_sha256(right):
            errors.append("格子的 %s 與重算不一致" % field)
    for field in REBUILT_PROVENANCE:
        if canonical_sha256(grid["provenance"].get(field)) != canonical_sha256(rebuilt["provenance"].get(field)):
            errors.append("格子的 provenance.%s 與重算不一致" % field)
    if canonical_sha256(grid["provenance"].get("code_version")) != canonical_sha256(rebuilt["provenance"].get("code_version")):
        warnings.append("provenance.code_version 已改變（程式與建立格子時不同），重算結果仍一致" if not errors else "provenance.code_version 已改變")
    return {"valid": not errors, "errors": errors, "warnings": warnings}
