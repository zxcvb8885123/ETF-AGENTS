#!/usr/bin/env python3
"""Agent 評測：資料抓齊率（不花用量）、建立評測集、重跑評測集（會呼叫 Agent）、產生報告。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.automation.agent_runner import ClaudeAgentRunner  # noqa: E402
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline  # noqa: E402
from etf_agent.data.universe import UniverseLoader  # noqa: E402
from etf_agent.evaluation import EvaluationError, build_suite, data_coverage, evaluate, run_suite  # noqa: E402
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
    run.add_argument("--model", help="交易與風險使用的模型；預設沿用 Claude CLI 設定")
    run.add_argument("--analyst-model", default="sonnet", help="分析師批次模型，與每日排程相同預設 sonnet")
    run.add_argument("--research-model", default="sonnet", help="多頭／空頭研究員模型，與每日排程相同預設 sonnet")
    run.add_argument("--max-workers", type=int, default=6, choices=range(1, 9), metavar="N")
    run.add_argument("--yes", action="store_true", help="確認會產生 Agent 用量")
    report = commands.add_parser("report", help="比較重跑結果，寫出 report.json 與 report.md")
    report.add_argument("--eval-id", required=True)
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
            if not args.yes:
                estimate = sum(float(case["reference_usage_usd"] or 0) for case in suite_data["cases"]) * args.repeats
                print("會重跑 %d 份考卷 × %d 次，完整決策鏈估算約 $%.0f（只跑分析師較少）。確認後加 --yes。" % (
                    len(suite_data["cases"]), args.repeats, estimate))
                return 2

            def make_pipeline(run_dir, decision_repository):
                return DailyDecisionPipeline(
                    ROOT, run_dir, ClaudeAgentRunner(ROOT, model=args.model, max_budget_usd=3.0), decision_repository,
                    analyst_runner=ClaudeAgentRunner(ROOT, model=args.analyst_model, max_budget_usd=3.0),
                    research_runner=ClaudeAgentRunner(ROOT, model=args.research_model, max_budget_usd=3.0),
                    technical_gate=True, max_workers=args.max_workers,
                )

            manifest = run_suite(ROOT, suite_data, make_pipeline, repeats=args.repeats, stop_after=args.stop_after)
            result = evaluate(ROOT, manifest["eval_id"])
            print(json.dumps({"eval_id": manifest["eval_id"], "overall": result["overall"],
                              "report": str(ROOT / EVALUATION_ROOT / "runs" / manifest["eval_id"] / "report.md")},
                             ensure_ascii=False, indent=2))
            return 0
        result = evaluate(ROOT, args.eval_id)
        print(json.dumps({"overall": result["overall"]}, ensure_ascii=False, indent=2))
        return 0
    except (EvaluationError, OSError, ValueError, KeyError) as error:
        print("評測停止：%s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
