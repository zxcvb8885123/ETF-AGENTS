#!/usr/bin/env python3
"""投資組合買賣決策、配置、風控與保存的結構化工具入口。"""

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
    DEFAULT_LOOKBACK_BARS,
    DecisionToolError,
    PortfolioDecisionApplicationService,
)


class PortfolioDecisionApplication:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        args = self.build_parser().parse_args(argv)
        try:
            if args.command == "build-input":
                result = PortfolioDecisionApplicationService.build_input_file(
                    args.snapshot,
                    args.database,
                    args.rules,
                    args.bundle,
                    research_paths=args.research,
                    trading_status_path=args.trading_status,
                    account_path=args.account_snapshot,
                    lookback_bars=args.lookback_bars,
                )
                self.emit(result)
                return 0 if result["valid"] else 2
            if args.command == "build-role-brief":
                result = PortfolioDecisionApplicationService.build_role_brief_file(
                    args.role_input, args.output
                )
                self.emit(
                    {
                        "ok": True,
                        "role": result["role"],
                        "symbol_count": len(result["symbols"]),
                        "brief_sha256": result["brief_sha256"],
                        "output": str(args.output) if args.output else None,
                    }
                )
                return 0
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
            if args.command == "build-policy":
                result = service.build_policy_file(args.template, args.sector, args.output)
                self.emit(result)
                return 0
            if args.command == "validate-sizing":
                result = service.validate_sizing_file(args.intent, args.input)
                self.emit(result)
                return 0 if result["valid"] else 2
            if args.command == "apply-sizing":
                result = service.apply_sizing_file(args.policy, args.intent, args.sizing, args.output)
                self.emit(result)
                return 0
            if args.command == "compute-proposal":
                result = service.compute_proposal_file(
                    args.momentum, args.debate, args.intent, args.policy, args.output
                )
                self.emit({"ok": True, "data": result, "output": str(args.output) if args.output else None})
                return 0
            if args.command == "validate-proposal":
                result = service.validate_proposal_file(
                    args.momentum, args.debate, args.intent, args.policy, args.input
                )
                self.emit(result)
                return 0 if result["valid"] else 2
            if args.command == "compute-scenarios":
                result = service.compute_scenario_file(
                    args.momentum, args.debate, args.intent, args.policy, args.proposal, args.output
                )
                self.emit({"ok": True, "data": result, "output": str(args.output) if args.output else None})
                return 0
            if args.command == "compute-guard":
                result = service.compute_guard_file(
                    args.momentum, args.debate, args.intent, args.policy, args.proposal,
                    args.scenario, args.output
                )
                self.emit({"ok": True, "data": result, "output": str(args.output) if args.output else None})
                return 0
            if args.command == "validate-risk":
                result = service.validate_risk_file(
                    args.momentum, args.debate, args.intent, args.policy, args.proposal,
                    args.scenario, args.guard, args.input
                )
                self.emit(result)
                return 0 if result["valid"] else 2
            if args.command == "revise-proposal":
                result = service.revise_proposal_file(
                    args.momentum, args.debate, args.intent, args.policy, args.proposal, args.scenario,
                    args.guard, args.review, args.output
                )
                self.emit({"ok": True, "data": result, "output": str(args.output) if args.output else None})
                return 0
            if args.command == "finalize":
                result = service.finalize_file(
                    args.momentum, args.debate, args.intent, args.policy, args.proposal, args.scenario,
                    args.guard, args.review, args.history, args.output
                )
                self.emit({"ok": True, "data": result, "output": str(args.output) if args.output else None})
                return 0
            if args.command == "validate-decision":
                result = service.validate_decision_file(
                    args.momentum, args.debate, args.intent, args.policy, args.proposal, args.scenario,
                    args.guard, args.review, args.history, args.input
                )
                self.emit(result)
                return 0 if result["valid"] else 2
            if args.command == "save-run":
                paths = {
                    "intent": args.intent,
                    "momentum": args.momentum,
                    "debate": args.debate,
                    "policy": args.policy,
                    "proposal": args.proposal,
                    "scenario": args.scenario,
                    "guard": args.guard,
                    "risk_review": args.review,
                    "revision_history": args.history,
                    "decision": args.decision,
                }
                if args.team_inputs is not None:
                    paths["team_inputs"] = args.team_inputs
                result = service.save_run_files(args.run_id, args.repository, paths)
                self.emit(result)
                return 0
            if args.command in {"build-history", "append-history"}:
                result = service.build_history_file(
                    args.momentum, args.debate, args.intent, args.policy,
                    args.proposal, args.scenario, args.guard,
                    args.review, args.output,
                    args.history if args.command == "append-history" else None,
                )
                self.emit({"ok": True, "data": result, "output": str(args.output) if args.output else None})
                return 0
            if args.command == "validate-history":
                result = service.validate_history_file(
                    args.momentum, args.debate, args.intent, args.policy, args.input
                )
                self.emit(result)
                return 0 if result["valid"] else 2
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
        build_input = commands.add_parser(
            "build-input",
            help="由 Snapshot、SQLite 歷史行情與決策規則建立 DecisionInputBundle（寫入 --bundle）；未附帳戶時輸出供 attach-account 使用的樣板",
        )
        build_input.add_argument(
            "--snapshot", type=Path, default=self.root / "artifacts" / "research_snapshot_latest.json"
        )
        build_input.add_argument("--database", type=Path, default=self.root / "var" / "etf_agent.db")
        build_input.add_argument(
            "--rules", type=Path, default=self.root / "config" / "decision_rules.json"
        )
        build_input.add_argument(
            "--research", type=Path, action="append", default=[],
            help="已驗證 ResearchResult，可重複指定",
        )
        build_input.add_argument("--trading-status", type=Path, help="trading_status.py build-bundle 的輸出")
        build_input.add_argument("--account-snapshot", type=Path)
        build_input.add_argument("--lookback-bars", type=int, default=DEFAULT_LOOKBACK_BARS)

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

        role_brief = commands.add_parser(
            "build-role-brief",
            help="由單一 Buy／Sell 角色輸入產生給子 Agent 閱讀的精簡摘要（不讀 --bundle）",
        )
        role_brief.add_argument("--role-input", type=Path, required=True)
        role_brief.add_argument("--output", type=Path, required=True)

        seal = commands.add_parser(
            "seal-artifact", help="為 Policy、Agent packet 或決策 artifact 計算內容雜湊"
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

        build_policy = commands.add_parser(
            "build-policy", help="由策略樣板、bundle 硬性規則與官方產業分類建立 DecisionPolicy"
        )
        build_policy.add_argument(
            "--template", type=Path, default=self.root / "config" / "decision_policy.json"
        )
        build_policy.add_argument(
            "--sector", type=Path, default=self.root / "data" / "sector_classification.json"
        )
        build_policy.add_argument("--output", type=Path, required=True)

        validate_sizing = commands.add_parser(
            "validate-sizing", help="驗證風控子 Agent 的 SizingPlan 等級、候選覆蓋與證據"
        )
        validate_sizing.add_argument("--intent", type=Path, required=True)
        validate_sizing.add_argument("--input", type=Path, required=True)

        apply_sizing = commands.add_parser(
            "apply-sizing", help="將已驗證 SizingPlan 綁入新版 DecisionPolicy（不輸出權重）"
        )
        apply_sizing.add_argument("--policy", type=Path, required=True)
        apply_sizing.add_argument("--intent", type=Path, required=True)
        apply_sizing.add_argument("--sizing", type=Path, required=True)
        apply_sizing.add_argument("--output", type=Path, required=True)

        proposal = commands.add_parser(
            "compute-proposal", help="由交易意圖以一張 1,000 股計算配置、訂單、費稅與現金"
        )
        proposal.add_argument("--intent", type=Path, required=True)
        proposal.add_argument("--momentum", type=Path, required=True)
        proposal.add_argument("--debate", type=Path, required=True)
        proposal.add_argument("--policy", type=Path, required=True)
        proposal.add_argument("--output", type=Path)

        validate_proposal = commands.add_parser(
            "validate-proposal", help="完整重算並驗證配置與訂單提案"
        )
        validate_proposal.add_argument("--intent", type=Path, required=True)
        validate_proposal.add_argument("--momentum", type=Path, required=True)
        validate_proposal.add_argument("--debate", type=Path, required=True)
        validate_proposal.add_argument("--policy", type=Path, required=True)
        validate_proposal.add_argument("--input", type=Path, required=True)

        scenarios = commands.add_parser(
            "compute-scenarios", help="計算基準、價格下跌與流動性壓力情境"
        )
        scenarios.add_argument("--policy", type=Path, required=True)
        self._add_upstream_intent_inputs(scenarios)
        scenarios.add_argument("--proposal", type=Path, required=True)
        scenarios.add_argument("--output", type=Path)

        guard = commands.add_parser(
            "compute-guard", help="對取整後組合執行完整硬性規則與全部基準檢查"
        )
        guard.add_argument("--policy", type=Path, required=True)
        self._add_upstream_intent_inputs(guard)
        guard.add_argument("--proposal", type=Path, required=True)
        guard.add_argument("--scenario", type=Path, required=True)
        guard.add_argument("--output", type=Path)

        validate_risk = commands.add_parser(
            "validate-risk", help="驗證 Portfolio Risk 審查與修正要求"
        )
        self._add_risk_inputs(validate_risk, include_intent=True, include_review=False)
        validate_risk.add_argument("--input", type=Path, required=True)

        revise = commands.add_parser(
            "revise-proposal", help="套用 allowlist 內的風險修正並重算提案"
        )
        self._add_risk_inputs(revise, include_intent=True)
        revise.add_argument("--output", type=Path)

        for name, help_text in (
            ("build-history", "建立 revision 0 修正歷程"),
            ("append-history", "將下一版提案與審查追加至修正歷程"),
        ):
            history = commands.add_parser(name, help=help_text)
            self._add_upstream_intent_inputs(history)
            history.add_argument("--policy", type=Path, required=True)
            history.add_argument("--proposal", type=Path, required=True)
            history.add_argument("--scenario", type=Path, required=True)
            history.add_argument("--guard", type=Path, required=True)
            history.add_argument("--review", type=Path, required=True)
            if name == "append-history":
                history.add_argument("--history", type=Path, required=True)
            history.add_argument("--output", type=Path)

        validate_history = commands.add_parser(
            "validate-history", help="重播並驗證完整提案修正鏈"
        )
        self._add_upstream_intent_inputs(validate_history)
        validate_history.add_argument("--policy", type=Path, required=True)
        validate_history.add_argument("--input", type=Path, required=True)

        finalize = commands.add_parser(
            "finalize", help="重建全部輸入並產生 approved、rejected 或 no_trade"
        )
        self._add_risk_inputs(finalize, include_intent=True, include_history=True)
        finalize.add_argument("--output", type=Path)

        validate_decision = commands.add_parser(
            "validate-decision", help="完整重算並驗證最終 DecisionResult"
        )
        self._add_risk_inputs(validate_decision, include_intent=True, include_history=True)
        validate_decision.add_argument("--input", type=Path, required=True)

        save = commands.add_parser("save-run", help="以不可變 manifest 原子保存完整執行")
        self._add_risk_inputs(save, include_intent=True, include_history=True)
        save.add_argument("--decision", type=Path, required=True)
        save.add_argument("--run-id", required=True)
        save.add_argument(
            "--team-inputs", type=Path,
            help="分析團隊新鏈必填：四份分析報告、事件研究與現金姿態，供完整重建驗證",
        )
        save.add_argument("--repository", type=Path, default=self.root / "artifacts" / "portfolio_decisions")
        return parser

    @staticmethod
    def _add_upstream_intent_inputs(parser) -> None:
        parser.add_argument("--momentum", type=Path, required=True)
        parser.add_argument("--debate", type=Path, required=True)
        parser.add_argument("--intent", type=Path, required=True)

    @staticmethod
    def _add_risk_inputs(
        parser, include_intent: bool, include_review: bool = True,
        include_history: bool = False,
    ) -> None:
        if include_intent:
            PortfolioDecisionApplication._add_upstream_intent_inputs(parser)
        parser.add_argument("--policy", type=Path, required=True)
        parser.add_argument("--proposal", type=Path, required=True)
        parser.add_argument("--scenario", type=Path, required=True)
        parser.add_argument("--guard", type=Path, required=True)
        if include_review:
            parser.add_argument("--review", type=Path, required=True)
        if include_history:
            parser.add_argument("--history", type=Path, required=True)

    @staticmethod
    def emit(payload) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(PortfolioDecisionApplication().run())
