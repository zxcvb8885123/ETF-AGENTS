#!/usr/bin/env python3
"""D-Plan 看門狗：交件期限前檢查今天該交的 D-Plan 主檔是否存在且通過本地驗證，缺少或被擋時發本機通知並記一行狀態。

設計給 launchd 平日 08:00 執行（交件期限 08:55 前、07:00 重做大致跑完後），也可手動執行。
只讀檔、不跑 Agent、不上傳、不改任何決策或 D-Plan；結束碼 0 代表有可交的 D-Plan（含備援），1 代表缺少或無效，
2 代表今天沒有要交的時段（休市）或無法判斷。通知失敗不影響結束碼。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.automation import local_notify  # noqa: E402
from etf_agent.competition import DPlanValidator  # noqa: E402
from etf_agent.competition.fallback import FallbackDPlanService  # noqa: E402
from etf_agent.core.run_layout import list_run_dirs, trade_date_from_session  # noqa: E402
from etf_agent.data import TradingCalendarError, calendar_from_capture, latest_calendar_capture_before  # noqa: E402
from etf_agent.data.trading_calendar import DECISION_DEADLINE  # noqa: E402

TITLE = "ETF Agent D-Plan 看門狗"


def log(message: str) -> None:
    print("[%s] %s" % (datetime.now(local_notify.TAIPEI).strftime("%H:%M:%S"), message), flush=True)


def expected_trade_date(root: Path, now: datetime):
    """今天（台北）要交 D-Plan 的交易日；今天不是交易日或已過交件期限回 None（晚上的備案由明早再查）。

    依封存的官方開休市日曆判斷；日曆缺漏時 fail-closed 回 (None, 原因)，不猜。"""
    local = now.astimezone(local_notify.TAIPEI)
    try:
        capture = latest_calendar_capture_before(root / "artifacts" / "data" / "trading_status", now.isoformat())
        if capture is None:
            return None, "找不到封存的官方開休市日期表"
        calendar = calendar_from_capture(capture)
        if not calendar.is_trading_day(local.date()):
            return None, "%s 休市，今天沒有要交的 D-Plan" % local.date().isoformat()
    except (TradingCalendarError, OSError, ValueError) as error:
        return None, "無法判定交易日：%s" % error
    if local.time() >= DECISION_DEADLINE:
        return None, "已過 %s 交件期限，本檢查只在期限前有意義" % DECISION_DEADLINE.strftime("%H:%M")
    return local.date().isoformat(), None


def latest_pipeline_for_trade_date(root: Path, trade_date: str):
    """該交易日最近一次完成或失敗的每日 run 摘要（依 finished_at／started_at 取最新）；沒有回 None。"""
    best = None
    for run_dir in list_run_dirs(root / "artifacts" / "daily"):
        path = run_dir / "pipeline.json"
        if not path.is_file():
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(summary, dict) or trade_date_from_session(summary.get("target_session")) != trade_date:
            continue
        stamp = str(summary.get("finished_at") or summary.get("started_at") or "")
        if best is None or stamp > best[0]:
            best = (stamp, summary)
    return best[1] if best else None


def check(root: Path, now: datetime) -> dict:
    """回傳 {"status": dplan_ready|dplan_fallback|dplan_missing|dplan_invalid|skipped, "trade_date", "detail", "main"}。"""
    trade_date, reason = expected_trade_date(root, now)
    if trade_date is None:
        return {"status": "skipped", "trade_date": None, "detail": reason, "main": None}
    try:
        profile = json.loads((root / "config" / "competition_profile.json").read_text(encoding="utf-8"))
        team_id = str(profile["team_id"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        return {"status": "dplan_invalid", "trade_date": trade_date, "detail": "無法讀取 competition_profile.team_id：%s" % error, "main": None}
    main = root / "artifacts" / "competition_d_plan" / ("D-Plan_%s_%s.json" % (team_id, trade_date))
    summary = latest_pipeline_for_trade_date(root, trade_date) or {}
    pipeline_status = (summary.get("dplan") or {}).get("status", "not_run")
    run_note = "最新 run %s：status=%s、dplan=%s" % (summary.get("run_id", "-"), summary.get("status", "-"), pipeline_status)
    if not main.exists():
        detail = "缺少 D-Plan 主檔 %s；%s；%s" % (main.name, run_note, (summary.get("dplan") or {}).get("error") or summary.get("error") or "沒有該交易日的 run")
        return {"status": "dplan_missing", "trade_date": trade_date, "detail": detail, "main": str(main)}
    try:
        plan = json.loads(main.read_text(encoding="utf-8"))
        errors = DPlanValidator().validate(plan)
    except (OSError, json.JSONDecodeError) as error:
        return {"status": "dplan_invalid", "trade_date": trade_date, "detail": "D-Plan 主檔無法讀取：%s" % error, "main": str(main)}
    if not isinstance(plan, dict):
        errors = ["D-Plan 必須是 JSON 物件"]
    elif plan.get("team_id") != team_id or plan.get("trade_date") != trade_date:
        errors.append("主檔 team_id／trade_date 與今天要交的不一致")
    if errors:
        return {"status": "dplan_invalid", "trade_date": trade_date, "detail": "D-Plan 主檔本地驗證失敗：%s" % "; ".join(errors[:5]), "main": str(main)}
    if FallbackDPlanService(root).main_plan_is_fallback(main, trade_date):
        return {"status": "dplan_fallback", "trade_date": trade_date,
                "detail": "主檔是程式產生的備援（全部續抱、無委託），尚未上傳，請人工決定是否繳交；%s；%s" % (run_note, main), "main": str(main)}
    detail = "D-Plan 主檔可交：%s（%d 筆委託）；%s" % (main, len(plan.get("orders", [])), run_note)
    if pipeline_status not in {"ready", "fallback"}:
        detail += "；最新 run 沒有產生新 D-Plan，主檔是較早那份（例如已還原的備案），上傳前請確認"
    return {"status": "dplan_ready", "trade_date": trade_date, "detail": detail, "main": str(main)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="專案根目錄（測試用）")
    parser.add_argument("--at", help="以指定台北時間檢查（ISO 8601 含時區；測試用），預設此刻")
    parser.add_argument("--no-notify", action="store_true", help="只印結果與寫狀態列，不發本機通知")
    args = parser.parse_args(argv)
    now = datetime.fromisoformat(args.at) if args.at else datetime.now(local_notify.TAIPEI)
    if now.tzinfo is None:
        parser.error("--at 必須含時區")
    result = check(args.root, now)
    log("%s：%s" % (result["status"], result["detail"]))
    line = local_notify.format_status_line(now, "watchdog", result["status"], result["trade_date"], None, result["detail"])
    local_notify.append_status_line(args.root / "var" / "logs", line, log=log)
    if not args.no_notify and result["status"] != "dplan_ready":
        local_notify.notify(TITLE, "%s：%s" % (result["status"], result["detail"][:200]), log=log)
    if result["status"] == "dplan_ready":
        return 0
    if result["status"] in {"dplan_missing", "dplan_invalid"}:
        return 1
    # 備援可交但要人決定：以 0 結束讓排程不當失敗，通知已發。
    return 0 if result["status"] == "dplan_fallback" else 2


if __name__ == "__main__":
    raise SystemExit(main())
