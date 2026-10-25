#!/usr/bin/env python3
"""市場情緒研究 Agent 的結構化、唯讀工具入口。"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence


class ProjectRootLocator:
    def __init__(self, script_path: Path):
        self.script_path = script_path

    def locate(self) -> Path:
        for parent in self.script_path.resolve().parents:
            if (parent / "src" / "etf_agent").is_dir():
                return parent
        raise RuntimeError("找不到 ETF_AGENTS 專案根目錄")


ROOT = ProjectRootLocator(Path(__file__)).locate()
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.perception import (  # noqa: E402
    MarketPerceptionApplicationService,
    PerceptionToolError,
)


class SentimentResearchApplication:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        args = self.build_parser().parse_args(argv)
        try:
            service = MarketPerceptionApplicationService.from_path(args.bundle)
            if args.command == "build-result":
                labels = service.read_json(args.labels, " SentimentLabelBundle").get("labels")
                result = service.build_result(args.symbols, labels, args.run_id)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2)
            elif args.command == "status":
                result = service.tools.status()
            elif args.command == "list-covered-symbols":
                result = service.tools.list_covered_symbols()
            elif args.command == "get-sentiment-items":
                result = service.tools.get_sentiment_items(
                    args.symbol, args.lookback_days
                )
            elif args.command == "aggregate-sentiment":
                label_payload = service.read_json(args.labels, " SentimentLabelBundle")
                result = service.tools.aggregate_sentiment(
                    args.symbol,
                    label_payload.get("labels"),
                    args.lookback_days,
                    args.min_items,
                    args.min_sources,
                )
            else:
                result = service.validate_file(args.input, args.output)
                self.emit(result)
                return 0 if result["valid"] else 2
        except (PerceptionToolError, OSError, TypeError, ValueError) as error:
            self.emit({"ok": False, "error": str(error)})
            return 1
        self.emit({"ok": True, "data": result})
        return 0

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--bundle",
            type=Path,
            default=self.root / "artifacts" / "data" / "perception_data_latest.json",
        )
        commands = parser.add_subparsers(dest="command", required=True)
        build = commands.add_parser("build-result", help="全池聚合情緒並驗證結果，拒絕覆寫")
        build.add_argument("--symbols", nargs="+", required=True)
        build.add_argument("--labels", type=Path, required=True)
        build.add_argument("--run-id", required=True)
        build.add_argument("--output", type=Path, required=True)
        commands.add_parser("status", help="檢查資料版本、cutoff、來源與覆蓋")
        commands.add_parser("list-covered-symbols", help="列出有情緒資料的股票")

        sentiment = commands.add_parser(
            "get-sentiment-items", help="取得待判讀、已去除 cutoff 後資料的情緒項目"
        )
        sentiment.add_argument("--symbol", required=True)
        sentiment.add_argument("--lookback-days", type=int, default=28)

        aggregate = commands.add_parser(
            "aggregate-sentiment", help="驗證逐筆標籤並確定性聚合情緒"
        )
        aggregate.add_argument("--symbol", required=True)
        aggregate.add_argument("--labels", type=Path, required=True)
        aggregate.add_argument("--lookback-days", type=int, default=28)
        aggregate.add_argument("--min-items", type=int, default=5)
        aggregate.add_argument("--min-sources", type=int, default=2)

        validate = commands.add_parser(
            "validate-result", help="重算所有數值並驗證 MarketPerceptionResult"
        )
        validate.add_argument("--input", type=Path, required=True)
        validate.add_argument("--output", type=Path)
        return parser

    @staticmethod
    def emit(payload) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(SentimentResearchApplication().run())
