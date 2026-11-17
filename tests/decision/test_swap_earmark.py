"""換股賣款專款專用（position_sizing.agent_swap.earmark）與換出者須有空頭 claim（agent_swap.victim_bear_claim），兩個版本閘門皆已實作、尚未開啟。

覆蓋：

1. Policy 驗證：形狀、值域、全部由人填、前提（agent_swap v2、entry_reserve、reuse_sell_proceeds）、與 proceeds_to_add／
   high_conviction_cash_floor 互斥、策略反思禁區；沒有欄位時逐位元不變。
2. ``_fit_cash``：換出者賣款只給同一對換入者（不外流到加碼持股）、交易 Agent 自己的 exit 賣款不受限、跳空預留口徑。
3. 整組取消：買不夠（earmark_underfill）、期末現金達上限（earmark_cash_ceiling，只挑剩餘專款大於 0 且最多的一組）。
4. 換出者沒有本次採納的該股空頭 claim 即取消該對（victim_no_bear_claim），其餘對照常。
5. Finalizer（ProposalValidator）重算一致；2026-11-17 封存 run 回放（replay_reference_only，缺檔即略過）與啟用／回滾樣板。

測試用的數值只放在測試內；config/decision_policy.json 不動。
"""

import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from etf_agent.decision import AllocationOrderEngine, DecisionToolError, ProposalValidator, decision_policy_sha256
from etf_agent.decision.risk import CompetitionGuardV2, ScenarioEngine
from etf_agent.decision.sizing import (
    AGENT_SWAP_FIELDS, BUY_GAP_RESERVE, GATE_PREREQUISITES, PROCEEDS_TO_ADD, earmark_gate, validate_position_sizing,
    victim_bear_claim_enabled,
)
from etf_agent.strategy_reflection.contracts import KNOBS, ReflectionError
from etf_agent.strategy_reflection.tools import check_proposal

from test_agent_swap_allocation import four_symbol_bundle, four_symbol_items, v2_world
from test_agent_swap_policy import errors_for
from test_entry_reserve import L90, RESERVE, reserve_sizing
from test_exposure_rotation import hold_item
from test_exposure_swap_allocation import LIMITS, TWO_PAIRS
from test_market_index_facts import GATE as MARKET_INDEX_GATE
from test_proceeds_to_add import ADD, BUYER, VICTIM, items

EARMARK = {"version": "v1", "min_fill_ratio": "0.5", "scope": "all"}
BEAR = {"version": "v1"}
ROOT = Path(__file__).resolve().parents[2]


def earmark_sizing(**changes):
    sizing = reserve_sizing()
    sizing["agent_swap"]["earmark"] = dict(EARMARK, **changes)
    return sizing


def swap_errors(sizing):
    return [e for e in errors_for(sizing) if "earmark" in e or "victim_bear_claim" in e]


def bind(policy, size="large", gate=None, **swap_fields):
    """把 v2_world 的 Policy 改成 entry_reserve 版本（每組 size），再加上指定的 agent_swap 子閘門。"""
    bound = copy.deepcopy(policy)
    sizing = bound["position_sizing"]
    swap = sizing["agent_swap"]
    swap["nominated_pairs"] = [dict(pair, purpose="thesis", size=size) for pair in swap["nominated_pairs"]]
    swap["exposure"] = {"version": "v1"}
    sizing.update(exposure_view={"version": "v1"}, market_index_facts=dict(MARKET_INDEX_GATE))
    swap["entry_reserve"] = dict(RESERVE if gate is None else gate)
    swap.update(copy.deepcopy(swap_fields))
    bound["content_sha256"] = decision_policy_sha256(bound)
    return bound


def traded(body):
    return {(o["symbol"], o["side"]): o["shares"] for o in body["order_proposal"]["orders"]}


def flags(body):
    return {(f["symbol"], f["code"]) for f in body["constraint_flags"]}


class EarmarkGateTests(unittest.TestCase):
    def test_valid_shape_registration_and_human_only(self):
        self.assertTrue({"earmark", "victim_bear_claim"} <= AGENT_SWAP_FIELDS)
        self.assertEqual(swap_errors(earmark_sizing()), [])
        self.assertEqual(swap_errors(earmark_sizing(scope="defensive", min_fill_ratio="1")), [])
        rules = {rule.field: rule for rule in GATE_PREREQUISITES}
        self.assertEqual(rules["agent_swap.earmark"].requires, ("agent_swap", "agent_swap.entry_reserve"))
        self.assertTrue(rules["agent_swap.earmark"].human_only and rules["agent_swap.victim_bear_claim"].human_only)
        self.assertFalse([knob for knob in KNOBS if knob.startswith("agent_swap")])
        for path in ("agent_swap.earmark", "agent_swap.earmark.min_fill_ratio", "agent_swap.victim_bear_claim"):
            with self.assertRaises(ReflectionError):  # 策略反思禁區（agent_swap 前綴）
                check_proposal(path, "0.3", {}, {})

    def test_bad_values_are_rejected(self):
        for label, changes in {
            "版本": {"version": "v2"}, "比例 0": {"min_fill_ratio": "0"}, "比例大於 1": {"min_fill_ratio": "1.5"},
            "比例負數": {"min_fill_ratio": "-0.1"}, "比例非字串": {"min_fill_ratio": 0.5}, "比例布林": {"min_fill_ratio": True},
            "比例非數字": {"min_fill_ratio": "abc"}, "範圍": {"scope": "aggressive"},
        }.items():
            self.assertTrue(swap_errors(earmark_sizing(**changes)), label)

    def test_every_field_is_required_and_unknown_fields_are_rejected(self):
        for field in sorted(EARMARK):
            sizing = earmark_sizing()
            del sizing["agent_swap"]["earmark"][field]
            self.assertTrue(swap_errors(sizing), field)
        self.assertTrue(swap_errors(earmark_sizing(extra="x")))
        sizing = earmark_sizing()
        sizing["agent_swap"]["earmark"] = "v1"
        self.assertTrue(swap_errors(sizing))

    def test_prerequisites_and_exclusions(self):
        sizing = earmark_sizing()
        sizing["agent_swap"]["version"] = "v1"
        self.assertTrue(any("earmark 需要 agent_swap v2" in e for e in swap_errors(sizing)))
        sizing = earmark_sizing()
        del sizing["agent_swap"]["entry_reserve"]
        self.assertTrue(any("agent_swap.earmark" in e and "entry_reserve" in e for e in errors_for(sizing)))
        sizing = earmark_sizing()
        sizing["agent_swap"]["proceeds_to_add"] = dict(PROCEEDS_TO_ADD)
        self.assertTrue(any("proceeds_to_add 同時啟用" in e for e in swap_errors(sizing)))
        sizing = earmark_sizing()
        sizing["high_conviction_cash_floor"] = {"version": "v1", "floor_ratio": "0.75"}
        self.assertTrue(any("high_conviction_cash_floor 同時啟用" in e for e in swap_errors(sizing)))

    def test_reuse_sell_proceeds_must_be_true_once_bound(self):
        policy = copy.deepcopy(errors_for.__globals__["base_policy"]())
        policy["position_review"] = {"version": "v1", "loss_alert": "0.15", "retrace_alert_20d": None, "gain_alert": None}
        policy["position_sizing"] = earmark_sizing()
        for value, rejected in ((True, False), (False, True)):
            policy["reuse_sell_proceeds"] = value
            errors = []
            validate_position_sizing(policy, ["1101.TW", "2317.TW", "2330.TW"], errors)
            self.assertEqual(any("reuse_sell_proceeds=true" in e for e in errors), rejected, value)

    def test_victim_bear_claim_shape_and_version(self):
        sizing = reserve_sizing()
        sizing["agent_swap"]["victim_bear_claim"] = dict(BEAR)
        self.assertEqual(swap_errors(sizing), [])
        self.assertTrue(victim_bear_claim_enabled(sizing))
        for bad in ({"version": "v2"}, "v1", {"version": "v1", "x": 1}):
            sizing["agent_swap"]["victim_bear_claim"] = bad
            self.assertTrue(swap_errors(sizing), bad)
        sizing["agent_swap"].update(version="v1", victim_bear_claim=dict(BEAR))
        self.assertTrue(any("victim_bear_claim 需要 agent_swap v2" in e for e in swap_errors(sizing)))

    def test_absent_gates_are_inert(self):
        sizing = reserve_sizing()
        self.assertIsNone(earmark_gate(sizing))
        self.assertFalse(victim_bear_claim_enabled(sizing))
        self.assertIsNone(earmark_gate(None))
        self.assertEqual(swap_errors(sizing), [])


class FitCashTests(unittest.TestCase):
    """直接呼叫 ``_fit_cash``：持股占滿、現金只有 6 萬的世界（2330 一萬股、換出者 1101 兩千股、換入者 2317）。"""

    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="60000", positions={ADD: 10000, VICTIM: 2000}, **L90)
        cls.intent, cls.policy, _, _ = v2_world(
            cls.bundle, items(), pairs=[(BUYER, VICTIM)], policy_fields=L90, level="neutral", grade="lower")

    def engine(self, earmark=True):
        engine = AllocationOrderEngine(self.bundle, bind(self.policy, **({"earmark": EARMARK} if earmark else {})))
        engine._swap_pairs = [(BUYER, VICTIM)]
        return engine

    def fit(self, engine, effective=None, extra_sells=()):
        effective = effective or engine._effective_policy({})
        orders = [engine._order(VICTIM, "sell", 2000, "exit", effective)]
        orders += [engine._order(symbol, "sell", shares, "exit", effective) for symbol, shares in extra_sells]
        orders += [engine._order(BUYER, "buy", 1000, "buy", effective), engine._order(ADD, "buy", 1000, "add", effective)]
        nav = Decimal("60000") + Decimal(10000) * engine.prices[ADD] + Decimal(2000) * engine.prices[VICTIM]
        fitted, cash, _ = engine._fit_cash(orders, Decimal("60000"), Decimal("60000"), nav, effective, {ADD: 10000, VICTIM: 2000})
        return {(o["symbol"], o["side"]): o for o in fitted}, cash, nav, effective

    def test_without_the_gate_the_victim_proceeds_flow_to_the_high_conviction_add(self):
        fitted, _, _, _ = self.fit(self.engine(earmark=False))
        self.assertIn((ADD, "buy"), fitted)  # 高信心加碼先拿現金（含換出者賣款）
        self.assertNotIn((BUYER, "buy"), fitted)  # 換入者買不到 → 舊行為會整組取消

    def test_with_the_gate_the_proceeds_stay_with_the_paired_buyer_and_the_rest_stays_cash(self):
        fitted, cash, _, _ = self.fit(self.engine())
        self.assertEqual(int(fitted[(BUYER, "buy")]["shares"]), 1000)
        self.assertNotIn((ADD, "buy"), fitted)  # 賣款不外流到加碼持股
        proceeds = Decimal(fitted[(VICTIM, "sell")]["net_cash_impact"])
        bought = -Decimal(fitted[(BUYER, "buy")]["net_cash_impact"])
        self.assertEqual(cash, Decimal("60000") + proceeds - bought)  # 剩餘專款留作現金

    def test_a_trader_exit_is_not_earmarked_and_can_fund_the_add(self):
        fitted, _, _, _ = self.fit(self.engine(), extra_sells=[("2412.TW", 2000)])
        self.assertEqual(int(fitted[(BUYER, "buy")]["shares"]), 1000)
        self.assertIn((ADD, "buy"), fitted)  # 交易 Agent 自己的 exit 賣款照舊進一般現金

    def test_gap_reserve_still_holds_the_floor_over_every_buy(self):
        engine = self.engine()
        effective = dict(engine._effective_policy({}), buy_gap_reserve=BUY_GAP_RESERVE, buy_gap_reserve_rate=Decimal("0.03"))
        fitted, cash, nav, _ = self.fit(engine, effective, extra_sells=[("2412.TW", 2000)])
        buys = sum((-Decimal(o["net_cash_impact"]) for (s, side), o in fitted.items() if side == "buy"), Decimal(0))
        floor = engine._cash_floor(nav, Decimal("60000"), effective)
        self.assertGreaterEqual(cash, floor + buys * Decimal("0.03"))
        self.assertIn((BUYER, "buy"), fitted)


class UnderfillTests(unittest.TestCase):
    """單股上限 90%、份量格上限 0.5：換入者 2317 保留兩張；以後段縮減（mock）只留一張，檢查買不夠的整組取消。"""

    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="336000", positions={ADD: 1000, VICTIM: 1000}, **L90)
        cls.intent, cls.policy, _, _ = v2_world(
            cls.bundle, items(), pairs=[(BUYER, VICTIM)], policy_fields=L90, level="neutral", grade="lower")

    def run_with(self, ratio, cut=True):
        bound = bind(self.policy, gate=dict(RESERVE, max_entry_weight="0.5"), earmark=dict(EARMARK, min_fill_ratio=ratio))
        original = AllocationOrderEngine._fit_liquidity

        def shrink(engine, orders, projected_cash, effective):
            orders, projected_cash, notes = original(engine, orders, projected_cash, effective)
            result = []
            for order in orders:
                if cut and order["symbol"] == BUYER and order["side"] == "buy" and int(order["shares"]) > 1000:
                    smaller = engine._order(BUYER, "buy", 1000, str(order["intent"]), effective)
                    projected_cash += Decimal(smaller["net_cash_impact"]) - Decimal(order["net_cash_impact"])
                    order = smaller
                result.append(order)
            return result, projected_cash, notes

        with mock.patch.object(AllocationOrderEngine, "_fit_liquidity", shrink):
            body = AllocationOrderEngine(self.bundle, bound).run(self.intent)
            self.assertEqual(ProposalValidator(self.bundle, bound, self.intent).validate(body), [])  # Finalizer 以同一 Policy 重算一致
        return body

    def test_without_the_cut_the_buyer_gets_both_reserved_lots(self):
        body = self.run_with("0.6", cut=False)
        self.assertEqual(traded(body).get((BUYER, "buy")), 2000)
        self.assertIn((VICTIM, "sell"), traded(body))

    def test_a_fill_below_the_ratio_cancels_the_whole_pair_and_the_victim_is_kept(self):
        body = self.run_with("0.6")
        self.assertIn((BUYER, "AGENT_SWAP_CANCELLED:earmark_underfill"), flags(body))
        self.assertNotIn((VICTIM, "sell"), traded(body))
        self.assertNotIn((BUYER, "buy"), traded(body))  # 不會只賣不買，也不會只買不賣

    def test_a_fill_at_the_ratio_keeps_the_pair(self):
        body = self.run_with("0.5")
        self.assertEqual(traded(body).get((BUYER, "buy")), 1000)
        self.assertIn((VICTIM, "sell"), traded(body))
        self.assertFalse(any(code.startswith("AGENT_SWAP_CANCELLED") for _, code in flags(body)))


class CashCeilingTests(unittest.TestCase):
    """期末現金達上限時的取消挑選（``_earmark_cancel``）與復原迴圈（``_run_exposure``）。"""

    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="336000", positions={ADD: 1000, VICTIM: 1000}, **LIMITS)
        cls.intent, cls.policy, _, _ = v2_world(
            cls.bundle, items(), pairs=[(BUYER, VICTIM)], policy_fields=LIMITS, level="neutral", grade="lower")

    def engine(self, scope="all", stance=None):
        engine = AllocationOrderEngine(self.bundle, bind(self.policy, earmark=dict(EARMARK, scope=scope)))
        if stance is not None:
            # 只換 _earmark_active 讀的姿態（綁其他姿態還要同步現金格點，與此處要測的無關）。
            engine.policy["position_sizing"] = dict(engine.policy["position_sizing"], cash_stance=stance)
        return engine

    @staticmethod
    def body(cash_weight, orders):
        return {
            "allocation_proposal": {"cash_weight": cash_weight},
            "order_proposal": {"orders": [
                {"symbol": symbol, "side": side, "shares": shares, "net_cash_impact": net} for symbol, side, shares, net in orders]},
            "constraint_flags": [],
        }

    def test_picks_the_pair_with_the_largest_positive_leftover(self):
        engine = self.engine()
        engine._swap_pairs = [("A.TW", "X.TW"), ("B.TW", "Y.TW"), ("C.TW", "Z.TW")]
        orders = [
            ("X.TW", "sell", 1000, "100000"), ("A.TW", "buy", 1000, "-120000"),  # 剩餘 −20000：取消反而讓現金上升，不挑
            ("Y.TW", "sell", 1000, "100000"), ("B.TW", "buy", 1000, "-40000"),   # 剩餘 60000
            ("Z.TW", "sell", 1000, "90000"), ("C.TW", "buy", 1000, "-80000"),    # 剩餘 10000
        ]
        gate = earmark_gate(engine.policy["position_sizing"])
        self.assertEqual(engine._earmark_cancel(self.body("0.95", orders), gate), ("B.TW", "earmark_cash_ceiling"))
        self.assertIsNone(engine._earmark_cancel(self.body("0.10", orders), gate))  # 未達上限（fixture 0.90）不取消

    def test_ties_cancel_the_later_pair_and_no_positive_leftover_does_nothing(self):
        engine = self.engine()
        gate = earmark_gate(engine.policy["position_sizing"])
        engine._swap_pairs = [("A.TW", "X.TW"), ("B.TW", "Y.TW")]
        tie = [("X.TW", "sell", 1000, "100000"), ("A.TW", "buy", 1000, "-50000"),
               ("Y.TW", "sell", 1000, "100000"), ("B.TW", "buy", 1000, "-50000")]
        self.assertEqual(engine._earmark_cancel(self.body("0.95", tie), gate), ("B.TW", "earmark_cash_ceiling"))
        short = [("X.TW", "sell", 1000, "100000"), ("A.TW", "buy", 1000, "-150000")]
        engine._swap_pairs = [("A.TW", "X.TW")]
        self.assertIsNone(engine._earmark_cancel(self.body("0.95", short), gate))  # 交給 cash_ceiling_fix／Guard

    def test_underfill_takes_precedence_and_cancels_the_last_short_pair(self):
        engine = self.engine()
        gate = earmark_gate(engine.policy["position_sizing"])
        engine._swap_pairs = [("A.TW", "X.TW"), ("B.TW", "Y.TW")]
        engine._reserved_shares = {"A.TW": 4000, "B.TW": 4000}
        orders = [("X.TW", "sell", 1000, "100000"), ("A.TW", "buy", 1000, "-50000"),
                  ("Y.TW", "sell", 1000, "100000"), ("B.TW", "buy", 1000, "-50000")]
        self.assertEqual(engine._earmark_cancel(self.body("0.95", orders), gate), ("B.TW", "earmark_underfill"))

    def test_the_recovery_loop_cancels_one_pair_then_reruns(self):
        engine = self.engine()
        calls = []
        over = self.body("0.95", [(VICTIM, "sell", 1000, "99457"), (BUYER, "buy", 1000, "-20000")])
        over["constraint_flags"] = [{"symbol": VICTIM, "code": "AGENT_SWAP"}, {"symbol": BUYER, "code": "AGENT_SWAP_FILL"}]
        clean = self.body("0.80", [])
        queue = [over, clean]

        def fake(intent_result, excluded, overrides, revision_count, parent, kept_exits, dropped, exposure_excluded, agent_cancelled):
            calls.append(dict(agent_cancelled or {}))
            engine._swap_pairs = [] if agent_cancelled else [(BUYER, VICTIM)]
            engine._reserved_shares = {}
            return queue.pop(0)

        engine._run = fake
        self.assertIs(engine._run_exposure(self.intent, None, None, 0, None, {}, {}), clean)
        self.assertEqual(calls, [{}, {BUYER: "earmark_cash_ceiling"}])

    def test_defensive_scope_follows_the_bound_stance_and_fails_closed_when_unbound(self):
        self.assertIsNotNone(self.engine("all")._earmark_active())
        self.assertIsNotNone(self.engine("defensive", "defensive")._earmark_active())
        self.assertIsNone(self.engine("defensive", "neutral")._earmark_active())
        engine = self.engine("defensive")
        engine.policy["position_sizing"] = dict(engine.policy["position_sizing"])
        engine.policy["position_sizing"].pop("cash_stance", None)
        with self.assertRaises(DecisionToolError):
            engine._earmark_active()

    def test_an_inactive_scope_is_the_same_allocation_as_no_gate(self):
        plain = AllocationOrderEngine(self.bundle, bind(self.policy)).run(self.intent)
        quiet = bind(self.policy, earmark=dict(EARMARK, scope="defensive"))
        self.assertEqual(quiet["position_sizing"]["cash_stance"], "neutral")
        body = AllocationOrderEngine(self.bundle, quiet).run(self.intent)
        for key in ("constraint_flags", "allocation_proposal", "order_proposal"):
            self.assertEqual(body[key], plain[key], key)


class VictimBearClaimTests(unittest.TestCase):
    """兩組提名（2412←1101、2317←2330）：2330 有本次採納的 bear-2330-1，1101 只採納多頭 claim。"""

    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="336000", positions={"2330.TW": 1000, "1101.TW": 1000}, **LIMITS)
        cls.intent, cls.policy, cls.plain, _ = v2_world(
            cls.bundle, four_symbol_items(), pairs=list(reversed(TWO_PAIRS)), policy_fields=LIMITS, level="neutral", grade="lower")

    def run_with(self, gate=True, intent=None):
        bound = copy.deepcopy(self.policy)
        if gate:
            bound["position_sizing"]["agent_swap"]["victim_bear_claim"] = dict(BEAR)
        bound["content_sha256"] = decision_policy_sha256(bound)
        intent = intent or self.intent
        body = AllocationOrderEngine(self.bundle, bound).run(intent)
        self.assertEqual(ProposalValidator(self.bundle, bound, intent).validate(body), [])
        return body

    def test_without_the_gate_both_pairs_fill(self):
        self.assertEqual(self.run_with(gate=False), self.plain)
        self.assertTrue({("1101.TW", "sell"), ("2330.TW", "sell"), ("2412.TW", "buy"), ("2317.TW", "buy")} <= set(traded(self.plain)))

    def test_a_victim_without_an_adopted_bear_claim_is_kept_and_only_its_pair_is_cancelled(self):
        body = self.run_with()
        self.assertIn(("2412.TW", "AGENT_SWAP_CANCELLED:victim_no_bear_claim"), flags(body))
        self.assertNotIn(("1101.TW", "sell"), traded(body))
        self.assertNotIn(("2412.TW", "buy"), traded(body))
        self.assertIn(("2330.TW", "AGENT_SWAP"), flags(body))  # 有空頭 claim 的換出者照常成交
        self.assertIn(("2317.TW", "buy"), traded(body))

    def test_adopting_a_bear_claim_for_the_victim_restores_the_pair(self):
        adopted = copy.deepcopy(self.intent)
        for item in adopted["items"]:
            if item["symbol"] == "1101.TW":
                item["adopted_claim_ids"] = ["bull-1101-1", "bear-1101-1"]
        body = self.run_with(intent=adopted)
        self.assertIn(("1101.TW", "sell"), traded(body))
        self.assertFalse(any(code.endswith("victim_no_bear_claim") for _, code in flags(body)))


RUN = ROOT / "artifacts" / "decisions" / "2026-11-17" / "decision-20261116T230305Z"


@unittest.skipUnless(RUN.exists(), "沒有 2026-11-17 的封存 run，略過")
class ArchivedRun1117Tests(unittest.TestCase):
    """2026-11-17 封存輸入重算（replay_reference_only）：換出者 2884 的賣款流向 2330 加碼、8415 只買到 11 張、2603←1101 因現金取消。"""

    @classmethod
    def setUpClass(cls):
        load = lambda name: json.loads((RUN / name).read_text(encoding="utf-8"))
        cls.bundle, cls.policy, cls.intent, cls.proposal = (
            load("decision_input.json"), load("policy.json"), load("intent.json"), load("proposal.json"))

    def run_with(self, policy):
        effective = self.proposal["effective_policy"]
        overrides = {key: effective[key] for key in (
            "cash_buffer_rate", "default_target_weight", "trim_fraction", "max_turnover_rate", "max_stock_weight", "max_positions")
            if key in effective}
        engine = ProposalValidator(self.bundle, policy, self.intent).engine
        return engine.run(self.intent, set(self.proposal.get("excluded_symbols", [])), overrides,
                          int(self.proposal.get("revision_count", 0)), self.proposal.get("parent_proposal_id"))

    def variant(self, **fields):
        policy = copy.deepcopy(self.policy)
        swap = policy["position_sizing"]["agent_swap"]
        swap.pop("proceeds_to_add", None)
        swap.update(copy.deepcopy(fields))
        policy["content_sha256"] = decision_policy_sha256(policy)
        return policy

    @staticmethod
    def core(body):
        return {key: body[key] for key in ("constraint_flags", "allocation_proposal", "order_proposal")}

    def test_the_archived_proposal_still_rebuilds(self):
        self.assertEqual(ProposalValidator(self.bundle, self.policy, self.intent).validate(self.proposal), [])
        self.assertEqual(self.run_with(self.policy), self.proposal)

    def test_proceeds_to_add_is_already_inert_under_entry_reserve(self):
        self.assertIn("entry_reserve", self.policy["position_sizing"]["agent_swap"])
        self.assertEqual(self.core(self.run_with(self.variant())), self.core(self.proposal))

    def test_victim_bear_claim_changes_nothing_because_every_victim_has_an_adopted_bear_claim(self):
        self.assertEqual(self.core(self.run_with(self.variant(victim_bear_claim=BEAR))), self.core(self.proposal))

    def test_earmark_keeps_the_proceeds_with_the_swaps_and_passes_the_guard(self):
        policy = self.variant(earmark=EARMARK, victim_bear_claim=BEAR)
        body = self.run_with(policy)
        orders = traded(body)
        self.assertNotIn(("2330.TW", "buy"), orders)  # 不再為加碼 2330 而賣股
        self.assertEqual(orders[("8415.TWO", "buy")], 67000)
        self.assertIn(("1101.TW", "sell"), orders)
        self.assertIn(("2603.TW", "buy"), orders)  # 2603←1101 救回
        self.assertNotIn(("6139.TW", "buy"), orders)  # UNNEEDED 組的換入者拿不到換股賣款
        self.assertIn(("6139.TW", "AGENT_SWAP_UNNEEDED"), flags(body))
        self.assertLess(Decimal(body["allocation_proposal"]["cash_weight"]), Decimal(policy["cash_weight_ceiling"]))
        self.assertEqual(ProposalValidator(self.bundle, policy, self.intent).validate(body), [])
        scenario = ScenarioEngine(self.bundle, policy).run(body)
        self.assertEqual(CompetitionGuardV2(self.bundle, policy).run(body, scenario)["status"], "passed")


class TemplateTests(unittest.TestCase):
    def load(self, name):
        return json.loads((ROOT / "config" / "policy_versions" / name).read_text(encoding="utf-8"))

    def test_enable_template_turns_both_gates_on_drops_proceeds_to_add_and_validates(self):
        template = self.load("2026-11-17-earmark-enable.json")
        rollback = self.load("2026-11-17-before-earmark.json")
        swap = template["position_sizing"]["agent_swap"]
        self.assertEqual(swap["earmark"], EARMARK)
        self.assertEqual(swap["victim_bear_claim"], BEAR)
        self.assertNotIn("proceeds_to_add", swap)
        # 啟用樣板與回滾樣板只差這三個欄位與策略識別、可得時間。
        before = copy.deepcopy(rollback)
        before_swap = before["position_sizing"]["agent_swap"]
        self.assertIn("proceeds_to_add", before_swap)
        self.assertIsNone(earmark_gate(before["position_sizing"]))
        before_swap.pop("proceeds_to_add")
        before_swap.update(earmark=EARMARK, victim_bear_claim=BEAR)
        before.update(policy_id=template["policy_id"], available_at=template["available_at"])
        self.assertEqual(before, template)
        policy = copy.deepcopy(template)
        policy.setdefault("cash_weight_ceiling", "0.5")  # 正式檔沒有這個欄位（由 pipeline 依 rules 綁定）
        policy["reuse_sell_proceeds"] = True
        errors = []
        validate_position_sizing(policy, ["1101.TW", "2317.TW", "2330.TW"], errors)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
