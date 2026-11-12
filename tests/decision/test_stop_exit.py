"""position_sizing.stop_exit v1（程式兜底的條件式硬停損，已實作、尚未開啟）的測試。

覆蓋：閘門形狀與前提、觸發條件的邊界（虧損／輸交易池／持有交易日數）、單日上限、硬規則優先（最少檔數、不可交易）、缺資料不強制、
與換股換出者／回撤減碼／再進場冷卻的互動、沒有閘門時逐位元不變、Finalizer 重建竄改即拒絕、回放略過、策略反思禁區，
以及依計畫文件反事實設計的兩個情境（11-04 連跌尾段抄底、10-27 建倉）。

行情全是合成的（按文件的量級），不讀 artifacts/；所有數字只放在測試內，沒有寫進 config。
"""

import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from etf_agent.core import canonical_sha256
from etf_agent.decision import (
    AllocationOrderEngine,
    DecisionPolicyValidator,
    ProposalValidator,
    bind_reentry_cooldown,
    build_position_review,
    decision_bundle_sha256,
    decision_policy_sha256,
    recent_exit_symbols,
)
from etf_agent.decision.fields import DecisionToolError
from etf_agent.decision.finalization import DecisionRepository
from etf_agent.decision.sizing import GATE_FIELDS, GATE_PREREQUISITES, POSITION_SIZING_FIELDS, validate_position_sizing
from etf_agent.decision.stop_exit import (
    STOP_EXIT_CANCELLED_CODE, STOP_EXIT_CODE, STOP_EXIT_UNAVAILABLE_CODE, build_stop_exit_plan, validate_stop_exit_gate,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_exposure_rotation import LIMITS, REVIEW_POLICY, decision_item, exposure_sizing, gate_with, hold_item
from test_gate_prerequisites import stance, sized, three_symbol_bundle, three_symbol_intent
from test_portfolio_risk_decision import policy as base_policy
from test_risk_limit_gates import flags, orders

REVIEW_V2 = {
    "version": "v2", "loss_alert": None, "retrace_alert_20d": None, "gain_alert": None,
    "trail_alert": None, "relative_loss_alert": None, "stale_alert": None,
}
STOP = {"version": "v1", "loss": "0.15", "lag_to_pool": "0.08", "min_holding_days": 3, "max_exits_per_run": 2}
POSITIONS = {"2330.TW": 1000, "1101.TW": 5000}
ENTRY_PRICE = {"2330.TW": Decimal("164"), "1101.TW": Decimal("100"), "2317.TW": Decimal("112"), "2412.TW": Decimal("100")}


def shape(bundle, paths):
    """把每檔最後 len(path) 根收盤改成「進場日收盤 × 比例」（path 的第一個元素是進場日，通常是 1），最後一根同步 Snapshot 與帳戶 NAV。"""
    bars_by_symbol = {row["symbol"]: row["bars"] for row in bundle["price_series"]}
    for symbol, ratios in paths.items():
        bars = bars_by_symbol[symbol]
        for bar, ratio in zip(bars[-len(ratios):], ratios):
            close = (ENTRY_PRICE[symbol] * Decimal(str(ratio))).quantize(Decimal("0.01"))
            bar.update(open=str(close), high=str((close * Decimal("1.01")).quantize(Decimal("0.01"))),
                       low=str((close * Decimal("0.99")).quantize(Decimal("0.01"))), close=str(close))
        last = bars[-1]["close"]
        latest = next(row for row in bundle["snapshot"]["latest_prices"] if row["symbol"] == symbol)
        latest.update(close_price=last, analysis_close_price=last)
    for row in bundle["price_series"]:
        row["series_sha256"] = canonical_sha256(row["bars"])
    prices = {row["symbol"]: Decimal(row["close_price"]) for row in bundle["snapshot"]["latest_prices"]}
    account = bundle["account_snapshot"]
    account["nav"] = str(Decimal(account["cash"]) + sum(Decimal(p["shares"]) * prices[p["symbol"]] for p in account["positions"]))
    bundle["snapshot_sha256"] = canonical_sha256(bundle["snapshot"])
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def quiet_gate(since, positions):
    """曝險換股設成幾乎不會觸發（下限 0.01、margin 0.001），本檔只看停損；held_since_by_symbol 必須依代號排序。"""
    gate = gate_with(held_since_by_symbol={symbol: since for symbol in sorted(positions)})
    gate.pop("conviction_gap_swap", None)
    gate["beta_floor_by_stance"] = {"aggressive": "0.01", "neutral": "0.01", "defensive": None}
    gate["release_margin"] = "0.001"
    return gate


def flat(days):
    return [1] * (days + 1)


def crash(days, final):
    """進場日 1，之後線性走到 final（days 個交易日）。"""
    final = Decimal(str(final))
    return [1] + [float(Decimal(1) + (final - 1) * Decimal(step) / Decimal(days)) for step in range(1, days + 1)]


def build(paths, days=4, stop=STOP, intents=None, positions=POSITIONS, review=REVIEW_V2, with_stop=True, cash="336000", sizing_extra=None, **rules):
    """合成世界：三檔、兩檔持股；回傳 (bundle, intent, policy, body)。``days`` 是進場日之後已有收盤的交易日數。"""
    bundle = shape(three_symbol_bundle(cash, positions, **{**LIMITS, **rules}), paths)
    bars = bundle["price_series"][0]["bars"]
    since = "%sT06:00:00+00:00" % bars[-1 - days]["trade_date"]
    gate = quiet_gate(since, positions)
    sizing = exposure_sizing(bundle, gate)
    if with_stop:
        sizing["stop_exit"] = copy.deepcopy(stop)
    sizing.update(sizing_extra or {})
    items = intents if intents is not None else [
        hold_item("1101.TW", "medium", adopt=["bull-1101-1"]),
        decision_item("2317.TW", "no_trade"),
        hold_item("2330.TW", "medium", adopt=["bull-2330-1", "bear-2330-1"]),
    ]
    intent = three_symbol_intent(bundle, items)
    base = base_policy()
    base["sector_by_symbol"]["1101.TW"] = "水泥"
    fields = dict(position_review=copy.deepcopy(review), liquidity_fill_rate="1", max_turnover_rate="1", **{**LIMITS, **rules})
    policy, body = sized(bundle, intent, stance("neutral", "middle", "concentrated"), sizing, base, **fields)
    return bundle, intent, policy, body


def codes(body, symbol=None):
    return sorted(flag["code"] for flag in body["constraint_flags"] if symbol is None or flag["symbol"] == symbol)


def sells(body):
    return {order["symbol"]: order for order in body["order_proposal"]["orders"] if order["side"] == "sell"}


# 2330 自進場一路跌到 -16%，1101 與 2317 持平：個股 -16%、同期交易池約 -5.3%，輸 10.7 個百分點。
CRASH_2330 = {"2330.TW": crash(4, "0.84"), "1101.TW": flat(4), "2317.TW": flat(4)}


class GateShapeTests(unittest.TestCase):
    def errors(self, stop=STOP, review=REVIEW_V2, drop=()):
        bundle, _, _, _ = build(CRASH_2330)
        policy = base_policy()
        sizing = exposure_sizing(bundle, gate_with())
        sizing["stop_exit"] = copy.deepcopy(stop)
        for field in drop:
            sizing.pop(field)
        policy["position_sizing"] = sizing
        policy["position_review"] = copy.deepcopy(review)
        errors = []
        validate_position_sizing(policy, ["2330.TW", "2317.TW", "1101.TW"], errors)
        return errors

    def test_a_complete_gate_is_valid(self):
        self.assertEqual(self.errors(), [])

    def test_fields_are_required_and_closed(self):
        for field in STOP:
            changed = {key: value for key, value in STOP.items() if key != field}
            self.assertTrue(self.errors(changed), field)
        self.assertTrue(self.errors(dict(STOP, extra="1")))
        self.assertTrue(self.errors(dict(STOP, version="v2")))

    def test_values_are_validated(self):
        for change in ({"loss": "0"}, {"loss": "1"}, {"lag_to_pool": "-0.1"}, {"min_holding_days": 0}, {"min_holding_days": 2.5},
                       {"min_holding_days": True}, {"max_exits_per_run": 0}, {"max_exits_per_run": "2"}):
            self.assertTrue(self.errors(dict(STOP, **change)), change)

    def test_prerequisites(self):
        self.assertTrue(any("position_review v2" in error for error in self.errors(review=REVIEW_POLICY)))
        for field in ("exposure_rotation", "reentry_cooldown"):
            self.assertTrue(any("stop_exit 必須與 %s 同時存在" % field in error for error in self.errors(drop=(field,))), field)

    def test_the_gate_is_human_only_and_listed_in_the_table(self):
        rule = next(rule for rule in GATE_PREREQUISITES if rule.field == "stop_exit")
        self.assertTrue(rule.human_only)
        self.assertIn("stop_exit", GATE_FIELDS)
        self.assertIn("stop_exit", POSITION_SIZING_FIELDS)
        self.assertIn("stop_exit", FORBIDDEN_PREFIXES)
        self.assertIn("position_review.trail_alert", FORBIDDEN_PREFIXES)

    def test_standalone_validator_rejects_a_non_object(self):
        errors = []
        validate_stop_exit_gate({"position_sizing": {"stop_exit": "v1"}}, errors)
        self.assertTrue(errors)


class TriggerTests(unittest.TestCase):
    def test_a_deep_loss_that_also_lags_the_pool_sells_the_whole_position_with_the_flag(self):
        _, intent, policy, body = build(CRASH_2330)
        self.assertIn(STOP_EXIT_CODE, codes(body, "2330.TW"))
        order = sells(body)["2330.TW"]
        self.assertEqual((order["shares"], order["intent"]), (1000, "exit"))  # 整檔賣出、賣單意圖由程式標 exit
        self.assertNotIn("2330.TW", {position["symbol"] for position in body["allocation_proposal"]["positions"]})
        # 交易 Agent 的意圖沒有被改寫：封存的意圖仍是 hold。
        self.assertEqual({item["symbol"]: item["intent"] for item in intent["items"]}["2330.TW"], "hold")
        self.assertNotIn(STOP_EXIT_CODE, codes(body, "1101.TW"))
        detail = next(flag["detail"] for flag in body["constraint_flags"] if flag["code"] == STOP_EXIT_CODE)
        self.assertIn("16.00%", detail)
        self.assertIn("交易 Agent 的意圖未被改寫", detail)

    def test_the_whole_pool_falling_together_does_not_trigger(self):
        everyone = {symbol: crash(4, "0.84") for symbol in ("2330.TW", "1101.TW", "2317.TW")}
        _, _, _, body = build(everyone)
        self.assertNotIn(STOP_EXIT_CODE, codes(body))
        self.assertEqual(sells(body), {})

    def test_loss_boundary_is_inclusive_by_one_minimum_unit(self):
        exact = build(CRASH_2330, stop=dict(STOP, loss="0.16"))[3]  # 剛好 -16.0000%
        self.assertIn(STOP_EXIT_CODE, codes(exact))
        just_short = {**CRASH_2330, "2330.TW": crash(4, "0.8401")}
        self.assertNotIn(STOP_EXIT_CODE, codes(build(just_short, stop=dict(STOP, loss="0.16"))[3]))

    def test_the_lag_to_pool_boundary(self):
        # 個股 -16%，另兩檔各 +x：交易池 = (-0.16 + 2x) / 3，相對差 = -0.16 - pool。lag 取 0.12。
        # x = 0.0 → 差 -0.1067（未達 0.12，不觸發）；x = -0.08（它們也跌，池 -0.1067、差 -0.0533）更不觸發。
        self.assertNotIn(STOP_EXIT_CODE, codes(build(CRASH_2330, stop=dict(STOP, lag_to_pool="0.12"))[3]))
        rising = {"2330.TW": crash(4, "0.84"), "1101.TW": crash(4, "1.04"), "2317.TW": crash(4, "1.04")}  # 池 -0.0267，差 -0.1333
        self.assertIn(STOP_EXIT_CODE, codes(build(rising, stop=dict(STOP, lag_to_pool="0.12"))[3]))

    def test_holding_days_must_reach_the_minimum(self):
        for days, expected in ((2, False), (3, True), (4, True)):
            paths = {"2330.TW": crash(days, "0.84"), "1101.TW": flat(days), "2317.TW": flat(days)}
            body = build(paths, days=days)[3]
            self.assertEqual(STOP_EXIT_CODE in codes(body), expected, days)

    @staticmethod
    def three_holdings():
        return [
            hold_item("1101.TW", "medium", adopt=["bull-1101-1"]),
            hold_item("2317.TW", "medium", adopt=["bull-2317-1"]),
            hold_item("2330.TW", "medium", adopt=["bull-2330-1", "bear-2330-1"]),
        ]

    def test_the_daily_cap_keeps_the_biggest_losers_and_flags_the_rest(self):
        # 2317（沒持有）上漲 12% 把交易池拉高，兩檔持股都明顯輸給交易池。
        paths = {"2330.TW": crash(4, "0.78"), "1101.TW": crash(4, "0.82"), "2317.TW": crash(4, "1.12")}
        held3 = dict(positions=dict(POSITIONS, **{"2317.TW": 2000}), intents=self.three_holdings())
        capped = build(paths, stop=dict(STOP, max_exits_per_run=1), **held3)[3]
        self.assertIn(STOP_EXIT_CODE, codes(capped, "2330.TW"))  # 虧損較大的先出場
        self.assertNotIn(STOP_EXIT_CODE, codes(capped, "1101.TW"))
        self.assertIn(STOP_EXIT_CANCELLED_CODE, codes(capped, "1101.TW"))
        self.assertEqual(set(sells(capped)), {"2330.TW"})
        both = build(paths, stop=dict(STOP, max_exits_per_run=2), **held3)[3]
        self.assertEqual(set(sells(both)), {"2330.TW", "1101.TW"})

    def test_equal_losses_are_ordered_by_symbol(self):
        paths = {"2330.TW": crash(4, "0.80"), "1101.TW": crash(4, "0.80"), "2317.TW": crash(4, "1.12")}
        held3 = dict(positions=dict(POSITIONS, **{"2317.TW": 2000}), intents=self.three_holdings())
        body = build(paths, stop=dict(STOP, max_exits_per_run=1), **held3)[3]
        self.assertEqual(set(sells(body)), {"1101.TW"})

    def test_an_exit_already_chosen_by_the_agent_gets_no_extra_stop_flag(self):
        items = [
            hold_item("1101.TW", "medium", adopt=["bull-1101-1"]),
            decision_item("2317.TW", "no_trade"),
            decision_item("2330.TW", "exit", adopt=["bear-2330-1"], reject=["bull-2330-1"]),
        ]
        body = build(CRASH_2330, intents=items)[3]
        self.assertNotIn(STOP_EXIT_CODE, codes(body))
        self.assertIn("2330.TW", sells(body))

    def test_a_stop_overrides_trim_and_add_on_the_same_holding(self):
        for intent_name, conviction in (("trim", None), ("add", "high")):
            items = [
                hold_item("1101.TW", "medium", adopt=["bull-1101-1"]),
                decision_item("2317.TW", "no_trade"),
                decision_item("2330.TW", intent_name, conviction, adopt=["bull-2330-1"], reject=["bear-2330-1"]),
            ]
            body = build(CRASH_2330, intents=items)[3]
            self.assertEqual(sells(body)["2330.TW"]["shares"], 1000, intent_name)
            self.assertEqual(sells(body)["2330.TW"]["intent"], "exit")
            self.assertFalse([order for order in body["order_proposal"]["orders"] if order["side"] == "buy" and order["symbol"] == "2330.TW"], intent_name)


class FailClosedTests(unittest.TestCase):
    def test_missing_entry_facts_never_force_a_sale_and_leave_a_flag(self):
        bundle, intent, policy, _ = build(CRASH_2330)
        for field in ("held_since_by_symbol",):
            changed = copy.deepcopy(policy)
            changed["position_sizing"]["exposure_rotation"][field] = {"1101.TW": "2000-01-03T06:00:00+00:00", "2330.TW": "2000-01-03T06:00:00+00:00"}
            changed["content_sha256"] = decision_policy_sha256(changed)
            body = AllocationOrderEngine(bundle, changed).run(intent)
            self.assertIn(STOP_EXIT_UNAVAILABLE_CODE, codes(body, "2330.TW"))
            self.assertNotIn(STOP_EXIT_CODE, codes(body))
            self.assertEqual(sells(body), {})

    def test_a_bar_after_the_cutoff_is_rejected_not_filled_in(self):
        bundle, intent, policy, _ = build(CRASH_2330)
        broken = copy.deepcopy(bundle)
        for row in broken["price_series"]:
            row["bars"][-1]["available_at"] = "2099-01-01T00:00:00+00:00"
        with self.assertRaises(DecisionToolError):
            AllocationOrderEngine(broken, policy).run(intent)

    def test_an_untradable_holding_is_not_forced_out(self):
        bundle, intent, policy, _ = build(CRASH_2330)
        with patch.object(AllocationOrderEngine, "_untradable", return_value={"2330.TW"}):
            body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertIn(STOP_EXIT_CANCELLED_CODE, codes(body, "2330.TW"))
        self.assertNotIn(STOP_EXIT_CODE, codes(body))
        self.assertEqual(sells(body), {})

    def test_the_minimum_positions_rule_wins_over_the_stop(self):
        bundle, intent, policy, body = build(CRASH_2330, min_positions=2)
        self.assertEqual(sells(body), {})
        self.assertIn("RISK_MIN_POSITIONS", codes(body, "2330.TW"))
        self.assertIn(STOP_EXIT_CANCELLED_CODE, codes(body, "2330.TW"))
        self.assertNotIn(STOP_EXIT_CODE, codes(body))
        self.assertIn("2330.TW", {position["symbol"] for position in body["allocation_proposal"]["positions"]})
        # 兩檔都觸發、只留得住一檔時：依 ATR 由低到高先放回一檔，另一檔照停損出場。
        both = {"2330.TW": crash(4, "0.80"), "1101.TW": crash(4, "0.80"), "2317.TW": crash(4, "1.12")}
        body = build(both, min_positions=1)[3]
        self.assertEqual(len(sells(body)), 1)
        self.assertEqual(len([code for code in codes(body) if code == STOP_EXIT_CANCELLED_CODE]), 1)


class WithoutTheGateTests(unittest.TestCase):
    def test_a_policy_without_stop_exit_never_calls_the_review_math_and_is_bit_for_bit_unchanged(self):
        with patch("etf_agent.decision.stop_exit.build_position_review", side_effect=AssertionError("不應呼叫持股檢視")):
            plain = build(CRASH_2330, with_stop=False)
        _, _, _, with_gate = build(CRASH_2330)
        self.assertNotIn(STOP_EXIT_CODE, codes(plain[3]))
        self.assertEqual(sells(plain[3]), {})
        # 同一個世界只差一個閘門：沒有閘門的結果就是交易 Agent 的意圖照走（沒有任何賣單）。
        self.assertNotEqual(plain[3]["order_proposal"]["orders"], with_gate["order_proposal"]["orders"])

    def test_v1_review_policy_without_stop_exit_gives_the_same_orders_as_v2_without_stop_exit(self):
        v1 = build(CRASH_2330, with_stop=False, review=REVIEW_POLICY)[3]
        v2 = build(CRASH_2330, with_stop=False)[3]
        for key in ("orders", "projected_cash", "turnover_rate"):
            self.assertEqual(v1["order_proposal"][key], v2["order_proposal"][key])
        self.assertEqual(v1["constraint_flags"], v2["constraint_flags"])
        self.assertEqual(v1["allocation_proposal"], v2["allocation_proposal"])


class FinalizerTests(unittest.TestCase):
    def setUp(self):
        self.bundle, self.intent, self.policy, self.body = build(CRASH_2330)

    def errors(self, body=None, policy=None, bundle=None):
        return ProposalValidator(bundle or self.bundle, policy or self.policy, self.intent).validate(body or self.body)

    def test_the_sealed_proposal_rebuilds_cleanly(self):
        self.assertEqual(self.errors(), [])

    def test_removing_or_adding_a_stop_flag_or_order_is_rejected(self):
        changed = copy.deepcopy(self.body)
        changed["constraint_flags"] = [flag for flag in changed["constraint_flags"] if flag["code"] != STOP_EXIT_CODE]
        self.assertTrue(self.errors(changed))
        changed = copy.deepcopy(self.body)
        changed["order_proposal"]["orders"] = []
        self.assertTrue(self.errors(changed))

    def test_changed_thresholds_rebuild_to_a_different_result(self):
        for change in ({"loss": "0.20"}, {"lag_to_pool": "0.15"}, {"min_holding_days": 9}, {"max_exits_per_run": 1}):
            policy = copy.deepcopy(self.policy)
            policy["position_sizing"]["stop_exit"].update(change)
            policy["content_sha256"] = decision_policy_sha256(policy)
            if change == {"max_exits_per_run": 1}:
                continue  # 只有一檔觸發時上限不影響結果；上限的效果在 TriggerTests
            self.assertTrue(self.errors(policy=policy), change)

    def test_removing_the_gate_or_forging_the_held_since_changes_the_rebuild(self):
        policy = copy.deepcopy(self.policy)
        del policy["position_sizing"]["stop_exit"]
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.assertTrue(self.errors(policy=policy))
        forged = copy.deepcopy(self.policy)
        forged["position_sizing"]["exposure_rotation"]["held_since_by_symbol"]["2330.TW"] = "2000-01-03T06:00:00+00:00"
        forged["content_sha256"] = decision_policy_sha256(forged)
        self.assertTrue(self.errors(policy=forged))

    def test_a_policy_with_stop_exit_but_review_v1_is_rejected_as_a_policy(self):
        policy = copy.deepcopy(self.policy)
        policy["position_review"] = copy.deepcopy(REVIEW_POLICY)
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.assertTrue(any("position_review v2" in error for error in DecisionPolicyValidator(self.bundle).validate(policy)))


class InteractionTests(unittest.TestCase):
    def test_stop_proceeds_count_toward_the_derisk_cash_need(self):
        """防守姿態且現金低於緩衝時，停損的賣款先抵減補現金的缺口，回撤減碼因此賣得比較少。"""
        defensive = stance("defensive", "lower", "concentrated")
        results = {}
        for label, with_stop in (("with", True), ("without", False)):
            bundle = shape(three_symbol_bundle("20000", POSITIONS, **LIMITS), CRASH_2330)
            bars = bundle["price_series"][0]["bars"]
            since = "%sT06:00:00+00:00" % bars[-5]["trade_date"]
            gate = quiet_gate(since, POSITIONS)
            sizing = exposure_sizing(bundle, gate)
            if with_stop:
                sizing["stop_exit"] = copy.deepcopy(STOP)
            intent = three_symbol_intent(bundle, [
                hold_item("1101.TW", "medium", adopt=["bull-1101-1"]), decision_item("2317.TW", "no_trade"),
                hold_item("2330.TW", "medium", adopt=["bull-2330-1", "bear-2330-1"]),
            ])
            base = base_policy()
            base["sector_by_symbol"]["1101.TW"] = "水泥"
            _, body = sized(bundle, intent, defensive, sizing, base, position_review=copy.deepcopy(REVIEW_V2),
                            liquidity_fill_rate="1", max_turnover_rate="1", **LIMITS)
            results[label] = body
        derisk_with = [flag for flag in results["with"]["constraint_flags"] if flag["code"] == "RISK_DRAWDOWN"]
        derisk_without = [flag for flag in results["without"]["constraint_flags"] if flag["code"] == "RISK_DRAWDOWN"]
        self.assertTrue(derisk_without)  # 對照組真的有減碼，比較才有意義
        self.assertIn(STOP_EXIT_CODE, codes(results["with"], "2330.TW"))
        # 停損賣掉 2330 的賣款已把現金補過緩衝，回撤減碼不再賣別檔；沒有停損時同一天會減碼 1101。
        self.assertEqual(derisk_with, [])
        self.assertEqual(set(sells(results["with"])), {"2330.TW"})
        self.assertEqual(set(sells(results["without"])), {"1101.TW"})
        self.assertGreater(
            Decimal(results["with"]["order_proposal"]["projected_cash"]), Decimal(results["without"]["order_proposal"]["projected_cash"]))

    @staticmethod
    def swap_world(paths, with_stop=True, pairs=(("2317.TW", "2330.TW"),), four=False):
        """沿用 test_agent_swap_allocation 的三檔換股世界（2317 買進被名額擋下、提名 買 2317／賣 2330），行情換成合成的崩跌路徑。"""
        from test_agent_swap_allocation import bind_pairs
        from test_cash_stance_turn_facts import GATE as TURN_FACTS_GATE
        from test_exposure_rotation import exposure_bundle
        from test_exposure_rotation_e2e import allocate
        from test_exposure_rotation_v3 import gate_v3
        from test_rebound_participation_v2 import REBOUND_V2

        if four:
            from test_agent_swap_allocation import four_symbol_bundle, four_symbol_items
            bundle, items = shape(four_symbol_bundle(), paths), four_symbol_items()
        else:
            bundle, items = shape(exposure_bundle(), paths), None
        since = "%sT06:00:00+00:00" % bundle["price_series"][0]["bars"][-5]["trade_date"]
        gate = gate_v3(defensive=None, held_since_by_symbol={"1101.TW": since, "2330.TW": since})
        intent, policy, _ = allocate(bundle, items, "neutral", "middle", gate, policy_fields={"position_review": copy.deepcopy(REVIEW_V2)})
        bound = copy.deepcopy(policy)
        sizing = bound["position_sizing"]
        sizing["rotation"].pop("forced_swap", None)
        sizing["exposure_rotation"].pop("conviction_gap_swap", None)
        sizing["cash_stance_turn_facts"] = dict(TURN_FACTS_GATE)
        sizing["rebound_participation"] = dict(REBOUND_V2, effective_state="none")
        bind_pairs(sizing, list(pairs))
        if with_stop:
            sizing["stop_exit"] = copy.deepcopy(STOP)
        bound["content_sha256"] = decision_policy_sha256(bound)
        body = AllocationOrderEngine(bundle, bound).run(intent)
        problems = ProposalValidator(bundle, bound, intent).validate(body)
        if problems:
            raise AssertionError(problems)
        return body

    def test_a_stopped_holding_that_is_also_the_nominated_victim_is_sold_once_and_the_buyer_is_still_bought(self):
        """停損先把換出者賣掉並釋出名額，換入者本來就拿得到名額（AGENT_SWAP_UNNEEDED）：訂單與沒有停損、照提名換股時完全相同，賣單只有一張。"""
        stopped = self.swap_world(CRASH_2330)
        swapped = self.swap_world(CRASH_2330, with_stop=False)
        self.assertEqual(stopped["order_proposal"]["orders"], swapped["order_proposal"]["orders"])
        self.assertIn(STOP_EXIT_CODE, codes(stopped, "2330.TW"))
        self.assertIn("AGENT_SWAP_UNNEEDED", codes(stopped, "2317.TW"))
        self.assertEqual([order["side"] for order in stopped["order_proposal"]["orders"] if order["symbol"] == "2330.TW"], ["sell"])
        self.assertEqual(sells(stopped)["2330.TW"]["intent"], "exit")

    def test_a_freed_slot_goes_by_the_normal_ranking_and_a_nominated_buyer_that_loses_it_is_cancelled_not_sold_twice(self):
        """已知互動：停損釋出的名額先照一般入選排序分配。提名的換入者若沒搶到（這裡 2412 排在前面），該對以 swap_cap 取消；
        換出者本來就因停損賣出，所以仍只有一張賣單、而且有買單（不會只賣不買）。"""
        paths = dict(CRASH_2330, **{"2412.TW": flat(4)})
        body = self.swap_world(paths, four=True)
        self.assertIn(STOP_EXIT_CODE, codes(body, "2330.TW"))
        self.assertIn("AGENT_SWAP_CANCELLED:swap_cap", codes(body, "2317.TW"))
        self.assertEqual([order["side"] for order in body["order_proposal"]["orders"] if order["symbol"] == "2330.TW"], ["sell"])
        self.assertTrue([order for order in body["order_proposal"]["orders"] if order["side"] == "buy"])
        without = self.swap_world(paths, with_stop=False, four=True)
        self.assertIn("AGENT_SWAP", codes(without, "2330.TW"))  # 對照：沒有停損時照提名換股
        self.assertNotIn(STOP_EXIT_CODE, codes(without))


class ScenarioTests(unittest.TestCase):
    """依計畫文件第 4.1／4.2 節的反事實量級設計的情境（合成行情，replay_reference_only，不是對 LLM 的前瞻驗證）。"""

    def test_nov_04_dip_buying_entries_are_not_washed_out_in_the_first_two_days(self):
        # 11-04 進場的候選股兩天內就跌到 -16%、輸交易池：任何 8～12% 的硬停損都會賣在低點；本閘門要持有滿 3 個交易日。
        paths = {"2330.TW": crash(2, "0.84"), "1101.TW": flat(2), "2317.TW": flat(2)}
        body = build(paths, days=2)[3]
        self.assertEqual(sells(body), {})
        self.assertNotIn(STOP_EXIT_CODE, codes(body))
        # 反彈日：同一檔第三天已回到 -8%、不再輸給交易池很多 → 即使持有滿 3 天也不觸發。
        rebounded = {"2330.TW": [1, 0.90, 0.84, 0.92], "1101.TW": [1, 1, 1, 1.0], "2317.TW": [1, 1, 1, 1.0]}
        self.assertEqual(sells(build(rebounded, days=3)[3]), {})

    def test_oct_27_build_up_the_pool_falls_together_then_only_the_real_laggard_is_cut(self):
        # 10-27 建倉後大盤一路跌：三檔都 -10%，沒有人觸發（個股虧損不到 15%）。
        market = {symbol: crash(8, "0.90") for symbol in ("2330.TW", "1101.TW", "2317.TW")}
        self.assertEqual(sells(build(market, days=8)[3]), {})
        # 大盤同樣 -10%，只有 2330 自己崩到 -22%：個股 -22%、輸交易池約 8.7%（超過 8%）→ 觸發；另一檔 -10% 不動。
        laggard = {"2330.TW": crash(8, "0.78"), "1101.TW": crash(8, "0.90"), "2317.TW": crash(8, "0.90")}
        body = build(laggard, days=8)[3]
        self.assertEqual(set(sells(body)), {"2330.TW"})
        self.assertNotIn(STOP_EXIT_CODE, codes(body, "1101.TW"))

    def test_a_pool_wide_crash_with_a_slightly_worse_holding_is_left_to_the_trader(self):
        # 全池 -18%，2330 -20%：個股虧損達標但只輸交易池 2 個百分點（< 8）→ 不觸發，仍由持股檢視警示與交易 Agent 判斷。
        paths = {"2330.TW": crash(5, "0.80"), "1101.TW": crash(5, "0.82"), "2317.TW": crash(5, "0.82")}
        self.assertEqual(sells(build(paths, days=5)[3]), {})


class ReentryCooldownTests(unittest.TestCase):
    """停損出場的股票要進冷卻名單：交易 Agent 原意圖是 hold，所以 v2／v3（只計交易 Agent 自己的出場）原本會漏掉。"""

    CUTOFF = "2026-09-18T12:00:00+00:00"
    START = "2026-09-19T01:00:00+00:00"

    def setUp(self):
        from test_position_sizing import sized_inputs
        from test_risk_limit_gates import session

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundle, _, _, self.policy = sized_inputs("medium", "low")
        self.session_bundle = session(copy.deepcopy(self.bundle), "2026-09-21T01:00:00+00:00")

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, run_id, sells_, agent_intents, flags_=(), proposal=True, proposal_id="proposal:1", decision_proposal_id="proposal:1"):
        run = self.root / "2026-09-19" / run_id
        run.mkdir(parents=True)
        decision_id = "trade-decision:%s" % run_id
        artifacts = {
            "decision": {"status": "approved", "decision_cutoff": self.CUTOFF, "trade_intent_result_id": decision_id,
                         "proposal_id": decision_proposal_id,
                         "orders": [{"symbol": symbol, "side": "sell", "intent": "exit"} for symbol in sells_]},
            "decision_input": {"decision_cutoff": self.CUTOFF, "account_snapshot": {"account_id": "account-1"},
                               "trading_status_bundle": {"target_session": {"start": self.START}}},
            "intent": {"decision_id": decision_id, "items": [{"symbol": s, "intent": i} for s, i in agent_intents.items()]},
        }
        if proposal:
            artifacts["proposal"] = {"proposal_id": proposal_id, "constraint_flags": [
                {"symbol": symbol, "code": code, "detail": "x"} for symbol, code in flags_]}
        for name, payload in artifacts.items():
            (run / ("%s.json" % name)).write_text(json.dumps(payload), encoding="utf-8")
        (run / "manifest.json").write_text(json.dumps(DecisionRepository(self.root).build_manifest(run_id, artifacts)), encoding="utf-8")

    def recent(self, include=True):
        return recent_exit_symbols(self.root, self.session_bundle, 3, agent_only=True, include_stop_exits=include)

    def test_stopped_holdings_are_listed_only_when_the_policy_has_stop_exit(self):
        self.archive("run-1", ["2330.TW", "2454.TW"], {"2330.TW": "hold", "2454.TW": "hold"},
                     flags_=[("2330.TW", STOP_EXIT_CODE), ("2454.TW", "RISK_DRAWDOWN")])
        self.assertEqual(self.recent(), (["2330.TW"], ["run-1"]))  # 只有被停損的；回撤減碼賣出的不算
        self.assertEqual(self.recent(include=False), ([], []))  # 沒有 stop_exit：與舊版逐位元相同

    def test_the_agents_own_exit_and_a_stop_are_merged(self):
        self.archive("run-1", ["2330.TW", "2317.TW"], {"2330.TW": "hold", "2317.TW": "exit"}, flags_=[("2330.TW", STOP_EXIT_CODE)])
        self.assertEqual(self.recent()[0], ["2317.TW", "2330.TW"])

    def test_a_missing_or_mismatched_proposal_is_rejected_not_treated_as_no_stop(self):
        self.archive("run-1", ["2330.TW"], {"2330.TW": "hold"}, proposal=False)
        with self.assertRaises(DecisionToolError):
            self.recent()
        shutil_target = self.root / "2026-09-19" / "run-1"
        for child in shutil_target.iterdir():
            child.unlink()
        shutil_target.rmdir()
        self.archive("run-2", ["2330.TW"], {"2330.TW": "hold"}, proposal_id="proposal:other")
        with self.assertRaises(DecisionToolError):
            self.recent()

    def test_proposal_is_not_read_when_every_exit_was_the_agents_own(self):
        self.archive("run-1", ["2317.TW"], {"2317.TW": "exit"}, proposal=False)  # 沒有 proposal.json 也不應失敗
        self.assertEqual(self.recent(), (["2317.TW"], ["run-1"]))

    def test_bind_passes_the_flag_only_when_the_policy_has_stop_exit(self):
        self.archive("run-1", ["2330.TW"], {"2330.TW": "hold"}, flags_=[("2330.TW", STOP_EXIT_CODE)])
        for stop, expected in ((True, ["2330.TW"]), (False, [])):
            template = copy.deepcopy(self.policy)
            template["position_sizing"]["reentry_cooldown"] = {"version": "v3", "days": 3}
            if stop:
                template["position_sizing"]["stop_exit"] = copy.deepcopy(STOP)
            bound = bind_reentry_cooldown(template, self.root, self.session_bundle)
            self.assertEqual(bound["position_sizing"]["reentry_cooldown"]["blocked_symbols"], expected)


class ReplayTests(unittest.TestCase):
    def test_replay_ignores_stop_exit_and_replays_review_v2_as_v1(self):
        from etf_agent.evaluation import replay

        source = open(replay.__file__, encoding="utf-8").read()
        self.assertIn('notes["stop_exit"] = "ignored_in_replay"', source)
        self.assertIn('notes["position_review_v2"] = "replayed_as_v1"', source)


if __name__ == "__main__":
    unittest.main()
