"""position_sizing.rebound_participation v1（反彈參與閘門）的測試。

覆蓋：

1. 程式狀態機 ``state_path``／``build_turn_state``：candidate 一日、confirmed 第 2～3 日、candidate 超過 3 日過期、遲滯（confirmed 後單日大跌不退，
   連 3 日跌且站回 MA5 比例低才退）、regime 不是 bear 恆 none、缺資料永不升級（歷史不足、覆蓋不足、bar 晚於最近交易日）、
   無記憶（截斷 bundle 重算與逐日路徑一致）。
2. 雙鑰匙 ``effective_state``：3×3 全組合、回撤下限 defensive 恆 none、缺列舉即拒絕；程式單獨不能升、Agent 單獨也不能升。
3. 對映 ``apply_rebound``：只放寬、不收緊；none 時閘門物件原樣。
4. 閘門形狀、前提閘門缺一拒絕、策略反思不得改、舊 Policy 不含此欄位。
5. 配置：防守姿態有效狀態 candidate／confirmed 時改用 0.55 停止線換股、減碼日略過、舊 Policy 逐位元不變。
6. team_inputs 2.8：缺 turn_state、多餘 turn_state、竄改 turn_state 都拒絕；風險 Agent 簡報附 turn_state（citable=false）；
   回放略過閘門。

合成行情（``series_bundle``）：10 檔，前 66 日各以不同的負報酬下跌（regime 為 bear，且只有少數個股在反彈後站上 MA20），
之後接指定的逐日池報酬。這是機制測試，不是對真實行情的驗證。
"""

import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.automation.agent_runner import DailyPipelineError
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.decision import (
    AllocationOrderEngine, DecisionToolError, MomentumEngine, apply_trade_decision, build_team_inputs, decision_policy_sha256,
)
from etf_agent.decision.finalization import (
    EXPOSURE_TEAM_VERSIONS, REBOUND_TEAM_FIELDS, REBOUND_TEAM_VERSION, TEAM_FIELDS_BY_VERSION,
)
from etf_agent.decision.rebound import (
    AGENT_STATE_BY_ASSESSMENT, TURN_STATE_FIELDS, TURN_STATE_UNAVAILABLE, TURN_STATES, apply_rebound, bind_effective_state,
    build_turn_state, effective_state, rebound_gate, state_path, validate_effective_state, validate_turn_state,
)
from etf_agent.decision.sizing import GATE_PREREQUISITES, POSITION_SIZING_FIELDS, validate_position_sizing
from etf_agent.decision.turn_facts import TURN_ASSESSMENTS, _Pool, turn_facts
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_exposure_rotation import decisions, exposure_bundle, exposure_sizing, flag_map, narrow_world
from test_exposure_rotation_e2e import allocate, codes as flag_codes
from test_exposure_rotation_v3 import gate_v3
from test_cash_stance_turn_facts import GATE as TURN_FACTS_GATE, overnight_bundle, stance as turn_stance
from test_daily_pipeline import FakeRunner
from test_portfolio_risk_decision import policy as base_policy
from test_typed_evidence import typed_reports

ROOT = Path(__file__).resolve().parents[2]
REBOUND = {
    "version": "v1",
    "candidate_return": "0.03", "candidate_ma5_share": "0.30",
    "confirm_return": "0", "confirm_ma5_share": "0.50", "confirm_new_low_share_max": "0.10",
    "candidate_expiry_days": 3,
    "retreat_return": "-0.04", "retreat_window_days": 3, "retreat_ma5_share": "0.30",
    "min_coverage": "0.80",
    "by_state": {
        "candidate": {"defensive_target": "0.55", "max_swaps_per_run": 2, "min_holding_days": 5},
        "confirmed": {"defensive_target": "0.55", "max_swaps_per_run": 4, "min_holding_days": 0},
    },
}


def rebound_gate_with(**changes):
    gate = copy.deepcopy(REBOUND)
    gate.update(changes)
    return gate


def dates(count):
    return ["2026-%02d-%02d" % (1 + index // 28, 1 + index % 28) for index in range(count)]


def series_bundle(moves, symbols=10, base=66, drift=Decimal("-0.006"), step=Decimal("-0.001"), typed=True):
    """前 ``base`` 日第 k 檔以 ``drift + step × k`` 的日報酬變動，之後全部股票同時依 ``moves`` 逐日變動。"""
    days = dates(base + len(moves))
    series = []
    for k in range(symbols):
        rate = drift + step * k
        close, bars = Decimal(100), []
        for index, day in enumerate(days):
            move = rate if index < base else Decimal(moves[index - base])
            close = (close * (1 + move)).quantize(Decimal("0.0001"))
            bars.append({"trade_date": day, "close": str(close)})
        series.append({"symbol": "S%d.TW" % k, "evidence_id": "p%d" % k, "bars": bars})
    bundle = {"snapshot": {"latest_trade_date": days[-1]}, "price_series": series}
    if typed:
        bundle["evidence_policy"] = "typed_evidence_v1"
    return bundle


def path_of(bundle, gate=REBOUND):
    return state_path(_Pool(bundle, bundle["snapshot"]["latest_trade_date"]), gate)


def last_state(moves, **kwargs):
    return build_turn_state(series_bundle(moves, **kwargs), REBOUND)


# ---------------------------------------------------------------- 1. 狀態機
class StateMachineTests(unittest.TestCase):
    def test_no_big_up_day_is_none(self):
        self.assertEqual(last_state([])["state"], "none")
        self.assertEqual(last_state(["0.01"])["state"], "none")  # 不到 candidate_return

    def test_one_big_up_day_makes_a_candidate_in_one_day(self):
        state = last_state(["0.04"])
        self.assertEqual((state["state"], state["regime"], state["days_since_candidate"]), ("candidate", "bear", 0))
        self.assertEqual(state["flags"], [])

    def test_second_day_confirms_when_the_cumulative_return_since_the_turn_is_not_negative(self):
        state = last_state(["0.04", "0.01"])
        self.assertEqual((state["state"], state["days_since_candidate"]), ("confirmed", 1))
        self.assertEqual(state["cumulative_return_since_candidate"], "0.010000")  # 不含轉向日當天的 +4%

    def test_a_negative_second_day_keeps_the_candidate_and_the_third_day_can_confirm(self):
        self.assertEqual(last_state(["0.04", "-0.005"])["state"], "candidate")
        third = last_state(["0.04", "-0.005", "0.01"])
        self.assertEqual((third["state"], third["days_since_candidate"]), ("confirmed", 2))

    def test_candidate_expires_on_the_fourth_day_without_confirmation(self):
        expected = ["candidate", "candidate", "candidate", "candidate", "none"]
        states = [row["state"] for row in path_of(series_bundle(["0.04", "-0.01", "-0.001", "-0.001"]))[-5:]]
        self.assertEqual(states, ["none", "candidate", "candidate", "candidate", "none"])
        self.assertEqual(expected[-1], states[-1])

    def test_a_single_down_day_does_not_retreat_from_confirmed(self):
        state = last_state(["0.04", "0.01", "-0.03"])
        self.assertEqual(state["state"], "confirmed")
        self.assertEqual(state["signals"]["pool_return_latest"], "-0.030000")

    def test_three_down_days_with_low_ma5_share_retreat_to_none(self):
        rows = path_of(series_bundle(["0.04", "0.01", "-0.02", "-0.02", "-0.02"]))[-5:]
        self.assertEqual([row["state"] for row in rows], ["candidate", "confirmed", "confirmed", "confirmed", "none"])
        self.assertLessEqual(rows[-1]["window_return"], Decimal("-0.04"))
        self.assertLess(rows[-1]["ma5"], Decimal("0.30"))

    def test_retreat_needs_both_conditions(self):
        # 3 日報酬夠差，但站回 MA5 比例不低（把門檻調到 0）→ 不退。
        gate = rebound_gate_with(retreat_ma5_share="0.0")
        rows = path_of(series_bundle(["0.04", "0.01", "-0.02", "-0.02", "-0.02"]), gate)
        self.assertEqual(rows[-1]["state"], "confirmed")
        # 站回 MA5 比例低，但 3 日報酬沒有那麼差（門檻 -20%）→ 不退。
        gate = rebound_gate_with(retreat_return="-0.2")
        rows = path_of(series_bundle(["0.04", "0.01", "-0.02", "-0.02", "-0.02"]), gate)
        self.assertEqual(rows[-1]["state"], "confirmed")

    def test_regime_that_is_not_bear_is_always_none(self):
        bull = series_bundle(["0.04", "0.01"], drift=Decimal("0.006"), step=Decimal("0.001"))
        rows = path_of(bull)
        self.assertTrue(all(row["state"] == "none" for row in rows))
        self.assertEqual(build_turn_state(bull, REBOUND)["regime"], "bull")
        # 全部股票同時站回均線 → regime 離開 bear → 已 candidate 的狀態也歸 none。
        flat = series_bundle(["0.04", "0.01"], drift=Decimal("-0.005"), step=Decimal("0"))
        self.assertEqual(path_of(flat)[-1]["state"], "none")
        self.assertEqual(path_of(flat)[-1]["regime"], "neutral")

    def test_short_history_never_upgrades(self):
        # 60 筆（第 61 筆才夠 MomentumEngine 的最少筆數）：regime 不可用 → none 並標旗標。
        short = series_bundle(["0.04"], base=59)
        state = build_turn_state(short, REBOUND)
        self.assertEqual(state["state"], "none")
        self.assertIn(TURN_STATE_UNAVAILABLE, state["flags"])
        enough = series_bundle(["0.04"], base=60)
        self.assertEqual(build_turn_state(enough, REBOUND)["state"], "candidate")

    def test_thin_coverage_never_upgrades(self):
        bundle = series_bundle(["0.04"])
        for row in bundle["price_series"][:3]:  # 3／10 檔缺最後一天：覆蓋 70% < 80%
            row["bars"].pop()
        state = build_turn_state(bundle, REBOUND)
        self.assertEqual(state["state"], "none")
        self.assertIn(TURN_STATE_UNAVAILABLE, state["flags"])
        self.assertEqual(state["signals"]["coverage"], "0.700000")

    def test_a_bar_later_than_the_latest_trade_date_is_none_with_a_flag_not_an_exception(self):
        bundle = series_bundle(["0.04"])
        bundle["snapshot"]["latest_trade_date"] = dates(67)[-2]
        state = build_turn_state(bundle, REBOUND)
        self.assertEqual((state["state"], state["regime"]), ("none", None))
        self.assertIn(TURN_STATE_UNAVAILABLE, state["flags"])
        with self.assertRaises(DecisionToolError):  # 上游的轉向事實仍是 fail-closed 拒絕
            turn_facts(bundle)

    def test_the_last_bar_must_be_the_latest_trade_date(self):
        bundle = series_bundle(["0.04"])
        bundle["snapshot"]["latest_trade_date"] = "2027-01-01"
        self.assertIn(TURN_STATE_UNAVAILABLE, build_turn_state(bundle, REBOUND)["flags"])

    def test_untyped_bundle_is_rejected(self):
        with self.assertRaisesRegex(DecisionToolError, "typed_evidence_v1"):
            build_turn_state(series_bundle(["0.04"], typed=False), REBOUND)

    def test_recomputing_on_a_truncated_bundle_matches_the_path_so_nothing_is_stored_across_runs(self):
        moves = ["0.04", "-0.005", "0.01", "-0.03", "-0.02", "-0.02", "0.04"]
        full = series_bundle(moves)
        rows = path_of(full)
        for cut in range(1, len(moves) + 1):
            truncated = series_bundle(moves[:cut])
            self.assertEqual(build_turn_state(truncated, REBOUND)["state"], rows[66 + cut - 1]["state"], cut)

    def test_output_shape_is_exactly_the_whitelist_and_is_deterministic(self):
        bundle = series_bundle(["0.04", "0.01"])
        first, second = build_turn_state(bundle, REBOUND), build_turn_state(bundle, REBOUND)
        self.assertEqual(first, second)
        self.assertEqual(set(first), TURN_STATE_FIELDS)
        self.assertEqual(len(first["recent_path"]), 5)
        json.dumps(first)  # 全部可序列化（字串與整數，沒有 Decimal）


# ---------------------------------------------------------------- 2. 雙鑰匙
class DualKeyTests(unittest.TestCase):
    def test_every_combination_takes_the_more_conservative_key(self):
        order = TURN_STATES.index
        for program in TURN_STATES:
            for assessment in TURN_ASSESSMENTS:
                with self.subTest(program=program, assessment=assessment):
                    got = effective_state({"state": program}, {"turn_assessment": assessment}, None)
                    agent = AGENT_STATE_BY_ASSESSMENT[assessment]
                    self.assertEqual(got, min(program, agent, key=order))
                    self.assertLessEqual(order(got), order(program))  # Agent 單獨不能升
                    self.assertLessEqual(order(got), order(agent))  # 程式單獨不能升

    def test_the_agent_enum_names_are_the_existing_ones(self):
        self.assertEqual(set(AGENT_STATE_BY_ASSESSMENT), set(TURN_ASSESSMENTS))
        self.assertEqual(AGENT_STATE_BY_ASSESSMENT, {
            "no_turn_evidence": "none", "turn_unconfirmed": "candidate", "turn_confirmed": "confirmed"})

    def test_a_defensive_stance_floor_always_wins(self):
        for floor, expected in (("defensive", "none"), ("neutral", "confirmed"), ("aggressive", "confirmed"), (None, "confirmed")):
            self.assertEqual(effective_state({"state": "confirmed"}, {"turn_assessment": "turn_confirmed"}, floor), expected, floor)

    def test_missing_or_invalid_keys_fail_closed(self):
        with self.assertRaises(DecisionToolError):
            effective_state({"state": "confirmed"}, {}, None)
        with self.assertRaises(DecisionToolError):
            effective_state({"state": "confirmed"}, {"turn_assessment": "bounce"}, None)
        with self.assertRaises(DecisionToolError):
            effective_state({"state": "strong"}, {"turn_assessment": "turn_confirmed"}, None)

    def test_binding_uses_the_program_state_from_the_bundle(self):
        sizing = {"rebound_participation": copy.deepcopy(REBOUND)}
        stance = {"turn_assessment": "turn_confirmed"}
        confirmed = bind_effective_state(sizing, series_bundle(["0.04", "0.01"]), stance, None)
        self.assertEqual(confirmed["effective_state"], "confirmed")
        self.assertEqual(bind_effective_state(sizing, series_bundle([]), stance, None)["effective_state"], "none")
        agent_none = bind_effective_state(sizing, series_bundle(["0.04", "0.01"]), {"turn_assessment": "no_turn_evidence"}, None)
        self.assertEqual(agent_none["effective_state"], "none")
        self.assertEqual(bind_effective_state(sizing, series_bundle(["0.04", "0.01"]), stance, "defensive")["effective_state"], "none")
        self.assertNotIn("effective_state", sizing["rebound_participation"])  # 樣板不被改動

    def test_a_prefilled_effective_state_is_rejected(self):
        sizing = {"rebound_participation": dict(copy.deepcopy(REBOUND), effective_state="confirmed")}
        with self.assertRaisesRegex(DecisionToolError, "只能由程式綁定"):
            bind_effective_state(sizing, series_bundle(["0.04", "0.01"]), {"turn_assessment": "turn_confirmed"}, None)

    def test_apply_trade_decision_binds_the_state_and_agent_alone_cannot_raise_it(self):
        from test_position_sizing import SIZING

        bundle = overnight_bundle()  # 歷史不足以算 regime → 程式狀態 none
        base = base_policy()
        base["position_sizing"] = {**copy.deepcopy(SIZING), "cash_stance_turn_facts": dict(TURN_FACTS_GATE),
                                   "rebound_participation": copy.deepcopy(REBOUND)}
        decision = {"decision_id": "trade-1", "content_sha256": "x" * 64, "items": []}
        bound = apply_trade_decision(base, decision, turn_stance("turn_confirmed"), bundle)
        self.assertEqual(bound["position_sizing"]["rebound_participation"]["effective_state"], "none")
        self.assertNotIn("effective_state", base["position_sizing"]["rebound_participation"])


# ---------------------------------------------------------------- 3. 對映
class MappingTests(unittest.TestCase):
    def exposure(self, **changes):
        gate = gate_v3(defensive=None, **changes)
        return gate

    def sizing(self, state, **changes):
        gate = rebound_gate_with(**changes)
        if state is not None:
            gate["effective_state"] = state
        return {"rebound_participation": gate}

    def test_none_returns_the_same_gate_object(self):
        exposure = self.exposure()
        for sizing in ({}, self.sizing("none")):
            effective, state = apply_rebound(exposure, sizing)
            self.assertIs(effective, exposure)
            self.assertEqual(state, "none")

    def test_candidate_and_confirmed_only_loosen(self):
        exposure = self.exposure(max_swaps_per_run=2, min_holding_days=5)
        effective, state = apply_rebound(exposure, self.sizing("candidate"))
        self.assertEqual(state, "candidate")
        self.assertEqual((effective["max_swaps_per_run"], effective["min_holding_days"]), (2, 5))  # 小動：只放寬停止線
        self.assertEqual(effective["beta_target_by_stance"], {"aggressive": "0.90", "neutral": "0.55", "defensive": "0.55"})
        effective, _ = apply_rebound(exposure, self.sizing("confirmed"))
        self.assertEqual((effective["max_swaps_per_run"], effective["min_holding_days"]), (4, 0))
        # 原閘門物件不被改動。
        self.assertEqual((exposure["max_swaps_per_run"], exposure["min_holding_days"]), (2, 5))
        self.assertIsNone(exposure["beta_target_by_stance"]["defensive"])

    def test_it_never_tightens_the_current_settings(self):
        exposure = self.exposure(max_swaps_per_run=5, min_holding_days=1,
                                 beta_target_by_stance={"aggressive": "0.90", "neutral": "0.70", "defensive": "0.60"})
        effective, _ = apply_rebound(exposure, self.sizing("confirmed"))
        self.assertEqual((effective["max_swaps_per_run"], effective["min_holding_days"]), (5, 0))
        self.assertEqual(effective["beta_target_by_stance"]["defensive"], "0.60")  # 0.55 < 0.60：取較大
        self.assertEqual(effective["beta_target_by_stance"]["neutral"], "0.70")

    def test_the_optional_neutral_target_only_applies_when_configured_and_larger(self):
        by_state = copy.deepcopy(REBOUND["by_state"])
        by_state["confirmed"]["neutral_target"] = "0.75"
        effective, _ = apply_rebound(self.exposure(), self.sizing("confirmed", by_state=by_state))
        self.assertEqual(effective["beta_target_by_stance"]["neutral"], "0.75")
        effective, _ = apply_rebound(self.exposure(), self.sizing("candidate", by_state=by_state))
        self.assertEqual(effective["beta_target_by_stance"]["neutral"], "0.55")

    def test_an_unbound_gate_is_rejected(self):
        with self.assertRaisesRegex(DecisionToolError, "尚未綁定 effective_state"):
            apply_rebound(self.exposure(), self.sizing(None))


# ---------------------------------------------------------------- 4. 閘門形狀與前提
def full_sizing(**gate_changes):
    sizing = exposure_sizing(exposure_bundle(), gate_v3(defensive=None))
    sizing["cash_stance_turn_facts"] = dict(TURN_FACTS_GATE)
    sizing["rebound_participation"] = rebound_gate_with(**gate_changes)
    return sizing


def sizing_errors(sizing):
    policy = base_policy()
    policy["position_sizing"] = sizing
    errors = []
    validate_position_sizing(policy, ["1101.TW", "2317.TW", "2330.TW"], errors)
    return errors


class GateShapeTests(unittest.TestCase):
    def test_valid_gate_passes_and_is_a_declared_human_only_gate(self):
        self.assertEqual(sizing_errors(full_sizing()), [])
        self.assertIn("rebound_participation", POSITION_SIZING_FIELDS)
        rule = next(rule for rule in GATE_PREREQUISITES if rule.field == "rebound_participation")
        self.assertTrue(rule.human_only)
        self.assertTrue(any(prefix == "rebound_participation" for prefix in FORBIDDEN_PREFIXES))

    def test_missing_unknown_and_malformed_fields_are_rejected(self):
        for field in sorted(set(REBOUND) - {"version"}):
            gate = copy.deepcopy(REBOUND)
            del gate[field]
            sizing = full_sizing()
            sizing["rebound_participation"] = gate
            self.assertTrue(any("缺少必要欄位" in e and field in e for e in sizing_errors(sizing)), field)
        cases = {
            "含未允許欄位": {"threshold": "1"},
            "version 只允許": {"version": "v3"},
            "candidate_return": {"candidate_return": 0.03},
            "retreat_return": {"retreat_return": "0.04"},
            "candidate_expiry_days": {"candidate_expiry_days": 1},
            "min_coverage": {"min_coverage": "0.2"},
            "剛好包含 candidate／confirmed": {"by_state": {"candidate": REBOUND["by_state"]["candidate"]}},
            "effective_state": {"effective_state": "strong"},
        }
        for message, change in cases.items():
            with self.subTest(message=message):
                self.assertTrue(any(message in e for e in sizing_errors(full_sizing(**change))), message)

    def test_by_state_rules(self):
        def with_state(**row_changes):
            by_state = copy.deepcopy(REBOUND["by_state"])
            for state, change in row_changes.items():
                by_state[state].update(change)
            return full_sizing(by_state=by_state)

        too_many = sizing_errors(with_state(confirmed={"max_swaps_per_run": 9}))
        self.assertTrue(any("不得高於 rotation.max_swaps_per_run" in e for e in too_many), too_many)
        self.assertTrue(any("confirmed.max_swaps_per_run ≥ candidate" in e for e in
                            sizing_errors(with_state(candidate={"max_swaps_per_run": 4}, confirmed={"max_swaps_per_run": 3}))))
        self.assertTrue(any("confirmed.min_holding_days ≤ candidate" in e for e in
                            sizing_errors(with_state(candidate={"min_holding_days": 0}, confirmed={"min_holding_days": 3}))))
        self.assertTrue(any("confirmed.defensive_target ≥ candidate" in e for e in
                            sizing_errors(with_state(candidate={"defensive_target": "0.60"}))))
        self.assertTrue(any("neutral_target 不得低於 defensive_target" in e for e in
                            sizing_errors(with_state(confirmed={"neutral_target": "0.40"}))))
        self.assertTrue(any("不得高於 exposure_rotation 的進攻目標" in e for e in
                            sizing_errors(with_state(confirmed={"defensive_target": "1.20"}))))
        self.assertTrue(any("含未允許欄位" in e for e in sizing_errors(with_state(candidate={"neutral_target": "0.60"}))))
        self.assertEqual(sizing_errors(with_state(confirmed={"neutral_target": "0.75"})), [])

    def test_every_prerequisite_gate_is_required(self):
        broken = full_sizing()
        broken["exposure_rotation"]["version"] = "v2"
        broken["exposure_rotation"]["beta_target_by_stance"]["defensive"] = None
        self.assertTrue(any("exposure_rotation.version=v3" in e for e in sizing_errors(broken)))
        for field, message in (("exposure_rotation", "同時存在"), ("cash_stance_turn_facts", "cash_stance_turn_facts"),
                               ("drawdown_derisk", "drawdown_derisk")):
            sizing = full_sizing()
            del sizing[field]
            errors = sizing_errors(sizing)
            self.assertTrue(any("rebound_participation" in e and message in e for e in errors), (field, errors))
        for version in ("v1", "v2", "v3"):
            sizing = full_sizing()
            sizing["drawdown_derisk"] = {"version": version, "max_derisk_fraction": "0.25"}
            self.assertTrue(any("drawdown_derisk.version=v4" in e for e in sizing_errors(sizing)), version)

    def test_a_policy_without_the_field_validates_exactly_as_before(self):
        sizing = full_sizing()
        del sizing["rebound_participation"]
        self.assertEqual(sizing_errors(sizing), [])
        self.assertIsNone(rebound_gate(sizing))
        self.assertIsNone(rebound_gate({}))
        self.assertIsNone(rebound_gate(None))


# ---------------------------------------------------------------- 5. 配置
class AllocationTests(unittest.TestCase):
    """防守姿態的世界：組合 beta 約 0.28，exposure_rotation v3 的防守目標為 null（現況：不觸發）。"""

    def world(self, state, level="defensive", exposure=None, rebound=None):
        bundle, intent, policy, body = narrow_world(level, items=decisions(c1101="medium"), gate=exposure or gate_v3(defensive=None))
        if state is None:
            return bundle, intent, policy, body
        return (bundle, intent) + self.rebind(bundle, intent, policy, state, rebound)

    def rebind(self, bundle, intent, policy, state, rebound=None):
        bound = copy.deepcopy(policy)
        bound["position_sizing"]["cash_stance_turn_facts"] = dict(TURN_FACTS_GATE)
        bound["position_sizing"]["rebound_participation"] = dict(rebound or REBOUND, effective_state=state)
        bound["content_sha256"] = decision_policy_sha256(bound)
        return bound, AllocationOrderEngine(bundle, bound).run(intent)

    def test_none_and_the_missing_gate_behave_exactly_like_before(self):
        bundle, intent, base_policy_, base_body = self.world(None)
        _, none_body = self.rebind(bundle, intent, base_policy_, "none")
        for key in ("allocation_proposal", "order_proposal", "constraint_flags"):
            self.assertEqual(none_body[key], base_body[key], key)
        self.assertEqual(base_body["order_proposal"]["orders"], [])
        self.assertIn(("PORTFOLIO", "EXPOSURE_ROTATION_SKIPPED:stance_not_triggered"), flag_map(none_body))
        self.assertFalse([code for _, code in flag_map(none_body) if code.startswith("REBOUND_PARTICIPATION")])

    def test_candidate_and_confirmed_make_the_defensive_stance_swap_without_rewriting_intent(self):
        bundle, intent, base_policy_, _ = self.world(None)
        for state in ("candidate", "confirmed"):
            with self.subTest(state=state):
                _, body = self.rebind(bundle, intent, base_policy_, state)
                found = flag_map(body)
                self.assertIn(("2330.TW", "RISK_EXPOSURE_ROTATION"), found)
                self.assertIn(("2317.TW", "EXPOSURE_ROTATION_FILL"), found)
                self.assertIn(("PORTFOLIO", "REBOUND_PARTICIPATION:%s" % state), found)
                sides = {(o["symbol"], o["side"], o["intent"]) for o in body["order_proposal"]["orders"]}
                self.assertEqual(sides, {("2330.TW", "sell", "exit"), ("2317.TW", "buy", "buy")})  # 不只賣不買、賣單標 exit
                self.assertEqual(next(i for i in intent["items"] if i["symbol"] == "2330.TW")["intent"], "hold")

    def test_min_holding_days_is_relaxed_for_the_candidate_state_but_not_below_what_the_state_allows(self):
        exposure = gate_v3(defensive=None, min_holding_days=60)
        bundle, intent, policy, body = self.world(None, exposure=exposure)
        self.assertEqual(body["order_proposal"]["orders"], [])
        _, candidate = self.rebind(bundle, intent, policy, "candidate")  # min(60, 5) = 5：持有天數夠 → 可換
        self.assertIn(("2330.TW", "RISK_EXPOSURE_ROTATION"), flag_map(candidate))
        tight = copy.deepcopy(REBOUND)
        tight["by_state"]["candidate"]["min_holding_days"] = 59
        tight["by_state"]["confirmed"]["min_holding_days"] = 59
        _, blocked = self.rebind(bundle, intent, policy, "candidate", tight)  # 持有天數仍不夠 → 不換
        self.assertEqual(blocked["order_proposal"]["orders"], [])

    def test_a_drawdown_derisk_day_skips_everything_including_the_rebound_flag(self):
        bundle = exposure_bundle(cash="136000", positions={"2330.TW": 1000, "1101.TW": 7000})
        intent, policy, body = allocate(bundle, level="defensive", grade="lower", gate=gate_v3(defensive=None))
        _, bound = self.rebind(bundle, intent, policy, "confirmed")
        found = flag_codes(bound)
        self.assertIn("RISK_DRAWDOWN", found)
        self.assertIn("EXPOSURE_ROTATION_SKIPPED:derisk_day", found)
        self.assertEqual({code for code in found if code.startswith("REBOUND_PARTICIPATION")} | (found & {"RISK_EXPOSURE_ROTATION"}), set())

    def test_an_unbound_state_stops_the_allocation(self):
        bundle, intent, policy, _ = self.world(None)
        unbound = copy.deepcopy(policy)
        unbound["position_sizing"]["cash_stance_turn_facts"] = dict(TURN_FACTS_GATE)
        unbound["position_sizing"]["rebound_participation"] = copy.deepcopy(REBOUND)
        unbound["content_sha256"] = decision_policy_sha256(unbound)
        with self.assertRaisesRegex(DecisionToolError, "尚未綁定 effective_state"):
            AllocationOrderEngine(bundle, unbound).run(intent)

    def test_neutral_and_aggressive_stances_are_not_changed_by_a_state_with_only_a_defensive_target(self):
        bundle, intent, base_policy_, _ = self.world(None, level="neutral")
        _, base = self.rebind(bundle, intent, base_policy_, "none")
        _, bound = self.rebind(bundle, intent, base_policy_, "candidate")
        self.assertEqual(base["order_proposal"]["orders"], bound["order_proposal"]["orders"])


# ---------------------------------------------------------------- 6. team_inputs 2.8、簡報與回放
class TeamInputsTests(unittest.TestCase):
    def test_version_table_and_builder(self):
        self.assertEqual(TEAM_FIELDS_BY_VERSION[REBOUND_TEAM_VERSION], REBOUND_TEAM_FIELDS)
        self.assertEqual(TEAM_FIELDS_BY_VERSION["2.7"] | {"turn_state"}, REBOUND_TEAM_FIELDS)
        self.assertEqual(EXPOSURE_TEAM_VERSIONS, ("2.7", "2.8", "2.9", "2.10"))  # 2.9（換股提名）、2.10（優質股裁決）是 2.8 的超集，同樣含曝險檢視
        self.assertEqual(REBOUND_TEAM_VERSION, "2.8")

    def test_builder_requires_the_exposure_review_and_keeps_older_versions_unchanged(self):
        from test_team_chain import team_world

        _, _, _, _, _, _, reports, research = team_world()
        from test_trader import STANCE as TEAM_STANCE
        with self.assertRaisesRegex(DecisionToolError, "必須與曝險檢視"):
            build_team_inputs(reports, research, dict(TEAM_STANCE), turn_state={"state": "none"})
        plain = build_team_inputs(reports, research, dict(TEAM_STANCE))
        self.assertNotIn("turn_state", plain)
        self.assertNotEqual(plain["schema_version"], REBOUND_TEAM_VERSION)

    def chain_errors(self, policy, team):
        """Finalizer 的團隊鏈重建驗證（只看它拒絕什麼；整份配置重建需要完整前提，這裡不跑）。"""
        from etf_agent.decision import DecisionFinalizer
        from test_team_chain import team_world

        bundle, momentum, debate, trade, base, _, _, _ = team_world()
        finalizer = DecisionFinalizer(bundle, policy(base), momentum, debate, trade, team)
        return finalizer._validate_team_chain()

    def test_finalizer_requires_turn_state_when_the_policy_has_the_gate_and_rejects_it_without(self):
        from test_team_chain import team_world

        _, _, _, _, _, team, _, _ = team_world()

        def gated(base):
            value = copy.deepcopy(base)
            value["position_sizing"]["rebound_participation"] = dict(copy.deepcopy(REBOUND), effective_state="none")
            value["content_sha256"] = decision_policy_sha256(value)
            return value

        token = "rebound_participation 必須與 team_inputs 2.8 同時存在"
        self.assertTrue(any(token in e for e in self.chain_errors(gated, team)))
        sneaked = copy.deepcopy(team)
        sneaked.update(schema_version=REBOUND_TEAM_VERSION, turn_state={"state": "none"})
        self.assertTrue(any("team_inputs 欄位必須剛好是" in e for e in self.chain_errors(lambda base: base, sneaked)))  # 2.8 欄位不全即拒絕
        self.assertFalse(any(token in e for e in self.chain_errors(lambda base: base, team)))  # 沒有閘門的舊 run 不受影響

    def test_validate_turn_state_rejects_missing_tampered_and_extra_fields(self):
        bundle = series_bundle(["0.04", "0.01"])
        policy = {"position_sizing": {"rebound_participation": copy.deepcopy(REBOUND)}}
        good = build_turn_state(bundle, REBOUND)
        errors = []
        validate_turn_state(bundle, policy, good, errors)
        self.assertEqual(errors, [])
        for label, value in (
            ("缺漏", None),
            ("升級", dict(good, state="confirmed" if good["state"] != "confirmed" else "none")),
            ("數字改寫", dict(good, signals=dict(good["signals"], pool_return_latest="0.090000"))),
            ("多餘欄位", dict(good, advice="買進")),
            ("少欄位", {k: v for k, v in good.items() if k != "flags"}),
        ):
            with self.subTest(label):
                errors = []
                validate_turn_state(bundle, policy, value, errors)
                self.assertTrue(errors, label)
        errors = []
        validate_turn_state(bundle, {"position_sizing": {}}, good, errors)
        self.assertTrue(errors)  # 沒有閘門的 Policy 不得有 turn_state


class FinalizerBindingTests(unittest.TestCase):
    """Finalizer 重算 Policy 綁定的有效狀態：必須等於 min(team_inputs.turn_state, 風險 Agent 列舉)（整鏈夾具沒有 typed 版本，直接測這個檢查）。"""

    def check(self, bound, state, assessment, floor="aggressive"):
        policy = {"position_sizing": {"rebound_participation": dict(copy.deepcopy(REBOUND), effective_state=bound)},
                  "drawdown_control": {"stance_floor": floor}}
        team = {"turn_state": {"state": state}, "cash_stance": {"turn_assessment": assessment}}
        errors = []
        validate_effective_state(policy, team, errors)
        return errors

    def test_the_bound_state_must_equal_the_recomputed_minimum(self):
        self.assertEqual(self.check("candidate", "confirmed", "turn_unconfirmed"), [])
        self.assertEqual(self.check("candidate", "candidate", "turn_confirmed"), [])
        self.assertEqual(self.check("none", "confirmed", "no_turn_evidence"), [])
        self.assertEqual(self.check("none", "confirmed", "turn_confirmed", floor="defensive"), [])
        for bound, state, assessment in (("confirmed", "candidate", "turn_confirmed"), ("confirmed", "confirmed", "turn_unconfirmed"),
                                         ("candidate", "none", "turn_confirmed"), ("none", "confirmed", "turn_confirmed")):
            with self.subTest(bound=bound, state=state, assessment=assessment):
                self.assertTrue(any("重算結果不一致" in e for e in self.check(bound, state, assessment)))

    def test_missing_enum_or_state_is_rejected_and_no_gate_is_a_no_op(self):
        self.assertTrue(self.check("none", "none", None))
        errors = []
        validate_effective_state({"position_sizing": {}}, {}, errors)
        self.assertEqual(errors, [])
        errors = []
        validate_effective_state({"position_sizing": {"rebound_participation": dict(REBOUND, effective_state="none")}}, {}, errors)
        self.assertTrue(errors)


class BriefAndReplayTests(unittest.TestCase):
    def test_risk_brief_carries_turn_state_as_non_citable_background_only_when_given(self):
        bundle = overnight_bundle()
        momentum = MomentumEngine(bundle).run()
        reports = typed_reports(bundle)
        state = build_turn_state(bundle, REBOUND)
        for given in (state, None):
            runner = FakeRunner({"cash_stance": [turn_stance()]})
            with tempfile.TemporaryDirectory() as directory:
                pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
                pipeline.run_cash_stance(bundle, momentum, reports, cash_stance_turn_facts=TURN_FACTS_GATE, turn_state=given)
                brief = json.loads((Path(directory) / "brief_cash_stance.json").read_text(encoding="utf-8"))
            if given is None:
                self.assertNotIn("turn_state", brief)
                self.assertNotIn("turn_state", runner.prompts[0][1])
            else:
                self.assertEqual(brief["turn_state"], {"citable": False, **state})
                self.assertNotIn("evidence_id", brief["turn_state"])
                self.assertIn("turn_state", runner.prompts[0][1])

    def test_replay_ignores_the_gate_and_adds_one_note(self):
        from etf_agent.evaluation.replay import bind_replay_policy
        from test_cash_ratio_range import STANCE as LEGACY_STANCE, chain_inputs
        from test_daily_pipeline import live_without_offense, without_stop_exit
        from test_exposure_rotation import REVIEW_POLICY

        bundle, _, debate, intent, archived, _, _, _ = chain_inputs()
        template = without_stop_exit(live_without_offense())  # stop_exit／position_review v2 依賴 exposure_rotation，對照組先降回 v1
        template.update(policy_id="replay-A", available_at="2026-09-19T00:00:00+00:00")
        template["position_sizing"].pop("agent_swap", None)  # 依賴另兩個閘門，要先拿掉
        template["position_sizing"].pop("exposure_view", None)  # 依賴 exposure_rotation，同上
        template["position_sizing"].pop("cash_stance_brief", None)  # 前提閘門被拿掉時，依賴它們的簡報閘門一併拿掉
        template["position_sizing"].pop("rebound_participation", None)
        template["position_sizing"].pop("exposure_rotation", None)
        _, plain_notes = bind_replay_policy(template, bundle, archived, intent, debate, dict(LEGACY_STANCE), [])
        self.assertNotIn("rebound_participation", plain_notes)
        gated = copy.deepcopy(template)
        gated["position_sizing"]["exposure_rotation"] = gate_v3(defensive=None)
        gated["position_sizing"]["rebound_participation"] = copy.deepcopy(REBOUND)
        gated.setdefault("position_review", copy.deepcopy(REVIEW_POLICY))
        policy, notes = bind_replay_policy(gated, bundle, archived, intent, debate, dict(LEGACY_STANCE), [])
        self.assertEqual(notes["rebound_participation"], "ignored_in_replay")
        self.assertEqual(notes["exposure_rotation"], "ignored_in_replay")
        self.assertNotIn("rebound_participation", policy["position_sizing"])


# ---------------------------------------------------------------- 7. 條件式現金放寬與 confirmed 換手上限
def with_loosening(cash="0.09", confirmed_cash=None, turnover=None):
    gate = copy.deepcopy(REBOUND)
    gate["by_state"]["candidate"]["defensive_cash_buffer"] = cash
    gate["by_state"]["confirmed"]["defensive_cash_buffer"] = confirmed_cash or cash
    if turnover is not None:
        gate["by_state"]["confirmed"]["max_turnover_rate"] = turnover
    return gate


class CashLooseningTests(unittest.TestCase):
    """兩把鑰匙都同意轉向且姿態為 defensive 時，現金比例只放寬到 by_state 的目標；其餘一律不動。"""

    def bound(self, state, **kwargs):
        return dict(with_loosening(**kwargs), effective_state=state)

    def test_only_defensive_with_a_candidate_or_confirmed_state_is_loosened_and_only_downward(self):
        from etf_agent.decision.rebound import loosened_cash_buffer

        for state in ("candidate", "confirmed"):
            self.assertEqual(loosened_cash_buffer(self.bound(state), "defensive", "0.18"),
                             ("0.09", {"from": "0.18", "to": "0.09"}))
        for level in ("neutral", "aggressive"):
            self.assertEqual(loosened_cash_buffer(self.bound("confirmed"), level, "0.10"), ("0.10", None))
        self.assertEqual(loosened_cash_buffer(self.bound("none"), "defensive", "0.18"), ("0.18", None))
        self.assertEqual(loosened_cash_buffer(None, "defensive", "0.18"), ("0.18", None))
        # 原比例已不高於目標就不動（永不收緊）。
        self.assertEqual(loosened_cash_buffer(self.bound("candidate"), "defensive", "0.08"), ("0.08", None))
        self.assertEqual(loosened_cash_buffer(self.bound("candidate"), "defensive", "0.09"), ("0.09", None))
        # 沒填目標的狀態不放寬。
        self.assertEqual(loosened_cash_buffer(dict(REBOUND, effective_state="confirmed"), "defensive", "0.18"), ("0.18", None))

    def test_field_rules(self):
        def errors_for(**kwargs):
            return sizing_errors(full_sizing(by_state=with_loosening(**kwargs)["by_state"]))

        self.assertEqual(errors_for(), [])
        self.assertEqual(errors_for(cash="0.09", confirmed_cash="0.08"), [])
        self.assertTrue(any("confirmed.defensive_cash_buffer ≤ candidate" in e for e in errors_for(cash="0.08", confirmed_cash="0.09")))
        self.assertTrue(any("不得低於中性姿態最低現金比例" in e for e in errors_for(cash="0.04")), errors_for(cash="0.04"))
        self.assertTrue(any("defensive_cash_buffer" in e for e in errors_for(cash="0.3")))
        bad = with_loosening()["by_state"]
        bad["candidate"]["max_turnover_rate"] = "0.4"  # 換手上限只有 confirmed 可填
        self.assertTrue(any("max_turnover_rate" in e for e in sizing_errors(full_sizing(by_state=bad))))
        self.assertTrue(any("max_turnover_rate" in e for e in errors_turnover("0.9")))
        self.assertTrue(any("cash_loosening" in e for e in sizing_errors(full_sizing(cash_loosening={"from": "0.18"}))))

    def test_allocation_flags_the_loosening_and_the_policy_buffer_must_match_the_recomputation(self):
        bundle, intent, policy, _ = AllocationTests().world(None)
        bound = copy.deepcopy(policy)
        bound["position_sizing"]["cash_stance_turn_facts"] = dict(TURN_FACTS_GATE)
        base_buffer = bound["cash_buffer_rate"]
        gate = dict(with_loosening(), effective_state="candidate", cash_loosening={"from": base_buffer, "to": "0.09"})
        bound["position_sizing"]["rebound_participation"] = gate
        bound["cash_buffer_rate"] = "0.09"
        bound["content_sha256"] = decision_policy_sha256(bound)
        body = AllocationOrderEngine(bundle, bound).run(intent)
        self.assertIn(("PORTFOLIO", "REBOUND_CASH_LOOSENED:candidate"), flag_map(body))
        self.assertEqual(Decimal(body["effective_policy"]["cash_buffer_rate"]), Decimal("0.09"))
        # 沒放寬（none）：不得留旗標。
        none_gate = dict(with_loosening(), effective_state="none")
        none_policy = copy.deepcopy(bound)
        none_policy["cash_buffer_rate"] = base_buffer
        none_policy["position_sizing"]["rebound_participation"] = none_gate
        none_policy["content_sha256"] = decision_policy_sha256(none_policy)
        self.assertFalse([c for _, c in flag_map(AllocationOrderEngine(bundle, none_policy).run(intent)) if c.startswith("REBOUND_CASH")])

    def test_apply_trade_decision_loosens_only_when_both_keys_agree_and_the_floor_is_not_defensive(self):
        from unittest import mock
        from test_position_sizing import SIZING

        base = base_policy()
        base["position_sizing"] = {**copy.deepcopy(SIZING), "cash_stance_turn_facts": dict(TURN_FACTS_GATE),
                                   "rebound_participation": with_loosening()}
        bundle = overnight_bundle()
        decision = {"decision_id": "trade-1", "content_sha256": "x" * 64, "items": []}
        stance = turn_stance("turn_confirmed", level="defensive")
        with mock.patch("etf_agent.decision.rebound.build_turn_state", return_value={"state": "confirmed"}):
            result = apply_trade_decision(base, decision, stance, bundle)
            self.assertEqual(result["cash_buffer_rate"], "0.09")
            self.assertEqual(result["position_sizing"]["rebound_participation"]["cash_loosening"], {"from": "0.20", "to": "0.09"})
            # 風險 Agent 那把鑰匙沒轉（no_turn_evidence）→ 不放寬。
            closed = apply_trade_decision(base, decision, turn_stance("no_turn_evidence", level="defensive"), bundle)
            self.assertEqual(closed["cash_buffer_rate"], "0.20")
            self.assertNotIn("cash_loosening", closed["position_sizing"]["rebound_participation"])
            # 回撤下限為 defensive：有效狀態恆 none，不放寬（回撤下限優先）。
            floored = dict(base, drawdown_control={"neutral_at": "0.05", "defensive_at": "0.10", "stance_floor": "defensive"})
            kept = apply_trade_decision(floored, decision, stance, bundle)
            self.assertEqual(kept["cash_buffer_rate"], "0.20")
        # 中性姿態不放寬。
        with mock.patch("etf_agent.decision.rebound.build_turn_state", return_value={"state": "confirmed"}):
            neutral = apply_trade_decision(base, decision, turn_stance("turn_confirmed", level="neutral"), bundle)
        self.assertEqual(neutral["cash_buffer_rate"], SIZING["cash_buffer_by_stance"]["neutral"])


def errors_turnover(value):
    gate = copy.deepcopy(REBOUND)
    gate["by_state"]["confirmed"]["max_turnover_rate"] = value
    return sizing_errors(full_sizing(by_state=gate["by_state"]))


class TurnoverCapTests(unittest.TestCase):
    """confirmed 的選填換手上限：放寬風控、人設定；只在有效狀態為 confirmed 時抬高，其餘沿用 Policy 的 0.3。"""

    def effective_cap(self, state, turnover="0.4"):
        bundle, intent, policy, _ = AllocationTests().world(None)
        bound = copy.deepcopy(policy)
        bound["max_turnover_rate"] = "0.3"
        bound["position_sizing"]["cash_stance_turn_facts"] = dict(TURN_FACTS_GATE)
        bound["position_sizing"]["rebound_participation"] = dict(with_loosening(turnover=turnover), effective_state=state)
        bound["position_sizing"]["rebound_participation"]["by_state"]["candidate"].pop("defensive_cash_buffer")
        bound["position_sizing"]["rebound_participation"]["by_state"]["confirmed"].pop("defensive_cash_buffer")
        bound["content_sha256"] = decision_policy_sha256(bound)
        body = AllocationOrderEngine(bundle, bound).run(intent)
        return Decimal(body["effective_policy"]["max_turnover_rate"]), flag_codes(body)

    def test_only_a_confirmed_state_raises_the_cap(self):
        for state in ("none", "candidate"):
            cap, codes = self.effective_cap(state)
            self.assertEqual(cap, Decimal("0.3"), state)
            self.assertNotIn("REBOUND_TURNOVER_RAISED:confirmed", codes)
        cap, codes = self.effective_cap("confirmed")
        self.assertEqual(cap, Decimal("0.4"))
        self.assertIn("REBOUND_TURNOVER_RAISED:confirmed", codes)

    def test_a_cap_not_above_the_policy_value_never_tightens(self):
        cap, codes = self.effective_cap("confirmed", turnover="0.2")
        self.assertEqual(cap, Decimal("0.3"))
        self.assertNotIn("REBOUND_TURNOVER_RAISED:confirmed", codes)

    def test_lifted_turnover_rate_is_none_without_the_gate_or_the_field(self):
        from etf_agent.decision.rebound import lifted_turnover_rate

        policy = {"max_turnover_rate": "0.3", "position_sizing": {}}
        self.assertIsNone(lifted_turnover_rate(policy))
        policy["position_sizing"]["rebound_participation"] = dict(REBOUND, effective_state="confirmed")
        self.assertIsNone(lifted_turnover_rate(policy))
        policy["position_sizing"]["rebound_participation"] = dict(with_loosening(turnover="0.4"), effective_state="confirmed")
        self.assertEqual(lifted_turnover_rate(policy), Decimal("0.4"))


class LiveConfigTests(unittest.TestCase):
    def test_the_live_config_enables_only_the_confirmed_state(self):
        """2026-11-09 23:45 開 v1、2026-11-10 切到 v2：candidate 目標 0.30、confirmed 0.55，且不填兩個放寬風控的選填欄位。"""
        from test_gate_prerequisites import live_production_policy

        sizing = live_production_policy()["position_sizing"]
        gate = sizing["rebound_participation"]
        self.assertEqual(gate["version"], "v2")
        self.assertEqual(set(gate), {"version", "min_coverage", "by_state"})  # v2 沒有任何轉向門檻
        self.assertEqual(sizing["exposure_rotation"]["version"], "v3")
        self.assertIsNone(sizing["exposure_rotation"]["beta_target_by_stance"]["defensive"])
        self.assertEqual(gate["by_state"]["candidate"]["defensive_target"], "0.30")  # 2026-11-10 由 0.01 改 0.30（0.01 恆 beta_at_target）
        self.assertEqual(gate["by_state"]["confirmed"]["defensive_target"], "0.55")
        # 2026-11-11 開 position_sizing.offense：防守姿態現金放寬改由 offense.by_view 填（兩者互斥），所以不再填 defensive_cash_buffer；換手上限仍不填。
        for row in gate["by_state"].values():
            self.assertNotIn("defensive_cash_buffer", row)
            self.assertNotIn("max_turnover_rate", row)


if __name__ == "__main__":
    unittest.main()
