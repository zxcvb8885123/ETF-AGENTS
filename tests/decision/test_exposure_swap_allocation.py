"""Agent 主導曝險階段 2、3 的配置測試（position_sizing.agent_swap.exposure）：docs/agent_led_exposure_plan.md 第 3.2、3.6、6 節。

覆蓋：

1. v2（正式）：``start_rebuild``／``rebuild`` 時 ``purpose: exposure`` 的配對與 thesis 配對走同一套可行性檢查（成交一對、換出者賣單標
   exit＋AGENT_SWAP、換入者 AGENT_SWAP_FILL、交易 Agent 的意圖不改寫）；``keep_defensive`` 時 exposure 配對一對都不成交、
   換出者不賣、程式不補挑（留 ``AGENT_SWAP_CANCELLED:exposure_view``），thesis 配對不受影響。
2. 對數上限：``max_pairs_by_view`` 對映的對數只限制 exposure 配對，超過從清單尾端取消（``exposure_cap``）；清單先後決定誰先成交，
   所以排在尾端的高信心配對最先被捨棄；0 表示不取用；對映上限不得放寬現行的每次換股上限（Policy 驗證，見 test_exposure_swap.py）。
3. v1（影子）：配置完全不讀 exposure 配對，訂單、配置與旗標與沒有這些配對時逐位元相同。
4. 硬性界線不放寬：減碼日整個略過、換入者因換手取消、換出者被現金上限留住時都不會只賣不買；v2 未綁定 view 一律拒絕；沒有閘門時
   配置逐位元不變。

夾具沿用 test_agent_swap_allocation 的 ``v2_world``（先用一般配對跑一次取得交易決策與已綁定的 Policy，再把 purpose、exposure 子閘門與
exposure_view 閘門綁上去重跑）。
"""

import copy
import unittest

from etf_agent.decision import AllocationOrderEngine, DecisionToolError, ProposalValidator, decision_policy_sha256
from etf_agent.decision.exposure_swap import PURPOSE_EXPOSURE, PURPOSE_THESIS

from test_agent_swap_allocation import PAIR, flag_codes, four_symbol_bundle, four_symbol_items, summary, v2_world
from test_exposure_rotation import exposure_bundle
from test_exposure_rotation_e2e import codes, sold_and_bought
from test_market_index_facts import GATE as MARKET_INDEX_GATE
from test_risk_limit_gates import guard

CAPS = {"start_rebuild": 1, "rebuild": 2}
LIMITS = {"max_stock_weight": "0.2", "special_weight_limits": {symbol: "0.2" for symbol in ("1101.TW", "2317.TW", "2330.TW", "2412.TW")}}
TWO_PAIRS = [("2317.TW", "2330.TW"), ("2412.TW", "1101.TW")]


def bind_exposure(bundle, intent, policy, purposes, view="start_rebuild", version="v2", caps=None):
    """把 purpose、``agent_swap.exposure``（v2 另綁 view）與 exposure_view 閘門綁進已有 nominated_pairs 的 Policy，重跑配置並重建驗證。"""
    bound = copy.deepcopy(policy)
    sizing = bound["position_sizing"]
    swap = sizing["agent_swap"]
    swap["nominated_pairs"] = [dict(pair, purpose=purpose) for pair, purpose in zip(swap["nominated_pairs"], purposes)]
    gate = {"version": version}
    if version == "v2":
        gate.update(max_pairs_by_view=dict(caps or CAPS), view=view)
    swap["exposure"] = gate
    sizing["exposure_view"] = {"version": "v1"}
    sizing["market_index_facts"] = dict(MARKET_INDEX_GATE)
    bound["content_sha256"] = decision_policy_sha256(bound)
    body = AllocationOrderEngine(bundle, bound).run(intent)
    problems = ProposalValidator(bundle, bound, intent).validate(body)
    if problems:
        raise AssertionError(problems)
    return bound, body


def world(bundle, pairs, purposes, items=None, view="start_rebuild", version="v2", caps=None, **kwargs):
    intent, policy, plain, control = v2_world(bundle, items, pairs=pairs, **kwargs)
    bound, body = bind_exposure(bundle, intent, policy, purposes, view, version, caps)
    return intent, bound, body, plain


def agent_flags(body):
    return sorted((f["symbol"], f["code"]) for f in body["constraint_flags"] if f["code"].startswith("AGENT_SWAP"))


class FormalExposureTests(unittest.TestCase):
    def test_an_exposure_pair_is_executed_like_a_thesis_pair_when_the_view_allows_it(self):
        bundle = exposure_bundle()
        for view in ("start_rebuild", "rebuild"):
            with self.subTest(view):
                intent, policy, body, plain = world(bundle, [PAIR], [PURPOSE_EXPOSURE], view=view)
                self.assertEqual(summary(body), [("2317.TW", "buy", 3000, "buy"), ("2330.TW", "sell", 1000, "exit")])
                self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_FILL"), ("2330.TW", "AGENT_SWAP")])
                # 與 thesis 配對完全相同的訂單與配置；只有換出者旗標的說明多寫了目的。
                self.assertEqual(body["order_proposal"], plain["order_proposal"])
                self.assertEqual(body["allocation_proposal"], plain["allocation_proposal"])
                victim = next(f for f in body["constraint_flags"] if f["code"] == "AGENT_SWAP")
                self.assertIn("目的：曝險", victim["detail"])
                self.assertIn(view, victim["detail"])
                self.assertEqual(next(i for i in intent["items"] if i["symbol"] == "2330.TW")["intent"], "hold")  # 意圖不被改寫
                self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_keep_defensive_cancels_every_exposure_pair_and_nobody_is_sold(self):
        bundle = exposure_bundle()
        _, policy, body, plain = world(bundle, [PAIR], [PURPOSE_EXPOSURE], view="keep_defensive")
        self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_CANCELLED:exposure_view")])
        self.assertEqual(body["order_proposal"]["orders"], [])  # 換出者不賣，換入者不買，程式也不補挑
        self.assertEqual(sold_and_bought(body), (set(), set()))
        self.assertEqual(len(body["allocation_proposal"]["positions"]), 2)
        self.assertTrue(guard(bundle, policy, body)["passed"])
        self.assertTrue(sold_and_bought(plain)[0])  # 對照：同一對以 thesis 提名時會成交

    def test_keep_defensive_does_not_touch_thesis_pairs(self):
        bundle = four_symbol_bundle(cash="736000", positions={"2330.TW": 1000, "1101.TW": 1000}, **LIMITS)
        _, _, body, _ = world(
            bundle, TWO_PAIRS, [PURPOSE_THESIS, PURPOSE_EXPOSURE], four_symbol_items(), view="keep_defensive", policy_fields=LIMITS)
        self.assertEqual(agent_flags(body), [
            ("2317.TW", "AGENT_SWAP_FILL"), ("2330.TW", "AGENT_SWAP"), ("2412.TW", "AGENT_SWAP_CANCELLED:exposure_view")])
        sold, bought = sold_and_bought(body)
        self.assertEqual((sold, bought), ({"2330.TW"}, {"2317.TW"}))

    def test_without_a_free_slot_need_the_exposure_pair_is_unneeded_and_the_victim_stays(self):
        bundle = exposure_bundle(cash="836000", positions={"2330.TW": 1000})  # 只持一檔，換入者本來就拿得到名額
        from test_exposure_rotation import hold_item
        from test_gate_prerequisites import decision_item
        items = [decision_item("2317.TW", "buy", "high", adopt=["bull-2317-1"]), hold_item("2330.TW", "medium", adopt=["bull-2330-1", "bear-2330-1"]),
                 hold_item("1101.TW", "low", adopt=["bull-1101-1"])]
        _, _, body, _ = world(bundle, [PAIR], [PURPOSE_EXPOSURE], items, held_since={"2330.TW": "2026-09-01T06:00:00+00:00"})
        self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_UNNEEDED")])
        self.assertEqual(sold_and_bought(body), (set(), {"2317.TW"}))


class ExposureCapTests(unittest.TestCase):
    def setUp(self):
        self.bundle = four_symbol_bundle(cash="736000", positions={"2330.TW": 1000, "1101.TW": 1000}, **LIMITS)

    def run_pairs(self, pairs, view, caps=None, purposes=None):
        return world(
            self.bundle, pairs, purposes or [PURPOSE_EXPOSURE] * len(pairs), four_symbol_items(), view=view, caps=caps, policy_fields=LIMITS)

    def test_start_rebuild_takes_only_the_first_pair_and_the_tail_is_cancelled(self):
        _, _, body, _ = self.run_pairs(TWO_PAIRS, "start_rebuild")
        self.assertEqual(agent_flags(body), [
            ("2317.TW", "AGENT_SWAP_FILL"), ("2330.TW", "AGENT_SWAP"), ("2412.TW", "AGENT_SWAP_CANCELLED:exposure_cap")])
        self.assertEqual(sold_and_bought(body), ({"2330.TW"}, {"2317.TW"}))  # 1101 不賣、2412 不買

    def test_the_order_of_the_list_decides_who_goes_first(self):
        _, _, body, _ = self.run_pairs(TWO_PAIRS[::-1], "start_rebuild")
        self.assertEqual(sold_and_bought(body), ({"1101.TW"}, {"2412.TW"}))
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:exposure_cap"), agent_flags(body))

    def test_rebuild_takes_both_pairs_and_one_for_one_holds(self):
        _, _, body, _ = self.run_pairs(TWO_PAIRS, "rebuild")
        self.assertEqual(sold_and_bought(body), ({"2330.TW", "1101.TW"}, {"2317.TW", "2412.TW"}))
        self.assertEqual(len(flag_codes(body, "AGENT_SWAP_FILL")), 2)
        self.assertEqual(len(body["allocation_proposal"]["positions"]), 2)

    def test_a_zero_cap_means_no_exposure_pair_is_used(self):
        _, _, body, _ = self.run_pairs(TWO_PAIRS, "rebuild", caps={"start_rebuild": 0, "rebuild": 0})
        self.assertEqual(sorted(code for _, code in agent_flags(body)), ["AGENT_SWAP_CANCELLED:exposure_cap"] * 2)
        self.assertEqual(body["order_proposal"]["orders"], [])

    def test_the_cap_does_not_limit_thesis_pairs(self):
        _, _, body, _ = self.run_pairs(TWO_PAIRS, "start_rebuild", caps={"start_rebuild": 0, "rebuild": 0}, purposes=[PURPOSE_THESIS, PURPOSE_THESIS])
        self.assertEqual(sold_and_bought(body), ({"2330.TW", "1101.TW"}, {"2317.TW", "2412.TW"}))  # 兩對都成交（受 agent_swap.max_swaps_per_run 限制，不受曝險對數限制）

    def test_high_conviction_pairs_at_the_tail_are_the_first_to_be_dropped(self):
        # 清單：中、低信心持股的曝險配對在前、高信心持股在後；對數不夠（start_rebuild 只有 1 對）時先成交的一定是前面的。
        items = four_symbol_items()
        _, _, body, _ = world(self.bundle, TWO_PAIRS, [PURPOSE_EXPOSURE] * 2, items, view="start_rebuild", policy_fields=LIMITS)
        sold, _ = sold_and_bought(body)
        self.assertEqual(sold, {"2330.TW"})  # 2330 是 hold／medium，在清單前面；排在後面的 1101 被 exposure_cap 取消

    def test_a_mixed_list_counts_only_exposure_pairs_against_the_cap(self):
        # start_rebuild 的上限 1 對：一對 exposure 加一對 thesis 都成交；兩對都是 exposure 時第二對才被 exposure_cap 取消。
        _, _, body, _ = self.run_pairs(TWO_PAIRS, "start_rebuild", purposes=[PURPOSE_EXPOSURE, PURPOSE_THESIS])
        self.assertEqual(sold_and_bought(body), ({"2330.TW", "1101.TW"}, {"2317.TW", "2412.TW"}))
        self.assertFalse([code for _, code in agent_flags(body) if code.endswith("exposure_cap")])


class ShadowAndHardLimitTests(unittest.TestCase):
    def test_the_shadow_version_ignores_exposure_pairs_bit_for_bit(self):
        bundle = exposure_bundle()
        intent, policy, plain, _ = v2_world(bundle, pairs=[])
        for view in ("rebuild",):
            _, shadow = bind_exposure(bundle, intent, v2_world(bundle, pairs=[PAIR])[1], [PURPOSE_EXPOSURE], view, version="v1")
            for key in ("allocation_proposal", "order_proposal", "constraint_flags"):
                self.assertEqual(shadow[key], plain[key], key)
            self.assertFalse([code for code in codes(shadow) if code.startswith("AGENT_SWAP")])

    def test_the_shadow_version_still_uses_thesis_pairs_in_the_same_list(self):
        bundle = four_symbol_bundle(cash="736000", positions={"2330.TW": 1000, "1101.TW": 1000}, **LIMITS)
        intent, policy, _, _ = v2_world(bundle, four_symbol_items(), pairs=TWO_PAIRS, policy_fields=LIMITS)
        _, shadow = bind_exposure(bundle, intent, policy, [PURPOSE_THESIS, PURPOSE_EXPOSURE], version="v1")
        self.assertEqual(agent_flags(shadow), [("2317.TW", "AGENT_SWAP_FILL"), ("2330.TW", "AGENT_SWAP")])
        _, _, only_thesis, _ = world(bundle, TWO_PAIRS[:1], [PURPOSE_THESIS], four_symbol_items(), version="v1", policy_fields=LIMITS)
        self.assertEqual(only_thesis["order_proposal"], shadow["order_proposal"])

    def test_the_derisk_day_skips_exposure_pairs_too(self):
        bundle = exposure_bundle(cash="136000", positions={"2330.TW": 1000, "1101.TW": 7000})
        _, _, body, control = world(bundle, [PAIR], [PURPOSE_EXPOSURE], level="defensive", grade="lower")
        found = codes(body)
        self.assertIn("RISK_DRAWDOWN", found)
        self.assertIn("AGENT_SWAP_SKIPPED:derisk_day", found)
        self.assertFalse(found & {"AGENT_SWAP", "AGENT_SWAP_FILL", "AGENT_SWAP_CANCELLED:exposure_view"})
        self.assertEqual(body["order_proposal"]["orders"], control["order_proposal"]["orders"])

    def test_a_buyer_without_a_buy_order_cancels_the_exposure_pair_and_the_victim_is_not_sold(self):
        bundle = exposure_bundle()
        _, policy, body, _ = world(bundle, [PAIR], [PURPOSE_EXPOSURE], policy_fields={"max_turnover_rate": "0.20"})
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:turnover"), agent_flags(body))
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_the_cash_ceiling_keeps_the_victim_and_cancels_the_buyer(self):
        bundle = exposure_bundle(cash_weight_must_be_below="0.25")
        _, _, body, _ = world(
            bundle, [PAIR], [PURPOSE_EXPOSURE], policy_fields={"cash_weight_ceiling": "0.25", "max_turnover_rate": "0.45"})
        found = {(f["symbol"], f["code"]) for f in body["constraint_flags"]}
        self.assertIn(("2330.TW", "RISK_CASH_CEILING"), found)
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:victim_kept"), found)
        self.assertEqual(body["order_proposal"]["orders"], [])

    def test_an_unbound_view_is_refused_instead_of_running_as_keep_defensive(self):
        bundle = exposure_bundle()
        intent, policy, _, _ = v2_world(bundle, pairs=[PAIR])
        bound, _ = bind_exposure(bundle, intent, policy, [PURPOSE_EXPOSURE])
        del bound["position_sizing"]["agent_swap"]["exposure"]["view"]
        bound["content_sha256"] = decision_policy_sha256(bound)
        with self.assertRaises(DecisionToolError):
            AllocationOrderEngine(bundle, bound).run(intent)

    def test_a_pair_without_purpose_is_refused_when_the_gate_is_on(self):
        bundle = exposure_bundle()
        intent, policy, _, _ = v2_world(bundle, pairs=[PAIR])
        bound, _ = bind_exposure(bundle, intent, policy, [PURPOSE_EXPOSURE])
        del bound["position_sizing"]["agent_swap"]["nominated_pairs"][0]["purpose"]
        bound["content_sha256"] = decision_policy_sha256(bound)
        with self.assertRaises(DecisionToolError):
            AllocationOrderEngine(bundle, bound)

    def test_without_the_exposure_gate_the_allocation_is_unchanged(self):
        bundle = exposure_bundle()
        _, _, plain, _ = v2_world(bundle, pairs=[PAIR])
        self.assertEqual(summary(plain), [("2317.TW", "buy", 3000, "buy"), ("2330.TW", "sell", 1000, "exit")])
        self.assertNotIn("目的：曝險", " ".join(f["detail"] for f in plain["constraint_flags"]))


if __name__ == "__main__":
    unittest.main()
