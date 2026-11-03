"""2026-11-03 風控閘門的測試：產業上限只擋增量（sector_fit）、換手上限主動縮減（turnover_fit）、出場冷卻（reentry_cooldown）、
回撤解除門檻與防守減碼（drawdown_control.release_*／drawdown_derisk）、強制換股需空頭證據（forced_swap.confirmation）。

每項都有正常流程與 fail-closed 分支；省略閘門的舊 Policy 維持原重建行為由 6 份封存 run 的重建驗證另行證明。
"""

import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionPolicyValidator,
    DecisionToolError,
    ProposalValidator,
    ScenarioEngine,
    bind_drawdown,
    bind_reentry_cooldown,
    decision_bundle_sha256,
    decision_policy_sha256,
    decision_rules_sha256,
    recent_exit_symbols,
    seal_trade_decision,
    stance_floor_path,
    trade_decision_envelope,
)
from etf_agent.decision.drawdown import validate_drawdown_control
from etf_agent.decision.sizing import (
    DRAWDOWN_DERISK_FRACTION,
    FORCED_SWAP_CONFIRMATION,
    HELD_ADD_PRIORITY,
    ROTATION_VERSION,
    SECTOR_FIT,
    TURNOVER_FIT,
    validate_position_sizing,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_cash_fit_and_limits import gated, production_policy, production_rules, with_rules
from test_portfolio_risk_decision import chain_inputs, policy as base_policy
from test_position_sizing import SIZING, sized_inputs, sized_policy


ROOT = Path(__file__).resolve().parents[2]
NEW_GATES = ("sector_fit", "turnover_fit", "reentry_cooldown", "drawdown_derisk")
CONTROL = {"neutral_at": "0.05", "defensive_at": "0.10", "release_neutral_below": "0.03", "release_defensive_below": "0.07"}
DERISK = {"version": "v1", "max_derisk_fraction": "0.25"}


def account(bundle, cash, shares_2330):
    """改帳戶現金與 2330 持股並重算 NAV 與雜湊（行情：2330=164、2317=112）。"""
    snapshot = bundle["account_snapshot"]
    snapshot["cash"] = snapshot["settled_cash"] = str(cash)
    snapshot["positions"] = [{"symbol": "2330.TW", "shares": shares_2330, "average_cost": "120"}] if shares_2330 else []
    snapshot["nav"] = str(Decimal(cash) + Decimal(shares_2330) * Decimal("164"))
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def flags(body):
    return {(flag["symbol"], flag["code"]) for flag in body["constraint_flags"]}


def orders(body):
    return {(o["symbol"], o["side"]): o for o in body["order_proposal"]["orders"]}


def guard(bundle, policy, body):
    scenario = ScenarioEngine(bundle, policy).run(body)
    return CompetitionGuardV2(bundle, policy).run(body, scenario)


def failed_rules(result):
    return [check["rule_id"] for check in result["checks"] if not check["passed"]]


class ProductionConfigTests(unittest.TestCase):
    def test_rules_cap_sector_at_60_percent_with_new_version(self):
        rules = production_rules()
        self.assertEqual(Decimal(rules["max_sector_weight"]), Decimal("0.6"))
        self.assertEqual(rules["version"], "decision-rules-2026-11-03b")

    def test_policy_caps_turnover_and_enables_every_new_gate(self):
        policy = production_policy()
        self.assertEqual(Decimal(policy["max_turnover_rate"]), Decimal("0.3"))
        sizing = policy["position_sizing"]
        self.assertEqual(sizing["sector_fit"], SECTOR_FIT)
        self.assertEqual(sizing["turnover_fit"], TURNOVER_FIT)
        self.assertEqual(sizing["reentry_cooldown"], {"version": "v1", "days": 3})
        self.assertEqual(sizing["drawdown_derisk"], DERISK)
        self.assertEqual(Decimal(DERISK["max_derisk_fraction"]), DRAWDOWN_DERISK_FRACTION)
        self.assertEqual(sizing["rotation"]["forced_swap"]["confirmation"], FORCED_SWAP_CONFIRMATION)
        self.assertEqual(policy["drawdown_control"], CONTROL)
        # 整份樣板（含解除門檻）通過 Validator。
        bundle = sized_inputs()[0]
        full = base_policy()
        full["position_sizing"] = copy.deepcopy(sizing)
        full["drawdown_control"] = dict(policy["drawdown_control"])
        full["content_sha256"] = decision_policy_sha256(full)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(full), [])

    def test_new_gates_are_forbidden_for_strategy_reflection(self):
        for field in NEW_GATES + ("drawdown_control.release_neutral_below", "drawdown_control.release_defensive_below",
                                  "rotation.forced_swap.confirmation"):
            self.assertTrue(any(field == p or field.startswith(p) for p in FORBIDDEN_PREFIXES), field)

    def test_gate_values_fail_closed(self):
        for bad in ({"sector_fit": "v2"}, {"turnover_fit": "v0"},
                    {"reentry_cooldown": {"version": "v1", "days": 0}},
                    {"reentry_cooldown": {"version": "v2", "days": 3}},
                    {"reentry_cooldown": {"version": "v1", "days": 3, "blocked_symbols": ["2317.TW"]}},
                    {"reentry_cooldown": {"version": "v1", "days": 3, "blocked_symbols": ["b", "a"], "source_run_ids": []}},
                    {"drawdown_derisk": {"version": "v2"}},
                    {"drawdown_derisk": {"version": "v1", "max_derisk_fraction": "0"}},
                    {"drawdown_derisk": {"version": "v1", "max_derisk_fraction": "1.5"}},
                    {"drawdown_derisk": {"version": "v1", "extra": 1}}):
            with self.subTest(bad=bad):
                policy = base_policy()
                policy["position_sizing"] = {**copy.deepcopy(SIZING), **bad}
                errors = []
                validate_position_sizing(policy, ["2330.TW", "2317.TW"], errors)
                self.assertTrue(errors, bad)


class SectorFitTests(unittest.TestCase):
    """兩檔同產業；2330 續抱已超過產業上限、2317 當日買進。"""

    def inputs(self, limit, shares_2330, cash="500000", sector_fit=True):
        bundle, intent, stance, policy = sized_inputs("medium", "low")
        bundle = account(bundle, cash, shares_2330)
        bundle = with_rules(bundle, max_sector_weight=limit)
        policy["max_sector_weight"] = limit
        policy["sector_by_symbol"] = {"2330.TW": "半導體", "2317.TW": "半導體"}
        policy["liquidity_fill_rate"] = "1"
        policy["max_turnover_rate"] = "1"
        # 2330 只 hold，不是本次候選；fixture 的交易決策是 2330 add，改成 hold 才能測「不賣既有持股」。
        items = [dict(item, intent="hold", conviction=None, adopted_claim_ids=["bull-2330-1"], rejected_claim_ids=["bear-2330-1"])
                 if item["symbol"] == "2330.TW" else item for item in intent["items"]]
        _, _, debate, _, _, _, _, _ = chain_inputs(copy.deepcopy(bundle))
        intent = seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), items)
        policy["content_sha256"] = decision_policy_sha256(policy)
        policy = sized_policy(policy, intent, stance, bundle)
        if sector_fit:
            policy = gated(policy, sector_fit=SECTOR_FIT)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        return bundle, intent, policy

    def test_over_limit_holding_is_not_sold_and_buy_is_cut_to_zero(self):
        # 2330 3 張 = 49.6% NAV > 30% 上限：買單整筆減光、2330 不賣，Guard 以 carried 容忍。
        bundle, intent, policy = self.inputs("0.30", 3000)
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertIn(("2317.TW", "RISK_SECTOR_WEIGHT"), flags(body))
        detail = next(f["detail"] for f in body["constraint_flags"] if f["code"] == "RISK_SECTOR_WEIGHT")
        self.assertIn("只減增量", detail)
        self.assertEqual(body["effective_policy"]["sector_fit"], SECTOR_FIT)
        result = guard(bundle, policy, body)
        self.assertTrue(result["passed"], failed_rules(result))
        check = next(c for c in result["checks"] if c["rule_id"] == "SECTOR_WEIGHT")
        self.assertEqual(check["details"]["violations"], [])
        self.assertEqual([c["sector"] for c in check["details"]["carried"]], ["半導體"])

    def test_buy_is_reduced_only_to_the_limit_when_room_remains(self):
        # 2330 1 張 = 24.7%，上限 45%：2317 只買得到 1 張，仍無賣單。
        bundle, intent, policy = self.inputs("0.45", 1000)
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        self.assertEqual({k: o["shares"] for k, o in orders(body).items()}, {("2317.TW", "buy"): 1000})
        self.assertIn(("2317.TW", "RISK_SECTOR_WEIGHT"), flags(body))
        result = guard(bundle, policy, body)
        self.assertTrue(result["passed"], failed_rules(result))
        self.assertEqual(next(c for c in result["checks"] if c["rule_id"] == "SECTOR_WEIGHT")["details"]["carried"], [])

    def test_without_gate_the_holding_is_trimmed_as_before(self):
        bundle, intent, policy = self.inputs("0.30", 3000, sector_fit=False)
        body = AllocationOrderEngine(bundle, policy).run(intent)
        sell = orders(body)[("2330.TW", "sell")]
        self.assertEqual(sell["intent"], "trim")
        self.assertNotIn("sector_fit", body["effective_policy"])
        result = guard(bundle, policy, body)
        self.assertTrue(result["passed"], failed_rules(result))
        self.assertIsInstance(next(c for c in result["checks"] if c["rule_id"] == "SECTOR_WEIGHT")["details"], list)

    def test_guard_without_gate_still_rejects_a_carried_over_limit_sector(self):
        """fail-closed：同一份「不賣持股」的提案，拿掉閘門的 Policy 進 Guard 必須因 SECTOR_WEIGHT 拒絕。"""
        bundle, intent, policy = self.inputs("0.30", 3000)
        body = AllocationOrderEngine(bundle, policy).run(intent)
        ungated = copy.deepcopy(policy)
        del ungated["position_sizing"]["sector_fit"]
        ungated["content_sha256"] = decision_policy_sha256(ungated)
        result = guard(bundle, ungated, body)
        self.assertIn("SECTOR_WEIGHT", failed_rules(result))

    def test_sector_with_a_buy_is_still_a_hard_violation_under_the_gate(self):
        """閘門只容忍「沒有買進」的產業：硬塞一張 2317 買單進已超限產業，Guard 仍拒絕。"""
        bundle, intent, policy = self.inputs("0.30", 3000)
        body = AllocationOrderEngine(bundle, policy).run(intent)
        engine = AllocationOrderEngine(bundle, policy)
        forged = copy.deepcopy(body)
        forged["order_proposal"]["orders"] = [engine._order("2317.TW", "buy", 1000, "buy", engine._effective_policy({}))]
        forged["allocation_proposal"]["positions"].append({"symbol": "2317.TW", "lots": 1, "shares": 1000, "price": "112.00",
                                                           "market_value": "112000", "weight": "0.11000000", "intent": "buy"})
        result = guard(bundle, policy, forged)
        self.assertIn("SECTOR_WEIGHT", failed_rules(result))


class TurnoverFitTests(unittest.TestCase):
    """現金 200 萬、2330 1 張：2317 買進（medium）、2330 加碼（high），兩筆買單合計換手 69%。"""

    def inputs(self, max_turnover, cash="2000000", tier_2317="medium", tier_2330="high", turnover_fit=True, min_positions=1):
        bundle, intent, stance, policy = sized_inputs(tier_2317, tier_2330)
        bundle = account(bundle, cash, 1000)
        bundle = with_rules(bundle, min_positions=min_positions)
        policy["min_positions"] = min_positions
        policy["max_turnover_rate"] = max_turnover
        policy["liquidity_fill_rate"] = "1"
        policy["content_sha256"] = decision_policy_sha256(policy)
        policy = sized_policy(policy, intent, stance, bundle)
        if turnover_fit:
            policy = gated(policy, turnover_fit=TURNOVER_FIT)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        return bundle, intent, policy

    def run_engine(self, bundle, intent, policy):
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        return body

    def test_weakest_buy_is_cancelled_first_and_guard_passes(self):
        bundle, intent, policy = self.inputs("0.40")
        body = self.run_engine(bundle, intent, policy)
        # 2317（medium）比 2330（high）弱：先被縮減；殘量不足目標一半就整筆取消（換股數減少）。
        self.assertEqual(sorted(orders(body)), [("2330.TW", "buy")])
        self.assertIn(("2317.TW", "TURNOVER_LIMIT"), flags(body))
        self.assertNotIn(("2330.TW", "TURNOVER_LIMIT"), flags(body))
        self.assertLessEqual(Decimal(body["order_proposal"]["turnover_rate"]), Decimal("0.40"))
        result = guard(bundle, policy, body)
        self.assertTrue(result["passed"], failed_rules(result))

    def test_partial_cut_keeps_the_buy_when_above_half_of_target(self):
        bundle, intent, policy = self.inputs("0.60")
        body = self.run_engine(bundle, intent, policy)
        self.assertEqual(orders(body)[("2317.TW", "buy")]["shares"], 4000)
        self.assertLessEqual(Decimal(body["order_proposal"]["turnover_rate"]), Decimal("0.60"))
        detail = next(f["detail"] for f in body["constraint_flags"] if f["code"] == "TURNOVER_LIMIT")
        self.assertIn("降為 4000", detail)

    def test_without_gate_the_old_path_clips_by_symbol_order(self):
        """對照組：舊路徑依取得現金的順序逐張擋換手，被削的是排在後面的 2330（high），不是最弱的 2317。"""
        bundle, intent, policy = self.inputs("0.40", turnover_fit=False)
        body = self.run_engine(bundle, intent, policy)
        self.assertIn(("2330.TW", "TURNOVER_LIMIT"), flags(body))
        self.assertNotIn(("2317.TW", "TURNOVER_LIMIT"), flags(body))
        self.assertNotIn("turnover_fit", body["effective_policy"])

    def test_sells_alone_over_the_limit_keep_an_exit_instead_of_rejecting(self):
        bundle, intent, stance, policy = sized_inputs("medium", "low")
        bundle = account(bundle, "500000", 1000)
        bundle = with_rules(bundle, min_positions=0)
        items = [dict(item, intent="exit", conviction=None, adopted_claim_ids=["bear-2330-1"], rejected_claim_ids=["bull-2330-1"])
                 if item["symbol"] == "2330.TW" else item for item in intent["items"]]
        _, _, debate, _, _, _, _, _ = chain_inputs(copy.deepcopy(bundle))
        intent = seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), items)
        policy["min_positions"] = 0
        policy["max_turnover_rate"] = "0.05"  # 2330 出場 164k 就是 24.7% 換手
        policy["content_sha256"] = decision_policy_sha256(policy)
        policy = gated(sized_policy(policy, intent, stance, bundle), turnover_fit=TURNOVER_FIT)
        body = self.run_engine(bundle, intent, policy)
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertIn(("2330.TW", "RISK_TURNOVER"), flags(body))
        self.assertEqual(body["allocation_proposal"]["positions"][0]["intent"], "exit")  # 交易 Agent 的意圖未被改寫
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_overrides_from_risk_review_still_tighten_the_fit(self):
        bundle, intent, policy = self.inputs("0.60")
        body = AllocationOrderEngine(bundle, policy).run(intent, overrides={"max_turnover_rate": "0.40"}, revision_count=1)
        self.assertEqual(sorted(orders(body)), [("2330.TW", "buy")])
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])

    def test_tampered_turnover_fit_is_rejected_on_rebuild(self):
        bundle, intent, policy = self.inputs("0.40")
        body = self.run_engine(bundle, intent, policy)
        forged = copy.deepcopy(body)
        forged["constraint_flags"] = [f for f in forged["constraint_flags"] if f["code"] != "TURNOVER_LIMIT"]
        self.assertTrue(ProposalValidator(bundle, policy, intent).validate(forged))


class ForcedSwapConfirmationTests(unittest.TestCase):
    """滿檔（max_positions=1，min_positions=0）、2330 續抱且在 broken 名單、2317 新買 high。"""

    def proposal(self, adopt_bear, confirmation=True, max_turnover="1", turnover_fit=False, min_positions=0):
        from test_team_chain import tradable_bundle
        from test_trader import items as trade_items

        full = tradable_bundle()
        full["rules"]["max_positions"] = 1
        full["rules"]["min_positions"] = min_positions
        full["rules"]["config_sha256"] = decision_rules_sha256(full["rules"])
        full["bundle_sha256"] = decision_bundle_sha256(full)
        bundle, _, debate, _, _, _, _, _ = chain_inputs(full)
        decisions = trade_items()
        decisions[0] = dict(decisions[0], conviction="high")
        decisions[1] = dict(
            decisions[1], intent="hold",
            adopted_claim_ids=["bull-2330-1", "bear-2330-1"] if adopt_bear else ["bull-2330-1"],
            rejected_claim_ids=[] if adopt_bear else ["bear-2330-1"],
        )
        intent = seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), decisions)
        stance = {"level": "neutral", "rationale": "市場廣度中性。", "evidence_ids": ["price-2330"]}
        policy = base_policy()
        policy["position_sizing"] = copy.deepcopy(SIZING)
        policy["position_sizing"]["held_add_priority"] = HELD_ADD_PRIORITY
        forced = {"version": "v1", "min_new_conviction": "high", "max_swaps_per_run": 1, "broken_symbols": ["2330.TW"],
                  "verdicts_ref": {"verdicts_id": "v-1", "as_of": "2026-10-30T11:30:00Z", "sha256": "a" * 64}}
        if confirmation:
            forced["confirmation"] = FORCED_SWAP_CONFIRMATION
        policy["position_sizing"]["rotation"] = {"version": ROTATION_VERSION, "max_swaps_per_run": 3,
                                                 "min_new_conviction": "low", "forced_swap": forced}
        if turnover_fit:
            policy["position_sizing"]["turnover_fit"] = TURNOVER_FIT
        policy["max_positions"] = 1
        policy["min_positions"] = min_positions
        policy["max_turnover_rate"] = max_turnover
        policy["liquidity_fill_rate"] = "1"  # fixture 的 50% 成交情境會讓 1 檔上限在換股時被誤判為 2 檔
        policy["content_sha256"] = decision_policy_sha256(policy)
        policy = sized_policy(policy, intent, stance, bundle)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        return bundle, policy, body

    def test_adopted_bear_claim_confirms_the_swap(self):
        bundle, policy, body = self.proposal(adopt_bear=True)
        self.assertEqual(orders(body)[("2330.TW", "sell")]["intent"], "exit")
        self.assertIn(("2317.TW", "buy"), orders(body))
        detail = next(f["detail"] for f in body["constraint_flags"] if f["code"] == "RISK_FORCED_SWAP")
        self.assertIn("依代號", detail)
        self.assertNotIn("20 日報酬", detail)
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_without_bear_evidence_nothing_is_swapped_and_the_reason_is_flagged(self):
        _, _, body = self.proposal(adopt_bear=False)
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertIn(("2330.TW", "RISK_FORCED_SWAP_UNCONFIRMED"), flags(body))
        self.assertNotIn(("2330.TW", "RISK_FORCED_SWAP"), flags(body))

    def test_old_gate_without_confirmation_swaps_on_the_list_alone(self):
        _, _, body = self.proposal(adopt_bear=False, confirmation=False)
        self.assertIn(("2330.TW", "RISK_FORCED_SWAP"), flags(body))
        detail = next(f["detail"] for f in body["constraint_flags"] if f["code"] == "RISK_FORCED_SWAP")
        self.assertIn("20 日報酬", detail)  # 舊文案逐字不變

    def test_need_is_capped_by_min_positions(self):
        """持股數 − min_positions = 0：換出後替補若買不成會跌破下限，所以不換。"""
        _, _, body = self.proposal(adopt_bear=True, min_positions=1)
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertNotIn(("2330.TW", "RISK_FORCED_SWAP"), flags(body))

    def test_turnover_cap_shrinks_the_replacement_but_keeps_the_swap(self):
        # 換出 2330（16.4 萬）＋ 2317 買 2 張（22.4 萬）= 58% 換手；上限 50% 時 2317 縮成 1 張（≥ 目標一半，不取消）。
        bundle, policy, body = self.proposal(adopt_bear=True, max_turnover="0.50", turnover_fit=True)
        self.assertEqual(orders(body)[("2317.TW", "buy")]["shares"], 1000)
        self.assertIn(("2330.TW", "RISK_FORCED_SWAP"), flags(body))
        self.assertIn(("2317.TW", "TURNOVER_LIMIT"), flags(body))
        self.assertLessEqual(Decimal(body["order_proposal"]["turnover_rate"]), Decimal("0.50"))
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_turnover_cap_below_the_victim_sale_keeps_the_victim(self):
        """換出的賣單本身就超過換手上限：被換出者被 RISK_TURNOVER 留住，整份不被拒絕。"""
        bundle, policy, body = self.proposal(adopt_bear=True, max_turnover="0.20", turnover_fit=True)
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertIn(("2330.TW", "RISK_TURNOVER"), flags(body))
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_bad_confirmation_value_fails_closed(self):
        errors = []
        policy = base_policy()
        policy["position_sizing"] = {**copy.deepcopy(SIZING), "held_add_priority": HELD_ADD_PRIORITY, "rotation": {
            "version": ROTATION_VERSION, "max_swaps_per_run": 3, "min_new_conviction": "low",
            "forced_swap": {"version": "v1", "min_new_conviction": "high", "max_swaps_per_run": 1, "confirmation": "v2"}}}
        validate_position_sizing(policy, ["2330.TW", "2317.TW"], errors)
        self.assertTrue(any("confirmation" in error for error in errors), errors)


def session(bundle, start):
    bundle["trading_status_bundle"] = {"target_session": {"start": start}}
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


class ReentryCooldownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundle, self.intent, self.stance, self.policy = sized_inputs("medium", "low")
        # 掃決策庫需要本次目標交易時段；fixture bundle 沒有交易狀態包，所以另備一份只加時段的副本給綁定用。
        self.session_bundle = session(copy.deepcopy(self.bundle), "2026-09-21T01:00:00+00:00")

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, run_id, day, start, cutoff, orders_, status="approved", account_id="account-1"):
        run = self.root / day / run_id
        run.mkdir(parents=True)
        (run / "decision.json").write_text(json.dumps({"status": status, "decision_cutoff": cutoff, "orders": orders_}), encoding="utf-8")
        (run / "decision_input.json").write_text(json.dumps({
            "decision_cutoff": cutoff, "account_snapshot": {"account_id": account_id},
            "trading_status_bundle": {"target_session": {"start": start}},
        }), encoding="utf-8")

    def test_recent_exits_are_collected_from_approved_runs_within_the_window(self):
        exit_order = [{"symbol": "2317.TW", "side": "sell", "intent": "exit"}, {"symbol": "2330.TW", "side": "sell", "intent": "trim"}]
        self.archive("run-recent", "2026-09-19", "2026-09-19T01:00:00+00:00", "2026-09-18T12:00:00+00:00", exit_order)
        self.archive("run-old", "2026-09-10", "2026-09-10T01:00:00+00:00", "2026-09-09T12:00:00+00:00",
                     [{"symbol": "1101.TW", "side": "sell", "intent": "forced_exit"}])
        self.archive("run-rejected", "2026-09-18", "2026-09-18T01:00:00+00:00", "2026-09-17T12:00:00+00:00",
                     [{"symbol": "2454.TW", "side": "sell", "intent": "exit"}], status="rejected")
        self.archive("run-other", "2026-09-18", "2026-09-18T01:00:00+00:00", "2026-09-17T12:00:00+00:00",
                     [{"symbol": "2603.TW", "side": "sell", "intent": "exit"}], account_id="account-2")
        symbols, runs = recent_exit_symbols(self.root, self.session_bundle, 3)
        self.assertEqual((symbols, runs), (["2317.TW"], ["run-recent"]))  # trim 不算出場；太舊、被拒、他帳戶都不算
        self.assertEqual(recent_exit_symbols(self.root, self.session_bundle, 12)[0], ["1101.TW", "2317.TW"])
        self.assertEqual(recent_exit_symbols(self.root / "missing", self.session_bundle, 3), ([], []))
        self.assertEqual(recent_exit_symbols(self.root, self.bundle, 3), ([], []))  # 本次沒有目標時段：不設冷卻，不是錯誤

    def test_bind_and_allocation_block_the_new_buy_but_not_a_held_add(self):
        self.archive("run-recent", "2026-09-19", "2026-09-19T01:00:00+00:00", "2026-09-18T12:00:00+00:00",
                     [{"symbol": "2317.TW", "side": "sell", "intent": "exit"}, {"symbol": "2330.TW", "side": "sell", "intent": "exit"}])
        template = copy.deepcopy(self.policy)
        template["position_sizing"]["reentry_cooldown"] = {"version": "v1", "days": 3}
        bound = bind_reentry_cooldown(template, self.root, self.session_bundle)
        cooldown = bound["position_sizing"]["reentry_cooldown"]
        self.assertEqual(cooldown, {"version": "v1", "days": 3, "blocked_symbols": ["2317.TW", "2330.TW"], "source_run_ids": ["run-recent"]})
        bound["content_sha256"] = decision_policy_sha256(bound)
        policy = sized_policy(bound, self.intent, self.stance, self.bundle)
        self.assertEqual(DecisionPolicyValidator(self.bundle).validate(policy), [])
        body = AllocationOrderEngine(self.bundle, policy).run(self.intent)
        self.assertEqual(ProposalValidator(self.bundle, policy, self.intent).validate(body), [])
        self.assertIn(("2317.TW", "RISK_REENTRY_COOLDOWN"), flags(body))
        self.assertNotIn(("2317.TW", "buy"), orders(body))
        self.assertNotIn(("2330.TW", "RISK_REENTRY_COOLDOWN"), flags(body))  # 已持有的加碼不受冷卻影響
        self.assertIn("run-recent", next(f["detail"] for f in body["constraint_flags"] if f["code"] == "RISK_REENTRY_COOLDOWN"))

    def test_template_without_the_gate_is_untouched(self):
        self.assertEqual(bind_reentry_cooldown(self.policy, self.root, self.session_bundle), self.policy)

    def test_bad_days_fails_closed_at_binding(self):
        template = copy.deepcopy(self.policy)
        template["position_sizing"]["reentry_cooldown"] = {"version": "v1", "days": 0}
        with self.assertRaises(DecisionToolError):
            bind_reentry_cooldown(template, self.root, self.session_bundle)


class DrawdownHysteresisTests(unittest.TestCase):
    def test_floor_path_releases_only_below_the_release_thresholds(self):
        D = Decimal
        self.assertEqual(stance_floor_path(CONTROL, [D(1000), D(940)]), "neutral")  # 6% 進 neutral
        self.assertEqual(stance_floor_path(CONTROL, [D(1000), D(940), D(960)]), "neutral")  # 4%：無記憶會解除，有解除門檻維持
        self.assertEqual(stance_floor_path(CONTROL, [D(1000), D(940), D(975)]), "aggressive")  # 2.5% < 3% 解除
        self.assertEqual(stance_floor_path(CONTROL, [D(1000), D(890), D(925)]), "defensive")  # 7.5% ≥ 7% 維持 defensive
        self.assertEqual(stance_floor_path(CONTROL, [D(1000), D(890), D(940)]), "neutral")  # 6% < 7% 降回 neutral
        self.assertEqual(stance_floor_path(CONTROL, [D(1000), D(890), D(980)]), "aggressive")  # 2% 直接解除
        self.assertEqual(stance_floor_path(CONTROL, [D(1000), D(890), D(1100)]), "aggressive")  # 創新高
        with self.assertRaises(DecisionToolError):
            stance_floor_path(CONTROL, [D(1000), D(0)])

    def setUp(self):
        self.bundle, _, _, _ = sized_inputs()
        self.nav = Decimal(self.bundle["account_snapshot"]["nav"])
        self.base = base_policy()
        self.base["position_sizing"] = {**copy.deepcopy(SIZING), "drawdown_derisk": dict(DERISK)}
        self.base["drawdown_control"] = dict(CONTROL)
        self.base["content_sha256"] = decision_policy_sha256(self.base)

    def history(self, *ratios):
        return [{"run_id": "run-%d" % i, "as_of": "2026-09-%02dT10:00:00+00:00" % (i + 1), "nav": str(self.nav * Decimal(r))}
                for i, r in enumerate(ratios)]

    def bound(self, *ratios):
        policy = bind_drawdown(self.base, self.bundle, self.history(*ratios))
        policy["content_sha256"] = decision_policy_sha256(policy)
        return policy

    def test_bound_floor_follows_the_path_and_validates_with_history(self):
        rows = self.history("1.0426", "1.0")  # 峰值 → 目前回撤 4.09%：無記憶 aggressive，走過 ≥5% 的點才有記憶
        policy = bind_drawdown(self.base, self.bundle, rows)
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.assertEqual(policy["drawdown_control"]["stance_floor"], "aggressive")
        rows = self.history("1.0638", "1.0")  # 峰值高 6.4%：目前回撤 6% 進 neutral
        policy = bind_drawdown(self.base, self.bundle, rows)
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.assertEqual(policy["drawdown_control"]["stance_floor"], "neutral")
        self.assertEqual(DecisionPolicyValidator(self.bundle).validate(policy, rows), [])
        # 歷史中間曾跌 6% 後回到 4%：無記憶門檻是 aggressive，解除門檻維持 neutral。
        rows = self.history("1.0417", "0.98", "1.0")
        policy = bind_drawdown(self.base, self.bundle, rows)
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.assertEqual(policy["drawdown_control"]["stance_floor"], "neutral")
        self.assertEqual(DecisionPolicyValidator(self.bundle).validate(policy, rows), [])
        self.assertEqual(DecisionPolicyValidator(self.bundle).validate(policy), [])  # 沒有歷史時只檢查不低於無記憶門檻

    def test_tampered_floor_is_rejected(self):
        rows = self.history("1.0417", "0.98", "1.0")
        policy = bind_drawdown(self.base, self.bundle, rows)
        policy["drawdown_control"]["stance_floor"] = "aggressive"
        policy["content_sha256"] = decision_policy_sha256(policy)
        errors = DecisionPolicyValidator(self.bundle).validate(policy, rows)
        self.assertTrue(any("重放" in e or "重算" in e for e in errors), errors)
        policy = self.bound("1.0638", "1.0")
        policy["drawdown_control"]["stance_floor"] = "aggressive"  # 無記憶門檻就是 neutral，沒有歷史也擋得住
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.assertTrue(any("不得低於" in e for e in DecisionPolicyValidator(self.bundle).validate(policy)))

    def test_release_fields_and_gate_must_appear_together_and_be_ordered(self):
        def errors_for(control, derisk):
            policy = base_policy()
            policy["position_sizing"] = {**copy.deepcopy(SIZING), **({"drawdown_derisk": dict(DERISK)} if derisk else {})}
            policy["drawdown_control"] = control
            errors = []
            validate_drawdown_control(policy, self.bundle, errors)
            return errors
        self.assertEqual(errors_for(dict(CONTROL), True), [])
        self.assertEqual(errors_for({"neutral_at": "0.05", "defensive_at": "0.10"}, False), [])
        self.assertTrue(errors_for(dict(CONTROL), False))
        self.assertTrue(errors_for({"neutral_at": "0.05", "defensive_at": "0.10"}, True))
        self.assertTrue(errors_for({**CONTROL, "release_neutral_below": "0.05"}, True))
        self.assertTrue(errors_for({**CONTROL, "release_defensive_below": "0.10"}, True))
        self.assertTrue(errors_for({**CONTROL, "release_defensive_below": "0.02"}, True))
        partial = dict(CONTROL)
        del partial["release_defensive_below"]
        self.assertTrue(errors_for(partial, True))


class DrawdownDeriskTests(unittest.TestCase):
    def inputs(self, cash, shares_2330, stance="defensive", derisk=True, fraction="0.25"):
        bundle, intent, stance_obj, policy = sized_inputs("medium", "low", stance)
        bundle = account(bundle, cash, shares_2330)
        # fixture 只有一檔持股：放寬單股上限，否則 RISK_STOCK_WEIGHT 會先減碼，測不到回撤減碼本身。
        bundle = with_rules(bundle, max_stock_weight="1", special_weight_limits={"2330.TW": "1"}, max_sector_weight="1")
        policy["max_stock_weight"] = "1"
        policy["special_weight_limits"] = {"2330.TW": "1"}
        policy["max_sector_weight"] = "1"
        policy["liquidity_fill_rate"] = "1"
        policy["max_turnover_rate"] = "1"
        # 2330 續抱（不是 add），減碼對象才是「交易 Agent 沒有要賣的持股」。
        items = [dict(item, intent="hold", conviction=None, adopted_claim_ids=["bull-2330-1"], rejected_claim_ids=["bear-2330-1"])
                 if item["symbol"] == "2330.TW" else item for item in intent["items"]]
        _, _, debate, _, _, _, _, _ = chain_inputs(copy.deepcopy(bundle))
        intent = seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), items)
        if derisk:
            policy["position_sizing"]["drawdown_derisk"] = {"version": "v1", "max_derisk_fraction": fraction}
            policy["drawdown_control"] = dict(CONTROL)
            policy = bind_drawdown(policy, bundle, [])  # 沒有更高的歷史點：回撤 0，下限 aggressive，姿態仍可由風險 Agent 選 defensive
        policy["content_sha256"] = decision_policy_sha256(policy)
        policy = sized_policy(policy, intent, stance_obj, bundle)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        return bundle, policy, body

    def test_defensive_stance_trims_to_the_cash_buffer(self):
        # 現金 10 萬、2330 5 張（82 萬）：現金 10.9% < 20% 緩衝，缺口 8.4 萬 → 賣 1 張。
        bundle, policy, body = self.inputs("100000", 5000)
        sell = orders(body)[("2330.TW", "sell")]
        self.assertEqual((sell["shares"], sell["intent"]), (1000, "trim"))
        self.assertIn(("2330.TW", "RISK_DRAWDOWN"), flags(body))
        self.assertEqual(next(p for p in body["allocation_proposal"]["positions"] if p["symbol"] == "2330.TW")["intent"], "hold")
        self.assertGreaterEqual(Decimal(body["allocation_proposal"]["cash_weight"]), Decimal("0.20"))
        self.assertNotIn(("2317.TW", "buy"), orders(body))  # 減碼收入不拿去買替補
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_derisk_is_capped_by_max_fraction_and_never_empties_the_last_position(self):
        _, _, body = self.inputs("0", 10000)  # 缺口 32.8 萬，上限 25% = 41 萬 → 2 張
        self.assertEqual(orders(body)[("2330.TW", "sell")]["shares"], 2000)
        _, _, body = self.inputs("0", 10000, fraction="0.10")  # 上限 16.4 萬 → 1 張
        self.assertEqual(orders(body)[("2330.TW", "sell")]["shares"], 1000)
        _, _, body = self.inputs("10000", 1000)  # 只有 1 檔 1 張且 min_positions=1：不得清空
        self.assertNotIn(("2330.TW", "sell"), orders(body))
        self.assertNotIn(("2330.TW", "RISK_DRAWDOWN"), flags(body))

    def test_neutral_stance_or_missing_gate_does_not_derisk(self):
        _, _, body = self.inputs("100000", 5000, stance="neutral")
        self.assertNotIn(("2330.TW", "sell"), orders(body))
        _, _, body = self.inputs("100000", 5000, derisk=False)
        self.assertNotIn(("2330.TW", "sell"), orders(body))
        self.assertNotIn(("2330.TW", "RISK_DRAWDOWN"), flags(body))

    def test_zero_budget_is_flagged_cash_buffer_under_the_gate_and_lot_size_without(self):
        # 現金 1 萬、2330 16.4 萬：預算 = 0.9 − 0.94 < 0，2317 的目標權重為 0。
        _, _, body = self.inputs("10000", 1000, stance="neutral")
        self.assertIn(("2317.TW", "CASH_BUFFER"), flags(body))
        self.assertNotIn(("2317.TW", "LOT_SIZE"), flags(body))
        _, _, body = self.inputs("10000", 1000, stance="neutral", derisk=False)
        self.assertIn(("2317.TW", "LOT_SIZE"), flags(body))
        self.assertNotIn(("2317.TW", "CASH_BUFFER"), flags(body))

    def test_tampered_derisk_sale_is_rejected_on_rebuild(self):
        bundle, policy, body = self.inputs("100000", 5000)
        _, intent, _, _ = sized_inputs("medium", "low", "defensive")
        items = [dict(item, intent="hold", conviction=None, adopted_claim_ids=["bull-2330-1"], rejected_claim_ids=["bear-2330-1"])
                 if item["symbol"] == "2330.TW" else item for item in intent["items"]]
        _, _, debate, _, _, _, _, _ = chain_inputs(copy.deepcopy(bundle))
        intent = seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), items)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        forged = copy.deepcopy(body)
        forged["order_proposal"]["orders"] = []
        forged["constraint_flags"] = [f for f in forged["constraint_flags"] if f["code"] != "RISK_DRAWDOWN"]
        self.assertTrue(ProposalValidator(bundle, policy, intent).validate(forged))


if __name__ == "__main__":
    unittest.main()
