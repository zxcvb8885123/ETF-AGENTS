#!/usr/bin/env python3
"""Check whether the project can safely run a competition submission."""

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "config" / "competition_rules.json"
UNIVERSE_PATH = ROOT / "data" / "official_universe.csv"
ACTIVE_ETF_PATH = ROOT / "data" / "active_etf_top10.csv"


def row_count(path: Path) -> int:
    with path.open(encoding="utf-8", newline="") as handle:
        return len(list(csv.DictReader(handle)))


def main() -> int:
    failures = []
    pending = []
    try:
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        print("PASS  規則設定已載入：%s" % rules["competition"])
    except (OSError, ValueError, KeyError) as error:
        failures.append("競賽規則設定無法讀取：%s" % error)
        rules = {}

    if rules and rules.get("min_positions") == 20 and rules.get("max_positions") == 30:
        print("PASS  持股數限制：20–30 檔")
    else:
        failures.append("持股數限制設定不正確")

    universe_count = row_count(UNIVERSE_PATH)
    if universe_count == rules.get("universe_size"):
        print("PASS  官方交易池：150 檔已載入")
    else:
        pending.append("官方交易池目前 %d/150 檔；請從競賽平台匯入" % universe_count)

    active_etf_count = row_count(ACTIVE_ETF_PATH)
    if active_etf_count:
        print("PASS  Active Share 基準：%d 筆已載入" % active_etf_count)
    else:
        pending.append("主動式 ETF 前十大資料尚未載入；送件前必須補齊")

    for message in failures:
        print("FAIL  " + message)
    for message in pending:
        print("TODO  " + message)

    if failures:
        return 1
    print("\n核心專案初始化完成；資料未齊前，CompetitionGuard 會拒絕送件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
