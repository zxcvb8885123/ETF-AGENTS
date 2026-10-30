#!/usr/bin/env python3
"""記錄期交所 OpenAPI 每日行情最新日期與台指期盤後（夜盤）何時出現，只觀察、不進決策。"""
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"
LOG = Path(__file__).resolve().parents[1] / "var" / "logs" / "taifex_night_probe.jsonl"


def main() -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    row = {"fetched_at": fetched_at, "url": URL}
    try:
        request = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            row["last_modified"] = response.headers.get("last-modified")
            rows = json.loads(response.read().decode("utf-8"))
        dates = sorted({r["Date"] for r in rows})
        night = [r for r in rows if r.get("Contract") == "TX" and r.get("TradingSession") == "盤後"
                 and r.get("ContractMonth(Week)", "").isdigit() and len(r["ContractMonth(Week)"]) == 6]
        row.update({
            "dates": dates, "rows": len(rows),
            "tx_night_dates": sorted({r["Date"] for r in night}),
            "tx_night_last": {r["Date"]: r["Last"] for r in night[:1]},
        })
    except Exception as error:  # 只記錄，不中斷
        row["error"] = repr(error)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row, ensure_ascii=False))
    return 0 if "error" not in row else 1


if __name__ == "__main__":
    sys.exit(main())
