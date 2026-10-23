"""固定評測集：挑已封存的每日決策當考卷，重跑 Agent，和當時的結論（參考答案）及彼此比較。

- 考卷是當時已驗證的 ``decision_input.json``，不重抓資料；重跑結果寫在 ``artifacts/evaluation/``，
  有自己的決策庫，不碰正式決策庫、帳本或 D-Plan。
- 「與參考一致」只表示和當時那次結論相同，不代表對錯；對錯由 ``calibration`` 用之後的實際價格衡量。
- 重跑會呼叫 Agent、產生用量，只在使用者明確執行 ``run`` 時發生。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from etf_agent.core import parse_aware_time
from etf_agent.core.run_layout import find_run_dir, list_run_dirs

from .answers import score_runs
from .compare import compare_outputs, load_outputs, pairwise_stability, process_stats, summarize
from .contracts import EVALUATION_SCHEMA_VERSION, STOP_STAGES, EvaluationError


EVALUATION_ROOT = Path("artifacts") / "evaluation"


def _write(path: Path, payload: Mapping[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _read(path: Path, label: str) -> Dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluationError("%s 無法讀取：%s" % (label, path)) from error
    if not isinstance(value, dict):
        raise EvaluationError("%s 必須是 JSON 物件" % label)
    return value


def eligible_case(run_dir: Path) -> Optional[Dict[str, object]]:
    """完成、未還原、有封存輸入與交易決策的每日 run 才能當考卷。"""
    summary_path, bundle_path = run_dir / "pipeline.json", run_dir / "decision_input.json"
    if not summary_path.is_file() or not bundle_path.is_file() or not (run_dir / "intent.json").is_file():
        return None
    summary = _read(summary_path, "每日 pipeline 摘要")
    if summary.get("status") != "completed" or summary.get("rolled_back"):
        return None
    bundle = _read(bundle_path, "DecisionInputBundle")
    return {
        "case_id": run_dir.name, "reference_dir": str(run_dir), "bundle_path": str(bundle_path),
        "bundle_sha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
        "decision_cutoff": bundle.get("decision_cutoff"), "reference_decision_run_id": summary.get("decision_run_id"),
        "reference_usage_usd": summary.get("agent_estimated_usage_usd"),
    }


def build_suite(root: Path, name: str, run_ids: Optional[Sequence[str]] = None) -> Dict[str, object]:
    """指定 run（或不指定時取全部合格的每日 run）建立評測集清單；同一份輸入只收一次。"""
    daily_root = Path(root) / "artifacts" / "daily"
    if run_ids:
        directories = []
        for run_id in run_ids:
            found = find_run_dir(daily_root, run_id, EvaluationError)
            if found is None:
                raise EvaluationError("找不到每日 run：%s" % run_id)
            directories.append(found)
    else:
        directories = list_run_dirs(daily_root)
    cases, seen = [], set()
    for directory in directories:
        case = eligible_case(directory)
        if case is None:
            if run_ids:
                raise EvaluationError("%s 不能當考卷：未完成、已還原或缺少封存輸入" % directory.name)
            continue
        if case["bundle_sha256"] in seen:
            continue
        seen.add(case["bundle_sha256"])
        cases.append(case)
    if not cases:
        raise EvaluationError("沒有可當考卷的每日 run")
    suite = {"schema_version": EVALUATION_SCHEMA_VERSION, "name": name,
             "created_at": datetime.now(timezone.utc).isoformat(), "cases": cases}
    _write(Path(root) / EVALUATION_ROOT / "suites" / ("%s.json" % name), suite)
    return suite


def eval_policy_template(policy_template_path: Path, decision_cutoff: str, output: Path) -> Dict[str, object]:
    """評測用策略樣板：設定版本晚於考卷 cutoff 時，把 available_at 提前到 cutoff 才能重建 Policy，並記錄這個改動。"""
    template = _read(policy_template_path, "策略樣板")
    original = str(template.get("available_at"))
    overridden = parse_aware_time(original, "available_at", error=EvaluationError) > parse_aware_time(
        decision_cutoff, "decision_cutoff", error=EvaluationError)
    if overridden:
        template["available_at"] = decision_cutoff
    _write(output, template)
    return {"policy_id": template.get("policy_id"), "original_available_at": original, "available_at_overridden": overridden}


def run_suite(
    root: Path,
    suite: Mapping[str, object],
    make_pipeline: Callable[[Path, Path], object],
    *,
    repeats: int = 2,
    stop_after: str = "full",
    eval_id: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> Dict[str, object]:
    """每份考卷重跑 ``repeats`` 次；``make_pipeline(run_dir, decision_repository)`` 回傳 DailyDecisionPipeline。"""
    if stop_after not in STOP_STAGES:
        raise EvaluationError("stop_after 必須是 %s" % "／".join(STOP_STAGES))
    if not 1 <= int(repeats) <= 5:
        raise EvaluationError("repeats 必須介於 1 與 5")
    root = Path(root)
    eval_id = eval_id or "eval-%s" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    eval_dir = root / EVALUATION_ROOT / "runs" / eval_id
    if eval_dir.exists():
        raise EvaluationError("評測 run 已存在，不覆寫：%s" % eval_dir)
    manifest = {"schema_version": EVALUATION_SCHEMA_VERSION, "eval_id": eval_id, "suite": suite.get("name"),
                "repeats": int(repeats), "stop_after": stop_after,
                "started_at": datetime.now(timezone.utc).isoformat(), "cases": []}
    for case in suite["cases"]:
        bundle_path = Path(case["bundle_path"])
        if hashlib.sha256(bundle_path.read_bytes()).hexdigest() != case["bundle_sha256"]:
            raise EvaluationError("考卷 %s 的輸入檔已改變，請重建評測集" % case["case_id"])
        case_dir = eval_dir / case["case_id"]
        policy = eval_policy_template(root / "config" / "decision_policy.json", str(case["decision_cutoff"]),
                                      case_dir / "policy_template.json")
        runs = []
        for repeat in range(1, int(repeats) + 1):
            run_dir = case_dir / ("r%d" % repeat)
            run_dir.mkdir(parents=True)
            log("[評測] %s 第 %d 次" % (case["case_id"], repeat))
            pipeline = make_pipeline(run_dir, case_dir / "decisions")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            result = pipeline.run_from_bundle(
                bundle_path, root / "var" / "etf_agent.db", case_dir / "policy_template.json",
                root / "data" / "sector_classification.json", "decision-%s-r%d" % (stamp, repeat),
                stop_after=None if stop_after == "full" else stop_after,
            )
            runs.append({"repeat": repeat, "run_dir": str(run_dir), "status": result.status, "errors": result.errors,
                         "agent_calls": result.agent_calls, "stage_durations": result.stage_durations})
            _write(run_dir / "eval_result.json", runs[-1])
        manifest["cases"].append({"case_id": case["case_id"], "policy": policy, "runs": runs})
        _write(eval_dir / "manifest.json", manifest)
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write(eval_dir / "manifest.json", manifest)
    return manifest


def evaluate(root: Path, eval_id: str, as_of: Optional[str] = None) -> Dict[str, object]:
    """讀重跑結果：與參考答案的一致度、重跑之間的穩定度、執行過程，以及 ``as_of`` 前已到齊的實際股價答案，
    寫出 report.json 與 report.md。答案會隨時間變多，之後再跑 ``report`` 即可更新。"""
    eval_dir = Path(root) / EVALUATION_ROOT / "runs" / eval_id
    manifest = _read(eval_dir / "manifest.json", "評測 manifest")
    suite = _read(Path(root) / EVALUATION_ROOT / "suites" / ("%s.json" % manifest["suite"]), "評測集")
    references = {case["case_id"]: case for case in suite["cases"]}
    cases, all_reference, all_stability, answer_inputs = [], [], [], []
    for case in manifest["cases"]:
        reference = load_outputs(Path(references[case["case_id"]]["reference_dir"]))
        completed = [run for run in case["runs"] if run["status"] == "completed"]
        outputs = [load_outputs(Path(run["run_dir"])) for run in completed]
        bundle = _read(Path(references[case["case_id"]]["bundle_path"]), "DecisionInputBundle")
        answer_inputs.append({
            "case_id": case["case_id"], "decision_cutoff": bundle["decision_cutoff"],
            "symbols": sorted(str(row["symbol"]).upper() for row in bundle["snapshot"]["latest_prices"]),
            "reference": reference, "reruns": outputs,
        })
        versus_reference = [compare_outputs(reference, output) for output in outputs]
        stability = pairwise_stability(outputs)
        all_reference.extend(versus_reference)
        all_stability.extend(stability)
        cases.append({
            "case_id": case["case_id"], "policy": case["policy"],
            "completed_runs": len(completed), "failed_runs": [
                {"repeat": run["repeat"], "errors": run["errors"][:3]} for run in case["runs"] if run["status"] != "completed"],
            "versus_reference": summarize(versus_reference), "stability": summarize(stability),
            "versus_reference_detail": versus_reference, "stability_detail": stability,
            "process": [{"repeat": run["repeat"], **process_stats(run["agent_calls"]),
                         "stage_durations": run["stage_durations"]} for run in case["runs"]],
        })
    report = {
        "schema_version": EVALUATION_SCHEMA_VERSION, "eval_id": eval_id, "suite": manifest["suite"],
        "stop_after": manifest["stop_after"], "repeats": manifest["repeats"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": {"versus_reference": summarize(all_reference), "stability": summarize(all_stability),
                    "runs": sum(len(case["runs"]) for case in manifest["cases"]),
                    "failed_runs": sum(len(case["failed_runs"]) for case in cases),
                    "estimated_usage_usd": round(sum(item["estimated_usage_usd"] for case in cases for item in case["process"]), 4)},
        "cases": cases,
        "answers": score_runs(Path(root) / "var" / "etf_agent.db",
                              as_of or datetime.now(timezone.utc).isoformat(), answer_inputs),
    }
    _write(eval_dir / "report.json", report)
    (eval_dir / "report.md").write_text(render_report(report), encoding="utf-8")
    return report


_LABELS = {"technical": "技術分析師看法", "fundamental": "基本面分析師看法", "event": "事件分析師看法",
           "event_materiality": "事件重要程度", "intent": "交易意圖／信心", "cash_stance": "現金姿態", "orders": "委託股票與方向"}


def _summary_lines(summary: Mapping[str, object]) -> List[str]:
    lines = ["| 項目 | 比較組數 | 平均相同 | 最低相同 | 正負相反 |", "| --- | --- | --- | --- | --- |"]
    for name, label in _LABELS.items():
        row = summary.get(name)
        if row:
            lines.append("| %s | %d | %.1f%% | %.1f%% | %d |" % (
                label, row["pairs"], row["mean_agreement"] * 100, row["min_agreement"] * 100, row["opposite"]))
        else:
            lines.append("| %s | 0 | 未比較 | 未比較 | - |" % label)
    return lines


def _answer_lines(answers: Mapping[str, object]) -> List[str]:
    lines = ["| 天數 | 對象 | 項目 | 完整決策日 | 筆數 | 命中率 |", "| --- | --- | --- | --- | --- | --- |"]
    for horizon, groups in answers["overall"].items():
        for who, label in (("reference", "當時結論"), ("reruns", "重跑")):
            for name, row in groups[who].items():
                rate = "%.1f%%" % (row["hit_rate"] * 100) if row.get("hit_rate") is not None else (
                    "樣本不足（需 %d 天）" % answers["required_decision_days"] if row["status"] == "insufficient_sample" else "無")
                lines.append("| %s 日 | %s | %s | %d | %d | %s |" % (horizon, label, _LABELS.get(name, name), row["complete_days"], row["n"], rate))
    if len(lines) == 2:
        lines.append("| - | - | 還沒有任何考卷的股價到齊 | 0 | 0 | 待答案 |")
    for case in answers["cases"]:
        pending = ["%s 日：%s" % (h, item["status"]) for h, item in case["horizons"].items() if item["status"] != "complete"]
        if pending:
            lines.append("")
            lines.append("- %s（成交日 %s）尚未有答案：%s" % (case["case_id"], case["entry_day"] or "未定", "、".join(pending)))
    return lines


def render_report(report: Mapping[str, object]) -> str:
    overall = report["overall"]
    lines = [
        "# Agent 評測報告：%s" % report["eval_id"], "",
        "評測集 %s；每份重跑 %d 次；範圍：%s。共 %d 次，失敗 %d 次；估算用量 $%s。" % (
            report["suite"], report["repeats"], "只到分析師" if report["stop_after"] == "analysts" else "完整決策鏈",
            overall["runs"], overall["failed_runs"], overall["estimated_usage_usd"]), "",
        "「相同」只表示結論一樣，不代表對錯；對錯要用之後的實際價格（calibration）衡量。", "",
        "## 與當時結論一致度", "", *_summary_lines(overall["versus_reference"]), "",
        "## 重跑穩定度（同一份輸入兩兩比較）", "", *_summary_lines(overall["stability"]), "",
        "## 準不準（之後的實際股價）", "",
        "看好＝分析師正面、交易 buy／add、買單；看壞＝分析師負面、交易 trim／exit、賣單。命中＝看好的贏過交易池平均、看壞的輸給平均（官方成交均價）。",
        "", *_answer_lines(report["answers"]), "",
        "## 每份考卷", "",
    ]
    for case in report["cases"]:
        lines.append("### %s" % case["case_id"])
        lines.append("")
        if case["policy"]["available_at_overridden"]:
            lines.append("- 策略設定版本晚於考卷，評測時把 available_at 提前到考卷 cutoff。")
        for item in case["failed_runs"]:
            lines.append("- 第 %d 次失敗：%s" % (item["repeat"], "；".join(item["errors"]) or "未知"))
        for item in case["process"]:
            lines.append("- 第 %d 次：%d 次呼叫，重試 %s，驗證失敗 %d 次，$%s，Agent 時間 %s 秒。" % (
                item["repeat"], item["calls"], "、".join(item["retried_steps"]) or "無",
                len(item["validation_failures"]), item["estimated_usage_usd"], item["agent_seconds"]))
        lines.append("")
    return "\n".join(lines) + "\n"
