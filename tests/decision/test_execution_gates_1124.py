"""2026-11-24 執行面版本閘門：restricted_hold、pinned_high_ceiling、gap_reserve_budget、cash_basis（皆 {"version": "v1"}）。

省略閘門的舊 Policy 逐位元維持原重建行為；開啟後的結果仍須通過 ProposalValidator 重建與 CompetitionGuardV2。
"""

import copy
import unittest
from decimal import Decimal
from unittest.mock import patch

from etf_agent.decision import (
    AllocationOrderEngine, DecisionToolError, ProposalValidator, canonical_sha256, decision_bundle_sha256,
    decision_policy_sha256,
)
from etf_agent.decision.sizing import (
    CASH_BASIS, GAP_RESERVE_BUDGET, PINNED_HIGH_CEILING, RESTRICTED_HOLD, alpha_floor_weights, cash_basis_enabled,
    gap_reserve_budget_enabled, pinned_high_ceiling_enabled, restricted_hold_enabled, validate_position_sizing,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_exposure_rotation import NARROW
from test_gate_prerequisites import (
    anchored_sizing, decision_item, sized, stance, three_symbol_bundle, three_symbol_intent,
)
from test_portfolio_risk_decision import policy as base_policy
from test_risk_limit_gates import failed_rules, flags, guard, orders

NEW_GATES = {
    "restricted_hold": RESTRICTED_HOLD, "pinned_high_ceiling": PINNED_HIGH_CEILING,
    "gap_reserve_budget": GAP_RESERVE_BUDGET, "cash_basis": CASH_BASIS,
}


RULES = dict(
    max_positions=3, min_positions=0, max_stock_weight="0.5", max_sector_weight="1",
    special_weight_limits={"2330.TW": "0.5", "2317.TW": "0.5", "1101.TW": "0.5"},
)
CASH = "20000000"
POLICY_FIELDS = dict(
    liquidity_fill_rate="1", max_turnover_rate="1",
    sector_by_symbol={"2330.TW": "semi", "2317.TW": "electronics", "1101.TW": "materials"}, **RULES,
)


def with_status(bundle, tradable, blocked):
    """Guard 讀 Snapshot 的可交易名單（沒有交易狀態包時）；改完重算雜湊。"""
    result = copy.deepcopy(bundle)
    result["snapshot"]["tradable_symbols"] = list(tradable)
    result["snapshot"]["not_tradable_symbols"] = list(blocked)
    result["snapshot_sha256"] = canonical_sha256(result["snapshot"])
    result["bundle_sha256"] = decision_bundle_sha256(result)
    return result


# ------------------------------------------------------------------ restricted_hold
class RestrictedHoldTests(unittest.TestCase):
    """審查 #1：處置／停牌的持股漲價超限時，程式不得為它產生 trim 賣單，Guard 也不得因 TRADABLE 整份拒絕。"""

    def run_world(self, gate):
        bundle = three_symbol_bundle("200000", {"2330.TW": 5000})  # 2330 約 80% NAV，超過 50% 上限
        items = [
            decision_item("2330.TW", "hold", "medium", adopt=["bull-2330-1"], reject=["bear-2330-1"]),
            decision_item("2317.TW", "buy", "high", adopt=["bull-2317-1"]),
            decision_item("1101.TW", "none"),
        ]
        intent = three_symbol_intent(bundle, items)
        sizing = anchored_sizing(bundle, **NARROW)
        if gate:
            sizing["restricted_hold"] = copy.deepcopy(RESTRICTED_HOLD)
        with patch.object(AllocationOrderEngine, "_untradable", return_value={"2330.TW"}):
            policy, body = sized(bundle, intent, stance("neutral", "middle", "concentrated"), sizing)
        return bundle, policy, body

    def test_without_the_gate_the_restricted_holding_is_trimmed_and_the_guard_rejects(self):
        bundle, policy, body = self.run_world(gate=False)
        self.assertNotIn("restricted_hold", policy["position_sizing"])
        self.assertIn(("2330.TW", "sell"), orders(body))
        self.assertIn(("2330.TW", "RISK_STOCK_WEIGHT"), flags(body))
        result = guard(with_status(bundle, ["1101.TW", "2317.TW"], ["2330.TW"]), policy, body)
        self.assertFalse(result["passed"])
        self.assertIn("TRADABLE", failed_rules(result))

    def test_with_the_gate_the_restricted_holding_is_carried_and_the_guard_passes(self):
        bundle, policy, body = self.run_world(gate=True)
        self.assertTrue(restricted_hold_enabled(policy["position_sizing"]))
        self.assertNotIn(("2330.TW", "sell"), orders(body))
        self.assertNotIn(("2330.TW", "RISK_STOCK_WEIGHT"), flags(body))
        result = guard(with_status(bundle, ["1101.TW", "2317.TW"], ["2330.TW"]), policy, body)
        self.assertTrue(result["passed"], failed_rules(result))
        stock = next(check for check in result["checks"] if check["rule_id"] == "STOCK_WEIGHT")
        self.assertEqual([item["symbol"] for item in stock["details"]["carried"]], ["2330.TW"])
        self.assertEqual(stock["details"]["violations"], [])
        self.assertIn("HELD_NOT_TRADABLE", [check["rule_id"] for check in result["checks"]])

    def test_a_tradable_overweight_holding_is_still_trimmed_with_the_gate(self):
        # 閘門只放過「受限」的持股；可交易的超限持股照舊由程式減碼。
        bundle = three_symbol_bundle("200000", {"2330.TW": 5000})
        items = [
            decision_item("2330.TW", "hold", "medium", adopt=["bull-2330-1"], reject=["bear-2330-1"]),
            decision_item("2317.TW", "buy", "high", adopt=["bull-2317-1"]),
            decision_item("1101.TW", "none"),
        ]
        intent = three_symbol_intent(bundle, items)
        sizing = anchored_sizing(bundle, **NARROW)
        sizing["restricted_hold"] = copy.deepcopy(RESTRICTED_HOLD)
        policy, body = sized(bundle, intent, stance("neutral", "middle", "concentrated"), sizing)
        self.assertIn(("2330.TW", "sell"), orders(body))
        self.assertIn(("2330.TW", "RISK_STOCK_WEIGHT"), flags(body))
        self.assertNotIn("STOCK_WEIGHT", failed_rules(guard(bundle, policy, body)))

    def test_removing_the_gate_after_the_fact_fails_the_rebuild(self):
        bundle, policy, body = self.run_world(gate=True)
        altered = copy.deepcopy(policy)
        del altered["position_sizing"]["restricted_hold"]
        altered["content_sha256"] = decision_policy_sha256(altered)
        intent = three_symbol_intent(bundle, [
            decision_item("2330.TW", "hold", "medium", adopt=["bull-2330-1"], reject=["bear-2330-1"]),
            decision_item("2317.TW", "buy", "high", adopt=["bull-2317-1"]),
            decision_item("1101.TW", "none"),
        ])
        with patch.object(AllocationOrderEngine, "_untradable", return_value={"2330.TW"}):
            # 把閘門拿掉的 Policy 重算會多出 trim 賣單，與封存的 proposal 不一致。
            try:
                rebuilt = ProposalValidator(bundle, altered, intent).validate(body)
            except DecisionToolError:
                rebuilt = ["policy rejected"]
        self.assertTrue(rebuilt)


# ------------------------------------------------------------------ pinned_high_ceiling
MULTIPLIERS = {"high": Decimal("1.5"), "medium": Decimal("0.6"), "low": Decimal("0.3")}


def pinned_case(pinned_below):
    """1 檔被流動性卡在 2% 的 high、1 檔正常 high、8 檔 medium；單股上限 0.097、預算 0.90、α × 上限 = 0.06。"""
    mediums = ["m%d" % index for index in range(8)]
    order = ["pinned", "high"] + mediums
    tiers = {"pinned": "high", "high": "high", **{symbol: "medium" for symbol in mediums}}
    limits = {symbol: Decimal("0.097") for symbol in order}
    limits["pinned"] = Decimal("0.02")
    floors = {"pinned": Decimal("0.02"), "high": Decimal("0.06")}
    volatility = {symbol: Decimal("0.02") for symbol in order}
    return alpha_floor_weights(
        order, tiers, MULTIPLIERS, volatility, Decimal("0.01"), limits, Decimal("0.90"), Decimal("0"), floors,
        pinned_below,
    ), mediums, limits


class PinnedHighCeilingTests(unittest.TestCase):
    def test_without_the_gate_one_pinned_high_squeezes_every_medium(self):
        weights, mediums, _ = pinned_case(None)
        self.assertEqual({weights[symbol] for symbol in mediums}, {Decimal("0.02")})
        self.assertLess(sum(weights.values()), Decimal("0.30"))  # 預算 0.90 只配出不到 0.30

    def test_with_the_gate_the_pinned_high_no_longer_sets_the_ceiling(self):
        weights, mediums, limits = pinned_case(Decimal("0.06"))
        self.assertEqual(weights["pinned"], Decimal("0.02"))
        self.assertGreater(sum(weights.values()), Decimal("0.85"))
        self.assertLessEqual(sum(weights.values()), Decimal("0.90"))
        for symbol, weight in weights.items():
            self.assertLessEqual(weight, limits[symbol])
        # 正常 high 仍是 medium 的天花板。
        self.assertTrue(all(weights[symbol] <= weights["high"] for symbol in mediums))

    def test_a_high_at_its_normal_limit_is_not_treated_as_pinned(self):
        # required == limits 但上限不低於 α × 上限：不是被卡住，天花板照舊（與沒有閘門逐位相同）。
        off, _, _ = pinned_case(None)
        mediums = ["m%d" % index for index in range(8)]
        order = ["pinned", "high"] + mediums
        tiers = {"pinned": "high", "high": "high", **{symbol: "medium" for symbol in mediums}}
        limits = {symbol: Decimal("0.097") for symbol in order}
        limits["pinned"] = Decimal("0.02")
        floors = {"pinned": Decimal("0.02"), "high": Decimal("0.06")}
        volatility = {symbol: Decimal("0.02") for symbol in order}
        same = alpha_floor_weights(
            order, tiers, MULTIPLIERS, volatility, Decimal("0.01"), limits, Decimal("0.90"), Decimal("0"), floors,
            Decimal("0.02"),  # 閾值不高於 pinned 的上限 → 沒有任何 high 算被卡住
        )
        self.assertEqual(same, off)

    def test_every_high_pinned_leaves_the_mediums_at_their_own_limits(self):
        order = ["pinned", "m0", "m1"]
        tiers = {"pinned": "high", "m0": "medium", "m1": "medium"}
        limits = {"pinned": Decimal("0.02"), "m0": Decimal("0.097"), "m1": Decimal("0.097")}
        weights = alpha_floor_weights(
            order, tiers, MULTIPLIERS, {symbol: Decimal("0.02") for symbol in order}, Decimal("0.01"), limits,
            Decimal("0.90"), Decimal("0"), {"pinned": Decimal("0.02")}, Decimal("0.06"),
        )
        self.assertEqual(weights, {"pinned": Decimal("0.02"), "m0": Decimal("0.097"), "m1": Decimal("0.097")})

    def test_the_gate_needs_limit_scaled_conviction(self):
        policy = base_policy()
        sizing = anchored_sizing(three_symbol_bundle("1000000", {}))
        del sizing["limit_scaled_conviction"]
        sizing["pinned_high_ceiling"] = copy.deepcopy(PINNED_HIGH_CEILING)
        policy["position_sizing"] = sizing
        errors = []
        validate_position_sizing(policy, ["1101.TW", "2317.TW", "2330.TW"], errors)
        self.assertTrue(any("pinned_high_ceiling" in error for error in errors), errors)


# ------------------------------------------------------------------ gap_reserve_budget／cash_basis（端到端）
def split_cash(bundle, settled, unsettled):
    """已交割／未交割現金分開（總現金不變），重算雜湊。"""
    result = copy.deepcopy(bundle)
    account = result["account_snapshot"]
    assert Decimal(settled) + Decimal(unsettled) == Decimal(account["cash"])
    account["settled_cash"], account["unsettled_cash"] = str(settled), str(unsettled)
    result["bundle_sha256"] = decision_bundle_sha256(result)
    return result


def buy_world(bundle, **gates):
    items = [
        decision_item("1101.TW", "buy", "high", adopt=["bull-1101-1"]),
        decision_item("2317.TW", "buy", "high", adopt=["bull-2317-1"]),
        decision_item("2330.TW", "buy", "high", adopt=["bull-2330-1"], reject=["bear-2330-1"]),
    ]
    intent = three_symbol_intent(bundle, items)
    sizing = anchored_sizing(bundle, positions_by_breadth={"concentrated": 3, "balanced": 3, "diversified": 3})
    sizing.update({key: copy.deepcopy(value) for key, value in gates.items()})
    policy, body = sized(
        bundle, intent, stance("neutral", "middle", "concentrated"), sizing, base_policy(),
        **POLICY_FIELDS,
    )
    return policy, body


def buy_cost(body):
    return sum(-Decimal(o["net_cash_impact"]) for o in body["order_proposal"]["orders"] if o["side"] == "buy")


def end_cash(bundle, body):
    return Decimal(bundle["account_snapshot"]["cash"]) + sum(Decimal(o["net_cash_impact"]) for o in body["order_proposal"]["orders"])


class GapReserveBudgetTests(unittest.TestCase):
    def setUp(self):
        self.bundle = three_symbol_bundle(CASH, {}, **RULES)

    def test_without_the_gate_the_last_buy_is_cut_to_pay_the_gap_reserve(self):
        policy, body = buy_world(self.bundle)
        self.assertNotIn("gap_reserve_budget", policy["position_sizing"])
        self.assertIn("CASH_BUFFER", {code for _, code in flags(body)})

    def test_with_the_gate_the_budget_already_pays_for_the_reserve(self):
        policy, body = buy_world(self.bundle, gap_reserve_budget=GAP_RESERVE_BUDGET)
        self.assertTrue(gap_reserve_budget_enabled(policy["position_sizing"]))
        self.assertNotIn("CASH_BUFFER", {code for _, code in flags(body)})
        nav = Decimal(self.bundle["account_snapshot"]["nav"])
        # 期末現金仍不低於緩衝 + 買進 × 3% 跳空預留（_fit_cash 的原條件）。
        self.assertGreaterEqual(end_cash(self.bundle, body), nav * Decimal("0.10") + buy_cost(body) * Decimal("0.03"))
        self.assertTrue(guard(self.bundle, policy, body)["passed"])

    def test_the_gate_needs_buy_gap_reserve(self):
        policy = base_policy()
        sizing = anchored_sizing(self.bundle)
        del sizing["buy_gap_reserve"], sizing["buy_gap_reserve_rate"]
        sizing["gap_reserve_budget"] = copy.deepcopy(GAP_RESERVE_BUDGET)
        policy["position_sizing"] = sizing
        errors = []
        validate_position_sizing(policy, ["1101.TW", "2317.TW", "2330.TW"], errors)
        self.assertTrue(any("gap_reserve_budget" in error for error in errors), errors)


class CashBasisTests(unittest.TestCase):
    """審查 #4：預算用 NAV 口徑、_fit_cash 用已交割口徑，期末現金多出未交割現金；開閘門後緩衝以總現金計，另獨立擋已交割透支。"""

    def setUp(self):
        base = three_symbol_bundle(CASH, {}, **RULES)
        self.bundle = split_cash(base, "17000000", "3000000")
        self.gates = {"gap_reserve_budget": GAP_RESERVE_BUDGET}

    def test_without_the_gate_unsettled_cash_inflates_the_ending_cash(self):
        policy, body = buy_world(self.bundle, **self.gates)
        self.assertNotIn("cash_basis", policy["position_sizing"])
        self.assertGreater(end_cash(self.bundle, body), Decimal(CASH) * Decimal("0.10") + Decimal("3000000"))

    def test_with_the_gate_buffer_is_checked_on_total_cash_and_settled_cash_never_overdraws(self):
        off_policy, off = buy_world(self.bundle, **self.gates)
        on_policy, on = buy_world(self.bundle, cash_basis=CASH_BASIS, **self.gates)
        self.assertTrue(cash_basis_enabled(on_policy["position_sizing"]))
        self.assertGreater(buy_cost(on), buy_cost(off))
        # 不透支：本次買進（含 3% 跳空預留）不超過已交割現金；總現金期末仍不低於緩衝。
        self.assertLessEqual(buy_cost(on) * Decimal("1.03"), Decimal("17000000"))
        self.assertGreaterEqual(end_cash(self.bundle, on), Decimal(CASH) * Decimal("0.10"))
        self.assertTrue(guard(self.bundle, on_policy, on)["passed"])
        self.assertTrue(guard(self.bundle, off_policy, off)["passed"])

    def test_settled_cash_binds_when_unsettled_is_large(self):
        # 已交割只有 300000：即使總現金足以支付，買進也不得超過已交割現金（主辦方資金不足會整批作廢）。
        bundle = split_cash(three_symbol_bundle(CASH, {}, **RULES), "6000000", "14000000")
        policy, body = buy_world(bundle, cash_basis=CASH_BASIS, gap_reserve_budget=GAP_RESERVE_BUDGET)
        self.assertLessEqual(buy_cost(body), Decimal("6000000"))
        self.assertGreater(buy_cost(body), Decimal("0"))

    def test_with_nothing_unsettled_the_gate_changes_nothing_in_the_orders(self):
        base = three_symbol_bundle(CASH, {}, **RULES)
        off = buy_world(base, **self.gates)[1]
        on = buy_world(base, cash_basis=CASH_BASIS, **self.gates)[1]
        self.assertEqual(off["order_proposal"]["orders"], on["order_proposal"]["orders"])

    def test_removing_the_gate_after_the_fact_fails_the_rebuild(self):
        policy, body = buy_world(self.bundle, cash_basis=CASH_BASIS, **self.gates)
        altered = copy.deepcopy(policy)
        del altered["position_sizing"]["cash_basis"]
        altered["content_sha256"] = decision_policy_sha256(altered)
        intent = three_symbol_intent(self.bundle, [
            decision_item("1101.TW", "buy", "high", adopt=["bull-1101-1"]),
            decision_item("2317.TW", "buy", "high", adopt=["bull-2317-1"]),
            decision_item("2330.TW", "buy", "high", adopt=["bull-2330-1"], reject=["bear-2330-1"]),
        ])
        try:
            rebuilt = ProposalValidator(self.bundle, altered, intent).validate(body)
        except DecisionToolError:
            rebuilt = ["policy rejected"]
        self.assertTrue(rebuilt)


# ------------------------------------------------------------------ 形狀與禁區
class GateShapeTests(unittest.TestCase):
    def errors(self, key, value):
        sizing = anchored_sizing(three_symbol_bundle("1000000", {}), **{key: value})
        policy = base_policy()
        policy["position_sizing"] = sizing
        errors = []
        validate_position_sizing(policy, ["1101.TW", "2317.TW", "2330.TW"], errors)
        return errors

    def test_only_the_exact_version_object_is_accepted(self):
        for key, good in NEW_GATES.items():
            with self.subTest(key=key):
                self.assertEqual(self.errors(key, copy.deepcopy(good)), [])
                for bad in ("v1", True, None, {}, {"version": "v2"}, {"version": "v1", "extra": 1}, ["v1"], 1):
                    self.assertTrue(any(key in error for error in self.errors(key, bad)), (key, bad))

    def test_enabled_helpers_are_fail_closed(self):
        for helper, key in (
            (restricted_hold_enabled, "restricted_hold"), (pinned_high_ceiling_enabled, "pinned_high_ceiling"),
            (gap_reserve_budget_enabled, "gap_reserve_budget"), (cash_basis_enabled, "cash_basis"),
        ):
            with self.subTest(key=key):
                self.assertFalse(helper({}))
                self.assertFalse(helper({key: {"version": "v2"}}))
                self.assertTrue(helper({key: {"version": "v1"}}))

    def test_strategy_reflection_cannot_touch_them(self):
        for key in NEW_GATES:
            with self.subTest(key=key):
                self.assertIn(key, FORBIDDEN_PREFIXES)


if __name__ == "__main__":
    unittest.main()
