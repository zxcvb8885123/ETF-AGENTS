"""跨市場隔夜訊號離線回測的薄 CLI：fetch 抓兩年歷史，analyze 做事先登記的統計。"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from etf_agent.research.cross_market_study import CrossMarketStudyError, fetch_history, fetch_pool, run_analysis
from etf_agent.research.cross_market_study.contracts import DEFAULT_HISTORY_DAYS


def main():
    parser = argparse.ArgumentParser(description="跨市場隔夜訊號回測（Yahoo 日線＋FinMind 台指期盤後，台北 07:00 決策時點）")
    parser.add_argument("--root", type=Path, default=ROOT / "artifacts" / "cross_market_study")
    sub = parser.add_subparsers(dest="action", required=True)
    fetch = sub.add_parser("fetch", help="抓歷史並不可變保存（需要 FINMIND_TOKEN）")
    fetch.add_argument("--days", type=int, default=DEFAULT_HISTORY_DAYS)
    pool = sub.add_parser("fetch-pool", help="讀本機資料庫的交易池日線與官方成交均價並凍結保存")
    pool.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    pool.add_argument("--decision-input", type=Path, required=True, help="取交易池股票清單的 decision_input.json")
    pool.add_argument("--days", type=int, default=DEFAULT_HISTORY_DAYS)
    sub.add_parser("analyze", help="用最新保存的歷史重建並統計")
    args = parser.parse_args()
    try:
        if args.action == "fetch":
            output = fetch_history(args.root, args.days)
        elif args.action == "fetch-pool":
            bundle = json.loads(args.decision_input.read_text(encoding="utf-8"))
            output = fetch_pool(args.root, args.database, [row["symbol"] for row in bundle["snapshot"]["latest_prices"]], args.days)
        else:
            output = run_analysis(args.root)
    except CrossMarketStudyError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
