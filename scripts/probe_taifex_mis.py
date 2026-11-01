#!/usr/bin/env .venv/bin/python
"""記錄期交所官網即時行情的臺指期月合約報價（夜盤 TAIFEX_MIS 的來源），只觀察、不進決策。

用來驗證夜盤完成條件：夜盤結束後（台北 05:00 之後）CDate／CTime 長什麼樣、週末與週一早上還看不看得到週六 05:00 的最後報價、
08:45 日盤開盤後報價何時切換。每筆紀錄附上程式的判斷（報價時間是否在 05:05 前、抓取是否在夜盤結束後）。"""
import json
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from etf_agent.perception.overnight_market.provider import taifex_night_rows  # noqa: E402

LOG = ROOT / "var" / "logs" / "taifex_mis_probe.jsonl"
TAIPEI = ZoneInfo("Asia/Taipei")


def main() -> int:
    now = datetime.now(timezone.utc)
    row = {"fetched_at": now.isoformat(), "fetched_taipei": now.astimezone(TAIPEI).isoformat()}
    try:
        rows = taifex_night_rows()
        row["rows"] = len(rows)
        traded = [r for r in rows if r["CTotalVolume"].isdigit() and int(r["CTotalVolume"]) > 0]
        top = max(traded, key=lambda r: int(r["CTotalVolume"])) if traded else None
        row["top"] = top
        if top:
            try:
                quote_day = datetime.strptime(top["CDate"], "%Y%m%d").date()
                quote_time = datetime.strptime(top["CTime"], "%H%M%S").time()
                ended = datetime.combine(quote_day, time(5, 0), tzinfo=TAIPEI)
                row["quote_time_le_0505"] = quote_time <= time(5, 5)
                row["fetched_after_night_end"] = ended <= now
                row["night_complete"] = row["quote_time_le_0505"] and row["fetched_after_night_end"]
            except ValueError as error:
                row["parse_error"] = repr(error)
    except Exception as error:  # 只記錄，不中斷
        row["error"] = repr(error)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row, ensure_ascii=False))
    return 0 if "error" not in row else 1


if __name__ == "__main__":
    sys.exit(main())
