"""position_sizing.high_risk_cap v1（高風險股上限）的配置層與 Policy 驗證測試。

合成世界沿用 test_agent_swap_allocation 的四檔世界與兩組提名（買 2317 換 2330、買 2412 換 1101）；
門檻用極端值控制哪些股票算高風險（數值只放在測試內，不是建議值）。
"""

import copy
import unittest
from decimal import Decimal

from etf_agent.decision import AllocationOrderEngine, ProposalValidator, decision_policy_sha256
from etf_agent.decision.fields import DecisionToolError
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_agent_swap_allocation import four_symbol_bundle, four_symbol_items, guard, summary, v2_world
from test_exposure_swap_allocation import LIMITS, TWO_PAIRS

NOTHING_HIGH = {"beta_at_least": "5", "atr_pct_at_least": "1"}
EVERYTHING_HIGH = {"beta_at_least": "0.000001", "atr_pct_at_least": "0.000001"}


def gate(mode="enforce", high=None, weight="1", entries=1, scope="all"):
    return {
        "version": "v1", "mode": mode, "scope": scope, "high": dict(high or EVERYTHING_HIGH),
        "max_high_risk_weight": weight, "max_new_high_risk_entries": entries,
    }


class HighRiskCapAllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="736000", positions={"2330.TW": 1000, "1101.TW": 1000}, **LIMITS)
        cls.intent, cls.base, cls.baseline, _ = v2_world(
            cls.bundle, four_symbol_items(), pairs=TWO_PAIRS, policy_fields=LIMITS)
        cls.baseline_failed = {
            c["rule_id"] for c in guard(cls.bundle, cls.base, cls.baseline)["checks"] if not c.get("passed")}

    def setUp(self):
        self.original_intent = copy.deepcopy(self.intent)

    def run_gate(self, value):
        policy = copy.deepcopy(self.base)
        policy["position_sizing"]["high_risk_cap"] = value
        policy["content_sha256"] = decision_policy_sha256(policy)
        body = AllocationOrderEngine(self.bundle, policy).run(self.intent)
        self.assertEqual(ProposalValidator(self.bundle, policy, self.intent).validate(body), [])
        # 合成世界的 2412.TW 沒有產業分類（基準配置本身就有 SECTOR_CLASSIFICATION），所以只要求不比基準多出任何失敗規則。
        failed = {c["rule_id"] for c in guard(self.bundle, policy, body)["checks"] if not c.get("passed")}
        self.assertLessEqual(failed, self.baseline_failed)
        self.assertEqual(self.intent, self.original_intent)  # 交易 Agent 的意圖未被改寫
        return body

    @staticmethod
    def codes(body):
        return {(f["symbol"], f["code"]) for f in body["constraint_flags"]}

    def test_baseline_world_takes_both_pairs(self):
        self.assertEqual(
            {(o["symbol"], o["side"]) for o in self.baseline["order_proposal"]["orders"]},
            {("2317.TW", "buy"), ("2330.TW", "sell"), ("2412.TW", "buy"), ("1101.TW", "sell")})

    def test_no_high_risk_symbol_leaves_orders_unchanged(self):
        body = self.run_gate(gate(high=NOTHING_HIGH, weight="0.01", entries=0))
        self.assertEqual(summary(body), summary(self.baseline))
        self.assertIn(("PORTFOLIO", "HIGH_RISK_CAP_STATUS"), self.codes(body))

    def test_only_the_first_nominated_high_risk_entry_is_kept(self):
        body = self.run_gate(gate(entries=1))
        sides = {(o["symbol"], o["side"]) for o in body["order_proposal"]["orders"]}
        self.assertEqual(sides, {("2317.TW", "buy"), ("2330.TW", "sell")})
        self.assertIn(("2412.TW", "AGENT_SWAP_CANCELLED:high_risk_cap"), self.codes(body))
        self.assertNotIn(("1101.TW", "sell"), sides)  # 換出者不賣，不會只賣不買

    def test_zero_new_entries_cancels_every_high_risk_pair(self):
        body = self.run_gate(gate(entries=0))
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertTrue({("2317.TW", "AGENT_SWAP_CANCELLED:high_risk_cap"), ("2412.TW", "AGENT_SWAP_CANCELLED:high_risk_cap")} <= self.codes(body))

    def test_portfolio_already_over_the_weight_cap_takes_no_high_risk_entry(self):
        body = self.run_gate(gate(weight="0.01", entries=5))
        self.assertEqual(body["order_proposal"]["orders"], [])

    def engine_with_stance(self, value, stance):
        policy = copy.deepcopy(self.base)
        policy["position_sizing"]["high_risk_cap"] = value
        policy["content_sha256"] = decision_policy_sha256(policy)
        engine = AllocationOrderEngine(self.bundle, policy)
        # 只換 scope 讀的姿態（綁其他姿態還要同步現金格點，與此處要測的無關；同 test_swap_earmark 的做法）。
        sizing = dict(engine.policy["position_sizing"])
        if stance is None:
            sizing.pop("cash_stance", None)
        else:
            sizing["cash_stance"] = stance
        engine.policy["position_sizing"] = sizing
        return engine

    def test_defensive_scope_brakes_only_when_the_risk_agent_says_defensive(self):
        self.assertIsNotNone(self.engine_with_stance(gate(scope="defensive"), "defensive")._high_risk_rules())
        for stance in ("neutral", "aggressive"):
            self.assertIsNone(self.engine_with_stance(gate(scope="defensive"), stance)._high_risk_rules())
        self.assertIsNotNone(self.engine_with_stance(gate(scope="all"), "aggressive")._high_risk_rules())

    def test_defensive_scope_fails_closed_when_stance_is_unbound(self):
        with self.assertRaises(DecisionToolError):
            self.engine_with_stance(gate(scope="defensive"), None)._high_risk_rules()

    def test_defensive_scope_end_to_end_follows_the_bound_stance(self):
        body = self.run_gate(gate(entries=0, scope="defensive"))
        if self.base["position_sizing"].get("cash_stance") == "defensive":
            self.assertEqual(body["order_proposal"], self.run_gate(gate(entries=0))["order_proposal"])
        else:
            self.assertEqual(body["order_proposal"]["orders"], self.baseline["order_proposal"]["orders"])

    def test_quality_dip_swap_ins_are_not_braked(self):
        engine = self.engine_with_stance(gate(entries=0), "defensive")
        sizing = dict(engine.policy["position_sizing"])
        swap = dict(sizing["agent_swap"])
        swap["nominated_pairs"] = [
            {"buy_symbol": "2317.TW", "sell_symbol": "2330.TW", "purpose": "quality_dip", "size": "small"},
            {"buy_symbol": "2412.TW", "sell_symbol": "1101.TW", "purpose": "thesis", "size": "small"},
        ]
        sizing["agent_swap"] = swap
        engine.policy["position_sizing"] = sizing
        self.assertEqual(engine._high_risk_exempt_buys(), {"2317.TW"})
        cancelled = []
        kept = engine._high_risk_swap_filter(
            list(TWO_PAIRS), {}, Decimal("1000000"), lambda buy, sell, reason: cancelled.append((buy, sell, reason)))
        self.assertEqual(kept, [("2317.TW", "2330.TW")])  # 撿便宜換入者不佔名額、不被取消
        self.assertEqual(cancelled, [("2412.TW", "1101.TW", "high_risk_cap")])

    def test_record_mode_changes_nothing_but_writes_what_would_be_cut(self):
        body = self.run_gate(gate(mode="record", entries=0))
        self.assertEqual(summary(body), summary(self.baseline))
        record = [f for f in body["constraint_flags"] if f["code"] == "HIGH_RISK_CAP_RECORD"]
        self.assertEqual(len(record), 1)
        self.assertIn("若開啟會縮", record[0]["detail"])
        self.assertIn("換股試算前會取消：買 2317.TW／賣 2330.TW", record[0]["detail"])  # 換股那一步的效果也看得到
        self.assertNotIn("AGENT_SWAP_CANCELLED:high_risk_cap", {f["code"] for f in body["constraint_flags"]})

    def test_missing_beta_or_atr_counts_as_high_risk(self):
        policy = copy.deepcopy(self.base)
        policy["position_sizing"]["high_risk_cap"] = gate(high=NOTHING_HIGH)
        policy["content_sha256"] = decision_policy_sha256(policy)
        rules = AllocationOrderEngine(self.bundle, policy)._high_risk_rules()
        self.assertIsNotNone(rules)
        self.assertTrue(rules[1]("9999.TW"))  # 世界裡沒有行情：缺值一律高風險
        self.assertFalse(rules[1]("2330.TW"))


class HighRiskCapPlainBuyTests(unittest.TestCase):
    """沒有換股提名、交易 Agent 直接買進的一般買單也受上限：由優先序最低者先縮。"""

    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="1736000", positions={"2330.TW": 1000}, **LIMITS)
        cls.intent, cls.base, cls.baseline, _ = v2_world(
            cls.bundle, four_symbol_items(), policy_fields=LIMITS, held_since={"2330.TW": "2026-09-01T06:00:00+00:00"})

    def run_gate(self, value):
        policy = copy.deepcopy(self.base)
        policy["position_sizing"]["high_risk_cap"] = value
        policy["content_sha256"] = decision_policy_sha256(policy)
        body = AllocationOrderEngine(self.bundle, policy).run(self.intent)
        self.assertEqual(ProposalValidator(self.bundle, policy, self.intent).validate(body), [])
        return body

    def test_weight_cap_shrinks_plain_buys(self):
        base_buys = {o["symbol"]: o["shares"] for o in self.baseline["order_proposal"]["orders"] if o["side"] == "buy"}
        self.assertTrue(base_buys)
        body = self.run_gate(gate(entries=5, weight="0.2"))
        buys = {o["symbol"]: o["shares"] for o in body["order_proposal"]["orders"] if o["side"] == "buy"}
        self.assertLess(sum(buys.values()), sum(base_buys.values()))
        self.assertTrue(any(f["code"] == "RISK_HIGH_RISK_CAP" for f in body["constraint_flags"]))
        self.assertTrue(all(buys.get(symbol, 0) <= shares for symbol, shares in base_buys.items()))  # 只縮不加
        self.assertEqual(
            {o["symbol"] for o in body["order_proposal"]["orders"] if o["side"] == "sell"},
            {o["symbol"] for o in self.baseline["order_proposal"]["orders"] if o["side"] == "sell"})  # 不碰賣單


class HighRiskCapPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="736000", positions={"2330.TW": 1000, "1101.TW": 1000}, **LIMITS)
        _, cls.base, _, _ = v2_world(cls.bundle, four_symbol_items(), pairs=TWO_PAIRS, policy_fields=LIMITS)

    def rejected(self, value, drop_exposure=False):
        policy = copy.deepcopy(self.base)
        policy["position_sizing"]["high_risk_cap"] = value
        if drop_exposure:
            policy["position_sizing"].pop("exposure_rotation")
        policy["content_sha256"] = decision_policy_sha256(policy)
        with self.assertRaises(DecisionToolError) as caught:
            AllocationOrderEngine(self.bundle, policy)
        return str(caught.exception)

    def test_missing_field_is_rejected(self):
        value = gate()
        value.pop("max_new_high_risk_entries")
        self.assertIn("max_new_high_risk_entries", self.rejected(value))

    def test_unknown_field_is_rejected(self):
        self.assertIn("high_risk_cap", self.rejected(dict(gate(), extra="1")))

    def test_bad_mode_and_version_are_rejected(self):
        self.assertIn("mode", self.rejected(dict(gate(), mode="shadow")))
        self.assertIn("version", self.rejected(dict(gate(), version="v2")))
        self.assertIn("scope", self.rejected(gate(scope="bullish")))

    def test_numbers_must_be_bounded_strings(self):
        self.assertIn("max_high_risk_weight", self.rejected(gate(weight="0")))
        self.assertIn("max_high_risk_weight", self.rejected(dict(gate(), max_high_risk_weight=0.15)))
        self.assertIn("atr_pct_at_least", self.rejected(gate(high={"beta_at_least": "1", "atr_pct_at_least": "2"})))
        self.assertIn("max_new_high_risk_entries", self.rejected(gate(entries=True)))
        self.assertIn("max_new_high_risk_entries", self.rejected(gate(entries=-1)))

    def test_missing_threshold_is_rejected(self):
        self.assertIn("beta_at_least", self.rejected(gate(high={"atr_pct_at_least": "0.06"})))

    def test_exposure_rotation_is_a_prerequisite(self):
        self.assertIn("exposure_rotation", self.rejected(gate(), drop_exposure=True))

    def test_strategy_reflection_cannot_touch_the_gate(self):
        self.assertIn("high_risk_cap", FORBIDDEN_PREFIXES)


if __name__ == "__main__":
    unittest.main()
