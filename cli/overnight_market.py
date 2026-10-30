"""隔夜市場背景（Yahoo Finance 美股指數與台股代理）補抓與檢視的薄 CLI。"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from etf_agent.perception.overnight_market import OvernightMarketError, build_overnight_market_input, capture_overnight_market


def main():
    parser = argparse.ArgumentParser(description="補抓 ^GSPC、^IXIC、^SOX、EWT、TSM、^N225、^KS11、^VIX 日線與台指期夜盤報價，或檢視截止前的隔夜市場背景")
    sub = parser.add_subparsers(dest="action", required=True)
    capture = sub.add_parser("capture", help="抓取並保存不可變原始回應")
    capture.add_argument("--output-root", type=Path, default=ROOT / "artifacts" / "data" / "overnight_market")
    show = sub.add_parser("show", help="以截止前最後一次抓取重算（只顯示，不寫入決策輸入）")
    show.add_argument("--output-root", type=Path, default=ROOT / "artifacts" / "data" / "overnight_market")
    show.add_argument("--approvals", type=Path, default=ROOT / "config" / "overnight_market_sources.json")
    show.add_argument("--decision-cutoff", default=datetime.now(timezone.utc).isoformat())
    show.add_argument("--latest-trade-date", required=True, help="台股最近交易日 YYYY-MM-DD；視窗從該日 13:30（台北）起算")
    args = parser.parse_args()
    try:
        if args.action == "capture":
            output = capture_overnight_market(args.output_root)
            code = 2 if output["failures"] else 0
        else:
            pair = build_overnight_market_input(args.output_root, args.approvals, "preview", args.decision_cutoff,
                                                args.latest_trade_date)
            if pair is None:
                raise OvernightMarketError("截止時間前沒有任何隔夜市場抓取")
            result = pair["result"]
            output = {key: result.get(key) for key in ("status", "outlook", "signal_pct", "proxy_signal_pct", "window", "findings",
                                                   "agreement", "components", "night_session", "data_gaps")}
            code = 0 if result["status"] == "available" else 2
    except OvernightMarketError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
