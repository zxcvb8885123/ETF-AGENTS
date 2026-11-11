#!/usr/bin/env python3
"""估值收集與稽核（V1）：抓證交所 BWIBBU_ALL 與 FinMind TaiwanStockPER、逐檔核對、不可變保存。

只收集與稽核；不進決策、不進簡報、不接 Agent。失敗請由呼叫端降級處理，不得讓 D-Plan 失敗。
退出碼：0 全部核對相符；2 有股票被拒絕（預期降級）；1 整份失敗（官方端點、金鑰或格式）。
"""
import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data.universe import UniverseLoader
from etf_agent.data.valuation import (
    FinMindValuationProvider, OfficialValuationProvider, ValuationCollector, listed_codes, summarize_valuation_audits, validate_valuation_run,
)


def main():
    parser = argparse.ArgumentParser(description="估值收集與稽核（V1，只收上市股）")
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="抓取、核對並保存一個 run")
    collect.add_argument("--universe", type=Path, default=ROOT / "data/official_universe.csv")
    collect.add_argument("--output-root", type=Path, default=ROOT / "artifacts/valuation_collection")
    collect.add_argument("--history-start", type=date.fromisoformat,
                         help="FinMind 歷史起日（YYYY-MM-DD）；預設只補資料日前 14 天，第一次建歷史請明確指定")
    collect.add_argument("--symbols", nargs="+", help="只收指定上市代號（不含 .TW），須在交易池內")
    check = sub.add_parser("validate", help="以原始回應整份重建並比對")
    check.add_argument("run_id")
    check.add_argument("--output-root", type=Path, default=ROOT / "artifacts/valuation_collection")
    summary_cmd = sub.add_parser("audit-summary", help="唯讀：逐資料日彙總已保存的稽核結果（不連外部端點）")
    summary_cmd.add_argument("--output-root", type=Path, default=ROOT / "artifacts/valuation_collection")
    args = parser.parse_args()
    if args.command == "audit-summary":
        print(json.dumps(summarize_valuation_audits(args.output_root), ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate":
        print(json.dumps(validate_valuation_run(args.output_root, args.run_id), ensure_ascii=False, indent=2))
        return 0
    codes = listed_codes(UniverseLoader().load(args.universe))
    if args.symbols:
        if not set(args.symbols) <= set(codes):
            raise ValueError("指定股票不在交易池的上市股內")
        codes = list(args.symbols)
    summary = ValuationCollector(args.output_root, OfficialValuationProvider(), FinMindValuationProvider()).collect(
        codes, args.history_start)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if summary["rejected"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as error:
        message = str(error).replace(os.environ.get("FINMIND_TOKEN", "\0"), "***")
        print("估值收集停止：%s" % message, file=sys.stderr)
        raise SystemExit(1)
