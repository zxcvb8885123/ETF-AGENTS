"""position_sizing.rebound_participation v2（只算事實、由風險 Agent 單獨決定）的測試。

v2 與 v1 的差別：程式不再用固定門檻推進 none／candidate／confirmed 狀態機，只整理最近幾日的事實與資料品質；
有效狀態只看風險 Agent 的 ``turn_assessment`` 對映，資料缺漏或回撤下限為 defensive 時壓回 none，regime 不是 bear 不擋。
v1 的全部測試（test_rebound_participation.py）維持不變。這是機制測試，不是對真實行情的驗證。
"""

import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.decision import MomentumEngine

from etf_agent.decision import DecisionToolError
from etf_agent.decision.rebound import (
    AGENT_STATE_BY_ASSESSMENT, GATE_FIELDS_V2, SIGNAL_FIELDS_V2, TURN_STATE_FIELDS_V2, TURN_STATE_UNAVAILABLE,
    apply_rebound, bind_effective_state, build_turn_state, effective_state, validate_effective_state, validate_turn_state,
)
from etf_agent.decision.turn_facts import TURN_ASSESSMENTS

from test_cash_stance_turn_facts import GATE as TURN_FACTS_GATE, overnight_bundle, stance as turn_stance
from test_daily_pipeline import FakeRunner
from test_rebound_participation import REBOUND, ROOT, dates, full_sizing, series_bundle, sizing_errors
from test_typed_evidence import typed_reports

REBOUND_V2 = {
    "version": "v2",
    "min_coverage": "0.80",
    "by_state": copy.deepcopy(REBOUND["by_state"]),
}


def facts(moves, **kwargs):
    return build_turn_state(series_bundle(moves, **kwargs), REBOUND_V2)


def v2_sizing(**changes):
    sizing = full_sizing()
    sizing["rebound_participation"] = dict(copy.deepcopy(REBOUND_V2), **changes)
    return sizing


class FactsTests(unittest.TestCase):
    def test_shape_is_exactly_the_whitelist_and_has_no_state_or_threshold(self):
        result = facts(["0.04", "0.01"])
        self.assertEqual(set(result), TURN_STATE_FIELDS_V2)
        self.assertEqual(set(result["signals"]), SIGNAL_FIELDS_V2)
        self.assertEqual((result["schema_version"], result["version"], result["data_status"]), ("2.0", "v2", "available"))
        self.assertNotIn("state", result)
        self.assertNotIn("candidate_since", result)
        self.assertEqual(result["flags"], [])
        self.assertEqual(result["signals"]["pool_return_latest"], "0.010000")
        self.assertEqual(result, facts(["0.04", "0.01"]))  # 確定性

    def test_recent_path_carries_the_last_five_days_so_the_agent_keeps_the_multi_day_view(self):
        result = facts(["0.04", "0.01", "-0.005"])
        self.assertEqual(len(result["recent_path"]), 5)
        self.assertEqual(result["recent_path"][-1]["pool_return"], "-0.005000")
        self.assertEqual(result["recent_path"][-2]["pool_return"], "0.010000")
        self.assertEqual(result["recent_path"][-3]["pool_return"], "0.040000")
        self.assertEqual(set(result["recent_path"][-1]), {
            "date", "pool_return", "share_above_ma5", "new_low_20d_share", "regime", "coverage"})

    def test_no_threshold_decides_anything_a_flat_day_is_still_available_data(self):
        self.assertEqual(facts([])["data_status"], "available")
        self.assertEqual(facts(["0.0001"])["data_status"], "available")

    def test_short_history_is_unavailable_with_a_flag(self):
        result = facts(["0.04"], base=59)  # regime 算不出
        self.assertEqual(result["data_status"], "unavailable")
        self.assertIn(TURN_STATE_UNAVAILABLE, result["flags"])

    def test_thin_coverage_is_unavailable(self):
        bundle = series_bundle(["0.04"])
        for row in bundle["price_series"][:3]:
            row["bars"].pop()
        result = build_turn_state(bundle, REBOUND_V2)
        self.assertEqual(result["data_status"], "unavailable")
        self.assertEqual(result["signals"]["coverage"], "0.700000")

    def test_a_bar_that_is_not_the_latest_trade_date_is_unavailable_not_an_exception(self):
        bundle = series_bundle(["0.04"])
        bundle["snapshot"]["latest_trade_date"] = dates(67)[-2]
        result = build_turn_state(bundle, REBOUND_V2)
        self.assertEqual((result["data_status"], result["regime"], result["recent_path"]), ("unavailable", None, []))
        self.assertIn(TURN_STATE_UNAVAILABLE, result["flags"])

    def test_untyped_bundle_is_rejected(self):
        with self.assertRaisesRegex(DecisionToolError, "typed_evidence_v1"):
            facts(["0.04"], typed=False)


class AgentKeyTests(unittest.TestCase):
    def test_the_agent_alone_sets_the_state_when_data_is_available(self):
        available = facts(["0.04", "0.01"])
        for assessment in TURN_ASSESSMENTS:
            with self.subTest(assessment=assessment):
                self.assertEqual(effective_state(available, {"turn_assessment": assessment}, None), AGENT_STATE_BY_ASSESSMENT[assessment])

    def test_a_non_bear_regime_no_longer_blocks(self):
        bull = series_bundle(["0.04", "0.01"], drift=Decimal("0.006"), step=Decimal("0.001"))
        result = build_turn_state(bull, REBOUND_V2)
        self.assertEqual((result["regime"], result["data_status"]), ("bull", "available"))
        self.assertEqual(effective_state(result, {"turn_assessment": "turn_confirmed"}, None), "confirmed")

    def test_missing_data_forces_none_whatever_the_agent_says(self):
        unavailable = facts(["0.04"], base=59)
        for assessment in TURN_ASSESSMENTS:
            self.assertEqual(effective_state(unavailable, {"turn_assessment": assessment}, None), "none", assessment)

    def test_a_defensive_stance_floor_always_wins(self):
        available = facts(["0.04", "0.01"])
        self.assertEqual(effective_state(available, {"turn_assessment": "turn_confirmed"}, "defensive"), "none")
        self.assertEqual(effective_state(available, {"turn_assessment": "turn_confirmed"}, "neutral"), "confirmed")

    def test_missing_or_invalid_keys_fail_closed(self):
        available = facts(["0.04", "0.01"])
        with self.assertRaises(DecisionToolError):
            effective_state(available, {}, None)
        with self.assertRaises(DecisionToolError):
            effective_state(available, {"turn_assessment": "bounce"}, None)
        with self.assertRaises(DecisionToolError):
            effective_state(dict(available, data_status="maybe"), {"turn_assessment": "turn_confirmed"}, None)

    def test_binding_and_mapping_reuse_the_v1_rows(self):
        sizing = {"rebound_participation": copy.deepcopy(REBOUND_V2)}
        bound = bind_effective_state(sizing, series_bundle(["0.04", "0.01"]), {"turn_assessment": "turn_confirmed"}, None)
        self.assertEqual(bound["effective_state"], "confirmed")
        self.assertNotIn("effective_state", sizing["rebound_participation"])
        exposure = {"max_swaps_per_run": 1, "min_holding_days": 10, "beta_target_by_stance": {"defensive": None, "neutral": "0.5"}}
        loosened, state = apply_rebound(exposure, {"rebound_participation": bound})
        self.assertEqual((state, loosened["max_swaps_per_run"], loosened["min_holding_days"]), ("confirmed", 4, 0))
        self.assertEqual(loosened["beta_target_by_stance"]["defensive"], "0.55")


class FinalizerTests(unittest.TestCase):
    def test_validate_turn_state_rejects_missing_tampered_and_extra_fields(self):
        bundle = series_bundle(["0.04", "0.01"])
        policy = {"position_sizing": {"rebound_participation": copy.deepcopy(REBOUND_V2)}}
        good = build_turn_state(bundle, REBOUND_V2)
        errors = []
        validate_turn_state(bundle, policy, good, errors)
        self.assertEqual(errors, [])
        for label, value in (
            ("缺漏", None),
            ("改資料狀態", dict(good, data_status="unavailable")),
            ("數字改寫", dict(good, signals=dict(good["signals"], pool_return_latest="0.090000"))),
            ("多餘欄位", dict(good, state="confirmed")),
            ("少欄位", {k: v for k, v in good.items() if k != "flags"}),
            ("v1 形狀", {**good, "schema_version": "1.0"}),
        ):
            with self.subTest(label):
                errors = []
                validate_turn_state(bundle, policy, value, errors)
                self.assertTrue(errors, label)

    def test_the_bound_state_must_equal_the_agent_mapping_and_data_must_be_available(self):
        def check(bound, turn_state, assessment, floor="aggressive"):
            policy = {"position_sizing": {"rebound_participation": dict(copy.deepcopy(REBOUND_V2), effective_state=bound)},
                      "drawdown_control": {"stance_floor": floor}}
            errors = []
            validate_effective_state(policy, {"turn_state": turn_state, "cash_stance": {"turn_assessment": assessment}}, errors)
            return errors

        available, unavailable = facts(["0.04", "0.01"]), facts(["0.04"], base=59)
        self.assertEqual(check("confirmed", available, "turn_confirmed"), [])
        self.assertEqual(check("candidate", available, "turn_unconfirmed"), [])
        self.assertEqual(check("none", unavailable, "turn_confirmed"), [])
        self.assertEqual(check("none", available, "turn_confirmed", floor="defensive"), [])
        for bound, turn_state, assessment in (
            ("confirmed", unavailable, "turn_confirmed"), ("candidate", available, "turn_confirmed"),
            ("none", available, "turn_confirmed"), ("confirmed", available, "no_turn_evidence"),
        ):
            with self.subTest(bound=bound, assessment=assessment):
                self.assertTrue(any("重算結果不一致" in e for e in check(bound, turn_state, assessment)))


class GateShapeTests(unittest.TestCase):
    def test_a_valid_v2_gate_passes_and_v1_stays_valid(self):
        self.assertEqual(sizing_errors(v2_sizing()), [])
        self.assertEqual(sizing_errors(full_sizing()), [])
        self.assertEqual(set(REBOUND_V2), {"version", "min_coverage", "by_state"})
        self.assertTrue({"version", "min_coverage", "by_state"} <= GATE_FIELDS_V2)

    def test_v1_threshold_fields_are_rejected_on_v2_and_missing_ones_on_v1(self):
        for field, value in (("candidate_return", "0.03"), ("confirm_ma5_share", "0.5"), ("retreat_window_days", 3)):
            with self.subTest(field=field):
                self.assertTrue(any("含未允許欄位" in e for e in sizing_errors(v2_sizing(**{field: value}))), field)
        v1_without = full_sizing()
        del v1_without["rebound_participation"]["candidate_return"]
        self.assertTrue(any("缺少必要欄位" in e and "candidate_return" in e for e in sizing_errors(v1_without)))

    def test_v2_field_rules(self):
        self.assertTrue(any("缺少必要欄位" in e and "min_coverage" in e for e in sizing_errors(_without("min_coverage"))))
        self.assertTrue(any("min_coverage" in e for e in sizing_errors(v2_sizing(min_coverage="0.2"))))
        self.assertTrue(any("version 只允許" in e for e in sizing_errors(v2_sizing(version="v3"))))
        self.assertTrue(any("effective_state" in e for e in sizing_errors(v2_sizing(effective_state="strong"))))
        self.assertTrue(any("剛好包含 candidate／confirmed" in e
                            for e in sizing_errors(v2_sizing(by_state={"candidate": REBOUND_V2["by_state"]["candidate"]}))))


class BriefTests(unittest.TestCase):
    def test_risk_brief_carries_v2_facts_as_non_citable_and_says_the_agent_alone_decides(self):
        bundle = overnight_bundle()
        momentum = MomentumEngine(bundle).run()
        reports = typed_reports(bundle)
        state = build_turn_state(bundle, REBOUND_V2)
        runner = FakeRunner({"cash_stance": [turn_stance()]})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            pipeline.run_cash_stance(bundle, momentum, reports, cash_stance_turn_facts=TURN_FACTS_GATE, turn_state=state)
            brief = json.loads((Path(directory) / "brief_cash_stance.json").read_text(encoding="utf-8"))
        self.assertEqual(brief["turn_state"], {"citable": False, **state})
        self.assertNotIn("evidence_id", brief["turn_state"])
        prompt = runner.prompts[0][1]
        self.assertIn("唯一決定", prompt)
        self.assertNotIn("取較保守者", prompt.split("turn_state（v2")[-1])


def _without(field):
    sizing = v2_sizing()
    del sizing["rebound_participation"][field]
    return sizing


if __name__ == "__main__":
    unittest.main()
