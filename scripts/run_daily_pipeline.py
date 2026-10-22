#!/usr/bin/env python3
"""每日一鍵：資料 → 決策子 Agent → 封存 Decision run → 最終中文交付 → 通知。

不自動下單或送件；產出的是供人工檢視的封存決策。設計給 launchd 平日定時執行，
也可手動執行。同一台北日期已完成時不重跑（--force 可強制）。
"""

import argparse
try:
    import fcntl
except ImportError:  # Windows 以 msvcrt 鎖定同一執行鎖。
    fcntl = None
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.decision_delivery import build_delivery, save_failure
from etf_agent.decision_delivery.repository import publication_lock
from etf_agent.competition import DPlanError, DPlanExportService

from etf_agent.automation.daily_pipeline import (  # noqa: E402
    ClaudeAgentRunner,
    CodexAgentRunner,
    OpenCodeAgentRunner,
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
from etf_agent.data.inferred_closures import update_inferred_closed_days  # noqa: E402
from etf_agent.data.source_capture import capture_ogd_candidates  # noqa: E402
from etf_agent.core.run_layout import day_from_run_id, find_run_dir, list_run_dirs  # noqa: E402
from etf_agent.core.timeutil import parse_aware_time  # noqa: E402
from etf_agent.data.evidence import TAIPEI_TIMEZONE  # noqa: E402
from etf_agent.virtual_account.daily import supersede_decision_runs  # noqa: E402

DEFAULT_CLAUDE_ANALYST_MODEL = "sonnet"
RESUME_LATEST = "latest"  # --resume 不帶 RUN_ID：續跑今天最近一個未完成的 run
DATA_READY_TIME = time(15, 0)  # 交易日 15:00 後當日行情才算齊全（yfinance 等來源收盤後才有）
TRADE_DATE_LOOKBACK_DAYS = 14  # 往前找最近交易日的上限，避免日曆缺漏時無窮迴圈


def log(message: str) -> None:
    print("[%s] %s" % (datetime.now(TAIPEI_TIMEZONE).strftime("%H:%M:%S"), message), flush=True)


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    script = 'display notification %s with title %s' % (json.dumps(message), json.dumps(title))
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def record_calibration() -> dict:
    """旁觀記錄：把已封存的決策記成校準觀測。只呼叫 CLI，不匯入校準套件；失敗只記錄，不影響決策、摘要狀態與結束碼。"""
    try:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "cli" / "calibration.py"), "snapshot"],
            cwd=ROOT, check=False, capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as error:
        log("[校準] 未執行：%s" % error)
        return {"status": "error", "error": str(error)}
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-200:]
        log("[校準] 記錄失敗（exit %d）：%s" % (completed.returncode, detail))
        return {"status": "error", "exit_code": completed.returncode, "error": detail}
    log("[校準] 已記錄封存決策的觀測")
    return {"status": "recorded"}


def run_data_stage(account_id: str, account_run_id: str, history_end=None) -> None:
    """沿用 ./start.sh daily：抓資料、建 Snapshot、推進帳本。

    history_end 是此刻應已有收盤行情的最近交易日；交易日 15:00 前它是前一個交易日，
    不能要求「今天」的日線。判斷不出時不傳，維持 collect_history 預設的當日要求（fail-closed）。"""
    env = dict(os.environ, ACCOUNT_ID=account_id, ACCOUNT_RUN_ID=account_run_id)
    if history_end is not None:
        env["HISTORY_END"] = history_end.isoformat()
    completed = subprocess.run([str(ROOT / "start.sh"), "daily"], cwd=ROOT, env=env, check=False)
    if completed.returncode != 0:
        raise RuntimeError("./start.sh daily 失敗（exit %d）" % completed.returncode)


def expected_latest_trade_date(captures_root: Path, now: datetime):
    """依官方開休市日曆，回傳此刻「應該已有行情」的最近交易日；日曆缺漏或判斷失敗回 None（不猜測）。

    交易日要到 DATA_READY_TIME 之後才算當日行情齊全；之前、或非交易日，則取前一個交易日。
    """
    try:
        capture = latest_calendar_capture_before(captures_root, now.isoformat())
        if capture is None:
            return None
        calendar = calendar_from_capture(capture)
        day = now.date()
        for _ in range(TRADE_DATE_LOOKBACK_DAYS):
            if calendar.is_trading_day(day) and (day < now.date() or now.time() >= DATA_READY_TIME):
                return day
            day -= timedelta(days=1)
    except (TradingCalendarError, OSError, ValueError):
        return None
    return None


def data_already_fetched(snapshot_path: Path, account_path: Path, expected_trade_date) -> bool:
    """Snapshot 可用、150 檔都有最新價、最新交易日就是應有的那一天，且在該日 DATA_READY_TIME 之後建立，
    帳戶快照也存在，才算「已抓到資料」。

    Yahoo 盤中就會回傳當日那根日線，但數值尚未定案；所以光看日期不夠，還要確認快照是收盤資料齊全後才建立的。
    任何讀取失敗、欄位缺漏或日期對不上都視為沒抓到，需重抓。
    """
    if expected_trade_date is None:
        return False
    try:
        if not account_path.is_file():
            return False
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        created_at = parse_aware_time(snapshot.get("created_at"), "snapshot.created_at")
    except (OSError, ValueError):
        return False
    ready_at = datetime.combine(expected_trade_date, DATA_READY_TIME, tzinfo=TAIPEI_TIMEZONE)
    return (
        created_at >= ready_at
        and snapshot.get("usable") is True
        and snapshot.get("latest_trade_date") == expected_trade_date.isoformat()
        and isinstance(snapshot.get("universe_size"), int)
        and snapshot.get("latest_price_symbols") == snapshot.get("universe_size")
    )


def daily_run_path(runs_root: Path, run_id: str) -> Path:
    """每日 run 放在 <runs_root>/<台北日期>/<run_id>；已存在則沿用其位置。"""
    existing = find_run_dir(runs_root, run_id, RuntimeError)
    if existing is not None:
        return existing
    day = day_from_run_id(run_id)
    if day is None:
        raise RuntimeError("每日 run 名稱沒有日期：%s" % run_id)
    return runs_root / day / run_id


def latest_resumable_run(runs_root: Path, run_id: str):
    """同一台北日期中，最近一個未完成且已保存 Agent 原始輸出的 run 目錄；沒有則回 None。"""
    candidates = []
    for run_dir in list_run_dirs(runs_root):
        if not run_dir.name.startswith(run_id):
            continue
        summary_path = run_dir / "pipeline.json"
        if not run_dir.is_dir() or not summary_path.exists() or not any(run_dir.glob("*_raw_*.json")):
            continue
        previous = json.loads(summary_path.read_text(encoding="utf-8"))
        # 決策被拒的 run 可在規則修正後續跑；被拒封存未移走時重新封存會衝突而停止。
        if previous.get("status") == "completed" and previous.get("decision_status") != "rejected":
            continue
        candidates.append((summary_path.stat().st_mtime, run_dir))
    return max(candidates)[1] if candidates else None


def named_resumable_run(runs_root: Path, name: str) -> Path:
    """指定續跑的 run 目錄（跨午夜或用量上限重置後，日期已變，不能靠今天的日期找）；必須未完成且已有 Agent 原始輸出。"""
    run_dir = daily_run_path(runs_root, name) if re.fullmatch(r"daily-[0-9A-Za-z-]+", name) else runs_root / "invalid"
    summary_path = run_dir / "pipeline.json"
    if not re.fullmatch(r"daily-[0-9A-Za-z-]+", name) or not run_dir.is_dir() or not summary_path.exists():
        raise RuntimeError("找不到可續跑的 run 目錄：%s" % name)
    previous = json.loads(summary_path.read_text(encoding="utf-8"))
    if previous.get("status") == "completed" and previous.get("decision_status") != "rejected":
        raise RuntimeError("%s 已完成，不需續跑" % name)
    if not any(run_dir.glob("*_raw_*.json")):
        raise RuntimeError("%s 沒有已保存的 Agent 輸出，無可沿用內容" % name)
    return run_dir


def resumable_agent_cli(run_dir: Path) -> str:
    """續跑必須沿用該 run 已記錄的執行器；舊版摘要預設為 Claude。"""
    previous = json.loads((run_dir / "pipeline.json").read_text(encoding="utf-8"))
    return previous.get("agent_cli", "claude")


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
    latest = json.loads((ROOT / "artifacts/ledger" / account_id / "latest.json").read_text(encoding="utf-8"))
    return str(latest["run_id"])


def capture_trading_sources(captures_root: Path) -> dict:
    """抓八份政府開放 CSV；成功才刪除舊封存，只留最新一份。"""
    report = json.loads((ROOT / "docs" / "source_audit" / "2026-09-25_ogd_crosscheck.json").read_text(encoding="utf-8"))
    return capture_ogd_candidates(report, captures_root, keep_latest_only=True)


def ensure_calendar_capture(captures_root: Path, now: datetime) -> None:
    """還沒有開休市日期表（新環境或資料夾被清掉）就先去抓；抓不到則照原本流程報錯停止。"""
    try:
        if latest_calendar_capture_before(captures_root, now.isoformat()) is not None:
            return
        log("找不到封存的開休市日期表，先抓政府開放 CSV")
        capture_trading_sources(captures_root)
    except (TradingCalendarError, OSError, ValueError) as error:
        log("政府開放 CSV 抓取失敗：%s" % error)


def refresh_inferred_closures(captures_root: Path, now: datetime) -> None:
    """用資料庫的官方行情缺口推斷官方日期表沒列的臨時休市日（如颱風假）並保存證據；失敗只記錄，不中斷。"""
    try:
        capture = latest_calendar_capture_before(captures_root, now.isoformat())
        database = ROOT / "var" / "etf_agent.db"
        if capture is None or not database.exists():
            return
        added = update_inferred_closed_days(database, captures_root, calendar_from_capture(capture), now)
        if added:
            log("依官方行情缺口，推斷臨時休市日：%s（證據：%s）" % ("、".join(added), captures_root / "inferred_closed_days.json"))
            notify("ETF Agent 每日決策", "推斷臨時休市：%s" % "、".join(added))
    except (TradingCalendarError, OSError, ValueError) as error:
        log("臨時休市推斷未執行：%s" % error)


def trading_day_check(captures_root: Path, now: datetime) -> bool:
    """依 now 前最近封存的官方開休市日曆判斷今天是否為交易日；日曆缺漏或未涵蓋今年即拋錯。"""
    capture = latest_calendar_capture_before(captures_root, now.isoformat())
    if capture is None:
        raise TradingCalendarError("找不到封存的官方開休市日期表（%s）" % captures_root)
    return calendar_from_capture(capture).is_trading_day(now.date())


def deliver_saved_run(root: Path, name: str) -> dict:
    """只重建已封存決策的交付，不抓資料或重跑模型。"""
    with publication_lock(root / "artifacts/daily", filename=".lock"):
        return _deliver_saved_run(root, name)


def _deliver_saved_run(root: Path, name: str) -> dict:
    if not re.fullmatch(r"daily-[0-9A-Za-z-]+", name):
        raise ValueError("每日 run 名稱不符合白名單")
    summary_path = daily_run_path(root / "artifacts/daily", name) / "pipeline.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    decision_id = summary.get("decision_run_id") or summary.get("planned_decision_run_id")
    if not decision_id:
        raise ValueError("此 run 沒有封存決策識別，不能只續跑交付")
    delivery = build_delivery(root / "artifacts/decisions", decision_id,
        root / "artifacts/reports", reports_root=root / "artifacts/latest",
        started_at=summary["started_at"])
    summary["delivery"] = delivery
    if delivery["report_status"] == "ready":
        summary["status"] = "completed"
        summary["decision_status"] = delivery["decision_status"]
        summary.pop("error", None)
    else:
        summary["status"] = "failed"
        summary["error"] = "最終報告重建失敗，詳見交付診斷"
    write_json(summary_path, summary)
    return delivery


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--perception", nargs=2, type=Path, metavar=("BUNDLE", "RESULT"),
        help="已核准授權且同 cutoff 的情緒與共識資料包，以及與其成對的已驗證結果；兩個檔案須同時提供",
    )
    parser.add_argument("--account-id", default="ai-cup-2026")
    parser.add_argument("--delivery-only", metavar="DAILY_RUN_ID", help="只重建既有每日 run 的最終交付，不執行資料準備或 Agent")
    parser.add_argument("--skip-data", action="store_true", help="沿用既有 Snapshot 與帳戶快照，不執行 ./start.sh daily")
    parser.add_argument("--force", action="store_true", help="休市日（週末或官方開休市日期表列出者）或當日已完成仍執行")
    parser.add_argument(
        "--resume", nargs="?", const=RESUME_LATEST, metavar="RUN_ID",
        help="沿用先前已通過驗證的 Agent 輸出續跑（例如中途撞到用量上限）；找到可續跑的 run 時隱含 --skip-data。"
             "不帶值時續跑今天最近一個未完成的 run；帶 RUN_ID（如 daily-20260929）則續跑指定 run，用量上限重置或跨午夜後使用",
    )
    parser.add_argument("--agent-cli", choices=("claude", "codex", "opencode"), default="claude", help="執行子 Agent 的 CLI；預設 claude")
    parser.add_argument("--model", help="子 Agent 模型；預設沿用所選 CLI 設定")
    parser.add_argument(
        "--analyst-model",
        help="技術／基本面／事件分析師批次使用的模型；使用 Claude CLI 時預設 %s，傳 inherit 則沿用 --model；其他 CLI 未指定時沿用 --model。多空、交易與現金姿態不受影響" % DEFAULT_CLAUDE_ANALYST_MODEL,
    )
    parser.add_argument(
        "--technical-gate", action=argparse.BooleanOptionalAction, default=True,
        help="趨勢一致的股票技術 brief 只附 momentum、不附逐日歷史（預設開啟；--no-technical-gate 改回全部附完整歷史）",
    )
    parser.add_argument(
        "--max-workers", type=int, default=6, choices=range(1, 9), metavar="N",
        help="分析師、多空研究與交易階段同時執行的子 Agent 數（1～8，預設 6；1 為串行）。同時呼叫越多越容易撞到用量上限",
    )
    parser.add_argument("--preflight-only", action="store_true", help="只檢查 Docker、所選 Agent CLI 登入、帳戶、必要檔案與磁碟")
    parser.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_BYTES / 1024 ** 3, help="執行前檢查要求的最低可用磁碟空間（GiB）")
    parser.add_argument("--max-budget-usd", type=float, help="Claude CLI 單次子 Agent 的估算用量上限；預設 3 美元")
    args = parser.parse_args()
    if args.analyst_model == "inherit":
        args.analyst_model = None
    elif args.analyst_model is None and args.agent_cli == "claude":
        args.analyst_model = DEFAULT_CLAUDE_ANALYST_MODEL
    if args.agent_cli != "claude" and args.max_budget_usd is not None:
        parser.error("--max-budget-usd 只適用 Claude CLI")
    if args.max_budget_usd is not None and (not math.isfinite(args.max_budget_usd) or args.max_budget_usd <= 0):
        parser.error("--max-budget-usd 必須是大於 0 的有限數值")
    if args.delivery_only:
        try:
            delivery = deliver_saved_run(ROOT, args.delivery_only)
            log(json.dumps(delivery, ensure_ascii=False))
            return 0 if delivery["report_status"] == "ready" else 1
        except (ValueError, OSError, KeyError) as error:
            log("交付續跑失敗：%s" % error)
            return 1
    perception_bundle, perception_result = args.perception if args.perception else (None, None)

    now = datetime.now(TAIPEI_TIMEZONE)
    runs_root = ROOT / "artifacts" / "daily"
    runs_root.mkdir(parents=True, exist_ok=True)
    lock_handle = (runs_root / ".lock").open("a+b")
    try:
        if fcntl is not None:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            lock_handle.seek(0)
            if not lock_handle.read(1):
                lock_handle.write(b"0")
                lock_handle.flush()
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log("另一個每日流程正在執行，本次略過。")
        return 0
    min_free_bytes = int(args.min_free_gb * 1024 ** 3)
    if args.preflight_only:
        checks = run_preflight(ROOT, args.account_id, args.skip_data, min_free_bytes=min_free_bytes, agent_cli=args.agent_cli)
        for check in checks:
            log("%s %s：%s" % ("通過" if check.ok else "失敗", check.name, check.detail))
        return 0 if all(check.ok for check in checks) else 1
    captures_root = ROOT / "artifacts" / "data" / "trading_status"
    if not args.force:
        ensure_calendar_capture(captures_root, now)
        refresh_inferred_closures(captures_root, now)
        try:
            if not trading_day_check(captures_root, now):
                log("%s 依官方開休市日曆休市，不執行（--force 可強制）。" % now.date().isoformat())
                return 0
        except (TradingCalendarError, OSError, ValueError) as error:
            log("無法判定今天是否為交易日，停止：%s" % error)
            try:
                save_failure(ROOT / "artifacts/reports", str(error),
                    "calendar-%s" % now.strftime("%Y%m%dT%H%M%S"),
                    reports_root=ROOT / "artifacts/latest", started_at=now.isoformat())
            except (ValueError, OSError) as publish_error:
                log("交付診斷發布失敗：%s" % publish_error)
            notify("ETF Agent 每日決策", "失敗：無法判定交易日：%s" % str(error)[:100])
            return 1
    run_id = "daily-%s" % now.strftime("%Y%m%d")
    run_dir = daily_run_path(runs_root, run_id)
    summary_path = run_dir / "pipeline.json"
    if summary_path.exists() and not args.force:
        previous = json.loads(summary_path.read_text(encoding="utf-8"))
        if previous.get("status") == "completed":
            log("%s 已完成（%s），不重跑。" % (run_id, previous.get("decision_status")))
            return 0
    if args.resume not in (None, RESUME_LATEST):
        try:
            resumable = named_resumable_run(runs_root, args.resume)
        except RuntimeError as error:
            log("無法續跑：%s" % error)
            return 1
    else:
        resumable = latest_resumable_run(runs_root, run_id) if args.resume else None
    if resumable is not None:
        previous_cli = resumable_agent_cli(resumable)
        if previous_cli != args.agent_cli:
            log("無法續跑：原 run 使用 %s，這次選擇 %s" % (previous_cli, args.agent_cli))
            return 1
        # 續跑沿用最近一個未完成且已有 Agent 輸出的 run 目錄，否則新目錄中沒有可沿用的輸出。
        run_id = resumable.name
        run_dir = resumable
        summary_path = run_dir / "pipeline.json"
        log("續跑沿用 run 目錄：%s" % run_id)
        # 可續跑的 run 已有 Agent 輸出，代表 Snapshot 與帳戶快照已建立；重抓資料會讓快照與已驗證輸出對不上。
        args.skip_data = True
    elif args.force and run_dir.exists():
        run_id = "%s-%s" % (run_id, now.strftime("%H%M%S"))
        run_dir = daily_run_path(runs_root, run_id)
        summary_path = run_dir / "pipeline.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    decision_run_id = "decision-%s" % stamp
    if resumable is not None:
        # Agent 任務名與 envelope 都含 decision run ID；續跑必須沿用同一個，已驗證輸出才對得上。
        decision_run_id = resumed_decision_run_id(run_dir) or decision_run_id
    original_start = json.loads(summary_path.read_text(encoding="utf-8"))["started_at"] if resumable is not None else now.isoformat()
    summary = {"run_id": run_id, "started_at": original_start, "status": "running", "planned_decision_run_id": decision_run_id, "agent_cli": args.agent_cli}
    if resumable is not None:
        summary["resumed_at"] = now.isoformat()
    if not args.skip_data:
        trade_date = expected_latest_trade_date(captures_root, now)
        if data_already_fetched(
            ROOT / "artifacts" / "data" / "research_snapshot_latest.json",
            ROOT / "artifacts" / "ledger" / args.account_id / "account_snapshot_latest.json", trade_date,
        ):
            log("已有 %s 的行情資料（Snapshot 150 檔齊全），沿用既有 Snapshot 與帳戶快照，不重抓。" % trade_date.isoformat())
            summary["data_reused_trade_date"] = trade_date.isoformat()
            args.skip_data = True
    write_json(summary_path, summary)

    try:
        # 在抓資料與呼叫任何子 Agent 前確認環境；一次列出全部失敗項目。
        # 全新帳戶自動開帳；已有帳本只驗證並沿用，不會重建。失敗時交給下方預檢一併回報。
        opened = subprocess.run(
            [sys.executable, str(ROOT / "cli/virtual_account.py"), "--account-id", args.account_id, "ensure-init"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        if opened.returncode != 0:
            log("帳戶自動開帳未成功：%s" % (opened.stderr or opened.stdout).strip()[-300:])
        log("[預檢] Docker、%s 登入、帳戶、必要檔案、磁碟空間" % args.agent_cli)
        checks = run_preflight(ROOT, args.account_id, args.skip_data, min_free_bytes=min_free_bytes, agent_cli=args.agent_cli)
        summary["preflight"] = checks_to_dict(checks)
        require_preflight(checks)
        snapshot_path = ROOT / "artifacts" / "data" / "research_snapshot_latest.json"
        account_path = ROOT / "artifacts" / "ledger" / args.account_id / "account_snapshot_latest.json"
        if not args.skip_data:
            # 交易狀態 CSV 必須在 Snapshot 固定 cutoff 之前封存，available_at 才不會晚於 cutoff。
            log("[0/4] 封存交易狀態與開休市政府開放 CSV")
            try:
                capture = capture_trading_sources(captures_root)
                summary["trading_status_capture"] = capture.get("run_id")
            except (OSError, ValueError, json.JSONDecodeError) as error:
                summary["trading_status_capture_error"] = str(error)
                log("交易狀態封存失敗，改用 cutoff 前最新的成功封存：%s" % error)
            log("[1/4] ./start.sh daily：資料、Snapshot、虛擬帳本")
            try:
                run_data_stage(args.account_id, "prepare-%s" % stamp, trade_date)
            finally:
                refresh_inferred_closures(captures_root, datetime.now(TAIPEI_TIMEZONE))
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

        log("[3/4] 決策鏈：分析團隊 → 多空研究 → 交易 → 風險（子 Agent 經 %s）" % args.agent_cli)
        def make_runner(model):
            return (ClaudeAgentRunner(ROOT, model=model, max_budget_usd=args.max_budget_usd or 3.0)
                    if args.agent_cli == "claude" else
                    CodexAgentRunner(ROOT, model=model) if args.agent_cli == "codex" else
                    OpenCodeAgentRunner(ROOT, model=model))

        agent_runner = make_runner(args.model)
        analyst_runner = make_runner(args.analyst_model) if args.analyst_model else None
        pipeline = DailyDecisionPipeline(
            ROOT, run_dir,
            agent_runner,
            ROOT / "artifacts" / "decisions",
            log=log,
            resume=args.resume is not None,
            analyst_runner=analyst_runner,
            technical_gate=args.technical_gate,
            max_workers=args.max_workers,
        )
        result = pipeline.run(
            snapshot_path, account_path, trading_status_path,
            ROOT / "config" / "decision_rules.json", ROOT / "var" / "etf_agent.db",
            ROOT / "config" / "decision_policy.json", ROOT / "data" / "sector_classification.json",
            decision_run_id,
            perception_bundle_path=perception_bundle, perception_result_path=perception_result,
        )
        summary.update(
            {
                "decision_status": result.decision_status,
                "decision_run_id": result.decision_run_id,
                "cash_stance": result.cash_stance,
                "order_count": len(result.orders),
                "agent_calls": result.agent_calls,
                "max_workers": args.max_workers,
                "stage_durations": result.stage_durations,
                "agent_estimated_usage_usd": round(sum(call["estimated_usage_usd"] for call in result.agent_calls), 4),
                "target_session": session,
            }
        )
        if result.status != "completed":
            raise RuntimeError("決策鏈失敗：%s" % "；".join(result.errors))
        summary["event_analysis_mode"] = "integrated_analyst"
        superseded = supersede_decision_runs(
            ROOT / "artifacts" / "decisions", result.decision_run_id,
            ROOT / "artifacts" / "archive" / "superseded_decisions",
        )
        if superseded:
            summary["superseded_decision_runs"] = superseded
            log("新決策取代同一份帳戶快照的舊決策，舊 run 已移到 artifacts/archive/superseded_decisions：%s" % "、".join(superseded))
        log("[交付] 重建風控後最終中文報告")
        summary["delivery"] = build_delivery(
            ROOT / "artifacts/decisions", result.decision_run_id,
            ROOT / "artifacts/reports", reports_root=ROOT / "artifacts/latest",
            started_at=summary["started_at"],
        )
        if summary["delivery"]["report_status"] != "ready":
            raise RuntimeError("最終報告重建失敗，詳見交付診斷")
        summary["status"] = "completed"
        summary["finished_at"] = datetime.now(TAIPEI_TIMEZONE).isoformat()
        if result.decision_status not in {"approved", "no_trade"}:
            summary["dplan"] = {
                "status": "blocked",
                "error": "決策狀態為 %s，只有 approved/no_trade 可產生 D-Plan。" % result.decision_status,
            }
            log("[D-Plan] 未產生：%s" % summary["dplan"]["error"])
        else:
            # D-Plan 綁定本次封存決策、prepare-day 帳本與 target_session；
            # 不接受使用者另外手抄 run ID 來繞過一致性檢查。
            try:
                summary["dplan"] = DPlanExportService(ROOT).export(
                    decision_repository=ROOT / "artifacts" / "decisions",
                    decision_run_id=result.decision_run_id,
                    daily_run=summary,
                    profile_path=ROOT / "config" / "competition_profile.json",
                    account_id=args.account_id,
                    account_run_id=account_run_id,
                )
                log("[D-Plan] 已產生：%s" % summary["dplan"]["output"])
            except (DPlanError, OSError, ValueError, TypeError, KeyError) as error:
                # 候選交件檔失敗不得否定已封存決策，也不得阻止中文報告重建。
                summary["dplan"] = {"status": "blocked", "error": str(error)}
                log("[D-Plan] 未產生：%s" % error)
    except Exception as error:  # noqa: BLE001 — 任何失敗都要寫入摘要並通知
        summary["status"] = "failed"
        summary["error"] = str(error)
        log("失敗：%s" % error)
        if "delivery" not in summary:
            try:
                summary["delivery"] = save_failure(
                    ROOT / "artifacts/reports", str(error), run_id,
                    reports_root=ROOT / "artifacts/latest", started_at=summary["started_at"],
                )
            except (ValueError, OSError) as publish_error:
                summary["delivery_publish_error"] = str(publish_error)
                log("交付診斷發布失敗：%s" % publish_error)
    summary["calibration"] = record_calibration()
    summary.setdefault("finished_at", datetime.now(TAIPEI_TIMEZONE).isoformat())
    write_json(summary_path, summary)

    log("[4/4] 通知")
    if summary["status"] == "completed":
        message = "決策 %s，%d 筆委託，現金姿態 %s，CLI %s" % (
            summary.get("decision_status"), summary.get("order_count", 0),
            summary.get("cash_stance"), args.agent_cli,
        )
        if args.agent_cli == "claude":
            message += "，Agent 估算用量 $%s（訂閱不計費）" % summary.get("agent_estimated_usage_usd")
    else:
        message = "失敗：%s" % str(summary.get("error"))[:120]
    if summary.get("delivery"):
        message += "；報告 %s；D-Plan %s" % (
            summary["delivery"]["report_status"], summary.get("dplan", {}).get("status", "not_run"),
        )
    notify("ETF Agent 每日決策", message)
    if summary.get("delivery"):
        dplan = summary.get("dplan", {})
        detail = dplan.get("output") if dplan.get("status") == "ready" else dplan.get("error", "not_run")
        log("最終報告：%s；D-Plan：%s（%s）" % (
            summary["delivery"]["report_path"], dplan.get("status", "not_run"), detail,
        ))
    log(message)
    log("摘要：%s" % summary_path)
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
