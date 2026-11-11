"""liquidity_fit v1（版本閘門）：Policy 驗證與配置引擎的買單流動性縮減。

沒有該欄位的舊 Policy 逐位元維持原行為；縮減只動買單、向下取整張、不足一張取消。
"""

import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from etf_agent.decision import AllocationOrderEngine
from etf_agent.decision import decision_policy_sha256
from etf_agent.decision.liquidity_fit import liquidity_fit_gate, liquidity_fit_limit, liquidity_fit_upstream

from test_agent_swap_policy import errors_for, sizing_with


def liquidity_errors(sizing):
    return [e for e in errors_for(sizing) if "liquidity_fit" in e]


def with_gate(**changes):
    sizing = sizing_with("v2")
    sizing["liquidity_fit"] = dict({"version": "v1", "max_participation": "0.10"}, **changes)
    return sizing


class GateTests(unittest.TestCase):
    def test_valid_gate_and_absent_gate(self):
        self.assertEqual(liquidity_errors(with_gate()), [])
        self.assertEqual(liquidity_fit_limit(with_gate()), Decimal("0.10"))
        self.assertIsNone(liquidity_fit_gate(sizing_with("v2")))
        self.assertIsNone(liquidity_fit_limit(sizing_with("v2")))

    def test_v2_is_accepted_and_only_v2_is_upstream(self):
        self.assertEqual(liquidity_errors(with_gate(version="v2")), [])
        self.assertTrue(liquidity_fit_upstream(with_gate(version="v2")))
        self.assertFalse(liquidity_fit_upstream(with_gate()))
        self.assertFalse(liquidity_fit_upstream(sizing_with("v2")))

    def test_bad_values_are_rejected(self):
        for label, changes in {
            "版本值域外": {"version": "v3"}, "0": {"max_participation": "0"}, "大於 1": {"max_participation": "1.5"},
            "非字串": {"max_participation": 0.1}, "布林": {"max_participation": True}, "非數字": {"max_participation": "abc"},
        }.items():
            self.assertTrue(liquidity_errors(with_gate(**changes)), label)

    def test_number_is_required_and_unknown_fields_rejected(self):
        sizing = with_gate()
        del sizing["liquidity_fit"]["max_participation"]
        self.assertTrue(liquidity_errors(sizing))
        sizing = with_gate()
        sizing["liquidity_fit"]["extra"] = 1
        self.assertTrue(liquidity_errors(sizing))
        sizing = with_gate()
        sizing["liquidity_fit"] = "v1"
        self.assertTrue(liquidity_errors(sizing))


def fake_order(symbol, side, shares, intent, effective):
    gross = Decimal(shares) * Decimal("10")
    return {"symbol": symbol, "side": side, "shares": shares, "intent": intent, "gross_amount": str(gross),
            "net_cash_impact": str(-gross if side == "buy" else gross)}


class FitLiquidityTests(unittest.TestCase):
    def engine(self, sizing):
        engine = AllocationOrderEngine.__new__(AllocationOrderEngine)
        engine.policy = {"position_sizing": sizing}
        engine.context = SimpleNamespace(bundle={})
        engine._order = fake_order
        return engine

    def run_fit(self, sizing, orders, volumes):
        items = [{"symbol": s, "average_volume_20d": v} for s, v in volumes.items()]
        with mock.patch("etf_agent.decision.allocation.MomentumEngine") as momentum:
            momentum.return_value.run.return_value = {"items": items}
            return self.engine(sizing)._fit_liquidity(orders, Decimal("0"), {"lot_size": 1000})

    def test_without_the_gate_orders_are_returned_unchanged(self):
        orders = [fake_order("A.TW", "buy", 900000, "buy", {})]
        fitted, cash, flags = self.run_fit({}, orders, {"A.TW": "100000"})
        self.assertIs(fitted, orders)
        self.assertEqual((cash, flags), (Decimal("0"), []))

    def test_oversized_buy_is_shrunk_to_whole_lots_and_cash_is_returned(self):
        orders = [fake_order("A.TW", "buy", 900000, "buy", {}), fake_order("B.TW", "sell", 900000, "exit", {})]
        fitted, cash, flags = self.run_fit(with_gate(), orders, {"A.TW": "123456", "B.TW": "1000"})
        buy = next(o for o in fitted if o["side"] == "buy")
        self.assertEqual(buy["shares"], 12000)  # 0.10 × 123456 = 12345.6 → 12 張
        self.assertEqual(buy["intent"], "buy")
        self.assertEqual(next(o for o in fitted if o["side"] == "sell")["shares"], 900000)  # 賣單不動
        self.assertEqual(cash, Decimal("9000000") - Decimal("120000"))
        self.assertEqual([f["code"] for f in flags], ["LIQUIDITY_LIMIT"])

    def test_below_one_lot_cancels_the_buy(self):
        fitted, cash, flags = self.run_fit(with_gate(), [fake_order("A.TW", "buy", 5000, "buy", {})], {"A.TW": "5000"})
        self.assertEqual(fitted, [])
        self.assertEqual(cash, Decimal("50000"))
        self.assertEqual(flags[0]["code"], "LIQUIDITY_LIMIT")

    def test_buy_within_the_limit_is_untouched_and_missing_volume_is_flagged_not_cut(self):
        within = fake_order("A.TW", "buy", 10000, "buy", {})
        unknown = fake_order("B.TW", "buy", 10000, "add", {})
        fitted, cash, flags = self.run_fit(with_gate(), [within, unknown], {"A.TW": "100000", "B.TW": None})
        self.assertEqual({o["symbol"]: o["shares"] for o in fitted}, {"A.TW": 10000, "B.TW": 10000})
        self.assertEqual([f["code"] for f in flags], ["LIQUIDITY_UNAVAILABLE"])
        self.assertEqual(cash, Decimal("0"))


class UpstreamCapsTests(unittest.TestCase):
    def caps(self, sizing, items):
        engine = AllocationOrderEngine.__new__(AllocationOrderEngine)
        engine.policy = {"position_sizing": sizing}
        engine.context = SimpleNamespace(bundle={})
        engine._liquidity_caps_cache = None
        with mock.patch("etf_agent.decision.allocation.MomentumEngine") as momentum:
            momentum.return_value.run.return_value = {"items": items}
            return engine._liquidity_caps({"lot_size": 1000})

    def test_v1_and_no_gate_have_no_upstream_caps(self):
        items = [{"symbol": "A.TW", "average_volume_20d": "100000"}]
        self.assertIsNone(self.caps(with_gate(), items))
        self.assertIsNone(self.caps(sizing_with("v2"), items))

    def test_v2_caps_are_whole_lots_and_missing_volume_is_left_out(self):
        items = [{"symbol": "a.tw", "average_volume_20d": "123456"}, {"symbol": "B.TW", "average_volume_20d": None},
                 {"symbol": "C.TW", "average_volume_20d": "0"}]
        self.assertEqual(self.caps(with_gate(version="v2"), items), {"A.TW": 12000})


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "artifacts" / "decisions" / "2026-11-12" / "decision-20261111T130851Z"


@unittest.skipUnless(RUN.exists(), "沒有 2026-11-12 的封存 run，略過")
class ArchivedRunV2Tests(unittest.TestCase):
    """同一天封存輸入重算（replay_reference_only）：v2 把流動性上限提前，釋出的預算重新分配，v1 只在最後縮減。"""

    @classmethod
    def setUpClass(cls):
        load = lambda name: json.loads((RUN / name).read_text())
        cls.bundle, cls.policy, cls.intent = load("decision_input.json"), load("policy.json"), load("intent.json")

    def run_with(self, version):
        policy = copy.deepcopy(self.policy)
        policy["position_sizing"]["agent_swap"]["entry_reserve"]["swap_priority"] = {"version": "v1"}
        policy["position_sizing"]["liquidity_fit"] = {"version": version, "max_participation": "0.10"}
        policy["content_sha256"] = decision_policy_sha256({k: v for k, v in policy.items() if k != "content_sha256"})
        return AllocationOrderEngine(self.bundle, policy).run(self.intent, set(), None, 0, None)

    def test_v2_keeps_the_buy_below_the_cap_and_redistributes_the_freed_budget(self):
        v1, v2 = self.run_with("v1"), self.run_with("v2")
        buys = lambda result: {o["symbol"]: o["shares"] for o in result["order_proposal"]["orders"] if o["side"] == "buy"}
        self.assertEqual(buys(v1)["8415.TWO"], buys(v2)["8415.TWO"])  # 兩者都縮到均量上限
        self.assertTrue(any(f["code"] == "LIQUIDITY_LIMIT" for f in v1["constraint_flags"]))
        self.assertFalse(any(f["code"] == "LIQUIDITY_LIMIT" for f in v2["constraint_flags"]))  # v2 在預算階段已封頂，不需最後縮減
        self.assertGreater(buys(v2)["2330.TW"], buys(v1)["2330.TW"])  # 釋出的預算流向其他買單
        self.assertLess(Decimal(v2["allocation_proposal"]["cash_weight"]), Decimal(v1["allocation_proposal"]["cash_weight"]))


if __name__ == "__main__":
    unittest.main()
