"""估值 V2：PER／PBR 在自身歷史區間的位置（``position_sizing.valuation_facts`` 版本閘門，背景、不可引用）。

設計見 ``docs/quality_dip_plan.md`` 第 2.6 節與第 10.5 節（實作紀錄與保守取捨）。閘門只由人設定、策略反思不得改；
沒有欄位的舊 Policy／舊 run：簡報、``team_inputs`` 與重建逐位元不變。

- 資料只讀估值 V1 已不可變保存的 run（``artifacts/valuation_collection``）：取 ``fetched_at``（= ``available_at``）不晚於
  ``decision_cutoff`` 的最新一個 run，先以 ``validate_valuation_run`` 依原始回應整份重建（manifest、雜湊、核對結果）；
  run 不合格、資料日晚於 Snapshot 最近交易日、或沒有 run 時整份 ``unavailable``（記 ``data_gaps``），不補值、不往前找舊 run。
- 只用逐檔核對相符（``status: matched``）且授權登記允許「背景進簡報」用途的紀錄。V1 的紀錄授權範圍是
  ``valuation_v1_collection_and_audit_only``（只收集與稽核），**不在允許清單內**：即使閘門開啟，每檔都是 ``LICENSE_SCOPE_NOT_APPROVED``。
  要開放須使用者核准新的用途範圍並改 V1 契約（不能只改收集器常數：重建以當下常數比對，會讓既有 run 全部重建失敗；
  見 docs/quality_dip_plan.md 第 10.5 節）。程式不替使用者擴大授權範圍，也不回溯改寫舊 run。
- 只算 PER 與 PBR；殖利率的股利基準尚未確認，不進簡報。上櫃股（``.TWO``）端點尚未確認，一律不可用。
- 位置：三個寫死的視窗並列（近 250 個資料列、近 750 個資料列、全部可得歷史）；百分位＝視窗內「不高於當日值」的筆數 ÷ 樣本數
  （含當日，四捨五入到小數 4 位）。固定視窗的資料列不足時該視窗 ``insufficient_history``、不算數字；全部歷史至少 2 筆才算。
  只有資料日當天與官方核對過，之前的 FinMind 歷史一律標 ``unverified_history``。
- 只給位置，不給便宜或貴的結論；程式不設任何門檻，Agent 不輸出數字，Skill 不得寫便宜／低估。
- Finalizer 以同一個估值資料夾（由決策庫位置推得）與 ``source.run_id`` 整份重算 ``team_inputs.valuation_facts``；
  run 事後被改、或讀不到資料夾時重算結果不同，即拒絕。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core import canonical_sha256, parse_aware_time

from .fields import DecisionToolError, reject_unknown_fields

VALUATION_FACTS_V1 = "v1"
VALUATION_FACTS_FIELDS = {"version"}
VALUATION_FACTS_KEY = "valuation_facts"  # team_inputs 的選配鍵：與 Policy 閘門雙向同時存在
VALUATION_FACTS_SCHEMA_VERSION = "1.0"
STORE_DIRNAME = "valuation_collection"
METRICS = ("per", "pbr")  # 殖利率的股利基準尚未確認，不進簡報（docs/quality_dip_plan.md 第 2.6 節 (2)）
WINDOWS: Tuple[Tuple[str, Optional[int]], ...] = (("rows_250", 250), ("rows_750", 750), ("all", None))
MIN_ALL_SAMPLE = 2
# 允許「背景進簡報」的授權範圍。V1 收集器記的是 valuation_v1_collection_and_audit_only（只收集與稽核），不在清單內；
# 要讓資料可用，須使用者核准新的用途範圍並改 V1 契約讓紀錄帶新範圍（做法由人選，見計畫第 10.5 節）。
APPROVED_LICENSE_SCOPES = ("valuation_v2_background_brief",)
PERCENTILE_QUANTUM = Decimal("0.0001")
VALUE_QUANTUM = Decimal("0.01")
PERCENTILE_RULE = "percentile = 視窗內不高於當日值的筆數 ÷ 樣本數（含當日，四捨五入到小數 4 位）；視窗以資料列計，不是日曆日"
FACTS_FIELDS = {
    "schema_version", "version", "facts_id", "as_of", "data_status", "source", "metrics", "windows", "percentile_rule",
    "data_gaps", "note", "items",
}
NOTE = (
    "PER／PBR 在該股自身歷史區間的位置（程式算的背景事實，citable=false、沒有 evidence_id，不可引用）。三個視窗並列，同一個數字在不同"
    "視窗可能落在完全相反的位置；位置低不等於低於本來價值（盈餘可能正在下修），PER 下降也可能來自盈餘上修。不得寫便宜、低估或貴；"
    "要提到時必須寫明指標與視窗，例如「PER 位於近 250 個資料列的第 12 百分位」。資料日之前的歷史只推定口徑相同、沒有逐日核對。"
)


# ---------------------------------------------------------------- 閘門

def valuation_facts_gate(sizing: object) -> Optional[Mapping[str, object]]:
    gate = sizing.get("valuation_facts") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def validate_valuation_facts_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """閘門形狀：{version: v1}。沒有 Policy 層的前提（資料可用性在建事實時判定，不可用時整份 unavailable）。"""
    gate = sizing["valuation_facts"]
    prefix = "position_sizing.valuation_facts"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, VALUATION_FACTS_FIELDS, prefix, errors)
    if gate.get("version") != VALUATION_FACTS_V1:
        errors.append("%s.version 只允許 %s" % (prefix, VALUATION_FACTS_V1))


def valuation_root_for(decision_repository: Optional[Path]) -> Optional[Path]:
    """估值資料夾與決策庫同在 ``artifacts/`` 底下；pipeline 與 Finalizer 用同一個推法，讀到的才會是同一份資料。"""
    return None if decision_repository is None else Path(decision_repository).parent / STORE_DIRNAME


# ---------------------------------------------------------------- 讀取 V1 的 run

def _run_time(run_id: str) -> Optional[str]:
    """``valuation-20261110T130046Z`` → ``2026-11-10T13:00:46+00:00``；格式不符回 None（不是估值 run）。"""
    stamp = run_id[len("valuation-"):] if run_id.startswith("valuation-") else ""
    if len(stamp) != 16 or stamp[8] != "T" or not stamp.endswith("Z"):
        return None
    return "%s-%s-%sT%s:%s:%s+00:00" % (stamp[0:4], stamp[4:6], stamp[6:8], stamp[9:11], stamp[11:13], stamp[13:15])


def _load(run_dir: Path, name: str) -> Mapping[str, object]:
    payload = json.loads((run_dir / ("%s.json" % name)).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("%s 必須是物件" % name)
    return payload


def _select_run(root: Optional[Path], cutoff, pinned: Optional[str], gaps: List[str]) -> Optional[Dict[str, object]]:
    """選 run 並以原始回應整份重建；不合格回 None 並記缺口（不往前找舊 run，避免掩蓋事後改寫）。"""
    from etf_agent.core.run_layout import find_run_dir, list_run_dirs
    from etf_agent.data.valuation import ValuationError, validate_valuation_run

    if root is None:
        gaps.append("VALUATION_STORE_NOT_CONFIGURED")
        return None
    if not Path(root).is_dir():
        gaps.append("NO_VALUATION_RUN")
        return None
    if pinned is not None:
        run_dir = find_run_dir(Path(root), pinned, ValuationError)
        if run_dir is None:
            gaps.append("VALUATION_RUN_MISSING:%s" % pinned)
            return None
        candidates = [run_dir]
    else:
        candidates = []
        for run_dir in sorted(list_run_dirs(Path(root)), key=lambda path: path.name, reverse=True):
            moment = _run_time(run_dir.name)
            if moment is not None and parse_aware_time(moment, "run_id") <= cutoff:
                candidates.append(run_dir)
                break
        if not candidates:
            gaps.append("NO_VALUATION_RUN")
            return None
    run_dir = candidates[0]
    try:
        validate_valuation_run(Path(root), run_dir.name)
        official, records, history = _load(run_dir, "official_raw"), _load(run_dir, "records"), _load(run_dir, "history")
        manifest = _load(run_dir, "manifest")
        fetched_at = str(official["fetched_at"])
        if parse_aware_time(fetched_at, "fetched_at") > cutoff:
            gaps.append("VALUATION_FETCHED_AFTER_CUTOFF:%s" % run_dir.name)
            return None
    except (ValuationError, OSError, KeyError, TypeError, ValueError) as error:
        gaps.append("VALUATION_RUN_INVALID:%s:%s" % (run_dir.name, type(error).__name__))
        return None
    return {
        "run_id": run_dir.name, "fetched_at": fetched_at, "data_date": str(records["data_date"]),
        "manifest_sha256": str(manifest.get("manifest_sha256")), "records": records["records"], "history": history["history"],
    }


# ---------------------------------------------------------------- 位置計算

def _q(value: Decimal, quantum: Decimal) -> str:
    return str(value.quantize(quantum, rounding=ROUND_HALF_UP))


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _window(rows: Sequence[Tuple[str, Optional[Decimal]]], current: Decimal, required: Optional[int]) -> Dict[str, object]:
    """``rows`` 是依日期排序、止於資料日的 (日期, 值)；固定視窗取最後 ``required`` 列，資料列不足即 insufficient_history。"""
    selected = list(rows if required is None else rows[-required:])
    if required is not None and len(rows) < required:
        return {"status": "insufficient_history", "required_rows": required, "available_rows": len(rows)}
    values = [value for _, value in selected if value is not None]
    if len(values) < MIN_ALL_SAMPLE:
        return {"status": "insufficient_history", "required_rows": required, "available_rows": len(rows)}
    at_or_below = sum(1 for value in values if value <= current)
    return {
        "status": "available", "rows": len(selected), "sample_size": len(values),
        "start": selected[0][0], "end": selected[-1][0],
        "min": _q(min(values), VALUE_QUANTUM), "median": _q(_median(values), VALUE_QUANTUM), "max": _q(max(values), VALUE_QUANTUM),
        "percentile": _q(Decimal(at_or_below) / Decimal(len(values)), PERCENTILE_QUANTUM),
    }


def _metric_position(metric: str, record: Mapping[str, object], rows: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    raw = (record.get("values") or {}).get(metric)  # type: ignore[union-attr]
    if raw is None:
        reason = (record.get("null_reasons") or {}).get(metric)  # type: ignore[union-attr]
        return {"value": None, "null_reason": reason or "NOT_PROVIDED", "windows": None}
    current = Decimal(str(raw))
    data_date = str(record["data_date"])
    series: List[Tuple[str, Optional[Decimal]]] = []
    for row in rows:
        value = (row.get("values") or {}).get(metric)  # type: ignore[union-attr]
        series.append((str(row["date"]), None if value is None else Decimal(str(value))))
    if not series or series[-1][0] != data_date or series[-1][1] != current:
        return {"value": str(raw), "null_reason": "HISTORY_NOT_ALIGNED", "windows": None}
    return {"value": str(raw), "null_reason": None, "windows": {name: _window(series, current, size) for name, size in WINDOWS}}


def _item(symbol: str, run: Optional[Mapping[str, object]], by_code, history_by_code, latest: date) -> Dict[str, object]:
    base: Dict[str, object] = {"symbol": symbol, "status": "unavailable", "reason": None, "data_date": None, "metrics": None}
    if symbol.endswith(".TWO"):
        return {**base, "reason": "OTC_NOT_COLLECTED"}
    if run is None:
        return {**base, "reason": "NO_VALID_RUN"}
    record = by_code.get(symbol.split(".")[0])
    if record is None:
        return {**base, "reason": "NOT_COLLECTED"}
    base["data_date"] = record.get("data_date")
    if record.get("status") != "matched":
        return {**base, "reason": str(record.get("reject_reason") or "REJECTED")}
    if record.get("license_status") != "approved":
        return {**base, "reason": "LICENSE_NOT_APPROVED"}
    if record.get("license_scope") not in APPROVED_LICENSE_SCOPES:
        return {**base, "reason": "LICENSE_SCOPE_NOT_APPROVED"}
    rows = sorted(
        (row for row in history_by_code.get(record["code"], []) if date.fromisoformat(str(row["date"])) <= latest
         and str(row["date"]) <= str(record["data_date"])),
        key=lambda row: str(row["date"]),
    )
    return {
        **base, "status": "available",
        "metrics": {metric: _metric_position(metric, record, rows) for metric in METRICS},
        "history_verification": "only_data_date_verified_against_official; earlier_rows_unverified_history",
    }


def build_valuation_facts(
    valuation_root: Optional[Path], bundle: Mapping[str, object], gate: Mapping[str, object], pinned_run_id: Optional[str] = None,
) -> Dict[str, object]:
    """全交易池的估值位置事實。缺資料、run 不合格或授權範圍不允許時降級為 unavailable，不拋錯、不補值。"""
    snapshot = bundle["snapshot"]
    cutoff = parse_aware_time(str(bundle["decision_cutoff"]), "decision_cutoff", error=DecisionToolError)
    latest = date.fromisoformat(str(snapshot["latest_trade_date"]))  # type: ignore[index]
    symbols = sorted(str(row["symbol"]).upper() for row in snapshot["latest_prices"])  # type: ignore[index]
    gaps: List[str] = []
    run = _select_run(valuation_root, cutoff, pinned_run_id, gaps)
    if run is not None and date.fromisoformat(str(run["data_date"])) > latest:
        gaps.append("VALUATION_DATA_DATE_AFTER_SNAPSHOT:%s" % run["data_date"])
        run = None
    by_code: Dict[str, Mapping[str, object]] = {}
    history_by_code: Dict[str, List[Mapping[str, object]]] = {}
    if run is not None:
        by_code = {str(record["code"]): record for record in run["records"] if isinstance(record, Mapping)}  # type: ignore[union-attr]
        for row in run["history"]:  # type: ignore[union-attr]
            if isinstance(row, Mapping):
                history_by_code.setdefault(str(row["code"]), []).append(row)
    items = [_item(symbol, run, by_code, history_by_code, latest) for symbol in symbols]
    if run is not None and not any(item["status"] == "available" for item in items):
        reasons = sorted({str(item["reason"]) for item in items})
        gaps.append("NO_USABLE_RECORD:%s" % ",".join(reasons))
    body: Dict[str, object] = {
        "schema_version": VALUATION_FACTS_SCHEMA_VERSION,
        "version": str(gate["version"]),
        "as_of": str(snapshot["latest_trade_date"]),  # type: ignore[index]
        "data_status": "available" if any(item["status"] == "available" for item in items) else "unavailable",
        "source": None if run is None else {
            key: run[key] for key in ("run_id", "fetched_at", "data_date", "manifest_sha256")
        },
        "metrics": list(METRICS),
        "windows": [name for name, _ in WINDOWS],
        "percentile_rule": PERCENTILE_RULE,
        "data_gaps": gaps,
        "note": NOTE,
        "items": items,
    }
    body["facts_id"] = "valuation-facts:" + canonical_sha256(body)[:20]
    return body


def validate_valuation_facts(
    valuation_root: Optional[Path], bundle: Mapping[str, object], policy: Mapping[str, object], team: Mapping[str, object],
    errors: List[str],
) -> None:
    """以同一個估值資料夾與 ``source.run_id`` 整份重算 ``team_inputs.valuation_facts``；多餘欄位或不一致即拒絕。"""
    prefix = "team_inputs.valuation_facts"
    facts = team.get(VALUATION_FACTS_KEY)
    if not isinstance(facts, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    gate = valuation_facts_gate(policy.get("position_sizing"))
    if gate is None:
        errors.append("%s 只能用於啟用 position_sizing.valuation_facts 的 Policy" % prefix)
        return
    reject_unknown_fields(facts, FACTS_FIELDS, prefix, errors)
    source = facts.get("source")
    pinned = str(source["run_id"]) if isinstance(source, Mapping) and isinstance(source.get("run_id"), str) else None
    try:
        expected = build_valuation_facts(valuation_root, bundle, gate, pinned)
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        errors.append("%s 無法重算：%s" % (prefix, error))
        return
    if dict(facts) != expected:
        errors.append("%s 與重算結果不同（估值 run 被改寫、讀不到估值資料夾或內容遭修改），拒絕" % prefix)


# ---------------------------------------------------------------- 簡報

def valuation_background_brief(facts: Mapping[str, object], symbols: Optional[Sequence[str]] = None) -> Dict[str, object]:
    """給基本面分析師與交易 Agent 的背景：沒有 facts_id、沒有 run 識別碼，``citable=false``。``symbols`` 指定時只列這些股票。"""
    wanted = None if symbols is None else {str(symbol).upper() for symbol in symbols}
    source = facts.get("source")
    return {
        "citable": False, "version": facts["version"], "data_status": facts["data_status"],
        "data_date": source.get("data_date") if isinstance(source, Mapping) else None,
        "metrics": facts["metrics"], "windows": facts["windows"], "percentile_rule": facts["percentile_rule"],
        "data_gaps": facts["data_gaps"], "note": facts["note"],
        "by_symbol": {
            str(item["symbol"]): {key: item.get(key) for key in ("status", "reason", "data_date", "metrics")}
            for item in facts["items"]  # type: ignore[union-attr]
            if wanted is None or str(item["symbol"]).upper() in wanted
        },
    }


def valuation_reference_tokens(facts: Optional[Mapping[str, object]]) -> List[str]:
    """交易 Agent 文字欄位不得出現的識別碼（背景不是證據）；沒有事實（舊 run）回空清單，驗證不變。"""
    if not isinstance(facts, Mapping):
        return []
    source = facts.get("source")
    tokens = ["valuation_background", VALUATION_FACTS_KEY, facts.get("facts_id"),
              source.get("run_id") if isinstance(source, Mapping) else None]
    return [token for token in tokens if isinstance(token, str) and token.strip()]


__all__ = [
    "APPROVED_LICENSE_SCOPES", "METRICS", "VALUATION_FACTS_KEY", "VALUATION_FACTS_V1", "WINDOWS", "build_valuation_facts",
    "validate_valuation_facts", "validate_valuation_facts_gate", "valuation_background_brief", "valuation_facts_gate",
    "valuation_reference_tokens", "valuation_root_for",
]
