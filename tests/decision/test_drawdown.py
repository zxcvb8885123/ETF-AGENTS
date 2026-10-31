import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionFinalizer,
    DecisionResultValidator,
    RevisionHistoryBuilder,
    ScenarioEngine,
    DecisionPolicyValidator,
    DecisionToolError,
    apply_trade_decision,
    bind_drawdown,
    decision_policy_sha256,
    measure_drawdown,
    stance_floor,
)
from etf_agent.virtual_account import (
    VirtualAccountError, VirtualAccountRepository, VirtualAccountService, ledger_nav_history_provider,
)
from test_portfolio_risk_decision import policy as base_policy, risk_review
from test_position_sizing import SIZING
from test_team_chain import finalize, team_world
from test_trader import STANCE


CONTROL = {"neutral_at": "0.05", "defensive_at": "0.10"}


def history(*navs):
    return [{"run_id": "run-%d" % index, "as_of": "2026-09-%02dT10:00:00+00:00" % (index + 1), "nav": nav}
            for index, nav in enumerate(navs)]


class DrawdownMathTests(unittest.TestCase):
    def test_drawdown_is_measured_from_peak_nav(self):
        result = measure_drawdown(Decimal("940"), history("1000", "1050", "990"))
        self.assertEqual(result["peak_nav"], "1050")
        self.assertEqual(result["peak_as_of"], "2026-09-02T10:00:00+00:00")
        self.assertEqual(result["drawdown"], "0.104762")
        # 創新高時回撤為 0，峰值就是目前 NAV。
        self.assertEqual(measure_drawdown(Decimal("1100"), history("1000"))["drawdown"], "0.000000")

    def test_stance_floor_follows_policy_thresholds(self):
        self.assertEqual(stance_floor(CONTROL, Decimal("0.049999")), "aggressive")
        self.assertEqual(stance_floor(CONTROL, Decimal("0.05")), "neutral")
        self.assertEqual(stance_floor(CONTROL, Decimal("0.10")), "defensive")

    def test_bad_history_fails_closed(self):
        for nav in ("0", "-1", "NaN", True, None):
            with self.assertRaises(DecisionToolError):
                measure_drawdown(Decimal("1000"), history(nav))


class DrawdownPolicyTests(unittest.TestCase):
    def setUp(self):
        self.bundle, _, self.debate, self.trade, _, _, _, _ = team_world()
        self.nav = Decimal(str(self.bundle["account_snapshot"]["nav"]))
        self.base = base_policy()
        self.base["position_sizing"] = copy.deepcopy(SIZING)
        self.base["drawdown_control"] = dict(CONTROL)
        self.base["content_sha256"] = decision_policy_sha256(self.base)

    def bound(self, peak_ratio):
        peak = str(self.nav * Decimal(peak_ratio))
        policy = bind_drawdown(self.base, self.bundle, history(peak))
        policy["content_sha256"] = decision_policy_sha256(policy)
        return policy

    def errors(self, policy):
        return DecisionPolicyValidator(self.bundle).validate(policy)

    def test_bound_policy_validates_and_records_floor(self):
        policy = self.bound("1.12")
        self.assertEqual(self.errors(policy), [])
        self.assertEqual(policy["drawdown_control"]["stance_floor"], "defensive")
        self.assertEqual(policy["drawdown_control"]["status"], "available")

    def test_stance_below_floor_is_refused(self):
        policy = self.bound("1.06")
        self.assertEqual(policy["drawdown_control"]["stance_floor"], "neutral")
        with self.assertRaisesRegex(DecisionToolError, "回撤下限"):
            apply_trade_decision(policy, self.trade, dict(STANCE, level="aggressive"), self.bundle)
        applied = apply_trade_decision(policy, self.trade, STANCE, self.bundle)
        applied["content_sha256"] = decision_policy_sha256(applied)
        self.assertEqual(self.errors(applied), [])
        # 綁定後再把姿態改成比下限積極，Validator 仍會擋下。
        applied["position_sizing"]["cash_stance"] = "aggressive"
        applied["content_sha256"] = decision_policy_sha256(applied)
        self.assertTrue(any("低於回撤下限" in error for error in self.errors(applied)))

    def test_tampered_drawdown_or_nav_is_rejected(self):
        policy = self.bound("1.12")
        changed = copy.deepcopy(policy)
        changed["drawdown_control"]["drawdown"] = "0.01"
        changed["content_sha256"] = decision_policy_sha256(changed)
        self.assertTrue(any("重算不一致" in error for error in self.errors(changed)))
        changed = copy.deepcopy(policy)
        changed["drawdown_control"]["stance_floor"] = "aggressive"
        changed["content_sha256"] = decision_policy_sha256(changed)
        self.assertTrue(any("stance_floor" in error for error in self.errors(changed)))
        changed = copy.deepcopy(policy)
        changed["drawdown_control"]["current_nav"] = "1"
        changed["content_sha256"] = decision_policy_sha256(changed)
        self.assertTrue(any("current_nav" in error for error in self.errors(changed)))

    def test_future_peak_time_or_bad_history_length_is_rejected(self):
        policy = self.bound("1.12")
        changed = copy.deepcopy(policy)
        changed["drawdown_control"]["peak_as_of"] = "2099-01-01T00:00:00+00:00"
        changed["content_sha256"] = decision_policy_sha256(changed)
        self.assertTrue(any("peak_as_of 晚於 decision_cutoff" in error for error in self.errors(changed)))
        changed = copy.deepcopy(policy)
        changed["drawdown_control"]["history_length"] = -3
        changed["content_sha256"] = decision_policy_sha256(changed)
        self.assertTrue(any("history_length" in error for error in self.errors(changed)))

    def test_history_ref_is_recorded_and_verified_against_ledger_history(self):
        peak = str(self.nav * Decimal("1.12"))
        rows = history(peak, str(self.nav))
        policy = bind_drawdown(self.base, self.bundle, rows)
        policy["content_sha256"] = decision_policy_sha256(policy)
        ref = policy["drawdown_control"]["history_ref"]
        self.assertEqual((ref["head_run_id"], ref["length"]), ("run-1", 2))
        validator = DecisionPolicyValidator(self.bundle)
        self.assertEqual(validator.validate(policy, rows), [])
        self.assertEqual(validator.validate(policy), [])

    def test_peak_forged_to_current_nav_is_rejected_with_ledger_history(self):
        rows = history(str(self.nav * Decimal("1.12")), str(self.nav))
        policy = bind_drawdown(self.base, self.bundle, rows)
        forged = copy.deepcopy(policy)
        control = forged["drawdown_control"]
        control.update(peak_nav=str(self.nav), peak_as_of=None, drawdown="0.000000", stance_floor="aggressive")
        forged["content_sha256"] = decision_policy_sha256(forged)
        errors = DecisionPolicyValidator(self.bundle).validate(forged, rows)
        self.assertTrue(any("peak_nav 與帳本 NAV 歷史重算不一致" in error for error in errors), errors)

    def test_history_ref_mismatch_or_missing_is_rejected(self):
        rows = history(str(self.nav * Decimal("1.12")), str(self.nav))
        policy = bind_drawdown(self.base, self.bundle, rows)
        validator = DecisionPolicyValidator(self.bundle)
        changed = copy.deepcopy(policy)
        changed["drawdown_control"]["history_ref"]["sha256"] = "0" * 64
        changed["content_sha256"] = decision_policy_sha256(changed)
        self.assertTrue(any("history_ref 與帳本" in error for error in validator.validate(changed, rows)))
        changed = copy.deepcopy(policy)
        changed["drawdown_control"]["history_ref"]["length"] = 1
        changed["content_sha256"] = decision_policy_sha256(changed)
        self.assertTrue(any("history_ref 必須含" in error for error in validator.validate(changed)))
        changed = copy.deepcopy(policy)
        del changed["drawdown_control"]["history_ref"]
        changed["content_sha256"] = decision_policy_sha256(changed)
        self.assertTrue(any("必須含 history_ref" in error for error in validator.validate(changed, rows)))
        # 沒有帳本歷史可讀時（舊 run 重建）只做結構檢查，不因缺 history_ref 而拒絕。
        self.assertEqual(validator.validate(changed), [])

    def test_rows_without_run_id_keep_legacy_policy_shape(self):
        rows = [{"as_of": "2026-09-02T10:00:00+00:00", "nav": str(self.nav * Decimal("1.12"))}]
        policy = bind_drawdown(self.base, self.bundle, rows)
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.assertNotIn("history_ref", policy["drawdown_control"])
        self.assertEqual(DecisionPolicyValidator(self.bundle).validate(policy, rows), [])

    def test_bound_stance_requires_recorded_drawdown(self):
        applied = apply_trade_decision(self.base, self.trade, STANCE, self.bundle)
        applied["content_sha256"] = decision_policy_sha256(applied)
        self.assertTrue(any("先記錄本次回撤" in error for error in self.errors(applied)))

    def test_missing_history_is_recorded_as_unavailable(self):
        policy = bind_drawdown(self.base, self.bundle, None)
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.assertEqual(policy["drawdown_control"]["status"], "unavailable")
        self.assertIsNone(policy["drawdown_control"]["stance_floor"])
        self.assertEqual(self.errors(policy), [])

    def test_bad_thresholds_are_rejected(self):
        policy = copy.deepcopy(self.base)
        policy["drawdown_control"] = {"neutral_at": "0.10", "defensive_at": "0.05"}
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.assertTrue(any("門檻" in error for error in self.errors(policy)))

    def test_policy_without_control_still_finalizes(self):
        # 舊決策的 policy 沒有 drawdown_control，照原樣重建驗證。
        bundle, momentum, debate, trade, policy, team, _, _ = team_world()
        self.assertNotIn("drawdown_control", policy)
        _, result = finalize(bundle, momentum, debate, trade, policy, team)
        self.assertEqual(result["status"], "approved", result["errors"])

    def finalize_with(self, policy, provider):
        """與 test_team_chain.finalize 相同，但帶入帳本 NAV 歷史來源；回傳 (artifacts, result, validate)。"""
        bundle, momentum, debate, trade, _, team, _, _ = team_world()
        proposal = AllocationOrderEngine(bundle, policy).run(trade)
        scenario = ScenarioEngine(bundle, policy).run(proposal)
        guard = CompetitionGuardV2(bundle, policy).run(proposal, scenario)
        review = risk_review(bundle, proposal, scenario, guard, decision="approve" if guard["passed"] else "reject")
        chain = RevisionHistoryBuilder(bundle, policy, trade).create(proposal, scenario, guard, review)
        result = DecisionFinalizer(
            bundle, policy, momentum, debate, trade, team, nav_history_provider=provider
        ).run(proposal, scenario, guard, review, chain)

        def validate(candidate, validator_provider):
            return DecisionResultValidator(
                bundle, policy, momentum, debate, trade, team, nav_history_provider=validator_provider
            ).validate(proposal, scenario, guard, review, chain, candidate)
        return result, validate

    def bound_final_policy(self):
        policy = apply_trade_decision(self.bound("1.06"), self.trade, STANCE, self.bundle, self.debate)
        policy["content_sha256"] = decision_policy_sha256(policy)
        return policy

    def ledger(self, policy):
        rows = history(policy["drawdown_control"]["peak_nav"])
        return rows, lambda head: rows

    def test_bound_policy_finalizes_with_ledger_history(self):
        policy = self.bound_final_policy()
        rows, provider = self.ledger(policy)
        calls = []
        result, validate = self.finalize_with(policy, lambda head: calls.append(head) or provider(head))
        self.assertEqual(result["status"], "approved", result["errors"])
        self.assertEqual(calls, [policy["drawdown_control"]["history_ref"]["head_run_id"]])
        self.assertEqual(validate(result, provider), [])

    def test_history_ref_without_ledger_source_is_rejected(self):
        policy = self.bound_final_policy()
        _, provider = self.ledger(policy)
        approved, validate = self.finalize_with(policy, provider)
        result, _ = self.finalize_with(policy, None)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("未提供帳本來源" in error for error in result["errors"]), result["errors"])
        # 已核准的結果在沒有帳本來源時無法重建通過。
        self.assertEqual(validate(approved, None), ["DecisionResult 與完整重建結果不一致"])

    def test_peak_forged_to_current_nav_is_rejected_on_rebuild(self):
        policy = self.bound_final_policy()
        rows, provider = self.ledger(policy)
        approved, _ = self.finalize_with(policy, provider)
        forged = copy.deepcopy(policy)
        forged["drawdown_control"].update(
            peak_nav=str(self.nav), peak_as_of=None, drawdown="0.000000", stance_floor="aggressive")
        forged["content_sha256"] = decision_policy_sha256(forged)
        result, validate = self.finalize_with(forged, provider)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("peak_nav 與帳本 NAV 歷史重算不一致" in error for error in result["errors"]), result["errors"])
        self.assertEqual(len(result["errors"]), len(set(result["errors"])), "同一錯誤訊息不得重複")
        self.assertNotEqual(validate(approved, provider), [])

    def test_missing_head_or_wrong_history_is_rejected(self):
        policy = self.bound_final_policy()
        rows, _ = self.ledger(policy)

        def missing_head(head):
            raise ValueError("找不到帳本 run：%s" % head)

        result, _ = self.finalize_with(policy, missing_head)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("無法由帳本讀回歷史" in error for error in result["errors"]), result["errors"])
        other = history(str(self.nav * Decimal("2")))
        result, _ = self.finalize_with(policy, lambda head: other)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("history_ref 與帳本" in error for error in result["errors"]), result["errors"])

    def test_policy_without_history_ref_never_reads_ledger(self):
        policy = apply_trade_decision(self.bound("1.06"), self.trade, STANCE, self.bundle, self.debate)
        del policy["drawdown_control"]["history_ref"]
        policy["content_sha256"] = decision_policy_sha256(policy)

        def never(head):
            raise AssertionError("舊 policy 不得讀帳本")

        result, validate = self.finalize_with(policy, never)
        self.assertEqual(result["status"], "approved", result["errors"])
        self.assertEqual(validate(result, None), [])


class LedgerNavHistoryTests(unittest.TestCase):
    def test_history_walks_parent_chain_from_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = root / "rules.json"
            rules.write_text(json.dumps({"initial_capital_twd": 1_000_000_000, "version": "fixture-v1"}), encoding="utf-8")
            repository = VirtualAccountRepository(root / "accounts", "cup-test")
            VirtualAccountService(repository).initialize(rules, "cup-test", "2026-09-18T17:00:00+08:00")
            self.assertEqual(repository.nav_history(), [
                {"run_id": "genesis", "as_of": repository.latest()["state"]["as_of"], "nav": "1000000000"}])
            self.assertEqual(repository.nav_history("genesis"), repository.nav_history())

            bundle = {"account_snapshot": {"account_id": "cup-test"}}
            provider = ledger_nav_history_provider(root / "accounts", bundle)
            self.assertEqual(provider("genesis"), repository.nav_history())
            with self.assertRaises(VirtualAccountError):
                provider("no-such-run")
            with self.assertRaises(VirtualAccountError):
                ledger_nav_history_provider(root / "accounts", {"account_snapshot": {"account_id": "other"}})("genesis")
            # 讀取不存在的帳戶不得順手建立目錄。
            self.assertFalse((root / "accounts" / "other").exists())


if __name__ == "__main__":
    unittest.main()
