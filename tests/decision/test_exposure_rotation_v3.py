"""position_sizing.exposure_rotation v3：欄位集合與行為都同 v2，唯一差別是 beta_target_by_stance.defensive 可填數字。

覆蓋：v3 閘門形狀（defensive 數字／null、值域與大小順序、v1 專屬欄位拒絕）、v1／v2 的 defensive 仍只能 null、
換股配對（防守姿態觸發、max_swaps_per_run、defensive 為 null 等同不觸發、與 v2 逐位相同的對照）、
端到端（防守換股結果、drawdown_derisk 日略過、竄改閘門被拒）。v1／v2 的行為由各自測試檔鎖定。
"""

import copy
import unittest
from decimal import Decimal

from etf_agent.decision import DecisionPolicyValidator, DecisionToolError, ProposalValidator, decision_policy_sha256
from etf_agent.decision.exposure import (
    EXPOSURE_ROTATION_V3, GATE_FIELDS_V2, GATE_REQUIRED_FIELDS_V2, exposure_rotation_plan, gate_has_targets, gate_is_v2,
    gate_is_v3,
)
from etf_agent.decision.sizing import validate_position_sizing

from test_exposure_rotation import (
    GATE, HELD_SINCE, codes, decisions, exposure_bundle, exposure_sizing, fill, flag_map, narrow_world, summary, victim,
)
from test_exposure_rotation_e2e import EXPOSURE_CODES, allocate, codes as flag_codes, sold_and_bought
from test_exposure_rotation_v2 import gate_v2
from test_portfolio_risk_decision import policy as base_policy
from test_risk_limit_gates import guard


def gate_v3(defensive="0.35", **changes):
    gate = gate_v2(**changes)
    gate["version"] = changes.get("version", "v3")
    if "beta_target_by_stance" not in changes:
        gate["beta_target_by_stance"] = {"aggressive": "0.90", "neutral": "0.55", "defensive": defensive}
    return gate


def sizing_errors(gate):
    sizing = exposure_sizing(exposure_bundle(), gate)
    policy = base_policy()
    policy["position_sizing"] = sizing
    errors = []
    validate_position_sizing(policy, ["1101.TW", "2317.TW", "2330.TW"], errors)
    return errors


class GateShapeV3Tests(unittest.TestCase):
    def test_v3_uses_the_v2_field_set_and_is_recognised(self):
        gate = gate_v3()
        self.assertEqual(sizing_errors(gate), [])
        self.assertTrue(gate_is_v3(gate))
        self.assertFalse(gate_is_v2(gate))
        self.assertTrue(gate_has_targets(gate) and gate_has_targets(gate_v2()))
        self.assertFalse(gate_has_targets(GATE) or gate_has_targets(None) or gate_is_v3(None))
        self.assertEqual(gate["version"], EXPOSURE_ROTATION_V3)
        self.assertEqual(set(gate) - {"held_since_by_symbol"}, set(GATE_FIELDS_V2) - {"held_since_by_symbol"})

    def test_defensive_may_be_null_or_a_number_in_v3(self):
        self.assertEqual(sizing_errors(gate_v3(defensive=None)), [])
        self.assertEqual(sizing_errors(gate_v3(defensive="0.35")), [])

    def test_defensive_number_is_still_rejected_in_v1_and_v2(self):
        v2 = gate_v2(beta_target_by_stance={"aggressive": "0.9", "neutral": "0.55", "defensive": "0.35"})
        self.assertTrue(any("defensive 只能是 null" in e for e in sizing_errors(v2)))
        v1 = copy.deepcopy(GATE)
        v1["held_since_by_symbol"] = dict(HELD_SINCE)
        v1["beta_floor_by_stance"] = {"aggressive": "0.9", "neutral": "0.55", "defensive": "0.35"}
        self.assertTrue(any("defensive 只能是 null" in e for e in sizing_errors(v1)))

    def test_v3_shape_violations_are_rejected(self):
        cases = {
            "數字字串": {"beta_target_by_stance": {"aggressive": "0.9", "neutral": "0.55", "defensive": 0.35}},
            "必須大於 0": {"beta_target_by_stance": {"aggressive": "0.9", "neutral": "0.55", "defensive": "0"}},
            "不超過 2": {"beta_target_by_stance": {"aggressive": "0.9", "neutral": "0.55", "defensive": "3"}},
            "剛好包含": {"beta_target_by_stance": {"aggressive": "0.9", "neutral": "0.55"}},
            "neutral ≥ defensive": {"beta_target_by_stance": {"aggressive": "0.9", "neutral": "0.30", "defensive": "0.35"}},
            "aggressive ≥ neutral": {"beta_target_by_stance": {"aggressive": "0.4", "neutral": "0.55", "defensive": "0.35"}},
            "beta_window_days": {"beta_window_days": 5},
            "max_swaps_per_run": {"max_swaps_per_run": 0},
        }
        for message, change in cases.items():
            with self.subTest(message=message):
                self.assertTrue(any(message in e for e in sizing_errors(gate_v3(**change))), message)

    def test_missing_required_fields_and_v1_only_fields_are_rejected(self):
        for field in sorted(GATE_REQUIRED_FIELDS_V2):
            with self.subTest(field=field):
                gate = gate_v3()
                del gate[field]
                self.assertTrue(any("缺少必要欄位" in e and field in e for e in sizing_errors(gate)), field)
        for field, value in (("release_margin", "0.1"), ("max_rotation_fraction", "0.2"), ("min_new_conviction", "medium"),
                             ("beta_floor_by_stance", {"aggressive": "0.9", "neutral": "0.55", "defensive": None})):
            with self.subTest(field=field):
                self.assertTrue(any("含未允許欄位" in e and field in e for e in sizing_errors(gate_v3(**{field: value}))), field)

    def test_unknown_version_is_still_rejected(self):
        self.assertTrue(any("version 只允許" in e for e in sizing_errors(gate_v3(version="v4"))))


class PlanV3Tests(unittest.TestCase):
    NAV = Decimal("1000000")

    def plan(self, victims, fills, stance_name="defensive", beta="0.13", gate=None, afford=None):
        return exposure_rotation_plan(
            gate or gate_v3(conviction_gap_swap=None), stance_name, None if beta is None else Decimal(beta), self.NAV,
            victims, fills, afford, min_new_conviction="medium",
        )

    def test_defensive_stance_triggers_when_the_target_is_a_number(self):
        result = self.plan([victim("V", "low", "0.05")], [fill("F", "high", "1.5")])
        self.assertEqual([(s["kind"], s["victim"], s["fill"]) for s in result["swaps"]], [("exposure", "V", "F")])

    def test_defensive_null_is_the_same_as_not_triggered(self):
        result = self.plan([victim("V", "low", "0.05")], [fill("F", "high", "1.5")], gate=gate_v3(defensive=None, conviction_gap_swap=None))
        self.assertEqual((result["swaps"], result["skipped"]["exposure"]), ([], "stance_not_triggered"))

    def test_defensive_at_or_above_the_target_does_not_swap(self):
        for beta in ("0.35", "0.50"):
            result = self.plan([victim("V", "low", "0.05")], [fill("F", "high", "1.5")], beta=beta)
            self.assertEqual((result["swaps"], result["skipped"]["exposure"]), ([], "beta_at_target"), beta)

    def test_swaps_never_exceed_max_swaps_per_run_and_stop_at_the_target(self):
        victims = [victim("V%d" % i, "low", "0.0%d" % i, weight="0.01", value="10000") for i in (1, 2, 3, 4)]
        fills = [fill("F%d" % i, "high", "2") for i in (1, 2, 3, 4)]
        gate = gate_v3(max_swaps_per_run=2, conviction_gap_swap=None, defensive="0.90",
                       beta_target_by_stance={"aggressive": "0.95", "neutral": "0.92", "defensive": "0.90"})
        result = self.plan(victims, fills, gate=gate, beta="0.10")
        self.assertEqual(len(result["swaps"]), 2)
        self.assertEqual(self.plan(victims, fills, gate=dict(gate, max_swaps_per_run=1), beta="0.10")["swaps"][0]["kind"], "exposure")
        self.assertEqual(len(self.plan(victims, fills, gate=dict(gate, max_swaps_per_run=1), beta="0.10")["swaps"]), 1)

    def test_no_swap_without_a_fill_so_it_is_never_sell_only(self):
        result = self.plan([victim("V", "low", "0.05")], [])
        self.assertEqual((result["swaps"], result["skipped"]["exposure"]), ([], "no_eligible_fill"))

    def test_v3_equals_v2_for_every_stance_with_a_null_defensive(self):
        victims = [victim("V1", "low", "0.05", weight="0.10"), victim("V2", "medium", "0.08", weight="0.10")]
        fills = [fill("F1", "high", "1.5"), fill("F2", "medium", "0.95")]
        v2 = gate_v2()
        v3 = gate_v3(defensive=None)
        for beta in ("0.10", "0.30", "0.44", "0.60", None):
            for name in ("neutral", "aggressive", "defensive"):
                with self.subTest(beta=beta, stance=name):
                    old = exposure_rotation_plan(v2, name, None if beta is None else Decimal(beta), self.NAV, victims, fills,
                                                 lambda *_: True, min_new_conviction="medium")
                    new = exposure_rotation_plan(v3, name, None if beta is None else Decimal(beta), self.NAV, victims, fills,
                                                 lambda *_: True, min_new_conviction="medium")
                    self.assertEqual(old, new)

    def test_missing_threshold_is_fail_closed(self):
        with self.assertRaises(DecisionToolError):
            exposure_rotation_plan(gate_v3(), "defensive", Decimal("0.1"), self.NAV, [victim("V", "low", "0.05")],
                                   [fill("F", "high", "2")], None)


class AllocationV3Tests(unittest.TestCase):
    def test_defensive_stance_swaps_the_low_beta_hold_for_the_high_beta_buy(self):
        # 世界：組合 beta ≈ 0.28 < 防守目標 0.35。1101 hold medium（避免信心差換股），2330 hold medium 且 beta 較低 → 換出。
        bundle, intent, policy, body = narrow_world("defensive", items=decisions(c1101="medium"), gate=gate_v3())
        # 防守姿態的現金緩衝較高，買量比中性少（2000 股）；重點是賣 2330（exit）、買 2317。
        self.assertEqual(summary(body), {("2330.TW", "sell"): (1000, "exit"), ("2317.TW", "buy"): (2000, "buy")})
        found = flag_map(body)
        self.assertIn(("2330.TW", "RISK_EXPOSURE_ROTATION"), found)
        self.assertIn(("2317.TW", "EXPOSURE_ROTATION_FILL"), found)
        self.assertTrue(guard(bundle, policy, body)["passed"])
        # 交易 Agent 的意圖不被改寫：2330 仍是 hold，只有訂單意圖標 exit。
        self.assertEqual(next(i for i in intent["items"] if i["symbol"] == "2330.TW")["intent"], "hold")
        # 不只賣不買。
        sides = {o["side"] for o in body["order_proposal"]["orders"]}
        self.assertEqual(sides, {"sell", "buy"})

    def test_defensive_null_in_v3_is_not_triggered(self):
        _, _, _, body = narrow_world("defensive", items=decisions(c1101="medium"), gate=gate_v3(defensive=None))
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertIn(("PORTFOLIO", "EXPOSURE_ROTATION_SKIPPED:stance_not_triggered"), flag_map(body))

    def test_v2_defensive_still_does_not_trigger_in_the_same_world(self):
        _, _, _, body = narrow_world("defensive", items=decisions(c1101="medium"), gate=gate_v2())
        self.assertEqual(body["order_proposal"]["orders"], [])

    def test_v3_matches_v2_in_neutral_and_aggressive(self):
        for level in ("neutral", "aggressive"):
            with self.subTest(level=level):
                v2 = narrow_world(level, gate=gate_v2(beta_target_by_stance={"aggressive": "0.90", "neutral": "0.55", "defensive": None}))[3]
                v3 = narrow_world(level, gate=gate_v3())[3]
                for key in ("allocation_proposal", "order_proposal", "constraint_flags"):
                    self.assertEqual(v2[key], v3[key], key)

    def test_target_below_the_portfolio_beta_does_not_swap_on_a_defensive_day(self):
        low = gate_v3(defensive="0.10", beta_target_by_stance={"aggressive": "0.90", "neutral": "0.55", "defensive": "0.10"})
        _, _, _, body = narrow_world("defensive", items=decisions(c1101="medium"), gate=low)
        self.assertIn(("PORTFOLIO", "EXPOSURE_ROTATION_SKIPPED:beta_at_target"), flag_map(body))
        self.assertEqual(codes(body, "RISK_EXPOSURE_ROTATION"), [])

    def test_drawdown_derisk_day_skips_even_when_defensive_has_a_target(self):
        bundle = exposure_bundle(cash="136000", positions={"2330.TW": 1000, "1101.TW": 7000})
        _, policy, body = allocate(bundle, level="defensive", grade="lower", gate=gate_v3())
        found = flag_codes(body)
        self.assertIn("RISK_DRAWDOWN", found)
        self.assertIn("EXPOSURE_ROTATION_SKIPPED:derisk_day", found)
        self.assertFalse(found & set(EXPOSURE_CODES), found)
        sold, _ = sold_and_bought(body)
        self.assertTrue(sold)
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_tampering_a_v3_gate_is_rejected(self):
        bundle, intent, policy, body = narrow_world("defensive", items=decisions(c1101="medium"), gate=gate_v3())
        for mutate in (
            lambda p: p["position_sizing"]["exposure_rotation"].update({"version": "v2"}),
            lambda p: p["position_sizing"]["exposure_rotation"].update(
                {"beta_target_by_stance": {"aggressive": "0.90", "neutral": "0.55", "defensive": None}}),
            lambda p: p["position_sizing"]["exposure_rotation"].update({"release_margin": "0.10"}),
            lambda p: p["position_sizing"]["exposure_rotation"]["beta_target_by_stance"].update({"defensive": "0.05"}),
        ):
            altered = copy.deepcopy(policy)
            mutate(altered)
            altered["content_sha256"] = decision_policy_sha256(altered)
            errors = DecisionPolicyValidator(bundle).validate(altered)
            rebuilt = ProposalValidator(bundle, altered, intent).validate(body) if not errors else errors
            self.assertTrue(rebuilt)


if __name__ == "__main__":
    unittest.main()
