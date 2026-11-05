"""cli/portfolio_decision.py 的決策庫參數（--decision-repository）。

cash_ratio_range（版本閘門）的前次比例來自前次決策（anchor.status=prior_decision）時，finalize／validate-decision
必須以決策庫重讀前次 run 重算；CLI 原本沒傳決策庫，這兩個指令一律拒絕。沒有閘門的舊 run 不讀決策庫，
finalize 輸出逐位元不變（以改動前算出的固定雜湊驗證）。
"""

import argparse
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionRepository,
    RevisionHistoryBuilder,
    ScenarioEngine,
    build_prior_decision_context,
    read_decision_run,
)

from test_cash_ratio_range import GatedWorld
from test_portfolio_risk_decision import chain_inputs, risk_review
from test_prior_decision import integrated_world, later_bundle, sealed_run

ROOT = Path(__file__).resolve().parents[2]
# 改動前（2026-11-05）以同程序 CLI 對同一 fixture 算出的 finalize 輸出 content_sha256：沒有閘門時必須逐位元不變。
LEGACY_CHAIN_DECISION_SHA256 = "3e55dd9058e515596784453338731139e31206826924779b1b32381349ccf4f0"
LEGACY_PRIOR_DECISION_SHA256 = "1ae2de98322c608e186b56048be12182cb3a6fa0a791988621a8a752666e4bae"


def load_cli():
    spec = importlib.util.spec_from_file_location("portfolio_decision_cli_under_test", ROOT / "cli" / "portfolio_decision.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CliRun:
    """把一組決策鏈輸入寫成檔案，以同程序呼叫 CLI 的 finalize 與 validate-decision。"""

    def __init__(self, directory, bundle, momentum, debate, trade, policy, team):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        proposal = AllocationOrderEngine(bundle, policy).run(trade)
        scenario = ScenarioEngine(bundle, policy).run(proposal)
        guard = CompetitionGuardV2(bundle, policy).run(proposal, scenario)
        review = risk_review(bundle, proposal, scenario, guard, decision="approve" if guard["passed"] else "reject")
        history = RevisionHistoryBuilder(bundle, policy, trade).create(proposal, scenario, guard, review)
        self.files = {}
        for name, payload in (("bundle", bundle), ("momentum", momentum), ("debate", debate), ("intent", trade),
                              ("policy", policy), ("team_inputs", team), ("proposal", proposal),
                              ("scenario", scenario), ("guard", guard), ("review", review), ("history", history)):
            self.files[name] = self.root / ("%s.json" % name)
            self.files[name].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.output = self.root / "decision.json"
        self.cli = load_cli()

    def call(self, command, extra, repository=None):
        argv = ["--bundle", str(self.files["bundle"])]
        if repository is not None:
            argv += ["--decision-repository", str(repository)]
        argv.append(command)
        for name in ("momentum", "debate", "intent", "policy", "proposal", "scenario", "guard", "review", "history"):
            argv += ["--" + name, str(self.files[name])]
        argv += ["--team-inputs", str(self.files["team_inputs"])] + extra
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.cli.PortfolioDecisionApplication(ROOT).run(argv)
        return code, json.loads(buffer.getvalue())

    def finalize(self, repository=None):
        code, payload = self.call("finalize", ["--output", str(self.output)], repository)
        return code, payload.get("data")

    def validate(self, repository=None):
        return self.call("validate-decision", ["--input", str(self.output)], repository)


class GatedCliTests(unittest.TestCase):
    def build(self, directory):
        world = GatedWorld().build(directory, "neutral", "upper")
        self.assertEqual(world.policy["position_sizing"]["cash_ratio_range"]["anchor"]["status"], "prior_decision")
        return world, CliRun(Path(directory) / "cli", world.bundle, world.momentum, world.debate, world.trade,
                             world.policy, world.team)

    def test_finalize_and_validate_succeed_with_decision_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            world, run = self.build(directory)
            code, decision = run.finalize(world.repo)
            self.assertEqual(code, 0)
            self.assertEqual(decision["status"], "approved", decision["errors"])
            code, result = run.validate(world.repo)
            self.assertEqual((code, result), (0, {"valid": True, "errors": []}))

    def test_missing_or_empty_repository_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            world, run = self.build(directory)
            for repository in (Path(directory) / "missing-repo", None):
                # None：沒指定時改用預設 artifacts/decisions，該處沒有 fixture 的前次 run，同樣必須拒絕。
                # 先以正確決策庫產出 approved，再以缺漏的決策庫驗證它：必須失敗，不得跳過前次比例的重算。
                self.assertEqual(run.finalize(world.repo)[1]["status"], "approved")
                code, result = run.validate(repository)
                self.assertEqual(code, 2, repository)
                self.assertFalse(result["valid"])
                code, decision = run.finalize(repository)
                self.assertEqual(code, 0)
                self.assertEqual(decision["status"], "rejected", repository)
                self.assertTrue(any("cash_ratio_range.anchor" in error for error in decision["errors"]), decision)

    def test_default_repository_is_used_only_for_gated_policy(self):
        cli = load_cli()
        app = cli.PortfolioDecisionApplication(ROOT)
        with tempfile.TemporaryDirectory() as directory:
            world, run = self.build(directory)
            args = argparse.Namespace(decision_repository=None, policy=run.files["policy"])
            self.assertEqual(app.decision_repository_for(args), ROOT / "artifacts" / "decisions")
            bundle, _, _, _, settings, _, _, _ = chain_inputs()
            legacy = Path(directory) / "legacy-policy.json"
            legacy.write_text(json.dumps(settings), encoding="utf-8")
            self.assertIsNone(app.decision_repository_for(argparse.Namespace(decision_repository=None, policy=legacy)))
            explicit = Path(directory) / "repo-x"
            args = argparse.Namespace(decision_repository=explicit, policy=legacy)
            self.assertEqual(app.decision_repository_for(args), explicit)
            # 預設值為 None：沒指定時才依 policy 決定。
            self.assertIsNone(app.build_parser().parse_args(["validate-input"]).decision_repository)


class LegacyCliTests(unittest.TestCase):
    """沒有 cash_ratio_range 的舊 run：CLI 不傳決策庫，finalize 輸出與改動前逐位元相同。"""

    def test_chain_fixture_hash_unchanged(self):
        bundle, momentum, debate, intent, settings, team, _, _ = chain_inputs()
        with tempfile.TemporaryDirectory() as directory:
            run = CliRun(directory, bundle, momentum, debate, intent, settings, team)
            code, decision = run.finalize()
            self.assertEqual(code, 0)
            self.assertEqual(decision["content_sha256"], LEGACY_CHAIN_DECISION_SHA256)
            self.assertEqual(run.validate(), (0, {"valid": True, "errors": []}))

    def test_prior_context_run_without_gate_does_not_read_default_repository(self):
        """2.2 整合鏈、前次決策背景 available、前次 run 不在預設決策庫：舊行為只驗內部一致性，必須照舊通過。"""
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            DecisionRepository(repo).save("decision-prior", sealed_run(), day="2026-09-20")
            bundle = later_bundle()
            prior = build_prior_decision_context(bundle, read_decision_run(repo, "decision-prior"))
            self.assertEqual(prior["status"], "available")
            momentum, debate, trade, policy, team, _ = integrated_world(bundle, prior)
            self.assertNotIn("cash_ratio_range", policy["position_sizing"])
            run = CliRun(Path(directory) / "cli", bundle, momentum, debate, trade, policy, team)
            code, decision = run.finalize()
            self.assertEqual(code, 0)
            self.assertEqual(decision["content_sha256"], LEGACY_PRIOR_DECISION_SHA256)
            self.assertEqual(run.validate(), (0, {"valid": True, "errors": []}))


if __name__ == "__main__":
    unittest.main()
