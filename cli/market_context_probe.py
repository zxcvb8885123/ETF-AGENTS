"""公開市場背景補抓的薄 CLI。"""

import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from etf_agent.data.market_probe import capture_market_context


def main():
    parser = argparse.ArgumentParser(description="補抓公開大盤成交、指數與法人資料，不產生情緒結論")
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--output-root", type=Path, default=ROOT / "artifacts/market_context_probes")
    args = parser.parse_args()
    print(json.dumps(capture_market_context(args.trade_date, args.output_root), ensure_ascii=False))


if __name__ == "__main__":
    main()
