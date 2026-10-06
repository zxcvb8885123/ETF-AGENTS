#!/usr/bin/env python3
"""新聞候選核對與診斷的薄 CLI；不呼叫完整決策鏈。"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from etf_agent.perception.news_service import run_news_preparation


def main():
    parser = argparse.ArgumentParser(description="核對資料庫新聞候選、保存原頁版本並驗證全量情緒標註")
    parser.add_argument("--database", type=Path, default=ROOT / "var/etf_agent.db")
    parser.add_argument("--captures", type=Path, required=True)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run_news_preparation(args.database, args.captures, args.output_root, args.labels)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"valid": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
