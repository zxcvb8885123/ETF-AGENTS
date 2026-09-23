#!/usr/bin/env python3
"""投資組合買賣決策 P0-P2 的結構化工具入口。"""

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
        raise RuntimeError("找不到 ETF-AGENTS 專案根目錄")


ROOT = ProjectRootLocator(Path(__file__)).locate()
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.decision import (  # noqa: E402
    DecisionToolError,
    PortfolioDecisionApplicationService,
)


class PortfolioDecisionApplication:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        args = self.build_parser().parse_args(argv)
        try:
            service = PortfolioDecisionApplicationService.from_path(args.bundle)
            if args.command == "validate-input":
                result = service.validate_input()
                self.emit(result)
                return 0 if result["valid"] else 2
            if args.command == "compute-momentum":
                result = service.compute_momentum(args.output)
                self.emit({"ok": True, "data": result, "output": str(args.output) if args.output else None})
                return 0
            if args.command == "build-role-input":
                result = service.build_role_input(
                    args.role, args.momentum, args.output
                )
                self.emit(
                    {
                        "ok": True,
                        "data": result,
                        "output": str(args.output) if args.output else None,
                    }
                )
                return 0
            if args.command == "seal-artifact":
                result = service.seal_artifact_file(args.input, args.output)
                self.emit(
                    {
                        "ok": True,
                        "data": result,
                        "output": str(args.output) if args.output else None,
                    }
                )
                return 0
            if args.command == "validate-momentum":
                result = service.validate_momentum_file(args.input)
            elif args.command == "validate-buy":
                result = service.validate_packet_file(
                    "buy", args.momentum, args.input, args.output
                )
            elif args.command == "validate-sell":
                result = service.validate_packet_file(
                    "sell", args.momentum, args.input, args.output
                )
            elif args.command == "validate-debate":
                result = service.validate_debate_file(
                    args.momentum, args.input, args.output
                )
            else:
                result = service.validate_intent_file(
                    args.momentum, args.debate, args.input, args.output
                )
            self.emit(result)
            return 0 if result["valid"] else 2
        except (DecisionToolError, OSError, TypeError, ValueError) as error:
            self.emit({"ok": False, "error": str(error)})
            return 1

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--bundle",
            type=Path,
            default=self.root / "artifacts" / "decision_input_latest.json",
        )
        commands = parser.add_subparsers(dest="command", required=True)
        commands.add_parser("validate-input", help="驗證共用 DecisionInputBundle")

        momentum = commands.add_parser(
            "compute-momentum", help="確定性計算 MomentumResult 與市場狀態"
        )
        momentum.add_argument("--output", type=Path)

        validate_momentum = commands.add_parser(
            "validate-momentum", help="完整重算並驗證 MomentumResult"
        )
        validate_momentum.add_argument("--input", type=Path, required=True)

        role_input = commands.add_parser(
            "build-role-input", help="建立不含對方 packet 的 Buy／Sell 隔離輸入"
        )
        role_input.add_argument("--role", choices=("buy", "sell"), required=True)
        role_input.add_argument("--momentum", type=Path, required=True)
        role_input.add_argument("--output", type=Path)

        seal = commands.add_parser(
            "seal-artifact", help="為 Buy／Sell／Debate／Intent artifact 計算內容雜湊"
        )
        seal.add_argument("--input", type=Path, required=True)
        seal.add_argument("--output", type=Path)

        for name, help_text in (
            ("validate-buy", "驗證獨立 BuyIntentPacket"),
            ("validate-sell", "驗證獨立 SellIntentPacket"),
            ("validate-debate", "驗證 Buy／Sell 共同輸入與互相隔離"),
        ):
            command = commands.add_parser(name, help=help_text)
            command.add_argument("--momentum", type=Path, required=True)
            command.add_argument("--input", type=Path, required=True)
            command.add_argument("--output", type=Path)

        intent = commands.add_parser(
            "validate-intent", help="驗證 TradeIntentResult 未新增事實或交易數字"
        )
        intent.add_argument("--momentum", type=Path, required=True)
        intent.add_argument("--debate", type=Path, required=True)
        intent.add_argument("--input", type=Path, required=True)
        intent.add_argument("--output", type=Path)
        return parser

    @staticmethod
    def emit(payload) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(PortfolioDecisionApplication().run())
