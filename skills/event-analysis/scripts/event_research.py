#!/usr/bin/env python3
"""事件研究 Agent 的結構化、唯讀工具入口。"""

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

from etf_agent.research import EventResearchApplicationService, ResearchToolError  # noqa: E402


class EventResearchApplication:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        args = self.build_parser().parse_args(argv)
        try:
            service = EventResearchApplicationService.from_paths(args.snapshot, args.database)
            if args.command == "status":
                result = service.tools.status()
            elif args.command == "list-events":
                result = service.tools.list_events(
                    symbol=args.symbol,
                    document_type=args.document_type,
                    lookback_days=args.lookback_days,
                    limit=args.limit,
                )
            elif args.command == "read-source":
                result = service.tools.read_source(args.evidence_id)
            elif args.command == "get-company-facts":
                result = service.tools.get_company_facts(args.symbol, args.limit)
            elif args.command == "find-related-events":
                result = service.tools.find_related_events(
                    args.symbol, args.event_id, args.limit
                )
            elif args.command == "analyze-event-context":
                result = service.tools.analyze_event_context(args.evidence_id)
            elif args.command == "get-price-features":
                result = service.tools.get_price_features(
                    args.symbol, args.event_published_at
                )
            elif args.command == "validate-result":
                result = service.validate_file(args.input, args.output)
                self.emit(result)
                return 0 if result["valid"] else 2
            else:
                result = service.validate_debate_file(args.input, args.output)
                self.emit(result)
                return 0 if result["valid"] else 2
        except (ResearchToolError, OSError, ValueError) as error:
            self.emit({"ok": False, "error": str(error)})
            return 1
        self.emit({"ok": True, "data": result})
        return 0

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--snapshot",
            type=Path,
            default=self.root / "artifacts" / "research_snapshot_latest.json",
        )
        parser.add_argument(
            "--database",
            type=Path,
            default=self.root / "var" / "etf_agent.db",
        )
        commands = parser.add_subparsers(dest="command", required=True)
        commands.add_parser("status", help="確認 Snapshot 可用性與資料數量")

        list_events = commands.add_parser("list-events", help="列出截止時間前的事件")
        list_events.add_argument("--symbol")
        list_events.add_argument("--document-type")
        list_events.add_argument("--lookback-days", type=int, default=45)
        list_events.add_argument("--limit", type=int, default=100)

        read_source = commands.add_parser("read-source", help="讀取單一正式來源")
        read_source.add_argument("--evidence-id", required=True)

        company = commands.add_parser("get-company-facts", help="取得公司歷史事實")
        company.add_argument("--symbol", required=True)
        company.add_argument("--limit", type=int, default=30)

        related = commands.add_parser("find-related-events", help="查找舊聞、更新與反證")
        related.add_argument("--symbol", required=True)
        related.add_argument("--event-id")
        related.add_argument("--limit", type=int, default=30)

        context = commands.add_parser(
            "analyze-event-context",
            help="建立事件基準、新穎性、歷史脈絡與事件後行情資料包",
        )
        context.add_argument("--evidence-id", required=True)

        price = commands.add_parser("get-price-features", help="計算截止時間前行情特徵")
        price.add_argument("--symbol", required=True)
        price.add_argument("--event-published-at")

        validate = commands.add_parser("validate-result", help="驗證並保存 ResearchResult")
        validate.add_argument("--input", type=Path, required=True)
        validate.add_argument("--output", type=Path)

        debate = commands.add_parser(
            "validate-debate", help="驗證事實、多方、空方與裁決子 Agent packet"
        )
        debate.add_argument("--input", type=Path, required=True)
        debate.add_argument("--output", type=Path)
        return parser

    @staticmethod
    def emit(payload) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(EventResearchApplication().run())
