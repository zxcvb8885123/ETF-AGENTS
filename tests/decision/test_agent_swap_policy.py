"""position_sizing.agent_swap（換股改由交易 Agent 提名）的 Policy 驗證：形狀、值域、前提、v2 互斥與舊 Policy 不變。

只測第 1 步（Policy 驗證）；提名契約見 test_swap_nomination.py。閘門尚未在 config/decision_policy.json 開啟。
"""

import copy
import unittest

from etf_agent.decision.sizing import (
    AGENT_SWAP_FIELDS, GATE_FIELDS, GATE_PREREQUISITES, POSITION_SIZING_FIELDS, validate_position_sizing,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES, KNOBS

from test_portfolio_risk_decision import policy as base_policy
from test_rebound_participation import full_sizing
from test_rebound_participation_v2 import REBOUND_V2
from test_gate_prerequisites import production_policy

UNIVERSE = ["1101.TW", "2317.TW", "2330.TW"]
GATE = {"version": "v1", "max_swaps_per_run": 2, "min_holding_days": 5}
REF = {"verdicts_id": "v-1", "as_of": "2026-11-09T10:00:00+08:00", "sha256": "a" * 64}
PREREQUISITES = (
    "selection_ranking", "rotation", "held_add_priority", "reentry_cooldown", "turnover_fit", "cash_buffer_floor",
    "exposure_rotation", "rebound_participation",
)


def sizing_with(version="v1", **changes):
    sizing = full_sizing()
    sizing["rebound_participation"] = copy.deepcopy(REBOUND_V2)
    sizing["rotation"].pop("forced_swap", None)  # v2 與舊換股路徑互斥；v1 共存由專屬測試加回
    sizing["exposure_rotation"].pop("conviction_gap_swap", None)
    sizing["agent_swap"] = dict(GATE, version=version, **changes)
    return sizing


def errors_for(sizing, review=True):
    policy = base_policy()
    if review:
        policy["position_review"] = {"version": "v1", "loss_alert": "0.15", "retrace_alert_20d": None, "gain_alert": None}
    policy["position_sizing"] = sizing
    errors = []
    validate_position_sizing(policy, UNIVERSE, errors)
    return errors


def agent_swap_errors(sizing, **kwargs):
    return [error for error in errors_for(sizing, **kwargs) if "agent_swap" in error]


class AgentSwapGateTests(unittest.TestCase):
    def test_fixture_is_valid_in_both_versions(self):
        for version in ("v1", "v2"):
            self.assertEqual(errors_for(sizing_with(version)), [], version)

    def test_gate_is_declared_human_only_and_forbidden_to_reflection(self):
        self.assertIn("agent_swap", GATE_FIELDS)
        self.assertIn("agent_swap", POSITION_SIZING_FIELDS)
        rule = next(rule for rule in GATE_PREREQUISITES if rule.field == "agent_swap")
        self.assertTrue(rule.human_only)
        self.assertEqual(set(PREREQUISITES), set(rule.requires))
        self.assertIn("agent_swap", FORBIDDEN_PREFIXES)
        self.assertEqual([knob for knob in KNOBS if knob.startswith("agent_swap")], [])

    def test_version_range_and_required_fields(self):
        self.assertTrue(agent_swap_errors(sizing_with("v3")))
        self.assertTrue(agent_swap_errors(sizing_with(None)))
        for field in ("version", "max_swaps_per_run", "min_holding_days"):
            sizing = sizing_with()
            del sizing["agent_swap"][field]
            self.assertTrue(any("缺少必要欄位" in error and field in error for error in agent_swap_errors(sizing)), field)

    def test_unknown_fields_and_wrong_shapes_are_rejected(self):
        sizing = sizing_with()
        sizing["agent_swap"]["extra"] = 1
        self.assertTrue(agent_swap_errors(sizing))
        for bad in ("v1", [], None):
            sizing = sizing_with()
            sizing["agent_swap"] = bad
            self.assertTrue(agent_swap_errors(sizing), bad)

    def test_numeric_ranges(self):
        for field, good, bad in (("max_swaps_per_run", (1, 10), (0, 11, -1, True, "2", 2.0)),
                                 ("min_holding_days", (0, 60), (-1, 61, False, "5", 5.0))):
            for value in good:
                self.assertEqual(agent_swap_errors(sizing_with(**{field: value})), [], (field, value))
            for value in bad:
                self.assertTrue(agent_swap_errors(sizing_with(**{field: value})), (field, value))

    def test_every_prerequisite_is_enforced(self):
        for required in PREREQUISITES:
            sizing = sizing_with()
            del sizing[required]
            errors = errors_for(sizing)
            self.assertTrue(any("agent_swap" in error and required in error for error in errors), (required, errors))

    def test_top_level_position_review_is_required(self):
        errors = errors_for(sizing_with(), review=False)
        self.assertTrue(any("agent_swap" in error and "position_review" in error for error in errors), errors)

    def test_exposure_rotation_must_be_v3_and_rebound_must_be_v2(self):
        sizing = sizing_with()
        sizing["exposure_rotation"]["version"] = "v2"
        self.assertTrue(any("exposure_rotation.version=v3" in error for error in agent_swap_errors(sizing)))
        sizing = sizing_with()
        sizing["rebound_participation"] = full_sizing()["rebound_participation"]  # v1
        self.assertTrue(any("rebound_participation.version=v2" in error for error in agent_swap_errors(sizing)))

    def test_v2_is_exclusive_with_forced_swap_and_conviction_gap_swap_but_v1_coexists(self):
        for version, expected in (("v1", False), ("v2", True)):
            sizing = sizing_with(version)
            sizing["rotation"]["forced_swap"] = {"version": "v1", "min_new_conviction": "high", "max_swaps_per_run": 2}
            errors = agent_swap_errors(sizing)
            self.assertEqual(any("forced_swap" in error for error in errors), expected, (version, errors))
            if not expected:
                self.assertEqual(errors_for(sizing), [])
            sizing = sizing_with(version)
            sizing["exposure_rotation"]["conviction_gap_swap"] = "v1"
            errors = agent_swap_errors(sizing)
            self.assertEqual(any("conviction_gap_swap" in error for error in errors), expected, (version, errors))
            if not expected:
                self.assertEqual(errors_for(sizing), [])

    def test_thesis_hint_binding_shape(self):
        sizing = sizing_with(thesis_hint_symbols=["2317.TW", "2330.TW"], verdicts_ref=dict(REF))
        self.assertEqual(errors_for(sizing), [])
        cases = {
            "hints without ref": dict(thesis_hint_symbols=["2330.TW"]),
            "ref without hints": dict(verdicts_ref=dict(REF)),
            "empty list": dict(thesis_hint_symbols=[], verdicts_ref=dict(REF)),
            "unsorted": dict(thesis_hint_symbols=["2330.TW", "2317.TW"], verdicts_ref=dict(REF)),
            "duplicates": dict(thesis_hint_symbols=["2330.TW", "2330.TW"], verdicts_ref=dict(REF)),
            "lowercase": dict(thesis_hint_symbols=["2330.tw"], verdicts_ref=dict(REF)),
            "not a list": dict(thesis_hint_symbols="2330.TW", verdicts_ref=dict(REF)),
            "ref missing field": dict(thesis_hint_symbols=["2330.TW"], verdicts_ref={"verdicts_id": "v-1"}),
            "ref extra field": dict(thesis_hint_symbols=["2330.TW"], verdicts_ref=dict(REF, extra="x")),
            "ref empty value": dict(thesis_hint_symbols=["2330.TW"], verdicts_ref=dict(REF, sha256="")),
        }
        for label, changes in cases.items():
            self.assertTrue(agent_swap_errors(sizing_with(**changes)), label)

    def test_nominated_pairs_binding_shape_and_version(self):
        pairs = [{"buy_symbol": "2317.TW", "sell_symbol": "2330.TW"}]
        ref = {"nomination_id": "nom-1", "sha256": "a" * 64, "status": "completed"}
        good = sizing_with("v2", nominated_pairs=pairs, nomination_ref=dict(ref))
        self.assertEqual(errors_for(good), [])
        self.assertEqual(errors_for(sizing_with("v2", nominated_pairs=[], nomination_ref=dict(ref, status="not_needed"))), [])
        cases = {
            "v1 影子不得綁定": sizing_with("v1", nominated_pairs=pairs, nomination_ref=dict(ref)),
            "pairs 沒有 ref": sizing_with("v2", nominated_pairs=pairs),
            "ref 沒有 pairs": sizing_with("v2", nomination_ref=dict(ref)),
            "ref 缺欄位": sizing_with("v2", nominated_pairs=pairs, nomination_ref={"nomination_id": "nom-1"}),
            "ref 多欄位": sizing_with("v2", nominated_pairs=pairs, nomination_ref=dict(ref, extra="x")),
            "status 不在值域": sizing_with("v2", nominated_pairs=pairs, nomination_ref=dict(ref, status="shadow")),
            "非 completed 卻有配對": sizing_with("v2", nominated_pairs=pairs, nomination_ref=dict(ref, status="exhausted")),
            "pairs 不是清單": sizing_with("v2", nominated_pairs="2317.TW", nomination_ref=dict(ref)),
            "配對多出欄位": sizing_with("v2", nominated_pairs=[dict(pairs[0], reason="x")], nomination_ref=dict(ref)),
            "小寫代號": sizing_with("v2", nominated_pairs=[{"buy_symbol": "2317.tw", "sell_symbol": "2330.TW"}], nomination_ref=dict(ref)),
            "換入換出相同": sizing_with("v2", nominated_pairs=[{"buy_symbol": "2330.TW", "sell_symbol": "2330.TW"}], nomination_ref=dict(ref)),
            "換入重複": sizing_with("v2", nominated_pairs=pairs + [{"buy_symbol": "2317.TW", "sell_symbol": "1101.TW"}], nomination_ref=dict(ref)),
            "換出重複": sizing_with("v2", nominated_pairs=pairs + [{"buy_symbol": "1101.TW", "sell_symbol": "2330.TW"}], nomination_ref=dict(ref)),
            "超過對數上限": sizing_with("v2", max_swaps_per_run=1, nominated_pairs=pairs + [{"buy_symbol": "1101.TW", "sell_symbol": "2412.TW"}],
                                  nomination_ref=dict(ref)),
        }
        for label, sizing in cases.items():
            self.assertTrue(agent_swap_errors(sizing), label)

    def test_v1_passes_on_the_full_fixture_that_still_has_the_old_swap_paths(self):
        sizing = full_sizing()
        sizing["rebound_participation"] = copy.deepcopy(REBOUND_V2)
        sizing["agent_swap"] = dict(GATE)
        self.assertEqual(errors_for(sizing), [])
        sizing["agent_swap"]["version"] = "v2"
        self.assertEqual(len([e for e in agent_swap_errors(sizing) if "不得與" in e]), 2)

    def test_policy_without_agent_swap_is_unchanged(self):
        sizing = full_sizing()
        self.assertEqual(errors_for(sizing, review=False), errors_for(sizing, review=True))
        self.assertFalse(any("agent_swap" in error for error in errors_for(sizing)))
        live = copy.deepcopy(production_policy()["position_sizing"])
        self.assertNotIn("agent_swap", live)
        before = []
        policy = base_policy()
        policy["position_sizing"] = live
        validate_position_sizing(policy, UNIVERSE[1:], before)
        self.assertEqual(before, [])
        self.assertEqual(set(AGENT_SWAP_FIELDS), {
            "version", "max_swaps_per_run", "min_holding_days", "thesis_hint_symbols", "verdicts_ref", "nominated_pairs", "nomination_ref",
            "exposure",  # Agent 主導曝險階段 2、3 的子閘門（decision/exposure_swap.py）
            "quality_dip",  # 優質股第三階段的子閘門（decision/quality_dip_swap.py）
            "derisk_day_view",  # 減碼日改由風險 Agent 的 exposure_view 決定是否換股（版本閘門）
            "proceeds_to_add",  # 換股賣款可流向高信心加碼持股（版本閘門）
        })


if __name__ == "__main__":
    unittest.main()
