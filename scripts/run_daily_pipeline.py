#!/usr/bin/env python3
"""每日一鍵：資料 → 決策子 Agent（claude -p）→ 封存 Decision run → DailyReport → 通知。

不自動下單或送件；產出的是供人工檢視的決策與報告。設計給 launchd 平日定時執行，
也可手動執行。同一台北日期已完成時不重跑（--force 可強制）。
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.automation.daily_pipeline import (  # noqa: E402
    ClaudeAgentRunner,
    DailyDecisionPipeline,
    degraded_research_result,
    next_weekday_session,
    validate_research_result,
)
from etf_agent.data import TradingStatusBundleBuilder, TradingStatusRequest  # noqa: E402
from etf_agent.data.evidence import TAIPEI_TIMEZONE  # noqa: E402


def log(message: str) -> None:
    print("[%s] %s" % (datetime.now(TAIPEI_TIMEZONE).strftime("%H:%M:%S"), message), flush=True)


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    script = 'display notification %s with title %s' % (json.dumps(message), json.dumps(title))
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def run_data_stage(account_id: str, account_run_id: str, report_run_id: str) -> None:
    """沿用 ./start.sh daily：抓資料、建 Snapshot、推進帳本；其報告工作流停在等待 Agent 屬預期。"""
    env = dict(os.environ, ACCOUNT_ID=account_id, ACCOUNT_RUN_ID=account_run_id, REPORT_RUN_ID=report_run_id)
    completed = subprocess.run([str(ROOT / "start.sh"), "daily"], cwd=ROOT, env=env, check=False)
    if completed.returncode not in (0, 3):
        raise RuntimeError("./start.sh daily 失敗（exit %d）" % completed.returncode)


def prepared_account_run(account_id: str) -> str:
    latest = json.loads((ROOT / "artifacts/virtual_accounts" / account_id / "latest.json").read_text(encoding="utf-8"))
    return str(latest["run_id"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", default="ai-cup-2026")
    parser.add_argument("--skip-data", action="store_true", help="沿用既有 Snapshot 與帳戶快照，不執行 ./start.sh daily")
    parser.add_argument("--force", action="store_true", help="週末或當日已完成仍執行")
    parser.add_argument("--model", help="子 Agent 使用的 Claude 模型；預設沿用 claude CLI 設定")
    parser.add_argument("--max-budget-usd", type=float, default=3.0, help="每次子 Agent 呼叫的費用上限")
    args = parser.parse_args()

    now = datetime.now(TAIPEI_TIMEZONE)
    runs_root = ROOT / "artifacts" / "daily_runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    lock_handle = (runs_root / ".lock").open("w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("另一個每日流程正在執行，本次略過。")
        return 0
    if now.weekday() >= 5 and not args.force:
        log("週末不執行（--force 可強制）。")
        return 0
    run_id = "daily-%s" % now.strftime("%Y%m%d")
    run_dir = runs_root / run_id
    summary_path = run_dir / "pipeline.json"
    if summary_path.exists() and not args.force:
        previous = json.loads(summary_path.read_text(encoding="utf-8"))
        if previous.get("status") == "completed":
            log("%s 已完成（%s），不重跑。" % (run_id, previous.get("decision_status")))
            return 0
    if args.force and run_dir.exists():
        run_id = "%s-%s" % (run_id, now.strftime("%H%M%S"))
        run_dir = runs_root / run_id
        summary_path = run_dir / "pipeline.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary = {"run_id": run_id, "started_at": now.isoformat(), "status": "running"}
    write_json(summary_path, summary)

    try:
        snapshot_path = ROOT / "artifacts" / "research_snapshot_latest.json"
        account_path = ROOT / "artifacts" / "virtual_accounts" / args.account_id / "account_snapshot_latest.json"
        if not args.skip_data:
            log("[1/5] ./start.sh daily：資料、Snapshot、虛擬帳本")
            run_data_stage(args.account_id, "prepare-%s" % stamp, "daily-%s" % stamp)
            if prepared_account_run(args.account_id) != "prepare-%s" % stamp:
                raise RuntimeError("虛擬帳本未建立本次 prepare 狀態（可能在等待前次決策的收盤價），不能產生新決策")
        account_run_id = prepared_account_run(args.account_id)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not snapshot.get("usable"):
            raise RuntimeError("Snapshot 不可用：%s" % snapshot.get("quality_flags"))

        log("[2/5] 交易狀態包與事件研究輸入")
        session = next_weekday_session(snapshot["decision_cutoff"])
        # 尚無核准的交易狀態來源：不提供 records／coverage，逐檔 unknown，Guard 會 fail-closed。
        request = TradingStatusRequest.from_snapshot(snapshot, session["start"], session["end"])
        status_bundle, assessment = TradingStatusBundleBuilder(request, [], []).build()
        trading_status_path = run_dir / "trading_status.json"
        write_json(trading_status_path, {"bundle": status_bundle, "assessment": assessment})
        research_path = ROOT / "artifacts" / "event_research_validated.json"
        research = json.loads(research_path.read_text(encoding="utf-8")) if research_path.exists() else None
        if research is None or research.get("snapshot_id") != snapshot["snapshot_id"] or validate_research_result(snapshot, research):
            research = degraded_research_result(snapshot, "%s-research" % run_id)
            research_path = run_dir / "research_result.json"
            write_json(research_path, research)
            errors = validate_research_result(snapshot, research)
            if errors:
                raise RuntimeError("降級 ResearchResult 驗證失敗：%s" % errors)

        log("[3/5] 決策鏈（子 Agent 經 claude -p）")
        pipeline = DailyDecisionPipeline(
            ROOT, run_dir,
            ClaudeAgentRunner(ROOT, model=args.model, max_budget_usd=args.max_budget_usd),
            ROOT / "artifacts" / "portfolio_decisions",
            log=log,
        )
        result = pipeline.run(
            snapshot_path, account_path, trading_status_path, research_path,
            ROOT / "config" / "decision_rules.json", ROOT / "var" / "etf_agent.db",
            ROOT / "config" / "decision_policy.json", ROOT / "data" / "sector_classification.json",
            "decision-%s" % stamp,
        )
        summary.update(
            {
                "decision_status": result.decision_status,
                "decision_run_id": result.decision_run_id,
                "cash_stance": result.cash_stance,
                "order_count": len(result.orders),
                "agent_calls": result.agent_calls,
                "agent_cost_usd": round(sum(call["cost_usd"] for call in result.agent_calls), 4),
                "target_session": session,
                "research_status": research.get("status"),
            }
        )
        if result.status != "completed":
            raise RuntimeError("決策鏈失敗：%s" % "；".join(result.errors))

        log("[4/5] 報告工作流：DailyReport")
        workflow = subprocess.run(
            [
                str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "cli" / "report_workflow.py"), "run",
                "--snapshot", str(snapshot_path),
                "--research", str(research_path),
                "--decision-repository", str(ROOT / "artifacts" / "portfolio_decisions"),
                "--decision-run-id", str(result.decision_run_id),
                "--virtual-account-repository", str(ROOT / "artifacts" / "virtual_accounts"),
                "--virtual-account-account-id", args.account_id,
                "--virtual-account-run-id", account_run_id,
                "--execution-mode", "official",
                "--run-id", "report-%s" % stamp,
            ],
            cwd=ROOT, capture_output=True, text=True, check=False,
            env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
        )
        (run_dir / "report_workflow.log").write_text(workflow.stdout + workflow.stderr, encoding="utf-8")
        summary["report_workflow_exit"] = workflow.returncode
        summary["report"] = str(ROOT / "artifacts" / "reports" / "latest.md")
        summary["status"] = "completed"
    except Exception as error:  # noqa: BLE001 — 任何失敗都要寫入摘要並通知
        summary["status"] = "failed"
        summary["error"] = str(error)
        log("失敗：%s" % error)
    summary["finished_at"] = datetime.now(TAIPEI_TIMEZONE).isoformat()
    write_json(summary_path, summary)

    log("[5/5] 通知")
    if summary["status"] == "completed":
        message = "決策 %s，%d 筆委託，現金姿態 %s，Agent 費用 $%s" % (
            summary.get("decision_status"), summary.get("order_count", 0),
            summary.get("cash_stance"), summary.get("agent_cost_usd"),
        )
    else:
        message = "失敗：%s" % str(summary.get("error"))[:120]
    notify("ETF Agent 每日決策", message)
    log(message)
    log("摘要：%s" % summary_path)
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
