"""agent_swap.proceeds_to_add（版本閘門 v1）：換股成立時，換出者賣款可流向高信心加碼持股。

覆蓋 ``_run_exposure`` 迴圈的控制流程（用假的配置結果驅動，避免依賴很難重現的額度分配細節）：

1. 換入者沒有買單、無縮減旗標，且有高信心加碼持股真的買進：該對不取消、重算一次，換入者改標 AGENT_SWAP_PROCEEDS_TO_ADD。
2. 沒有高信心加碼持股買進：照舊取消（AGENT_SWAP_CANCELLED），換出者不賣，不會只賣不買。
3. 沒有閘門：行為與舊版相同（照舊取消）。
4. 閘門形狀與前提：只允許 {"version": "v1"}，需要 agent_swap v2。
"""

import copy
import unittest

from etf_agent.decision import AllocationOrderEngine, decision_policy_sha256
from etf_agent.decision.sizing import AGENT_SWAP_FIELDS, PROCEEDS_TO_ADD, proceeds_to_add_enabled, validate_position_sizing

from test_agent_swap_allocation import PAIR, four_symbol_bundle, v2_world
from test_exposure_rotation import hold_item
from test_exposure_swap_allocation import LIMITS
from test_gate_prerequisites import decision_item

BUYER, VICTIM, ADD = "2317.TW", "1101.TW", "2330.TW"


def items(add_conviction="high"):
    return [
        decision_item(ADD, "add", add_conviction, adopt=["bull-2330-1"]),
        hold_item(VICTIM, "low", adopt=["bull-1101-1"]),
        decision_item(BUYER, "buy", "medium", adopt=["bull-2317-1"]),
        hold_item("2412.TW", "low", adopt=["bull-1101-1"]),
    ]


def body_with(buys, fill=True):
    flags = [{"symbol": VICTIM, "code": "AGENT_SWAP", "detail": "x"}]
    flags.append({"symbol": BUYER, "code": "AGENT_SWAP_FILL", "detail": "x"})
    return {
        "constraint_flags": flags,
        "order_proposal": {"orders": [{"symbol": symbol, "side": "buy", "shares": 1000} for symbol in buys]},
    }


def clean_body():
    """取消該對之後的結果：沒有換入者旗標，迴圈結束。"""
    return {"constraint_flags": [], "order_proposal": {"orders": []}}


class Driver:
    """換掉 ``_run``：記錄每一輪收到的狀態，依腳本回傳假結果。"""

    def __init__(self, gate, add_conviction="high"):
        bundle = four_symbol_bundle(cash="336000", positions={ADD: 1000, VICTIM: 5000}, **LIMITS)
        intent, policy, _, _ = v2_world(bundle, items(add_conviction), pairs=[(BUYER, VICTIM)], policy_fields=LIMITS,
                                        level="neutral", grade="lower")
        policy = copy.deepcopy(policy)
        if gate:
            policy["position_sizing"]["agent_swap"]["proceeds_to_add"] = dict(PROCEEDS_TO_ADD)
            policy["content_sha256"] = decision_policy_sha256(policy)
        self.engine = AllocationOrderEngine(bundle, policy)
        self.intent = intent
        self.calls = []

    def run(self, bodies):
        queue = list(bodies)

        def fake(intent_result, excluded, overrides, revision_count, parent, kept_exits, dropped, exposure_excluded, agent_cancelled):
            self.calls.append({"accepted": set(self.engine._proceeds_accepted), "cancelled": dict(agent_cancelled or {})})
            return queue.pop(0)

        self.engine._run = fake
        return self.engine._run_exposure(self.intent, None, None, 0, None, {}, {})


class ProceedsToAddLoopTests(unittest.TestCase):
    def test_the_pair_is_kept_when_a_high_conviction_add_is_actually_bought(self):
        driver = Driver(gate=True)
        final = driver.run([body_with([ADD]), body_with([ADD])])
        self.assertEqual(len(driver.calls), 2)
        self.assertEqual(driver.calls[0]["accepted"], set())
        self.assertEqual(driver.calls[1]["accepted"], {BUYER})
        self.assertEqual(driver.calls[1]["cancelled"], {})
        self.assertIsNotNone(final)

    def test_the_pair_is_cancelled_when_no_high_conviction_add_is_bought(self):
        driver = Driver(gate=True)
        driver.run([body_with([]), clean_body()])
        self.assertEqual(driver.calls[1]["cancelled"], {BUYER: "no_buy_order"})
        self.assertEqual(driver.calls[1]["accepted"], set())

    def test_a_medium_conviction_add_does_not_qualify(self):
        driver = Driver(gate=True, add_conviction="medium")
        driver.run([body_with([ADD]), clean_body()])
        self.assertEqual(driver.calls[1]["cancelled"], {BUYER: "no_buy_order"})

    def test_an_accepted_pair_is_cancelled_if_the_funding_disappears_on_the_next_pass(self):
        driver = Driver(gate=True)
        driver.run([body_with([ADD]), body_with([]), clean_body()])
        self.assertEqual(driver.calls[2]["cancelled"], {BUYER: "no_buy_order"})
        self.assertEqual(driver.calls[2]["accepted"], set())

    def test_without_the_gate_the_old_behaviour_cancels_the_pair(self):
        driver = Driver(gate=False)
        driver.run([body_with([ADD]), clean_body()])
        self.assertEqual(driver.calls[1]["cancelled"], {BUYER: "no_buy_order"})
        self.assertEqual(driver.calls[1]["accepted"], set())


class ProceedsToAddGateTests(unittest.TestCase):
    def test_shape_and_prerequisite(self):
        self.assertIn("proceeds_to_add", AGENT_SWAP_FIELDS)
        self.assertFalse(proceeds_to_add_enabled({}))
        self.assertTrue(proceeds_to_add_enabled({"agent_swap": {"proceeds_to_add": {"version": "v1"}}}))
        bundle = four_symbol_bundle(cash="336000", positions={ADD: 1000, VICTIM: 5000}, **LIMITS)
        _, policy, _, _ = v2_world(bundle, items(), pairs=[(BUYER, VICTIM)], policy_fields=LIMITS, level="neutral", grade="lower")
        symbols = ["2330.TW", "1101.TW", "2317.TW", "2412.TW"]
        for value, version, ok in (({"version": "v1"}, "v2", True), ({"version": "v2"}, "v2", False), ({"version": "v1"}, "v1", False)):
            with self.subTest(value=value, version=version):
                candidate = copy.deepcopy(policy)
                candidate["position_sizing"]["agent_swap"]["proceeds_to_add"] = value
                candidate["position_sizing"]["agent_swap"]["version"] = version
                errors = []
                validate_position_sizing(candidate, symbols, errors)
                self.assertEqual(any("proceeds_to_add" in error for error in errors), not ok, errors)


if __name__ == "__main__":
    unittest.main()
