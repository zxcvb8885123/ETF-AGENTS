import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from etf_agent.competition import (
    DPlanContextBuilder, DPlanError, DPlanExportService, DPlanExporter, DPlanValidator, load_decision_run,
    verify_account_binding,
)
from etf_agent.decision import (
    AllocationOrderEngine, CompetitionGuardV2, DecisionFinalizer,
    RevisionHistoryBuilder, ScenarioEngine,
)
from etf_agent.virtual_account import VirtualAccountRepository, VirtualAccountService
from test_portfolio_risk_decision import chain_inputs, policy, risk_review, valid_inputs
from test_virtual_account import prepare_and_decide


def valid_context():
    return {
        "sources": [{
            "source_id": "S1", "authority": "vendor", "url": "https://data.example.test/price",
            "content_as_of": "2026-09-19T18:00:00+08:00",
        }],
        "observations": [{
            "obs_id": "O1", "source_ref": ["S1"], "statement": "測試用事實資料，不代表真實市場行情。",
            "values": {"fixture_value": 1.0},
        }],
        "market_view": {
            "basis_refs": ["O1"], "logic": "此為測試內容，不構成任何真實市場方向判斷。",
            "regime": "neutral", "stance": "neutral",
            "posture": {"net_exposure_intent": "hold", "target_cash_pct_range": [0.0, 0.25]},
            "counter_evidence": None,
        },
        "inferences": [{
            "inf_id": "I1", "premise_refs": ["O1"],
            "logic": "此為測試用推論內容，僅用於驗證引用鏈和欄位契約。",
            "counter_evidence": None,
        }],
        "inference_refs_by_ticker": {"2330": ["I1"], "2317": ["I1"]},
        "agent_metadata": {
            "model_provider": "openai", "model_version": "codex-test-1",
            "run_started_at": "2026-09-19T20:01:00+08:00",
            "run_completed_at": "2026-09-19T20:02:00+08:00", "code_version": "test-fixture",
        },
    }


def approved_artifacts():
    bundle, momentum, debate, intent, settings, team, _, _ = chain_inputs()
    proposal = AllocationOrderEngine(bundle, settings).run(intent)
    scenario = ScenarioEngine(bundle, settings).run(proposal)
    guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
    review = risk_review(bundle, proposal, scenario, guard)
    history = RevisionHistoryBuilder(bundle, settings, intent).create(proposal, scenario, guard, review)
    result = DecisionFinalizer(bundle, settings, momentum, debate, intent, team).run(proposal, scenario, guard, review, history)
    return {
        "decision_input": bundle, "momentum": momentum, "debate": debate, "intent": intent,
        "policy": settings, "proposal": proposal, "scenario": scenario, "guard": guard,
        "risk_review": review, "revision_history": history, "decision": result, "team_inputs": team,
    }


def valid_plan():
    context = valid_context()
    return {
        "schema_version": "4.3", "doc_type": "D-Plan", "team_id": "TEAM_042", "trade_date": "2026-09-22",
        "sources": context["sources"], "observations": context["observations"], "market_view": context["market_view"],
        "inferences": context["inferences"], "decisions": [], "no_trade_decisions": [], "orders": [],
        "agent_metadata": context["agent_metadata"],
    }


def approved_profile():
    return {
        "schema_version": "1.0", "team_id": "TEAM_11067", "strategy_status": "approved",
        "strategy": {
            "etf_name": "主動測試ETF", "investment_theme": "測試用投資主題",
            "investment_philosophy": "測試用投資理念。" * 12,
            "approved_at": "2026-09-19T20:00:00+08:00",
        },
        "agent": {"model_provider": "openai", "model_version": "codex-test-1"},
    }


def daily_run():
    return {
        "run_id": "daily-test", "started_at": "2026-09-19T20:01:00+08:00",
        "finished_at": "2026-09-19T20:02:00+08:00",
    }


class DPlanContractTests(unittest.TestCase):
    def test_context_builder_uses_sealed_artifacts_and_rejects_unapproved_profile(self):
        artifacts = approved_artifacts()
        with self.assertRaisesRegex(DPlanError, "策略尚未確認"):
            DPlanContextBuilder().build(artifacts, {"schema_version": "1.0", "team_id": "TEAM_11067", "strategy_status": "pending_user_confirmation"}, daily_run(), "git:test")
        for row in artifacts["decision_input"]["snapshot"]["source_evidence"]:
            row["url"] = "https://data.example.test/%s" % row["evidence_id"]
            row["authority"] = "vendor"
            row["source"] = "TEST_VENDOR"
            row["content_as_of"] = "2026-09-19T18:00:00+08:00"
            row["fetched_at"] = "2026-09-19T18:01:00+08:00"
        for row in artifacts["intent"]["items"]:
            if len(row.get("rationale", "")) < 20:
                row["rationale"] = "此為測試用既有交易理由，僅驗證 D-Plan context 自動引用鏈。"
        snapshot = artifacts["decision_input"]["snapshot"]
        existing = {row["symbol"][:4] for row in snapshot["latest_prices"]}
        for number in range(1000, 9999):
            ticker = "%04d" % number
            if ticker in existing:
                continue
            snapshot["latest_prices"].append({"symbol": ticker + ".TW"})
            existing.add(ticker)
            if len(existing) == 150:
                break
        snapshot["universe_size"] = 150
        context = DPlanContextBuilder().build(artifacts, approved_profile(), daily_run(), "git:test")
        self.assertNotIn("eligible_universe_source_refs", context)
        self.assertEqual(context["agent_metadata"]["model_provider"], "openai")
        self.assertEqual(context["market_view"]["basis_refs"], ["O1"])
        self.assertTrue(context["inference_refs_by_ticker"])


class DPlanExportServiceTests(unittest.TestCase):
    def test_export_uses_daily_target_session_and_fixed_competition_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile.json"
            profile.write_text(json.dumps(approved_profile()), encoding="utf-8")
            daily = {
                "run_id": "daily-20261020",
                "started_at": "2026-10-19T18:00:00+08:00",
                "finished_at": "2026-10-19T18:30:00+08:00",
                "target_session": {"start": "2026-10-20T09:00:00+08:00", "end": "2026-10-20T13:30:00+08:00"},
            }
            context = {"fixture": "context"}
            plan = {"fixture": "dplan"}
            service = DPlanExportService(root)
            with patch("etf_agent.competition.service.load_decision_run", return_value={"fixture": "artifacts"}), \
                    patch("etf_agent.competition.service.DPlanContextBuilder.build", return_value=context), \
                    patch("etf_agent.competition.service.verify_account_binding", return_value={"run_id": "prepare-1"}), \
                    patch("etf_agent.competition.service.DPlanExporter.build", return_value=plan), \
                    patch.object(service, "code_version", return_value="git:test"):
                result = service.export(
                    decision_repository=root / "decisions",
                    decision_run_id="decision-1",
                    daily_run=daily,
                    profile_path=profile,
                    account_id="ai-cup-2026",
                    account_run_id="prepare-1",
                )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["trade_date"], "2026-10-20")
            output = root / "artifacts/competition_d_plan/D-Plan_TEAM_11067_2026-10-20.json"
            context_output = root / "artifacts/competition_d_plan/contexts/context_decision-1.json"
            self.assertEqual(Path(result["output"]), output)
            self.assertEqual(Path(result["context_output"]), context_output)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), plan)
            self.assertEqual(json.loads(context_output.read_text(encoding="utf-8")), context)

    def test_trade_date_requires_a_timed_target_session(self):
        with self.assertRaisesRegex(DPlanError, "target_session"):
            DPlanExportService.trade_date({})
        with self.assertRaisesRegex(DPlanError, "缺少時區"):
            DPlanExportService.trade_date({"target_session": {"start": "2026-10-20T09:00:00"}})

    def test_export_latest_reads_date_partitioned_daily(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "artifacts" / "daily" / "2026-10-20" / "daily-20261020"
            run_dir.mkdir(parents=True)
            daily = {
                "run_id": "daily-20261020",
                "status": "completed",
                "decision_status": "approved",
                "decision_run_id": "decision-20261020",
                "finished_at": "2026-10-20T18:30:00+08:00",
                "target_session": {"start": "2026-10-21T09:00:00+08:00"},
            }
            (run_dir / "pipeline.json").write_text(json.dumps(daily), encoding="utf-8")
            service = DPlanExportService(root)
            expected = {"status": "ready"}
            with patch.object(service, "current_account_run_id", return_value="prepare-1"), \
                    patch.object(service, "export", return_value=expected) as export:
                self.assertEqual(service.export_latest(), expected)
            export.assert_called_once_with(
                decision_repository=root / "artifacts" / "decisions",
                decision_run_id="decision-20261020",
                daily_run=daily,
                profile_path=root / "config" / "competition_profile.json",
                account_id="ai-cup-2026",
                account_run_id="prepare-1",
            )


class DPlanAdditionalContractTests(unittest.TestCase):
    def test_detects_orphan_references_unknown_fields_and_noncontiguous_ids(self):
        plan = valid_plan()
        self.assertEqual(DPlanValidator().validate(plan), [])
        changed = copy.deepcopy(plan)
        changed["observations"][0]["source_ref"] = ["S9"]
        changed["_unofficial"] = "not allowed"
        errors = DPlanValidator().validate(changed)
        self.assertTrue(any("引用不存在" in error for error in errors))
        self.assertTrue(any("未定義欄位" in error for error in errors))

    def test_exporter_requires_explicit_reasoning_context_and_rejects_mismatch(self):
        artifacts = approved_artifacts()
        with self.assertRaisesRegex(DPlanError, "context 缺少"):
            DPlanExporter().build("TEAM_042", "2026-09-22", {}, artifacts)
        with self.assertRaisesRegex(DPlanError, "eligible_tickers"):
            DPlanExporter().build("TEAM_042", "2026-09-22", valid_context(), artifacts)

    def test_validator_rejects_trade_orphan_and_bad_lot(self):
        plan = valid_plan()
        plan["decisions"] = [{"decision_id": "D1", "ticker": "2330", "action": "BUY", "target_weight": 0.1, "inference_refs": ["I1"]}]
        plan["orders"] = [{"ticker": "2330", "side": "BUY", "shares": 500, "decision_ref": "D999"}]
        errors = DPlanValidator().validate(plan)
        self.assertTrue(any("引用不存在" in error for error in errors))
        self.assertTrue(any("1,000 股整數倍" in error for error in errors))

    def test_validator_accepts_v43_array_boundaries(self):
        plan = valid_plan()
        plan["sources"] = [
            {
                "source_id": "S%d" % index,
                "authority": "vendor",
                "url": "https://data.example.test/%d" % index,
                "content_as_of": "2026-09-19T18:00:00+08:00",
            }
            for index in range(1, 11)
        ]
        plan["observations"][0]["source_ref"] = ["S%d" % index for index in range(1, 11)]
        plan["inferences"] = [
            {
                "inf_id": "I%d" % index,
                "premise_refs": ["O1"],
                "logic": "此為測試用推論內容，僅用於驗證 v4.3 引用數量上限。",
                "counter_evidence": None,
            }
            for index in range(1, 9)
        ]
        refs = ["I%d" % index for index in range(1, 9)]
        plan["decisions"] = [
            {
                "decision_id": "D%d" % index,
                "ticker": "%04d" % (1000 + index),
                "action": "BUY",
                "target_weight": 0.01,
                "inference_refs": refs,
            }
            for index in range(1, 61)
        ]
        plan["orders"] = [
            {
                "ticker": "%04d" % (1000 + index),
                "side": "BUY",
                "shares": 1000,
                "decision_ref": "D%d" % index,
            }
            for index in range(1, 61)
        ]
        self.assertEqual(DPlanValidator().validate(plan), [])

    def test_validator_rejects_legacy_v40_and_v43_array_overflow(self):
        plan = valid_plan()
        plan["schema_version"] = "4.0"
        errors = DPlanValidator().validate(plan)
        self.assertTrue(any("4.3/D-Plan" in error for error in errors))

        plan = valid_plan()
        plan["sources"] += [
            {
                "source_id": "S%d" % index,
                "authority": "vendor",
                "url": "https://data.example.test/%d" % index,
                "content_as_of": "2026-09-19T18:00:00+08:00",
            }
            for index in range(2, 12)
        ]
        plan["observations"][0]["source_ref"] = ["S%d" % index for index in range(1, 12)]
        errors = DPlanValidator().validate(plan)
        self.assertTrue(any("1–10 個來源引用" in error for error in errors))

        plan = valid_plan()
        plan["decisions"] = [{}] * 61
        plan["orders"] = [{}] * 61
        plan["decisions"][0]["inference_refs"] = ["I%d" % index for index in range(1, 10)]
        errors = DPlanValidator().validate(plan)
        self.assertTrue(any("decisions 必須是 0–60 筆陣列" in error for error in errors))
        self.assertTrue(any("orders 必須是 0–60 筆陣列" in error for error in errors))

        plan = valid_plan()
        plan["decisions"] = [{
            "decision_id": "D1", "ticker": "2330", "action": "BUY",
            "target_weight": 0.1,
            "inference_refs": ["I%d" % index for index in range(1, 10)],
        }]
        errors = DPlanValidator().validate(plan)
        self.assertTrue(any("1–8 個推論引用" in error for error in errors))

class DPlanAccountBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        rules = self.root / "rules.json"
        rules.write_text(json.dumps({"initial_capital_twd": 1_000_000_000, "version": "fixture-v1"}), encoding="utf-8")
        self.accounts = self.root / "accounts"
        self.repository = VirtualAccountRepository(self.accounts, "cup-test")
        self.service = VirtualAccountService(self.repository)
        self.service.initialize(rules, "cup-test", "2026-09-18T17:00:00+08:00")
        self.genesis_run = self.repository.latest()["run_id"]
        _, decisions = prepare_and_decide(self.service, self.root)
        self.artifacts = load_decision_run(str(decisions), "decision-1")

    def tearDown(self):
        self.temp.cleanup()

    def bind(self, artifacts=None, run_id="prepare-with-orders"):
        return verify_account_binding(artifacts or self.artifacts, str(self.accounts), "cup-test", run_id)

    def test_accepts_latest_prepare_day_run_used_by_decision(self):
        binding = self.bind()
        self.assertEqual(binding["run_id"], "prepare-with-orders")
        self.assertTrue(binding["account_snapshot_sha256"])

    def test_rejects_run_that_is_not_latest(self):
        with self.assertRaisesRegex(DPlanError, "latest"):
            self.bind(run_id=self.genesis_run)

    def test_rejects_changed_account_snapshot(self):
        changed = copy.deepcopy(self.artifacts)
        changed["decision_input"]["account_snapshot"]["cash"] = "1"
        with self.assertRaisesRegex(DPlanError, "AccountSnapshot"):
            self.bind(changed)

    def test_rejects_snapshot_or_cutoff_mismatch(self):
        changed = copy.deepcopy(self.artifacts)
        changed["decision_input"]["decision_cutoff"] = "2026-09-21T00:55:00+00:00"
        with self.assertRaisesRegex(DPlanError, "Snapshot／cutoff"):
            self.bind(changed)
        changed = copy.deepcopy(self.artifacts)
        changed["decision_input"]["snapshot"]["snapshot_id"] = "other-snapshot"
        with self.assertRaisesRegex(DPlanError, "Snapshot／cutoff"):
            self.bind(changed)

    def test_rejects_missing_account_run(self):
        with self.assertRaisesRegex(DPlanError, "VirtualAccount run 驗證失敗"):
            self.bind(run_id="prepare-missing")


if __name__ == "__main__":
    unittest.main()
