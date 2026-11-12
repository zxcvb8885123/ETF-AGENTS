"""帳戶對基準的每日上漲／下跌捕捉（純觀察指標，不進任何決策輸入）。

口徑：
- 日報酬＝同一條 NAV 序列相鄰兩個交易日的 NAV 比（含開帳點；第一天相對本金）。只有帳戶與基準在
  「前一個基準交易日」和「當日」都有估值時才算；任一邊缺值、日期對不上就略過並列出原因，不跨日硬湊。
- 基準日報酬 > 0 為上漲日、< 0 為下跌日、= 0 為持平日（不進任何一側）。
- 上漲捕捉＝上漲日帳戶日報酬平均 ÷ 同期基準日報酬平均；下跌捕捉同理（各日報酬平均值之比，不是累積比）。
  > 100% 代表漲時漲得比基準多；下跌捕捉 < 100% 代表跌時跌得比基準少。
- 任一側樣本少於 ``MIN_CAPTURE_DAYS`` 個交易日標 ``small_sample``（數字仍附，但不可當作結論）；
  該側沒有任何日子時 ``unavailable``，不補假值。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, Sequence

from .tools import rate

MIN_CAPTURE_DAYS = 5

DEFINITION = {
    "daily_return": "同一條 NAV 序列相鄰交易日的 NAV 比 − 1（第一天相對本金）；前一日或當日任一邊缺估值就略過、不跨日硬湊",
    "direction": "基準日報酬 > 0 為上漲日、< 0 為下跌日、= 0 為持平日（不計入捕捉）",
    "up_capture": "上漲日帳戶日報酬平均 ÷ 同期基準日報酬平均；> 100% 代表漲時漲得比基準多",
    "down_capture": "下跌日帳戶日報酬平均 ÷ 同期基準日報酬平均；< 100% 代表跌時跌得比基準少",
    "small_sample": "任一側樣本少於 %d 個交易日標 small_sample；沒有該方向的日子則 unavailable" % MIN_CAPTURE_DAYS,
    "cumulative_excess": "帳戶累積報酬 − 基準累積報酬，取雙方都有估值的最後一個交易日",
}


def _side(rows: Sequence[Mapping[str, Decimal]], name: str) -> Dict[str, object]:
    days = len(rows)
    if days == 0:
        return {"status": "unavailable", "days": 0, "reason": "基準沒有%s日" % name, "small_sample": False,
                "account_mean_return": None, "benchmark_mean_return": None, "capture": None}
    account_mean = sum((row["account"] for row in rows), Decimal(0)) / days
    benchmark_mean = sum((row["benchmark"] for row in rows), Decimal(0)) / days
    return {"status": "ok", "days": days, "reason": None, "small_sample": days < MIN_CAPTURE_DAYS,
            "account_mean_return": rate(account_mean), "benchmark_mean_return": rate(benchmark_mean),
            "capture": rate(account_mean / benchmark_mean)}


def build_daily_capture(
    account_navs: Mapping[str, Decimal], benchmark_series: Sequence[Mapping[str, object]], capital: Decimal,
) -> Dict[str, object]:
    """``account_navs``：交易日 → 帳戶 NAV；``benchmark_series``：基準逐日序列（含開帳點，nav 可為 None）。"""
    rows: List[Dict[str, object]] = []
    skipped: List[Dict[str, str]] = []
    up: List[Dict[str, Decimal]] = []
    down: List[Dict[str, Decimal]] = []
    flat = 0
    last_aligned = None
    for index in range(1, len(benchmark_series)):
        previous, current = benchmark_series[index - 1], benchmark_series[index]
        date = str(current["trade_date"])
        prev_date = str(previous["trade_date"])
        if previous["nav"] is None or current["nav"] is None:
            skipped.append({"trade_date": date, "reason": "基準 %s 缺估值" % (date if current["nav"] is None else prev_date)})
            continue
        if prev_date not in account_navs or date not in account_navs:
            missing = date if date not in account_navs else prev_date
            skipped.append({"trade_date": date, "reason": "帳本沒有 %s 的 NAV" % missing})
            continue
        b_prev, b_cur = Decimal(str(previous["nav"])), Decimal(str(current["nav"]))
        a_prev, a_cur = account_navs[prev_date], account_navs[date]
        if b_prev <= 0 or a_prev <= 0:
            skipped.append({"trade_date": date, "reason": "前一日 NAV 非正數"})
            continue
        account_return, benchmark_return = a_cur / a_prev - 1, b_cur / b_prev - 1
        direction = "up" if benchmark_return > 0 else "down" if benchmark_return < 0 else "flat"
        rows.append({"trade_date": date, "account_return": rate(account_return), "benchmark_return": rate(benchmark_return),
                     "difference": rate(account_return - benchmark_return), "benchmark_direction": direction})
        pair = {"account": account_return, "benchmark": benchmark_return}
        if direction == "up":
            up.append(pair)
        elif direction == "down":
            down.append(pair)
        else:
            flat += 1
        last_aligned = (date, a_cur, b_cur)
    if not rows:
        return {"status": "unavailable", "reason": "沒有帳戶與基準相鄰兩日都有估值的交易日", "definition": dict(DEFINITION),
                "min_days_per_side": MIN_CAPTURE_DAYS, "rows": [], "skipped": skipped}
    excess = None
    if last_aligned is not None and capital > 0:
        excess = {"trade_date": last_aligned[0], "account_cumulative_return": rate(last_aligned[1] / capital - 1),
                  "benchmark_cumulative_return": rate(last_aligned[2] / capital - 1),
                  "cumulative_excess": rate((last_aligned[1] - last_aligned[2]) / capital)}
    return {"status": "ok", "reason": None, "definition": dict(DEFINITION), "min_days_per_side": MIN_CAPTURE_DAYS,
            "flat_days": flat, "up": _side(up, "上漲"), "down": _side(down, "下跌"), "cumulative": excess,
            "rows": rows, "skipped": skipped}
