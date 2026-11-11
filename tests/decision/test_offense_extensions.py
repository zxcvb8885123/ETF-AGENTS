"""position_sizing.offense 的兩個選配子欄位（已實作、尚未開啟）：docs/agent_led_offense_plan.md。

- ``thesis_size_cap``（A4）：放寬現金期間，purpose=thesis 的配對份量格也縮到該級 max_size（只縮不取消、不占曝險對數、不被 beta_stop 取消）。
- ``cash_glide``（A2）：offense 放寬現金之後，防守姿態的現金回升受 ``cash_ratio_range.max_daily_raise`` 約束，只限前次放寬過且回撤條件成立時；
  綁定紀錄 ``cash_raise_limited``，anchor 多一個布林 ``prior_offense_loosened``。

覆蓋：欄位形狀（fail-closed）、閘門缺席逐位元不變、各條件不成立時照格點（方向是多留現金）、Finalizer 重算綁定、竄改即拒絕、
前次 run 的放寬記錄讀取（雜湊不符一律拒絕）。數值只放在測試內。
"""

import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.core import canonical_sha256
from etf_agent.decision import AllocationOrderEngine, DecisionToolError, decision_policy_sha256
from etf_agent.decision.cash_ratio import _prior_offense_loosened, glide_enabled
from etf_agent.decision.contracts import artifact_content_sha256
from etf_agent.decision.offense import (
    CASH_RAISE_LIMITED_FIELD, bind_offense, bound_field_names, cash_glide_brief, cash_glide_ceiling, cash_glide_enabled, cash_raise_limited,
    drawdown_allows_glide, expected_cash_buffer, thesis_size_cap_active, thesis_size_cap_enabled, validate_offense_binding,
)
from etf_agent.decision.sizing import _validate_cash_anchor_shape, validate_position_sizing

from test_agent_swap_allocation import four_symbol_bundle, four_symbol_items, v2_world
from test_entry_reserve import RESERVE
from test_exposure_swap_allocation import LIMITS, TWO_PAIRS
from test_market_index_facts import GATE as MARKET_INDEX_GATE
from test_offense import OFFENSE, live, with_offense

THESIS_CAP = {"version": "v1"}
GLIDE = {"version": "v1"}
SOURCE = {
    "run_id": "r", "selection": "latest_cutoff", "target_session_start": "2026-11-10T09:00:00+08:00",
    "decision_sha256": "d", "proposal_id": "p", "proposal_sha256": "s",
}


def optional_errors(policy):
    errors = []
    validate_position_sizing(policy, ["2330.TW"], errors)
    return [error for error in errors if "offense" in error or "cash_glide" in error or "cash_raise_limited" in error or "prior_offense" in error]


def with_optional(**fields):
    def mutate(policy):
        policy["position_sizing"]["offense"].update(copy.deepcopy(fields))
    return with_offense(mutate)


class ThesisSizeCapGateTests(unittest.TestCase):
    def test_valid_shape_and_absent_field_unchanged(self):
        self.assertEqual(optional_errors(with_optional(thesis_size_cap=THESIS_CAP)), [])
        self.assertEqual(optional_errors(with_offense()), [])
        self.assertFalse(thesis_size_cap_enabled(OFFENSE))
        self.assertTrue(thesis_size_cap_enabled(dict(OFFENSE, thesis_size_cap=THESIS_CAP)))

    def test_bad_shapes_are_rejected(self):
        for label, value in {
            "未知版本": {"version": "v2"}, "缺版本": {}, "多欄位": {"version": "v1", "max_size": "large"}, "字串": "v1", "null": None, "布林": True,
        }.items():
            self.assertTrue(optional_errors(with_optional(thesis_size_cap=value)), label)


class ThesisSizeCapActiveTests(unittest.TestCase):
    LOOSENING = {"from": "0.15", "to": "0.08"}

    def gate(self, **changes):
        base = dict(OFFENSE, thesis_size_cap=THESIS_CAP, view="rebuild", effective_view="rebuild", suppressed_reason=None, cash_loosening=self.LOOSENING)
        return {**base, **changes}

    def test_active_only_when_bound_and_cash_is_loosened(self):
        self.assertTrue(thesis_size_cap_active(self.gate()))
        self.assertFalse(thesis_size_cap_active(self.gate(cash_loosening=None)))  # 沒放寬現金
        self.assertFalse(thesis_size_cap_active(self.gate(effective_view="keep_defensive", suppressed_reason="facts_unavailable")))
        self.assertFalse(thesis_size_cap_active({k: v for k, v in self.gate().items() if k != "effective_view"}))  # 未綁定
        self.assertFalse(thesis_size_cap_active({k: v for k, v in self.gate().items() if k != "thesis_size_cap"}))  # 欄位缺席
        self.assertFalse(thesis_size_cap_active(None))


class ThesisSizeCapAllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="736000", positions={"2330.TW": 1000, "1101.TW": 1000}, **LIMITS)
        # 防守姿態、格點 lower（0.15）：offense 放寬後現金目標 0.08（cash_loosening 由 Policy 驗證重算，必須一致）。
        cls.intent, base, _, _ = v2_world(cls.bundle, four_symbol_items(), pairs=TWO_PAIRS, policy_fields=LIMITS, level="defensive", grade="lower")
        cls.base = copy.deepcopy(base)
        sizing = cls.base["position_sizing"]
        sizing["agent_swap"]["exposure"] = {"version": "v2", "max_pairs_by_view": {"probe": 1, "start_rebuild": 2, "rebuild": 2}, "view": "keep_defensive"}
        sizing["exposure_view"] = {"version": "v2"}
        sizing["market_index_facts"] = dict(MARKET_INDEX_GATE)

    def run_offense(self, cap=True, loosening="default", purposes=("thesis", "thesis"), stop=None, max_size="medium", view="rebuild", size="large"):
        policy = copy.deepcopy(self.base)
        sizing = policy["position_sizing"]
        sizing["agent_swap"]["entry_reserve"] = dict(RESERVE, max_entry_weight="0.2")
        for pair, purpose in zip(sizing["agent_swap"]["nominated_pairs"], purposes):
            pair["purpose"] = purpose
            pair["size"] = size
        offense = copy.deepcopy(OFFENSE)
        offense["offense_off_drawdown"] = "0.08"
        offense["by_view"]["probe"]["max_exposure_pairs"] = 1
        offense["by_view"]["start_rebuild"]["max_exposure_pairs"] = 2
        offense["by_view"]["rebuild"]["max_exposure_pairs"] = 2
        for row in offense["by_view"].values():
            row["max_size"] = max_size
            if stop:
                row["beta_stop"] = stop
        if cap:
            offense["thesis_size_cap"] = dict(THESIS_CAP)
        if loosening == "default":
            loosening = {"from": "0.15", "to": offense["by_view"][view]["defensive_cash_buffer"]}
        if loosening is not None:
            policy["cash_buffer_rate"] = loosening["to"]
        else:  # 沒放寬現金：風險 Agent 維持防守（有效等級 keep_defensive），thesis 配對照常成交
            view = "keep_defensive"
        sizing["offense"] = dict(offense, view=view, effective_view=view, suppressed_reason=None, cash_loosening=loosening)
        sizing["agent_swap"]["exposure"]["view"] = view
        policy["content_sha256"] = decision_policy_sha256(policy)
        return AllocationOrderEngine(self.bundle, policy).run(self.intent)

    @staticmethod
    def flags(body):
        return {(f["symbol"], f["code"]) for f in body["constraint_flags"] if f["code"].startswith(("OFFENSE", "AGENT_SWAP"))}

    @staticmethod
    def sides(body):
        return {(o["symbol"], o["side"]) for o in body["order_proposal"]["orders"]}

    def test_thesis_pairs_are_capped_when_cash_is_loosened(self):
        capped, uncapped = self.run_offense(), self.run_offense(cap=False)
        self.assertIn(("2317.TW", "OFFENSE_SIZE_CAPPED"), self.flags(capped))
        self.assertIn(("2412.TW", "OFFENSE_SIZE_CAPPED"), self.flags(capped))
        self.assertFalse({s for s, c in self.flags(uncapped) if c == "OFFENSE_SIZE_CAPPED"})
        self.assertEqual(self.sides(capped), self.sides(uncapped))  # medium 上限仍買得到一張：只縮不取消

    def test_the_capped_size_is_what_the_reserve_plan_uses(self):
        """縮後的份量格才是保留預算的權重：small 的權重不足一張，整組以 entry_reserve_lot 取消（不是 beta 停止線）；不開欄位時同一組照常成交。"""
        capped, uncapped = self.run_offense(max_size="small"), self.run_offense(cap=False, max_size="small")
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:entry_reserve_lot"), self.flags(capped))
        self.assertEqual(capped["order_proposal"]["orders"], [])  # 取消的組：換出者不賣
        self.assertEqual(self.sides(uncapped), {("2330.TW", "sell"), ("2317.TW", "buy"), ("1101.TW", "sell"), ("2412.TW", "buy")})

    def test_absent_field_and_unloosened_cash_leave_the_allocation_bitwise_unchanged(self):
        reference = self.run_offense(cap=False, loosening=None)
        body = self.run_offense(cap=True, loosening=None)
        self.assertEqual(body["order_proposal"], reference["order_proposal"])
        self.assertEqual(body["constraint_flags"], reference["constraint_flags"])
        self.assertFalse({s for s, c in self.flags(body) if c == "OFFENSE_SIZE_CAPPED"})

    def test_a_smaller_trader_size_is_not_raised(self):
        body = self.run_offense(size="small")
        self.assertFalse({s for s, c in self.flags(body) if c == "OFFENSE_SIZE_CAPPED"})

    def test_beta_stop_never_cancels_a_thesis_pair_but_still_stops_exposure_pairs(self):
        thesis_only = self.run_offense(stop="0.05")
        self.assertEqual(self.sides(thesis_only), {("2330.TW", "sell"), ("2317.TW", "buy"), ("1101.TW", "sell"), ("2412.TW", "buy")})
        self.assertFalse({c for _, c in self.flags(thesis_only) if c.startswith("AGENT_SWAP_CANCELLED")})
        mixed = self.run_offense(stop="0.05", purposes=("thesis", "exposure"))
        self.assertEqual(self.sides(mixed), {("2330.TW", "sell"), ("2317.TW", "buy")})
        self.assertIn(("2412.TW", "AGENT_SWAP_CANCELLED:offense_beta_stop"), self.flags(mixed))
        self.assertIn(("2317.TW", "OFFENSE_SIZE_CAPPED"), self.flags(mixed))

    def test_thesis_pairs_do_not_use_the_exposure_pair_quota(self):
        body = self.run_offense(view="probe", purposes=("thesis", "thesis"))  # probe 的曝險配對上限是 1：thesis 不占
        self.assertEqual(self.sides(body), {("2330.TW", "sell"), ("2317.TW", "buy"), ("1101.TW", "sell"), ("2412.TW", "buy")})

    def test_exposure_only_behaviour_is_identical_with_or_without_the_field(self):
        with_cap = self.run_offense(purposes=("exposure", "exposure"), stop="0.6")
        without = self.run_offense(cap=False, purposes=("exposure", "exposure"), stop="0.6")
        self.assertEqual(with_cap["order_proposal"], without["order_proposal"])
        self.assertEqual(with_cap["constraint_flags"], without["constraint_flags"])


class CashGlideGateTests(unittest.TestCase):
    def test_valid_shape_and_absent_field_unchanged(self):
        self.assertEqual(optional_errors(with_optional(cash_glide=GLIDE)), [])
        self.assertFalse(cash_glide_enabled(OFFENSE))
        self.assertEqual(bound_field_names(OFFENSE), {"view", "effective_view", "suppressed_reason", "cash_loosening"})
        self.assertEqual(bound_field_names(dict(OFFENSE, cash_glide=GLIDE)),
                         {"view", "effective_view", "suppressed_reason", "cash_loosening", CASH_RAISE_LIMITED_FIELD})

    def test_bad_shapes_and_missing_cash_ratio_range_are_rejected(self):
        for label, value in {"未知版本": {"version": "v2"}, "缺版本": {}, "多欄位": {"version": "v1", "x": 1}, "字串": "v1"}.items():
            self.assertTrue(optional_errors(with_optional(cash_glide=value)), label)

        def drop(policy):
            policy["position_sizing"]["offense"]["cash_glide"] = dict(GLIDE)
            policy["position_sizing"].pop("cash_ratio_range")
        self.assertTrue(any("cash_ratio_range" in e for e in optional_errors(with_offense(drop))))

    def test_template_cannot_prefill_the_bound_record(self):
        policy = with_optional(cash_glide=GLIDE)
        self.assertEqual(optional_errors(policy), [])
        with self.assertRaises(DecisionToolError):
            bind_offense(
                dict(policy["position_sizing"], offense=dict(policy["position_sizing"]["offense"], cash_raise_limited=None)),
                {"exposure_view": "keep_defensive"}, None, None)

    def test_bind_adds_a_null_record_only_when_enabled(self):
        sizing = with_optional(cash_glide=GLIDE)["position_sizing"]
        bound = bind_offense(sizing, {"exposure_view": "keep_defensive"}, None, None)
        self.assertIn(CASH_RAISE_LIMITED_FIELD, bound)
        self.assertIsNone(bound[CASH_RAISE_LIMITED_FIELD])
        plain = bind_offense(with_offense()["position_sizing"], {"exposure_view": "keep_defensive"}, None, None)
        self.assertNotIn(CASH_RAISE_LIMITED_FIELD, plain)

    def test_a_bound_gate_must_carry_or_omit_the_record_with_the_field(self):
        policy = with_optional(cash_glide=GLIDE)
        sizing = policy["position_sizing"]
        sizing["offense"].update(view="keep_defensive", effective_view="keep_defensive", suppressed_reason=None, cash_loosening=None)
        errors = optional_errors(policy)
        self.assertTrue(errors)  # 缺 cash_raise_limited（以及未綁其他必要欄位）即拒絕
        sizing["offense"][CASH_RAISE_LIMITED_FIELD] = {"from": "0.15"}
        self.assertTrue(any("cash_raise_limited" in e for e in optional_errors(policy)))


def glide_sizing(anchor_ratio="0.10000000", status="prior_decision", loosened=True):
    sizing = copy.deepcopy(live()["position_sizing"])
    sizing["offense"]["cash_glide"] = dict(GLIDE)
    anchor = {"status": status, "ratio": anchor_ratio, "source": SOURCE if status == "prior_decision" else None}
    if status == "prior_decision" and loosened is not None:
        anchor["prior_offense_loosened"] = loosened
    sizing["cash_ratio_range"]["anchor"] = anchor
    return sizing


CONTROL = {"status": "available", "drawdown": "0.02", "stance_floor": "aggressive"}


class CashGlideRuleTests(unittest.TestCase):
    def limited(self, sizing=None, control=None, level="defensive", target="0.15"):
        sizing = sizing or glide_sizing()
        return cash_raise_limited(sizing, sizing["offense"], CONTROL if control is None else control, level, target)

    def test_all_conditions_hold_limits_the_raise_to_anchor_plus_max_daily_raise(self):
        self.assertEqual(self.limited(), {"from": "0.15", "to": "0.13"})
        self.assertEqual(cash_glide_ceiling(glide_sizing(), glide_sizing()["offense"], CONTROL, "defensive"), Decimal("0.13"))

    def test_a_target_already_within_the_ceiling_is_not_recorded(self):
        self.assertIsNone(self.limited(target="0.13"))
        self.assertIsNone(self.limited(target="0.10"))
        self.assertIsNone(self.limited(sizing=glide_sizing("0.14000000")))  # 上限 0.17 高於目標
        self.assertIsNone(self.limited(sizing=glide_sizing("0.12000000")))  # 上限剛好等於目標

    def test_each_failing_condition_falls_back_to_the_grid_point(self):
        cases = {
            "今日不是 defensive": self.limited(level="neutral"),
            "anchor 是 account_cash": self.limited(sizing=glide_sizing(status="account_cash")),
            "前次沒放寬": self.limited(sizing=glide_sizing(loosened=False)),
            "anchor 缺前次放寬欄位": self.limited(sizing=glide_sizing(loosened=None)),
            "回撤下限 defensive": self.limited(control=dict(CONTROL, stance_floor="defensive")),
            "帳本回撤不可用": self.limited(control={"status": "unavailable", "drawdown": None, "stance_floor": "aggressive"}),
            "回撤為 null": self.limited(control=dict(CONTROL, drawdown=None)),
            "回撤達停用線": self.limited(control=dict(CONTROL, drawdown="0.12")),
            "回撤超過停用線": self.limited(control=dict(CONTROL, drawdown="0.14")),
            "沒有 control": cash_raise_limited(glide_sizing(), glide_sizing()["offense"], None, "defensive", "0.15"),
        }
        for label, value in cases.items():
            self.assertIsNone(value, label)

    def test_without_the_field_nothing_glides(self):
        sizing = glide_sizing()
        sizing["offense"].pop("cash_glide")
        self.assertIsNone(cash_raise_limited(sizing, sizing["offense"], CONTROL, "defensive", "0.15"))

    def test_drawdown_condition_is_independent_of_the_agent_view(self):
        gate = glide_sizing()["offense"]
        self.assertTrue(drawdown_allows_glide(gate, CONTROL))
        self.assertTrue(drawdown_allows_glide(gate, dict(CONTROL, drawdown="0.119")))
        self.assertFalse(drawdown_allows_glide(gate, dict(CONTROL, drawdown="0.12")))
        self.assertFalse(drawdown_allows_glide(gate, dict(CONTROL, stance_floor="defensive")))
        self.assertFalse(drawdown_allows_glide(gate, "unavailable"))


class CashGlideBriefTests(unittest.TestCase):
    def test_brief_block_only_when_the_prior_run_loosened_cash(self):
        sizing = glide_sizing()
        block = cash_glide_brief(sizing["offense"], sizing["cash_ratio_range"])
        self.assertEqual((block["citable"], block["max_cash_target_today"]), (False, "0.13"))
        self.assertNotIn("run", json.dumps(block).lower().replace("running", ""))  # 不放前次 run 識別碼
        for label, other in {
            "前次沒放寬": glide_sizing(loosened=False), "account_cash": glide_sizing(status="account_cash"), "缺旗標": glide_sizing(loosened=None),
        }.items():
            self.assertIsNone(cash_glide_brief(other["offense"], other["cash_ratio_range"]), label)
        plain = glide_sizing()
        plain["offense"].pop("cash_glide")
        self.assertIsNone(cash_glide_brief(plain["offense"], plain["cash_ratio_range"]))


class CashGlideBindingTests(unittest.TestCase):
    """Finalizer 重算：Policy 綁定的 cash_raise_limited 必須等於重算結果；竄改（含把限制拿掉）即拒絕。"""

    def policy(self, limited, level="defensive", grade="lower", **control):
        policy = copy.deepcopy(live())
        sizing = glide_sizing()
        sizing["offense"].update(view="keep_defensive", effective_view="keep_defensive", suppressed_reason=None, cash_loosening=None)
        sizing["offense"][CASH_RAISE_LIMITED_FIELD] = limited
        sizing["cash_grade"] = grade
        policy["position_sizing"] = sizing
        policy["drawdown_control"] = dict(CONTROL, **control)
        team = {"cash_stance": {"level": level, "cash_grade": grade, "exposure_view": "keep_defensive"}}
        return policy, team

    def errors(self, policy, team):
        errors = []
        validate_offense_binding(policy, team, errors)
        return errors

    def test_the_recomputed_record_passes_and_tampering_is_rejected(self):
        good = {"from": "0.15", "to": "0.13"}
        self.assertEqual(self.errors(*self.policy(good)), [])
        for label, value in {
            "拿掉限制": None, "放寬 to": {"from": "0.15", "to": "0.15"}, "改 from": {"from": "0.18", "to": "0.13"}, "更緊": {"from": "0.15", "to": "0.11"},
        }.items():
            self.assertTrue(self.errors(*self.policy(value)), label)

    def test_a_null_record_is_rejected_when_the_glide_should_apply_and_accepted_when_it_should_not(self):
        self.assertTrue(self.errors(*self.policy(None)))
        self.assertEqual(self.errors(*self.policy(None, drawdown="0.13")), [])  # 回撤達停用線：取消滑行、照格點
        self.assertEqual(self.errors(*self.policy(None, stance_floor="defensive")), [])
        self.assertEqual(self.errors(*self.policy(None, level="neutral", grade="lower")), [])
        self.assertTrue(self.errors(*self.policy({"from": "0.15", "to": "0.13"}, drawdown="0.13")))  # 不該滑行卻綁了限制

    def test_expected_cash_buffer_uses_the_bound_record_and_checks_from(self):
        sizing = glide_sizing()
        sizing["offense"].update(view="keep_defensive", effective_view="keep_defensive", suppressed_reason=None, cash_loosening=None)
        sizing["offense"][CASH_RAISE_LIMITED_FIELD] = {"from": "0.15", "to": "0.13"}
        errors = []
        self.assertEqual(expected_cash_buffer(sizing, "defensive", "0.15", errors), "0.13")
        self.assertEqual(errors, [])
        sizing["offense"][CASH_RAISE_LIMITED_FIELD] = {"from": "0.18", "to": "0.13"}
        errors = []
        expected_cash_buffer(sizing, "defensive", "0.15", errors)
        self.assertTrue(any("cash_raise_limited.from" in e for e in errors))
        sizing["offense"][CASH_RAISE_LIMITED_FIELD] = None
        errors = []
        self.assertEqual(expected_cash_buffer(sizing, "defensive", "0.15", errors), "0.15")
        self.assertEqual(errors, [])


class AnchorShapeAndPriorTests(unittest.TestCase):
    def shape_errors(self, anchor, glide):
        errors = []
        _validate_cash_anchor_shape({"anchor": anchor, "max_daily_raise": "0.03"}, "p", errors, glide)
        return errors

    def test_anchor_shape_is_unchanged_without_the_field_and_needs_the_boolean_with_it(self):
        plain = {"status": "prior_decision", "ratio": "0.10000000", "source": SOURCE}
        self.assertEqual(self.shape_errors(plain, False), [])
        self.assertTrue(self.shape_errors(dict(plain, prior_offense_loosened=True), False))  # 沒開卻多一鍵
        self.assertEqual(self.shape_errors(dict(plain, prior_offense_loosened=True), True), [])
        self.assertTrue(self.shape_errors(plain, True))  # 開了卻缺
        self.assertTrue(self.shape_errors(dict(plain, prior_offense_loosened="yes"), True))
        account = {"status": "account_cash", "ratio": "0.10000000", "source": None}
        self.assertEqual(self.shape_errors(account, True), [])  # account_cash 不帶這個鍵

    def test_glide_enabled_reads_only_the_field(self):
        self.assertTrue(glide_enabled({"position_sizing": {"offense": {"cash_glide": GLIDE}}}))
        self.assertFalse(glide_enabled({"position_sizing": {"offense": {}}}))
        self.assertFalse(glide_enabled({"position_sizing": {}}))
        self.assertFalse(glide_enabled({}))

    def run_dir(self, directory, offense, policy_hash=None, mutate_policy=False):
        policy = {"policy_id": "prior", "position_sizing": {"offense": offense} if offense is not None else {}}
        policy["content_sha256"] = artifact_content_sha256(policy)
        text = dict(policy)
        if mutate_policy:
            text["policy_id"] = "tampered"
        (Path(directory) / "policy.json").write_text(json.dumps(text), encoding="utf-8")
        return {"policy_hash": policy_hash or policy["content_sha256"]}

    def test_prior_loosening_is_read_from_the_prior_policy_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            for offense, expected in (
                ({"cash_loosening": {"from": "0.15", "to": "0.08"}, "cash_raise_limited": None}, True),
                ({"cash_loosening": None, "cash_raise_limited": {"from": "0.15", "to": "0.13"}}, True),
                ({"cash_loosening": None, "cash_raise_limited": None}, False),
                ({"cash_loosening": None}, False),
                (None, False),
            ):
                proposal = self.run_dir(run, offense)
                self.assertEqual(_prior_offense_loosened(run, "prior-run", proposal), expected, offense)
            proposal = self.run_dir(run, {"cash_loosening": None}, mutate_policy=True)
            with self.assertRaises(DecisionToolError):  # policy 內容雜湊不一致
                _prior_offense_loosened(run, "prior-run", proposal)
            proposal = self.run_dir(run, {"cash_loosening": None}, policy_hash="0" * 64)
            with self.assertRaises(DecisionToolError):  # proposal 與 policy 不是同一組
                _prior_offense_loosened(run, "prior-run", proposal)
            (run / "policy.json").unlink()
            with self.assertRaises(DecisionToolError):
                _prior_offense_loosened(run, "prior-run", {"policy_hash": "x"})


if __name__ == "__main__":
    unittest.main()
