"""FinMind 個股新聞來源核對的薄 CLI；核准只對保存之後的決策截止時間生效。"""
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from etf_agent.perception.company_news import record_source_review


def main():
    parser = argparse.ArgumentParser(description='保存 FinMind 個股新聞的來源使用核對')
    sub = parser.add_subparsers(dest='action', required=True)
    review = sub.add_parser('review', help='抓取 FinMind 規範原文並保存核對；只對保存後的決策截止時間生效')
    review.add_argument('--database', type=Path, required=True)
    review.add_argument('--status', choices=('approved', 'unverified', 'restricted'), required=True)
    review.add_argument('--evidence', required=True, help='核對依據，例如稽核紀錄路徑與判斷')
    args = parser.parse_args()
    output = record_source_review(args.database, args.status, args.evidence)
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
