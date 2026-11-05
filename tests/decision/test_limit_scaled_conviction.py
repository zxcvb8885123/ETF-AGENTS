"""依單股上限填充高信心份額（position_sizing.limit_scaled_conviction，α 方案，版本閘門）的測試。

high 的份額至少為 max(原式, min(α × 單股上限, 封頂))；必要份額總和超過預算時依入選名次填到用完；其餘照原式，
volatility_exponent = 0 時受跨等級天花板限制（分不完留作現金）。省略閘門逐位走原式。
"""

import copy
import unittest
from decimal import Decimal

from etf_agent.decision import (
    AllocationOrderEngine,
    DecisionPolicyValidator,
    ProposalValidator,
    decision_policy_sha256,
)
from etf_agent.decision.sizing import (
    LIMIT_HEADROOM,
    LIMIT_SCALED_CONVICTION,
    SELECTION_RANKING,
    SIZING_METHOD_V2,
    alpha_floor_weights,
    conviction_weights,
    limit_scaled_alpha,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_cash_fit_and_limits import sizing_errors, with_rules
from test_high_conviction_cash_floor import GATE as CASH_GATE
from test_position_sizing import sized_inputs, sized_policy
from test_risk_limit_gates import guard

GATE = {"version": LIMIT_SCALED_CONVICTION, "alpha": "0.6"}
MULTIPLIERS = {"high": Decimal("1.5"), "medium": Decimal("0.6"), "low": Decimal("0.3")}
HEADROOM = Decimal("0.97")
FLOOR = Decimal("0.01")


def universe(highs, mediums=0, lows=0, special=("BIG",)):
    """high 依傳入順序（即入選名次）；BIG 的上限 25%，其餘 10%。回傳 (order, tiers, limits, floors(α=0.6))."""
    order = list(highs) + ["M%02d" % i for i in range(mediums)] + ["L%02d" % i for i in range(lows)]
    tiers = {s: "high" for s in highs}
    tiers.update({s: "medium" for s in order if s.startswith("M")})
    tiers.update({s: "low" for s in order if s.startswith("L")})
    rule = {s: Decimal("0.25") if s in special else Decimal("0.10") for s in order}
    limits = {s: rule[s] * HEADROOM for s in order}
    floors = {s: min(Decimal("0.6") * rule[s], limits[s]) for s in highs}
    return order, tiers, limits, floors


def weights(order, tiers, limits, floors, budget, multipliers=MULTIPLIERS, exponent=Decimal("0"), volatility=None):
    volatility = volatility or {s: Decimal("0.03") for s in order}
    return alpha_floor_weights(order, tiers, multipliers, volatility, FLOOR, limits, budget, exponent, floors)


class AlphaFloorWeightsTests(unittest.TestCase):
    def assertClose(self, left, right):
        self.assertLess(abs(left - right), Decimal("1e-25"), (left, right))

    def test_reproduces_the_2026_11_05_estimate(self):
        # 11-05：預算 0.358864、2330（25%）與 6446（10%）為 high、13 檔 medium（plan 第 4 節 D 類估算）。
        order, tiers, limits, floors = universe(["BIG", "H2"], mediums=13)
        result = weights(order, tiers, limits, floors, Decimal("0.358864"))
        self.assertEqual(result["BIG"], Decimal("0.150"))
        self.assertEqual(result["H2"], Decimal("0.060"))
        for symbol in order[2:]:
            self.assertClose(result[symbol], (Decimal("0.358864") - Decimal("0.21")) / 13)
        self.assertClose(sum(result.values()), Decimal("0.358864"))

    def test_budget_is_conserved(self):
        for budget in ("0.05", "0.2", "0.358864", "0.5", "0.7", "0.9"):
            order, tiers, limits, floors = universe(["BIG", "H2", "H3"], mediums=6, lows=4)
            result = weights(order, tiers, limits, floors, Decimal(budget))
            self.assertClose(sum(result.values()), min(Decimal(budget), sum(limits.values())))

    def test_scarce_budget_fills_highs_by_rank_until_exhausted(self):
        order, tiers, limits, floors = universe(["BIG", "H2"], mediums=3)
        result = weights(order, tiers, limits, floors, Decimal("0.12"))
        self.assertEqual((result["BIG"], result["H2"]), (Decimal("0.12"), Decimal("0")))
        self.assertTrue(all(result[s] == 0 for s in order[2:]))
        # 名次反過來：H2 先填到 6%，BIG 拿剩下的 6%。
        order, tiers, limits, floors = universe(["H2", "BIG"], mediums=3)
        result = weights(order, tiers, limits, floors, Decimal("0.12"))
        self.assertEqual((result["H2"], result["BIG"]), (Decimal("0.060"), Decimal("0.060")))

    def test_floor_is_a_minimum_not_a_target(self):
        # 預算大時原式份額已高於下限：high 照原式（不被壓回 α × 上限）。
        order, tiers, limits, floors = universe(["H1", "H2"], mediums=3)
        legacy = conviction_weights(order, tiers, MULTIPLIERS, {s: Decimal("0.03") for s in order}, FLOOR, limits,
                                    Decimal("0.6"), Decimal("0"))
        result = weights(order, tiers, limits, floors, Decimal("0.6"))
        self.assertEqual(result, legacy)
        self.assertGreater(result["H1"], floors["H1"])

    def test_cross_tier_order_is_preserved_under_any_multipliers(self):
        cases = [MULTIPLIERS, {"high": Decimal("1"), "medium": Decimal("1"), "low": Decimal("1")},
                 {"high": Decimal("3"), "medium": Decimal("1"), "low": Decimal("0.2")},
                 {"high": Decimal("1.2"), "medium": Decimal("1.1"), "low": Decimal("1")}]
        for multipliers in cases:
            for budget in ("0.1", "0.25", "0.4", "0.6", "0.8", "0.95"):
                for shape in ((["BIG", "H2"], 13, 2), (["H1"], 3, 3), (["BIG"], 1, 0), (["H1", "H2", "BIG"], 2, 5)):
                    order, tiers, limits, floors = universe(shape[0], shape[1], shape[2])
                    result = weights(order, tiers, limits, floors, Decimal(budget), multipliers)
                    by_tier = {t: [result[s] for s in order if tiers[s] == t] for t in ("high", "medium", "low")}
                    if by_tier["medium"]:
                        self.assertGreaterEqual(min(by_tier["high"]), max(by_tier["medium"]), (multipliers, budget, shape))
                    if by_tier["medium"] and by_tier["low"]:
                        self.assertGreaterEqual(min(by_tier["medium"]), max(by_tier["low"]), (multipliers, budget, shape))

    def test_scaled_weight_is_capped_at_limit_with_headroom(self):
        order, tiers, limits, floors = universe(["BIG"], mediums=1)
        result = weights(order, tiers, limits, floors, Decimal("0.9"))
        self.assertEqual(result["BIG"], Decimal("0.25") * HEADROOM)
        self.assertEqual(result["M00"], Decimal("0.10") * HEADROOM)

    def test_leftover_returns_to_pinned_highs_when_others_saturate(self):
        # 同乘數：原式各 0.14，BIG 低於下限 0.15 被固定；M00 封頂 0.097 後剩 0.033 回給 BIG，預算守恆。
        equal = {"high": Decimal("1"), "medium": Decimal("1"), "low": Decimal("1")}
        order, tiers, limits, floors = universe(["BIG"], mediums=1)
        result = weights(order, tiers, limits, floors, Decimal("0.28"), equal)
        self.assertEqual(result["M00"], Decimal("0.097"))
        self.assertClose(result["BIG"], Decimal("0.183"))

    def test_without_highs_matches_the_original_formula(self):
        order, tiers, limits, _ = universe([], mediums=4, lows=2)
        volatility = {s: Decimal("0.01") * (i + 1) for i, s in enumerate(order)}
        for exponent in (None, Decimal("0"), Decimal("0.5")):
            self.assertEqual(
                weights(order, tiers, limits, {}, Decimal("0.5"), exponent=exponent, volatility=volatility),
                conviction_weights(order, tiers, MULTIPLIERS, volatility, FLOOR, limits, Decimal("0.5"), exponent),
            )

    def test_single_candidate(self):
        for symbol, cap in (("BIG", Decimal("0.25") * HEADROOM), ("H1", Decimal("0.10") * HEADROOM)):
            order, tiers, limits, floors = universe([symbol])
            self.assertEqual(weights(order, tiers, limits, floors, Decimal("0.5"))[symbol], cap)
            self.assertEqual(weights(order, tiers, limits, floors, Decimal("0.05"))[symbol], Decimal("0.05"))

    def test_non_positive_budget_gives_zero(self):
        order, tiers, limits, floors = universe(["BIG", "H2"], mediums=2)
        self.assertTrue(all(value == 0 for value in weights(order, tiers, limits, floors, Decimal("-0.1")).values()))


def legacy(order, tiers, limits, budget, multipliers=MULTIPLIERS, exponent=Decimal("0"), volatility=None):
    volatility = volatility or {s: Decimal("0.03") for s in order}
    return conviction_weights(order, tiers, multipliers, volatility, FLOOR, limits, budget, exponent)


def tier_order_ok(result, order, tiers):
    by_tier = {t: [result[s] for s in order if tiers[s] == t] for t in ("high", "medium", "low")}
    upper = None
    for tier in ("high", "medium", "low"):
        if not by_tier[tier]:
            continue
        if upper is not None and max(by_tier[tier]) > upper:
            return False
        upper = min(by_tier[tier])
    return True


class AlphaFloorFixTests(unittest.TestCase):
    """Codex 審查的兩個缺陷：抬高一檔 high 不得縮小另一檔 high；特殊上限的 medium／low 不得高於 high。"""

    def assertClose(self, left, right):
        self.assertLess(abs(left - right), Decimal("1e-25"), (left, right))

    def test_lifting_one_high_does_not_shrink_another_high(self):
        # 原式 BIG、H2 各 0.09375；BIG 下限 0.15 生效，H2 維持原式 0.09375，medium 分剩下的 0.05625。
        order, tiers, limits, floors = universe(["BIG", "H2"], mediums=3)
        result = weights(order, tiers, limits, floors, Decimal("0.30"))
        base = legacy(order, tiers, limits, Decimal("0.30"))
        self.assertEqual(base["H2"], Decimal("0.09375"))
        self.assertEqual(result["BIG"], Decimal("0.15"))
        self.assertEqual(result["H2"], Decimal("0.09375"))
        for symbol in order[2:]:
            self.assertClose(result[symbol], Decimal("0.01875"))
        self.assertClose(sum(result.values()), Decimal("0.30"))

    def test_special_limit_medium_is_capped_at_the_high_and_rest_stays_cash(self):
        # 一般股 high 封頂 0.097，特殊上限股 medium 封頂 0.2425；原式給 medium 0.203（> high）。
        order, tiers, limits, floors = universe(["H1"], mediums=1, special=("M00",))
        base = legacy(order, tiers, limits, Decimal("0.30"))
        self.assertGreater(base["M00"], base["H1"])
        result = weights(order, tiers, limits, floors, Decimal("0.30"))
        self.assertEqual(result["H1"], Decimal("0.097"))
        self.assertEqual(result["M00"], Decimal("0.097"))
        self.assertEqual(sum(result.values()), Decimal("0.194"))  # 0.106 無法分配，留作現金

    def test_special_limit_low_is_capped_at_the_lowest_medium(self):
        order, tiers, limits, floors = universe(["H1"], mediums=1, lows=1, special=("L00",))
        equal = {"high": Decimal("1"), "medium": Decimal("1"), "low": Decimal("1")}
        result = weights(order, tiers, limits, floors, Decimal("0.5"), equal)
        self.assertEqual((result["H1"], result["M00"], result["L00"]), (Decimal("0.097"),) * 3)
        # 沒有 medium 時 low 的天花板是最小的 high。
        order, tiers, limits, floors = universe(["H1"], lows=1, special=("L00",))
        result = weights(order, tiers, limits, floors, Decimal("0.5"), equal)
        self.assertEqual((result["H1"], result["L00"]), (Decimal("0.097"), Decimal("0.097")))

    def test_2330_as_medium_or_low_never_exceeds_the_highs(self):
        for shape in ((["H1"], 1, 0, ("M00",)), (["H1", "H2"], 3, 2, ("M00",)), (["H1"], 2, 1, ("L00",)),
                      (["H1"], 0, 2, ("L00",)), (["H1", "BIG"], 2, 2, ("BIG", "M01"))):
            for budget in ("0.1", "0.2", "0.3", "0.45", "0.6", "0.9"):
                order, tiers, limits, floors = universe(shape[0], shape[1], shape[2], special=shape[3])
                result = weights(order, tiers, limits, floors, Decimal(budget))
                self.assertTrue(tier_order_ok(result, order, tiers), (shape, budget, result))
                # 1e-25：原式 Decimal 除法的尾差（原式本身也有）。
                self.assertLessEqual(sum(result.values()), Decimal(budget) + Decimal("1e-25"))
                self.assertTrue(all(result[s] <= limits[s] for s in order))

    def test_highs_never_fall_below_the_original_formula_when_budget_suffices(self):
        for shape in ((["BIG", "H2"], 3, 0), (["BIG", "H2", "H3"], 6, 4), (["H1", "BIG"], 13, 0), (["BIG"], 2, 2)):
            for budget in ("0.25", "0.3", "0.358864", "0.45", "0.6", "0.8"):
                order, tiers, limits, floors = universe(*shape)
                base = legacy(order, tiers, limits, Decimal(budget))
                required = sum(max(base[s], floors[s]) for s in floors)
                if required >= Decimal(budget):
                    continue
                result = weights(order, tiers, limits, floors, Decimal(budget))
                for symbol in floors:
                    self.assertGreaterEqual(result[symbol], max(base[symbol], floors[symbol]), (shape, budget, symbol))

    def test_all_high(self):
        order, tiers, limits, floors = universe(["BIG", "H2", "H3"])
        # 預算寬裕：原式已封頂，α 不起作用。
        self.assertEqual(weights(order, tiers, limits, floors, Decimal("0.9")), legacy(order, tiers, limits, Decimal("0.9")))
        # 原式 BIG 0.106 < 下限 0.15：必要份額 0.15 + 0.097 + 0.097 > 0.30，依名次填到用完。
        result = weights(order, tiers, limits, floors, Decimal("0.30"))
        self.assertEqual((result["BIG"], result["H2"]), (Decimal("0.15"), Decimal("0.097")))
        self.assertClose(result["H3"], Decimal("0.053"))
        # 全部 high 時預算 ≥ 0.344 原式 BIG 就已超過下限，α 不起作用。
        self.assertEqual(weights(order, tiers, limits, floors, Decimal("0.36")),
                         legacy(order, tiers, limits, Decimal("0.36")))

    def test_ample_budget_alpha_has_no_effect(self):
        for shape in ((["BIG", "H2"], 3, 0), (["H1"], 3, 3), (["BIG", "H2", "H3"], 0, 0)):
            order, tiers, limits, floors = universe(*shape)
            for budget in ("0.7", "0.9"):
                self.assertEqual(weights(order, tiers, limits, floors, Decimal(budget)),
                                 legacy(order, tiers, limits, Decimal(budget)), (shape, budget))

    def test_positive_exponent_keeps_the_documented_exception(self):
        # volatility_exponent > 0 不加跨等級天花板：非 high 的部分照原式分剩餘預算（與文件揭露一致）。
        order, tiers, limits, floors = universe(["H1"], mediums=1, special=("M00",))
        volatility = {"H1": Decimal("0.05"), "M00": Decimal("0.01")}
        result = weights(order, tiers, limits, floors, Decimal("0.30"), exponent=Decimal("0.5"), volatility=volatility)
        self.assertEqual(result, legacy(order, tiers, limits, Decimal("0.30"), exponent=Decimal("0.5"),
                                        volatility=volatility))


class EngineTests(unittest.TestCase):
    """2330 high（上限 0.9）、2317 medium（上限 0.5）；同乘數下原式各 0.4，α=0.6 的下限讓 2330 至少 0.54。"""

    def world(self, gate=True, tier_2317="medium", overrides=None, cash_gate=False, tier_2330="high"):
        bundle, intent, stance, policy = sized_inputs(tier_2317, tier_2330)
        bundle = with_rules(bundle, special_weight_limits={"2330.TW": "0.9"})
        policy["special_weight_limits"] = {"2330.TW": "0.9"}
        policy["liquidity_fill_rate"] = "1"
        policy["position_sizing"].update(
            method=SIZING_METHOD_V2, volatility_exponent="0", selection_ranking=SELECTION_RANKING,
            conviction_multipliers={"high": "1", "medium": "1", "low": "1"},
        )
        if gate:
            policy["position_sizing"]["limit_scaled_conviction"] = dict(GATE)
        if cash_gate:
            policy["position_sizing"].update(cash_buffer_floor="v1", cash_fit_order="v1",
                                             high_conviction_cash_floor=dict(CASH_GATE))
        policy["content_sha256"] = decision_policy_sha256(policy)
        policy = sized_policy(policy, intent, stance, bundle)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        body = AllocationOrderEngine(bundle, policy).run(intent, overrides=overrides)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        return bundle, policy, intent, body

    def test_gate_lifts_the_high_target_to_alpha_of_its_limit(self):
        bundle, policy, _, body = self.world()
        targets = {s: Decimal(w) for s, w in body["position_sizing"]["target_weights"].items()}
        self.assertEqual(targets["2330.TW"], Decimal("0.54"))
        self.assertEqual(targets["2317.TW"], Decimal("0.9") - Decimal("0.54"))
        self.assertEqual(body["position_sizing"]["limit_scaled_conviction"], {"version": "v1", "alpha": "0.60000000"})
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_without_gate_targets_follow_the_original_formula(self):
        _, _, _, body = self.world(gate=False)
        self.assertEqual(body["position_sizing"]["target_weights"], {"2317.TW": "0.45000000", "2330.TW": "0.45000000"})
        self.assertNotIn("limit_scaled_conviction", body["position_sizing"])

    def test_override_of_max_stock_weight_only_shrinks_floors(self):
        # 兩檔都 high：2317 下限 0.6 × 0.5 = 0.3；修正循環把 max_stock_weight 調到 0.3 後下限變 0.18、封頂 0.2985
        #（fixture 未啟用 stock_limit_headroom，封頂係數 0.995）。
        _, _, _, body = self.world(tier_2317="high")
        _, _, _, tightened = self.world(tier_2317="high", overrides={"max_stock_weight": "0.3"})
        before = Decimal(body["position_sizing"]["target_weights"]["2317.TW"])
        after = Decimal(tightened["position_sizing"]["target_weights"]["2317.TW"])
        self.assertLess(after, before)
        self.assertEqual(after, Decimal("0.3") * LIMIT_HEADROOM)

    def test_2330_as_medium_is_capped_at_the_high_target(self):
        # 2317 high（max_stock_weight 調到 0.3，封頂 0.2985）、2330 medium（特殊上限 0.9）：原式給 2330 其餘全部預算，
        # 閘門下 2330 不得高於 2317，分不完的預算留作現金；重建通過。
        _, _, _, legacy_body = self.world(gate=False, tier_2317="high", tier_2330="medium",
                                          overrides={"max_stock_weight": "0.3"})
        legacy_targets = {s: Decimal(w) for s, w in legacy_body["position_sizing"]["target_weights"].items()}
        self.assertGreater(legacy_targets["2330.TW"], legacy_targets["2317.TW"])
        bundle, policy, _, body = self.world(tier_2317="high", tier_2330="medium", overrides={"max_stock_weight": "0.3"})
        self.assertTrue(guard(bundle, policy, body)["passed"])
        targets = {s: Decimal(w) for s, w in body["position_sizing"]["target_weights"].items()}
        self.assertEqual(targets["2317.TW"], Decimal("0.3") * LIMIT_HEADROOM)
        self.assertEqual(targets["2330.TW"], targets["2317.TW"])

    def test_tampered_target_weights_are_rejected(self):
        bundle, policy, intent, body = self.world()
        forged = copy.deepcopy(body)
        forged["position_sizing"]["target_weights"]["2330.TW"] = "0.45000000"
        self.assertTrue(ProposalValidator(bundle, policy, intent).validate(forged))
        forged = copy.deepcopy(body)
        forged["position_sizing"].pop("limit_scaled_conviction")
        self.assertTrue(ProposalValidator(bundle, policy, intent).validate(forged))

    def test_both_gates_rebuild_and_pass_guard(self):
        """兩閘門同時啟用：份額先放大 high，現金下限只決定能否支應；重建、情境與 Guard 都通過。
        名次第一的 high 先用掉軟下限額度的相依，見 test_high_conviction_cash_floor 的
        test_rank_first_high_add_takes_the_soft_floor_before_new_high。"""
        bundle, policy, intent, body = self.world(cash_gate=True)
        self.assertIn("high_conviction_cash_floor", body["effective_policy"])
        self.assertEqual(body["position_sizing"]["limit_scaled_conviction"]["alpha"], "0.60000000")
        self.assertTrue(guard(bundle, policy, body)["passed"])


class PolicyValidationTests(unittest.TestCase):
    BASE = {"method": SIZING_METHOD_V2, "volatility_exponent": "0", "selection_ranking": SELECTION_RANKING}

    def errors(self, gate, **extra):
        return sizing_errors(**{**self.BASE, **extra, "limit_scaled_conviction": gate})

    def test_valid_gate_passes(self):
        self.assertEqual(self.errors(dict(GATE)), [])
        self.assertEqual(self.errors({"version": "v1", "alpha": "1"}), [])
        self.assertEqual(limit_scaled_alpha({"limit_scaled_conviction": dict(GATE)}), Decimal("0.6"))
        self.assertIsNone(limit_scaled_alpha({}))

    def test_alpha_is_required_and_bounded(self):
        self.assertTrue(any("alpha" in e for e in self.errors({"version": "v1"})))
        for alpha in ("0", "1.1", "-0.1", "abc", 0.6, True, None):
            self.assertTrue(any("alpha" in e for e in self.errors({"version": "v1", "alpha": alpha})), alpha)

    def test_unknown_version_subfield_and_type_fail_closed(self):
        for version in ("v2", True, 1, None):
            self.assertTrue(any("limit_scaled_conviction.version" in e
                                for e in self.errors({"version": version, "alpha": "0.6"})), version)
        self.assertTrue(self.errors(dict(GATE, k="1")))
        self.assertTrue(any("必須是物件" in e for e in self.errors("v1")))

    def test_prerequisites(self):
        errors = sizing_errors(limit_scaled_conviction=dict(GATE), selection_ranking=SELECTION_RANKING)
        self.assertTrue(any(SIZING_METHOD_V2 in e and "limit_scaled_conviction" in e for e in errors))
        errors = sizing_errors(limit_scaled_conviction=dict(GATE), method=SIZING_METHOD_V2, volatility_exponent="0")
        self.assertTrue(any("selection_ranking" in e for e in errors))

    def test_gate_is_forbidden_for_strategy_reflection(self):
        for field in ("limit_scaled_conviction", "limit_scaled_conviction.alpha"):
            self.assertTrue(any(field == p or field.startswith(p) for p in FORBIDDEN_PREFIXES), field)


if __name__ == "__main__":
    unittest.main()


class PipelineBothGatesTests(unittest.TestCase):
    """樣板（暫存檔，非 live config）同時加兩個閘門，跑完整每日決策鏈：Finalizer 核准、DecisionResultValidator 重建通過，
    風險 Agent 簡報附高信心現金下限；竄改 proposal 的委託會被拒絕。"""

    def test_daily_chain_with_both_gates_finalizes_and_rebuilds(self):
        import json
        import tempfile
        from pathlib import Path

        from etf_agent.decision import DecisionRepository, DecisionResultValidator
        from test_daily_pipeline import FakeRunner, PipelineWorld, team_outputs

        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            template = json.loads(world.paths["template"].read_text(encoding="utf-8"))
            template["position_sizing"]["high_conviction_cash_floor"] = dict(CASH_GATE)
            template["position_sizing"]["limit_scaled_conviction"] = dict(GATE)
            world.paths["template"].write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
            result = world.run(FakeRunner(team_outputs()))
            self.assertEqual(result.status, "completed", result.errors)
            run_dir = DecisionRepository(world.repository).verify("decision-fixture")
            load = lambda name: json.loads((run_dir / ("%s.json" % name)).read_text(encoding="utf-8"))  # noqa: E731
            names = ("decision_input", "policy", "momentum", "debate", "intent", "team_inputs", "proposal", "scenario",
                     "guard", "risk_review", "revision_history", "decision")
            art = {name: load(name) for name in names}
            self.assertEqual(art["decision"]["status"], "approved", art["decision"].get("errors"))
            sizing = art["policy"]["position_sizing"]
            self.assertEqual(sizing["high_conviction_cash_floor"], CASH_GATE)
            self.assertEqual(sizing["limit_scaled_conviction"], GATE)
            self.assertIn("high_conviction_floor_ratio", art["proposal"]["effective_policy"])
            self.assertIn("limit_scaled_conviction", art["proposal"]["position_sizing"])
            brief = json.loads((world.run_dir / "brief_cash_stance.json").read_text(encoding="utf-8"))
            self.assertIn("high_conviction_cash_floor_by_stance", brief)

            def validate(proposal):
                return DecisionResultValidator(
                    art["decision_input"], art["policy"], art["momentum"], art["debate"], art["intent"], art["team_inputs"],
                ).validate(proposal, art["scenario"], art["guard"], art["risk_review"], art["revision_history"], art["decision"])

            self.assertEqual(validate(art["proposal"]), [])
            forged = copy.deepcopy(art["proposal"])
            forged["position_sizing"]["limit_scaled_conviction"]["alpha"] = "0.90000000"
            self.assertTrue(validate(forged))
