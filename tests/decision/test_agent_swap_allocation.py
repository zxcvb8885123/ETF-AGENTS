"""換股改由交易 Agent 提名（position_sizing.agent_swap v2）的配置測試：docs/agent_nominated_rotation_plan.md 2.6 B 段與 5.1。

覆蓋：

1. 正常：成交一對（換出者賣單標 exit＋AGENT_SWAP、換入者 AGENT_SWAP_FILL、交易 Agent 的意圖不被改寫、曝險換股留
   ``EXPOSURE_ROTATION_SKIPPED:agent_swap``）、等價測試（與提名相同人選時，v2 的訂單與舊曝險換股路徑相同、只有旗標代碼不同）、
   換入者本來就拿得到名額（AGENT_SWAP_UNNEEDED、換出者不賣）、依清單先後取用與尾端 swap_cap、影子 v1 逐位不變。
2. 取消與降級（每一項都確認「不會只賣不買」）：換入者因現金／單股／產業／換手沒有買單、換出者被 RISK_CASH_CEILING 留住、
   Active Share 取消換入者、風險審查修正循環以 override／excluded 重跑、減碼日整個略過、提名 exhausted／unavailable、
   換出者不再是 hold。
3. 舊路徑不變：沒有 agent_swap 的 Policy 與 v1 影子的配置逐位相同；v2 未綁定提名一律拒絕。
4. 重複實作已合併：``untradable_symbols``、成交價／手續費／證交稅函式由配置與提名估計共用。

夾具：三檔世界（2330 持 1000 股約 16%、1101 持 5000 股約 50%、現金 33.6%、max_positions=2；2317 買進被名額擋下）沿用
test_exposure_rotation 的夾具；兩對提名需要第四檔（2412），由 ``four_symbol_bundle`` 在同一世界加一檔新股。
"""

import copy
import unittest
from decimal import Decimal

from etf_agent.decision import AllocationOrderEngine, DecisionToolError, ProposalValidator, decision_bundle_sha256, decision_policy_sha256
from etf_agent.decision.allocation import commission_fee, execution_price, sell_tax
from etf_agent.decision.contracts import untradable_symbols
from etf_agent.decision.sizing import agent_swap_binding
from etf_agent.core import canonical_sha256

from test_cash_stance_turn_facts import GATE as TURN_FACTS_GATE
from test_exposure_rotation import decisions, exposure_bundle, hold_item
from test_exposure_rotation_e2e import allocate, codes, sold_and_bought
from test_exposure_rotation_v3 import gate_v3
from test_gate_prerequisites import decision_item
from test_rebound_participation_v2 import REBOUND_V2
from test_risk_limit_gates import guard, orders

AGENT_SWAP = {"version": "v2", "max_swaps_per_run": 2, "min_holding_days": 5}
PAIR = ("2317.TW", "2330.TW")


def bind_pairs(sizing, pairs, status="completed", version="v2"):
    sizing["agent_swap"] = dict(AGENT_SWAP, version=version)
    if version == "v2":
        sizing["agent_swap"].update(
            nominated_pairs=[{"buy_symbol": buy, "sell_symbol": sell} for buy, sell in pairs],
            nomination_ref={"nomination_id": "nom-1", "sha256": "a" * 64, "status": status},
        )


def v2_world(bundle, items=None, pairs=(), status="completed", version="v2", level="neutral", grade="middle", policy_fields=None,
             sector_1101="水泥", rotation_max=None, held_since=None):
    """用既有三檔世界產生交易決策與綁定好的 Policy，再把它改成「agent_swap＋rebound v2＋turn_facts」的樣子（與 test_rebound_participation 的
    ``rebind`` 同一手法）。回傳 (intent, policy, body, control_body)：body 是 v2（或 v1）配置，control_body 是原 Policy（曝險換股 v3）的配置。"""
    gate = gate_v3(defensive=None, **({"held_since_by_symbol": held_since} if held_since else {}))
    intent, policy, control = allocate(bundle, items, level, grade, gate, policy_fields=policy_fields, sector_1101=sector_1101)
    bound = copy.deepcopy(policy)
    sizing = bound["position_sizing"]
    rebound = copy.deepcopy(REBOUND_V2)
    if rotation_max is not None:
        sizing["rotation"]["max_swaps_per_run"] = rotation_max
        for row in rebound["by_state"].values():
            row["max_swaps_per_run"] = min(int(row["max_swaps_per_run"]), rotation_max)
    if version == "v2":
        sizing["rotation"].pop("forced_swap", None)  # v2 與舊換股路徑互斥
        sizing["exposure_rotation"].pop("conviction_gap_swap", None)
    sizing["cash_stance_turn_facts"] = dict(TURN_FACTS_GATE)
    sizing["rebound_participation"] = dict(rebound, effective_state="none")
    bind_pairs(sizing, pairs, status, version)
    bound["content_sha256"] = decision_policy_sha256(bound)
    body = AllocationOrderEngine(bundle, bound).run(intent)
    problems = ProposalValidator(bundle, bound, intent).validate(body)
    if problems:
        raise AssertionError(problems)
    return intent, bound, body, control


def four_symbol_bundle(**kwargs):
    """三檔世界再加一檔 2412.TW（價格 100，行情複製 1101），供兩對提名使用。"""
    bundle = exposure_bundle(**kwargs)
    snapshot = bundle["snapshot"]
    series = copy.deepcopy(next(item for item in bundle["price_series"] if item["symbol"] == "1101.TW"))
    series.update(symbol="2412.TW", evidence_id="price-2412")
    bundle["price_series"].append(series)
    snapshot["source_evidence"].append({"evidence_id": "price-2412", "source": "FIXTURE", "authority": "vendor", "data_type": "daily_price"})
    snapshot["latest_prices"].append({
        "symbol": "2412.TW", "trade_date": "2026-09-19", "analysis_close_price": "100", "close_price": "100",
        "source_evidence_id": "price-2412",
    })
    snapshot.update(universe_size=4, tradable_symbols=sorted(snapshot["tradable_symbols"] + ["2412.TW"]))
    bundle["snapshot_sha256"] = canonical_sha256(snapshot)
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def four_symbol_items():
    return [
        decision_item("2412.TW", "buy", "high", adopt=["bull-2317-1"]),
        hold_item("1101.TW", "low", adopt=["bull-1101-1"]),
        decision_item("2317.TW", "buy", "high", adopt=["bull-2317-1"]),
        hold_item("2330.TW", "medium", adopt=["bull-2330-1", "bear-2330-1"]),
    ]


def summary(body):
    return sorted((o["symbol"], o["side"], o["shares"], o["intent"]) for o in body["order_proposal"]["orders"])


def flag_codes(body, prefix):
    return sorted((f["symbol"], f["code"]) for f in body["constraint_flags"] if f["code"].startswith(prefix))


class NormalSwapTests(unittest.TestCase):
    def test_one_pair_is_executed_without_rewriting_the_intent(self):
        bundle = exposure_bundle()
        intent, policy, body, _ = v2_world(bundle, pairs=[PAIR])
        self.assertEqual(summary(body), [("2317.TW", "buy", 3000, "buy"), ("2330.TW", "sell", 1000, "exit")])
        self.assertEqual(
            flag_codes(body, "AGENT_SWAP"), [("2317.TW", "AGENT_SWAP_FILL"), ("2330.TW", "AGENT_SWAP")])
        self.assertIn(("PORTFOLIO", "EXPOSURE_ROTATION_SKIPPED:agent_swap"), {(f["symbol"], f["code"]) for f in body["constraint_flags"]})
        self.assertFalse(codes(body) & {"RISK_EXPOSURE_ROTATION", "EXPOSURE_ROTATION_FILL", "RISK_FORCED_SWAP"})
        self.assertEqual(next(i for i in intent["items"] if i["symbol"] == "2330.TW")["intent"], "hold")  # 交易 Agent 的意圖沒被改寫
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_equivalence_with_the_old_exposure_path_when_the_nomination_picks_the_same_people(self):
        """等價測試（replay_reference_only）：提名與舊曝險換股會選的人相同時，訂單與配置相同，只有旗標代碼不同。"""
        bundle = exposure_bundle()
        _, _, body, control = v2_world(bundle, pairs=[PAIR])
        self.assertIn("RISK_EXPOSURE_ROTATION", codes(control))
        self.assertEqual(body["order_proposal"]["orders"], control["order_proposal"]["orders"])
        self.assertEqual(body["allocation_proposal"], control["allocation_proposal"])
        self.assertEqual(body["order_proposal"]["turnover_rate"], control["order_proposal"]["turnover_rate"])

    def test_a_buyer_that_already_has_a_slot_does_not_use_the_pair_and_the_victim_is_not_sold(self):
        bundle = exposure_bundle(cash="836000", positions={"2330.TW": 1000})  # 只持一檔，還有空名額
        items = [decision_item("2317.TW", "buy", "high", adopt=["bull-2317-1"]), hold_item("2330.TW", "medium", adopt=["bull-2330-1", "bear-2330-1"]),
                 hold_item("1101.TW", "low", adopt=["bull-1101-1"])]
        _, _, body, _ = v2_world(bundle, items, pairs=[PAIR], held_since={"2330.TW": "2026-09-01T06:00:00+00:00"})
        self.assertIn(("2317.TW", "AGENT_SWAP_UNNEEDED"), flag_codes(body, "AGENT_SWAP"))
        self.assertEqual(flag_codes(body, "AGENT_SWAP"), [("2317.TW", "AGENT_SWAP_UNNEEDED")])
        sold, bought = sold_and_bought(body)
        self.assertEqual((sold, bought), (set(), {"2317.TW"}))  # 本來就買得到的新股不多賣一檔

    def test_pairs_are_used_in_the_agents_order_and_the_tail_is_cancelled_with_swap_cap(self):
        bundle = four_symbol_bundle()
        pairs = [("2317.TW", "2330.TW"), ("2412.TW", "1101.TW")]
        # 兩個空名額都不夠：rotation 上限只有 1 個換股名額（rebound 的換股檔數同步降為 1，Policy 驗證要求不得高於 rotation）。
        _, _, body, _ = v2_world(bundle, four_symbol_items(), pairs, rotation_max=1)
        self.assertEqual({(f["symbol"], f["code"]) for f in body["constraint_flags"] if f["code"].startswith("AGENT_SWAP")}, {
            ("2330.TW", "AGENT_SWAP"), ("2317.TW", "AGENT_SWAP_FILL"), ("2412.TW", "AGENT_SWAP_CANCELLED:swap_cap"),
        })
        sold, bought = sold_and_bought(body)
        self.assertEqual((sold, bought), ({"2330.TW"}, {"2317.TW"}))  # 先取清單前面的一對，尾端取消、1101 不賣
        # 換回順序：同一份提名反過來，取用的就是另一對。
        _, _, reversed_body, _ = v2_world(bundle, four_symbol_items(), pairs[::-1], rotation_max=1)
        sold, bought = sold_and_bought(reversed_body)
        self.assertEqual((sold, bought), ({"1101.TW"}, {"2412.TW"}))
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:swap_cap"), flag_codes(reversed_body, "AGENT_SWAP"))

    def test_two_pairs_are_both_executed_when_the_slots_and_turnover_allow(self):
        limits = {"max_stock_weight": "0.2", "special_weight_limits": {symbol: "0.2" for symbol in ("1101.TW", "2317.TW", "2330.TW", "2412.TW")}}
        bundle = four_symbol_bundle(cash="736000", positions={"2330.TW": 1000, "1101.TW": 1000}, **limits)
        pairs = [("2317.TW", "2330.TW"), ("2412.TW", "1101.TW")]
        _, _, body, _ = v2_world(bundle, four_symbol_items(), pairs, policy_fields=limits)
        sold, bought = sold_and_bought(body)
        self.assertEqual((sold, bought), ({"2330.TW", "1101.TW"}, {"2317.TW", "2412.TW"}))
        self.assertEqual(len(flag_codes(body, "AGENT_SWAP_FILL")), 2)
        self.assertEqual(len(body["allocation_proposal"]["positions"]), 2)  # 一換一，檔數不超過上限

    def test_the_earlier_pair_is_cancelled_by_turnover_but_the_rest_still_executes_one_for_one(self):
        # 兩對的賣單加買單超過 100% 換手上限：復原迴圈取消換入者沒有買單的那一對（turnover），另一對照常成交，每筆賣單仍有對應買單。
        bundle = four_symbol_bundle()
        pairs = [("2317.TW", "2330.TW"), ("2412.TW", "1101.TW")]
        _, _, body, _ = v2_world(bundle, four_symbol_items(), pairs)
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:turnover"), flag_codes(body, "AGENT_SWAP"))
        sold, bought = sold_and_bought(body)
        self.assertEqual((sold, bought), ({"1101.TW"}, {"2412.TW"}))
        self.assertEqual(len(body["allocation_proposal"]["positions"]), 2)

    def test_not_needed_and_empty_nominations_leave_no_trace_and_equal_the_no_nomination_result(self):
        bundle = exposure_bundle()
        for status in ("not_needed", "completed"):
            with self.subTest(status):
                _, _, body, _ = v2_world(bundle, pairs=[], status=status)
                self.assertEqual(flag_codes(body, "AGENT_SWAP"), [])
                self.assertEqual(sold_and_bought(body), (set(), set()))  # 提名為空就不換，程式不補挑
                self.assertIn(("PORTFOLIO", "EXPOSURE_ROTATION_SKIPPED:agent_swap"), {(f["symbol"], f["code"]) for f in body["constraint_flags"]})

    def test_shadow_v1_allocation_is_bit_for_bit_the_same_as_without_the_gate(self):
        bundle = exposure_bundle()
        _, _, body, control = v2_world(bundle, pairs=[], version="v1")
        for key in ("allocation_proposal", "order_proposal", "constraint_flags"):
            self.assertEqual(body[key], control[key], key)
        self.assertFalse([code for _, code in flag_codes(body, "AGENT_SWAP")])
        self.assertFalse([code for code in codes(body) if code.endswith("SKIPPED:agent_swap")])


class CancellationTests(unittest.TestCase):
    def assertNeverSellOnly(self, body):
        sold, bought = sold_and_bought(body)
        victims = {f["symbol"] for f in body["constraint_flags"] if f["code"] == "AGENT_SWAP"}
        fills = {f["symbol"] for f in body["constraint_flags"] if f["code"] == "AGENT_SWAP_FILL"}
        self.assertLessEqual(victims, sold)
        self.assertLessEqual(fills, bought)
        self.assertEqual(len(victims), len(fills))
        if not bought:
            self.assertEqual(victims, set())

    def test_a_buyer_without_a_buy_order_cancels_the_pair_and_the_victim_is_not_sold(self):
        cases = [  # (標籤, bundle 規則, policy 欄位, 1101 產業, 取消原因)
            ("換手", {}, {"max_turnover_rate": "0.20"}, "水泥", "turnover"),
            ("產業", {"max_sector_weight": "0.55"}, {"max_sector_weight": "0.55"}, "電子製造", "sector_fit"),
        ]
        for label, rules, fields, sector, reason in cases:
            with self.subTest(label):
                bundle = exposure_bundle(**rules)
                _, policy, body, _ = v2_world(bundle, pairs=[PAIR], policy_fields=fields, sector_1101=sector)
                self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:%s" % reason), flag_codes(body, "AGENT_SWAP"))
                self.assertEqual(body["order_proposal"]["orders"], [])  # 換出者不賣
                self.assertNeverSellOnly(body)
                self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_a_victim_that_is_kept_by_the_cash_ceiling_cancels_the_buyer_too(self):
        # 換手上限 45% 把買單縮到 2000 股：換股後現金比例（約 27.6%）達 25% 的競賽上限。程式優先留住換出者（RISK_CASH_CEILING），
        # 換入者同步取消，不會留下只買不賣（也不會多出一檔而超過持股上限）。
        bundle = exposure_bundle(cash_weight_must_be_below="0.25")
        _, _, body, _ = v2_world(bundle, pairs=[PAIR], policy_fields={"cash_weight_ceiling": "0.25", "max_turnover_rate": "0.45"})
        found = {(f["symbol"], f["code"]) for f in body["constraint_flags"]}
        self.assertIn(("2330.TW", "RISK_CASH_CEILING"), found)
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:victim_kept"), found)
        self.assertFalse({code for _, code in found} & {"AGENT_SWAP", "AGENT_SWAP_FILL"})
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertNeverSellOnly(body)
        self.assertEqual(len(body["allocation_proposal"]["positions"]), 2)  # 最終檔數不超過上限
        # 對照：沒有現金上限壓力時同一份提名照常成交一對（上面的取消確實來自留住換出者）。
        bundle = exposure_bundle()
        _, _, plain, _ = v2_world(bundle, pairs=[PAIR], policy_fields={"max_turnover_rate": "0.45"})
        self.assertIn(("2330.TW", "AGENT_SWAP"), flag_codes(plain, "AGENT_SWAP"))

    def test_active_share_cancelling_the_buyer_cancels_the_pair(self):
        bundle = exposure_bundle()
        intent, policy, _, _ = v2_world(bundle, pairs=[PAIR])
        engine = AllocationOrderEngine(bundle, policy)
        body = engine._run_min_positions(intent, None, None, 0, None, {"2317.TW": "與 ETF 重疊"})
        found = {(f["symbol"], f["code"]) for f in body["constraint_flags"]}
        self.assertIn(("2317.TW", "RISK_ACTIVE_SHARE"), found)
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:active_share"), found)
        self.assertEqual(body["order_proposal"]["orders"], [])

    def test_review_revisions_recompute_the_same_rules_without_asking_the_agent_again(self):
        bundle = exposure_bundle()
        intent, policy, base, _ = v2_world(bundle, pairs=[PAIR])
        engine = AllocationOrderEngine(bundle, policy)
        self.assertEqual(engine.run(intent), base)  # 確定性：同輸入同輸出
        excluded = engine.run(intent, excluded_symbols={"2317.TW"}, revision_count=1, parent_proposal_id="p")
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:risk_excluded"), flag_codes(excluded, "AGENT_SWAP"))
        self.assertEqual(excluded["order_proposal"]["orders"], [])
        tightened = engine.run(intent, overrides={"max_turnover_rate": "0.20"}, revision_count=1, parent_proposal_id="p")
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:turnover"), flag_codes(tightened, "AGENT_SWAP"))
        self.assertEqual(tightened["order_proposal"]["orders"], [])
        for revised in (excluded, tightened):
            self.assertEqual(ProposalValidator(bundle, policy, intent).validate(revised), [])  # 重建驗證接受

    def test_derisk_day_skips_the_nomination_entirely(self):
        bundle = exposure_bundle(cash="136000", positions={"2330.TW": 1000, "1101.TW": 7000})
        _, _, body, control = v2_world(bundle, pairs=[PAIR], level="defensive", grade="lower")
        found = codes(body)
        self.assertIn("RISK_DRAWDOWN", found)
        self.assertIn("AGENT_SWAP_SKIPPED:derisk_day", found)
        self.assertFalse(found & {"AGENT_SWAP", "AGENT_SWAP_FILL", "AGENT_SWAP_UNNEEDED"})
        # 減碼日的賣單與買單全部是減碼本身與一般名額配對的結果，提名沒有改變任何訂單。
        self.assertEqual(body["order_proposal"]["orders"], control["order_proposal"]["orders"])

    def test_exhausted_and_unavailable_nominations_leave_a_skip_flag_and_change_nothing_else(self):
        bundle = exposure_bundle()
        _, _, empty, _ = v2_world(bundle, pairs=[], status="not_needed")
        for status in ("exhausted", "unavailable"):
            with self.subTest(status):
                _, _, body, _ = v2_world(bundle, pairs=[], status=status)
                self.assertIn(("PORTFOLIO", "AGENT_SWAP_SKIPPED:nomination_%s" % status), flag_codes(body, "AGENT_SWAP"))
                self.assertEqual(body["order_proposal"], empty["order_proposal"])
                self.assertEqual(body["allocation_proposal"], empty["allocation_proposal"])

    def test_a_victim_that_is_no_longer_hold_or_a_buyer_that_is_not_a_candidate_cancels_the_pair(self):
        bundle = exposure_bundle()
        items = [
            hold_item("1101.TW", "low", adopt=["bull-1101-1"]),
            decision_item("2317.TW", "buy", "high", adopt=["bull-2317-1"]),
            decision_item("2330.TW", "no_trade", None, adopt=["bull-2330-1"]),
        ]
        _, _, body, _ = v2_world(bundle, items, pairs=[PAIR])
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:victim_unavailable"), flag_codes(body, "AGENT_SWAP"))
        self.assertEqual(body["order_proposal"]["orders"], [])
        _, _, body, _ = v2_world(bundle, pairs=[("2412.TW", "2330.TW")])  # 2412 不在交易池，不是候選
        self.assertIn(("2412.TW", "AGENT_SWAP_CANCELLED:buy_unavailable"), flag_codes(body, "AGENT_SWAP"))
        self.assertEqual(body["order_proposal"]["orders"], [])


class PolicyBindingTests(unittest.TestCase):
    def test_an_unbound_v2_policy_is_refused_instead_of_running_without_nominations(self):
        bundle = exposure_bundle()
        intent, policy, _, _ = v2_world(bundle, pairs=[PAIR])
        unbound = copy.deepcopy(policy)
        for field in ("nominated_pairs", "nomination_ref"):
            unbound["position_sizing"]["agent_swap"].pop(field)
        unbound["content_sha256"] = decision_policy_sha256(unbound)
        with self.assertRaisesRegex(DecisionToolError, "尚未綁定換股提名"):
            AllocationOrderEngine(bundle, unbound).run(intent)

    def test_status_values_do_not_drift_between_sizing_and_the_nomination_module(self):
        from etf_agent.decision import swap_nomination
        from etf_agent.decision.sizing import NOMINATION_STATUSES

        self.assertEqual(NOMINATION_STATUSES, swap_nomination.STATUSES)

    def test_binding_reader(self):
        self.assertIsNone(agent_swap_binding(None))
        self.assertIsNone(agent_swap_binding({}))
        self.assertIsNone(agent_swap_binding({"agent_swap": dict(AGENT_SWAP, version="v1")}))  # 影子不讀提名
        sizing = {}
        bind_pairs(sizing, [PAIR])
        self.assertEqual(agent_swap_binding(sizing), ([PAIR], "completed"))

    def test_a_tampered_binding_changes_the_proposal_so_the_rebuild_rejects_a_stale_body(self):
        bundle = exposure_bundle()
        intent, policy, body, _ = v2_world(bundle, pairs=[PAIR])
        other = copy.deepcopy(policy)
        other["position_sizing"]["agent_swap"]["nominated_pairs"] = []
        other["content_sha256"] = decision_policy_sha256(other)
        self.assertTrue(ProposalValidator(bundle, other, intent).validate(body))  # 配對被改掉，舊配置不再通過重建


class SharedImplementationTests(unittest.TestCase):
    def test_untradable_symbols_is_one_function(self):
        import etf_agent.decision.swap_nomination as nomination

        bundle = exposure_bundle()
        self.assertIs(nomination.untradable_symbols, untradable_symbols)
        self.assertEqual(AllocationOrderEngine(bundle, v2_world(bundle, pairs=[])[1])._untradable(), untradable_symbols(bundle))

    def test_order_cost_formulas_are_shared_between_allocation_and_the_nomination_estimate(self):
        bundle = exposure_bundle()
        _, policy, _, _ = v2_world(bundle, pairs=[])
        engine = AllocationOrderEngine(bundle, policy)
        effective = engine._effective_policy({})
        for side in ("buy", "sell"):
            order = engine._order("2317.TW", side, 3000, "buy", effective)
            price = execution_price(engine.prices["2317.TW"], side, effective["slippage_bps"])
            gross = price * 3000
            self.assertEqual(order["estimated_price"], str(price.quantize(Decimal("0.01"))))
            self.assertEqual(order["commission"], str(commission_fee(gross, effective["commission_rate"], effective["minimum_commission"])))
            self.assertEqual(order["tax"], str(sell_tax(gross, side, effective["sell_tax_rate"])))

    def test_the_nomination_cost_estimate_equals_the_allocation_orders(self):
        from etf_agent.decision.swap_nomination import SwapEstimator

        bundle = exposure_bundle()
        intent, policy, _, _ = v2_world(bundle, pairs=[])
        estimator = SwapEstimator(bundle, policy, intent)
        engine = AllocationOrderEngine(bundle, policy)
        effective = engine._effective_policy({})
        for symbol, shares in (("2330.TW", 1000), ("1101.TW", 3000)):
            sell = engine._order(symbol, "sell", shares, "exit", effective)
            self.assertEqual(estimator.sell_net(symbol, shares), Decimal(sell["net_cash_impact"]))
            buy = engine._order(symbol, "buy", shares, "buy", effective)
            self.assertEqual(estimator.gross(symbol, "buy", shares), Decimal(buy["gross_amount"]))


class ReplayTests(unittest.TestCase):
    def test_a_v2_template_is_ignored_in_replay_and_the_replayed_allocation_never_uses_nominations(self):
        """回放（replay_reference_only）對 v2 同樣略過 agent_swap：封存 run 沒有提名，回放不換股；其餘 notes 與 v1 相同。"""
        from etf_agent.evaluation.replay import bind_replay_policy
        from test_cash_ratio_range import STANCE as LEGACY_STANCE, chain_inputs
        from test_daily_pipeline import live_policy_template

        bundle, _, debate, intent, archived, _, _, _ = chain_inputs()

        def replay(version):
            template = live_policy_template()
            template.update(policy_id="replay-A", available_at="2026-09-19T00:00:00+00:00")
            sizing = template["position_sizing"]
            if version == "v2":
                sizing["rotation"].pop("forced_swap", None)
                sizing["exposure_rotation"].pop("conviction_gap_swap", None)
            sizing["agent_swap"]["version"] = version
            if version == "v1":  # 正式樣板的曝險子閘門是 v2（只允許配 agent_swap v2），v1 對照組降回影子
                sizing["agent_swap"]["exposure"] = {"version": "v1"}
                sizing["agent_swap"]["quality_dip"]["version"] = "v1"  # 同理：v2 只允許配 agent_swap v2
                sizing["agent_swap"].pop("derisk_day_view", None)  # 同理：它需要 agent_swap.exposure v2
            return bind_replay_policy(template, bundle, archived, intent, debate, dict(LEGACY_STANCE), [])

        v1_policy, v1_notes = replay("v1")
        v2_policy, v2_notes = replay("v2")
        self.assertEqual(v2_notes["agent_swap"], "ignored_in_replay")
        self.assertNotIn("agent_swap", v2_policy["position_sizing"])
        self.assertEqual(set(v2_notes), set(v1_notes))
        body = AllocationOrderEngine(bundle, v2_policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, v2_policy, intent).validate(body), [])
        self.assertFalse([code for code in codes(body) if code.startswith("AGENT_SWAP")])


if __name__ == "__main__":
    unittest.main()
