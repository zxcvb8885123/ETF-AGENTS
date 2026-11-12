"""agent_swap.entry_reserve v1（階段 3，版本閘門，已實作、尚未開啟）：docs/agent_led_offense_plan.md 4.6、4.11～4.12。

覆蓋：

1. 閘門（Policy 驗證）：形狀、值域、全部數字必填、前提（agent_swap v2、agent_swap.exposure、quality_dip 只能 v3／v4）、
   max_nominations 不得小於成交上限、max_entry_weight 不得高於單股上限；沒有此欄位的 Policy 不受影響。
2. 綁定與純函式：nominated_pairs 每組必須綁 size（fail-closed）、份量格對權重上限（large 1、medium 2/3、small 1/3）。
3. 提名層：Schema 的 size 不含空字串、Validator 要求每組 size、封存還原。
4. 配置：份量格決定買進股數（只縮不放）、縮到不足一張整組取消且換出者不賣、換手超過上限從該組起取消、
   與 proceeds_to_add 不並用、沒有閘門的 Policy 逐位元維持原配置。

測試用的數值（max_entry_weight、max_nominations）只放在測試內，沒有寫進 config。
"""

import copy
import unittest
from decimal import Decimal

from etf_agent.automation.agent_schemas import SWAP_NOMINATION_RESERVE_SCHEMA
from etf_agent.decision import AllocationOrderEngine, DecisionToolError, ProposalValidator, decision_policy_sha256
from etf_agent.decision.entry_reserve import (
    ENTRY_RESERVE_V1, entry_reserve_gate, pair_sizes, reserve_weight, validate_entry_reserve_gate,
)
from etf_agent.decision.sizing import AGENT_SWAP_FIELDS, validate_position_sizing

from test_agent_swap_allocation import four_symbol_bundle, v2_world
from test_agent_swap_policy import agent_swap_errors, errors_for, sizing_with
from test_exposure_swap_allocation import LIMITS
from test_market_index_facts import GATE as MARKET_INDEX_GATE
from test_quality_dip_swap import QualityWorld
from test_swap_nomination import good_output
from etf_agent.decision.swap_nomination import nomination_facts, seal_swap_nomination, swap_nomination_brief
from test_proceeds_to_add import ADD, BUYER, VICTIM, items
from test_gate_prerequisites import decision_item
from test_agent_swap_allocation import four_symbol_items
from test_exposure_rotation_e2e import sold_and_bought
from test_quality_dip_sizing import V3, bind_v3
from etf_agent.decision.sizing import PROCEEDS_TO_ADD

RESERVE = {"version": "v1", "max_entry_weight": "0.2", "max_nominations": 3}
QUALITY_V2 = {"version": "v2"}


def reserve_sizing(**changes):
    sizing = sizing_with("v2")
    sizing["exposure_view"] = {"version": "v1"}
    sizing["market_index_facts"] = dict(MARKET_INDEX_GATE)
    sizing["agent_swap"]["exposure"] = {"version": "v1"}
    sizing["agent_swap"]["entry_reserve"] = dict(RESERVE, **changes)
    return sizing


def reserve_errors(sizing):
    return [e for e in errors_for(sizing) if "entry_reserve" in e]


class GateTests(unittest.TestCase):
    def test_valid_shape_and_field_registration(self):
        self.assertIn("entry_reserve", AGENT_SWAP_FIELDS)
        self.assertEqual(reserve_errors(reserve_sizing()), [])
        self.assertEqual(entry_reserve_gate(reserve_sizing())["version"], ENTRY_RESERVE_V1)

    def test_bad_values_are_rejected(self):
        for label, changes in {
            "版本值域外": {"version": "v2"}, "權重 0": {"max_entry_weight": "0"}, "權重 1": {"max_entry_weight": "1"},
            "權重非字串": {"max_entry_weight": 0.2}, "權重布林": {"max_entry_weight": True}, "權重不是數字": {"max_entry_weight": "abc"},
            "清單數 0": {"max_nominations": 0}, "清單數布林": {"max_nominations": True}, "清單數太大": {"max_nominations": 21},
            "清單數小於成交上限": {"max_nominations": 1},
        }.items():
            self.assertTrue(reserve_errors(reserve_sizing(**changes)), label)

    def test_every_number_is_required_and_unknown_fields_are_rejected(self):
        for field in ("version", "max_entry_weight", "max_nominations"):
            sizing = reserve_sizing()
            del sizing["agent_swap"]["entry_reserve"][field]
            self.assertTrue(reserve_errors(sizing), field)
        sizing = reserve_sizing()
        sizing["agent_swap"]["entry_reserve"]["extra"] = 1
        self.assertTrue(reserve_errors(sizing))
        sizing = reserve_sizing()
        sizing["agent_swap"]["entry_reserve"] = "v1"
        self.assertTrue(reserve_errors(sizing))

    def test_prerequisites(self):
        sizing = reserve_sizing()
        sizing["agent_swap"]["version"] = "v1"
        self.assertTrue(any("agent_swap.version=v2" in e for e in reserve_errors(sizing)))
        sizing = reserve_sizing()
        del sizing["agent_swap"]["exposure"]
        self.assertTrue(any("agent_swap.exposure" in e for e in reserve_errors(sizing)))
        sizing = reserve_sizing()
        sizing["agent_swap"]["quality_dip"] = dict(QUALITY_V2)
        self.assertTrue(any("quality_dip" in e for e in reserve_errors(sizing)))

    def test_entry_weight_may_not_exceed_the_single_stock_limit(self):
        policy = copy.deepcopy(errors_for.__globals__["base_policy"]())
        policy["position_sizing"] = reserve_sizing(max_entry_weight="0.9")
        errors = []
        validate_position_sizing(policy, ["2330.TW", "1101.TW", "2317.TW", "2412.TW"], errors)
        self.assertTrue(any("max_stock_weight" in e for e in errors), errors)

    def test_policy_without_the_gate_is_unchanged(self):
        sizing = sizing_with("v2")
        self.assertIsNone(entry_reserve_gate(sizing))
        self.assertEqual(pair_sizes(sizing), {})
        self.assertFalse(any("entry_reserve" in e for e in errors_for(sizing)))
        errors = []
        validate_entry_reserve_gate(sizing, errors)
        self.assertEqual(errors, [])


class BindingTests(unittest.TestCase):
    REF = {"nomination_id": "n", "sha256": "a" * 64, "status": "completed"}

    def sizing(self, pairs):
        sizing = reserve_sizing()
        sizing["agent_swap"].update(nomination_ref=dict(self.REF), nominated_pairs=pairs)
        return sizing

    def test_every_pair_needs_a_size(self):
        pair = {"buy_symbol": "1101.TW", "sell_symbol": "2330.TW", "purpose": "thesis"}
        self.assertEqual([e for e in agent_swap_errors(self.sizing([dict(pair, size="medium")])) if "nominated_pairs" in e], [])
        self.assertTrue([e for e in agent_swap_errors(self.sizing([pair])) if "size" in e])
        self.assertTrue([e for e in agent_swap_errors(self.sizing([dict(pair, size="huge")])) if "size" in e])

    def test_pair_sizes_fail_closed_and_weight_grades(self):
        sizing = self.sizing([{"buy_symbol": "1101.TW", "sell_symbol": "2330.TW", "purpose": "thesis", "size": "small"}])
        self.assertEqual(pair_sizes(sizing), {("1101.TW", "2330.TW"): "small"})
        unbound = reserve_sizing()
        with self.assertRaises(DecisionToolError):
            pair_sizes(unbound)
        sizing["agent_swap"]["nominated_pairs"][0].pop("size")
        with self.assertRaises(DecisionToolError):
            pair_sizes(sizing)
        gate = RESERVE
        self.assertEqual(reserve_weight(gate, "large"), Decimal("0.2"))
        self.assertEqual(reserve_weight(gate, "medium"), Decimal("0.2") * 2 / 3)
        self.assertEqual(reserve_weight(gate, "small"), Decimal("0.2") / 3)


class NominationLayerTests(unittest.TestCase):
    def world(self, with_gate=True):
        world = QualityWorld(gate_on=False)
        if with_gate:
            swap = world.policy["position_sizing"]["agent_swap"]
            swap["version"] = "v2"  # 提名事實與驗證只在 agent_swap v2 才讀 entry_reserve
            swap["entry_reserve"] = dict(RESERVE, max_entry_weight="0.1")
        return world

    def raw(self, **pair_changes):
        output = good_output(lower_conviction_review=[])
        output["nominations"][0].update(purpose="thesis", **pair_changes)
        return output

    def validate(self, world, raw):
        return world.validator().validate(seal_swap_nomination(world.envelope, raw, True, False, True))

    def test_every_pair_needs_a_size(self):
        world = self.world()
        self.assertTrue(self.validate(world, self.raw()))  # 缺 size
        self.assertTrue(self.validate(world, self.raw(size="")))  # 空字串等於缺席
        self.assertTrue(self.validate(world, self.raw(size="huge")))
        self.assertEqual(self.validate(world, self.raw(size="medium")), [])

    def test_facts_list_cap_is_max_nominations_and_fill_cap_is_kept(self):
        facts = self.world().facts()
        self.assertEqual(facts["pair_cap"], 3)
        self.assertEqual(facts["fill_cap"], 2)
        self.assertEqual(self.world(with_gate=False).facts().get("pair_cap"), 2)
        self.assertNotIn("fill_cap", self.world(with_gate=False).facts())

    def test_brief_carries_the_size_note_only_with_the_gate(self):
        with_gate = self.world().brief()
        without = self.world(with_gate=False).brief()
        self.assertIn("entry_reserve", repr(with_gate))
        self.assertNotIn("entry_reserve", repr(without))


class SchemaTests(unittest.TestCase):
    def test_size_is_required_and_has_no_empty_value(self):
        text = repr(SWAP_NOMINATION_RESERVE_SCHEMA)
        self.assertIn("size", text)
        pair = SWAP_NOMINATION_RESERVE_SCHEMA["properties"]["nominations"]["items"]
        self.assertIn("size", pair["required"])
        self.assertEqual(sorted(pair["properties"]["size"]["enum"]), ["large", "medium", "small"])


class AllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="336000", positions={ADD: 1000, VICTIM: 1000}, **LIMITS)
        cls.intent, cls.policy, cls.plain, _ = v2_world(
            cls.bundle, items(), pairs=[(BUYER, VICTIM)], policy_fields=LIMITS, level="neutral", grade="lower")

    def reserved(self, size="large", gate=None, policy_changes=None, validate=True):
        bound = copy.deepcopy(self.policy)
        sizing = bound["position_sizing"]
        swap = sizing["agent_swap"]
        swap["nominated_pairs"] = [dict(pair, purpose="thesis", size=size) for pair in swap["nominated_pairs"]]
        swap["exposure"] = {"version": "v1"}
        sizing.update(exposure_view={"version": "v1"}, market_index_facts=dict(MARKET_INDEX_GATE))
        swap["entry_reserve"] = dict(RESERVE if gate is None else gate)
        bound.update(policy_changes or {})
        bound["content_sha256"] = decision_policy_sha256(bound)
        body = AllocationOrderEngine(self.bundle, bound).run(self.intent)
        if validate:
            self.assertEqual(ProposalValidator(self.bundle, bound, self.intent).validate(body), [])
        return body

    @staticmethod
    def orders(body):
        return sorted((o["symbol"], o["side"], o["shares"]) for o in body["order_proposal"]["orders"])

    @staticmethod
    def flags(body):
        return {(f["symbol"], f["code"]) for f in body["constraint_flags"]}

    def test_large_fills_the_pair_with_the_reserved_lot_and_says_why(self):
        body = self.reserved("large")
        self.assertEqual(self.orders(body), self.orders(self.plain))
        self.assertIn((BUYER, "AGENT_SWAP_FILL"), self.flags(body))
        victim = next(f for f in body["constraint_flags"] if f["symbol"] == VICTIM and f["code"] == "AGENT_SWAP")
        self.assertIn("份量格 large", victim["detail"])
        self.assertIn("回到一般預算", victim["detail"])

    def test_a_size_that_shrinks_below_one_lot_cancels_the_whole_pair_and_the_victim_is_not_sold(self):
        body = self.reserved("medium")
        self.assertIn((BUYER, "AGENT_SWAP_CANCELLED:entry_reserve_lot"), self.flags(body))
        self.assertNotIn((VICTIM, "sell"), {(o["symbol"], o["side"]) for o in body["order_proposal"]["orders"]})
        self.assertNotIn(BUYER, {o["symbol"] for o in body["order_proposal"]["orders"]})  # 不會只賣不買
        small = self.reserved("small")
        self.assertIn((BUYER, "AGENT_SWAP_CANCELLED:entry_reserve_lot"), self.flags(small))

    def test_the_buy_is_never_larger_than_the_size_weight_allows(self):
        body = self.reserved("large", gate=dict(RESERVE, max_entry_weight="0.12"))
        bought = sum(o["shares"] for o in body["order_proposal"]["orders"] if o["symbol"] == BUYER and o["side"] == "buy")
        full = sum(o["shares"] for o in self.plain["order_proposal"]["orders"] if o["symbol"] == BUYER and o["side"] == "buy")
        self.assertLessEqual(bought, full)

    def test_a_pair_over_the_turnover_limit_is_cancelled_from_that_pair(self):
        body = self.reserved("large", policy_changes={"max_turnover_rate": "0.3"})
        self.assertIn((BUYER, "AGENT_SWAP_CANCELLED:entry_reserve_turnover"), self.flags(body))
        sides = {(o["symbol"], o["side"]) for o in body["order_proposal"]["orders"]}
        self.assertNotIn((VICTIM, "sell"), sides)  # 整組取消，換出者不賣
        self.assertNotIn((BUYER, "buy"), sides)

    def test_turnover_skip_keeps_a_single_over_limit_pair_cancelled(self):
        # 單一組超過上限時，turnover_skip 與舊行為結果相同（整組取消、換出者不賣）；差別只在其後還有較小的組時。
        gate = dict(RESERVE, turnover_skip={"version": "v1"})
        body = self.reserved("large", gate=gate, policy_changes={"max_turnover_rate": "0.3"})
        self.assertIn((BUYER, "AGENT_SWAP_CANCELLED:entry_reserve_turnover"), self.flags(body))
        self.assertNotIn((VICTIM, "sell"), {(o["symbol"], o["side"]) for o in body["order_proposal"]["orders"]})

    def test_forced_overweight_trims_that_come_later_count_toward_the_pair_turnover(self):
        # 2330 現有權重高於單股上限，後段 _fit_stock_weights 會把它賣回上限（此刻還不在 target_shares）。
        # 保守計入基底換手：0.5 的上限下由 entry_reserve 自己整組取消，而不是留給後段 turnover_fit。
        body = self.reserved("large", policy_changes={"max_turnover_rate": "0.5"})
        self.assertIn((BUYER, "AGENT_SWAP_CANCELLED:entry_reserve_turnover"), self.flags(body))
        self.assertNotIn((BUYER, "AGENT_SWAP_CANCELLED:turnover"), self.flags(body))
        self.assertNotIn((VICTIM, "sell"), {(o["symbol"], o["side"]) for o in body["order_proposal"]["orders"]})

    def test_a_missing_size_binding_is_rejected(self):
        bound = copy.deepcopy(self.policy)
        sizing = bound["position_sizing"]
        sizing["agent_swap"]["exposure"] = {"version": "v1"}
        sizing.update(exposure_view={"version": "v1"}, market_index_facts=dict(MARKET_INDEX_GATE))
        sizing["agent_swap"]["nominated_pairs"] = [dict(p, purpose="thesis") for p in sizing["agent_swap"]["nominated_pairs"]]
        sizing["agent_swap"]["entry_reserve"] = dict(RESERVE)
        bound["content_sha256"] = decision_policy_sha256(bound)
        with self.assertRaises(DecisionToolError):
            AllocationOrderEngine(self.bundle, bound).run(self.intent)

    def test_without_the_gate_the_allocation_is_bit_for_bit_the_old_one(self):
        again = AllocationOrderEngine(self.bundle, self.policy).run(self.intent)
        self.assertEqual(again, self.plain)
        self.assertFalse(any("entry_reserve" in code for _, code in self.flags(self.plain)))


L90 = {"max_stock_weight": "0.9", "special_weight_limits": {symbol: "0.9" for symbol in ("1101.TW", "2317.TW", "2330.TW", "2412.TW")}}


class StarvedBuyerRegressionTests(unittest.TestCase):
    """2026-11-11 的問題：持股占滿（2330 約 85%，現金很少）時，換入者的信心預算只剩不到一張，換出者照賣卻買不到換入者。

    自建小型世界：2330 持 10000 股（約 85%）、換出者 1101 持 2000 股、現金 6 萬；換入者 2317 一張約 11.2 萬、信心預算約 10.7 萬。
    """

    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="60000", positions={ADD: 10000, VICTIM: 2000}, **L90)
        cls.intent, cls.policy, cls.plain, _ = v2_world(
            cls.bundle, items(), pairs=[(BUYER, VICTIM)], policy_fields=L90, level="neutral", grade="lower")

    def bound(self, reserve=True, proceeds=True):
        bound = copy.deepcopy(self.policy)
        sizing = bound["position_sizing"]
        swap = sizing["agent_swap"]
        if proceeds:
            swap["proceeds_to_add"] = dict(PROCEEDS_TO_ADD)  # 與正式設定相同：賣款可流向高信心加碼
        if reserve:
            swap["nominated_pairs"] = [dict(pair, purpose="thesis", size="large") for pair in swap["nominated_pairs"]]
            swap["exposure"] = {"version": "v1"}
            sizing.update(exposure_view={"version": "v1"}, market_index_facts=dict(MARKET_INDEX_GATE))
            swap["entry_reserve"] = {"version": "v1", "max_entry_weight": "0.2", "max_nominations": 3}
        bound["content_sha256"] = decision_policy_sha256(bound)
        body = AllocationOrderEngine(self.bundle, bound).run(self.intent)
        self.assertEqual(ProposalValidator(self.bundle, bound, self.intent).validate(body), [])
        return body

    @staticmethod
    def traded(body):
        return {(o["symbol"], o["side"]): o["shares"] for o in body["order_proposal"]["orders"]}

    def test_without_the_gate_the_buyer_gets_no_lot_and_the_pair_is_cancelled(self):
        for body in (self.plain, self.bound(reserve=False, proceeds=False), self.bound(reserve=False, proceeds=True)):
            flags = {(f["symbol"], f["code"]) for f in body["constraint_flags"]}
            self.assertIn((BUYER, "AGENT_SWAP_CANCELLED:no_buy_order"), flags)
            self.assertNotIn((BUYER, "buy"), self.traded(body))
            self.assertNotIn((VICTIM, "sell"), self.traded(body))  # 沒買到就整組取消，不是只賣不買

    def test_with_the_gate_the_buyer_is_reserved_a_lot_and_the_proceeds_do_not_flow_elsewhere(self):
        body = self.bound()
        traded = self.traded(body)
        self.assertEqual(traded.get((BUYER, "buy")), 1000)
        self.assertEqual(traded.get((VICTIM, "sell")), 2000)
        self.assertNotIn((ADD, "buy"), traded)  # 賣款沒有流向 2330
        flags = {(f["symbol"], f["code"]) for f in body["constraint_flags"]}
        self.assertIn((BUYER, "AGENT_SWAP_FILL"), flags)
        self.assertNotIn((BUYER, "AGENT_SWAP_PROCEEDS_TO_ADD"), flags)


VACANT = dict(L90, max_positions=4)


class VacantSlotRegressionTests(unittest.TestCase):
    """審查 A1：有空名額（持 2 檔、上限 4 檔）時，目標不足一張的換入者（權重被高信心加碼吃光；slot_recycle 以權重 0 留在 weights，
    沒有 slot_recycle 時留一張的最小權重）不得被標 AGENT_SWAP_UNNEEDED，否則 entry_reserve 的保留預算整個被繞過，放寬的現金全流向 2330。

    世界與 ``StarvedBuyerRegressionTests`` 相同（2330 約 85%、換出者 1101、換入者 2317），只是持股上限 4 檔而 breadth 對應 4 檔。"""

    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="60000", positions={ADD: 10000, VICTIM: 2000}, **VACANT)
        cls.intent, cls.policy, cls.plain, _ = v2_world(
            cls.bundle, items(), pairs=[(BUYER, VICTIM)], policy_fields=VACANT, level="neutral", grade="lower")

    def bound(self, reserve=True, recycle=True, cash_buyer=False):
        bound = copy.deepcopy(self.policy)
        sizing = bound["position_sizing"]
        sizing["positions_by_breadth"] = {key: 4 for key in sizing["positions_by_breadth"]}
        if recycle:
            sizing["slot_recycle"] = {"version": "v1"}
        swap = sizing["agent_swap"]
        swap["proceeds_to_add"] = dict(PROCEEDS_TO_ADD)
        if reserve:
            swap["nominated_pairs"] = [dict(pair, purpose="thesis", size="large") for pair in swap["nominated_pairs"]]
            swap["exposure"] = {"version": "v1"}
            sizing.update(exposure_view={"version": "v1"}, market_index_facts=dict(MARKET_INDEX_GATE))
            swap["entry_reserve"] = {"version": "v1", "max_entry_weight": "0.2", "max_nominations": 3}
        bound["content_sha256"] = decision_policy_sha256(bound)
        body = AllocationOrderEngine(self.bundle, bound).run(self.intent)
        self.assertEqual(ProposalValidator(self.bundle, bound, self.intent).validate(body), [])
        return body

    @staticmethod
    def traded(body):
        return {(o["symbol"], o["side"]): o["shares"] for o in body["order_proposal"]["orders"]}

    @staticmethod
    def flags(body):
        return {(f["symbol"], f["code"]) for f in body["constraint_flags"]}

    def test_a_starved_buyer_with_a_vacant_slot_still_gets_its_reserved_lot(self):
        for recycle in (True, False):
            with self.subTest(slot_recycle=recycle):
                body = self.bound(recycle=recycle)
                traded = self.traded(body)
                flags = self.flags(body)
                self.assertNotIn((BUYER, "AGENT_SWAP_UNNEEDED"), flags)
                self.assertEqual(traded.get((BUYER, "buy")), 1000)
                self.assertEqual(traded.get((VICTIM, "sell")), 2000)
                self.assertNotIn((ADD, "buy"), traded)  # 放寬的現金沒有全流向 2330
                self.assertIn((BUYER, "AGENT_SWAP_FILL"), flags)

    def test_without_the_gate_the_old_unneeded_behaviour_is_bit_for_bit_kept(self):
        for recycle in (True, False):
            with self.subTest(slot_recycle=recycle):
                body = self.bound(reserve=False, recycle=recycle)
                self.assertIn((BUYER, "AGENT_SWAP_UNNEEDED"), self.flags(body))
                self.assertNotIn((VICTIM, "sell"), self.traded(body))  # 沒有閘門：換出者不賣，與修正前相同

    def test_a_buyer_that_really_has_a_slot_and_a_lot_stays_unneeded_with_the_gate(self):
        bundle = four_symbol_bundle(cash="836000", positions={ADD: 1000}, **LIMITS)
        base_items = [decision_item(BUYER, "buy", "high", adopt=["bull-2317-1"]),
                      decision_item(ADD, "hold", "medium", adopt=["bull-2330-1", "bear-2330-1"]),
                      decision_item(VICTIM, "hold", "low", adopt=["bull-1101-1"])]
        intent, policy, plain, _ = v2_world(
            bundle, base_items, pairs=[(BUYER, VICTIM)], policy_fields=LIMITS, held_since={ADD: "2026-09-01T06:00:00+00:00"})
        self.assertIn((BUYER, "AGENT_SWAP_UNNEEDED"), {(f["symbol"], f["code"]) for f in plain["constraint_flags"]})
        bound = copy.deepcopy(policy)
        sizing = bound["position_sizing"]
        swap = sizing["agent_swap"]
        swap["nominated_pairs"] = [dict(pair, purpose="thesis", size="large") for pair in swap["nominated_pairs"]]
        swap["exposure"] = {"version": "v1"}
        sizing.update(exposure_view={"version": "v1"}, market_index_facts=dict(MARKET_INDEX_GATE))
        swap["entry_reserve"] = dict(RESERVE)
        bound["content_sha256"] = decision_policy_sha256(bound)
        body = AllocationOrderEngine(bundle, bound).run(intent)
        flags = {(f["symbol"], f["code"]) for f in body["constraint_flags"]}
        self.assertIn((BUYER, "AGENT_SWAP_UNNEEDED"), flags)  # 真正取得名額且買得到一張以上：維持 UNNEEDED、換出者不賣
        self.assertNotIn((VICTIM, "sell"), {(o["symbol"], o["side"]) for o in body["order_proposal"]["orders"]})
        self.assertEqual(body["order_proposal"]["orders"], plain["order_proposal"]["orders"])


class ConvictionTargetsReservedTests(unittest.TestCase):
    """``_conviction_targets(reserved=…)``：直接以配置時的實際引數呼叫，比較有無保留。"""

    @classmethod
    def setUpClass(cls):
        world = StarvedBuyerRegressionTests
        world.setUpClass()
        bound = copy.deepcopy(world.policy)
        sizing = bound["position_sizing"]
        swap = sizing["agent_swap"]
        swap["nominated_pairs"] = [dict(pair, purpose="thesis", size="large") for pair in swap["nominated_pairs"]]
        swap["exposure"] = {"version": "v1"}
        sizing.update(exposure_view={"version": "v1"}, market_index_facts=dict(MARKET_INDEX_GATE))
        swap["entry_reserve"] = {"version": "v1", "max_entry_weight": "0.2", "max_nominations": 3}
        bound["content_sha256"] = decision_policy_sha256(bound)
        cls.engine = AllocationOrderEngine(world.bundle, bound)
        calls = []
        original = cls.engine._conviction_targets

        def spy(*args, **kwargs):
            calls.append((args, dict(kwargs)))
            return original(*args, **kwargs)

        cls.engine._conviction_targets = spy
        cls.engine.run(world.intent)
        cls.calls = [call for call in calls if call[1].get("reserved")]
        cls.original = original

    def test_the_reserved_symbol_gets_exactly_its_reserved_weight_and_the_rest_share_what_is_left(self):
        self.assertTrue(self.calls)
        args, kwargs = self.calls[-1]
        reserved = dict(kwargs["reserved"])
        buyer = BUYER
        self.assertIn(buyer, reserved)
        plain_kwargs = {key: value for key, value in kwargs.items() if key != "reserved"}
        _, with_weights, _, with_budget, _ = self.original(*args, **kwargs)
        _, plain_weights, _, plain_budget, _ = self.original(*args, **plain_kwargs)
        self.assertEqual(with_weights[buyer], reserved[buyer])
        self.assertEqual(with_budget, plain_budget - sum(reserved.values()))
        self.assertLessEqual(sum(w for symbol, w in with_weights.items() if symbol not in reserved),
                             sum(plain_weights.values()))
        # 不保留時，換入者的信心預算不足一張（權重低於一張的價值）
        self.assertLess(plain_weights.get(buyer, Decimal(0)) * 1, with_weights[buyer])

    def test_without_reserved_the_call_is_the_plain_one(self):
        args, kwargs = self.calls[-1]
        plain_kwargs = {key: value for key, value in kwargs.items() if key != "reserved"}
        self.assertEqual(self.original(*args, **plain_kwargs), self.original(*args, **dict(plain_kwargs, reserved=None)))
        self.assertEqual(self.original(*args, **plain_kwargs), self.original(*args, **dict(plain_kwargs, reserved={})))


class QualityDipTogetherTests(unittest.TestCase):
    """quality_dip 配對與 entry_reserve 同時成交：quality_dip 沿用自己的硬上限與裁決，thesis 配對走 entry_reserve 的份量格。"""

    def test_both_pairs_fill_and_the_quality_pair_still_buys_no_more_than_its_proceeds(self):
        limits = {"max_stock_weight": "0.2", "special_weight_limits": {s: "0.2" for s in ("1101.TW", "2317.TW", "2330.TW", "2412.TW")}}
        bundle = four_symbol_bundle(cash="736000", positions={"2330.TW": 1000, "1101.TW": 1000}, **limits)
        pairs = [("2317.TW", "2330.TW"), ("2412.TW", "1101.TW")]
        intent, policy, _, _ = v2_world(bundle, four_symbol_items(), pairs, policy_fields=limits)
        nominated = [
            {"buy_symbol": "2317.TW", "sell_symbol": "2330.TW", "purpose": "quality_dip", "size": "large"},
            {"buy_symbol": "2412.TW", "sell_symbol": "1101.TW", "purpose": "thesis", "size": "large"},
        ]
        changes = {"nominated_pairs": nominated, "entry_reserve": {"version": "v1", "max_entry_weight": "0.2", "max_nominations": 3}}
        _, _, body = bind_v3(
            bundle, intent, policy, purposes=("quality_dip", "thesis"), sizes=["large", None], rulings=["approve"],
            gate=dict(V3, max_entry_weight="0.5"), agent_swap_changes=changes)
        self.assertEqual(sold_and_bought(body), ({"2330.TW", "1101.TW"}, {"2317.TW", "2412.TW"}))
        orders = {(o["symbol"], o["side"]): o for o in body["order_proposal"]["orders"]}
        buy = orders[("2317.TW", "buy")]
        sell = orders[("2330.TW", "sell")]
        self.assertLessEqual(-Decimal(buy["net_cash_impact"]), Decimal(sell["net_cash_impact"]))  # quality_dip：買進不超過換出賣款
        flags = {(f["symbol"], f["code"]) for f in body["constraint_flags"]}
        self.assertIn(("2412.TW", "AGENT_SWAP_FILL"), flags)
        self.assertIn(("2317.TW", "AGENT_SWAP_FILL"), flags)
        detail = next(f["detail"] for f in body["constraint_flags"] if f["symbol"] == "2412.TW" and f["code"] == "AGENT_SWAP_FILL")
        self.assertNotIn("先保留", detail)  # 2412 的說明寫在換出者旗標上；換入者旗標沿用原文
        victims = [f["detail"] for f in body["constraint_flags"] if f["code"] == "AGENT_SWAP"]
        self.assertTrue(any("換入者先保留預算" in text for text in victims))


    def test_a_starved_quality_dip_buyer_with_a_vacant_slot_is_not_marked_unneeded(self):
        """有空名額且換入者目標不足一張時，quality_dip 配對也照常走換股（修正前被標 AGENT_SWAP_UNNEEDED、整個繞過）。"""
        bundle = four_symbol_bundle(cash="60000", positions={ADD: 10000, VICTIM: 2000}, **VACANT)
        intent, policy, _, _ = v2_world(bundle, items(), pairs=[(BUYER, VICTIM)], policy_fields=VACANT, level="neutral", grade="lower")
        policy = copy.deepcopy(policy)
        sizing = policy["position_sizing"]
        sizing["positions_by_breadth"] = {key: 4 for key in sizing["positions_by_breadth"]}
        sizing["slot_recycle"] = {"version": "v1"}
        nominated = [{"buy_symbol": BUYER, "sell_symbol": VICTIM, "purpose": "quality_dip", "size": "large"}]
        changes = {"nominated_pairs": nominated, "entry_reserve": {"version": "v1", "max_entry_weight": "0.2", "max_nominations": 3}}
        _, _, body = bind_v3(
            bundle, intent, policy, purposes=("quality_dip",), sizes=["large"], rulings=["approve"],
            gate=dict(V3, max_entry_weight="0.5"), agent_swap_changes=changes)
        flags = {(f["symbol"], f["code"]) for f in body["constraint_flags"]}
        self.assertNotIn((BUYER, "AGENT_SWAP_UNNEEDED"), flags)
        self.assertTrue(any(symbol == BUYER and code.startswith("AGENT_SWAP_") for symbol, code in flags), flags)


if __name__ == "__main__":
    unittest.main()
