"""台股恐懼貪婪指標補抓與檢視的薄 CLI。"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from etf_agent.perception.market_fear_greed import FearGreedError, build_fear_greed_input, capture_fear_greed


def main():
    parser = argparse.ArgumentParser(description="補抓期交所 VIX、Put/Call 比與 FinMind 融資，或檢視截止前的恐懼貪婪分數")
    sub = parser.add_subparsers(dest="action", required=True)
    capture = sub.add_parser("capture", help="抓取並保存不可變原始回應")
    capture.add_argument("--output-root", type=Path, default=ROOT / "artifacts" / "data" / "market_fear_greed")
    show = sub.add_parser("show", help="以截止前最後一次抓取重算分數（只顯示，不寫入決策輸入）")
    show.add_argument("--output-root", type=Path, default=ROOT / "artifacts" / "data" / "market_fear_greed")
    show.add_argument("--approvals", type=Path, default=ROOT / "config" / "market_fear_greed_sources.json")
    show.add_argument("--decision-cutoff", default=datetime.now(timezone.utc).isoformat())
    args = parser.parse_args()
    try:
        if args.action == "capture":
            output = capture_fear_greed(args.output_root)
            code = 2 if output["failures"] else 0
        else:
            pair = build_fear_greed_input(args.output_root, args.approvals, "preview", args.decision_cutoff)
            if pair is None:
                raise FearGreedError("截止時間前沒有任何恐懼貪婪抓取")
            result = pair["result"]
            output = {key: result[key] for key in ("status", "score", "band", "outlook", "findings", "components", "data_gaps")}
            code = 0 if result["status"] == "available" else 2
    except FearGreedError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
