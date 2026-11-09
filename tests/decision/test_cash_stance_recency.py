"""cash_stance_recency v1：風險 Agent 現金姿態的資料新舊時間表與 recency_reconciliation 列舉。"""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from etf_agent.automation.agent_schemas import (
    CASH_GRADE_STANCE_SCHEMA, CASH_STANCE_SCHEMA, cash_stance_recency_schema,
)
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.automation.agent_runner import DailyPipelineError
from etf_agent.decision import MARKET_REGIME_EVIDENCE_ID, DecisionToolError, MomentumEngine
from etf_agent.decision.contracts import DecisionContext
from etf_agent.decision.recency import (
    BUCKET_ACCUMULATED, BUCKET_AFTER_CLOSE, BUCKET_LATEST_SESSION, RECENCY_RECONCILIATIONS, cash_stance_recency_gate,
    evidence_recency, validate_cash_stance_recency_gate,
)
from etf_agent.decision.sizing import POSITION_SIZING_FIELDS, validate_cash_stance
from etf_agent.perception.overnight_market import attach_overnight_market, evaluate
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_daily_pipeline import FakeRunner
from test_analysts import bundle_with_event
from test_overnight_market import LATEST_TRADE_DATE, approvals, captures, pack, with_trade_date
from test_typed_evidence import typed_bundle, typed_reports

ROOT = Path(__file__).resolve().parents[2]
GATE = {"version": "v1"}
ARCHIVED_INPUT = ROOT / "artifacts/daily/2026-11-09/daily-20261109-070407/decision_input.json"


def overnight_bundle(typed=True, approved=True):
    """決策輸入附隔夜市場（週五為台股最近交易日），可選 typed_evidence_v1。"""
    base = with_trade_date(bundle_with_event())
    cutoff = datetime.fromisoformat(base["decision_cutoff"])
    rows = captures(available=(cutoff - timedelta(minutes=5)).isoformat())
    value = pack(snapshot_id=base["snapshot_id"], cutoff=base["decision_cutoff"], rows=rows,
                 source_approvals=approvals() if approved else approvals("unverified"))
    attached = attach_overnight_market(base, {"bundle": value, "result": evaluate(value)})
    return typed_bundle(attached) if typed else attached


def stance(evidence, reconciliation="newest_confirms", **extra):
    value = {"level": "neutral", "breadth": "balanced", "rationale": "依最新一組與走勢取捨。", "evidence_ids": evidence,
             "recency_reconciliation": reconciliation}
    value.update(extra)
    return value


def errors_of(bundle, value, gate=GATE):
    errors = []
    validate_cash_stance(DecisionContext(bundle), value, "cash_stance", errors, recency_gate=gate)
    return errors


class RecencyGateShapeTests(unittest.TestCase):
    def test_gate_is_a_registered_human_only_field(self):
        self.assertIn("cash_stance_recency", POSITION_SIZING_FIELDS)
        self.assertIn("cash_stance_recency", FORBIDDEN_PREFIXES)
        self.assertEqual(cash_stance_recency_gate({"cash_stance_recency": GATE}), GATE)
        self.assertIsNone(cash_stance_recency_gate({}))
        self.assertIsNone(cash_stance_recency_gate({"cash_stance_recency": "v1"}))

    def test_shape_is_closed(self):
        for bad in ("v1", {"version": "v2"}, {"version": "v1", "window_hours": 48}, {}, None):
            errors = []
            validate_cash_stance_recency_gate({"cash_stance_recency": bad}, errors)
            self.assertTrue(errors, bad)
        errors = []
        validate_cash_stance_recency_gate({"cash_stance_recency": GATE}, errors)
        self.assertEqual(errors, [])


class EvidenceRecencyTests(unittest.TestCase):
    def setUp(self):
        self.bundle = overnight_bundle()
        self.context = DecisionContext(self.bundle)
        self.recency = evidence_recency(self.bundle, self.context.market_evidence_ids)

    def test_buckets_use_content_time_not_capture_time(self):
        buckets = self.recency["buckets"]
        overnight = [row for row in buckets[BUCKET_AFTER_CLOSE] if row["kind"] == "overnight"]
        self.assertEqual(len(overnight), 5)
        for row in overnight:
            self.assertTrue(row["evidence_id"].startswith("overnight:"))
            self.assertGreater(Decimal_of(row["hours_before_cutoff"]), 0)
        self.assertEqual([row["evidence_id"] for row in buckets[BUCKET_ACCUMULATED]], [MARKET_REGIME_EVIDENCE_ID])
        self.assertEqual(self.recency["data_gaps"], [])

    def test_output_has_ids_times_and_enums_only(self):
        text = json.dumps(self.recency, ensure_ascii=False)
        self.assertNotIn("title", text)
        self.assertNotIn("summary", text)
        for rows in self.recency["buckets"].values():
            for row in rows:
                self.assertLessEqual(set(row), {"evidence_id", "kind", "content_time", "content_date", "hours_before_cutoff", "basis", "bucket"})

    def test_sorted_newest_first_and_stable(self):
        rows = self.recency["buckets"][BUCKET_AFTER_CLOSE]
        hours = [Decimal_of(row["hours_before_cutoff"]) for row in rows]
        self.assertEqual(hours, sorted(hours))
        self.assertEqual(self.recency, evidence_recency(self.bundle, self.context.market_evidence_ids))

    def test_content_time_after_cutoff_is_rejected_not_dropped(self):
        tampered = copy.deepcopy(self.bundle)
        tampered["overnight_market_input"]["result"]["components"][0]["session_end"] = "2099-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(DecisionToolError, "晚於 decision_cutoff"):
            evidence_recency(tampered, self.context.market_evidence_ids)

    def test_naive_content_time_is_rejected(self):
        tampered = copy.deepcopy(self.bundle)
        tampered["overnight_market_input"]["result"]["components"][0]["session_end"] = "2026-09-18T20:00:00"
        with self.assertRaises(DecisionToolError):
            evidence_recency(tampered, self.context.market_evidence_ids)

    def test_missing_trade_date_is_rejected(self):
        tampered = copy.deepcopy(self.bundle)
        del tampered["snapshot"]["latest_trade_date"]
        with self.assertRaises(DecisionToolError):
            evidence_recency(tampered, self.context.market_evidence_ids)

    def test_evidence_without_content_time_is_a_data_gap(self):
        recency = evidence_recency(self.bundle, self.context.market_evidence_ids | {"market-news:" + "0" * 24})
        self.assertEqual(len(recency["data_gaps"]), 1)
        self.assertIn("market-news:", recency["data_gaps"][0])

    def test_content_time_equal_to_boundary_is_latest_session(self):
        from etf_agent.perception.overnight_market.tools import window_start

        tampered = copy.deepcopy(self.bundle)
        tampered["overnight_market_input"]["result"]["components"][0]["session_end"] = window_start(LATEST_TRADE_DATE).isoformat()
        recency = evidence_recency(tampered, self.context.market_evidence_ids)
        first = tampered["overnight_market_input"]["result"]["components"][0]["evidence_id"]
        self.assertIn(first, [row["evidence_id"] for row in recency["buckets"][BUCKET_LATEST_SESSION]])

    @unittest.skipUnless(ARCHIVED_INPUT.exists(), "沒有 11-09 07:00 封存")
    def test_archived_run_groups_weekend_news_after_the_latest_close(self):
        bundle = json.loads(ARCHIVED_INPUT.read_text(encoding="utf-8"))
        recency = evidence_recency(bundle, DecisionContext(bundle).market_evidence_ids)
        after = recency["buckets"][BUCKET_AFTER_CLOSE]
        self.assertEqual([row["kind"] for row in after].count("news"), 9)
        self.assertEqual(after[0]["hours_before_cutoff"], "0.7")
        self.assertEqual([row["hours_before_cutoff"] for row in after if "^N225" in row["evidence_id"]], ["64.7"])
        self.assertEqual(recency["data_gaps"], [])
        self.assertEqual([row["evidence_id"] for row in recency["buckets"][BUCKET_ACCUMULATED]], [MARKET_REGIME_EVIDENCE_ID])


def Decimal_of(value):
    from decimal import Decimal
    return Decimal(value)


class ReconciliationValidationTests(unittest.TestCase):
    def setUp(self):
        self.bundle = overnight_bundle()
        context = DecisionContext(self.bundle)
        self.newest = [row["evidence_id"] for row in evidence_recency(self.bundle, context.market_evidence_ids)["buckets"][BUCKET_AFTER_CLOSE]]
        self.assertTrue(self.newest)

    def test_valid_stance_passes_and_field_is_required_under_the_gate(self):
        self.assertEqual(errors_of(self.bundle, stance([self.newest[0]])), [])
        missing = stance([self.newest[0]])
        del missing["recency_reconciliation"]
        self.assertTrue(any("recency_reconciliation" in error for error in errors_of(self.bundle, missing)))

    def test_values_outside_the_enum_are_rejected(self):
        for bad in ("confirms", "", None, 1, True, ["newest_confirms"]):
            self.assertTrue(errors_of(self.bundle, stance([self.newest[0]], bad)), bad)
        self.assertEqual(set(RECENCY_RECONCILIATIONS), {"newest_confirms", "newest_contradicts", "newest_insufficient"})

    def test_insufficient_is_rejected_when_the_newest_group_has_evidence(self):
        errors = errors_of(self.bundle, stance([self.newest[0]], "newest_insufficient"))
        self.assertTrue(any("不能填 newest_insufficient" in error for error in errors), errors)

    def test_newest_group_must_be_cited(self):
        errors = errors_of(self.bundle, stance([MARKET_REGIME_EVIDENCE_ID], "newest_contradicts"))
        self.assertTrue(any("最新一組" in error and "至少要引用" in error for error in errors), errors)
        self.assertEqual(errors_of(self.bundle, stance([MARKET_REGIME_EVIDENCE_ID, self.newest[0]], "newest_contradicts")), [])

    def test_empty_newest_group_only_allows_insufficient(self):
        bundle = overnight_bundle(approved=False)  # 來源未核准：隔夜不可用，沒有任何新資料（如 21:00 備援 run）
        context = DecisionContext(bundle)
        recency = evidence_recency(bundle, context.market_evidence_ids)
        self.assertEqual(recency["buckets"][BUCKET_AFTER_CLOSE], [])
        ok = errors_of(bundle, stance([MARKET_REGIME_EVIDENCE_ID], "newest_insufficient"))
        self.assertFalse([e for e in ok if "recency_reconciliation" in e], ok)
        bad = errors_of(bundle, stance([MARKET_REGIME_EVIDENCE_ID], "newest_confirms"))
        self.assertTrue(any("沒有證據" in error for error in bad), bad)

    def test_alignment_with_stance_level_is_not_required(self):
        for level in ("aggressive", "neutral", "defensive"):
            self.assertEqual(errors_of(self.bundle, stance([self.newest[0]], "newest_contradicts", level=level)), [])

    def test_gate_requires_typed_evidence(self):
        errors = errors_of(overnight_bundle(typed=False), stance([self.newest[0]]))
        self.assertTrue(any("typed_evidence_v1" in error for error in errors), errors)

    def test_without_gate_the_field_is_rejected_and_validation_is_unchanged(self):
        errors = errors_of(self.bundle, stance([self.newest[0]]), gate=None)
        self.assertTrue(any("未允許欄位" in error and "recency_reconciliation" in error for error in errors), errors)
        plain = {k: v for k, v in stance([self.newest[0]]).items() if k != "recency_reconciliation"}
        self.assertEqual(errors_of(self.bundle, plain, gate=None), [])

    def test_retry_feedback_lists_citable_ids_only(self):
        errors = errors_of(self.bundle, stance([MARKET_REGIME_EVIDENCE_ID], "newest_confirms"))
        self.assertTrue(all(identifier in "".join(errors) for identifier in self.newest[:1]))


class SchemaAndBriefTests(unittest.TestCase):
    def test_schema_adds_only_the_enum(self):
        for graded, base in ((False, CASH_STANCE_SCHEMA), (True, CASH_GRADE_STANCE_SCHEMA)):
            schema = cash_stance_recency_schema(graded)
            self.assertEqual(schema["properties"]["recency_reconciliation"], {"enum": list(RECENCY_RECONCILIATIONS)})
            self.assertEqual(schema["required"], base["required"] + ["recency_reconciliation"])
            self.assertEqual({k: v for k, v in schema["properties"].items() if k != "recency_reconciliation"}, base["properties"])
            self.assertNotIn("recency_reconciliation", base["properties"])  # 舊 Schema 物件不變

    def run_stance(self, bundle, outputs, gate):
        momentum = MomentumEngine(bundle).run()
        reports = typed_reports(bundle)
        runner = FakeRunner({"cash_stance": outputs})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            kwargs = {} if gate is None else {"cash_stance_recency": gate}
            try:
                result = pipeline.run_cash_stance(bundle, momentum, reports, **kwargs)
            except DailyPipelineError as error:
                result = error
            brief_text = (Path(directory) / "brief_cash_stance.json").read_text(encoding="utf-8")
        return result, json.loads(brief_text), runner

    def test_brief_puts_the_time_table_first_and_prompt_mentions_it(self):
        bundle = overnight_bundle()
        newest = DecisionContext(bundle)
        ids = [row["evidence_id"] for row in evidence_recency(bundle, newest.market_evidence_ids)["buckets"][BUCKET_AFTER_CLOSE]]
        good = stance([ids[0]])
        result, brief, runner = self.run_stance(bundle, [good], GATE)
        self.assertEqual(result, good)
        self.assertEqual(next(iter(brief)), "evidence_recency")
        self.assertEqual(brief["evidence_recency"], evidence_recency(bundle, newest.market_evidence_ids))
        self.assertIn("evidence_recency", runner.prompts[0][1])
        self.assertIn("recency_reconciliation", runner.prompts[0][1])

    def test_without_gate_brief_has_no_time_table(self):
        bundle = overnight_bundle()
        plain = {k: v for k, v in stance(["price-2330", MARKET_REGIME_EVIDENCE_ID]).items() if k != "recency_reconciliation"}
        result, brief, runner = self.run_stance(bundle, [plain], None)
        self.assertEqual(result, plain)
        self.assertNotIn("evidence_recency", brief)
        self.assertNotIn("evidence_recency", runner.prompts[0][1])

    def test_invalid_output_is_corrected_through_the_retry_loop(self):
        bundle = overnight_bundle()
        ids = [row["evidence_id"] for row in evidence_recency(bundle, DecisionContext(bundle).market_evidence_ids)["buckets"][BUCKET_AFTER_CLOSE]]
        bad = stance([MARKET_REGIME_EVIDENCE_ID], "newest_confirms")
        good = stance([MARKET_REGIME_EVIDENCE_ID, ids[0]], "newest_confirms")
        result, _, runner = self.run_stance(bundle, [bad, good], GATE)
        self.assertEqual(result, good)
        self.assertEqual(len(runner.prompts), 2)
        self.assertIn("最新一組", runner.prompts[1][1])

    def test_gate_on_untyped_bundle_stops_before_asking_the_agent(self):
        bundle = overnight_bundle(typed=False)
        momentum = MomentumEngine(bundle).run()
        runner = FakeRunner({"cash_stance": []})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            with self.assertRaisesRegex(DailyPipelineError, "typed_evidence_v1"):
                pipeline.run_cash_stance(bundle, momentum, typed_reports(typed_bundle()), cash_stance_recency=GATE)
        self.assertEqual(runner.prompts, [])


class FinalizerWiringTests(unittest.TestCase):
    """真實 DecisionFinalizer：閘門要傳到現金姿態重驗。完整通過的路徑見 validate_cash_stance 與 apply_trade_decision 的測試。"""

    def world(self):
        from test_team_chain import team_world

        return team_world()

    def finalize(self, bundle, momentum, debate, trade, policy, team):
        from test_team_chain import finalize

        return finalize(bundle, momentum, debate, trade, policy, team)[1]

    def test_policy_gate_reaches_the_finalizer(self):
        from etf_agent.decision import build_team_inputs, decision_policy_sha256
        from test_trader import STANCE as TEAM_STANCE

        bundle, momentum, debate, trade, policy, _, reports, research = self.world()
        gated = copy.deepcopy(policy)
        gated["position_sizing"]["cash_stance_recency"] = dict(GATE)
        gated["content_sha256"] = decision_policy_sha256(gated)
        team = build_team_inputs(reports, research, dict(TEAM_STANCE, recency_reconciliation="newest_confirms"))
        result = self.finalize(bundle, momentum, debate, trade, gated, team)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("typed_evidence_v1" in error for error in result["errors"]), result["errors"])

    def test_field_without_policy_gate_is_rejected_by_the_finalizer(self):
        from etf_agent.decision import build_team_inputs
        from test_trader import STANCE as TEAM_STANCE

        bundle, momentum, debate, trade, policy, _, reports, research = self.world()
        team = build_team_inputs(reports, research, dict(TEAM_STANCE, recency_reconciliation="newest_confirms"))
        result = self.finalize(bundle, momentum, debate, trade, policy, team)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("recency_reconciliation" in error for error in result["errors"]), result["errors"])

    def test_without_gate_or_field_the_finalizer_still_approves(self):
        bundle, momentum, debate, trade, policy, team, _, _ = self.world()
        result = self.finalize(bundle, momentum, debate, trade, policy, team)
        self.assertEqual(result["status"], "approved", result["errors"])


class TradeBindingAndReplayTests(unittest.TestCase):
    def policy_with_gate(self):
        from test_portfolio_risk_decision import policy as base_policy
        from test_position_sizing import SIZING

        base = base_policy()
        base["position_sizing"] = {**copy.deepcopy(SIZING), "cash_stance_recency": dict(GATE)}
        return base

    def test_apply_trade_decision_enforces_the_enum_and_the_citation(self):
        from etf_agent.decision import apply_trade_decision

        bundle = overnight_bundle()
        newest = [row["evidence_id"] for row in evidence_recency(bundle, DecisionContext(bundle).market_evidence_ids)["buckets"][BUCKET_AFTER_CLOSE]]
        decision = {"decision_id": "trade-1", "content_sha256": "x" * 64, "items": []}
        base = self.policy_with_gate()
        plain = {k: v for k, v in stance([newest[0]]).items() if k != "recency_reconciliation"}
        with self.assertRaisesRegex(DecisionToolError, "recency_reconciliation"):
            apply_trade_decision(base, decision, plain, bundle)
        with self.assertRaisesRegex(DecisionToolError, "至少要引用"):
            apply_trade_decision(base, decision, stance([MARKET_REGIME_EVIDENCE_ID]), bundle)
        bound = apply_trade_decision(base, decision, stance([newest[0]]), bundle)
        self.assertEqual(bound["position_sizing"]["cash_stance"], "neutral")
        self.assertEqual(bound["position_sizing"]["cash_stance_recency"], GATE)

    def test_without_gate_trade_binding_rejects_the_field(self):
        from etf_agent.decision import apply_trade_decision
        from test_portfolio_risk_decision import policy as base_policy
        from test_position_sizing import SIZING

        bundle = overnight_bundle()
        base = base_policy()
        base["position_sizing"] = copy.deepcopy(SIZING)
        decision = {"decision_id": "trade-1", "content_sha256": "x" * 64, "items": []}
        with self.assertRaisesRegex(DecisionToolError, "未允許欄位"):
            apply_trade_decision(base, decision, stance([MARKET_REGIME_EVIDENCE_ID]), bundle)

    def test_replay_ignores_the_gate_and_the_field_without_touching_legacy_notes(self):
        from etf_agent.evaluation.replay import bind_replay_policy
        from test_cash_ratio_range import STANCE as LEGACY_STANCE, chain_inputs
        from test_daily_pipeline import gateless_policy_template

        bundle, _, debate, intent, archived, _, _, _ = chain_inputs()
        template = gateless_policy_template()
        template.update(policy_id="replay-A", available_at="2026-09-19T00:00:00+00:00")
        _, plain_notes = bind_replay_policy(template, bundle, archived, intent, debate, dict(LEGACY_STANCE), [])
        self.assertNotIn("cash_stance_recency", plain_notes)
        gated = copy.deepcopy(template)
        gated["position_sizing"]["cash_stance_recency"] = dict(GATE)
        policy, notes = bind_replay_policy(
            gated, bundle, archived, intent, debate, dict(LEGACY_STANCE, recency_reconciliation="newest_confirms"), [])
        self.assertEqual(notes["cash_stance_recency"], "ignored_in_replay")
        self.assertNotIn("cash_stance_recency", policy["position_sizing"])


if __name__ == "__main__":
    unittest.main()
