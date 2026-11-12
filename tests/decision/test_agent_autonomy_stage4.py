"""docs/plans/agent_autonomy_plan.md 階段 4（現金分配的小規則與冷卻改成提示）的版本閘門測試。

- ``cash_fit_order`` v2：新股買不到半份不再整筆取消（沒有 RISK_CASH_FIT），v2 不允許 ``cash_fit_min_fill_ratio``；v1 逐位元不變。
- ``high_conviction_cash_priority`` v2：保留「現金先給 high」，移除「high 被縮減後擋掉後面未持有的中低信心新股」；v1 不變。
- ``held_weight_budget`` v2：預算算法與 v1 逐位相同，只多持股權重事實（``held_weight_facts``）；事實整份重算、竄改即拒絕。
- ``reentry_cooldown`` v3：名單不擋買單，改成交易 Agent 的提示；買回須在 adopted_claim_ids 回應，沒回應的買單被取消並留旗標。

每項都有正常流程、與舊版的等價對照、fail-closed 分支。回放與等價測試只算 ``replay_reference_only``。
"""

import copy
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

from etf_agent.decision import (
    AllocationOrderEngine,
    DecisionPolicyValidator,
    ProposalValidator,
    TradeDecisionValidator,
    artifact_content_sha256,
    decision_policy_sha256,
)
from etf_agent.decision.fields import DecisionToolError
from etf_agent.decision.finalization import build_team_inputs
from etf_agent.decision.held_weight_facts import (
    HELD_WEIGHT_FACTS_KEY,
    build_held_weight_facts,
    held_weight_brief,
    validate_held_weight_facts,
)
from etf_agent.decision.sizing import (
    CASH_FIT_ORDER,
    CASH_FIT_ORDER_V2,
    HELD_WEIGHT_BUDGET,
    HELD_WEIGHT_BUDGET_V2,
    HIGH_CONVICTION_CASH_PRIORITY,
    HIGH_CONVICTION_CASH_PRIORITY_V2,
    TURNOVER_FIT,
    held_weight_facts_gate,
    reentry_cooldown_agent_only,
    reentry_cooldown_hint_only,
    reentry_hint_id,
    reentry_hint_symbols,
    validate_position_sizing,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

import test_exposure_rotation_e2e as e2e
from test_cash_fit_and_limits import gated, sizing_errors
from test_portfolio_decision_agent import refresh_artifact_hash
from test_portfolio_risk_decision import policy as base_policy, valid_inputs
from test_position_sizing import SIZING, sized_inputs, sized_policy
from test_risk_limit_gates import account, flags, guard, orders
from test_trader import items as trade_items, world as trader_world


def engine_with(sizing_fields, tiers, rank=None):
    """直接對 ``_fit_cash``／``_fit_turnover`` 做單元測試的引擎（緩衝 0、換手不限）。"""
    bundle, _, _, _ = valid_inputs()
    engine = AllocationOrderEngine(bundle, base_policy())
    sizing = {"conviction_by_symbol": tiers, **sizing_fields}
    if rank is not None:
        sizing["rank_by_symbol"] = rank
    engine.policy = {**engine.policy, "position_sizing": sizing}
    effective = engine._effective_policy({})
    effective["cash_buffer_rate"] = Decimal("0")
    effective["max_turnover_rate"] = Decimal("1")
    return engine, effective


def cost(engine, effective, symbol, shares):
    return -Decimal(engine._order(symbol, "buy", shares, "buy", effective)["net_cash_impact"])


def shares_of(orders_):
    return {item["symbol"]: int(item["shares"]) for item in orders_}


class CashFitOrderV2Tests(unittest.TestCase):
    """現金不足時：v1 把新股零頭整筆取消（RISK_CASH_FIT）；v2 買得到幾張買幾張。"""

    def case(self, version, current=None, ample=False):
        engine, effective = engine_with({"cash_fit_order": version}, {"2317.TW": "low", "2330.TW": "high"})
        low = engine._order("2317.TW", "buy", 4000, "buy", effective)
        high = engine._order("2330.TW", "buy", 2000, "buy", effective)
        cash = cost(engine, effective, "2330.TW", 2000) + cost(engine, effective, "2317.TW", 1000) + Decimal("1")
        if ample:
            cash = cost(engine, effective, "2330.TW", 2000) + cost(engine, effective, "2317.TW", 4000) + Decimal("1")
        fitted, _, fit_flags = engine._fit_cash([low, high], cash, cash, Decimal("100000000"), effective, current or {})
        return engine, effective, fitted, fit_flags

    def test_v1_cancels_the_dust_and_v2_keeps_it(self):
        _, _, v1, v1_flags = self.case(CASH_FIT_ORDER)
        self.assertEqual(shares_of(v1), {"2330.TW": 2000})
        self.assertEqual([(f["symbol"], f["code"]) for f in v1_flags], [("2317.TW", "RISK_CASH_FIT")])
        _, _, v2, v2_flags = self.case(CASH_FIT_ORDER_V2)
        self.assertEqual(shares_of(v2), {"2330.TW": 2000, "2317.TW": 1000})
        self.assertEqual([(f["symbol"], f["code"]) for f in v2_flags], [("2317.TW", "CASH_BUFFER")])
        self.assertIn("降為 1000", v2_flags[0]["detail"])

    def test_v2_still_funds_by_conviction_and_does_not_touch_held_adds(self):
        # 買單順序沒變：高信心先拿現金，所以 high 全買、low 只剩零頭。
        _, _, fitted, _ = self.case(CASH_FIT_ORDER_V2)
        self.assertEqual(shares_of(fitted)["2330.TW"], 2000)
        _, _, held, flags_ = self.case(CASH_FIT_ORDER_V2, current={"2317.TW": 3000})
        self.assertEqual(shares_of(held), {"2330.TW": 2000, "2317.TW": 1000})
        self.assertEqual([f["code"] for f in flags_], ["CASH_BUFFER"])

    def test_v2_equals_v1_when_nothing_is_clipped(self):
        _, _, v1, v1_flags = self.case(CASH_FIT_ORDER, ample=True)
        _, _, v2, v2_flags = self.case(CASH_FIT_ORDER_V2, ample=True)
        self.assertEqual(v1, v2)
        self.assertEqual((v1_flags, v2_flags), ([], []))

    def test_effective_policy_has_no_min_fill_ratio_under_v2_but_v1_keeps_it(self):
        engine, effective = engine_with({"cash_fit_order": CASH_FIT_ORDER}, {})
        self.assertEqual(effective["cash_fit_order"], "v1")
        self.assertEqual(effective["cash_fit_min_fill_ratio"], Decimal("0.5"))
        engine, effective = engine_with({"cash_fit_order": CASH_FIT_ORDER_V2}, {})
        self.assertEqual(effective["cash_fit_order"], "v2")
        self.assertNotIn("cash_fit_min_fill_ratio", effective)

    def test_min_fill_ratio_is_not_allowed_with_v2_and_unknown_versions_fail_closed(self):
        self.assertEqual(sizing_errors(cash_fit_order="v2"), [])
        self.assertTrue(any("cash_fit_min_fill_ratio" in e and "v2" in e for e in sizing_errors(cash_fit_order="v2", cash_fit_min_fill_ratio="0.5")))
        self.assertEqual(sizing_errors(cash_fit_order="v1", cash_fit_min_fill_ratio="0.5"), [])
        for bad in ("v3", "V2", 2, True, None):
            self.assertTrue(any("cash_fit_order" in e for e in sizing_errors(cash_fit_order=bad)), bad)

    def test_turnover_fit_dust_cancel_is_not_changed_by_v2(self):
        """換手護欄的新股殘量取消（TURNOVER_LIMIT）不在本版範圍：v2 與 v1 一樣取消（保守解讀，列在回報）。"""
        def run(version):
            engine, effective = engine_with({"cash_fit_order": version, "turnover_fit": TURNOVER_FIT}, {"2330.TW": "medium"})
            order = engine._order("2330.TW", "buy", 4000, "buy", effective)
            fitted, _, fit_flags = engine._fit_turnover({}, [order], Decimal("100000000"), Decimal(order["gross_amount"]) * Decimal("0.3"), effective)
            return fitted, fit_flags
        for version in (CASH_FIT_ORDER, CASH_FIT_ORDER_V2):
            fitted, fit_flags = run(version)
            self.assertEqual(fitted, [], version)
            self.assertEqual([f["code"] for f in fit_flags], ["TURNOVER_LIMIT"])

    def test_gate_is_human_only(self):
        self.assertIn("cash_fit_order", FORBIDDEN_PREFIXES)


class HighPriorityV2Tests(unittest.TestCase):
    """v1：high 被現金縮減後擋掉後面未持有的中低信心新股；v2 保留現金先給 high、移除封鎖。"""

    def case(self, fit, priority, current=None):
        fields = {"cash_fit_order": fit}
        if priority is not None:
            fields["high_conviction_cash_priority"] = priority
        engine, effective = engine_with(fields, {"2330.TW": "high", "2317.TW": "medium"})
        high = engine._order("2330.TW", "buy", 4000, "buy", effective)
        medium = engine._order("2317.TW", "buy", 2000, "buy", effective)
        cash = cost(engine, effective, "2330.TW", 1000) + cost(engine, effective, "2317.TW", 1000) + Decimal("1")
        fitted, _, fit_flags = engine._fit_cash([high, medium], cash, cash, Decimal("100000000"), effective, current or {})
        return shares_of(fitted), {(f["symbol"], f["code"]) for f in fit_flags}

    def test_v1_blocks_the_medium_new_stock(self):
        for fit in (CASH_FIT_ORDER, CASH_FIT_ORDER_V2):
            shares, codes = self.case(fit, HIGH_CONVICTION_CASH_PRIORITY)
            self.assertEqual(shares, {"2330.TW": 1000}, fit)
            self.assertIn(("2317.TW", "CASH_BUFFER"), codes)

    def test_v2_does_not_block_and_cash_still_goes_to_high_first(self):
        for fit in (CASH_FIT_ORDER, CASH_FIT_ORDER_V2):
            shares, codes = self.case(fit, HIGH_CONVICTION_CASH_PRIORITY_V2)
            # high 先拿到一張（零頭不取消），後面的中信心新股拿剩下的一張，不再被整批擋掉。
            self.assertEqual(shares, {"2330.TW": 1000, "2317.TW": 1000}, fit)
            self.assertNotIn(("2330.TW", "RISK_CASH_FIT"), codes)
            self.assertNotIn(("2317.TW", "RISK_CASH_FIT"), codes)

    def test_v2_without_the_priority_gate_is_the_plain_dust_path(self):
        shares, codes = self.case(CASH_FIT_ORDER, None)
        self.assertEqual(shares, {"2317.TW": 2000})
        self.assertIn(("2330.TW", "RISK_CASH_FIT"), codes)

    def test_v2_equals_v1_when_high_is_not_clipped(self):
        def run(priority):
            engine, effective = engine_with(
                {"cash_fit_order": CASH_FIT_ORDER_V2, "high_conviction_cash_priority": priority},
                {"2330.TW": "high", "2317.TW": "medium"})
            high = engine._order("2330.TW", "buy", 1000, "buy", effective)
            medium = engine._order("2317.TW", "buy", 1000, "buy", effective)
            cash = cost(engine, effective, "2330.TW", 1000) + cost(engine, effective, "2317.TW", 1000) + Decimal("1")
            return engine._fit_cash([high, medium], cash, cash, Decimal("100000000"), effective, {})[0]
        self.assertEqual(run(HIGH_CONVICTION_CASH_PRIORITY), run(HIGH_CONVICTION_CASH_PRIORITY_V2))

    def test_turnover_fit_keeps_high_dust_under_v2_as_under_v1(self):
        def run(priority):
            engine, effective = engine_with(
                {"cash_fit_order": CASH_FIT_ORDER_V2, "turnover_fit": TURNOVER_FIT, "high_conviction_cash_priority": priority},
                {"2330.TW": "high"})
            order = engine._order("2330.TW", "buy", 4000, "buy", effective)
            return engine._fit_turnover({}, [order], Decimal("100000000"), Decimal(order["gross_amount"]) * Decimal("0.3"), effective)[0]
        for priority in (HIGH_CONVICTION_CASH_PRIORITY, HIGH_CONVICTION_CASH_PRIORITY_V2):
            self.assertEqual([(o["symbol"], int(o["shares"])) for o in run(priority)], [("2330.TW", 1000)], priority)

    def test_gate_values_and_prerequisite_fail_closed(self):
        self.assertEqual(sizing_errors(cash_fit_order="v1", high_conviction_cash_priority="v2"), [])
        self.assertTrue(any("high_conviction_cash_priority" in e for e in sizing_errors(cash_fit_order="v1", high_conviction_cash_priority="v3")))
        self.assertTrue(any("high_conviction_cash_priority" in e for e in sizing_errors(high_conviction_cash_priority="v2")))  # 沒有 cash_fit_order
        self.assertIn("high_conviction_cash_priority", FORBIDDEN_PREFIXES)


class FullEngineInvariantTests(unittest.TestCase):
    """整條配置（engine.run＋ProposalValidator＋Guard）：v2 不破壞檔數、現金緩衝與換手上限，且與 v1 的差異只在版本字串。"""

    def body(self, fit_version, priority=None):
        bundle, intent, stance, policy = sized_inputs("high", "medium", stance="neutral")
        bundle = account(bundle, "180000", 3000)  # 2330 持 3 張，現金吃緊：2317 新股被 CASH_BUFFER 縮減
        policy = sized_policy(policy, intent, stance, bundle)
        fields = {"cash_buffer_floor": "v1", "cash_fit_order": fit_version, "turnover_fit": TURNOVER_FIT}
        if fit_version == CASH_FIT_ORDER:
            fields["cash_fit_min_fill_ratio"] = "0.5"
        if priority:
            fields["high_conviction_cash_priority"] = priority
        policy = gated(policy, **fields)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        return bundle, policy, body

    def test_v2_keeps_every_hard_limit_and_matches_v1_except_the_version_strings(self):
        bundle, policy, v2 = self.body(CASH_FIT_ORDER_V2, HIGH_CONVICTION_CASH_PRIORITY_V2)
        _, _, v1 = self.body(CASH_FIT_ORDER, HIGH_CONVICTION_CASH_PRIORITY)
        self.assertTrue(guard(bundle, policy, v2)["passed"])
        self.assertLessEqual(len(v2["allocation_proposal"]["positions"]), int(policy["max_positions"]))
        self.assertGreaterEqual(Decimal(v2["allocation_proposal"]["cash_weight"]), Decimal(policy["cash_buffer_rate"]) - Decimal("0.0001"))
        self.assertLessEqual(Decimal(v2["order_proposal"]["turnover_rate"]), Decimal(policy["max_turnover_rate"]))
        for key in ("order_proposal", "allocation_proposal", "constraint_flags"):
            self.assertEqual(v1[key], v2[key], key)  # 這個情境沒有零頭，v1 與 v2 的配置逐位相同
        self.assertEqual(v2["effective_policy"]["cash_fit_order"], "v2")
        self.assertNotIn("cash_fit_min_fill_ratio", v2["effective_policy"])
        self.assertEqual(v2["effective_policy"]["high_conviction_cash_priority"], "v2")

    def test_forged_effective_policy_version_is_rejected_on_rebuild(self):
        bundle, policy, body = self.body(CASH_FIT_ORDER_V2)
        intent = sized_inputs("high", "medium", stance="neutral")[1]
        forged = copy.deepcopy(body)
        forged["effective_policy"]["cash_fit_order"] = "v1"
        self.assertTrue(ProposalValidator(bundle, policy, intent).validate(forged))


class HeldWeightV2Tests(unittest.TestCase):
    """預算算法與 v1 逐位相同；v2 只多一份持股權重事實。"""

    def proposal(self, version):
        bundle, intent, stance, policy = sized_inputs("high", "medium", stance="neutral")
        bundle = account(bundle, "500000", 3000)
        policy = sized_policy(policy, intent, stance, bundle)
        policy = gated(
            policy, method="conviction_volatility_v2", volatility_exponent="0", held_weight_budget=version,
            selection_ranking="v1", rank_by_symbol={"2317.TW": 1, "2330.TW": 2}, selection_ranking_id="rank-1",
            selection_ranking_sha256="a" * 64,
        )
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        return bundle, policy, body

    def test_budget_is_identical_to_v1_and_the_flag_is_kept(self):
        _, _, v1 = self.proposal(HELD_WEIGHT_BUDGET)
        _, _, v2 = self.proposal(HELD_WEIGHT_BUDGET_V2)
        self.assertIn(("2330.TW", "RISK_HELD_WEIGHT"), flags(v2))
        for key in ("order_proposal", "allocation_proposal", "constraint_flags", "position_sizing"):
            self.assertEqual(v1[key], v2[key], key)
        self.assertEqual(v2["effective_policy"], v1["effective_policy"])

    def test_v2_requires_selection_ranking_and_unknown_versions_fail_closed(self):
        errors = []
        policy = base_policy()
        policy["position_sizing"] = {**copy.deepcopy(SIZING), "held_weight_budget": "v2"}
        validate_position_sizing(policy, ["2330.TW", "2317.TW"], errors)
        self.assertTrue(any("held_weight_budget=v2" in e and "selection_ranking" in e for e in errors), errors)
        for bad in ("v3", True, None):
            errors = []
            policy["position_sizing"] = {**copy.deepcopy(SIZING), "held_weight_budget": bad}
            validate_position_sizing(policy, ["2330.TW", "2317.TW"], errors)
            self.assertTrue(any("held_weight_budget" in e for e in errors), bad)
        self.assertIn("held_weight_budget", FORBIDDEN_PREFIXES)

    def test_gate_helper(self):
        self.assertTrue(held_weight_facts_gate({"held_weight_budget": "v2"}))
        self.assertFalse(held_weight_facts_gate({"held_weight_budget": "v1"}))
        self.assertFalse(held_weight_facts_gate({}))
        self.assertFalse(held_weight_facts_gate(None))


class HeldWeightFactsTests(unittest.TestCase):
    def setUp(self):
        bundle, intent, stance, policy = sized_inputs("high", "medium", stance="neutral")
        self.bundle = account(bundle, "500000", 3000)  # 2330：3000 股 × 164 ÷ NAV 992000
        policy = gated(policy, held_weight_budget=HELD_WEIGHT_BUDGET_V2, selection_ranking="v1")
        self.policy = policy
        self.alpha_policy = gated(policy, limit_scaled_conviction={"version": "v1", "alpha": "0.6"})

    def test_facts_report_the_current_weight_and_the_high_floor_only_with_the_alpha_gate(self):
        facts = build_held_weight_facts(self.bundle, self.policy)
        row = facts["holdings"][0]
        self.assertEqual(row["symbol"], "2330.TW")
        self.assertEqual(row["current_weight"], str((Decimal(3000) * Decimal("164") / Decimal("992000")).quantize(Decimal("0.000001"))))
        self.assertEqual((row["high_min_weight"], row["at_or_above_high_min"]), (None, None))  # 沒有 limit_scaled_conviction，不編造
        alpha = build_held_weight_facts(self.bundle, self.alpha_policy)["holdings"][0]
        limit = Decimal(self.policy["max_stock_weight"])
        self.assertEqual(Decimal(alpha["weight_limit"]), limit.quantize(Decimal("0.000001")))
        self.assertEqual(Decimal(alpha["high_min_weight"]), min(Decimal("0.6") * limit, limit * Decimal("0.995")).quantize(Decimal("0.000001")))
        self.assertIsInstance(alpha["at_or_above_high_min"], bool)

    def test_facts_are_deterministic_and_have_no_agent_text(self):
        first = build_held_weight_facts(self.bundle, self.policy)
        self.assertEqual(first, build_held_weight_facts(copy.deepcopy(self.bundle), copy.deepcopy(self.policy)))
        self.assertTrue(first["facts_id"].startswith("held-weight-facts:"))
        brief = held_weight_brief(first)
        self.assertIs(brief["background"]["citable"], False)
        self.assertNotIn("evidence_id", str(brief["by_symbol"]))  # 逐檔事實沒有可引用的識別碼
        self.assertEqual(set(brief["by_symbol"]), {"2330.TW"})

    def test_build_refuses_without_the_gate_or_without_a_holding_price(self):
        plain = copy.deepcopy(self.policy)
        plain["position_sizing"]["held_weight_budget"] = HELD_WEIGHT_BUDGET
        with self.assertRaises(DecisionToolError):
            build_held_weight_facts(self.bundle, plain)
        broken = copy.deepcopy(self.bundle)
        broken["snapshot"]["latest_prices"] = [row for row in broken["snapshot"]["latest_prices"] if row["symbol"] != "2330.TW"]
        with self.assertRaises(DecisionToolError):
            build_held_weight_facts(broken, self.policy)

    def test_validate_rejects_tampering_missing_extra_fields_and_gateless_policy(self):
        facts = build_held_weight_facts(self.bundle, self.policy)
        errors = []
        validate_held_weight_facts(self.bundle, self.policy, {HELD_WEIGHT_FACTS_KEY: facts}, errors)
        self.assertEqual(errors, [])
        for mutate, token in (
            (lambda f: f["holdings"][0].update(current_weight="0.010000"), "與重算結果不同"),
            (lambda f: f.update(note="改寫"), "與重算結果不同"),
            (lambda f: f.update(extra=1), "未允許欄位"),
        ):
            forged = copy.deepcopy(facts)
            mutate(forged)
            errors = []
            validate_held_weight_facts(self.bundle, self.policy, {HELD_WEIGHT_FACTS_KEY: forged}, errors)
            self.assertTrue(any(token in e for e in errors), (token, errors))
        errors = []
        validate_held_weight_facts(self.bundle, self.policy, {}, errors)
        self.assertTrue(any("必須是物件" in e for e in errors))
        plain = copy.deepcopy(self.policy)
        plain["position_sizing"]["held_weight_budget"] = HELD_WEIGHT_BUDGET
        errors = []
        validate_held_weight_facts(self.bundle, plain, {HELD_WEIGHT_FACTS_KEY: facts}, errors)
        self.assertTrue(any("只能用於" in e for e in errors))

    def test_facts_ignore_the_bound_trade_decision_so_base_and_final_policy_agree(self):
        bundle, intent, stance, _ = sized_inputs("high", "medium", stance="neutral")
        bound = sized_policy(self.policy, intent, stance, self.bundle)
        self.assertEqual(build_held_weight_facts(self.bundle, self.policy), build_held_weight_facts(self.bundle, bound))

    def test_team_inputs_key_needs_the_ranking_version_and_stays_optional(self):
        # 沒有傳入時 team_inputs 逐鍵不變（build_team_inputs 簽章多一個選配參數，預設 None）。
        facts = build_held_weight_facts(self.bundle, self.policy)
        with self.assertRaises(DecisionToolError):
            build_team_inputs({"technical": {}, "fundamental": {}, "event": {}}, None, {"level": "neutral"}, held_weight_facts=facts)


def reentry_world(version="v3", symbols=("2317.TW",), respond=True, intent_name="buy"):
    """2317 近期出場（名單綁在 Policy）、本次交易 Agent 想買回。"""
    bundle, intent, stance, policy = sized_inputs("high", "medium", stance="neutral")
    policy["position_sizing"]["reentry_cooldown"] = {
        "version": version, "days": 3, "blocked_symbols": list(symbols), "source_run_ids": ["run-1"],
    }
    policy["content_sha256"] = decision_policy_sha256(policy)
    if respond:
        intent = copy.deepcopy(intent)
        for item in intent["items"]:
            if item["symbol"] == "2317.TW":
                item["adopted_claim_ids"] = list(item["adopted_claim_ids"]) + [reentry_hint_id("2317.TW")]
        refresh_artifact_hash(intent)
    policy = sized_policy(policy, intent, stance, bundle)
    return bundle, intent, policy


class ReentryCooldownV3AllocationTests(unittest.TestCase):
    def run_engine(self, **kwargs):
        bundle, intent, policy = reentry_world(**kwargs)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        return body

    def test_a_responded_buy_back_is_not_blocked(self):
        body = self.run_engine()
        self.assertIn(("2317.TW", "buy"), orders(body))
        self.assertNotIn("RISK_REENTRY_COOLDOWN", {code for _, code in flags(body)})

    def test_an_unanswered_buy_back_is_cancelled_with_a_flag_and_nothing_else_changes(self):
        body = self.run_engine(respond=False)
        self.assertNotIn(("2317.TW", "buy"), orders(body))
        self.assertIn(("2317.TW", "RISK_REENTRY_COOLDOWN"), flags(body))
        detail = next(f["detail"] for f in body["constraint_flags"] if f["code"] == "RISK_REENTRY_COOLDOWN")
        self.assertIn("cooldown:2317.TW", detail)
        self.assertIn("交易 Agent 的意圖未被改寫", detail)

    def test_v2_still_blocks_even_when_the_response_is_present(self):
        body = self.run_engine(version="v2", respond=True)
        self.assertNotIn(("2317.TW", "buy"), orders(body))
        detail = next(f["detail"] for f in body["constraint_flags"] if f["code"] == "RISK_REENTRY_COOLDOWN")
        self.assertIn("不得再買進", detail)  # v1／v2 文案逐字不變

    def test_a_symbol_not_on_the_list_needs_no_response(self):
        body = self.run_engine(symbols=("1101.TW",), respond=False)
        self.assertIn(("2317.TW", "buy"), orders(body))
        self.assertNotIn("RISK_REENTRY_COOLDOWN", {code for _, code in flags(body)})

    def test_a_held_symbol_on_the_list_is_never_hinted(self):
        bundle, intent, policy = reentry_world(symbols=("2330.TW",), respond=False)  # 2330 已持有（add）
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertNotIn("RISK_REENTRY_COOLDOWN", {code for _, code in flags(body)})

    def test_forged_flag_or_order_is_rejected_on_rebuild(self):
        bundle, intent, policy = reentry_world(respond=False)
        body = AllocationOrderEngine(bundle, policy).run(intent)
        forged = copy.deepcopy(body)
        forged["constraint_flags"] = [f for f in forged["constraint_flags"] if f["code"] != "RISK_REENTRY_COOLDOWN"]
        self.assertTrue(ProposalValidator(bundle, policy, intent).validate(forged))

    def test_helpers(self):
        v3 = {"reentry_cooldown": {"version": "v3", "days": 3, "blocked_symbols": ["B", "A"], "source_run_ids": ["r"]}}
        self.assertTrue(reentry_cooldown_hint_only(v3))
        self.assertTrue(reentry_cooldown_agent_only(v3))  # 名單口徑同 v2：Finalizer 以 agent_only 重算
        self.assertEqual(reentry_hint_symbols(v3), ["A", "B"])
        for sizing in ({"reentry_cooldown": {"version": "v2", "days": 3, "blocked_symbols": ["A"], "source_run_ids": []}},
                       {"reentry_cooldown": {"version": "v1", "days": 3}}, {}, None):
            self.assertIsNone(reentry_hint_symbols(sizing))
            self.assertFalse(reentry_cooldown_hint_only(sizing))
        self.assertEqual(reentry_hint_id("2317.tw"), "cooldown:2317.TW")


class ReentryHintValidatorTests(unittest.TestCase):
    """交易 Agent 驗證只檢查提示的結構；沒回應不是驗證錯誤（避免耗盡修正循環丟掉整份決策）。"""

    def setUp(self):
        self.bundle, self.momentum, _, self.debate = trader_world()

    def decision(self, extra_adopted=(), extra_rejected=(), symbol="2317.TW"):
        values = trade_items()
        for item in values:
            if item["symbol"] == symbol:
                item["adopted_claim_ids"] = list(item["adopted_claim_ids"]) + list(extra_adopted)
                item["rejected_claim_ids"] = list(item["rejected_claim_ids"]) + list(extra_rejected)
        from test_trader import decision as seal
        return seal(self.bundle, self.debate, values)

    def validate(self, decision, hints=("2317.TW",)):
        return TradeDecisionValidator(self.bundle, self.momentum, self.debate, reentry_hint_symbols=hints).validate(decision)

    def test_response_is_accepted_and_missing_response_is_not_a_validation_error(self):
        self.assertEqual(self.validate(self.decision(["cooldown:2317.TW"])), [])
        self.assertEqual(self.validate(self.decision()), [])

    def test_without_hints_the_id_is_an_unknown_claim_like_before(self):
        errors = TradeDecisionValidator(self.bundle, self.momentum, self.debate).validate(self.decision(["cooldown:2317.TW"]))
        self.assertTrue(any("不屬於該股票的 claim" in e for e in errors), errors)
        self.assertEqual(TradeDecisionValidator(self.bundle, self.momentum, self.debate).validate(self.decision()), [])

    def test_hint_cannot_be_rejected_duplicated_or_used_on_another_symbol(self):
        errors = self.validate(self.decision(extra_rejected=["cooldown:2317.TW"]))
        self.assertTrue(any("冷卻提示" in e and "不可否決" in e for e in errors), errors)
        errors = self.validate(self.decision(["cooldown:2317.TW", "cooldown:2317.TW"]))
        self.assertTrue(any("不得重複" in e for e in errors), errors)
        errors = self.validate(self.decision(["cooldown:2317.TW"], symbol="2330.TW"))  # 2330 沒有提示、且已持有
        self.assertTrue(any("不屬於該股票的 claim" in e for e in errors), errors)

    def test_held_symbols_get_no_hint(self):
        errors = self.validate(self.decision(["cooldown:2330.TW"], symbol="2330.TW"), hints=("2330.TW",))
        self.assertTrue(any("不屬於該股票的 claim" in e for e in errors), errors)

    def test_hint_does_not_count_as_a_bull_basis(self):
        values = trade_items()
        values[0] = dict(values[0], adopted_claim_ids=["cooldown:2317.TW"], rejected_claim_ids=["bull-2317-1"])
        from test_trader import decision as seal
        errors = self.validate(seal(self.bundle, self.debate, values))
        self.assertTrue(any("至少一個被採納的多頭 claim" in e for e in errors), errors)


def chain_world(directory, **sizing_fields):
    """test_exposure_rotation_e2e 的 2.7 整條鏈，另把閘門放進 position_sizing（配置前就在）。"""
    original = e2e.exposure_sizing

    def with_fields(*args, **kwargs):
        sizing = original(*args, **kwargs)
        sizing.update(copy.deepcopy(sizing_fields))
        return sizing

    with mock.patch.object(e2e, "exposure_sizing", with_fields):
        return e2e.ChainWorld(directory)


class HeldWeightFinalizerChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.world = chain_world(cls.directory.name, held_weight_budget="v2")
        cls.plain = e2e.ChainWorld(cls.directory.name)
        cls.facts = build_held_weight_facts(cls.world.bundle, cls.world.policy)
        cls.team = cls.world.tampered_team(lambda team: team.update(held_weight_facts=cls.facts))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_gate_with_facts_is_approved_and_the_version_is_unchanged(self):
        result = self.world.finalize(team=self.team)
        self.assertEqual(result["status"], "approved", result["errors"])
        self.assertEqual(self.team["schema_version"], "2.7")

    def test_missing_tampered_or_gateless_facts_are_rejected(self):
        missing = self.world.finalize(team=self.world.team)
        self.assertEqual(missing["status"], "rejected")
        self.assertTrue(any("held_weight_budget=v2" in e for e in missing["errors"]), missing["errors"])
        tampered = copy.deepcopy(self.team)
        tampered["held_weight_facts"]["holdings"][0]["current_weight"] = "0.010000"
        result = self.world.finalize(team=tampered)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("held_weight_facts 與重算結果不同" in e for e in result["errors"]), result["errors"])
        sneaked = self.plain.tampered_team(lambda team: team.update(held_weight_facts=self.facts))
        result = self.plain.finalize(team=sneaked)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("held_weight_budget=v2" in e for e in result["errors"]), result["errors"])

    def test_facts_need_the_ranking_version(self):
        old = self.world.tampered_team(lambda team: (team.update(held_weight_facts=self.facts), team.update(schema_version="2.4")))
        result = self.world.finalize(team=old)
        self.assertEqual(result["status"], "rejected")

    def test_allocation_output_is_identical_with_and_without_v2(self):
        for key in ("allocation_proposal", "order_proposal", "constraint_flags"):
            self.assertEqual(self.world.proposal[key], self.plain.proposal[key], key)


class ReentryCooldownV3FinalizerTests(unittest.TestCase):
    """Finalizer 對 v3 名單的重算比對與提示結構檢查（冷卻名單以 patch 提供，與 Policy 綁定的一致才通過）。"""

    LIST = (["2317.TW"], ["run-x"])

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.cooldown = {"version": "v3", "days": 3, "blocked_symbols": ["2317.TW"], "source_run_ids": ["run-x"]}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def world(self, respond):
        original = e2e.decisions if hasattr(e2e, "decisions") else None
        patches = [mock.patch("etf_agent.decision.finalization.recent_exit_symbols", return_value=self.LIST)]
        if respond and original is not None:
            def with_response(*args, **kwargs):
                values = original(*args, **kwargs)
                for item in values:
                    if item["symbol"] == "2317.TW":
                        item["adopted_claim_ids"] = list(item["adopted_claim_ids"]) + ["cooldown:2317.TW"]
                return values
            patches.append(mock.patch.object(e2e, "decisions", with_response))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return chain_world(self.directory.name, reentry_cooldown=dict(self.cooldown))

    def test_a_responded_hint_passes_the_finalizer_and_the_buy_stands(self):
        world = self.world(respond=True)
        self.assertEqual(world.result["status"], "approved", world.result["errors"])
        self.assertIn(("2317.TW", "buy"), {(o["symbol"], o["side"]) for o in world.result["orders"]})
        self.assertNotIn(("2317.TW", "RISK_REENTRY_COOLDOWN"), flags(world.proposal))

    def test_an_unanswered_hint_cancels_only_that_buy_and_the_finalizer_rebuilds_it(self):
        world = self.world(respond=False)
        self.assertIn(("2317.TW", "RISK_REENTRY_COOLDOWN"), flags(world.proposal))
        self.assertNotIn(("2317.TW", "buy"), {(o["symbol"], o["side"]) for o in world.result["orders"]})
        self.assertFalse([e for e in world.result["errors"] if "冷卻" in e and "重算" in e], world.result["errors"])

    def test_a_list_that_differs_from_the_repository_recomputation_is_rejected(self):
        world = self.world(respond=False)
        with mock.patch("etf_agent.decision.finalization.recent_exit_symbols", return_value=([], [])):
            result = world.finalize()
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("與決策庫重算不一致" in e for e in result["errors"]), result["errors"])

    def test_v3_must_be_bound(self):
        unbound = {"version": "v3", "days": 3}
        original = e2e.exposure_sizing

        def sizing(*args, **kwargs):
            value = original(*args, **kwargs)
            value["reentry_cooldown"] = dict(unbound)
            return value

        with mock.patch.object(e2e, "exposure_sizing", sizing):
            world = e2e.ChainWorld(self.directory.name)
        self.assertEqual(world.result["status"], "rejected")
        self.assertTrue(any("v2 必須綁定" in e or "必須綁定" in e for e in world.result["errors"]), world.result["errors"])


if __name__ == "__main__":
    unittest.main()
