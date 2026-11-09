"""position_sizing.exposure_rotation v2：刪 max_rotation_fraction／min_new_conviction、release_margin 併進停止線。

覆蓋：v2 閘門形狀（欄位白名單、停止線值域、v1 專屬欄位一律拒絕）、v1 閘門不受影響、換股配對純函式（停止線、
換入者 beta 嚴格高於組合 beta、沒有賣出比例上限、信心門檻由 rotation 傳入）、與 v1 的等價對照、
端到端配置（換股結果與 v1 相同、竄改閘門被拒、缺 rotation 門檻 fail-closed）。
v1 的行為由 test_exposure_rotation.py 逐位元鎖定，本檔不重複。
"""

import copy
import unittest
from decimal import Decimal

from etf_agent.decision import DecisionPolicyValidator, DecisionToolError, ProposalValidator, decision_policy_sha256
from etf_agent.decision.exposure import (
    EXPOSURE_ROTATION_V2, GATE_FIELDS, GATE_FIELDS_V2, GATE_REQUIRED_FIELDS, GATE_REQUIRED_FIELDS_V2,
    exposure_rotation_plan, gate_is_v2,
)
from etf_agent.decision.sizing import validate_position_sizing

from test_exposure_rotation import (
    GATE, HELD_SINCE, codes, decisions, exposure_bundle, exposure_sizing, fill, flag_map, narrow_world, summary, victim,
)
from test_gate_prerequisites import anchored_sizing
from test_portfolio_risk_decision import policy as base_policy
from test_risk_limit_gates import guard

GATE_V2 = {
    "version": "v2",
    "beta_window_days": 60,
    "beta_target_by_stance": {"aggressive": "0.90", "neutral": "0.55", "defensive": None},
    "max_swaps_per_run": 2,
    "min_holding_days": 5,
    "hold_conviction": "v1",
    "conviction_gap_swap": "v1",
}
V1_ONLY = ("beta_floor_by_stance", "release_margin", "max_rotation_fraction", "min_new_conviction")


def gate_v2(**changes):
    gate = copy.deepcopy(GATE_V2)
    gate["held_since_by_symbol"] = dict(HELD_SINCE)
    gate.update(changes)
    return gate


class GateShapeV2Tests(unittest.TestCase):
    def sizing(self, gate=None, **changes):
        return exposure_sizing(exposure_bundle(), gate if gate is not None else gate_v2(**changes))

    def errors(self, sizing):
        policy = base_policy()
        policy["position_sizing"] = sizing
        errors = []
        validate_position_sizing(policy, ["1101.TW", "2317.TW", "2330.TW"], errors)
        return errors

    def test_field_sets_drop_the_three_redundant_fields_and_fold_the_margin_into_the_target(self):
        self.assertEqual(GATE_REQUIRED_FIELDS - GATE_REQUIRED_FIELDS_V2, set(V1_ONLY))
        self.assertEqual(GATE_REQUIRED_FIELDS_V2 - GATE_REQUIRED_FIELDS, {"beta_target_by_stance"})
        self.assertEqual(GATE_FIELDS_V2 - GATE_FIELDS, {"beta_target_by_stance"})
        self.assertTrue(set(GATE_FIELDS_V2).isdisjoint(set(V1_ONLY)))

    def test_valid_v2_gate_passes_and_is_recognised(self):
        sizing = self.sizing()
        self.assertEqual(self.errors(sizing), [])
        self.assertTrue(gate_is_v2(sizing["exposure_rotation"]))
        self.assertEqual(sizing["exposure_rotation"]["version"], EXPOSURE_ROTATION_V2)
        self.assertFalse(gate_is_v2(None))
        self.assertFalse(gate_is_v2(GATE))

    def test_v1_gate_is_still_valid_and_not_v2(self):
        sizing = exposure_sizing(exposure_bundle())
        self.assertEqual(self.errors(sizing), [])
        self.assertFalse(gate_is_v2(sizing["exposure_rotation"]))

    def test_gap_swap_is_optional_and_the_target_can_be_switched_off(self):
        gate = gate_v2(beta_target_by_stance={"aggressive": None, "neutral": None, "defensive": None})
        del gate["conviction_gap_swap"]
        self.assertEqual(self.errors(self.sizing(gate)), [])

    def test_v1_only_fields_are_rejected_in_v2(self):
        values = {"beta_floor_by_stance": {"aggressive": "0.9", "neutral": "0.55", "defensive": None},
                  "release_margin": "0.10", "max_rotation_fraction": "0.20", "min_new_conviction": "medium"}
        for field in V1_ONLY:
            with self.subTest(field=field):
                errors = self.errors(self.sizing(**{field: values[field]}))
                self.assertTrue(any("含未允許欄位" in error and field in error for error in errors), errors)

    def test_v2_fields_are_rejected_in_v1(self):
        gate = copy.deepcopy(GATE)
        gate["held_since_by_symbol"] = dict(HELD_SINCE)
        gate["beta_target_by_stance"] = {"aggressive": "0.90", "neutral": "0.55", "defensive": None}
        errors = self.errors(self.sizing(gate))
        self.assertTrue(any("含未允許欄位" in error and "beta_target_by_stance" in error for error in errors), errors)

    def test_missing_required_fields_are_rejected(self):
        for field in sorted(GATE_REQUIRED_FIELDS_V2):
            with self.subTest(field=field):
                gate = gate_v2()
                del gate[field]
                self.assertTrue(any("缺少必要欄位" in error and field in error for error in self.errors(self.sizing(gate))), field)

    def test_shape_violations_are_rejected(self):
        cases = {
            "version 只允許": {"version": "v4"},
            "beta_window_days": {"beta_window_days": 5},
            "max_swaps_per_run": {"max_swaps_per_run": 0},
            "min_holding_days": {"min_holding_days": -1},
            "hold_conviction": {"hold_conviction": "v2"},
            "defensive 只能是 null": {"beta_target_by_stance": {"aggressive": "0.9", "neutral": "0.55", "defensive": "0.3"}},
            "aggressive ≥ neutral": {"beta_target_by_stance": {"aggressive": "0.5", "neutral": "0.55", "defensive": None}},
            "剛好包含": {"beta_target_by_stance": {"aggressive": "0.9", "neutral": "0.55"}},
            "必須大於 0": {"beta_target_by_stance": {"aggressive": "0", "neutral": "0", "defensive": None}},
            "數字字串": {"beta_target_by_stance": {"aggressive": 0.9, "neutral": "0.55", "defensive": None}},
            "held_since_by_symbol 必須是物件": {"held_since_by_symbol": ["2330.TW"]},
        }
        for message, change in cases.items():
            with self.subTest(message=message):
                errors = self.errors(self.sizing(**change))
                self.assertTrue(any(message in error for error in errors), (message, errors))

    def test_prerequisites_are_the_same_as_v1(self):
        for field in ("rotation", "held_add_priority", "selection_ranking", "reentry_cooldown", "turnover_fit", "cash_buffer_floor"):
            sizing = self.sizing()
            sizing.pop(field)
            self.assertTrue(any("exposure_rotation 必須與 %s 同時存在" % field in error for error in self.errors(sizing)), field)


class PlanV2Tests(unittest.TestCase):
    NAV = Decimal("1000000")

    def plan(self, victims, fills, stance_name="neutral", beta="0.20", gate=None, afford=None, min_new="medium"):
        return exposure_rotation_plan(
            gate or gate_v2(), stance_name, None if beta is None else Decimal(beta), self.NAV, victims, fills, afford,
            min_new_conviction=min_new,
        )

    def test_the_target_is_both_the_trigger_and_the_stop_line(self):
        victims, fills = [victim("V", "low", "0.1")], [fill("F", "high", "2")]
        exposure_only = gate_v2(conviction_gap_swap=None)
        del exposure_only["conviction_gap_swap"]
        for beta in ("0.55", "0.90"):
            result = self.plan(victims, fills, beta=beta, gate=exposure_only)
            self.assertEqual((result["swaps"], result["skipped"]["exposure"]), ([], "beta_at_target"), beta)
        result = self.plan(victims, fills, beta="0.549999", gate=exposure_only)
        self.assertEqual([(s["victim"], s["fill"]) for s in result["swaps"] if s["kind"] == "exposure"], [("V", "F")])

    def test_there_is_no_release_margin_band(self):
        # v1 在 [下限 − margin, 下限) 觸發但零換股（beta_within_release_margin）；v2 沒有這條帶，低於目標就換。
        result = self.plan([victim("V", "low", "0.1")], [fill("F", "high", "2")], beta="0.50")
        self.assertEqual(len([s for s in result["swaps"] if s["kind"] == "exposure"]), 1)
        self.assertNotEqual(result["skipped"]["exposure"], "beta_within_release_margin")

    def test_defensive_or_null_target_does_not_trigger(self):
        victims, fills = [victim("V", "low", "0.1")], [fill("F", "high", "2")]
        defensive = self.plan(victims, fills, stance_name="defensive", gate=gate_v2(conviction_gap_swap=None))
        self.assertEqual(defensive["skipped"]["exposure"], "stance_not_triggered")
        off = gate_v2(beta_target_by_stance={"aggressive": None, "neutral": None, "defensive": None})
        self.assertEqual(self.plan(victims, fills, gate=off)["skipped"]["exposure"], "stance_not_triggered")
        self.assertEqual(self.plan([], [], beta=None)["skipped"]["exposure"], "beta_unavailable")

    def test_fill_beta_must_be_strictly_above_the_portfolio_beta(self):
        victims = [victim("V", "low", "0.10")]
        for beta in ("0.20", "0.19", None):
            result = self.plan(victims, [fill("F", "high", beta)], gate=gate_v2(conviction_gap_swap=None))
            self.assertEqual(result["swaps"], [], beta)
            self.assertEqual(result["skipped"]["exposure"], "no_eligible_fill", beta)
        ok = self.plan(victims, [fill("F", "high", "0.200001")])
        self.assertEqual(ok["swaps"][0]["fill"], "F")

    def test_fill_conviction_threshold_comes_from_the_caller(self):
        victims = [victim("V", "low", "0.10")]
        gate = gate_v2(conviction_gap_swap=None)
        low = self.plan(victims, [fill("F", "low", "2")], gate=gate, min_new="low")
        self.assertEqual(low["swaps"][0]["fill"], "F")
        medium = self.plan(victims, [fill("F", "low", "2")], gate=gate, min_new="medium")
        self.assertEqual((medium["swaps"], medium["skipped"]["exposure"]), ([], "no_eligible_fill"))
        high = self.plan(victims, [fill("F", "medium", "2")], gate=gate, min_new="high")
        self.assertEqual((high["swaps"], high["skipped"]["exposure"]), ([], "no_eligible_fill"))

    def test_missing_or_invalid_threshold_is_fail_closed(self):
        for bad in (None, "ultra"):
            with self.assertRaises(DecisionToolError):
                self.plan([victim("V", "low", "0.1")], [fill("F", "high", "2")], min_new=bad)

    def test_there_is_no_rotation_fraction_cap(self):
        # 每檔 60% NAV 的賣出市值：v1 的 0.20 上限會擋，v2 只受 max_swaps_per_run 限制。
        victims = [victim("V%d" % i, "low", "0.0%d" % i, weight="0.001", value="600000") for i in (1, 2, 3)]
        fills = [fill("F%d" % i, "high", "9") for i in (1, 2, 3)]
        gate = gate_v2(conviction_gap_swap=None)
        result = self.plan(victims, fills, gate=gate)
        self.assertEqual(len(result["swaps"]), 2)
        self.assertNotEqual(result["skipped"]["exposure"], "rotation_fraction")
        wide = self.plan(victims, fills, gate=dict(gate, max_swaps_per_run=3))
        self.assertEqual(len(wide["swaps"]), 3)

    def test_victim_order_cash_check_and_the_swap_cap_are_unchanged(self):
        victims = [victim("D", "medium", "0.05"), victim("C", "low", "0.15"), victim("B", "low", "0.10"),
                   victim("A", "low", "0.10"), victim("H", "high", "0.01")]
        fills = [fill("F1", "high", "2"), fill("F2", "high", "2"), fill("F3", "medium", "2")]
        result = self.plan(victims, fills, gate=gate_v2(max_swaps_per_run=3, conviction_gap_swap=None))
        self.assertEqual([s["victim"] for s in result["swaps"]], ["A", "B", "C"])
        none = self.plan(victims, fills, afford=lambda *_: False)
        self.assertEqual((none["swaps"], none["skipped"]["exposure"]), ([], "cash_infeasible"))

    def test_stops_when_the_projected_beta_reaches_the_target(self):
        # 目標 0.55，目前 0.20：第一對 Δ = 0.30 × (2 − 0.1) = 0.57，預估 0.77 ≥ 0.55，不再配第二對。
        victims = [victim("V1", "low", "0.10", weight="0.30"), victim("V2", "low", "0.11", weight="0.30")]
        fills = [fill("F1", "high", "2"), fill("F2", "high", "2")]
        result = self.plan(victims, fills, gate=gate_v2(conviction_gap_swap=None))
        self.assertEqual([(s["victim"], s["fill"]) for s in result["swaps"]], [("V1", "F1")])

    def test_gap_swap_shares_the_swap_cap_and_comes_after_exposure(self):
        both = self.plan([victim("V", "low", "0.01"), victim("W", "low", "0.02")],
                         [fill("F", "high", "2"), fill("G", "high", "2")], gate=gate_v2(max_swaps_per_run=1))
        self.assertEqual([s["kind"] for s in both["swaps"]], ["exposure"])
        self.assertEqual(both["skipped"]["gap"], "max_swaps")

    def test_equivalent_to_v1_when_the_v1_floor_minus_margin_equals_the_v2_target(self):
        # v1：下限 0.55、margin 0.10 → 觸發與停止線 0.45；換入者要 ≥ 組合 beta + 0.10。
        # v2：目標 0.45、換入者只要高於組合 beta。換入者 beta 都 ≥ 組合 beta + 0.10 時兩者結果必須相同。
        v1 = copy.deepcopy(GATE)
        v1["beta_floor_by_stance"] = {"aggressive": "0.90", "neutral": "0.55", "defensive": None}
        v2 = gate_v2(beta_target_by_stance={"aggressive": "0.80", "neutral": "0.45", "defensive": None})
        victims = [victim("V1", "low", "0.05", weight="0.10"), victim("V2", "medium", "0.08", weight="0.10"),
                   victim("V3", "low", "0.30", weight="0.10")]
        fills = [fill("F1", "high", "1.5"), fill("F2", "medium", "0.95"), fill("F3", "low", "3")]
        for beta in ("0.10", "0.20", "0.30", "0.44", "0.45", "0.60", None):
            for name in ("neutral", "aggressive", "defensive"):
                with self.subTest(beta=beta, stance=name):
                    old = exposure_rotation_plan(
                        v1, name, None if beta is None else Decimal(beta), self.NAV, victims, fills, lambda *_: True)
                    new = self.plan(victims, fills, stance_name=name, beta=beta, gate=v2, afford=lambda *_: True)
                    self.assertEqual(old["swaps"], new["swaps"])
                    # 原因代碼只允許「釋放帶」這一條不同（v2 沒有這條帶）。
                    folded = {key: ("beta_at_target" if value == "beta_within_release_margin" else value)
                              for key, value in old["skipped"].items()}
                    # v1 在 [0.45, 0.55) 零換股；v2 的目標是 0.45，同區間同樣零換股。
                    self.assertEqual(folded, new["skipped"])


class AllocationV2Tests(unittest.TestCase):
    def test_the_same_world_swaps_the_same_pair_as_v1(self):
        bundle, intent, policy, body = narrow_world(gate=gate_v2())
        self.assertEqual(summary(body), {("2330.TW", "sell"): (1000, "exit"), ("2317.TW", "buy"): (3000, "buy")})
        self.assertEqual(flag_map(body), {
            ("PORTFOLIO", "CONVICTION_GAP_SWAP_SKIPPED:no_eligible_fill"),
            ("2330.TW", "RISK_EXPOSURE_ROTATION"), ("2317.TW", "EXPOSURE_ROTATION_FILL"),
        })
        self.assertTrue(guard(bundle, policy, body)["passed"])
        self.assertEqual(next(i for i in intent["items"] if i["symbol"] == "2330.TW")["intent"], "hold")
        self.assertTrue(gate_is_v2(policy["position_sizing"]["exposure_rotation"]))

    def test_defensive_stance_does_not_trigger(self):
        # 1101 hold 信心 medium：沒有 low 的持股，信心差換股也不會動作，只剩曝險換股被姿態擋下。
        _, _, _, body = narrow_world("defensive", items=decisions(c1101="medium"), gate=gate_v2())
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertIn(("PORTFOLIO", "EXPOSURE_ROTATION_SKIPPED:stance_not_triggered"), flag_map(body))

    def test_gap_swap_is_no_longer_bounded_by_a_rotation_fraction(self):
        # 行為差異（計劃第 5.1 節的隱含前提）：v1 的 max_rotation_fraction 0.20 會擋掉 1101（約 50% NAV）的信心差換股；
        # v2 沒有這個上限，換出量只受 max_swaps_per_run 與單股上限（正式設定 10%）間接限制。
        _, _, _, v1 = narrow_world("defensive")
        self.assertEqual(v1["order_proposal"]["orders"], [])
        self.assertIn(("1101.TW", "RISK_CONVICTION_GAP_SWAP"), flag_map(narrow_world("defensive", gate=gate_v2())[3]))

    def test_target_below_the_portfolio_beta_does_not_swap(self):
        low = gate_v2(beta_target_by_stance={"aggressive": "0.30", "neutral": "0.25", "defensive": None})
        _, _, _, body = narrow_world(gate=low)
        self.assertIn(("PORTFOLIO", "EXPOSURE_ROTATION_SKIPPED:beta_at_target"), flag_map(body))
        self.assertEqual(codes(body, "RISK_EXPOSURE_ROTATION"), [])

    def test_new_buy_below_the_rotation_conviction_is_not_a_fill(self):
        # v2 沒有自己的 min_new_conviction：門檻是 rotation.min_new_conviction（測試世界為 medium）。
        _, _, _, body = narrow_world(items=decisions(fill="low"), gate=gate_v2())
        self.assertEqual(codes(body, "RISK_EXPOSURE_ROTATION"), [])
        self.assertEqual(codes(body, "EXPOSURE_ROTATION_FILL"), [])

    def test_no_rotation_fraction_cap_so_a_big_holding_can_be_rotated_out(self):
        # v1 的 max_rotation_fraction 0.10 低於 2330 的權重（約 16%）→ 不換；v2 沒有這個上限 → 換。
        _, _, _, v1 = narrow_world(gate=dict(copy.deepcopy(GATE), held_since_by_symbol=dict(HELD_SINCE), max_rotation_fraction="0.10"))
        self.assertEqual(v1["order_proposal"]["orders"], [])
        _, _, _, v2 = narrow_world(gate=gate_v2())
        self.assertIn(("2330.TW", "RISK_EXPOSURE_ROTATION"), flag_map(v2))

    def test_tampering_a_v2_gate_is_rejected(self):
        bundle, intent, policy, body = narrow_world(gate=gate_v2())
        for mutate in (
            lambda p: p["position_sizing"]["exposure_rotation"].update({"release_margin": "0.10"}),
            lambda p: p["position_sizing"]["exposure_rotation"].update({"max_rotation_fraction": "0.20"}),
            lambda p: p["position_sizing"]["exposure_rotation"].update({"min_new_conviction": "medium"}),
            lambda p: p["position_sizing"]["exposure_rotation"].update(
                {"beta_target_by_stance": {"aggressive": "0.10", "neutral": "0.10", "defensive": None}}),
            lambda p: p["position_sizing"]["exposure_rotation"]["held_since_by_symbol"].update({"2330.TW": "2026-09-18T06:00:00+00:00"}),
        ):
            altered = copy.deepcopy(policy)
            mutate(altered)
            altered["content_sha256"] = decision_policy_sha256(altered)
            errors = DecisionPolicyValidator(bundle).validate(altered)
            rebuilt = ProposalValidator(bundle, altered, intent).validate(body) if not errors else errors
            self.assertTrue(rebuilt)

    def test_missing_rotation_rule_is_fail_closed(self):
        # v2 的 Fill 信心門檻來自 rotation；Policy 沒有 rotation 時由前提檢查拒絕，不會用任何預設值。
        bundle = exposure_bundle()
        sizing = exposure_sizing(bundle, gate_v2())
        sizing.pop("rotation")
        policy = base_policy()
        policy["position_sizing"] = sizing
        errors = []
        validate_position_sizing(policy, ["1101.TW", "2317.TW", "2330.TW"], errors)
        self.assertTrue(any("exposure_rotation 必須與 rotation 同時存在" in error for error in errors), errors)

    def test_anchored_sizing_helper_is_untouched(self):
        # 對照組：anchored_sizing 沒有曝險換股閘門，行為與 v1／v2 無關。
        self.assertNotIn("exposure_rotation", anchored_sizing(exposure_bundle()))


if __name__ == "__main__":
    unittest.main()
