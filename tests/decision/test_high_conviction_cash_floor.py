"""高信心現金下限（position_sizing.high_conviction_cash_floor，版本閘門）的測試。

high 買單可把現金用到 min(NAV × 姿態緩衝 × floor_ratio, 起始現金)，medium／low 維持 cash_buffer_floor；
drawdown_derisk 的觸發與目標同改為軟下限（帶狀防守）；當日觸發減碼時不套用；份額預算不變。
省略閘門的舊 Policy 逐位維持原重建行為（固定 fixture 雜湊）。
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
    MomentumEngine,
    ProposalValidator,
    RiskReviewValidator,
    ScenarioEngine,
    bind_drawdown,
    decision_policy_sha256,
    revision_effects,
    seal_trade_decision,
    trade_decision_envelope,
)
from etf_agent.decision.sizing import (
    CASH_BUFFER_FLOOR,
    CASH_FIT_ORDER,
    FORCED_SWAP_CONFIRMATION,
    HIGH_CONVICTION_CASH_FLOOR,
    ROTATION_VERSION,
    high_conviction_floor_ratio,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_cash_fit_and_limits import ALL_GATES, LEGACY_EFFECTIVE_KEYS, gated, sizing_errors, with_rules
from test_portfolio_risk_decision import chain_inputs, risk_review, valid_inputs, policy as base_policy
from test_position_sizing import sized_inputs, sized_policy
from test_risk_limit_gates import CONTROL, account, failed_rules, flags, guard, orders

ROOT = Path(__file__).resolve().parents[2]
GATE = {"version": HIGH_CONVICTION_CASH_FLOOR, "floor_ratio": "0.75"}
# 改動前（2026-11-05）以同一 fixture 重算的 proposal content_sha256：省略新閘門時必須逐位不變。
LEGACY_FIXTURE_SHA256 = "ca0599aa8dec2e342b4e53146e36ad6e9806c324dfc422fec8fcca243c278915"
ALL_GATES_FIXTURE_SHA256 = "aad7133b831d88b365e7b1ceb536afe1e4c1fa4cd1653aff81083187876cfc7a"
NAV = Decimal("10000000")


class LegacyBitwiseTests(unittest.TestCase):
    def test_policy_without_new_gates_rebuilds_the_same_proposal_hash(self):
        bundle, _, _, intent, policy, _, _, _ = chain_inputs()
        proposal = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(proposal["content_sha256"], LEGACY_FIXTURE_SHA256)
        self.assertEqual(set(proposal["effective_policy"]), LEGACY_EFFECTIVE_KEYS)
        policy = gated(policy, **ALL_GATES, cash_fit_min_fill_ratio="0.5", buy_gap_reserve_rate="0.03")
        proposal = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(proposal["content_sha256"], ALL_GATES_FIXTURE_SHA256)
        self.assertNotIn("high_conviction_cash_floor", proposal["effective_policy"])
        self.assertNotIn("high_conviction_floor_ratio", proposal["effective_policy"])

    def test_derisk_and_forced_swap_paths_rebuild_the_same_hashes(self):
        """本次改動了 _drawdown_derisk（目標與說明文字）與 _forced_swap_victims（共用下限）；以改動前程式碼算出的雜湊
        鎖定沒有新閘門時這兩條路徑逐位不變（不把 TestCase 類別匯入本模組，避免重複收集）。"""
        import test_risk_limit_gates as gates

        derisk = gates.DrawdownDeriskTests("test_defensive_stance_trims_to_the_cash_buffer")
        forced = gates.ForcedSwapConfirmationTests("test_adopted_bear_claim_confirms_the_swap")
        cases = {
            "96051f3eaa3dbdc2b67d76b9a760f87348b016083ade5e4e24b1906263530739": lambda: derisk.inputs("100000", 5000, version="v1")[2],
            "746c0ee8fef1964b0bbe42f1aa73fdf8e01cf541a8d222f47bb97d5d60f310ca": lambda: derisk.inputs("100000", 5000, version="v3")[2],
            "6af96f6f4f6a19b5aaa4f92368a8a203b6d1911387c4fa0fa143e2feec097c1c":
                lambda: derisk.inputs("100000", 5000, held_intent="add", version="v1")[2],
            "b47ba7545d6ce126b87218de6a54c2bb6d90020322e4697f98dd999483780668": lambda: forced.proposal(adopt_bear=True)[2],
            "f53333c639c232205e10af2a36c3a1261f3807e6e6257a21c4eb4f2d75a5019f":
                lambda: forced.proposal(adopt_bear=False, confirmation=False)[2],
            "810ae524b832f68fdf320066d9b1bf973be97df2556113beb76374acd16128ac": forced.ranked_three_symbol_proposal,
        }
        for expected, build in cases.items():
            self.assertEqual(build()["content_sha256"], expected)


class CashFloorUnitTests(unittest.TestCase):
    """直接測 _fit_cash：NAV 1,000 萬、防守緩衝 20%、floor_ratio 0.75（軟下限 150 萬）。"""

    def engine(self, tiers, gate=True, rank=None):
        bundle, _, _, _ = valid_inputs()
        engine = AllocationOrderEngine(bundle, base_policy())
        sizing = {"conviction_by_symbol": tiers, "cash_fit_order": CASH_FIT_ORDER, "cash_buffer_floor": CASH_BUFFER_FLOOR}
        if rank is not None:
            sizing["rank_by_symbol"] = rank
        if gate:
            sizing["high_conviction_cash_floor"] = dict(GATE)
        engine.policy = {**engine.policy, "position_sizing": sizing}
        effective = engine._effective_policy({})
        effective["cash_buffer_rate"] = Decimal("0.20")
        effective["max_turnover_rate"] = Decimal("1")
        return engine, effective

    def fit(self, engine, effective, orders_, starting_cash, current=None, floor_disabled=False):
        result, cash, flags_ = engine._fit_cash(
            orders_, starting_cash, starting_cash, NAV, effective, current or {}, floor_disabled=floor_disabled,
        )
        return {o["symbol"]: int(o["shares"]) for o in result if o["side"] == "buy"}, cash, flags_

    def test_high_buy_may_use_cash_down_to_soft_floor(self):
        engine, effective = self.engine({"2317.TW": "high"})
        buy = engine._order("2317.TW", "buy", 3000, "buy", effective)
        shares, cash, flags_ = self.fit(engine, effective, [buy], Decimal("1890000"))
        self.assertEqual(shares, {"2317.TW": 3000})
        self.assertGreaterEqual(cash, Decimal("1500000"))
        soft = [f for f in flags_ if f["code"] == "RISK_CASH_SOFT_FLOOR"]
        self.assertEqual(len(soft), 1)
        self.assertIn("交易 Agent 的意圖未被改寫", soft[0]["detail"])
        # 原有現金全部低於一般下限（起始現金 189 萬）：動用量＝買單成本。
        self.assertIn(str(-Decimal(buy["net_cash_impact"])), soft[0]["detail"])

    def test_medium_and_low_still_limited_to_starting_cash(self):
        for tier in ("medium", "low"):
            engine, effective = self.engine({"2317.TW": tier})
            buy = engine._order("2317.TW", "buy", 3000, "buy", effective)
            shares, cash, flags_ = self.fit(engine, effective, [buy], Decimal("1890000"))
            self.assertEqual(shares, {}, tier)
            self.assertEqual(cash, Decimal("1890000"))
            self.assertNotIn("RISK_CASH_SOFT_FLOOR", {f["code"] for f in flags_})

    def test_soft_floor_is_a_level_not_a_daily_allowance(self):
        # 起始現金已低於軟下限（14%）：high 也只能用賣款，不會逐日往下。
        engine, effective = self.engine({"2317.TW": "high"})
        buy = engine._order("2317.TW", "buy", 1000, "buy", effective)
        shares, cash, flags_ = self.fit(engine, effective, [buy], Decimal("1400000"))
        self.assertEqual(shares, {})
        self.assertEqual(cash, Decimal("1400000"))
        self.assertEqual(engine._cash_floor(NAV, Decimal("1400000"), effective, "high"), Decimal("1400000"))

    def test_soft_floor_never_above_full_buffer(self):
        engine, effective = self.engine({"2317.TW": "high"})
        rich = Decimal("2500000")
        self.assertEqual(engine._cash_floor(NAV, rich, effective, "high"), Decimal("1500000"))
        self.assertEqual(engine._cash_floor(NAV, rich, effective, "medium"), Decimal("2000000"))
        self.assertEqual(engine._cash_floor(NAV, rich, effective), Decimal("2000000"))
        # 現金高於完整緩衝時，high 可用到 150 萬；只動用到 200 萬以下的部分才記旗標。
        # 九張約 101 萬會使現金低於 150 萬，所以只到八張（約 89.8 萬，現金約 160 萬）。
        buy = engine._order("2317.TW", "buy", 9000, "buy", effective)
        shares, cash, flags_ = self.fit(engine, effective, [buy], rich)
        self.assertEqual(shares, {"2317.TW": 8000})
        self.assertGreaterEqual(cash, Decimal("1500000"))
        self.assertLess(cash + Decimal(engine._order("2317.TW", "buy", 1000, "buy", effective)["net_cash_impact"]),
                        Decimal("1500000"))
        soft = [f for f in flags_ if f["code"] == "RISK_CASH_SOFT_FLOOR"]
        self.assertEqual(len(soft), 1)
        # 未啟用 buy_gap_reserve：動用量＝一般下限 200 萬 − 買後現金。
        self.assertIn("原有現金 %s 元" % (Decimal("2000000") - cash), soft[0]["detail"])

    def test_no_flag_when_high_buy_stays_above_normal_floor(self):
        engine, effective = self.engine({"2317.TW": "high"})
        buy = engine._order("2317.TW", "buy", 1000, "buy", effective)
        _, _, flags_ = self.fit(engine, effective, [buy], Decimal("2500000"))
        self.assertEqual(flags_, [])

    def test_high_buys_take_cash_before_medium(self):
        # 11-05 縮小版：high 新股 2317 先用到軟下限，medium 已持有的 2330 只剩不到一張（只減量、不取消）。
        engine, effective = self.engine({"2317.TW": "high", "2330.TW": "medium"})
        high = engine._order("2317.TW", "buy", 3000, "buy", effective)
        medium = engine._order("2330.TW", "buy", 2000, "add", effective)
        shares, cash, flags_ = self.fit(engine, effective, [medium, high], Decimal("1890000"), {"2330.TW": 1000})
        self.assertEqual(shares, {"2317.TW": 3000})
        self.assertIn(("2330.TW", "CASH_BUFFER"), {(f["symbol"], f["code"]) for f in flags_})
        self.assertIn(("2317.TW", "RISK_CASH_SOFT_FLOOR"), {(f["symbol"], f["code"]) for f in flags_})

    def test_rank_first_high_add_takes_the_soft_floor_before_new_high(self):
        """與 limit_scaled_conviction 同時啟用時的硬相依（cash plan 第 5.4 節）：份額放大後，名次第一、已持有的 high
        （11-05 的 2330）先用掉軟下限的額度，名次第二的 high 新股（6446）只剩零頭而被 RISK_CASH_FIT 整筆取消。
        依人的決定維持「依名次」，不讓未持有新股在 high 內優先；名次反過來時新股先拿。"""
        engine, effective = self.engine({"2330.TW": "high", "2317.TW": "high"}, rank={"2330.TW": 1, "2317.TW": 2})
        add = engine._order("2330.TW", "buy", 1000, "add", effective)
        new = engine._order("2317.TW", "buy", 4000, "buy", effective)
        shares, _, flags_ = self.fit(engine, effective, [new, add], Decimal("1850000"), {"2330.TW": 1000})
        self.assertEqual(shares, {"2330.TW": 1000})
        self.assertIn(("2317.TW", "RISK_CASH_FIT"), {(f["symbol"], f["code"]) for f in flags_})
        engine, effective = self.engine({"2330.TW": "high", "2317.TW": "high"}, rank={"2330.TW": 2, "2317.TW": 1})
        shares, _, flags_ = self.fit(engine, effective, [new, add], Decimal("1850000"), {"2330.TW": 1000})
        self.assertEqual(shares, {"2317.TW": 3000})
        self.assertIn(("2330.TW", "CASH_BUFFER"), {(f["symbol"], f["code"]) for f in flags_})

    def test_soft_floor_not_applied_on_derisk_day(self):
        engine, effective = self.engine({"2317.TW": "high"})
        buy = engine._order("2317.TW", "buy", 1000, "buy", effective)
        # 減碼日緩衝回到完整的 NAV × 20%（200 萬）：189 萬的現金連一張都不能買。
        shares, cash, flags_ = self.fit(engine, effective, [buy], Decimal("1890000"), floor_disabled=True)
        self.assertEqual(shares, {})
        self.assertNotIn("RISK_CASH_SOFT_FLOOR", {f["code"] for f in flags_})
        self.assertEqual(engine._cash_floor(NAV, Decimal("1890000"), effective, "high", floor_disabled=True), Decimal("2000000"))

    def test_without_gate_high_is_treated_like_every_other_tier(self):
        engine, effective = self.engine({"2317.TW": "high"}, gate=False)
        self.assertNotIn("high_conviction_floor_ratio", effective)
        buy = engine._order("2317.TW", "buy", 3000, "buy", effective)
        shares, _, flags_ = self.fit(engine, effective, [buy], Decimal("1890000"))
        self.assertEqual(shares, {})
        self.assertEqual(engine._cash_floor(NAV, Decimal("2500000"), effective, "high"), Decimal("2000000"))

    def test_increase_cash_buffer_override_moves_soft_floor(self):
        engine, _ = self.engine({"2317.TW": "high"})
        effective = engine._effective_policy({"cash_buffer_rate": "0.22"})
        self.assertEqual(engine._cash_floor(NAV, Decimal("3000000"), effective, "high"), Decimal("1650000"))
        self.assertEqual(effective["high_conviction_floor_ratio"], Decimal("0.75"))
        # 風險 Agent 的修正不得覆寫比例。
        with self.assertRaises(Exception):
            engine._effective_policy({"high_conviction_floor_ratio": "0.5"})


def forced_swap_victims(gate, derisk_day=False):
    """21 檔滿檔、2 檔 high 新股（9000 每張 10 萬、1000 每張 100 萬），broken 的 3000 賣款約 5 萬。

    起始現金 10 萬、NAV 100 萬、緩衝 10%：一般下限 10 萬（沒有原有現金可用），floor_ratio 0.4 的軟下限 4 萬，
    高信心換入者可多用 6 萬：3000 的賣款加 6 萬買得起 9000 一張。"""
    bundle, intent, stance, policy = sized_inputs("high", "high")
    policy = sized_policy(policy, intent, stance, bundle)
    engine = AllocationOrderEngine(bundle, policy)
    candidates = ["1000.TW", "9000.TW"]
    held = ["%04d.TW" % number for number in range(3000, 3021)]
    current = {symbol: 1000 for symbol in held}
    engine.prices.update({symbol: Decimal("10") for symbol in held})
    engine.prices.update({"1000.TW": Decimal("1000"), "9000.TW": Decimal("100"), "3000.TW": Decimal("50"),
                          "3001.TW": Decimal("90")})
    sizing = engine.policy["position_sizing"]
    sizing["conviction_by_symbol"].update({symbol: "high" for symbol in candidates})
    sizing["rank_by_symbol"] = {"9000.TW": 1, "1000.TW": 2}
    sizing["rotation"] = {
        "version": ROTATION_VERSION, "max_swaps_per_run": 2, "min_new_conviction": "high",
        "forced_swap": {"version": "v1", "min_new_conviction": "high", "max_swaps_per_run": 2,
                        "broken_symbols": ["3000.TW", "3001.TW"], "confirmation": FORCED_SWAP_CONFIRMATION},
    }
    sizing["cash_buffer_floor"] = CASH_BUFFER_FLOOR
    if gate:
        sizing["high_conviction_cash_floor"] = {"version": "v1", "floor_ratio": "0.4"}
    engine.policy["min_positions"] = 0
    effective = engine._effective_policy({})
    effective["cash_buffer_rate"] = Decimal("0.10")
    return engine._forced_swap_victims(
        candidates, current, set(current), current, {symbol: "hold" for symbol in current}, {}, set(), 21,
        {"3000.TW", "3001.TW"}, Decimal("100000"), Decimal("1000000"), effective, derisk_day=derisk_day,
    )


class ForcedSwapFeasibilityTests(unittest.TestCase):
    def test_forced_swap_feasibility_uses_same_floor(self):
        self.assertEqual(forced_swap_victims(False), ([], {}))
        victims, unconfirmed = forced_swap_victims(True)
        self.assertEqual(victims, ["3000.TW"])
        self.assertEqual(unconfirmed, {})

    def test_forced_swap_uses_full_buffer_on_derisk_day_only_under_the_gate(self):
        self.assertEqual(forced_swap_victims(True, derisk_day=True), ([], {}))
        # 舊路徑不看減碼日（維持原重建）：沒有閘門時 derisk_day 不影響結果。
        self.assertEqual(forced_swap_victims(False, derisk_day=True), forced_swap_victims(False))


class EndToEndTests(unittest.TestCase):
    """2317 high 新股、2330 low 加碼；2330 兩萬股（328 萬）＋現金 77 萬：現金 19.0%，介於 15% 與 20% 之間。

    沒有閘門：防守減碼把 2330 賣一張補到 20%；有閘門：不減碼，2317 用原有現金買一張，現金落在 15%～19%。"""

    def world(self, gate=True, stance="defensive", cash="770000", shares=20000, derisk=True, ceiling=None):
        bundle, intent, stance_obj, policy = sized_inputs("high", "low", stance)
        bundle = account(bundle, cash, shares)
        # ceiling：改用比賽的現金上限（rules 與 policy 同時改，兩者必須一致）；None 維持 fixture 的 0.90。
        limits = {"cash_weight_must_be_below": ceiling} if ceiling else {}
        bundle = with_rules(bundle, max_stock_weight="1", special_weight_limits={"2330.TW": "1"}, max_sector_weight="1",
                            **limits)
        policy.update(max_stock_weight="1", special_weight_limits={"2330.TW": "1"}, max_sector_weight="1",
                      liquidity_fill_rate="1", max_turnover_rate="1")
        if ceiling:
            policy["cash_weight_ceiling"] = ceiling
        # 2330 標 add（low）：仍在入選內，所以配置預算不被 2330 的既有部位吃光（目標低於現有股數，不會真的加碼）。
        items = list(intent["items"])
        _, _, debate, _, _, _, _, _ = chain_inputs(copy.deepcopy(bundle))
        intent = seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), items)
        policy["position_sizing"].update(
            cash_buffer_floor=CASH_BUFFER_FLOOR, cash_fit_order=CASH_FIT_ORDER, cash_fit_min_fill_ratio="0.01",
            buy_gap_reserve="v1", buy_gap_reserve_rate="0.03",
        )
        if derisk:
            policy["position_sizing"]["drawdown_derisk"] = {"version": "v3", "max_derisk_fraction": "0.25"}
            policy["drawdown_control"] = dict(CONTROL)
            policy = bind_drawdown(policy, bundle, [])
        if gate:
            policy["position_sizing"]["high_conviction_cash_floor"] = dict(GATE)
        policy["content_sha256"] = decision_policy_sha256(policy)
        policy = sized_policy(policy, intent, stance_obj, bundle)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        return bundle, policy, intent, body

    def test_without_gate_derisk_sells_back_to_the_full_buffer(self):
        _, _, _, body = self.world(gate=False)
        self.assertIn(("2330.TW", "RISK_DRAWDOWN"), flags(body))
        self.assertEqual(orders(body)[("2330.TW", "sell")]["shares"], 1000)
        self.assertGreaterEqual(Decimal(body["allocation_proposal"]["cash_weight"]), Decimal("0.20"))
        self.assertNotIn("RISK_CASH_SOFT_FLOOR", {f["code"] for f in body["constraint_flags"]})
        self.assertNotIn("high_conviction_cash_floor", body["effective_policy"])

    def test_gate_buys_the_high_name_without_derisk_and_passes_guard(self):
        bundle, policy, intent, body = self.world()
        self.assertNotIn(("2330.TW", "sell"), orders(body))
        self.assertNotIn(("2330.TW", "RISK_DRAWDOWN"), flags(body))
        self.assertEqual(orders(body)[("2317.TW", "buy")]["shares"], 1000)
        self.assertIn(("2317.TW", "RISK_CASH_SOFT_FLOOR"), flags(body))
        self.assertGreaterEqual(Decimal(body["allocation_proposal"]["cash_weight"]), Decimal("0.15"))
        self.assertLess(Decimal(body["allocation_proposal"]["cash_weight"]), Decimal("0.19"))
        self.assertEqual(body["effective_policy"]["high_conviction_cash_floor"], HIGH_CONVICTION_CASH_FLOOR)
        self.assertEqual(body["effective_policy"]["high_conviction_floor_ratio"], "0.75000000")
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_derisk_targets_soft_floor(self):
        # 現金 12%：有閘門時只補到 15%，沒有閘門補到 20%；說明文字寫軟下限。
        _, _, _, gated_body = self.world(cash="0", shares=20000)
        _, _, _, legacy_body = self.world(gate=False, cash="0", shares=20000)
        self.assertEqual(orders(gated_body)[("2330.TW", "sell")]["shares"], 3000)
        self.assertEqual(orders(legacy_body)[("2330.TW", "sell")]["shares"], 4000)
        detail = next(f["detail"] for f in gated_body["constraint_flags"] if f["code"] == "RISK_DRAWDOWN")
        self.assertIn("15.00% 的高信心現金下限", detail)
        legacy = next(f["detail"] for f in legacy_body["constraint_flags"] if f["code"] == "RISK_DRAWDOWN")
        self.assertIn("20% 的姿態緩衝", legacy)
        # 減碼日：減碼收入只補緩衝，不拿去買 high 新股。
        self.assertNotIn(("2317.TW", "buy"), orders(gated_body))
        self.assertNotIn(("2317.TW", "RISK_CASH_SOFT_FLOOR"), flags(gated_body))

    def test_budget_unchanged_by_gate(self):
        with_gate = self.world(stance="neutral")[3]
        without = self.world(gate=False, stance="neutral")[3]
        self.assertEqual(with_gate["position_sizing"]["target_weights"], without["position_sizing"]["target_weights"])

    def test_proposal_validator_rejects_tampered_orders_and_effective_policy(self):
        bundle, policy, intent, body = self.world()
        forged = copy.deepcopy(body)
        for order in forged["order_proposal"]["orders"]:
            if order["symbol"] == "2317.TW":
                order["shares"] = 2000
                order["lots"] = 2
        self.assertTrue(ProposalValidator(bundle, policy, intent).validate(forged))
        forged = copy.deepcopy(body)
        forged["effective_policy"].pop("high_conviction_floor_ratio")
        self.assertTrue(ProposalValidator(bundle, policy, intent).validate(forged))
        forged = copy.deepcopy(body)
        forged["constraint_flags"] = [f for f in forged["constraint_flags"] if f["code"] != "RISK_CASH_SOFT_FLOOR"]
        self.assertTrue(ProposalValidator(bundle, policy, intent).validate(forged))


class IncreaseCashBufferCeilingTests(unittest.TestCase):
    """修正循環的 increase_cash_buffer 必須在競賽現金上限（比賽為 25%）之下留 3 個百分點邊際：值 ≤ 上限 − 0.03。

    重現（2026-11-05）：防守姿態＋drawdown_derisk、現金約 22%。0.25／0.30 時修正後現金 ≥ 25%，Guard 的 CASH_WEIGHT 失敗，
    而之後的修正不得再降低緩衝，等於把當日推向 rejected。即使低於上限（例如起始現金約 21%、緩衝 0.24），整張取整與現金
    分配仍可能讓現金到 25.12% 而被拒絕，所以改為在審查驗證就要求留邊際。"""

    def setUp(self):
        self.bundle, self.policy, self.intent, self.body = EndToEndTests().world(
            gate=False, cash="1500000", shares=20000, ceiling="0.25")
        self.scenario = ScenarioEngine(self.bundle, self.policy).run(self.body)
        self.guard = CompetitionGuardV2(self.bundle, self.policy).run(self.body, self.scenario)
        self.assertTrue(self.guard["passed"])

    def review(self, value):
        review = risk_review(self.bundle, self.body, self.scenario, self.guard, "revise", 1,
                             [{"type": "increase_cash_buffer", "value": value, "reason": "提高緩衝。"}])
        return review, RiskReviewValidator(self.bundle, self.policy).validate(self.body, self.scenario, self.guard, review)

    def revised_guard(self, review):
        effects = revision_effects(review, self.body)
        revised = AllocationOrderEngine(self.bundle, self.policy).run(
            self.intent, excluded_symbols=effects["excluded_symbols"], overrides=effects["overrides"],
            revision_count=1, parent_proposal_id=self.body["proposal_id"])
        return guard(self.bundle, self.policy, revised)

    def margin_error(self, errors):
        return any("競賽現金上限" in error and "0.25" in error and "0.03" in error and "0.22" in error for error in errors)

    def test_margin_is_single_constant(self):
        from etf_agent.decision.risk import CASH_BUFFER_CEILING_MARGIN

        self.assertEqual(CASH_BUFFER_CEILING_MARGIN, Decimal("0.03"))

    def test_value_at_ceiling_minus_margin_is_accepted_and_passes_guard(self):
        review, errors = self.review("0.22")
        self.assertEqual(errors, [])
        self.assertTrue(self.revised_guard(review)["passed"])

    def test_value_inside_margin_is_rejected_before_revision(self):
        for value in ("0.2201", "0.23", "0.24"):
            review, errors = self.review(value)
            self.assertTrue(self.margin_error(errors), (value, errors))

    def test_value_at_or_above_ceiling_is_rejected_before_revision(self):
        for value in ("0.25", "0.30"):
            review, errors = self.review(value)
            self.assertTrue(self.margin_error(errors), (value, errors))
            # 若未在審查擋下，修正後提案會被 Guard 的現金上限拒絕（缺口的實際後果）。
            self.assertIn("CASH_WEIGHT", failed_rules(self.revised_guard(review)))


class PolicyValidationTests(unittest.TestCase):
    BASE = {"cash_buffer_floor": "v1", "cash_fit_order": "v1"}

    def errors(self, gate, **extra):
        return sizing_errors(**{**self.BASE, **extra, "high_conviction_cash_floor": gate})

    def test_valid_gate_passes(self):
        self.assertEqual(self.errors(dict(GATE)), [])
        self.assertEqual(self.errors(dict(GATE), drawdown_derisk={"version": "v3"}), [])
        self.assertEqual(high_conviction_floor_ratio({"high_conviction_cash_floor": dict(GATE)}), Decimal("0.75"))
        self.assertIsNone(high_conviction_floor_ratio({}))

    def test_policy_rejects_unknown_version(self):
        for version in ("v2", True, 1, None):
            self.assertTrue(any("high_conviction_cash_floor.version" in e
                                for e in self.errors({"version": version, "floor_ratio": "0.75"})), version)

    def test_policy_rejects_floor_ratio_out_of_range(self):
        for ratio in ("0", "1", "1.2", "-0.1", "abc", 0.75, True, None):
            self.assertTrue(any("floor_ratio" in e for e in self.errors({"version": "v1", "floor_ratio": ratio})), ratio)
        self.assertTrue(any("floor_ratio" in e for e in self.errors({"version": "v1"})))

    def test_policy_rejects_unknown_subfield_and_non_object(self):
        self.assertTrue(self.errors(dict(GATE, extra="1")))
        self.assertTrue(any("必須是物件" in e for e in self.errors("v1")))

    def test_policy_rejects_gate_without_prerequisites(self):
        errors = sizing_errors(high_conviction_cash_floor=dict(GATE), cash_fit_order="v1")
        self.assertTrue(any("cash_buffer_floor" in e for e in errors))
        errors = sizing_errors(high_conviction_cash_floor=dict(GATE), cash_buffer_floor="v1")
        self.assertTrue(any("cash_fit_order" in e for e in errors))
        for version in ("v1", "v2"):
            self.assertTrue(any("drawdown_derisk.version" in e
                                for e in self.errors(dict(GATE), drawdown_derisk={"version": version})), version)

    def test_gate_is_forbidden_for_strategy_reflection(self):
        for field in ("high_conviction_cash_floor", "high_conviction_cash_floor.floor_ratio"):
            self.assertTrue(any(field == p or field.startswith(p) for p in FORBIDDEN_PREFIXES), field)


class CashStanceBriefTests(unittest.TestCase):
    BUFFERS = {"aggressive": "0.03", "neutral": "0.10", "defensive": "0.20"}

    def brief(self, gate):
        from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
        from test_cash_stance_guard import STANCE
        from test_daily_pipeline import FakeRunner
        from test_typed_evidence import typed_bundle, typed_reports

        bundle = typed_bundle()
        momentum = MomentumEngine(bundle).run()
        runner = FakeRunner({"cash_stance": [dict(STANCE)]})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            pipeline.run_cash_stance(bundle, momentum, typed_reports(bundle), None, None, self.BUFFERS, gate)
            brief = json.loads((Path(directory) / "brief_cash_stance.json").read_text(encoding="utf-8"))
        return brief, runner.prompts[0][1]

    def test_brief_lists_soft_floors_only_with_the_gate(self):
        brief, prompt = self.brief(dict(GATE))
        self.assertEqual(brief["high_conviction_cash_floor_by_stance"],
                         {"aggressive": "0.0225", "neutral": "0.0750", "defensive": "0.1500"})
        self.assertIn("high_conviction_cash_floor_by_stance", prompt)
        brief, prompt = self.brief(None)
        self.assertNotIn("high_conviction_cash_floor_by_stance", brief)
        self.assertNotIn("high_conviction_cash_floor_by_stance", prompt)
        self.assertEqual(brief["cash_buffer_by_stance"], self.BUFFERS)


if __name__ == "__main__":
    unittest.main()
