#!/usr/bin/env python3
"""記錄期交所 OpenAPI 每日行情最新日期與台指期盤後（夜盤）何時出現，只觀察、不進決策。"""
import csv
import io
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"
LOG = Path(__file__).resolve().parents[1] / "var" / "logs" / "taifex_night_probe.jsonl"


CSV_KEYS = {"日期": "Date", "契約代號": "Contract", "到期月份(週別)": "ContractMonth(Week)",
            "最後成交價": "Last", "交易時段": "TradingSession"}


def parse_rows(text: str) -> list:
    """期交所 2026-11-04 起由 JSON 改為 CSV（含 BOM、中文欄名）；兩種都收。"""
    if text.lstrip().startswith(("[", "{")):
        return json.loads(text)
    return [{CSV_KEYS.get(k, k): v for k, v in r.items()} for r in csv.DictReader(io.StringIO(text))]


def main() -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    row = {"fetched_at": fetched_at, "url": URL}
    try:
        request = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            row["last_modified"] = response.headers.get("last-modified")
            rows = parse_rows(response.read().decode("utf-8-sig"))
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
