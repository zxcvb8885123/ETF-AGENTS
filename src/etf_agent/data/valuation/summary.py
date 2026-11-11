"""估值收集的稽核彙總（唯讀）：逐資料日整理已保存的 run，不重新抓取、不連外部端點。

每個 run 先以不可變保存的 manifest 與雜湊驗證（任何不符即整批拒絕），再讀 ``audit.json`` 與 ``records.json`` 並互相核對
（資料日、要求／相符／拒絕檔數與原因分佈）。同一資料日有多個 run 時，採用「相符檔數最多、並列取較晚」的一次；
所有 run 仍逐一列出。「連續全部相符的資料日數」只數已收集的資料日（從最新一天往回、遇到第一個非全部相符即停），
沒有交易日曆可對照，所以中間沒收集的日子偵測不到。

只描述已保存的稽核結果，不是績效，也不是對 LLM 判斷的前瞻驗證（證據等級 replay_reference_only）；
不進決策、簡報或報告。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Dict, List, Mapping

from etf_agent.core.run_layout import list_run_dirs

from .contracts import ValuationError
from .repository import valuation_store

SUMMARY_VERSION = "valuation_audit_summary.v1"
EVIDENCE_GRADE = "replay_reference_only"
NOTICE = "僅供參考：這是已保存的稽核結果彙總，不構成對 LLM 判斷的前瞻驗證，也不代表估值可用於決策。"
NO_RUNS = "no_valuation_runs"
OK = "ok"
ADOPTION_RULE = "同一資料日取相符檔數最多的 run，並列取 run_id 較晚者；所有 run 仍逐一列出"
CALENDAR_BASIS = "collected_data_dates_only"


def _read(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValuationError("無法讀取 %s" % path) from error
    if not isinstance(payload, Mapping):
        raise ValuationError("%s 必須是物件" % path.name)
    return payload


def _count(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValuationError("%s 必須是非負整數" % label)
    return value


def _load_run(root: Path, run_dir: Path) -> Dict[str, object]:
    run_id = run_dir.name
    valuation_store(root).verify(run_id)
    audit = _read(run_dir / "audit.json")
    body = _read(run_dir / "records.json")
    try:
        data_date = date.fromisoformat(str(audit.get("data_date"))).isoformat()
    except ValueError as error:
        raise ValuationError("%s 的 audit.data_date 不是合法日期" % run_id) from error
    if body.get("data_date") != data_date:
        raise ValuationError("%s 的 records 與 audit 資料日不一致" % run_id)
    records = body.get("records")
    if not isinstance(records, list):
        raise ValuationError("%s 的 records 必須是陣列" % run_id)
    requested, matched, rejected = (_count(audit.get(key), "%s audit.%s" % (run_id, key)) for key in ("requested", "matched", "rejected"))
    rejected_symbols: List[Dict[str, object]] = []
    reasons: Counter = Counter()
    seen = set()
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(record.get("code"), str) or record.get("data_date") != data_date:
            raise ValuationError("%s 的逐檔紀錄欄位不合法或資料日不一致" % run_id)
        code = record["code"]
        if code in seen:
            raise ValuationError("%s 的 records 重複出現 %s" % (run_id, code))
        seen.add(code)
        status = record.get("status")
        if status == "matched":
            continue
        if status != "rejected" or not isinstance(record.get("reject_reason"), str):
            raise ValuationError("%s 的 %s 狀態或拒絕原因不合法" % (run_id, code))
        reasons[record["reject_reason"]] += 1
        entry: Dict[str, object] = {"code": code, "reject_reason": record["reject_reason"]}
        if record.get("mismatched_metrics") is not None:
            entry["mismatched_metrics"] = list(record["mismatched_metrics"])
        rejected_symbols.append(entry)
    if (len(records), len(records) - len(rejected_symbols), len(rejected_symbols)) != (requested, matched, rejected):
        raise ValuationError("%s 的 audit 檔數與 records 不一致" % run_id)
    if dict(sorted(reasons.items())) != audit.get("reject_reasons"):
        raise ValuationError("%s 的 audit.reject_reasons 與 records 不一致" % run_id)
    return {
        "run_id": run_id, "fetch_day": run_dir.parent.name, "data_date": data_date, "requested": requested, "matched": matched,
        "rejected": rejected, "reject_reasons": dict(sorted(reasons.items())),
        "rejected_symbols": sorted(rejected_symbols, key=lambda item: str(item["code"])),
    }


def summarize_valuation_audits(root: Path) -> Dict[str, object]:
    """逐資料日彙總 ``root`` 底下全部估值 run；任何一份 run 不合格即拒絕整批，沒有 run 時回報 ``no_valuation_runs``。"""
    base = {"report_version": SUMMARY_VERSION, "evidence_grade": EVIDENCE_GRADE, "notice": NOTICE,
            "adoption_rule": ADOPTION_RULE, "calendar_basis": CALENDAR_BASIS}
    runs = [_load_run(Path(root), run_dir) for run_dir in list_run_dirs(Path(root))]
    if not runs:
        return {**base, "status": NO_RUNS, "run_count": 0, "days": [], "consecutive_all_matched_days": 0}
    by_date: Dict[str, List[Dict[str, object]]] = {}
    for run in runs:
        by_date.setdefault(str(run["data_date"]), []).append(run)
    days: List[Dict[str, object]] = []
    for data_date in sorted(by_date):
        day_runs = sorted(by_date[data_date], key=lambda run: str(run["run_id"]))
        adopted = max(day_runs, key=lambda run: (run["matched"], run["run_id"]))
        days.append({
            "data_date": data_date,
            "adopted_run_id": adopted["run_id"],
            "requested": adopted["requested"], "matched": adopted["matched"], "rejected": adopted["rejected"],
            "reject_reasons": adopted["reject_reasons"], "rejected_symbols": adopted["rejected_symbols"],
            "all_matched": adopted["requested"] > 0 and adopted["rejected"] == 0,
            "runs": [{key: run[key] for key in ("run_id", "fetch_day", "requested", "matched", "rejected", "reject_reasons")} for run in day_runs],
        })
    streak = 0
    for day in reversed(days):
        if not day["all_matched"]:
            break
        streak += 1
    return {**base, "status": OK, "run_count": len(runs), "days": days, "consecutive_all_matched_days": streak}


__all__ = ["SUMMARY_VERSION", "summarize_valuation_audits"]
