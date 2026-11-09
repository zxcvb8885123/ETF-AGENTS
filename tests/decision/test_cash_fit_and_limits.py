"""2026-11-03 執行面版本閘門的測試。

涵蓋：交割口徑（rules.reuse_sell_proceeds 正式值）、買單順序與塵埃取消（cash_fit_order）、
現金上限補救（cash_ceiling_fix）、單股封頂空間（stock_limit_headroom）、跳空預留（buy_gap_reserve），
以及「省略閘門的舊 Policy 逐位維持原重建行為」。
"""

import copy
import json
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionPolicyValidator,
    ProposalValidator,
    ScenarioEngine,
    decision_bundle_sha256,
    decision_policy_sha256,
    decision_rules_sha256,
)
from etf_agent.decision.contracts import parse_time
from etf_agent.decision.sizing import (
    BUY_GAP_RESERVE,
    BUY_GAP_RESERVE_RATE,
    CASH_CEILING_FIX,
    CASH_FIT_MIN_FILL_RATIO,
    CASH_FIT_ORDER,
    HIGH_CONVICTION_CASH_PRIORITY,
    INSUFFICIENT_FUNDS_VOID_BATCH,
    LIMIT_HEADROOM,
    LIMIT_HEADROOM_BUY,
    STOCK_LIMIT_HEADROOM,
    validate_position_sizing,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_portfolio_decision_agent import refresh_artifact_hash
from test_portfolio_risk_decision import chain_inputs, policy as base_policy, valid_inputs


ROOT = Path(__file__).resolve().parents[2]
LEGACY_EFFECTIVE_KEYS = {
    "cash_buffer_rate", "commission_rate", "default_target_weight", "lot_size", "max_positions", "max_stock_weight",
    "max_turnover_rate", "minimum_commission", "reuse_sell_proceeds", "sell_tax_rate", "slippage_bps", "trim_fraction",
}
ALL_GATES = {
    "cash_fit_order": CASH_FIT_ORDER, "cash_ceiling_fix": CASH_CEILING_FIX,
    "stock_limit_headroom": STOCK_LIMIT_HEADROOM, "buy_gap_reserve": BUY_GAP_RESERVE,
    "cash_buffer_floor": "v1",
}


def production_rules():
    return json.loads((ROOT / "config" / "decision_rules.json").read_text(encoding="utf-8"))


def live_production_policy():
    return json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))


def production_policy():
    """固定版本配置夾具，排除需要額外 Agent／帳本輸入的閘門；live 驗證另用 live_production_policy。"""
    policy = json.loads((ROOT / "config/policy_versions/2026-11-09-before-review.json").read_text(encoding="utf-8"))
    policy["position_sizing"].pop("cash_stance_recency", None)
    policy["position_sizing"].pop("exposure_rotation", None)  # 需要帳本持股歷史與 team_inputs 2.7，由 test_exposure_rotation* 覆蓋
    return policy


def with_rules(bundle, **changes):
    """改 bundle 內封存的 rules 並重算雜湊（live 檔不會被讀）。"""
    bundle["rules"].update(changes)
    bundle["rules"]["config_sha256"] = decision_rules_sha256(bundle["rules"])
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def gated(policy, **sizing_fields):
    """在已綁定交易決策的 policy 上加版本閘門並重新封存。"""
    result = copy.deepcopy(policy)
    result["position_sizing"].update(sizing_fields)
    result["content_sha256"] = decision_policy_sha256(result)
    return result


def sizing_errors(**fields):
    policy = base_policy()
    policy["position_sizing"] = {
        "method": "conviction_volatility_v1",
        "conviction_multipliers": {"high": "1.5", "medium": "1", "low": "0.5"},
        "volatility_floor": "0.01",
        "cash_buffer_by_stance": {"aggressive": "0.03", "neutral": "0.10", "defensive": "0.20"},
        **fields,
    }
    errors = []
    validate_position_sizing(policy, ["2330.TW", "2317.TW"], errors)
    return errors


class ProductionConfigTests(unittest.TestCase):
    def test_fixed_policy_fixture_is_independent_of_the_live_configuration(self):
        expected = production_policy()
        with patch(__name__ + ".live_production_policy", side_effect=AssertionError("舊 fixture 不得讀 live")):
            self.assertEqual(production_policy(), expected)

    def test_review_policy_has_its_own_identity_and_preserves_the_old_snapshot(self):
        from etf_agent.core import canonical_sha256, parse_aware_time

        archived = json.loads((ROOT / "config/policy_versions/2026-11-09-before-review.json").read_text(encoding="utf-8"))
        self.assertEqual(canonical_sha256(archived), "11f94abcd3d50043a4856065d5aa06d136f30ed1eb1bae5e7f2a232750c109a0")
        live = live_production_policy()
        self.assertNotEqual(live["policy_id"], archived["policy_id"])
        self.assertGreater(parse_aware_time(live["available_at"], "available_at"),
                           parse_aware_time(archived["available_at"], "available_at"))

    def test_production_rules_reuse_sell_proceeds_true_with_new_version(self):
        rules = production_rules()
        self.assertIs(rules["reuse_sell_proceeds"], True)
        # 2026-11-03 晚間再改一次（產業上限 0.6）：版本接續為 11-03b，可得時間不得晚於當晚 21:00 的下一個 cutoff。
        self.assertEqual(rules["version"], "decision-rules-2026-11-03b")
        self.assertEqual(Decimal(rules["max_sector_weight"]), Decimal("0.6"))
        available = parse_time(rules["available_at"], "available_at")
        self.assertLessEqual(available, parse_time("2026-11-03T21:00:00+08:00", "cutoff"))
        self.assertLessEqual(parse_time(rules["published_at"], "published_at"), available)
        # config_sha256 由 DecisionInputBuilder 計算，不手寫進檔案。
        self.assertNotIn("config_sha256", rules)
        self.assertEqual(len(decision_rules_sha256(rules)), 64)

    def test_production_policy_enables_every_gate_and_validates(self):
        sizing = production_policy()["position_sizing"]
        for field, value in ALL_GATES.items():
            self.assertEqual(sizing.get(field), value, field)
        self.assertEqual(Decimal(sizing["cash_fit_min_fill_ratio"]), CASH_FIT_MIN_FILL_RATIO)
        self.assertEqual(Decimal(sizing["buy_gap_reserve_rate"]), BUY_GAP_RESERVE_RATE)
        self.assertEqual(sizing["insufficient_funds_policy"], INSUFFICIENT_FUNDS_VOID_BATCH)
        policy = base_policy()
        policy["position_sizing"] = copy.deepcopy(sizing)
        errors = []
        validate_position_sizing(policy, ["2330.TW", "2317.TW"], errors)
        self.assertEqual(errors, [])

    def test_gates_are_forbidden_for_strategy_reflection(self):
        for field in ("cash_fit_order", "cash_fit_min_fill_ratio", "cash_ceiling_fix", "stock_limit_headroom",
                      "buy_gap_reserve", "buy_gap_reserve_rate", "insufficient_funds_policy",
                      "high_conviction_cash_floor", "limit_scaled_conviction", "cash_ratio_range", "cash_grade"):
            self.assertTrue(any(field == p or field.startswith(p) for p in FORBIDDEN_PREFIXES), field)


class ReuseSellProceedsTests(unittest.TestCase):
    """交割以淨額計：同日賣出款可買替補（正式 rules 為 true）；false 時維持舊重建行為。"""

    def _fit(self, reuse):
        bundle, _, _, _ = valid_inputs()
        with_rules(bundle, reuse_sell_proceeds=reuse)
        settings = base_policy()
        settings["reuse_sell_proceeds"] = reuse
        settings["content_sha256"] = decision_policy_sha256(settings)
        engine = AllocationOrderEngine(bundle, settings)
        engine.policy = {**engine.policy, "position_sizing": {"cash_buffer_floor": "v1"}}
        effective = engine._effective_policy({})
        self.assertIs(effective["reuse_sell_proceeds"], reuse)
        sell = engine._order("2330.TW", "sell", 5000, "exit", effective)
        buy = engine._order("2317.TW", "buy", 1000, "buy", effective)
        self.assertGreater(Decimal(sell["net_cash_impact"]), -Decimal(buy["net_cash_impact"]))
        return engine._fit_cash([sell, buy], Decimal("1000"), Decimal("1000"), Decimal("1000000000"), effective, {"2330.TW": 5000})

    def test_production_true_lets_exit_proceeds_fund_the_replacement(self):
        reuse = production_rules()["reuse_sell_proceeds"]
        self.assertIs(reuse, True)
        orders, cash, flags = self._fit(reuse)
        self.assertEqual([item["side"] for item in orders], ["sell", "buy"])
        self.assertEqual(flags, [])
        self.assertGreater(cash, Decimal("0"))

    def test_false_keeps_old_behaviour_and_drops_the_replacement(self):
        orders, _, flags = self._fit(False)
        self.assertEqual([item["side"] for item in orders], ["sell"])
        self.assertEqual([item["code"] for item in flags], ["CASH_BUFFER"])

    def test_bundle_rules_win_over_live_file(self):
        # 舊 run 封存 false 時，即使 live 檔已改 true，重建仍走 false（engine 只讀 bundle 與 policy）。
        bundle, _, _, intent = valid_inputs()
        with_rules(bundle, reuse_sell_proceeds=False)
        settings = base_policy()
        settings["reuse_sell_proceeds"] = False
        settings["content_sha256"] = decision_policy_sha256(settings)
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        self.assertIs(proposal["effective_policy"]["reuse_sell_proceeds"], False)
        self.assertEqual(ProposalValidator(bundle, settings, intent).validate(proposal), [])


class LegacyEffectivePolicyTests(unittest.TestCase):
    def test_policy_without_gates_keeps_legacy_effective_policy_keys(self):
        """2026-11-03 前封存的 run 其 effective_policy 沒有任何閘門鍵；多一個 null 鍵就會讓重建比對失敗。"""
        bundle, _, _, intent = valid_inputs()
        proposal = AllocationOrderEngine(bundle, base_policy()).run(intent)
        self.assertEqual(set(proposal["effective_policy"]), LEGACY_EFFECTIVE_KEYS)

    def test_bound_policy_without_gates_also_keeps_legacy_keys(self):
        bundle, _, _, intent, policy, _, _, _ = chain_inputs()
        proposal = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(set(proposal["effective_policy"]), LEGACY_EFFECTIVE_KEYS)


class CashFitOrderTests(unittest.TestCase):
    """現金不足時：啟用閘門依（信心、名次、代號）分錢，新股殘量低於比例就整筆取消；舊路徑依代號逐張減少。"""

    def _engine(self, gate, tiers, rank=None, ratio=None):
        bundle, _, _, _ = valid_inputs()
        engine = AllocationOrderEngine(bundle, base_policy())
        sizing = {"conviction_by_symbol": tiers}
        if gate:
            sizing["cash_fit_order"] = CASH_FIT_ORDER
        if rank is not None:
            sizing["rank_by_symbol"] = rank
        if ratio is not None:
            sizing["cash_fit_min_fill_ratio"] = ratio
        engine.policy = {**engine.policy, "position_sizing": sizing}
        effective = engine._effective_policy({})
        effective["cash_buffer_rate"] = Decimal("0")
        effective["max_turnover_rate"] = Decimal("1")
        return engine, effective

    def _cost(self, engine, effective, symbol, shares):
        return -Decimal(engine._order(symbol, "buy", shares, "buy", effective)["net_cash_impact"])

    def _case(self, gate, current, tiers=None, rank=None, ratio=None):
        engine, effective = self._engine(gate, tiers or {"2317.TW": "low", "2330.TW": "high"}, rank, ratio)
        low = engine._order("2317.TW", "buy", 4000, "buy", effective)
        high = engine._order("2330.TW", "buy", 2000, "add" if "2330.TW" in current else "buy", effective)
        # 剛好夠高信心全買＋低信心一張：低信心殘量 1／4 低於預設 0.5。
        cash = self._cost(engine, effective, "2330.TW", 2000) + self._cost(engine, effective, "2317.TW", 1000) + Decimal("1")
        orders, _, flags = engine._fit_cash([low, high], cash, cash, Decimal("100000000"), effective, current)
        return {item["symbol"]: int(item["shares"]) for item in orders}, flags

    def test_gate_cancels_dust_buy_of_new_symbol_and_funds_higher_conviction_first(self):
        shares, flags = self._case(True, current={})
        self.assertEqual(shares, {"2330.TW": 2000})
        self.assertEqual([(flag["symbol"], flag["code"]) for flag in flags], [("2317.TW", "RISK_CASH_FIT")])
        self.assertIn("交易 Agent 的意圖未被改寫", flags[0]["detail"])

    def test_gate_keeps_reduced_increment_for_held_add(self):
        shares, flags = self._case(True, current={"2317.TW": 3000})
        self.assertEqual(shares, {"2330.TW": 2000, "2317.TW": 1000})
        self.assertEqual([(flag["symbol"], flag["code"]) for flag in flags], [("2317.TW", "CASH_BUFFER")])

    def test_without_gate_symbol_order_wins_and_dust_is_kept(self):
        shares, flags = self._case(False, current={})
        self.assertIn("2317.TW", shares)
        self.assertGreater(shares["2317.TW"], shares.get("2330.TW", 0))
        self.assertNotIn("RISK_CASH_FIT", {flag["code"] for flag in flags})
        self.assertTrue({flag["code"] for flag in flags} <= {"CASH_BUFFER"})

    def _priority_case(self, priority, current=None):
        # 高信心新股只買得到 1 張（目標 4 張，低於 50%），後面有便宜的中信心買單。
        bundle, _, _, _ = valid_inputs()
        engine = AllocationOrderEngine(bundle, base_policy())
        sizing = {"conviction_by_symbol": {"2330.TW": "high", "2317.TW": "medium"}, "cash_fit_order": CASH_FIT_ORDER}
        if priority:
            sizing["high_conviction_cash_priority"] = HIGH_CONVICTION_CASH_PRIORITY
        engine.policy = {**engine.policy, "position_sizing": sizing}
        effective = engine._effective_policy({})
        effective["cash_buffer_rate"] = Decimal("0")
        effective["max_turnover_rate"] = Decimal("1")
        high = engine._order("2330.TW", "buy", 4000, "buy", effective)
        medium = engine._order("2317.TW", "buy", 2000, "buy", effective)
        cost = lambda s, n: -Decimal(engine._order(s, "buy", n, "buy", effective)["net_cash_impact"])
        cash = cost("2330.TW", 1000) + cost("2317.TW", 1000) + Decimal("1")
        orders, _, flags = engine._fit_cash([high, medium], cash, cash, Decimal("100000000"), effective, current or {})
        return {item["symbol"]: int(item["shares"]) for item in orders}, flags

    def test_priority_gate_gives_the_remaining_cash_to_high_and_blocks_medium(self):
        shares, flags = self._priority_case(True)
        self.assertEqual(shares, {"2330.TW": 1000})
        codes = {(flag["symbol"], flag["code"]) for flag in flags}
        self.assertNotIn(("2330.TW", "RISK_CASH_FIT"), codes)
        self.assertIn(("2317.TW", "CASH_BUFFER"), codes)

    def test_without_priority_gate_high_dust_is_cancelled_and_medium_gets_the_cash(self):
        shares, flags = self._priority_case(False)
        self.assertEqual(shares, {"2317.TW": 2000})
        self.assertIn(("2330.TW", "RISK_CASH_FIT"), {(flag["symbol"], flag["code"]) for flag in flags})

    def test_priority_gate_does_not_block_adds_to_held_stocks(self):
        shares, _ = self._priority_case(True, current={"2317.TW": 1000})
        self.assertEqual(shares.get("2330.TW"), 1000)
        self.assertGreater(shares.get("2317.TW", 0), 0)

    def test_priority_gate_does_not_block_when_high_is_cancelled_entirely(self):
        bundle, _, _, _ = valid_inputs()
        engine = AllocationOrderEngine(bundle, base_policy())
        engine.policy = {**engine.policy, "position_sizing": {
            "conviction_by_symbol": {"2330.TW": "high", "2317.TW": "medium"},
            "cash_fit_order": CASH_FIT_ORDER, "high_conviction_cash_priority": HIGH_CONVICTION_CASH_PRIORITY}}
        effective = engine._effective_policy({})
        effective["cash_buffer_rate"] = Decimal("0")
        effective["max_turnover_rate"] = Decimal("1")
        high = engine._order("2330.TW", "buy", 1000, "buy", effective)
        medium = engine._order("2317.TW", "buy", 1000, "buy", effective)
        high_one = -Decimal(engine._order("2330.TW", "buy", 1000, "buy", effective)["net_cash_impact"])
        medium_one = -Decimal(medium["net_cash_impact"])
        cash = medium_one + Decimal("1")
        self.assertLess(cash, high_one)  # 買不起一張高信心（fixture 價格若不符會在此失敗）
        orders, _, _ = engine._fit_cash([high, medium], cash, cash, Decimal("100000000"), effective, {})
        self.assertEqual({o["symbol"] for o in orders}, {"2317.TW"})

    def test_priority_gate_blocked_flag_explains_the_reason(self):
        _, flags = self._priority_case(True)
        detail = [f["detail"] for f in flags if (f["symbol"], f["code"]) == ("2317.TW", "CASH_BUFFER")]
        self.assertTrue(detail and "高信心" in detail[0])

    def test_priority_gate_two_high_buys_both_get_cash_in_rank_order(self):
        bundle, _, _, _ = valid_inputs()
        engine = AllocationOrderEngine(bundle, base_policy())
        engine.policy = {**engine.policy, "position_sizing": {
            "conviction_by_symbol": {"2330.TW": "high", "2317.TW": "high"},
            "rank_by_symbol": {"2330.TW": 1, "2317.TW": 2},
            "cash_fit_order": CASH_FIT_ORDER, "high_conviction_cash_priority": HIGH_CONVICTION_CASH_PRIORITY}}
        effective = engine._effective_policy({})
        effective["cash_buffer_rate"] = Decimal("0")
        effective["max_turnover_rate"] = Decimal("1")
        first = engine._order("2330.TW", "buy", 4000, "buy", effective)
        second = engine._order("2317.TW", "buy", 4000, "buy", effective)
        cost = lambda s, n: -Decimal(engine._order(s, "buy", n, "buy", effective)["net_cash_impact"])
        cash = cost("2330.TW", 1000) + cost("2317.TW", 1000) + Decimal("1")
        orders, _, flags = engine._fit_cash([second, first], cash, cash, Decimal("100000000"), effective, {})
        # 名次 1 的先拿到現金（被縮減但不取消零頭）；第二檔高信心不被擋，拿到剩下的現金。
        self.assertEqual({o["symbol"]: int(o["shares"]) for o in orders}, {"2330.TW": 1000, "2317.TW": 1000})
        self.assertNotIn("RISK_CASH_FIT", {f["code"] for f in flags})

    def test_turnover_fit_keeps_high_dust_only_with_the_priority_gate(self):
        from etf_agent.decision.sizing import TURNOVER_FIT

        def run(priority):
            bundle, _, _, _ = valid_inputs()
            engine = AllocationOrderEngine(bundle, base_policy())
            sizing = {"conviction_by_symbol": {"2330.TW": "high"}, "cash_fit_order": CASH_FIT_ORDER,
                      "turnover_fit": TURNOVER_FIT}
            if priority:
                sizing["high_conviction_cash_priority"] = HIGH_CONVICTION_CASH_PRIORITY
            engine.policy = {**engine.policy, "position_sizing": sizing}
            effective = engine._effective_policy({})
            effective["cash_buffer_rate"] = Decimal("0")
            effective["max_turnover_rate"] = Decimal("1")
            order = engine._order("2330.TW", "buy", 4000, "buy", effective)
            gross = Decimal(order["gross_amount"])
            # 預算只剩總額的 30%：必須砍到剩 1 張（低於目標 4 張的一半）。
            fitted, _, flags = engine._fit_turnover({}, [order], Decimal("100000000"), gross * Decimal("0.3"), effective)
            return fitted, flags

        kept, flags = run(True)
        self.assertEqual([(o["symbol"], int(o["shares"])) for o in kept], [("2330.TW", 1000)])
        self.assertEqual([f["code"] for f in flags], ["TURNOVER_LIMIT"])
        cancelled, _ = run(False)
        self.assertEqual(cancelled, [])

    def test_priority_gate_requires_cash_fit_order(self):
        sizing = {**production_policy()["position_sizing"]}
        del sizing["cash_fit_order"]
        del sizing["cash_fit_min_fill_ratio"]
        errors = []
        # 前提改由 GATE_PREREQUISITES 統一檢查（validate_gate_prerequisites），不再散在 _validate_execution_gates。
        from etf_agent.decision.sizing import validate_gate_prerequisites
        validate_gate_prerequisites(sizing, errors)
        self.assertTrue(any("high_conviction_cash_priority" in message for message in errors))

    def test_priority_gate_is_forbidden_for_strategy_reflection(self):
        self.assertIn("high_conviction_cash_priority", FORBIDDEN_PREFIXES)

    def test_rank_orders_within_the_same_conviction(self):
        shares, _ = self._case(True, current={}, tiers={"2317.TW": "high", "2330.TW": "high"},
                               rank={"2317.TW": 2, "2330.TW": 1})
        self.assertEqual(shares, {"2330.TW": 2000})
        shares, _ = self._case(True, current={}, tiers={"2317.TW": "high", "2330.TW": "high"},
                               rank={"2317.TW": 1, "2330.TW": 2})
        self.assertIn("2317.TW", shares)

    def test_min_fill_ratio_parameter_is_honoured(self):
        engine, effective = self._engine(True, {"2317.TW": "low", "2330.TW": "high"}, ratio="0.25")
        low = engine._order("2317.TW", "buy", 4000, "buy", effective)
        high = engine._order("2330.TW", "buy", 2000, "buy", effective)
        cash = self._cost(engine, effective, "2330.TW", 2000) + self._cost(engine, effective, "2317.TW", 1000) + Decimal("1")
        orders, _, flags = engine._fit_cash([low, high], cash, cash, Decimal("100000000"), effective, {})
        # 1／4 不低於 0.25，保留一張並記 CASH_BUFFER。
        self.assertEqual({item["symbol"]: int(item["shares"]) for item in orders}, {"2317.TW": 1000, "2330.TW": 2000})
        self.assertEqual([flag["code"] for flag in flags], ["CASH_BUFFER"])
        self.assertEqual(effective["cash_fit_min_fill_ratio"], Decimal("0.25"))

    def test_policy_validation_fails_closed(self):
        self.assertTrue(any("cash_fit_order" in e for e in sizing_errors(cash_fit_order="v2")))
        self.assertTrue(any("cash_fit_min_fill_ratio" in e for e in sizing_errors(cash_fit_min_fill_ratio="0.5")))
        for bad in ("0", "1.5", "abc"):
            self.assertTrue(any("cash_fit_min_fill_ratio" in e for e in sizing_errors(cash_fit_order="v1", cash_fit_min_fill_ratio=bad)), bad)
        self.assertEqual(sizing_errors(cash_fit_order="v1"), [])
        self.assertEqual(sizing_errors(cash_fit_order="v1", cash_fit_min_fill_ratio="1"), [])


class CashCeilingFixTests(unittest.TestCase):
    """2330 出場後現金比例達上限：啟用閘門時由程式留住續抱（RISK_CASH_CEILING），否則 Guard CASH_WEIGHT 拒絕。"""

    def _world(self, gate, min_positions=0):
        bundle, _, _, _ = valid_inputs()
        with_rules(bundle, cash_weight_must_be_below="0.45", min_positions=min_positions)
        settings = base_policy()
        settings.update({"cash_weight_ceiling": "0.45", "min_positions": min_positions, "max_turnover_rate": "1"})
        settings["content_sha256"] = decision_policy_sha256(settings)
        bundle, _, _, intent, policy, _, _, _ = chain_inputs(bundle, None, settings)
        intent = copy.deepcopy(intent)
        item = next(row for row in intent["items"] if row["symbol"] == "2330.TW")
        item.update(intent="exit", adopted_claim_ids=["bear-2330-1"], rejected_claim_ids=["bull-2330-1"])
        refresh_artifact_hash(intent)
        if gate:
            policy = gated(policy, cash_ceiling_fix=CASH_CEILING_FIX)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        proposal = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(proposal), [])
        scenario = ScenarioEngine(bundle, policy).run(proposal)
        guard = CompetitionGuardV2(bundle, policy).run(proposal, scenario)
        return proposal, guard

    def test_gate_keeps_exit_until_cash_is_below_ceiling(self):
        proposal, guard = self._world(True)
        codes = {(flag["symbol"], flag["code"]) for flag in proposal["constraint_flags"]}
        self.assertIn(("2330.TW", "RISK_CASH_CEILING"), codes)
        self.assertNotIn(("2330.TW", "RISK_MIN_POSITIONS"), codes)
        self.assertIn("2330.TW", {row["symbol"] for row in proposal["allocation_proposal"]["positions"]})
        self.assertEqual(next(row for row in proposal["allocation_proposal"]["positions"] if row["symbol"] == "2330.TW")["intent"], "exit")
        self.assertLess(Decimal(proposal["allocation_proposal"]["cash_weight"]), Decimal("0.45"))
        self.assertTrue(guard["passed"], [(c["rule_id"], c["details"]) for c in guard["checks"] if not c["passed"]])

    def test_without_gate_guard_rejects_on_cash_weight(self):
        proposal, guard = self._world(False)
        self.assertNotIn("RISK_CASH_CEILING", {flag["code"] for flag in proposal["constraint_flags"]})
        self.assertNotIn("2330.TW", {row["symbol"] for row in proposal["allocation_proposal"]["positions"]})
        self.assertGreaterEqual(Decimal(proposal["allocation_proposal"]["cash_weight"]), Decimal("0.45"))
        self.assertFalse(guard["passed"])
        self.assertIn("CASH_WEIGHT", {c["rule_id"] for c in guard["checks"] if not c["passed"]})

    def test_min_positions_keeps_the_exit_first_and_ceiling_then_passes(self):
        # 2317 買進後只有 1 檔，min_positions=2 先留住 2330（RISK_MIN_POSITIONS）；留住後現金已低於上限，不再標 CEILING。
        proposal, guard = self._world(True, min_positions=2)
        codes = {(flag["symbol"], flag["code"]) for flag in proposal["constraint_flags"]}
        self.assertIn(("2330.TW", "RISK_MIN_POSITIONS"), codes)
        self.assertNotIn(("2330.TW", "RISK_CASH_CEILING"), codes)
        self.assertTrue(guard["passed"])

    def test_policy_validation_fails_closed(self):
        self.assertTrue(any("cash_ceiling_fix" in e for e in sizing_errors(cash_ceiling_fix="v2")))
        self.assertEqual(sizing_errors(cash_ceiling_fix="v1"), [])


class StockLimitHeadroomTests(unittest.TestCase):
    def _targets(self, gate):
        bundle, _, _, intent, policy, _, _, _ = chain_inputs()
        if gate:
            policy = gated(policy, stock_limit_headroom=STOCK_LIMIT_HEADROOM)
        proposal = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(proposal), [])
        return proposal

    def test_buy_candidate_is_capped_at_0_97_of_the_limit(self):
        limit = Decimal(base_policy()["max_stock_weight"])
        gated_weight = Decimal(self._targets(True)["position_sizing"]["target_weights"]["2317.TW"])
        legacy_weight = Decimal(self._targets(False)["position_sizing"]["target_weights"]["2317.TW"])
        self.assertEqual(gated_weight, limit * LIMIT_HEADROOM_BUY)
        self.assertEqual(legacy_weight, limit * LIMIT_HEADROOM)
        self.assertLess(gated_weight, legacy_weight)

    def test_hold_only_position_still_uses_0_995(self):
        """既有持股因價格漂移超限：有無閘門都只減到 0.995，不會因 0.97 多賣（價格波動有 5 日寬限）。"""
        def trims(gate):
            bundle, _, _, _ = valid_inputs()
            account = bundle["account_snapshot"]
            account["positions"][0]["shares"] = 5000
            price = Decimal(bundle["snapshot"]["latest_prices"][0]["analysis_close_price"])
            account["nav"] = str(Decimal(account["cash"]) + 5000 * price)
            bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
            bundle, _, _, intent, policy, _, _, _ = chain_inputs(bundle)
            if gate:
                policy = gated(policy, stock_limit_headroom=STOCK_LIMIT_HEADROOM)
            proposal = AllocationOrderEngine(bundle, policy).run(intent)
            return [(o["symbol"], o["side"], o["shares"], o["intent"]) for o in proposal["order_proposal"]["orders"] if o["symbol"] == "2330.TW"]
        with_gate, without_gate = trims(True), trims(False)
        self.assertEqual(with_gate, without_gate)
        self.assertEqual(len(with_gate), 1)
        self.assertEqual((with_gate[0][1], with_gate[0][3]), ("sell", "trim"))

    def test_headroom_helper_and_validation(self):
        self.assertEqual(AllocationOrderEngine._headroom({"stock_limit_headroom": "v1"}, buying=True), LIMIT_HEADROOM_BUY)
        self.assertEqual(AllocationOrderEngine._headroom({"stock_limit_headroom": "v1"}, buying=False), LIMIT_HEADROOM)
        self.assertEqual(AllocationOrderEngine._headroom({}, buying=True), LIMIT_HEADROOM)
        self.assertTrue(any("stock_limit_headroom" in e for e in sizing_errors(stock_limit_headroom="v2")))
        self.assertEqual(sizing_errors(stock_limit_headroom="v1"), [])


class BuyGapReserveTests(unittest.TestCase):
    def _engine(self, gate, rate=None):
        bundle, _, _, _ = valid_inputs()
        engine = AllocationOrderEngine(bundle, base_policy())
        sizing = {}
        if gate:
            sizing["buy_gap_reserve"] = BUY_GAP_RESERVE
        if rate is not None:
            sizing["buy_gap_reserve_rate"] = rate
        engine.policy = {**engine.policy, "position_sizing": sizing}
        effective = engine._effective_policy({})
        effective["cash_buffer_rate"] = Decimal("0")
        effective["max_turnover_rate"] = Decimal("1")
        return engine, effective

    def test_reserve_requires_extra_cash_only_when_gated(self):
        for gate in (False, True):
            engine, effective = self._engine(gate)
            buy = engine._order("2317.TW", "buy", 2000, "buy", effective)
            cost = -Decimal(buy["net_cash_impact"])
            orders, _, flags = engine._fit_cash([buy], cost, cost, Decimal("100000000"), effective, {})
            if gate:
                self.assertEqual(effective["buy_gap_reserve_rate"], BUY_GAP_RESERVE_RATE)
                self.assertEqual([int(o["shares"]) for o in orders], [1000])
                self.assertEqual([f["code"] for f in flags], ["CASH_BUFFER"])
                enough = cost * (Decimal("1") + BUY_GAP_RESERVE_RATE)
                orders, _, flags = engine._fit_cash([buy], enough, enough, Decimal("100000000"), effective, {})
                self.assertEqual([int(o["shares"]) for o in orders], [2000])
                self.assertEqual(flags, [])
            else:
                self.assertNotIn("buy_gap_reserve_rate", effective)
                self.assertEqual([int(o["shares"]) for o in orders], [2000])
                self.assertEqual(flags, [])

    def test_reserve_is_cumulative_over_all_buys(self):
        engine, effective = self._engine(True)
        first = engine._order("2317.TW", "buy", 1000, "buy", effective)
        second = engine._order("2330.TW", "buy", 1000, "buy", effective)
        c1, c2 = -Decimal(first["net_cash_impact"]), -Decimal(second["net_cash_impact"])
        # 只預留第一筆的 3%：第二筆因累計預留不足而被整張砍掉。
        cash = c1 + c2 + c1 * BUY_GAP_RESERVE_RATE
        orders, _, flags = engine._fit_cash([first, second], cash, cash, Decimal("100000000"), effective, {})
        self.assertEqual([o["symbol"] for o in orders], ["2317.TW"])
        self.assertEqual([(f["symbol"], f["code"]) for f in flags], [("2330.TW", "CASH_BUFFER")])
        cash = (c1 + c2) * (Decimal("1") + BUY_GAP_RESERVE_RATE)
        orders, _, flags = engine._fit_cash([first, second], cash, cash, Decimal("100000000"), effective, {})
        self.assertEqual([o["symbol"] for o in orders], ["2317.TW", "2330.TW"])

    def test_custom_rate_and_validation(self):
        engine, effective = self._engine(True, rate="0.10")
        self.assertEqual(effective["buy_gap_reserve_rate"], Decimal("0.10"))
        buy = engine._order("2317.TW", "buy", 1000, "buy", effective)
        cost = -Decimal(buy["net_cash_impact"])
        orders, _, _ = engine._fit_cash([buy], cost * Decimal("1.05"), cost * Decimal("1.05"), Decimal("100000000"), effective, {})
        self.assertEqual(orders, [])
        self.assertTrue(any("buy_gap_reserve" in e for e in sizing_errors(buy_gap_reserve="v2")))
        self.assertTrue(any("buy_gap_reserve_rate" in e for e in sizing_errors(buy_gap_reserve_rate="0.03")))
        for bad in ("1", "-0.01", "x"):
            self.assertTrue(any("buy_gap_reserve_rate" in e for e in sizing_errors(buy_gap_reserve="v1", buy_gap_reserve_rate=bad)), bad)
        self.assertEqual(sizing_errors(buy_gap_reserve="v1", buy_gap_reserve_rate="0"), [])


class AllGatesEndToEndTests(unittest.TestCase):
    def test_all_gates_rebuild_through_validator_and_expose_effective_policy(self):
        bundle, _, _, intent, policy, _, _, _ = chain_inputs()
        policy = gated(policy, **ALL_GATES, cash_fit_min_fill_ratio="0.5", buy_gap_reserve_rate="0.03",
                       insufficient_funds_policy=INSUFFICIENT_FUNDS_VOID_BATCH)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        proposal = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(proposal), [])
        effective = proposal["effective_policy"]
        self.assertEqual(set(effective), LEGACY_EFFECTIVE_KEYS | set(ALL_GATES) | {"cash_fit_min_fill_ratio", "buy_gap_reserve_rate"})
        self.assertEqual(effective["cash_fit_min_fill_ratio"], "0.50000000")
        self.assertEqual(effective["buy_gap_reserve_rate"], "0.03000000")
        # insufficient_funds_policy 只影響帳本結算，不進配置的 effective_policy。
        self.assertNotIn("insufficient_funds_policy", effective)
        tampered = copy.deepcopy(proposal)
        tampered["effective_policy"].pop("cash_fit_order")
        self.assertTrue(ProposalValidator(bundle, policy, intent).validate(tampered))
        self.assertTrue(any("insufficient_funds_policy" in e for e in sizing_errors(insufficient_funds_policy="reduce_lots")))


if __name__ == "__main__":
    unittest.main()
