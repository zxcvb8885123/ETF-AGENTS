import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.decision import (
    DecisionPolicyValidator,
    DecisionToolError,
    apply_trade_decision,
    bind_drawdown,
    decision_policy_sha256,
    measure_drawdown,
    stance_floor,
)
from etf_agent.virtual_account import VirtualAccountRepository, VirtualAccountService
from test_portfolio_risk_decision import policy as base_policy
from test_position_sizing import SIZING
from test_team_chain import finalize, team_world
from test_trader import STANCE


CONTROL = {"neutral_at": "0.05", "defensive_at": "0.10"}


def history(*navs):
    return [{"run_id": "run-%d" % index, "as_of": "2026-10-%02dT10:00:00+00:00" % (index + 1), "nav": nav}
            for index, nav in enumerate(navs)]


class DrawdownMathTests(unittest.TestCase):
    def test_drawdown_is_measured_from_peak_nav(self):
        result = measure_drawdown(Decimal("940"), history("1000", "1050", "990"))
        self.assertEqual(result["peak_nav"], "1050")
        self.assertEqual(result["peak_as_of"], "2026-10-02T10:00:00+00:00")
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

    def test_bound_policy_finalizes(self):
        policy = apply_trade_decision(self.bound("1.06"), self.trade, STANCE, self.bundle, self.debate)
        policy["content_sha256"] = decision_policy_sha256(policy)
        bundle, momentum, debate, trade, _, team, _, _ = team_world()
        _, result = finalize(bundle, momentum, debate, trade, policy, team)
        self.assertEqual(result["status"], "approved", result["errors"])


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


if __name__ == "__main__":
    unittest.main()
