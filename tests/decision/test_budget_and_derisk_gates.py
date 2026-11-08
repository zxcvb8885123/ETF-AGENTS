"""審查 D-1／D-2 的版本閘門測試（只由人設定、策略反思禁區，省略即舊重建行為）：

- D-1 ``held_weight_budget``：加碼持股的現有權重已高於等級份額時，依現有權重計入配置預算、不加碼，
  其他候選不再超配後再被 CASH_BUFFER／RISK_CASH_FIT 砍回。
- D-2 ``drawdown_derisk.version: v4``：防守減碼的單次預算另扣交易 Agent 賣單後的換手餘額，不再與
  RISK_TURNOVER「留住 exit」互相拉扯（留住交易 Agent 想賣的、改賣別檔）。

每項都有正常流程、對照組（省略閘門維持原行為）與 fail-closed 分支。
"""

import copy
import unittest
from decimal import Decimal

from etf_agent.decision import (
    AllocationOrderEngine,
    DecisionPolicyValidator,
    ProposalValidator,
    bind_drawdown,
    decision_bundle_sha256,
    decision_policy_sha256,
    seal_trade_decision,
    trade_decision_envelope,
)
from etf_agent.decision.sizing import (
    DRAWDOWN_DERISK_VERSION_V4,
    HELD_WEIGHT_BUDGET,
    SIZING_METHOD_V2,
    drawdown_derisk_v2,
    drawdown_derisk_v3,
    drawdown_derisk_v4,
    validate_position_sizing,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES, KNOBS

from test_cash_fit_and_limits import gated, production_policy, with_rules
from test_portfolio_risk_decision import chain_inputs, policy as base_policy
from test_position_sizing import SIZING, sized_inputs, sized_policy
from test_risk_limit_gates import CONTROL, account, flags, guard, orders


def two_positions(bundle, cash, shares_2330, shares_2317):
    """帳戶現金與兩檔持股（行情：2330=164、2317=112），重算 NAV 與雜湊。"""
    snapshot = bundle["account_snapshot"]
    snapshot["cash"] = snapshot["settled_cash"] = str(cash)
    snapshot["positions"] = [
        {"symbol": "2330.TW", "shares": shares_2330, "average_cost": "120"},
        {"symbol": "2317.TW", "shares": shares_2317, "average_cost": "100"},
    ]
    snapshot["nav"] = str(Decimal(cash) + Decimal(shares_2330) * Decimal("164") + Decimal(shares_2317) * Decimal("112"))
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def reseal(bundle, intent, items):
    _, _, debate, _, _, _, _, _ = chain_inputs(copy.deepcopy(bundle))
    return seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), items)


class HeldWeightBudgetTests(unittest.TestCase):
    """審查 D-1 重現：2330 持有 3 張（現有權重 49.6%）標 add（medium）、2317 新買（high）、現金 50 萬、
    conviction_volatility_v2 且 volatility_exponent=0。"""

    def proposal(self, gate=True, tier_2330="medium"):
        bundle, intent, stance, policy = sized_inputs("high", tier_2330, stance="neutral")
        bundle = account(bundle, "500000", 3000)
        policy = sized_policy(policy, intent, stance, bundle)
        fields = {"method": SIZING_METHOD_V2, "volatility_exponent": "0"}
        if gate:
            fields["held_weight_budget"] = HELD_WEIGHT_BUDGET
        policy = gated(policy, **fields)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        return bundle, intent, policy, body

    def test_without_gate_the_new_buy_is_over_allocated_then_clipped(self):
        """對照組（現行行為）：2330 以 40.25% 份額進預算、實際保留 49.6%，2317 被配到 49.75%，再靠 CASH_BUFFER 砍回。"""
        bundle, _, _, body = self.proposal(gate=False)
        targets = {k: Decimal(v) for k, v in body["position_sizing"]["target_weights"].items()}
        current_weight = Decimal(3000) * Decimal("164") / Decimal(bundle["account_snapshot"]["nav"])
        self.assertLess(targets["2330.TW"], current_weight)  # 份額 40.25% 進預算，實際保留 49.6%
        self.assertGreater(targets["2317.TW"] + current_weight + Decimal("0.10"), Decimal("1"))  # 2317 超配
        self.assertIn(("2317.TW", "CASH_BUFFER"), flags(body))
        self.assertNotIn("held_weight_budget", body["effective_policy"])

    def test_gate_counts_the_held_weight_into_the_budget(self):
        bundle, _, policy, body = self.proposal()
        targets = {k: Decimal(v) for k, v in body["position_sizing"]["target_weights"].items()}
        nav = Decimal(bundle["account_snapshot"]["nav"])
        current_weight = Decimal(3000) * Decimal("164") / nav
        # 2330 的目標權重就是現有權重；預算 = 1 − 10% 緩衝 − 49.6% → 2317 拿到剩下的 40.4%，總和不超過 90%。
        self.assertEqual(targets["2330.TW"], current_weight.quantize(Decimal("0.00000001")))
        self.assertLessEqual(sum(targets.values()), Decimal("0.90"))
        self.assertNotIn(("2317.TW", "CASH_BUFFER"), flags(body))
        self.assertIn(("2330.TW", "RISK_HELD_WEIGHT"), flags(body))
        self.assertEqual(sorted(orders(body)), [("2317.TW", "buy")])
        self.assertNotIn(("2330.TW", "buy"), orders(body))
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_gate_is_a_no_op_when_the_sized_weight_exceeds_the_current_weight(self):
        """2330 只持 1 張（16%）、high 份額遠高於現有權重：閘門不凍結，訂單與旗標逐位元同無閘門。"""
        def body_for(gate):
            bundle, intent, stance, policy = sized_inputs("medium", "high", stance="neutral")
            bundle = account(bundle, "500000", 1000)
            policy = sized_policy(policy, intent, stance, bundle)
            fields = {"method": SIZING_METHOD_V2, "volatility_exponent": "0"}
            if gate:
                fields["held_weight_budget"] = HELD_WEIGHT_BUDGET
            policy = gated(policy, **fields)
            body = AllocationOrderEngine(bundle, policy).run(intent)
            self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
            return body
        with_gate, without = body_for(True), body_for(False)
        self.assertEqual(with_gate["order_proposal"]["orders"], without["order_proposal"]["orders"])
        self.assertEqual(with_gate["constraint_flags"], without["constraint_flags"])
        self.assertEqual(with_gate["position_sizing"]["target_weights"], without["position_sizing"]["target_weights"])

    def test_tampered_held_weight_flag_is_rejected_on_rebuild(self):
        bundle, intent, policy, body = self.proposal()
        forged = copy.deepcopy(body)
        forged["constraint_flags"] = [f for f in forged["constraint_flags"] if f["code"] != "RISK_HELD_WEIGHT"]
        self.assertTrue(ProposalValidator(bundle, policy, intent).validate(forged))
        forged = copy.deepcopy(body)
        forged["position_sizing"]["target_weights"]["2330.TW"] = "0.40250000"
        self.assertTrue(ProposalValidator(bundle, policy, intent).validate(forged))

    def test_gate_value_fails_closed_and_is_not_a_reflection_knob(self):
        for bad in ({"held_weight_budget": "v2"}, {"held_weight_budget": True}, {"held_weight_budget": None}):
            with self.subTest(bad=bad):
                policy = base_policy()
                policy["position_sizing"] = {**copy.deepcopy(SIZING), **bad}
                errors = []
                validate_position_sizing(policy, ["2330.TW", "2317.TW"], errors)
                self.assertTrue(any("held_weight_budget" in e for e in errors), errors)
        # 不在策略反思可調白名單（check_proposal 先查 FORBIDDEN_PREFIXES 再查 KNOBS，不在 KNOBS 即拒絕）。
        self.assertNotIn("held_weight_budget", KNOBS)


class TurnoverAwareDeriskTests(unittest.TestCase):
    """審查 D-2 重現：防守姿態、現金 3 萬（2.75%）、2330 1 張標 exit、2317 8 張續抱；換手上限 20%、單次減碼上限 25%。

    v3：exit 164k ＋ 減碼 1 張 2317 112k = 25.3% 超過上限 → RISK_TURNOVER 留住 2330 的 exit → 現金更低 →
    減碼改賣 2 張 2317（20.5%），仍超過上限且交易 Agent 想賣的沒賣。v4：減碼預算扣掉交易 Agent 賣單後只剩 5.4 萬，
    不足一張就不減碼，交易 Agent 的 exit 照賣、換手 15%。"""

    def proposal(self, version, max_turnover="0.20", cash="30000", shares_2330=1000, shares_2317=8000):
        bundle, intent, stance, policy = sized_inputs("medium", "low", "defensive")
        bundle = two_positions(bundle, cash, shares_2330, shares_2317)
        bundle = with_rules(
            bundle, max_stock_weight="1", special_weight_limits={"2330.TW": "1", "2317.TW": "1"},
            max_sector_weight="1", min_positions=1,
        )
        items = [
            dict(item, intent="hold", conviction=None, adopted_claim_ids=["bull-2317-1"], rejected_claim_ids=[])
            if item["symbol"] == "2317.TW" else
            dict(item, intent="exit", conviction=None, adopted_claim_ids=["bear-2330-1"], rejected_claim_ids=["bull-2330-1"])
            for item in intent["items"]
        ]
        intent = reseal(bundle, intent, items)
        policy["max_stock_weight"] = "1"
        policy["special_weight_limits"] = {"2330.TW": "1", "2317.TW": "1"}
        policy["max_sector_weight"] = "1"
        policy["liquidity_fill_rate"] = "1"
        policy["min_positions"] = 1
        policy["max_turnover_rate"] = max_turnover
        policy["position_sizing"]["turnover_fit"] = "v1"
        policy["position_sizing"]["drawdown_derisk"] = {"version": version, "max_derisk_fraction": "0.25"}
        policy["drawdown_control"] = dict(CONTROL)
        policy = bind_drawdown(policy, bundle, [])
        policy["content_sha256"] = decision_policy_sha256(policy)
        policy = sized_policy(policy, intent, stance, bundle)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        return bundle, policy, body

    def test_v3_keeps_the_trader_exit_and_sells_another_position_instead(self):
        """對照組（現行行為）：交易 Agent 想賣 2330 被留住，程式改賣 2 張 2317，換手仍超過上限。"""
        bundle, policy, body = self.proposal("v3")
        self.assertIn(("2330.TW", "RISK_TURNOVER"), flags(body))
        self.assertNotIn(("2330.TW", "sell"), orders(body))
        self.assertEqual(orders(body)[("2317.TW", "sell")]["shares"], 2000)
        self.assertGreater(Decimal(body["order_proposal"]["turnover_rate"]), Decimal("0.20"))
        self.assertFalse(guard(bundle, policy, body)["passed"])

    def test_v4_derisk_budget_leaves_room_for_the_trader_sells(self):
        bundle, policy, body = self.proposal("v4")
        sell = orders(body)[("2330.TW", "sell")]
        self.assertEqual((sell["shares"], sell["intent"]), (1000, "exit"))
        self.assertNotIn(("2317.TW", "sell"), orders(body))
        self.assertNotIn(("2330.TW", "RISK_TURNOVER"), flags(body))
        self.assertNotIn(("2317.TW", "RISK_DRAWDOWN"), flags(body))
        self.assertLessEqual(Decimal(body["order_proposal"]["turnover_rate"]), Decimal("0.20"))
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_v4_still_derisks_within_the_remaining_turnover_room(self):
        # 上限 30%：扣掉 exit 163,840 後還有 163,160，容納 1 張 2317；減碼旗標註明受換手上限約束。
        bundle, policy, body = self.proposal("v4", max_turnover="0.30")
        self.assertEqual(orders(body)[("2317.TW", "sell")]["shares"], 1000)
        self.assertEqual(orders(body)[("2330.TW", "sell")]["shares"], 1000)
        self.assertLessEqual(Decimal(body["order_proposal"]["turnover_rate"]), Decimal("0.30"))
        detail = next(f["detail"] for f in body["constraint_flags"] if f["code"] == "RISK_DRAWDOWN")
        self.assertIn("換手上限", detail)
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_v4_matches_v3_when_the_turnover_cap_does_not_bind(self):
        # 上限 100%：換手餘額遠大於單次減碼上限，v4 的訂單與旗標逐位元同 v3。
        v3 = self.proposal("v3", max_turnover="1")[2]
        v4 = self.proposal("v4", max_turnover="1")[2]
        self.assertEqual(v4["order_proposal"]["orders"], v3["order_proposal"]["orders"])
        self.assertEqual(v4["constraint_flags"], v3["constraint_flags"])

    def test_v4_converges_when_the_trader_sells_alone_exceed_the_cap(self):
        # 上限 10%（109k）：exit 本身就超過，留住 2330 後餘額 109k 仍不足一張 2317，沒有任何賣單、不再拉扯。
        bundle, policy, body = self.proposal("v4", max_turnover="0.10")
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertIn(("2330.TW", "RISK_TURNOVER"), flags(body))
        self.assertNotIn(("2317.TW", "RISK_DRAWDOWN"), flags(body))

    def test_v4_is_accepted_by_the_gate_validators_and_v5_is_not(self):
        helpers = (drawdown_derisk_v2, drawdown_derisk_v3, drawdown_derisk_v4)
        self.assertEqual([h({"drawdown_derisk": {"version": "v4"}}) for h in helpers], [True, True, True])
        self.assertEqual([h({"drawdown_derisk": {"version": "v3"}}) for h in helpers], [True, True, False])
        self.assertEqual([h({}) for h in helpers], [False, False, False])
        template = production_policy()["position_sizing"]
        for version, ok in (("v4", True), ("v5", False)):
            with self.subTest(version=version):
                policy = base_policy()
                policy["position_sizing"] = {**copy.deepcopy(template), "drawdown_derisk": {"version": version, "max_derisk_fraction": "0.25"}}
                policy["position_sizing"]["cash_ratio_range"].pop("anchor", None)
                policy["drawdown_control"] = dict(CONTROL)
                errors = []
                validate_position_sizing(policy, ["2330.TW", "2317.TW"], errors)
                self.assertEqual(not errors, ok, errors)
        self.assertEqual(DRAWDOWN_DERISK_VERSION_V4, "v4")
        self.assertTrue(any("drawdown_derisk" == p for p in FORBIDDEN_PREFIXES))
