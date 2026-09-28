import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline, degraded_research_result
from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionFinalizer,
    DecisionRepository,
    DecisionResultValidator,
    MomentumEngine,
    PortfolioDecisionApplicationService,
    RevisionHistoryBuilder,
    ScenarioEngine,
    apply_trade_decision,
    build_research_debate,
    build_stance_brief,
    build_team_inputs,
    canonical_sha256,
    decision_bundle_sha256,
    decision_policy_sha256,
    seal_stance_packet,
    seal_trade_decision,
    sentiment_unavailable_report,
    trade_decision_envelope,
)

from test_analysts import bundle_with_event, event_items, report, technical_items
from test_daily_pipeline import FakeRunner
from test_portfolio_risk_decision import policy as base_policy, risk_review
from test_position_sizing import SIZING
from test_stance import bear_items, bull_items
from test_trader import STANCE, items as trade_items


ROOT = Path(__file__).resolve().parents[1]


def tradable_bundle():
    bundle = bundle_with_event()
    bundle["snapshot"]["tradable_symbols"] = ["2330.TW", "2317.TW"]
    bundle["snapshot"]["not_tradable_symbols"] = []
    bundle["snapshot_sha256"] = canonical_sha256(bundle["snapshot"])
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def team_world(bundle=None, decisions=None, settings=None):
    """分析團隊新鏈的完整 fixture：可交易的 2 檔、四份報告、多空辯論、交易決策與現金姿態。"""
    bundle = bundle or tradable_bundle()
    momentum = MomentumEngine(bundle).run()
    unknown = [
        {"symbol": symbol, "outlook": "unknown", "findings": [], "data_gaps": ["NO_FINANCIAL_STATEMENTS"]}
        for symbol in ("2317.TW", "2330.TW")
    ]
    reports = {
        "technical": report(bundle, "technical", technical_items()),
        "fundamental": report(bundle, "fundamental", unknown),
        "event": report(bundle, "event", event_items()),
        "sentiment": sentiment_unavailable_report(bundle),
    }
    research = degraded_research_result(bundle["snapshot"], "research-1")
    bull = seal_stance_packet(build_stance_brief(bundle, momentum, reports, research, "bull")["packet_envelope"], bull_items())
    bear = seal_stance_packet(build_stance_brief(bundle, momentum, reports, research, "bear")["packet_envelope"], bear_items())
    debate = build_research_debate(bundle, bull, bear, "debate-1")
    trade = seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), decisions or trade_items())
    base = settings or base_policy()
    base["position_sizing"] = copy.deepcopy(SIZING)
    policy = apply_trade_decision(base, trade, STANCE, bundle)
    policy["content_sha256"] = decision_policy_sha256(policy)
    team = build_team_inputs(reports, research, STANCE)
    return bundle, momentum, debate, trade, policy, team, reports, research


def finalize(bundle, momentum, debate, trade, policy, team):
    proposal = AllocationOrderEngine(bundle, policy).run(trade)
    scenario = ScenarioEngine(bundle, policy).run(proposal)
    guard = CompetitionGuardV2(bundle, policy).run(proposal, scenario)
    review = risk_review(bundle, proposal, scenario, guard, decision="approve" if guard["passed"] else "reject")
    history = RevisionHistoryBuilder(bundle, policy, trade).create(proposal, scenario, guard, review)
    artifacts = {"proposal": proposal, "scenario": scenario, "guard": guard, "risk_review": review, "revision_history": history}
    result = DecisionFinalizer(bundle, policy, momentum, debate, trade, team).run(proposal, scenario, guard, review, history)
    return artifacts, result


class TeamChainFinalizationTests(unittest.TestCase):
    def test_team_chain_is_approved_rebuilt_and_saved(self):
        bundle, momentum, debate, trade, policy, team, _, _ = team_world()
        artifacts, result = finalize(bundle, momentum, debate, trade, policy, team)

        self.assertEqual(result["status"], "approved", result["errors"])
        self.assertEqual(result["trade_intent_result_id"], "trade-1")
        self.assertEqual(result["team_inputs_sha256"], canonical_sha256(team))
        self.assertEqual([order["symbol"] for order in result["orders"]], ["2317.TW"])
        validator = DecisionResultValidator(bundle, policy, momentum, debate, trade, team)
        self.assertEqual(validator.validate(*[artifacts[name] for name in ("proposal", "scenario", "guard", "risk_review", "revision_history")], result), [])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            for name, payload in {**artifacts, "momentum": momentum, "debate": debate, "intent": trade,
                                  "policy": policy, "decision": result, "team_inputs": team}.items():
                paths[name] = root / ("%s.json" % name)
                paths[name].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            saved = PortfolioDecisionApplicationService(bundle).save_run_files("team-run", root / "repo", paths)
            self.assertTrue(saved["verified"])
            run_dir = DecisionRepository(root / "repo").verify("team-run")
            self.assertTrue((run_dir / "team_inputs.json").exists())

    def test_team_chain_without_team_inputs_is_rejected(self):
        bundle, momentum, debate, trade, policy, _, _, _ = team_world()
        _, result = finalize(bundle, momentum, debate, trade, policy, None)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("team_inputs" in error for error in result["errors"]), result["errors"])

    def test_policy_binding_must_match_trade_decision_and_cash_stance(self):
        bundle, momentum, debate, trade, policy, team, reports, research = team_world()
        tampered = copy.deepcopy(policy)
        tampered["position_sizing"]["conviction_by_symbol"]["2317.TW"] = "high"
        tampered["content_sha256"] = decision_policy_sha256(tampered)
        _, result = finalize(bundle, momentum, debate, trade, tampered, team)
        self.assertTrue(any("conviction_by_symbol" in error for error in result["errors"]), result["errors"])

        other_stance = build_team_inputs(reports, research, dict(STANCE, level="defensive"))
        _, result = finalize(bundle, momentum, debate, trade, policy, other_stance)
        self.assertTrue(any("cash_stance" in error for error in result["errors"]), result["errors"])

        swapped_research = build_team_inputs(reports, dict(research, run_id="research-2"), STANCE)
        _, result = finalize(bundle, momentum, debate, trade, policy, swapped_research)
        self.assertTrue(any("shared_input_sha256" in error or "隔離輸入" in error for error in result["errors"]), result["errors"])


class TeamChainDownstreamTests(unittest.TestCase):
    def test_virtual_account_applies_a_team_chain_decision_run(self):
        from etf_agent.virtual_account import VirtualAccountRepository, VirtualAccountService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = root / "rules.json"
            rules.write_text(json.dumps({"initial_capital_twd": 1_000_000_000, "version": "fixture-v1"}), encoding="utf-8")
            service = VirtualAccountService(VirtualAccountRepository(root / "accounts", "cup-test"))
            service.initialize(rules, "cup-test", "2026-09-18T17:00:00+08:00")
            bundle = tradable_bundle()
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(bundle["snapshot"]), encoding="utf-8")
            prepared = service.prepare_day(snapshot_path, "prepare-team")
            bundle["account_snapshot"] = prepared["account_snapshot"]
            bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
            # 空倉：2317 買進，2330 未持有只能 no_trade。
            decisions = trade_items()
            decisions[1] = dict(decisions[1], intent="no_trade")
            settings = base_policy()
            settings["liquidity_fill_rate"] = "1"
            bundle, momentum, debate, trade, policy, team, _, _ = team_world(bundle, decisions, settings)
            artifacts, result = finalize(bundle, momentum, debate, trade, policy, team)
            self.assertEqual(result["status"], "approved", result["errors"])
            DecisionRepository(root / "decisions").save("team-decision", {
                "decision_input": bundle, "policy": policy, "momentum": momentum, "debate": debate,
                "intent": trade, "decision": result, "team_inputs": team, **artifacts,
            })
            quotes = {row["symbol"]: row["analysis_close_price"] for row in bundle["snapshot"]["latest_prices"]}
            execution = root / "execution.json"
            execution.write_text(json.dumps({
                "price_basis": "unadjusted", "available_at": "2026-09-20T01:30:00+00:00", "execution_at": "2026-09-20T01:31:00+00:00",
                "quotes": {symbol: {"tradable": True, "available_lots": 10000, "execution_price": price} for symbol, price in quotes.items()},
            }), encoding="utf-8")
            close = root / "close.json"
            close.write_text(json.dumps({
                "price_basis": "unadjusted", "available_at": "2026-09-20T08:00:00+00:00",
                "quotes": {symbol: {"close_price": price} for symbol, price in quotes.items()},
            }), encoding="utf-8")
            applied = service.apply_decision(root / "decisions", "team-decision", execution, close, "2026-09-22", "close-team")

        self.assertTrue(applied["transition"]["execution"]["fills"])
        self.assertEqual([item["symbol"] for item in applied["state"]["positions"]], ["2317.TW"])


class CashStanceAgentTests(unittest.TestCase):
    def test_cash_stance_is_validated_and_retried(self):
        bundle, momentum, debate, trade, _, _, reports, research = team_world()
        runner = FakeRunner({"cash_stance": [dict(STANCE, evidence_ids=["no-such"]), STANCE]})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            stance = pipeline.run_cash_stance(bundle, momentum, reports, research, trade)
            brief = json.loads((Path(directory) / "brief_cash_stance.json").read_text(encoding="utf-8"))

        self.assertEqual(stance["level"], "neutral")
        self.assertEqual(len(runner.prompts), 2)
        self.assertEqual(brief["trade_intents"], {"buy": 1, "hold": 1})
        self.assertNotIn("%", json.dumps(brief["trade_intents"]))


if __name__ == "__main__":
    unittest.main()
