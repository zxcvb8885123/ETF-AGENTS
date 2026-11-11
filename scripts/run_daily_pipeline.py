#!/usr/bin/env python3
"""每日一鍵：資料 → 決策子 Agent → 封存 Decision run → 最終中文交付 → 通知。

不自動下單或送件；產出的是供人工檢視的封存決策。設計給 launchd 平日定時執行，
也可手動執行。同一台北日期已完成時不重跑（--force 可強制）。
"""

import argparse
import shutil
import sqlite3
try:
    import fcntl
except ImportError:  # Windows 以 msvcrt 鎖定同一執行鎖。
    fcntl = None
import json
import math
import os
import re
import subprocess
import time as time_module
import sys
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.decision_delivery import build_delivery, save_failure
from etf_agent.decision_delivery.repository import publication_lock
from etf_agent.competition import DataPackError, DPlanError, DPlanExportService, find_datapack, load_datapack, load_universe, rebase_account_to_datapack, reconcile
from etf_agent.competition.datapack import DEFAULT_CASH_TOLERANCE_RATE
from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository, active_account_id

from etf_agent.automation import local_notify  # noqa: E402
from etf_agent.automation.daily_pipeline import (  # noqa: E402
    ClaudeAgentRunner,
    CodexAgentRunner,
    OpenCodeAgentRunner,
    DailyDecisionPipeline,
)
from etf_agent.automation.preflight import (  # noqa: E402
    DEFAULT_MIN_FREE_BYTES,
    agent_cli_versions,
    checks_to_dict,
    effective_research_agent_cli,
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
from etf_agent.data.supplemental_sources import SupplementalRepository  # noqa: E402
from etf_agent.data import MarketDataDatabase, UniverseLoader  # noqa: E402
from etf_agent.data.inferred_closures import update_inferred_closed_days  # noqa: E402
from etf_agent.data.trading_calendar import DECISION_DEADLINE  # noqa: E402
from etf_agent.data.source_capture import capture_ogd_candidates, capture_with_retry  # noqa: E402
from etf_agent.core.run_layout import day_from_run_id, find_run_dir, list_run_dirs, trade_date_from_session_start  # noqa: E402
from etf_agent.core.timeutil import parse_aware_time  # noqa: E402
from etf_agent.data.evidence import TAIPEI_TIMEZONE  # noqa: E402
from etf_agent.virtual_account.daily import (  # noqa: E402
    archive_decision_run,
    pending_decision,
    restore_superseded_decision,
    supersede_decision_runs,
)

DEFAULT_CLAUDE_MODEL = "opus"
# 分析師批次逐則分級大量重大訊息，opus 實測漏分級且單批用量約 sonnet 兩倍，維持 sonnet。
DEFAULT_CLAUDE_ANALYST_MODEL = "sonnet"
# 多頭／空頭逐檔寫論點、讀得多，opus 用量約 sonnet 2.5 倍；opus 只留給最後拍板的交易與風險。
DEFAULT_CLAUDE_RESEARCH_MODEL = "sonnet"
# 決策鏈失敗後自動續跑一次的最短剩餘時間：完整鏈約 22 分鐘，續跑沿用已驗證輸出，通常更短。
AUTO_RESUME_MIN_REMAINING = timedelta(minutes=25)
# 額度用完要等重置、安全拒絕不該重試：這些錯誤續跑無效。
NON_RESUMABLE_MARKERS = ("session limit", "usage limit", "hit your", "rate limit", "秘密", "未核准工具")
RESUME_LATEST = "latest"  # --resume 不帶 RUN_ID：續跑今天最近一個未完成的 run
NEWS_CATCHUP_TIMEOUT_SECONDS = 900  # 個股新聞補抓上限；逐股 150 檔約數分鐘，卡住不拖垮每日流程
DATA_READY_TIME = time(15, 0)  # 交易日 15:00 後當日行情才算齊全（yfinance 等來源收盤後才有）
TRADE_DATE_LOOKBACK_DAYS = 14  # 往前找最近交易日的上限，避免日曆缺漏時無窮迴圈
RECENT_RUN_WINDOW = timedelta(hours=1)  # 同時段的 run 在此時間內完成才略過；更早的（例如半夜手動跑）照常重做
LOG_RETENTION_DAYS = 30  # var/logs/daily-YYYY-MM-DD.log（台北日期，一天一檔）的保留天數
DAILY_LOG_PATTERN = re.compile(r"daily-(\d{4}-\d{2}-\d{2})\.log")
_log_file = None  # open_daily_log() 開啟後，log() 同時寫終端與此檔


def daily_logs_root() -> Path:
    """本腳本自己的日誌資料夾（`.gitignore` 已排除 `var/logs/`）；launchd 的 stdout／stderr 也由樣板導到這裡。"""
    return ROOT / "var" / "logs"


def log(message: str) -> None:
    line = "[%s] %s" % (datetime.now(TAIPEI_TIMEZONE).strftime("%H:%M:%S"), message)
    print(line, flush=True)
    if _log_file is not None:
        try:
            _log_file.write(line + "\n")
            _log_file.flush()
        except (OSError, ValueError):
            pass  # 日誌檔寫不進去不影響決策流程；終端／launchd 仍有輸出


def open_daily_log(logs_root: Path, now: datetime):
    """開啟 ``logs_root/daily-<台北日期>.log``（附加模式）讓 ``log()`` 同步寫入；回傳路徑，開不了時回 None 並只印到終端。

    跨午夜的 run 仍寫在開始當天的檔案；``start.sh daily`` 等子程序的輸出不經過 ``log()``，仍只到終端或 launchd 日誌。"""
    global _log_file
    close_daily_log()
    path = logs_root / ("daily-%s.log" % now.astimezone(TAIPEI_TIMEZONE).strftime("%Y-%m-%d"))
    try:
        logs_root.mkdir(parents=True, exist_ok=True)
        _log_file = path.open("a", encoding="utf-8")
    except OSError as error:
        _log_file = None
        print("[日誌] 無法開啟 %s，只輸出到終端：%s" % (path, error), flush=True)
        return None
    return path


def close_daily_log() -> None:
    global _log_file
    if _log_file is not None:
        try:
            _log_file.close()
        except OSError:
            pass
        _log_file = None


def prune_daily_logs(logs_root: Path, now: datetime, keep_days: int = LOG_RETENTION_DAYS) -> list:
    """刪除檔名完全符合 ``daily-YYYY-MM-DD.log`` 且日期早於保留天數的舊日誌；其他檔案（含 launchd.log）一律不動。"""
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 0:
        raise ValueError("keep_days 必須是非負整數")
    cutoff = now.astimezone(TAIPEI_TIMEZONE).date() - timedelta(days=keep_days)
    removed = []
    try:
        candidates = sorted(logs_root.iterdir())
    except OSError:
        return removed
    for path in candidates:
        match = DAILY_LOG_PATTERN.fullmatch(path.name)
        if match is None or not path.is_file() or path.is_symlink():
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                continue
    return removed


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def notify(title: str, message: str) -> bool:
    """本機通知（macOS `display notification`，不用會阻塞的 `display alert`／`display dialog`）。

    通知只是提醒：osascript 不存在、逾時、非零結束（含通知權限被拒）都只記日誌、不拋出，不影響 run 的結果與結束碼。"""
    return local_notify.notify(title, message, log=log)


def record_run_status(status: str, *, trade_date=None, run_id=None, detail: str = "", now=None, logs_root=None) -> "Path | None":
    """把這次 run 的最終狀態附加一行到 var/logs/daily_status.log；寫不進去只記日誌，不影響 run。

    status 取值：approved／no_trade／rejected／blocked／fallback／failed，略過時為 skipped。"""
    if status not in local_notify.RUN_STATUSES:
        raise ValueError("未定義的最終狀態：%s" % status)
    now = now or datetime.now(TAIPEI_TIMEZONE)
    line = local_notify.format_status_line(now, local_notify.classify_run_type(now), status, trade_date, run_id, detail)
    return local_notify.append_status_line(logs_root or daily_logs_root(), line, log=log)


def final_run_status(summary: dict) -> str:
    """由 pipeline.json 摘要算出對外的單一最終狀態。

    approved／no_trade：決策完成且 D-Plan ready；fallback：備援 D-Plan 已產生；rejected：決策被拒且沒有備援；
    blocked：決策完成但 D-Plan 被擋且沒有備援；failed：流程失敗且沒有備援。"""
    dplan_status = (summary.get("dplan") or {}).get("status")
    if dplan_status == "fallback":
        return "fallback"
    if summary.get("status") != "completed":
        return "failed"
    decision = summary.get("decision_status")
    if decision == "rejected":
        return "rejected"
    if dplan_status == "ready" and decision in {"approved", "no_trade"}:
        return decision
    return "blocked"


def finish_notification(summary: dict, args) -> str:
    """結束時的通知文字：最終狀態、委託數、姿態、D-Plan 路徑或未產生原因、備援與還原提示。"""
    status = final_run_status(summary)
    dplan = summary.get("dplan") or {}
    if summary.get("status") == "completed":
        message = "最終狀態 %s；決策 %s，%d 筆委託，現金姿態 %s，持股廣度 %s，CLI %s" % (
            status, summary.get("decision_status"), summary.get("order_count", 0),
            summary.get("cash_stance"), summary.get("position_breadth") or "-", args.agent_cli,
        )
        if "claude" in (args.agent_cli, args.research_agent_cli):
            message += "，Agent 估算用量 $%s（訂閱不計費）" % summary.get("agent_estimated_usage_usd")
    else:
        message = "最終狀態 %s；失敗：%s" % (status, str(summary.get("error"))[:120])
    if status == "fallback":
        message += "；備援 D-Plan（全部續抱、無委託，程式產生）已存為 %s，請人工決定是否上傳" % dplan.get("output")
        if not dplan.get("main_replaced"):
            message += "（主檔仍是正式決策，備援只在 versions/）"
    elif dplan.get("fallback", {}).get("status") == "not_generated":
        message += "；備援 D-Plan 也未產生：%s" % str(dplan["fallback"].get("error"))[:160]
    if summary.get("rolled_back"):
        message += "；已還原重跑前的備案決策，請上傳原本的 D-Plan 主檔（latest 報告仍是本次內容）"
    elif summary.get("rollback_error"):
        message += "；還原重跑前的決策失敗，請人工檢查帳本"
    if summary.get("delivery") and status not in {"fallback"}:
        message += "；報告 %s；D-Plan %s" % (summary["delivery"]["report_status"], dplan.get("status", "not_run"))
    return message


def status_detail(summary: dict) -> str:
    """狀態列的最後一欄：有 D-Plan 時是路徑，否則是未產生的原因。"""
    dplan = summary.get("dplan") or {}
    if dplan.get("status") in {"ready", "fallback"}:
        return str(dplan.get("output"))
    reasons = [str(summary.get("error") or dplan.get("error") or "")]
    if dplan.get("fallback", {}).get("status") == "not_generated":
        reasons.append("備援未產生：%s" % dplan["fallback"].get("error"))
    return "；".join(part for part in reasons if part) or "not_run"


def attempt_fallback_dplan(summary: dict, args, session, account_path: Path, snapshot_path: Path, now: datetime) -> None:
    """決策被拒、D-Plan 被擋或流程失敗時，嘗試產生「全部續抱、無委託」的備援 D-Plan 候選；結果寫進 summary["dplan"]。

    不產生的情況都明寫原因：已還原重跑前的備案（原主檔仍有效）、已過交件期限、沒有目標時段、DataPack 未對帳、
    比賽前提不滿足（持股檔數、現金比、單股上限、150 檔、持股與資料包不同）。備援只是本機候選檔，不會上傳。"""
    from etf_agent.competition import FallbackDPlanService

    dplan = summary.get("dplan") or {}
    if dplan.get("status") in {"ready", "fallback"}:
        return
    if summary.get("status") == "completed" and summary.get("decision_status") == "rejected":
        reason, detail = "decision_rejected", "決策被風控拒絕；%s" % (dplan.get("error") or "")
    elif summary.get("status") == "completed":
        reason, detail = "dplan_blocked", str(dplan.get("error") or "D-Plan 未產生")
    else:
        reason, detail = "pipeline_failed", str(summary.get("error") or "每日流程失敗")
    summary.setdefault("dplan", {"status": "blocked", "error": detail})
    if summary.get("rolled_back"):
        summary["dplan"]["fallback"] = {"status": "not_generated", "error": "已還原重跑前的備案決策，原 D-Plan 主檔仍可上傳，不需備援"}
        log("[備援] 不產生：已還原重跑前的備案決策")
        return
    if session is None or not isinstance(summary.get("datapack"), dict):
        summary["dplan"]["fallback"] = {"status": "not_generated", "error": "流程在目標時段或 DataPack 對帳前就失敗，缺少產生備援所需的輸入"}
        log("[備援] 不產生：缺少目標時段或 DataPack 對帳結果")
        notify("ETF Agent 備援 D-Plan", "未產生備援：流程在 DataPack 對帳前失敗，今日沒有可交的 D-Plan，請人工處理")
        return
    if session_deadline_passed(session, now):
        summary["dplan"]["fallback"] = {"status": "not_generated", "error": "已過目標時段的交件期限，備援 D-Plan 無法繳交"}
        log("[備援] 不產生：已過交件期限")
        return
    result = FallbackDPlanService(ROOT).generate(
        daily_run=dict(summary, target_session=session), account_path=account_path, snapshot_path=snapshot_path,
        profile_path=ROOT / "config" / "competition_profile.json", account_id=args.account_id,
        reason=reason, detail=detail, now=now,
    )
    if result.get("status") == "fallback":
        summary["dplan"] = dict(result, original={k: dplan.get(k) for k in ("status", "error") if k in dplan})
        log("[備援] 已產生備援 D-Plan（全部續抱、無委託，程式產生，未上傳）：%s" % result["output"])
        if not result.get("main_replaced"):
            log("[備援] 主檔是正式決策的 D-Plan，備援只存在 versions/，請人工決定要交哪一份")
    else:
        summary["dplan"]["fallback"] = result
        log("[備援] 未產生備援 D-Plan：%s" % result.get("error"))
        notify("ETF Agent 備援 D-Plan", "未產生備援 D-Plan：%s" % str(result.get("error"))[:160])


LOCK_STALE_HOURS = 6


def acquire_run_lock(handle) -> bool:
    """取得每日流程鎖；鎖被別的流程佔用回 False，其他 OSError（權限、磁碟）往上拋。"""
    if fcntl is not None:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    import errno
    import msvcrt
    handle.seek(0)
    if not handle.read(1):
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EDEADLK, errno.EAGAIN):
            return False
        raise
    return True


def write_lock_holder(handle, started_at: datetime) -> None:
    """鎖檔寫入持有者的 pid 與開始時間，供鎖被佔用時辨識是否為殭屍流程；Windows 鎖住的是第 0 個位元組，不改內容。"""
    if fcntl is None:
        return
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({"pid": os.getpid(), "started_at": started_at.isoformat()}).encode("utf-8"))
    handle.flush()


def describe_lock_holder(path: Path, now: datetime) -> str:
    """讀鎖檔裡的持有者資訊；超過 LOCK_STALE_HOURS 小時仍未結束視為疑似殭屍並明講。讀不到時回空字串，不影響略過流程。"""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        started = parse_aware_time(info["started_at"], "started_at")
        pid = int(info["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    hours = (now - started).total_seconds() / 3600
    text = "持有者 pid %d，自 %s 起（約 %.1f 小時）。" % (pid, started.astimezone(TAIPEI_TIMEZONE).strftime("%m-%d %H:%M"), hours)
    if hours >= LOCK_STALE_HOURS:
        text += "超過 %d 小時仍未結束，疑似卡住的殭屍流程，請人工檢查。" % LOCK_STALE_HOURS
    return text


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


def ensure_company_news(now: datetime) -> dict:
    """個股新聞只由 20:40 排程抓；當天沒抓齊（逐股成功次數少於交易池）就在建 Snapshot 前補抓一次。

    新聞的 available_at 是抓取時間，必須早於 Snapshot cutoff 才進得了本次決策。補抓失敗只記錄、
    不中止：事件分析師會照舊只看重大訊息，與沒有核准個股新聞時相同（不填補假值）。"""
    day = now.astimezone(TAIPEI_TIMEZONE).date()
    try:
        universe = len(UniverseLoader().load(ROOT / "data" / "official_universe.csv"))
        repository = SupplementalRepository(MarketDataDatabase(ROOT / "var" / "etf_agent.db"))
        done = repository.count_collected_days("FINMIND_NEWS", day, TAIPEI_TIMEZONE)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        return {"status": "check_failed", "error": str(error)}
    if done >= universe:
        return {"status": "already_collected", "collected": done}
    log("個股新聞今天只抓到 %d／%d 檔，建 Snapshot 前補抓" % (done, universe))
    try:
        completed = subprocess.run([str(ROOT / "scripts" / "run_finmind_news.sh"), day.isoformat()],
                                   cwd=ROOT, check=False, timeout=NEWS_CATCHUP_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "failed", "collected_before": done, "error": str(error)}
    if completed.returncode != 0:
        return {"status": "failed", "collected_before": done, "exit_code": completed.returncode}
    return {"status": "caught_up", "collected_before": done}


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


def refresh_before_deadline(captures_root: Path, snapshot_path: Path, now: datetime) -> bool:
    """既有 Snapshot 與此刻新建的 Snapshot 是否排到同一個交易時段（仍在交件期限 08:55 前）。

    是的話應重建 Snapshot：日線由 start.sh 判斷已齊全而略過，重大訊息、新聞與恐懼貪婪等照常
    重抓，cutoff 改為此刻；帳本會把該時段尚未執行的舊決策移到封存區。已過期限、日曆缺漏或讀取
    失敗時回 False，維持沿用既有 Snapshot。
    """
    try:
        capture = latest_calendar_capture_before(captures_root, now.isoformat())
        if capture is None:
            return False
        calendar = calendar_from_capture(capture)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        return calendar.next_trading_session(snapshot["decision_cutoff"]) == calendar.next_trading_session(now.isoformat())
    except (TradingCalendarError, OSError, ValueError, KeyError, TypeError):
        return False


def daily_run_path(runs_root: Path, run_id: str, day_hint: "str | None" = None) -> Path:
    """每日 run 放在 <runs_root>/<台北日期>/<run_id>；已存在則沿用其位置。

    新建時優先用 day_hint（目標交易日，跟 D-Plan 一致），缺少才退回 run_id 內的執行日期。"""
    existing = find_run_dir(runs_root, run_id, RuntimeError)
    if existing is not None:
        return existing
    day = day_hint or day_from_run_id(run_id)
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
        # 已還原的 run，其 Agent 輸出綁定已移走的帳本狀態，不能續跑。
        if previous.get("rolled_back"):
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
    if previous.get("rolled_back"):
        raise RuntimeError("%s 已還原成重跑前的帳本與決策，不能續跑；請改用 --force 重新執行" % name)
    if not any(run_dir.glob("*_raw_*.json")):
        raise RuntimeError("%s 沒有已保存的 Agent 輸出，無可沿用內容" % name)
    return run_dir


def resumable_agent_cli(run_dir: Path) -> str:
    """續跑必須沿用該 run 已記錄的執行器；舊版摘要預設為 Claude。"""
    previous = json.loads((run_dir / "pipeline.json").read_text(encoding="utf-8"))
    return previous.get("agent_cli", "claude")


def resumable_research_agent_cli(run_dir: Path) -> str:
    """續跑時多空研究員使用的執行器；舊版摘要沒有此欄位，視為與主執行器相同。"""
    previous = json.loads((run_dir / "pipeline.json").read_text(encoding="utf-8"))
    return previous.get("research_agent_cli") or previous.get("agent_cli", "claude")


def make_agent_runner(cli: str, model, max_budget_usd=None):
    """依 CLI 名稱建立子 Agent 執行器。"""
    if cli == "claude":
        return ClaudeAgentRunner(ROOT, model=model, max_budget_usd=max_budget_usd or 3.0)
    if cli == "codex":
        return CodexAgentRunner(ROOT, model=model)
    return OpenCodeAgentRunner(ROOT, model=model)


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


CAPTURE_RETRY_MARGIN = timedelta(hours=1)  # 重試最晚停在交件期限前一小時，留時間跑決策鏈


def capture_retry_stop_at(now: datetime) -> datetime:
    """下一個 08:55 交件期限前一小時；重抓資料最晚做到這個時間。"""
    deadline = datetime.combine(now.astimezone(TAIPEI_TIMEZONE).date(), DECISION_DEADLINE, TAIPEI_TIMEZONE)
    if now >= deadline - CAPTURE_RETRY_MARGIN:
        deadline += timedelta(days=1)
    return deadline - CAPTURE_RETRY_MARGIN


def ensure_calendar_capture(captures_root: Path, now: datetime) -> bool:
    """還沒有開休市日期表（新環境或資料夾被清掉）就先去抓；抓不到則照原本流程報錯停止。"""
    try:
        if latest_calendar_capture_before(captures_root, now.isoformat()) is not None:
            return False
        log("找不到封存的開休市日期表，先抓政府開放 CSV")
        capture_trading_sources(captures_root)
        return True
    except (TradingCalendarError, OSError, ValueError) as error:
        log("政府開放 CSV 抓取失敗：%s" % error)
        return False


def refresh_inferred_closures(captures_root: Path, now: datetime) -> None:
    """用資料庫的官方行情缺口推斷官方日期表沒列的臨時休市日（如颱風假）並保存證據；失敗只記錄，不中斷。"""
    try:
        capture = latest_calendar_capture_before(captures_root, now.isoformat())
        database = ROOT / "var" / "etf_agent.db"
        if capture is None or not database.exists():
            return
        added = update_inferred_closed_days(database, ROOT / "var", calendar_from_capture(capture), now)
        if added:
            log("依官方行情缺口，推斷臨時休市日：%s（證據：%s）" % ("、".join(added), ROOT / "var" / "inferred_closed_days.json"))
            notify("ETF Agent 每日決策", "推斷臨時休市：%s" % "、".join(added))
    except (TradingCalendarError, OSError, ValueError) as error:
        log("臨時休市推斷未執行：%s" % error)


def trading_day_check(captures_root: Path, now: datetime) -> bool:
    """依 now 前最近封存的官方開休市日曆判斷此刻是否該跑；日曆缺漏或未涵蓋今年即拋錯。

    今天是交易日就跑。今天休市時，DATA_READY_TIME 後的晚間場是替隔天做備案，隔天是交易日也要跑
    （例如補假那晚先做好隔天的備案，早上還能再重做一次）。
    """
    capture = latest_calendar_capture_before(captures_root, now.isoformat())
    if capture is None:
        raise TradingCalendarError("找不到封存的官方開休市日期表（%s）" % captures_root)
    calendar = calendar_from_capture(capture)
    if calendar.is_trading_day(now.date()):
        return True
    return now.time() >= DATA_READY_TIME and calendar.is_trading_day(now.date() + timedelta(days=1))


def upcoming_session_start(captures_root: Path, now: datetime):
    """此刻做決策會排到的交易時段開盤時間；日曆缺漏或判斷失敗回 None。"""
    try:
        capture = latest_calendar_capture_before(captures_root, now.isoformat())
        if capture is None:
            return None
        return calendar_from_capture(capture).next_trading_session(now.isoformat())["start"]
    except (TradingCalendarError, OSError, ValueError):
        return None


def finished_recently(summary: dict, now: datetime) -> bool:
    """run 是否在 ``RECENT_RUN_WINDOW`` 內完成；沒有可解析的完成時間時保守視為剛完成。"""
    value = summary.get("finished_at") or summary.get("started_at")
    try:
        finished = datetime.fromisoformat(str(value))
    except ValueError:
        return True
    if finished.tzinfo is None:
        return True
    return now - finished <= RECENT_RUN_WINDOW


def completed_run_for_session(runs_root: Path, run_id: str, session_start, now: datetime):
    """同一台北日期、目標時段相同且剛完成的 run；回傳 run 名稱，沒有則回 None。

    早上的 run 為當天時段、晚上的 run 為下一時段，兩者同日期但不算重複。``session_start`` 為
    None（日曆缺漏）或舊 run 沒記錄目標時段時，視同相同時段。同時段但完成已超過
    ``RECENT_RUN_WINDOW``（例如半夜手動跑過）不算，排程照常用新資料重做；舊版保留不刪。
    """
    for run_dir in list_run_dirs(runs_root):
        if run_dir.name != run_id and not run_dir.name.startswith(run_id + "-"):
            continue
        summary_path = run_dir / "pipeline.json"
        if not summary_path.exists():
            continue
        previous = json.loads(summary_path.read_text(encoding="utf-8"))
        if previous.get("status") != "completed" or previous.get("rolled_back"):
            continue
        previous_start = (previous.get("target_session") or {}).get("start")
        same_session = session_start is None or previous_start is None or previous_start == session_start
        if same_session and finished_recently(previous, now):
            return run_dir.name, previous.get("decision_status")
    return None


ROLLBACK_SNAPSHOT = "rollback_research_snapshot.json"  # 重跑前的 Snapshot 副本，還原時放回 latest


def auto_resume_blocker(errors, deadline, now: datetime) -> "str | None":
    """決策鏈失敗後不該自動續跑的原因；可以續跑時回 None。"""
    text = "；".join(str(error) for error in errors)
    lowered = text.lower()
    for marker in NON_RESUMABLE_MARKERS:
        if marker.lower() in lowered:
            return "錯誤屬額度用完或安全拒絕（%s），續跑無效" % marker
    if deadline is None:
        return "無法判斷交件期限"
    if now + AUTO_RESUME_MIN_REMAINING > deadline:
        return "距交件期限不足 %d 分鐘" % (AUTO_RESUME_MIN_REMAINING.total_seconds() // 60)
    return None


QUOTA_MARKERS = ("session limit", "usage limit", "hit your", "rate limit")
QUOTA_RESET_PATTERN = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE)
QUOTA_RESET_MARGIN = timedelta(minutes=2)  # 重置時間後多等一下再續跑
QUOTA_FALLBACK_WAIT = timedelta(minutes=30)  # 看不懂重置時間時的等待
MAX_QUOTA_WAITS = 3


def quota_wait(errors, now: datetime) -> "timedelta | None":
    """錯誤是額度用完時，回傳該等多久才續跑；不是額度問題回 None。看不懂重置時間就等 30 分鐘。"""
    text = "；".join(str(error) for error in errors)
    if not any(marker in text.lower() for marker in QUOTA_MARKERS):
        return None
    match = QUOTA_RESET_PATTERN.search(text)
    if match is None:
        return QUOTA_FALLBACK_WAIT
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "pm" else 0)
    local = now.astimezone(TAIPEI_TIMEZONE)
    reset = local.replace(hour=hour, minute=int(match.group(2) or 0), second=0, microsecond=0)
    if reset <= local:
        reset += timedelta(days=1)
    return reset - local + QUOTA_RESET_MARGIN


def run_decision_chain(build, run, deadline, now, log, auto_resume=True, sleep=time_module.sleep):
    """跑決策鏈；失敗且 ``auto_resume_blocker`` 允許時，以 resume 沿用已驗證輸出自動續跑一次。

    額度用完（5 小時額度等）時，等到重置時間再 resume，最多 MAX_QUOTA_WAITS 次，且必須來得及在交件期限前跑完。
    ``build(resume)`` 回傳 pipeline，``run(pipeline)`` 回傳 PipelineResult，``now()`` 回傳含時區現在時間。
    回傳 (result, agent_calls, auto_resume)：agent_calls 合併各次呼叫紀錄；auto_resume 為 None（未失敗或關閉）或紀錄。"""
    first = run(build(False))
    if first.status == "completed" or not auto_resume:
        return first, list(first.agent_calls), None
    result, calls, waits, plain_resumes = first, list(first.agent_calls), [], 0
    while result.status != "completed":
        delay = quota_wait(result.errors, now())
        if delay is not None:
            if len(waits) >= MAX_QUOTA_WAITS:
                blocker = "額度等待已達 %d 次" % MAX_QUOTA_WAITS
            elif deadline is None or now() + delay + AUTO_RESUME_MIN_REMAINING > deadline:
                blocker = "額度重置後距交件期限不足 %d 分鐘" % (AUTO_RESUME_MIN_REMAINING.total_seconds() // 60)
            else:
                blocker = None
        elif plain_resumes:
            blocker = "已續跑過一次"
        else:
            blocker = auto_resume_blocker(result.errors, deadline, now())
        if blocker is not None:
            log("[續跑] 不自動續跑：%s" % blocker)
            attempted = bool(waits) or plain_resumes > 0
            return result, calls, {"attempted": attempted, "reason": blocker, "quota_waits": waits}
        if delay is not None:
            waits.append(int(delay.total_seconds()))
            log("[續跑] 額度用完，%d 分鐘後（重置時間）再續跑：%s" % (int(delay.total_seconds() // 60), "；".join(map(str, result.errors))[:200]))
            sleep(delay.total_seconds())
        else:
            plain_resumes += 1
            log("[續跑] 決策鏈失敗，沿用已驗證輸出自動續跑一次：%s" % "；".join(map(str, result.errors))[:300])
        result = run(build(True))
        calls += list(result.agent_calls)
    record = {"attempted": True, "first_errors": [str(error)[:300] for error in first.errors[:5]], "status": result.status}
    if waits:
        record["quota_waits"] = waits
    return result, calls, record


def session_deadline(session):
    """目標時段當天的交件期限（08:55，含時區）；缺少或無法解析開盤時間時回 None。"""
    try:
        start = datetime.fromisoformat(str((session or {}).get("start")))
    except ValueError:
        return None
    if start.tzinfo is None:
        return None
    return datetime.combine(start.astimezone(TAIPEI_TIMEZONE).date(), DECISION_DEADLINE, TAIPEI_TIMEZONE)


def session_deadline_passed(session, now: datetime) -> bool:
    """目標時段當天的交件期限（08:55）是否已過；缺少或無法解析開盤時間時視為已過（fail-closed）。"""
    deadline = session_deadline(session)
    return deadline is None or now >= deadline


def refuse_late_decision(summary: dict, session, decision_run_id: str, now: datetime) -> None:
    """決策跑完時已過目標時段的交件期限：不換 D-Plan 主檔、不留新決策。

    有被取代的備案時，結束時的還原會一併封存新決策；沒有備案時直接封存新決策，帳本視為該時段沒交件。
    """
    if not session_deadline_passed(session, now):
        return
    if not summary.get("superseded_pending"):
        summary["late_decision_archived_to"] = str(archive_decision_run(
            ROOT / "artifacts" / "decisions", decision_run_id, ROOT / "artifacts" / "archive" / "superseded_decisions"))
    raise RuntimeError("決策跑完時已過目標時段 %s 的交件期限 %s，不產生 D-Plan，也不取代已上傳的決策" % (
        (session or {}).get("start"), DECISION_DEADLINE.strftime("%H:%M")))


def superseded_pending(account_id: str, pending) -> bool:
    """資料階段是否把重跑前的待成交決策移到封存區；帳本可能已前進，也可能在建立新 prepare 前就失敗。"""
    if pending is None:
        return False
    if find_run_dir(ROOT / "artifacts" / "decisions", pending["decision_run_id"], RuntimeError) is not None:
        return False
    # 目標時段休市而作廢的決策不是被取代的備案，沒產生新 D-Plan 時也不能還原。
    return voided_pending(pending) is None


def voided_pending(pending) -> "Path | None":
    """待成交決策因目標時段休市被帳本作廢時，回傳它在作廢區的位置。"""
    if pending is None:
        return None
    return find_run_dir(ROOT / "artifacts" / "archive" / "voided_closed_sessions", pending["decision_run_id"], RuntimeError)


def roll_back_superseded(summary: dict, run_dir: Path, account_id: str, decision_run_id: str) -> None:
    """本次取代了待成交決策卻沒產生新 D-Plan：帳本、決策與 Snapshot 還原成重跑前，原 D-Plan 主檔仍對得上。"""
    try:
        restored = restore_superseded_decision(
            VirtualAccountRepository(ROOT / "artifacts" / "ledger", account_id),
            ROOT / "artifacts" / "decisions",
            ROOT / "artifacts" / "archive" / "superseded_decisions",
            summary["superseded_pending"],
            ROOT / "artifacts" / "ledger" / account_id / "account_snapshot_latest.json",
            discard_decision_run_id=decision_run_id,
        )
        backup = run_dir / ROLLBACK_SNAPSHOT
        if backup.exists():
            shutil.copy2(backup, ROOT / "artifacts" / "data" / "research_snapshot_latest.json")
        summary["rolled_back"] = restored
        log("本次沒有產生新 D-Plan，已還原重跑前的帳本與決策 %s；原本的 D-Plan 主檔仍可上傳。" % restored["decision_run_id"])
    except (VirtualAccountError, OSError, ValueError, KeyError) as error:
        summary["rollback_error"] = str(error)
        log("還原重跑前的決策失敗，請人工檢查帳本：%s" % error)


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
        started_at=summary["started_at"], ledger_repository=root / "artifacts/ledger")
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


def datapack_gate(args, run_dir: Path, account_path: Path, session, previous, snapshot_path: Path = None, stamp: str = "") -> dict:
    """本次目標交易日的 DataPack 必須與帳本一致；結果寫入 pipeline.json 供 D-Plan 綁定。"""
    if args.skip_datapack:
        log("[對帳] 依 --skip-datapack 略過 DataPack 對帳；D-Plan 會記錄為略過")
        return {"status": "skipped"}
    team_id = json.loads((ROOT / "config" / "competition_profile.json").read_text(encoding="utf-8")).get("team_id")
    trade_date = DPlanExportService.trade_date({"target_session": session})
    log("[對帳] 主辦方 DataPack（%s，交易日 %s）" % (team_id, trade_date))
    loaded = load_datapack(args.datapack) if args.datapack else find_datapack(args.datapack_dir, team_id, trade_date)
    if isinstance(previous, dict) and previous.get("datapack_sha256") and previous["datapack_sha256"] != loaded["raw_sha256"]:
        raise DataPackError("續跑時 DataPack 與原本對帳的檔案不同；請不帶 --resume 重新執行")
    shutil.copyfile(loaded["path"], run_dir / "datapack.json")
    account = json.loads(account_path.read_text(encoding="utf-8"))
    universe = load_universe(ROOT / "data" / "official_universe.csv")
    result = reconcile(loaded, account, team_id=team_id, trade_date=trade_date, universe=universe,
                       cash_tolerance_rate=args.datapack_cash_tolerance)
    if result["status"] == "mismatch" and getattr(args, "rebase_to_datapack", False):
        log("[對帳] 不一致，依 --rebase-to-datapack 以 DataPack 重建帳本：%s" % "；".join(result["problems"]))
        rebased = rebase_account_to_datapack(ROOT, args.account_id, loaded, snapshot_path,
                                             "prepare-%s-datapack" % stamp, account_path, universe)
        result = reconcile(loaded, rebased["account_snapshot"], team_id=team_id, trade_date=trade_date, universe=universe,
                           cash_tolerance_rate=args.datapack_cash_tolerance)
        result["rebased"] = {"rebase_run_id": rebased["rebase_run_id"], "prepare_run_id": rebased["prepare_run_id"]}
    for warning in result["warnings"]:
        log("[對帳] 注意：%s" % warning)
    if result["status"] != "matched":
        raise DataPackError("帳本與主辦方 DataPack 不一致，已在呼叫 Agent 前停止：%s。確認資料包是最新的後，"
                            "加 --rebase-to-datapack 重新執行即可依資料包重建帳本" % "；".join(result["problems"]))
    log("[對帳] 一致：持股 %d 檔，現金差 %s 元" % (len(result["sellable_shares"]), result["cash"]["difference"]))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--perception", nargs=2, type=Path, metavar=("BUNDLE", "RESULT"),
        help="已核准授權且同 cutoff 的情緒資料包，以及與其成對的已驗證結果；兩個檔案須同時提供",
    )
    parser.add_argument("--account-id", help="預設讀取 config/active_account.json")
    parser.add_argument("--datapack", type=Path, help="指定本次要對帳的主辦方 DataPack；預設在 --datapack-dir 找本隊、本交易日最新的一份")
    parser.add_argument("--datapack-dir", type=Path, default=ROOT / "var" / "datapack", help="放主辦方 DataPack 的資料夾（預設 var/datapack）")
    parser.add_argument("--no-market-news", action="store_true", help="不接入全市場新聞（新聞步驟出問題時的緊急開關）；市場情緒改用恐懼貪婪指標")
    parser.add_argument("--rebase-to-datapack", action="store_true", help="帳本與 DataPack 不一致時，以 DataPack 重建帳本並重建決策前快照（會重抓資料建立新 Snapshot）；舊帳本狀態保留")
    parser.add_argument("--skip-datapack", action="store_true", help="明確略過 DataPack 對帳（會記錄在 pipeline.json）；比賽期間不要使用")
    parser.add_argument("--datapack-cash-tolerance", default=str(DEFAULT_CASH_TOLERANCE_RATE), help="帳本與 DataPack 現金差的容忍比率（分母 nav_tradable），預設 %s" % DEFAULT_CASH_TOLERANCE_RATE)
    parser.add_argument("--delivery-only", metavar="DAILY_RUN_ID", help="只重建既有每日 run 的最終交付，不執行資料準備或 Agent")
    parser.add_argument("--skip-data", action="store_true", help="沿用既有 Snapshot 與帳戶快照，不執行 ./start.sh daily")
    parser.add_argument("--force", action="store_true", help="休市日（週末或官方開休市日期表列出者）或當日已完成仍執行")
    parser.add_argument(
        "--resume", nargs="?", const=RESUME_LATEST, metavar="RUN_ID",
        help="沿用先前已通過驗證的 Agent 輸出續跑（例如中途撞到用量上限）；找到可續跑的 run 時隱含 --skip-data。"
             "不帶值時續跑今天最近一個未完成的 run；帶 RUN_ID（如 daily-20260929）則續跑指定 run，用量上限重置或跨午夜後使用",
    )
    parser.add_argument("--agent-cli", choices=("claude", "codex", "opencode"), default="claude", help="執行子 Agent 的 CLI；預設 claude。多空研究員預設與它相同（codex 時研究員也用 Codex 模型，隔離改由研究階段輸出暫存保證），可用 --research-agent-cli 另行指定")
    parser.add_argument(
        "--research-agent-cli", choices=("claude", "codex", "opencode"),
        help="多頭／空頭研究員使用的 CLI；預設與 --agent-cli 相同。"
             "研究員為 codex 時（含 --agent-cli codex 的預設），多空隔離改由研究階段輸出暫存保證（時序隔離）：Codex 仍可讀整個專案，包含舊 run 的多空輸出，隔離弱於 claude／opencode",
    )
    parser.add_argument("--model", help="交易、現金姿態與風險審查的模型；使用 Claude CLI 時預設 %s，其他 CLI 預設沿用該 CLI 設定" % DEFAULT_CLAUDE_MODEL)
    parser.add_argument(
        "--analyst-model",
        help="技術／基本面／事件分析師批次使用的模型；使用 Claude CLI 時預設 %s，傳 inherit 則沿用 --model；其他 CLI 未指定時沿用 --model。多空、交易與現金姿態不受影響" % DEFAULT_CLAUDE_ANALYST_MODEL,
    )
    parser.add_argument(
        "--research-model",
        help="多頭／空頭研究員批次使用的模型；研究員用 Claude CLI 時預設 %s，傳 inherit 則沿用 --model（研究員與主 CLI 相同時）；其他 CLI 未指定時，與主 CLI 相同者沿用 --model，不同者用該 CLI 的預設設定" % DEFAULT_CLAUDE_RESEARCH_MODEL,
    )
    parser.add_argument(
        "--event-reuse", action=argparse.BooleanOptionalAction, default=True,
        help="同一交易日前一次 run 中，事件簡報逐檔完全相同的股票直接沿用已驗證的事件分析（預設開啟；--no-event-reuse 全部重新交給 Agent）",
    )
    parser.add_argument(
        "--technical-gate", action=argparse.BooleanOptionalAction, default=True,
        help="趨勢一致的股票技術 brief 只附 momentum、不附逐日歷史（預設開啟；--no-technical-gate 改回全部附完整歷史）",
    )
    parser.add_argument(
        "--max-workers", type=int, default=8, choices=range(1, 9), metavar="N",
        help="分析師、多空研究與交易階段同時執行的子 Agent 數（1～8，預設 8；1 為串行）。同時呼叫越多越容易撞到用量上限",
    )
    parser.add_argument("--preflight-only", action="store_true", help="只檢查 Docker、所選 Agent CLI 登入、帳戶、必要檔案與磁碟")
    parser.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_BYTES / 1024 ** 3, help="執行前檢查要求的最低可用磁碟空間（GiB）")
    parser.add_argument(
        "--no-auto-resume", action="store_true",
        help="決策鏈失敗時不自動續跑（預設：離交件期限 25 分鐘以上、且不是額度用完或安全拒絕時，沿用已驗證輸出續跑一次）",
    )
    parser.add_argument("--max-budget-usd", type=float, help="Claude CLI 單次子 Agent 的估算用量上限；預設 3 美元")
    args = parser.parse_args()
    args.account_id = args.account_id or active_account_id(ROOT)
    try:
        args.datapack_cash_tolerance = Decimal(args.datapack_cash_tolerance)
    except InvalidOperation:
        parser.error("--datapack-cash-tolerance 必須是數值")
    if not args.datapack_cash_tolerance.is_finite() or args.datapack_cash_tolerance < 0:
        parser.error("--datapack-cash-tolerance 必須是非負有限數值")
    if args.skip_datapack and args.datapack:
        parser.error("--skip-datapack 不能和 --datapack 同時使用")
    if args.rebase_to_datapack and (args.skip_datapack or args.resume is not None):
        parser.error("--rebase-to-datapack 不能和 --skip-datapack 或 --resume 同時使用")
    # 研究員 CLI 未指定時與主 CLI 相同（codex 亦然，Codex 研究員走研究階段輸出暫存）。
    args.research_agent_cli = effective_research_agent_cli(args.agent_cli, args.research_agent_cli)
    if args.model is None and args.agent_cli == "claude":
        args.model = DEFAULT_CLAUDE_MODEL
    if args.analyst_model == "inherit":
        args.analyst_model = None
    elif args.analyst_model is None and args.agent_cli == "claude":
        args.analyst_model = DEFAULT_CLAUDE_ANALYST_MODEL
    if args.research_model == "inherit":
        args.research_model = None
    elif args.research_model is None and args.research_agent_cli == "claude":
        args.research_model = DEFAULT_CLAUDE_RESEARCH_MODEL
    if "claude" not in (args.agent_cli, args.research_agent_cli) and args.max_budget_usd is not None:
        parser.error("--max-budget-usd 只適用 Claude CLI")
    if args.max_budget_usd is not None and (not math.isfinite(args.max_budget_usd) or args.max_budget_usd <= 0):
        parser.error("--max-budget-usd 必須是大於 0 的有限數值")
    # 參數檢查通過後才開日誌檔：之後所有 log() 同時寫終端與 var/logs/daily-<日期>.log，並清掉超過保留天數的舊檔。
    open_daily_log(daily_logs_root(), datetime.now(TAIPEI_TIMEZONE))
    removed_logs = prune_daily_logs(daily_logs_root(), datetime.now(TAIPEI_TIMEZONE))
    if removed_logs:
        log("[日誌] 已刪除超過 %d 天的舊日誌：%s" % (LOG_RETENTION_DAYS, "、".join(removed_logs)))
    try:
        return _main(args)
    finally:
        close_daily_log()


def _main(args) -> int:
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
        acquired = acquire_run_lock(lock_handle)
    except OSError as error:
        # 權限、磁碟等非「鎖被佔用」的錯誤：不能當成別的流程在跑而靜默成功，否則排程漏跑沒人知道。
        log("無法取得每日流程鎖（artifacts/daily/.lock）：%s" % error)
        notify("ETF Agent 每日決策", "失敗：無法取得每日流程鎖：%s" % str(error)[:100])
        record_run_status("failed", detail="無法取得每日流程鎖：%s" % error)
        return 1
    if not acquired:
        holder = describe_lock_holder(runs_root / ".lock", datetime.now(TAIPEI_TIMEZONE))
        log("另一個每日流程正在執行，本次略過。%s" % holder)
        notify("ETF Agent 每日決策", "本次略過：另一個每日流程仍在執行。%s" % holder)
        record_run_status("skipped", detail="另一個每日流程仍在執行。%s" % holder)
        return 0
    write_lock_holder(lock_handle, datetime.now(TAIPEI_TIMEZONE))
    min_free_bytes = int(args.min_free_gb * 1024 ** 3)
    if args.preflight_only:
        checks = run_preflight(ROOT, args.account_id, args.skip_data, min_free_bytes=min_free_bytes,
                                agent_cli=args.agent_cli, research_agent_cli=args.research_agent_cli)
        for check in checks:
            log("%s %s：%s" % ("通過" if check.ok else "失敗", check.name, check.detail))
        return 0 if all(check.ok for check in checks) else 1
    captures_root = ROOT / "artifacts" / "data" / "trading_status"
    if not args.force:
        if ensure_calendar_capture(captures_root, now):
            # 剛抓到的日期表完成時間晚於 now，沿用舊 now 會被當成未來資料而找不到。
            now = datetime.now(TAIPEI_TIMEZONE)
        refresh_inferred_closures(captures_root, now)
        try:
            if not trading_day_check(captures_root, now):
                log("%s 依官方開休市日曆休市、隔天也不是交易時段的晚間備案，不執行（--force 可強制）。" % now.date().isoformat())
                record_run_status("skipped", detail="%s 休市，不執行" % now.date().isoformat())
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
            record_run_status("failed", detail="無法判定交易日：%s" % error)
            return 1
    day_run_prefix = "daily-%s" % now.strftime("%Y%m%d")
    session_start_hint = upcoming_session_start(captures_root, now)
    if not args.force:
        # 同一時段剛完成才略過；另一時段（早上為當天、晚上為隔天）或完成已久的照常另開新 run。
        done = completed_run_for_session(runs_root, day_run_prefix, session_start_hint, now)
        if done is not None:
            log("%s 已完成（%s），不重跑。" % done)
            record_run_status("skipped", trade_date=trade_date_from_session_start(session_start_hint), run_id=done[0],
                              detail="同一時段 1 小時內已完成（%s），不重跑" % done[1])
            return 0
    # 每次 run 目錄都帶台北時間，同日多次執行各自保留、互不覆蓋；資料夾改用目標交易日
    # （跟 D-Plan 一致），日曆缺漏等估不出目標時段時才退回執行日期。
    run_id = "%s-%s" % (day_run_prefix, now.strftime("%H%M%S"))
    run_dir = daily_run_path(runs_root, run_id, trade_date_from_session_start(session_start_hint))
    summary_path = run_dir / "pipeline.json"
    if args.resume not in (None, RESUME_LATEST):
        try:
            resumable = named_resumable_run(runs_root, args.resume)
        except RuntimeError as error:
            log("無法續跑：%s" % error)
            record_run_status("failed", detail="無法續跑：%s" % error)
            return 1
    else:
        resumable = latest_resumable_run(runs_root, day_run_prefix) if args.resume else None
    if resumable is not None:
        previous_cli = resumable_agent_cli(resumable)
        if previous_cli != args.agent_cli:
            log("無法續跑：原 run 使用 %s，這次選擇 %s" % (previous_cli, args.agent_cli))
            record_run_status("failed", run_id=resumable.name, detail="無法續跑：原 run 使用 %s，這次選擇 %s" % (previous_cli, args.agent_cli))
            return 1
        previous_research_cli = resumable_research_agent_cli(resumable)
        if previous_research_cli != args.research_agent_cli:
            log("無法續跑：原 run 的多空研究員使用 %s，這次選擇 %s" % (previous_research_cli, args.research_agent_cli))
            record_run_status("failed", run_id=resumable.name, detail="無法續跑：研究員 CLI %s 與原 run %s 不同" % (args.research_agent_cli, previous_research_cli))
            return 1
        # 續跑沿用最近一個未完成且已有 Agent 輸出的 run 目錄，否則新目錄中沒有可沿用的輸出。
        run_id = resumable.name
        run_dir = resumable
        summary_path = run_dir / "pipeline.json"
        log("續跑沿用 run 目錄：%s" % run_id)
        # 可續跑的 run 已有 Agent 輸出，代表 Snapshot 與帳戶快照已建立；重抓資料會讓快照與已驗證輸出對不上。
        args.skip_data = True
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    decision_run_id = "decision-%s" % stamp
    if resumable is not None:
        # Agent 任務名與 envelope 都含 decision run ID；續跑必須沿用同一個，已驗證輸出才對得上。
        decision_run_id = resumed_decision_run_id(run_dir) or decision_run_id
    previous_summary = json.loads(summary_path.read_text(encoding="utf-8")) if resumable is not None else {}
    original_start = previous_summary["started_at"] if resumable is not None else now.isoformat()
    summary = {"run_id": run_id, "started_at": original_start, "status": "running", "planned_decision_run_id": decision_run_id, "agent_cli": args.agent_cli,
               "research_agent_cli": args.research_agent_cli,
               "research_isolation": "staged_outputs" if args.research_agent_cli == "codex" else "deny_reads", "market_news": "disabled" if args.no_market_news else "enabled"}
    if resumable is not None:
        summary["resumed_at"] = now.isoformat()
    if not args.skip_data:
        trade_date = expected_latest_trade_date(captures_root, now)
        latest_snapshot = ROOT / "artifacts" / "data" / "research_snapshot_latest.json"
        # 依 DataPack 重建時一律建立新 Snapshot，cutoff 才會晚於資料包產生時間。
        fetched = not args.rebase_to_datapack and data_already_fetched(
            latest_snapshot, ROOT / "artifacts" / "ledger" / args.account_id / "account_snapshot_latest.json", trade_date,
        )
        if fetched and refresh_before_deadline(captures_root, latest_snapshot, now):
            log("已有 %s 的行情資料，但仍在交件期限前：日線略過，重抓事件與情緒資料並重建 Snapshot。" % trade_date.isoformat())
            summary["data_refreshed_before_deadline"] = trade_date.isoformat()
        elif fetched:
            log("已有 %s 的行情資料（Snapshot 150 檔齊全），沿用既有 Snapshot 與帳戶快照，不重抓。" % trade_date.isoformat())
            summary["data_reused_trade_date"] = trade_date.isoformat()
            args.skip_data = True
    write_json(summary_path, summary)
    # 備援 D-Plan 需要目標時段、帳戶快照與 Snapshot 路徑；流程在哪一步失敗都要能判斷還缺什麼。
    session = None
    snapshot_path = ROOT / "artifacts" / "data" / "research_snapshot_latest.json"
    account_path = ROOT / "artifacts" / "ledger" / args.account_id / "account_snapshot_latest.json"

    try:
        # 在抓資料與呼叫任何子 Agent 前確認環境；一次列出全部失敗項目。
        # 全新帳戶自動開帳；已有帳本只驗證並沿用，不會重建。失敗時交給下方預檢一併回報。
        opened = subprocess.run(
            [sys.executable, str(ROOT / "cli/virtual_account.py"), "--account-id", args.account_id, "ensure-init"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        if opened.returncode != 0:
            log("帳戶自動開帳未成功：%s" % (opened.stderr or opened.stdout).strip()[-300:])
        log("[預檢] Docker、%s 登入、帳戶、必要檔案、磁碟空間" % (
            args.agent_cli if args.research_agent_cli == args.agent_cli else "%s／%s（多空研究員）" % (args.agent_cli, args.research_agent_cli)))
        checks = run_preflight(ROOT, args.account_id, args.skip_data, min_free_bytes=min_free_bytes,
                                agent_cli=args.agent_cli, research_agent_cli=args.research_agent_cli)
        summary["preflight"] = checks_to_dict(checks)
        require_preflight(checks)
        # 只記錄本次使用的 Agent CLI 版本供事後稽核（新欄位，舊 run 沒有也不影響續跑與重建）。
        summary["agent_cli_versions"] = agent_cli_versions(args.agent_cli, args.research_agent_cli)
        log("Agent CLI 版本：%s" % "、".join("%s %s" % (name, version or "未知") for name, version in summary["agent_cli_versions"].items()))
        if not args.skip_data:
            # 交易狀態 CSV 必須在 Snapshot 固定 cutoff 之前封存，available_at 才不會晚於 cutoff。
            log("[0/4] 封存交易狀態與開休市政府開放 CSV")
            try:
                capture = capture_with_retry(
                    lambda: capture_trading_sources(captures_root),
                    now=lambda: datetime.now(TAIPEI_TIMEZONE), sleep=time_module.sleep,
                    stop_at=capture_retry_stop_at(datetime.now(TAIPEI_TIMEZONE)), log=log,
                )
                summary["trading_status_capture"] = capture.get("run_id")
                if capture.get("status") != "captured_candidate_only":
                    summary["trading_status_capture_error"] = "重試後仍未完整抓到：%s" % capture.get("reason", capture.get("status"))
                    log("交易狀態封存重試後仍不完整，改用 cutoff 前最新的成功封存")
                    notify("ETF Agent 每日決策", "交易狀態資料重試後仍抓不完整，沿用舊資料（可能被拒絕）")
            except (OSError, ValueError, json.JSONDecodeError) as error:
                summary["trading_status_capture_error"] = str(error)
                log("交易狀態封存失敗，改用 cutoff 前最新的成功封存：%s" % error)
            # 記下重跑前的待成交決策與 Snapshot；本次若取代它卻沒產生新 D-Plan，結束時還原。
            pending = pending_decision(
                VirtualAccountRepository(ROOT / "artifacts" / "ledger", args.account_id), ROOT / "artifacts" / "decisions",
            )
            if pending is not None and snapshot_path.exists():
                shutil.copy2(snapshot_path, run_dir / ROLLBACK_SNAPSHOT)
            summary["company_news"] = ensure_company_news(now)
            if summary["company_news"]["status"] in {"failed", "check_failed"}:
                log("個股新聞補抓未成功（%s），本次事件分析不含個股新聞" % summary["company_news"].get("error", summary["company_news"].get("exit_code")))
            write_json(summary_path, summary)
            log("[1/4] ./start.sh daily：資料、Snapshot、虛擬帳本")
            try:
                run_data_stage(args.account_id, "prepare-%s" % stamp, trade_date)
            finally:
                refresh_inferred_closures(captures_root, datetime.now(TAIPEI_TIMEZONE))
                if superseded_pending(args.account_id, pending):
                    summary["superseded_pending"] = pending
                elif voided_pending(pending) is not None:
                    summary["voided_pending"] = dict(pending, archived_to=str(voided_pending(pending)))
                    log("前一份決策的目標時段休市，已作廢（不成交）：%s" % pending["decision_run_id"])
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
        if session_deadline_passed(session, datetime.now(TAIPEI_TIMEZONE)):
            # 資料階段可能拖過期限；沿用的舊 Snapshot 仍排到已過交件期限的時段；再做決策會取代已上傳那份，帳本與實際交件對不上。
            raise RuntimeError("目標時段 %s 已過交件期限 %s，不再為它產生決策；收盤資料到齊後再跑會排到下一時段" % (
                session.get("start"), DECISION_DEADLINE.strftime("%H:%M")))
        summary["trading_status"] = {
            "capture_dir": str(capture_dir) if capture_dir else None,
            "approved_sources": sorted(approvals),
            "not_applicable": ["%s/%s" % (item["market"], item["category"]) for item in not_applicable],
            "states": {state: sum(1 for item in assessment["symbols"] if item["state"] == state) for state in ("allowed", "blocked", "unknown")},
        }
        trading_status_path = run_dir / "trading_status.json"
        write_json(trading_status_path, {"bundle": status_bundle, "assessment": assessment})
        # Guard 的 TRADABILITY_COVERAGE 對任何 unknown 一律拒絕；先停下，不為注定被拒的決策花子 Agent 額度。
        unknown_count = summary["trading_status"]["states"].get("unknown", 0)
        if unknown_count:
            raise RuntimeError("交易狀態有 %d 檔不明（資料沒抓齊或太舊），Guard 必定拒絕；已在呼叫子 Agent 前停止" % unknown_count)

        # 在呼叫任何子 Agent 前對帳，對不上就停，不耗用量。
        summary["datapack"] = datapack_gate(args, run_dir, account_path, session, previous_summary.get("datapack"), snapshot_path, stamp)
        account_run_id = prepared_account_run(args.account_id)
        write_json(summary_path, summary)

        log("[3/4] 決策鏈：分析團隊 → 多空研究 → 交易 → 風險（子 Agent 經 %s%s）" % (
            args.agent_cli, "" if args.research_agent_cli == args.agent_cli else "，多空研究員經 %s" % args.research_agent_cli))
        agent_runner = make_agent_runner(args.agent_cli, args.model, args.max_budget_usd)
        analyst_runner = make_agent_runner(args.agent_cli, args.analyst_model, args.max_budget_usd) if args.analyst_model else None
        # 研究員 CLI 與主 CLI 不同時一律另建（沒指定模型就用該 CLI 的預設）；相同時只在指定模型時另建。
        research_runner = (make_agent_runner(args.research_agent_cli, args.research_model, args.max_budget_usd)
                           if args.research_model or args.research_agent_cli != args.agent_cli else None)
        def build_pipeline(resume):
            return DailyDecisionPipeline(
                ROOT, run_dir,
                agent_runner,
                ROOT / "artifacts" / "decisions",
                log=log,
                resume=resume or args.resume is not None,
                analyst_runner=analyst_runner,
                research_runner=research_runner,
                technical_gate=args.technical_gate,
                event_reuse=args.event_reuse,
                max_workers=args.max_workers,
                # 子 Agent 暫時性失敗（逾時、非零結束、輸出不是 JSON）再試一次，但不拖過目標時段的交件期限。
                retry_deadline=session_deadline(session),
            )

        def run_pipeline(pipeline):
            try:
                return _run_pipeline(pipeline)
            except Exception:
                # 預期之外的錯誤照常往上拋（fail-closed），但已花的 Agent 呼叫與階段耗時先寫進執行紀錄。
                partial = pipeline.last_result
                if partial is not None:
                    summary["agent_calls"] = list(partial.agent_calls)
                    summary["skill_versions"] = partial.skill_versions
                    summary["stage_durations"] = partial.stage_durations
                    summary["agent_estimated_usage_usd"] = round(sum(call["estimated_usage_usd"] for call in partial.agent_calls), 4)
                    write_json(summary_path, summary)
                raise

        def _run_pipeline(pipeline):
            return pipeline.run(
                snapshot_path, account_path, trading_status_path,
                ROOT / "config" / "decision_rules.json", ROOT / "var" / "etf_agent.db",
                ROOT / "config" / "decision_policy.json", ROOT / "data" / "sector_classification.json",
                decision_run_id,
                perception_bundle_path=perception_bundle, perception_result_path=perception_result,
                fear_greed_root=ROOT / "artifacts" / "data" / "market_fear_greed",
                fear_greed_approvals_path=ROOT / "config" / "market_fear_greed_sources.json",
                overnight_root=ROOT / "artifacts" / "data" / "overnight_market",
                overnight_approvals_path=ROOT / "config" / "overnight_market_sources.json",
                market_news_root=None if args.no_market_news else ROOT / "artifacts" / "data" / "market_news",
                nav_history=VirtualAccountRepository(ROOT / "artifacts" / "ledger", args.account_id).nav_history(),
                holdings_history=VirtualAccountRepository(ROOT / "artifacts" / "ledger", args.account_id).holdings_history(),
                applied_decision_run_ids=VirtualAccountRepository(ROOT / "artifacts" / "ledger", args.account_id).applied_decision_run_ids(),
                active_share_holdings_path=ROOT / "data" / "active_etf_holdings.csv",
                active_share_approvals_path=ROOT / "config" / "active_share_sources.json",
            )

        result, agent_calls, auto_resume = run_decision_chain(
            build_pipeline, run_pipeline, session_deadline(session), lambda: datetime.now(TAIPEI_TIMEZONE), log,
            auto_resume=not args.no_auto_resume,
        )
        if auto_resume is not None:
            summary["auto_resume"] = auto_resume
        summary.update(
            {
                "decision_status": result.decision_status,
                "decision_run_id": result.decision_run_id,
                "cash_stance": result.cash_stance,
                "position_breadth": result.position_breadth,
                "order_count": len(result.orders),
                "agent_calls": agent_calls,
                "skill_versions": result.skill_versions,
                "max_workers": args.max_workers,
                "stage_durations": result.stage_durations,
                "agent_estimated_usage_usd": round(sum(call["estimated_usage_usd"] for call in agent_calls), 4),
                "target_session": session,
            }
        )
        if result.status != "completed":
            raise RuntimeError("決策鏈失敗：%s" % "；".join(result.errors))
        summary["event_analysis_mode"] = "integrated_analyst"
        refuse_late_decision(summary, session, result.decision_run_id, datetime.now(TAIPEI_TIMEZONE))
        superseded = supersede_decision_runs(
            ROOT / "artifacts" / "decisions", result.decision_run_id,
            ROOT / "artifacts" / "archive" / "superseded_decisions",
            # 以完成當下判斷：跑到超過 08:55 才完成時，舊決策可能已上傳，改保留舊的。
            now=datetime.now(TAIPEI_TIMEZONE),
        )
        if superseded:
            summary["superseded_decision_runs"] = superseded
            log("新決策取代同一份帳戶快照的舊決策，舊 run 已移到 artifacts/archive/superseded_decisions：%s" % "、".join(superseded))
        log("[交付] 重建風控後最終中文報告")
        summary["delivery"] = build_delivery(
            ROOT / "artifacts/decisions", result.decision_run_id,
            ROOT / "artifacts/reports", reports_root=ROOT / "artifacts/latest",
            started_at=summary["started_at"], ledger_repository=ROOT / "artifacts/ledger",
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
    if summary.get("superseded_pending") and summary.get("dplan", {}).get("status") != "ready":
        roll_back_superseded(summary, run_dir, args.account_id, decision_run_id)
    if summary.get("dplan", {}).get("status") != "ready":
        # 決策被拒、D-Plan 被擋或流程失敗：嘗試程式產生的「全部續抱、無委託」備援候選（只落地、不上傳）。
        try:
            attempt_fallback_dplan(summary, args, session, account_path, snapshot_path, datetime.now(TAIPEI_TIMEZONE))
        except Exception as error:  # noqa: BLE001 — 備援本身出錯不得改變原本的失敗處理
            summary.setdefault("dplan", {"status": "blocked", "error": str(summary.get("error"))})["fallback"] = {
                "status": "not_generated", "error": "備援程式例外：%s" % error}
            log("[備援] 備援程式例外，未產生：%s" % error)
    summary["calibration"] = record_calibration()
    summary.setdefault("finished_at", datetime.now(TAIPEI_TIMEZONE).isoformat())
    summary["final_status"] = final_run_status(summary)
    write_json(summary_path, summary)

    log("[4/4] 通知")
    message = finish_notification(summary, args)
    notify("ETF Agent 每日決策", message)
    record_run_status(summary["final_status"], trade_date=trade_date_from_session_start((session or {}).get("start")) if session else None,
                      run_id=run_id, detail=status_detail(summary))
    if summary.get("delivery"):
        dplan = summary.get("dplan", {})
        detail = dplan.get("output") if dplan.get("status") in {"ready", "fallback"} else dplan.get("error", "not_run")
        log("最終報告：%s；D-Plan：%s（%s）" % (
            summary["delivery"]["report_path"], dplan.get("status", "not_run"), detail,
        ))
    log(message)
    log("摘要：%s" % summary_path)
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
