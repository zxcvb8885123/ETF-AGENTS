#!/usr/bin/env python3
"""每日一鍵：資料 → 決策子 Agent（claude -p）→ 封存 Decision run → 通知。

不自動下單或送件；產出的是供人工檢視的封存決策。設計給 launchd 平日定時執行，
也可手動執行。同一台北日期已完成時不重跑（--force 可強制）。
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.automation.daily_pipeline import (  # noqa: E402
    ClaudeAgentRunner,
    DailyDecisionPipeline,
)
from etf_agent.automation.preflight import (  # noqa: E402
    DEFAULT_MIN_FREE_BYTES,
    checks_to_dict,
    require_preflight,
    run_preflight,
)
from etf_agent.data import (  # noqa: E402
    TradingCalendarError,
    build_trading_status_from_capture,
    calendar_from_capture,
    latest_calendar_capture_before,
    latest_capture_before,
    load_approvals,
    load_not_applicable,
)
from etf_agent.data.source_capture import capture_ogd_candidates  # noqa: E402
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


def run_data_stage(account_id: str, account_run_id: str) -> None:
    """沿用 ./start.sh daily：抓資料、建 Snapshot、推進帳本。"""
    env = dict(os.environ, ACCOUNT_ID=account_id, ACCOUNT_RUN_ID=account_run_id)
    completed = subprocess.run([str(ROOT / "start.sh"), "daily"], cwd=ROOT, env=env, check=False)
    if completed.returncode != 0:
        raise RuntimeError("./start.sh daily 失敗（exit %d）" % completed.returncode)


def latest_resumable_run(runs_root: Path, run_id: str):
    """同一台北日期中，最近一個未完成且已保存 Agent 原始輸出的 run 目錄；沒有則回 None。"""
    candidates = []
    for run_dir in runs_root.glob("%s*" % run_id):
        summary_path = run_dir / "pipeline.json"
        if not run_dir.is_dir() or not summary_path.exists() or not any(run_dir.glob("*_raw_*.json")):
            continue
        previous = json.loads(summary_path.read_text(encoding="utf-8"))
        # 決策被拒的 run 可在規則修正後續跑；被拒封存未移走時重新封存會衝突而停止。
        if previous.get("status") == "completed" and previous.get("decision_status") != "rejected":
            continue
        candidates.append((summary_path.stat().st_mtime, run_dir))
    return max(candidates)[1] if candidates else None


def resumed_decision_run_id(run_dir: Path):
    """續跑目錄先前預定的 decision run ID；舊版 summary 沒記錄時，由已保存的事件研究輸出檔名推回。"""
    planned = json.loads((run_dir / "pipeline.json").read_text(encoding="utf-8")).get("planned_decision_run_id")
    if planned:
        return str(planned)
    found = {
        match.group(1)
        for path in run_dir.glob("event-research_decision-*_raw_*.json")
        for match in [re.match(r"event-research_(decision-\d{8}T\d{6}Z)_", path.name)]
        if match
    }
    if len(found) > 1:
        raise RuntimeError("續跑目錄含多個 decision run ID，無法判定：%s" % sorted(found))
    return found.pop() if found else None


def prepared_account_run(account_id: str) -> str:
    latest = json.loads((ROOT / "artifacts/virtual_accounts" / account_id / "latest.json").read_text(encoding="utf-8"))
    return str(latest["run_id"])


def trading_day_check(captures_root: Path, now: datetime) -> bool:
    """依 now 前最近封存的官方開休市日曆判斷今天是否為交易日；日曆缺漏或未涵蓋今年即拋錯。"""
    capture = latest_calendar_capture_before(captures_root, now.isoformat())
    if capture is None:
        raise TradingCalendarError("找不到封存的官方開休市日期表（%s）" % captures_root)
    return calendar_from_capture(capture).is_trading_day(now.date())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--perception-bundle", type=Path, help="已核准授權且同 cutoff 的情緒與共識資料包")
    parser.add_argument("--perception", type=Path, help="與資料包成對的已驗證情緒與共識結果")
    parser.add_argument("--account-id", default="ai-cup-2026")
    parser.add_argument("--skip-data", action="store_true", help="沿用既有 Snapshot 與帳戶快照，不執行 ./start.sh daily")
    parser.add_argument("--force", action="store_true", help="休市日（週末或官方開休市日期表列出者）或當日已完成仍執行")
    parser.add_argument(
        "--resume", action="store_true",
        help="沿用同一 run 目錄中先前已通過驗證的 Agent 輸出（例如中途撞到用量上限後續跑）；建議搭配 --skip-data",
    )
    parser.add_argument("--model", help="子 Agent 使用的 Claude 模型；預設沿用 claude CLI 設定")
    parser.add_argument("--preflight-only", action="store_true", help="只執行執行前檢查（Docker、claude 登入、帳戶、必要檔案、磁碟）後結束")
    parser.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_BYTES / 1024 ** 3, help="執行前檢查要求的最低可用磁碟空間（GiB）")
    parser.add_argument("--max-budget-usd", type=float, default=3.0, help="單次子 Agent 的估算用量上限（防失控；claude.ai 訂閱登入時不另計費）")
    args = parser.parse_args()
    if args.perception is not None and args.perception_bundle is None:
        parser.error("--perception 必須搭配 --perception-bundle")

    now = datetime.now(TAIPEI_TIMEZONE)
    runs_root = ROOT / "artifacts" / "daily_runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    lock_handle = (runs_root / ".lock").open("w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("另一個每日流程正在執行，本次略過。")
        return 0
    min_free_bytes = int(args.min_free_gb * 1024 ** 3)
    if args.preflight_only:
        checks = run_preflight(ROOT, args.account_id, args.skip_data, min_free_bytes=min_free_bytes)
        for check in checks:
            log("%s %s：%s" % ("通過" if check.ok else "失敗", check.name, check.detail))
        return 0 if all(check.ok for check in checks) else 1
    captures_root = ROOT / "artifacts" / "source-audit" / "captures"
    if not args.force:
        try:
            if not trading_day_check(captures_root, now):
                log("%s 依官方開休市日曆休市，不執行（--force 可強制）。" % now.date().isoformat())
                return 0
        except (TradingCalendarError, OSError, ValueError) as error:
            log("無法判定今天是否為交易日，停止：%s" % error)
            notify("ETF Agent 每日決策", "失敗：無法判定交易日：%s" % str(error)[:100])
            return 1
    run_id = "daily-%s" % now.strftime("%Y%m%d")
    run_dir = runs_root / run_id
    summary_path = run_dir / "pipeline.json"
    if summary_path.exists() and not args.force:
        previous = json.loads(summary_path.read_text(encoding="utf-8"))
        if previous.get("status") == "completed":
            log("%s 已完成（%s），不重跑。" % (run_id, previous.get("decision_status")))
            return 0
    resumable = latest_resumable_run(runs_root, run_id) if args.resume else None
    if resumable is not None:
        # 續跑沿用最近一個未完成且已有 Agent 輸出的 run 目錄，否則新目錄中沒有可沿用的輸出。
        run_id = resumable.name
        run_dir = resumable
        summary_path = run_dir / "pipeline.json"
        log("續跑沿用 run 目錄：%s" % run_id)
    elif args.force and run_dir.exists():
        run_id = "%s-%s" % (run_id, now.strftime("%H%M%S"))
        run_dir = runs_root / run_id
        summary_path = run_dir / "pipeline.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    decision_run_id = "decision-%s" % stamp
    if resumable is not None:
        # Agent 任務名與 envelope 都含 decision run ID；續跑必須沿用同一個，已驗證輸出才對得上。
        decision_run_id = resumed_decision_run_id(run_dir) or decision_run_id
    summary = {"run_id": run_id, "started_at": now.isoformat(), "status": "running", "planned_decision_run_id": decision_run_id}
    write_json(summary_path, summary)

    try:
        # 在抓資料與呼叫任何子 Agent 前確認環境；一次列出全部失敗項目。
        log("[預檢] Docker、claude 登入、帳戶、必要檔案、磁碟空間")
        checks = run_preflight(ROOT, args.account_id, args.skip_data, min_free_bytes=min_free_bytes)
        summary["preflight"] = checks_to_dict(checks)
        require_preflight(checks)
        snapshot_path = ROOT / "artifacts" / "research_snapshot_latest.json"
        account_path = ROOT / "artifacts" / "virtual_accounts" / args.account_id / "account_snapshot_latest.json"
        if not args.skip_data:
            # 交易狀態 CSV 必須在 Snapshot 固定 cutoff 之前封存，available_at 才不會晚於 cutoff。
            log("[0/4] 封存交易狀態與開休市政府開放 CSV")
            try:
                report = json.loads((ROOT / "docs" / "source_audit" / "2026-09-25_ogd_crosscheck.json").read_text(encoding="utf-8"))
                capture = capture_ogd_candidates(report, captures_root)
                summary["trading_status_capture"] = capture.get("run_id")
            except (OSError, ValueError, json.JSONDecodeError) as error:
                summary["trading_status_capture_error"] = str(error)
                log("交易狀態封存失敗，改用 cutoff 前最新的成功封存：%s" % error)
            log("[1/4] ./start.sh daily：資料、Snapshot、虛擬帳本")
            run_data_stage(args.account_id, "prepare-%s" % stamp)
            if prepared_account_run(args.account_id) != "prepare-%s" % stamp:
                raise RuntimeError("虛擬帳本未建立本次 prepare 狀態（可能在等待前次決策的收盤價），不能產生新決策")
        account_run_id = prepared_account_run(args.account_id)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not snapshot.get("usable"):
            raise RuntimeError("Snapshot 不可用：%s" % snapshot.get("quality_flags"))

        log("[2/4] 交易狀態包（官方開休市日推算目標時段；未核准來源一律 fail-closed）")
        capture_dir = latest_capture_before(captures_root, snapshot["decision_cutoff"])
        approvals_path = ROOT / "config" / "trading_status_approvals.json"
        approvals = load_approvals(approvals_path)
        not_applicable = load_not_applicable(approvals_path)
        status_bundle, assessment, session = build_trading_status_from_capture(snapshot, capture_dir, approvals, not_applicable)
        summary["trading_status"] = {
            "capture_dir": str(capture_dir) if capture_dir else None,
            "approved_sources": sorted(approvals),
            "not_applicable": ["%s/%s" % (item["market"], item["category"]) for item in not_applicable],
            "states": {state: sum(1 for item in assessment["symbols"] if item["state"] == state) for state in ("allowed", "blocked", "unknown")},
        }
        trading_status_path = run_dir / "trading_status.json"
        write_json(trading_status_path, {"bundle": status_bundle, "assessment": assessment})

        log("[3/4] 決策鏈：分析團隊 → 重大事件研究 → 多空研究 → 交易 → 風險（子 Agent 經 claude -p）")
        pipeline = DailyDecisionPipeline(
            ROOT, run_dir,
            ClaudeAgentRunner(ROOT, model=args.model, max_budget_usd=args.max_budget_usd),
            ROOT / "artifacts" / "portfolio_decisions",
            log=log,
            resume=args.resume,
        )
        result = pipeline.run(
            snapshot_path, account_path, trading_status_path,
            ROOT / "config" / "decision_rules.json", ROOT / "var" / "etf_agent.db",
            ROOT / "config" / "decision_policy.json", ROOT / "data" / "sector_classification.json",
            decision_run_id,
            perception_bundle_path=args.perception_bundle, perception_result_path=args.perception,
        )
        summary.update(
            {
                "decision_status": result.decision_status,
                "decision_run_id": result.decision_run_id,
                "cash_stance": result.cash_stance,
                "order_count": len(result.orders),
                "agent_calls": result.agent_calls,
                "agent_estimated_usage_usd": round(sum(call["estimated_usage_usd"] for call in result.agent_calls), 4),
                "target_session": session,
            }
        )
        if result.status != "completed":
            raise RuntimeError("決策鏈失敗：%s" % "；".join(result.errors))
        research_path = result.research_result_path
        summary["research_status"] = json.loads(research_path.read_text(encoding="utf-8")).get("status")
        summary["status"] = "completed"
    except Exception as error:  # noqa: BLE001 — 任何失敗都要寫入摘要並通知
        summary["status"] = "failed"
        summary["error"] = str(error)
        log("失敗：%s" % error)
    summary["finished_at"] = datetime.now(TAIPEI_TIMEZONE).isoformat()
    write_json(summary_path, summary)

    log("[4/4] 通知")
    if summary["status"] == "completed":
        message = "決策 %s，%d 筆委託，現金姿態 %s，Agent 估算用量 $%s（訂閱不計費）" % (
            summary.get("decision_status"), summary.get("order_count", 0),
            summary.get("cash_stance"), summary.get("agent_estimated_usage_usd"),
        )
    else:
        message = "失敗：%s" % str(summary.get("error"))[:120]
    notify("ETF Agent 每日決策", message)
    log(message)
    log("摘要：%s" % summary_path)
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
