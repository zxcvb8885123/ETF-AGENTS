"""完整評測沿當時 head 封存帳本，缺資料／時間洩漏在 Agent 呼叫前拒絕。"""

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.core import content_sha256
from etf_agent.decision import bind_drawdown, decision_policy_sha256
from etf_agent.decision.drawdown import history_reference
from etf_agent.evaluation import EvaluationError, build_suite, run_suite
from etf_agent.evaluation.ledger_context import validate_ledger_context

from test_daily_pipeline import (
    TRADE, FakeRunner, PipelineWorld, fixed_policy_template, team_outputs,
)


class EvaluationLedgerContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "app"
        world = PipelineWorld(Path(self.temp.name), approved_status=True)
        result = world.run(FakeRunner(team_outputs()))
        self.assertEqual(result.status, "completed", result.errors)
        self.reference = self.root / "artifacts/daily/2026-09-20/daily-fixture"
        shutil.copytree(world.run_dir, self.reference)
        self.write(self.reference / "pipeline.json", {"status": "completed", "decision_run_id": "decision-fixture"})
        for target, source in (("config/decision_policy.json", "template"), ("data/sector_classification.json", "sectors"),
                               ("var/etf_agent.db", "database")):
            destination = self.root / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(world.paths[source], destination)
        self.bundle = json.loads((self.reference / "decision_input.json").read_text(encoding="utf-8"))
        self.nav = [{"run_id": "ledger-1", "as_of": "2026-09-01T06:00:00+00:00", "nav": "650000"},
                    {"run_id": "ledger-2", "as_of": "2026-09-19T20:00:00+00:00", "nav": "660000"}]
        self.holdings = [{"run_id": row["run_id"], "as_of": row["as_of"], "symbols": ["2330.TW"]} for row in self.nav]
        self.template_path = self.root / "config/decision_policy.json"
        template = json.loads(self.template_path.read_text(encoding="utf-8"))
        template["position_sizing"]["exposure_rotation"] = fixed_policy_template()["position_sizing"]["exposure_rotation"]
        self.write(self.template_path, template)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def suite_with_history(self):
        path = self.reference / "policy.json"
        policy = bind_drawdown(json.loads(path.read_text(encoding="utf-8")), self.bundle, self.nav)
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.write(path, policy)
        with patch("etf_agent.virtual_account.VirtualAccountRepository") as repository:
            repository.return_value.nav_history.return_value = self.nav
            repository.return_value.holdings_history.return_value = self.holdings
            suite = build_suite(self.root, "with-ledger")
            repository.return_value.nav_history.assert_called_once_with("ledger-2")
            repository.return_value.holdings_history.assert_called_once_with("ledger-2")
            repository.return_value.latest.assert_not_called()
        return suite

    def test_full_eval_uses_frozen_histories_without_reopening_the_live_ledger(self):
        suite = self.suite_with_history()
        trade = copy.deepcopy(TRADE)
        trade["items"][1]["conviction"] = "medium"
        seen = []

        def make(run_dir, repository, resume=False):
            pipeline = DailyDecisionPipeline(self.root, run_dir, FakeRunner(team_outputs(trader_b0=[trade])), repository,
                                             log=lambda _: None, resume=resume)
            seen.append(pipeline)
            return pipeline

        with patch("etf_agent.virtual_account.VirtualAccountRepository", side_effect=AssertionError("不可重讀今日帳本")):
            manifest = run_suite(self.root, suite, make, repeats=1, log=lambda _: None)
        record = manifest["cases"][0]
        self.assertEqual(record["runs"][0]["status"], "completed", record["runs"][0]["errors"])
        self.assertEqual(record["ledger_context_sha256"], suite["cases"][0]["ledger_context"]["content_sha256"])
        run_dir = Path(record["runs"][0]["run_dir"])
        policy = json.loads((run_dir / "policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["drawdown_control"]["history_ref"]["head_run_id"], "ledger-2")
        self.assertEqual(policy["position_sizing"]["exposure_rotation"]["held_since_by_symbol"],
                         {"2330.TW": self.holdings[0]["as_of"]})
        self.assertEqual(json.loads((run_dir / "team_inputs.json").read_text(encoding="utf-8"))["schema_version"], "2.7")
        self.assertEqual(len(seen), 1)

    def test_missing_history_rejects_the_whole_suite_before_creating_any_pipeline(self):
        suite = self.suite_with_history()
        legacy = copy.deepcopy(suite["cases"][0])
        legacy["case_id"] = "missing-ledger"
        del legacy["ledger_context"]
        suite["cases"].append(legacy)
        with patch("builtins.print") as log:
            with self.assertRaisesRegex(EvaluationError, "缺少封存帳本快照"):
                run_suite(self.root, suite, log, repeats=1, log=lambda _: None)
            log.assert_not_called()

    def test_missing_history_does_not_block_an_explicit_analysts_only_eval(self):
        suite = build_suite(self.root, "legacy")

        def make(run_dir, repository, resume=False):
            return DailyDecisionPipeline(self.root, run_dir, FakeRunner(team_outputs()), repository, log=lambda _: None)

        manifest = run_suite(self.root, suite, make, repeats=1, stop_after="analysts", log=lambda _: None)
        self.assertEqual(manifest["cases"][0]["runs"][0]["status"], "completed")

    def test_hash_time_snapshot_and_chain_mismatches_are_rejected(self):
        context = self.suite_with_history()["cases"][0]["ledger_context"]
        broken = copy.deepcopy(context)
        broken["nav_history"][0]["nav"] = "1"
        with self.assertRaisesRegex(EvaluationError, "雜湊"):
            validate_ledger_context(broken, self.bundle)
        for mutate in (
            lambda c: c.update(snapshot_id="other"),
            lambda c: c["nav_history"][0].update(as_of="2099-01-01T00:00:00+00:00"),
            lambda c: c["holdings_history"][0].update(run_id="different-head"),
            lambda c: c["history_ref"].update(sha256="0" * 64),
            lambda c: c["holdings_history"][-1].update(symbols=[]),
        ):
            with self.subTest(mutate=mutate):
                broken = copy.deepcopy(context)
                mutate(broken)
                broken["content_sha256"] = content_sha256(broken)
                with self.assertRaises(EvaluationError):
                    validate_ledger_context(broken, self.bundle)

    def test_unreadable_anchored_ledger_stops_suite_creation(self):
        self.suite_with_history()
        with patch("etf_agent.virtual_account.VirtualAccountRepository") as repository:
            repository.return_value.nav_history.side_effect = ValueError("帳本損壞")
            with self.assertRaisesRegex(EvaluationError, "帳本損壞"):
                build_suite(self.root, "broken")

    def test_rebase_valuation_times_do_not_reorder_the_verified_parent_chain(self):
        self.nav[0]["as_of"] = "2026-09-19T20:01:00+00:00"
        self.holdings[0]["as_of"] = self.nav[0]["as_of"]
        context = self.suite_with_history()["cases"][0]["ledger_context"]
        self.assertEqual(context["history_ref"], history_reference(self.nav))
        self.assertEqual([row["run_id"] for row in context["nav_history"]], ["ledger-1", "ledger-2"])

    def test_resume_rejects_a_changed_ledger_context_before_agent_calls(self):
        suite = self.suite_with_history()
        outputs = team_outputs(trader_b0=[{"items": []}, {"items": []}])

        def make(run_dir, repository, resume=False):
            return DailyDecisionPipeline(self.root, run_dir, FakeRunner(outputs), repository, log=lambda _: None, resume=resume)

        manifest = run_suite(self.root, suite, make, repeats=1, log=lambda _: None)
        changed = copy.deepcopy(suite)
        context = changed["cases"][0]["ledger_context"]
        context["holdings_history"][0]["symbols"] = []
        context["content_sha256"] = content_sha256(context)
        with patch("builtins.print") as factory:
            with self.assertRaisesRegex(EvaluationError, "帳本快照與原評測不同"):
                run_suite(self.root, changed, factory, repeats=1, resume_eval_id=manifest["eval_id"], log=lambda _: None)
            factory.assert_not_called()
