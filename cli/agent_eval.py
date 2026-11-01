#!/usr/bin/env python3
"""Agent 評測：資料抓齊率（不花用量）、建立評測集、重跑評測集（會呼叫 Agent，可續跑）、產生報告、回測格子（零 token）。"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.automation.agent_runner import ClaudeAgentRunner  # noqa: E402
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline  # noqa: E402
from etf_agent.data.universe import UniverseLoader  # noqa: E402
from etf_agent.evaluation import (  # noqa: E402
    DEFAULT_BENCHMARK_SYMBOL, EvaluationError, build_grid, build_suite, data_coverage, evaluate, run_suite, save_grid,
    unfinished_runs, verify_grid,
)
from etf_agent.evaluation.service import EVALUATION_ROOT  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    coverage = commands.add_parser("coverage", help="資料抓齊率：官方來源最新回應有多少已存進資料庫（只讀、不連網）")
    coverage.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    suite = commands.add_parser("suite", help="以已封存的每日 run 建立評測集")
    suite.add_argument("--name", required=True)
    suite.add_argument("--runs", nargs="*", help="每日 run ID；省略時收全部完成且未還原的 run")
    run = commands.add_parser("run", help="重跑評測集（會呼叫 Agent、產生用量）")
    run.add_argument("--suite", required=True)
    run.add_argument("--repeats", type=int, default=2, help="每份考卷重跑次數（1～5，預設 2，用來量穩定度）")
    run.add_argument("--stop-after", choices=("analysts", "full"), default="full",
                     help="analysts 只跑分析師（較省）；full 跑完整決策鏈")
    run.add_argument("--resume", metavar="EVAL_ID", help="續跑既有評測：只重跑未完成卷次，沿用已通過驗證的原始 Agent 輸出")
    run.add_argument("--model", help="交易與風險使用的模型；預設沿用 Claude CLI 設定")
    run.add_argument("--analyst-model", default="sonnet", help="分析師批次模型，與每日排程相同預設 sonnet")
    run.add_argument("--research-model", default="sonnet", help="多頭／空頭研究員模型，與每日排程相同預設 sonnet")
    run.add_argument("--max-workers", type=int, default=6, choices=range(1, 9), metavar="N")
    run.add_argument("--yes", action="store_true", help="確認會產生 Agent 用量")
    report = commands.add_parser("report", help="比較重跑結果，寫出 report.json、report.md 與 report.html")
    report.add_argument("--eval-id", required=True)
    grid = commands.add_parser("grid", help="回測格子：每筆決策一列（run × symbol），按評級分組的兩種超額報酬（零 token）")
    grid.add_argument("--start", help="成交日 T 起（YYYY-MM-DD）")
    grid.add_argument("--end", help="成交日 T 迄（YYYY-MM-DD）")
    grid.add_argument("--runs", nargs="*", help="指定 Decision run ID（省略時用日期範圍）")
    grid.add_argument("--as-of", help="只用此時間前抓到的官方價格（含時區 ISO；預設現在）")
    grid.add_argument("--benchmark", default=DEFAULT_BENCHMARK_SYMBOL, help="基準 ETF 代號（預設 %s）" % DEFAULT_BENCHMARK_SYMBOL)
    grid.add_argument("--decisions", type=Path, default=ROOT / "artifacts" / "decisions", help="決策庫（預設 artifacts/decisions）")
    grid.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db", help="行情資料庫（唯讀開啟）")
    grid.add_argument("--grid-id", help="自訂格子 ID（預設 grid-<UTC 時間>）")
    grid_verify = commands.add_parser("grid-verify", help="以格子記錄的輸入重算並逐欄比對")
    grid_verify.add_argument("--grid-id", required=True)
    grid_verify.add_argument("--decisions", type=Path, help="改用指定決策庫重算（預設用格子記錄的路徑）")
    grid_verify.add_argument("--database", type=Path, help="改用指定資料庫重算（預設用格子記錄的路徑）")
    args = parser.parse_args()
    try:
        if args.command == "coverage":
            result = data_coverage(args.database, UniverseLoader().load(ROOT / "data" / "official_universe.csv"),
                                   json.loads((ROOT / "config" / "data_sources.json").read_text(encoding="utf-8")))
            output = ROOT / EVALUATION_ROOT / "coverage_latest.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"status": result["status"], "incomplete": result["incomplete_sources"],
                              "universe_coverage": result["universe_coverage"], "output": str(output)},
                             ensure_ascii=False, indent=2))
            return 0 if result["status"] == "complete" else 2
        if args.command == "suite":
            created = build_suite(ROOT, args.name, args.runs)
            print(json.dumps({"name": created["name"], "cases": [case["case_id"] for case in created["cases"]],
                              "reference_usage_usd": sum(float(case["reference_usage_usd"] or 0) for case in created["cases"])},
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            suite_data = json.loads((ROOT / EVALUATION_ROOT / "suites" / ("%s.json" % args.suite)).read_text(encoding="utf-8"))
            usage_by_case = {case["case_id"]: float(case["reference_usage_usd"] or 0) for case in suite_data["cases"]}
            if args.resume:
                pending = unfinished_runs(ROOT, args.resume)
                if not pending:
                    print("評測 %s 沒有未完成的卷次，不需要續跑；要更新報告請用 report。" % args.resume)
                    return 2
                estimate = sum(usage_by_case.get(item.split("/", 1)[0], 0.0) for item in pending)
                if not args.yes:
                    print("會續跑 %d 個未完成卷次（%s），完整決策鏈估算最多約 $%.0f（已通過驗證的原始輸出會沿用，實際較少）。確認後加 --yes。" % (
                        len(pending), "、".join(pending), estimate))
                    return 2
            elif not args.yes:
                estimate = sum(usage_by_case.values()) * args.repeats
                print("會重跑 %d 份考卷 × %d 次，完整決策鏈估算約 $%.0f（只跑分析師較少）。確認後加 --yes。" % (
                    len(suite_data["cases"]), args.repeats, estimate))
                return 2

            def make_pipeline(run_dir, decision_repository, resume=False):
                return DailyDecisionPipeline(
                    ROOT, run_dir, ClaudeAgentRunner(ROOT, model=args.model, max_budget_usd=3.0), decision_repository,
                    analyst_runner=ClaudeAgentRunner(ROOT, model=args.analyst_model, max_budget_usd=3.0),
                    research_runner=ClaudeAgentRunner(ROOT, model=args.research_model, max_budget_usd=3.0),
                    technical_gate=True, max_workers=args.max_workers, resume=resume,
                )

            manifest = run_suite(ROOT, suite_data, make_pipeline, repeats=args.repeats, stop_after=args.stop_after,
                                 resume_eval_id=args.resume)
            result = evaluate(ROOT, manifest["eval_id"])
            print(json.dumps({"eval_id": manifest["eval_id"], "overall": result["overall"],
                              "report": str(ROOT / EVALUATION_ROOT / "runs" / manifest["eval_id"] / "report.md"),
                              "html": str(ROOT / EVALUATION_ROOT / "runs" / manifest["eval_id"] / "report.html")},
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == "grid":
            body = build_grid(
                ROOT, decision_root=args.decisions, database=args.database,
                as_of=args.as_of or datetime.now(timezone.utc).isoformat(), start=args.start, end=args.end, run_ids=args.runs,
                benchmark=args.benchmark, log=print,
            )
            grid_id, run_dir = save_grid(ROOT, body, args.grid_id)
            summary = {horizon: {"status": entry["status"], "complete_days": entry["complete_days"], "n_obs": entry["n_obs"]}
                       for horizon, entry in body["horizons"].items()}
            print(json.dumps({"grid_id": grid_id, "rows": len(body["rows"]), "days": len(body["days"]), "horizons": summary,
                              "json": str(run_dir / "grid.json"), "html": str(run_dir.parent / ("%s.html" % grid_id))},
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == "grid-verify":
            result = verify_grid(ROOT, args.grid_id, decision_root=args.decisions, database=args.database, log=print)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["valid"] else 1
        result = evaluate(ROOT, args.eval_id)
        print(json.dumps({"overall": result["overall"]}, ensure_ascii=False, indent=2))
        return 0
    except (EvaluationError, OSError, ValueError, KeyError) as error:
        print("評測停止：%s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
