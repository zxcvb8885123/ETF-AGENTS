"""position_sizing.exposure_rotation v1：持股 beta 事實、hold 信心、有上限有遲滯的曝險換股與信心差換股。

覆蓋：閘門形狀與前提、beta 計算（獨立重算）、曝險檢視（team_inputs 2.7）、換股配對純函式、持有天數（帳本）、
交易 Validator 的 hold 信心、配置引擎（觸發／遲滯／排除／上限／優先序／只賣不買復原）、整條鏈與 Finalizer fail-closed、回放略過。
沒有欄位的舊 Policy 逐位元不變由既有測試（含 6 份封存 run 重建）與本檔的對照組共同證明。
"""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from etf_agent.automation.agent_runner import DailyPipelineError
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.decision import (
    AllocationOrderEngine,
    DecisionFinalizer,
    DecisionPolicyValidator,
    DecisionRepository,
    DecisionResultValidator,
    DecisionToolError,
    ProposalValidator,
    TradeDecisionValidator,
    apply_trade_decision,
    bind_holding_days,
    build_exposure_review,
    build_team_inputs,
    canonical_sha256,
    decision_bundle_sha256,
    decision_policy_sha256,
    exposure_review_brief,
    seal_trade_decision,
    trade_decision_envelope,
    validate_exposure_review,
)
from etf_agent.decision.exposure import (
    EXPOSURE_REVIEW_VERSION, ITEM_FIELDS, PORTFOLIO_FIELDS, REVIEW_FIELDS, BetaModel, Fill, Victim,
    conviction_gap_swap_enabled, exposure_rotation_gate, exposure_rotation_plan, held_since_from_history,
    holding_days_by_symbol, holding_weights, hold_conviction_enabled, portfolio_beta, validate_exposure_rotation_gate,
    validate_held_since,
)
from etf_agent.decision.finalization import EXPOSURE_TEAM_FIELDS, EXPOSURE_TEAM_VERSION, FUNDAMENTAL_TEAM_VERSION
from etf_agent.decision.sizing import GATE_PREREQUISITES, POSITION_SIZING_FIELDS, validate_position_sizing
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES, KNOBS

from test_gate_prerequisites import (
    anchored_sizing, decision_item, rule_for, sized, stance, three_symbol_bundle, three_symbol_intent,
)
from test_portfolio_risk_decision import policy as base_policy
from test_risk_limit_gates import flags, guard, orders

GATE = {
    "version": "v1",
    "beta_window_days": 60,
    "beta_floor_by_stance": {"aggressive": "0.90", "neutral": "0.55", "defensive": None},
    "release_margin": "0.10",
    "max_swaps_per_run": 2,
    "max_rotation_fraction": "0.20",
    "min_holding_days": 5,
    "min_new_conviction": "medium",
    "hold_conviction": "v1",
    "conviction_gap_swap": "v1",
}
HELD_SINCE = {"1101.TW": "2026-09-01T06:00:00+00:00", "2330.TW": "2026-09-01T06:00:00+00:00"}
REVIEW_POLICY = {"version": "v1", "loss_alert": None, "retrace_alert_20d": None, "gain_alert": None}
# 兩檔持股都不要太擠：max_positions=2 時持股已滿，buy 新股一定先被 MAX_POSITIONS 擋下。
LIMITS = dict(max_positions=2, min_positions=0, max_stock_weight="1",
              special_weight_limits={"2330.TW": "1", "2317.TW": "1", "1101.TW": "1"}, max_sector_weight="1")
# 因子 = 對「全池平均因子」的相對係數；池平均 = (0.2 + 0.5 + 2.3) / 3 = 1，所以 beta ≈ 因子。
FACTORS = {"2330.TW": Decimal("0.2"), "1101.TW": Decimal("0.5"), "2317.TW": Decimal("2.3")}


def with_factors(bundle, factors=FACTORS):
    """把每檔行情改成「日報酬 = 因子 × 市場日報酬」（市場日報酬交替正負且大小不同），讓 beta 可預期；最後一根收盤維持原價。"""
    dates = [bar["trade_date"] for bar in bundle["price_series"][0]["bars"]]
    market = [Decimal("0.012") if index % 2 == 0 else Decimal("-0.007") for index in range(len(dates) - 1)]
    latest = {row["symbol"]: Decimal(row["close_price"]) for row in bundle["snapshot"]["latest_prices"]}
    for series in bundle["price_series"]:
        closes = [Decimal(100)]
        for value in market:
            closes.append(closes[-1] * (1 + factors[series["symbol"]] * value))
        scale = latest[series["symbol"]] / closes[-1]
        for bar, close in zip(series["bars"], closes):
            close = (close * scale).quantize(Decimal("0.0001"))
            bar.update(open=str(close), high=str((close * Decimal("1.01")).quantize(Decimal("0.0001"))),
                       low=str((close * Decimal("0.99")).quantize(Decimal("0.0001"))), close=str(close))
        series["series_sha256"] = canonical_sha256(series["bars"])
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def exposure_bundle(cash="336000", positions=None, factors=FACTORS, **rules):
    """2330 持 1000 股（約 16%）、1101 持 5000 股（約 50%）、現金 33.6%；NAV 約 100 萬。"""
    positions = positions if positions is not None else {"2330.TW": 1000, "1101.TW": 5000}
    return with_factors(three_symbol_bundle(cash, positions, **{**LIMITS, **rules}), factors)


def gate_with(**changes):
    gate = copy.deepcopy(GATE)
    gate["held_since_by_symbol"] = dict(HELD_SINCE)
    gate.update(changes)
    return gate


def exposure_sizing(bundle, gate=None, **overrides):
    sizing = anchored_sizing(bundle)
    sizing["exposure_rotation"] = gate if gate is not None else gate_with()
    sizing.update(overrides)
    return sizing


def hold_item(symbol, conviction, adopt=(), reject=()):
    return decision_item(symbol, "hold", conviction, adopt=adopt, reject=reject)


def decisions(c1101="low", c2330="medium", fill="high", fill_intent="buy"):
    """2317 買進（被名額擋下的換入候選）；兩檔持股都續抱並給信心。"""
    return [
        hold_item("1101.TW", c1101, adopt=["bull-1101-1"]),
        decision_item("2317.TW", fill_intent, fill, adopt=["bull-2317-1"]),
        hold_item("2330.TW", c2330, adopt=["bull-2330-1", "bear-2330-1"]),
    ]


def run_world(bundle=None, items=None, level="neutral", gate=None, sizing_overrides=None, policy=None, **policy_fields):
    bundle = bundle or exposure_bundle()
    intent = three_symbol_intent(bundle, items if items is not None else decisions())
    sizing = exposure_sizing(bundle, gate, **(sizing_overrides or {}))
    base = policy if policy is not None else base_policy()
    base["sector_by_symbol"]["1101.TW"] = "水泥"
    fields = dict(position_review=copy.deepcopy(REVIEW_POLICY), liquidity_fill_rate="1", max_turnover_rate="1", **LIMITS)
    fields.update(policy_fields)
    policy, body = sized(bundle, intent, stance(level, "middle", "concentrated"), sizing, base, **fields)
    return bundle, intent, policy, body


def flag_map(body):
    return {(flag["symbol"], flag["code"]) for flag in body["constraint_flags"]}


def codes(body, prefix):
    return [flag for flag in body["constraint_flags"] if flag["code"].startswith(prefix)]


# ---------------------------------------------------------------- 閘門形狀、前提與登記
class GateShapeTests(unittest.TestCase):
    def sizing(self, **changes):
        bundle = exposure_bundle()
        return exposure_sizing(bundle, gate_with(**changes))

    def errors(self, sizing):
        policy = base_policy()
        policy["position_sizing"] = sizing
        errors = []
        validate_position_sizing(policy, ["1101.TW", "2317.TW", "2330.TW"], errors)
        return errors

    def test_registered_as_human_only_gate_and_binding_field(self):
        self.assertIn("exposure_rotation", POSITION_SIZING_FIELDS)
        self.assertIn("hold_conviction_by_symbol", POSITION_SIZING_FIELDS)
        self.assertIn("exposure_rotation", FORBIDDEN_PREFIXES)
        self.assertIn("hold_conviction_by_symbol", FORBIDDEN_PREFIXES)
        self.assertTrue(rule_for("exposure_rotation").human_only)
        self.assertFalse(any(knob.startswith("exposure_rotation") for knob in KNOBS))
        for field in ("rotation", "held_add_priority", "selection_ranking", "reentry_cooldown", "turnover_fit", "cash_buffer_floor"):
            self.assertIn(field, rule_for("exposure_rotation").requires)

    def test_valid_gate_passes(self):
        self.assertEqual(self.errors(self.sizing()), [])
        self.assertEqual(exposure_rotation_gate(self.sizing())["version"], "v1")
        self.assertIsNone(exposure_rotation_gate({}))
        self.assertTrue(hold_conviction_enabled(self.sizing()))
        self.assertFalse(hold_conviction_enabled({}))
        self.assertFalse(hold_conviction_enabled(None))
        self.assertTrue(conviction_gap_swap_enabled(self.sizing()["exposure_rotation"]))

    def test_gap_swap_is_optional_and_exposure_trigger_can_be_switched_off(self):
        sizing = self.sizing()
        del sizing["exposure_rotation"]["conviction_gap_swap"]
        sizing["exposure_rotation"]["beta_floor_by_stance"] = {"aggressive": None, "neutral": None, "defensive": None}
        self.assertEqual(self.errors(sizing), [])
        self.assertFalse(conviction_gap_swap_enabled(sizing["exposure_rotation"]))

    def test_shape_violations_are_rejected(self):
        cases = {
            "未允許欄位": {"threshold": "1"},
            "version 只允許": {"version": "v4"},
            "hold_conviction": {"hold_conviction": "v2"},
            "conviction_gap_swap": {"conviction_gap_swap": "v2"},
            "beta_window_days": {"beta_window_days": 5},
            "max_swaps_per_run": {"max_swaps_per_run": 0},
            "min_holding_days": {"min_holding_days": -1},
            "release_margin": {"release_margin": "0"},
            "max_rotation_fraction": {"max_rotation_fraction": "0.9"},
            "min_new_conviction": {"min_new_conviction": "ultra"},
            "defensive 只能是 null": {"beta_floor_by_stance": {"aggressive": "0.9", "neutral": "0.55", "defensive": "0.3"}},
            "aggressive ≥ neutral": {"beta_floor_by_stance": {"aggressive": "0.5", "neutral": "0.55", "defensive": None}},
            "大於 release_margin": {"beta_floor_by_stance": {"aggressive": "0.9", "neutral": "0.08", "defensive": None}},
            "剛好包含": {"beta_floor_by_stance": {"aggressive": "0.9", "neutral": "0.55"}},
            "held_since_by_symbol 必須是物件": {"held_since_by_symbol": ["2330.TW"]},
            "依代號排序的大寫代號": {"held_since_by_symbol": {"2330.tw": HELD_SINCE["2330.TW"]}},
            "必須包含時區": {"held_since_by_symbol": {"2330.TW": "2026-09-01T06:00:00"}},
        }
        for message, change in cases.items():
            with self.subTest(message=message):
                errors = self.errors(self.sizing(**change))
                self.assertTrue(any(message in error for error in errors), (message, errors))

    def test_missing_required_fields_are_rejected(self):
        for field in ("version", "beta_window_days", "beta_floor_by_stance", "release_margin", "max_swaps_per_run",
                      "max_rotation_fraction", "min_holding_days", "min_new_conviction", "hold_conviction"):
            sizing = self.sizing()
            del sizing["exposure_rotation"][field]
            self.assertTrue(any("缺少必要欄位" in error and field in error for error in self.errors(sizing)), field)
        sizing = self.sizing()
        sizing["exposure_rotation"] = "v1"
        self.assertTrue(any("必須是物件" in error for error in self.errors(sizing)))

    def test_min_new_conviction_must_be_at_least_as_strict_as_rotation(self):
        sizing = self.sizing(min_new_conviction="low")  # rotation.min_new_conviction = medium
        self.assertTrue(any("不低於 rotation.min_new_conviction" in error for error in self.errors(sizing)))

    def test_prerequisites_are_enforced(self):
        for field in ("rotation", "held_add_priority", "selection_ranking", "reentry_cooldown", "turnover_fit", "cash_buffer_floor"):
            sizing = self.sizing()
            sizing.pop(field)
            self.assertTrue(any("exposure_rotation 必須與 %s 同時存在" % field in error for error in self.errors(sizing)), field)

    def test_position_review_is_a_top_level_prerequisite(self):
        bundle = exposure_bundle()
        policy = base_policy()
        policy["position_sizing"] = exposure_sizing(bundle)
        policy["content_sha256"] = decision_policy_sha256(policy)
        errors = DecisionPolicyValidator(bundle).validate(policy)
        self.assertTrue(any("必須與 position_review 同時存在" in error for error in errors), errors)

    def test_hold_conviction_binding_requires_the_gate(self):
        bundle = exposure_bundle()
        intent = three_symbol_intent(bundle, decisions())
        # 沒有閘門時，綁定表出現即拒絕；有閘門時缺少即拒絕。
        _, _, policy, _ = run_world(bundle)
        broken = copy.deepcopy(policy)
        del broken["position_sizing"]["hold_conviction_by_symbol"]
        errors = DecisionPolicyValidator(bundle).validate(broken)
        self.assertTrue(any("hold_conviction_by_symbol 必須與" in error for error in errors), errors)
        extra = copy.deepcopy(policy)
        del extra["position_sizing"]["exposure_rotation"]
        errors = DecisionPolicyValidator(bundle).validate(extra)
        self.assertTrue(any("hold_conviction_by_symbol 必須與" in error for error in errors), errors)
        dup = copy.deepcopy(policy)
        dup["position_sizing"]["hold_conviction_by_symbol"]["2317.TW"] = "high"
        self.assertTrue(any("不得與 conviction_by_symbol 重複" in error for error in DecisionPolicyValidator(bundle).validate(dup)))
        bad = copy.deepcopy(policy)
        bad["position_sizing"]["hold_conviction_by_symbol"]["2330.TW"] = "huge"
        self.assertTrue(any("等級無效" in error for error in DecisionPolicyValidator(bundle).validate(bad)))
        self.assertIsNotNone(intent)


# ---------------------------------------------------------------- beta 模型
def independent_betas(bundle, window=60):
    """不經 BetaModel 的獨立重算（以收盤價序列逐日報酬、等權池平均）。"""
    closes = {s["symbol"]: [Decimal(bar["close"]) for bar in s["bars"][-(window + 1):]] for s in bundle["price_series"]}
    returns = {symbol: [b / a - 1 for a, b in zip(values, values[1:])] for symbol, values in closes.items()}
    pool = [sum(day) / len(day) for day in zip(*returns.values())]
    mean = sum(pool) / len(pool)
    variance = sum((value - mean) ** 2 for value in pool)
    result = {}
    for symbol, row in returns.items():
        row_mean = sum(row) / len(row)
        covariance = sum((a - row_mean) * (b - mean) for a, b in zip(row, pool))
        up = [(a, b) for a, b in zip(row, pool) if b > 0]
        down = [(a, b) for a, b in zip(row, pool) if b < 0]
        result[symbol] = (
            (covariance / variance).quantize(Decimal("0.000001")),
            (sum(a for a, _ in up) / len(up) / (sum(b for _, b in up) / len(up))).quantize(Decimal("0.000001")),
            (sum(a for a, _ in down) / len(down) / (sum(b for _, b in down) / len(down))).quantize(Decimal("0.000001")),
        )
    return result


class BetaModelTests(unittest.TestCase):
    def setUp(self):
        self.bundle = exposure_bundle()

    def test_betas_and_captures_match_an_independent_computation(self):
        model = BetaModel(self.bundle, 60)
        expected = independent_betas(self.bundle)
        self.assertTrue(model.available)
        for symbol, values in expected.items():
            self.assertEqual(model.stats(symbol), values, symbol)
        # 因子模型：beta 約等於因子（池平均因子為 1），低 beta 與高 beta 的次序確定。
        self.assertLess(model.beta("2330.TW"), model.beta("1101.TW"))
        self.assertLess(model.beta("1101.TW"), model.beta("2317.TW"))
        self.assertAlmostEqual(float(model.beta("2317.TW")), 2.3, delta=0.02)

    def test_window_uses_the_last_n_plus_one_dates(self):
        model = BetaModel(self.bundle, 30)
        self.assertEqual(len(model.dates), 31)
        self.assertEqual(model.dates[-1], "2026-09-19")
        self.assertEqual(model.stats("2330.TW"), independent_betas(self.bundle, 30)["2330.TW"])

    def test_not_enough_history_makes_everything_unavailable_without_filling(self):
        model = BetaModel(self.bundle, 200)
        self.assertFalse(model.available)
        self.assertEqual(model.stats("2330.TW"), (None, None, None))

    def test_missing_bar_in_the_window_is_null_not_zero(self):
        bundle = copy.deepcopy(self.bundle)
        series = next(s for s in bundle["price_series"] if s["symbol"] == "1101.TW")
        del series["bars"][-10]
        model = BetaModel(bundle, 60)
        self.assertEqual(model.stats("1101.TW"), (None, None, None))
        self.assertIsNotNone(model.beta("2330.TW"))
        self.assertEqual(model.stats("9999.TW"), (None, None, None))

    def test_zero_pool_variance_is_unavailable(self):
        flat = exposure_bundle(factors={"2330.TW": Decimal(0), "1101.TW": Decimal(0), "2317.TW": Decimal(0)})
        self.assertFalse(BetaModel(flat, 60).available)

    def test_portfolio_beta_never_treats_missing_as_zero(self):
        weights = {"A": Decimal("0.4"), "B": Decimal("0.2")}
        self.assertEqual(portfolio_beta(weights, {"A": Decimal("1"), "B": Decimal("0.5")}), Decimal("0.500000"))
        self.assertIsNone(portfolio_beta(weights, {"A": Decimal("1"), "B": None}))
        self.assertEqual(portfolio_beta({}, {}), Decimal("0.000000"))

    def test_holding_weights_use_the_engine_nav_formula(self):
        weights, nav, cash_weight = holding_weights(self.bundle)
        self.assertEqual(nav, Decimal("336000") + Decimal("1000") * Decimal("164") + Decimal("5000") * Decimal("100"))
        self.assertEqual(sum(weights.values()) + cash_weight, Decimal(1))


# ---------------------------------------------------------------- 曝險檢視（team_inputs 2.7 的內容）
class ExposureReviewTests(unittest.TestCase):
    def setUp(self):
        self.bundle = exposure_bundle()
        self.policy = {"position_sizing": exposure_sizing(self.bundle)}

    def test_review_fields_and_numbers(self):
        review = build_exposure_review(self.bundle, self.policy)
        self.assertEqual(set(review), REVIEW_FIELDS)
        self.assertEqual(review["schema_version"], EXPOSURE_REVIEW_VERSION)
        self.assertEqual(set(review["portfolio"]), PORTFOLIO_FIELDS)
        self.assertEqual([item["symbol"] for item in review["items"]], ["1101.TW", "2330.TW"])
        for item in review["items"]:
            self.assertEqual(set(item), ITEM_FIELDS)
        expected = independent_betas(self.bundle)
        by_symbol = {item["symbol"]: item for item in review["items"]}
        self.assertEqual(by_symbol["2330.TW"]["beta"], str(expected["2330.TW"][0]))
        weights, _, cash_weight = holding_weights(self.bundle)
        total = sum(weights[s] * expected[s][0] for s in weights)
        self.assertEqual(review["portfolio"]["portfolio_beta"], str(total.quantize(Decimal("0.000001"))))
        self.assertEqual(review["portfolio"]["cash_weight"], str(cash_weight.quantize(Decimal("0.000001"))))
        # 2330 beta 0.2 < 0.3：只有它算低 beta，佔 NAV 約 16.4%。
        self.assertEqual(review["portfolio"]["low_beta_share"], str(weights["2330.TW"].quantize(Decimal("0.000001"))))
        self.assertEqual(review["portfolio"]["uncovered_symbols"], [])
        self.assertEqual(review["pool"], {"size": 3, "return_days": 60, "first_date": review["pool"]["first_date"], "last_date": "2026-09-19"})

    def test_review_contains_no_market_judgement_or_instruction(self):
        text = json.dumps(build_exposure_review(self.bundle, self.policy), ensure_ascii=False)
        for word in ("regime", "bull", "bear", "轉向", "建議賣出", "stance"):
            self.assertNotIn(word, text)

    def test_missing_coverage_makes_portfolio_level_null(self):
        bundle = copy.deepcopy(self.bundle)
        series = next(s for s in bundle["price_series"] if s["symbol"] == "1101.TW")
        del series["bars"][-10]
        review = build_exposure_review(bundle, self.policy)
        self.assertIsNone(review["portfolio"]["portfolio_beta"])
        self.assertIsNone(review["portfolio"]["invested_beta"])
        self.assertIsNone(review["portfolio"]["low_beta_share"])
        self.assertEqual(review["portfolio"]["uncovered_symbols"], ["1101.TW"])
        self.assertIsNone(next(i for i in review["items"] if i["symbol"] == "1101.TW")["beta"])

    def test_no_gate_is_a_hard_error(self):
        with self.assertRaisesRegex(DecisionToolError, "沒有 position_sizing.exposure_rotation"):
            build_exposure_review(self.bundle, {"position_sizing": {}})
        broken = {"position_sizing": exposure_sizing(self.bundle, gate_with(beta_window_days=3))}
        with self.assertRaises(DecisionToolError):
            build_exposure_review(self.bundle, broken)

    def test_validator_rejects_tampering_and_unknown_fields(self):
        review = build_exposure_review(self.bundle, self.policy)
        errors = []
        validate_exposure_review(self.bundle, self.policy, review, errors)
        self.assertEqual(errors, [])
        tampered = copy.deepcopy(review)
        tampered["portfolio"]["portfolio_beta"] = "0.900000"
        tampered_item = copy.deepcopy(review)
        tampered_item["items"][0]["beta"] = "1.000000"
        extra = dict(review, advice="買進")
        for case in (tampered, tampered_item, extra, "not-a-mapping", {}):
            errors = []
            validate_exposure_review(self.bundle, self.policy, case, errors)
            self.assertTrue(errors, case)

    def test_base_and_final_policy_build_the_same_review(self):
        bundle, _, final, _ = run_world()
        base = copy.deepcopy(final)
        base["position_sizing"] = {k: v for k, v in final["position_sizing"].items() if k != "hold_conviction_by_symbol"}
        self.assertEqual(build_exposure_review(bundle, base), build_exposure_review(bundle, final))

    def test_brief_is_background_only(self):
        review = build_exposure_review(self.bundle, self.policy)
        brief = exposure_review_brief(review)
        self.assertFalse(brief["background"]["citable"])
        self.assertNotIn("evidence_ids", json.dumps(brief))
        self.assertEqual(set(brief["by_symbol"]), {"1101.TW", "2330.TW"})
        self.assertEqual(set(brief["by_symbol"]["2330.TW"]), {"weight", "beta", "up_capture", "down_capture"})


# ---------------------------------------------------------------- 換股配對（純函式）
def victim(symbol, conviction, beta, weight="0.05", value="50000"):
    return Victim(symbol, conviction, None if beta is None else Decimal(beta), Decimal(weight), Decimal(value))


def fill(symbol, conviction, beta):
    return Fill(symbol, conviction, None if beta is None else Decimal(beta))


class PlanTests(unittest.TestCase):
    NAV = Decimal("1000000")

    @staticmethod
    def exposure_only(**changes):
        gate = gate_with(**changes)
        del gate["conviction_gap_swap"]
        return gate

    def plan(self, victims, fills, stance_name="neutral", beta="0.20", gate=None, afford=None):
        return exposure_rotation_plan(
            gate or gate_with(), stance_name, None if beta is None else Decimal(beta), self.NAV, victims, fills, afford,
        )

    def test_defensive_or_null_floor_does_not_trigger(self):
        result = self.plan([victim("V", "low", "0.1")], [fill("F", "high", "2")], stance_name="defensive", gate=self.exposure_only())
        self.assertEqual(result["swaps"], [])
        self.assertEqual(result["skipped"]["exposure"], "stance_not_triggered")
        off = self.exposure_only(beta_floor_by_stance={"aggressive": None, "neutral": None, "defensive": None})
        self.assertEqual(self.plan([victim("V", "low", "0.1")], [fill("F", "high", "2")], gate=off)["skipped"]["exposure"],
                         "stance_not_triggered")

    def test_beta_unavailable_and_at_target(self):
        self.assertEqual(self.plan([], [], beta=None)["skipped"]["exposure"], "beta_unavailable")
        self.assertEqual(self.plan([], [], beta="0.55")["skipped"]["exposure"], "beta_at_target")
        self.assertEqual(self.plan([], [], beta="0.90")["skipped"]["exposure"], "beta_at_target")

    def test_release_margin_boundary_is_literal(self):
        # 下限 0.55、margin 0.10：beta 在 [0.45, 0.55) 觸發但已在釋放帶內，零換股；低於 0.45 才補。
        victims, fills = [victim("V", "low", "0.1")], [fill("F", "high", "2")]
        for beta in ("0.45", "0.50", "0.549999"):
            result = self.plan(victims, fills, beta=beta, gate=self.exposure_only())
            self.assertEqual((result["swaps"], result["skipped"]["exposure"]), ([], "beta_within_release_margin"), beta)
        result = self.plan(victims, fills, beta="0.449999", gate=self.exposure_only())
        self.assertEqual([(s["victim"], s["fill"]) for s in result["swaps"]], [("V", "F")])

    def test_victim_order_is_low_conviction_then_beta_then_symbol_and_high_is_never_a_victim(self):
        victims = [victim("D", "medium", "0.05"), victim("C", "low", "0.15"), victim("B", "low", "0.10"),
                   victim("A", "low", "0.10"), victim("H", "high", "0.01")]
        fills = [fill("F1", "high", "2"), fill("F2", "high", "2"), fill("F3", "medium", "2")]
        gate = gate_with(max_swaps_per_run=3, conviction_gap_swap=None)
        del gate["conviction_gap_swap"]
        result = self.plan(victims, fills, beta="0.20", gate=gate)
        self.assertEqual([s["victim"] for s in result["swaps"]], ["A", "B", "C"])
        self.assertNotIn("H", [s["victim"] for s in result["swaps"]])

    def test_victim_beta_must_be_below_portfolio_beta_and_known(self):
        result = self.plan([victim("V", "low", "0.30"), victim("U", "low", None)], [fill("F", "high", "2")], beta="0.20")
        self.assertEqual(result["swaps"][:1] if result["swaps"] and result["swaps"][0]["kind"] == "exposure" else [], [])
        self.assertEqual(result["skipped"]["exposure"], "no_eligible_victim")

    def test_fill_needs_beta_margin_conviction_and_known_beta(self):
        victims = [victim("V", "low", "0.10")]
        for bad in (fill("F", "high", "0.29"), fill("F", "high", None), fill("F", "low", "2")):
            result = self.plan(victims, [bad])
            self.assertEqual([s for s in result["swaps"] if s["kind"] == "exposure"], [], bad)
            self.assertEqual(result["skipped"]["exposure"], "no_eligible_fill", bad)
        # 恰好 = 組合 beta + margin 可入選
        ok = self.plan(victims, [fill("F", "medium", "0.30")])
        self.assertEqual(ok["swaps"][0]["fill"], "F")

    def test_caps_swaps_and_rotation_fraction(self):
        victims = [victim("V%d" % i, "low", "0.0%d" % i, weight="0.001", value="60000") for i in (1, 2, 3)]
        fills = [fill("F1", "high", "9"), fill("F2", "high", "9"), fill("F3", "high", "9")]
        gate = gate_with(max_swaps_per_run=2, max_rotation_fraction="0.20", conviction_gap_swap=None)
        del gate["conviction_gap_swap"]
        self.assertEqual(len(self.plan(victims, fills, gate=gate)["swaps"]), 2)
        # 12 萬 = 12% NAV；0.15 的上限只容得下兩對（再加一對 18% 超過），0.11 連第二對都不行。
        for fraction, count in (("0.15", 2), ("0.11", 1)):
            capped = dict(gate, max_rotation_fraction=fraction, max_swaps_per_run=3)
            self.assertEqual(len(self.plan(victims, fills, gate=capped)["swaps"]), count, fraction)
        tiny = dict(gate, max_rotation_fraction="0.05")
        result = self.plan(victims, fills, gate=tiny)
        self.assertEqual((result["swaps"], result["skipped"]["exposure"]), ([], "rotation_fraction"))

    def test_stops_when_projected_beta_reaches_the_release_level(self):
        # 目標 0.45：第一對 Δ = 0.30 × (2 − 0.1) = 0.57 已超過，不再配第二對。
        victims = [victim("V1", "low", "0.10", weight="0.30"), victim("V2", "low", "0.11", weight="0.30")]
        fills = [fill("F1", "high", "2"), fill("F2", "high", "2")]
        result = self.plan(victims, fills)
        self.assertEqual([(s["victim"], s["fill"]) for s in result["swaps"] if s["kind"] == "exposure"], [("V1", "F1")])

    def test_cash_infeasible_skips_to_the_next_victim_and_reports(self):
        calls = []

        def afford(victim_symbol, fill_symbol):
            calls.append((victim_symbol, fill_symbol))
            return victim_symbol == "V2"

        victims = [victim("V1", "low", "0.01"), victim("V2", "low", "0.02")]
        result = self.plan(victims, [fill("F", "high", "2")], afford=afford)
        self.assertEqual([(s["victim"], s["fill"]) for s in result["swaps"]], [("V2", "F")])
        self.assertEqual(calls, [("V1", "F"), ("V2", "F")])
        none = self.plan(victims, [fill("F", "high", "2")], afford=lambda *_: False)
        self.assertEqual((none["swaps"], none["skipped"]["exposure"]), ([], "cash_infeasible"))

    def test_cash_infeasible_top_fill_does_not_block_an_affordable_later_fill(self):
        victims = [victim("V1", "low", "0.01"), victim("V2", "low", "0.02")]
        fills = [fill("EXPENSIVE", "high", "2"), fill("AFFORDABLE", "medium", "2")]
        calls = []

        def afford(v, f):
            calls.append((v, f))
            return f == "AFFORDABLE"

        result = self.plan(victims, fills, gate=self.exposure_only(), afford=afford)
        self.assertEqual([(s["victim"], s["fill"]) for s in result["swaps"]], [("V1", "AFFORDABLE")])
        self.assertEqual(calls, [("V1", "EXPENSIVE"), ("V2", "EXPENSIVE"), ("V1", "AFFORDABLE")])

    def test_gap_swap_also_tries_the_next_fill_and_keeps_the_swap_cap(self):
        victims = [victim("V1", "low", "0.01"), victim("V2", "low", "0.02")]
        fills = [fill(s, "high", "2") for s in ("EXPENSIVE", "F2", "F3")]
        result = self.plan(victims, fills, stance_name="defensive", gate=gate_with(max_swaps_per_run=1),
                           afford=lambda v, f: f != "EXPENSIVE")
        self.assertEqual([(s["kind"], s["fill"]) for s in result["swaps"]], [("gap", "F2")])

    def test_a_victim_and_a_fill_are_used_at_most_once_and_pairs_are_balanced(self):
        victims = [victim("V1", "low", "0.01"), victim("V2", "low", "0.02")]
        result = self.plan(victims, [fill("F1", "high", "2")], gate=gate_with(max_swaps_per_run=2))
        exposure = [s for s in result["swaps"] if s["kind"] == "exposure"]
        self.assertEqual(len(exposure), 1)
        used = [s["victim"] for s in result["swaps"]] + [s["fill"] for s in result["swaps"]]
        self.assertEqual(len(used), len(set(used)))

    def test_gap_swap_ignores_stance_and_beta_and_comes_after_exposure(self):
        victims = [victim("L1", "low", None), victim("M", "medium", "0.01")]
        fills = [fill("H1", "high", None), fill("M1", "medium", "3")]
        result = self.plan(victims, fills, stance_name="defensive", beta=None)
        self.assertEqual([(s["kind"], s["victim"], s["fill"]) for s in result["swaps"]], [("gap", "L1", "H1")])
        self.assertEqual(result["skipped"]["exposure"], "stance_not_triggered")
        self.assertIsNone(result["skipped"]["gap"])
        # 共用 max_swaps_per_run：曝險換股先用完名額，信心差換股就沒有名額
        both = self.plan([victim("V", "low", "0.01"), victim("W", "low", "0.02")],
                         [fill("F", "high", "2"), fill("G", "high", "2")], gate=gate_with(max_swaps_per_run=1))
        self.assertEqual([s["kind"] for s in both["swaps"]], ["exposure"])
        self.assertEqual(both["skipped"]["gap"], "max_swaps")

    def test_gap_swap_can_be_switched_off_independently(self):
        gate = gate_with()
        del gate["conviction_gap_swap"]
        result = self.plan([victim("L", "low", None)], [fill("H", "high", None)], stance_name="defensive", beta=None, gate=gate)
        self.assertEqual(result["swaps"], [])
        self.assertIsNone(result["skipped"]["gap"])

    def test_gap_swap_only_pairs_high_new_with_low_held(self):
        result = self.plan([victim("M", "medium", None)], [fill("H", "high", None)], stance_name="defensive", beta=None)
        self.assertEqual((result["swaps"], result["skipped"]["gap"]), ([], "no_eligible_victim"))
        result = self.plan([victim("L", "low", None)], [fill("M", "medium", None)], stance_name="defensive", beta=None)
        self.assertEqual((result["swaps"], result["skipped"]["gap"]), ([], "no_eligible_fill"))


# ---------------------------------------------------------------- 持有天數（帳本）
def rows(*pairs):
    return [{"run_id": "r%d" % index, "as_of": as_of, "symbols": list(symbols)} for index, (as_of, symbols) in enumerate(pairs)]


class HoldingDaysTests(unittest.TestCase):
    def test_continuous_run_back_from_the_head_and_resets_after_a_gap(self):
        history = rows(
            ("2026-09-01T06:00:00+00:00", ["2330.TW"]),
            ("2026-09-02T06:00:00+00:00", ["1101.TW"]),          # 2330 賣出，1101 買進
            ("2026-09-03T06:00:00+00:00", ["1101.TW", "2330.TW"]),  # 2330 重新買進
            ("2026-09-04T06:00:00+00:00", ["1101.TW", "2330.TW"]),
        )
        result = held_since_from_history(history, ["2330.TW", "1101.TW"])
        self.assertEqual(result, {"1101.TW": "2026-09-02T06:00:00+00:00", "2330.TW": "2026-09-03T06:00:00+00:00"})

    def test_head_must_equal_the_account_holdings(self):
        history = rows(("2026-09-01T06:00:00+00:00", ["2330.TW"]))
        for held in (["2330.TW", "1101.TW"], []):
            with self.assertRaisesRegex(DecisionToolError, "不一致"):
                held_since_from_history(history, held)
        with self.assertRaisesRegex(DecisionToolError, "空的"):
            held_since_from_history([], [])

    def test_bind_rejects_holdings_chain_that_differs_from_nav_chain(self):
        bundle, _, policy, _ = run_world()
        template = copy.deepcopy(policy)
        template["position_sizing"] = anchored_gate_template(bundle)
        template["drawdown_control"] = {"history_ref": {"head_run_id": "r1", "length": 2, "sha256": "x"}}
        held = ["1101.TW", "2330.TW"]
        history = rows(("2026-09-01T06:00:00+00:00", held), ("2026-09-02T06:00:00+00:00", held))
        nav = [{"run_id": r["run_id"], "as_of": r["as_of"], "nav": "1"} for r in history]
        bind_holding_days(template, bundle, history, nav)  # 同一條鏈：通過
        mixed = [dict(history[0], run_id="other", as_of="2026-08-01T06:00:00+00:00"), history[1]]  # head 相同、中間不同
        for bad_holdings, bad_nav in ((mixed, nav), (history[1:], nav), (history, nav[:1] + [dict(nav[1], as_of="x")])):
            with self.subTest(len=len(bad_holdings)):
                with self.assertRaisesRegex(DecisionToolError, "不是同一條鏈|晚於|最新一筆"):
                    bind_holding_days(template, bundle, bad_holdings, bad_nav)

    def test_bind_requires_history_head_and_refuses_prefill(self):
        bundle, _, policy, _ = run_world()
        template = copy.deepcopy(policy)
        template["position_sizing"] = anchored_gate_template(bundle)
        template["drawdown_control"] = {"history_ref": {"head_run_id": "r1", "length": 2, "sha256": "x"}}
        history = rows(("2026-09-01T06:00:00+00:00", ["1101.TW", "2330.TW"]), ("2026-09-02T06:00:00+00:00", ["1101.TW", "2330.TW"]))
        bound = bind_holding_days(template, bundle, history)
        self.assertEqual(bound["position_sizing"]["exposure_rotation"]["held_since_by_symbol"],
                         {"1101.TW": "2026-09-01T06:00:00+00:00", "2330.TW": "2026-09-01T06:00:00+00:00"})
        self.assertNotIn("held_since_by_symbol", template["position_sizing"]["exposure_rotation"])
        # 沒有閘門：原樣、不碰帳本
        self.assertEqual(bind_holding_days({"position_sizing": {}}, bundle, None), {"position_sizing": {}})
        cases = (
            (None, "未提供"),
            (history[:1], "最新一筆不是"),
            ([], "最新一筆不是"),
            (rows(("2026-09-30T06:00:00+00:00", ["1101.TW", "2330.TW"]), ("2026-09-30T07:00:00+00:00", ["1101.TW", "2330.TW"])), "晚於 decision_cutoff"),
            (rows(("2026-09-01T06:00:00+00:00", ["1101.TW"]), ("2026-09-02T06:00:00+00:00", ["1101.TW"])), "不一致"),
        )
        for bad, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(DecisionToolError, message):
                    bind_holding_days(template, bundle, bad)
        prefilled = copy.deepcopy(template)
        prefilled["position_sizing"]["exposure_rotation"]["held_since_by_symbol"] = dict(HELD_SINCE)
        with self.assertRaisesRegex(DecisionToolError, "不得預填"):
            bind_holding_days(prefilled, bundle, history)
        no_ref = copy.deepcopy(template)
        no_ref["drawdown_control"] = {}
        with self.assertRaisesRegex(DecisionToolError, "history_ref"):
            bind_holding_days(no_ref, bundle, history)

    def test_holding_days_are_calendar_days_in_taipei_and_fail_closed(self):
        bundle, _, policy, _ = run_world()
        cutoff = datetime.fromisoformat(bundle["decision_cutoff"]).astimezone(timezone(timedelta(hours=8))).date()
        days = holding_days_by_symbol(policy, bundle)
        self.assertEqual(days["2330.TW"], (cutoff - datetime(2026, 9, 1, 14, 0).date()).days)
        unbound = copy.deepcopy(policy)
        del unbound["position_sizing"]["exposure_rotation"]["held_since_by_symbol"]
        with self.assertRaisesRegex(DecisionToolError, "尚未綁定"):
            holding_days_by_symbol(unbound, bundle)
        mismatch = copy.deepcopy(policy)
        mismatch["position_sizing"]["exposure_rotation"]["held_since_by_symbol"].pop("1101.TW")
        with self.assertRaisesRegex(DecisionToolError, "不一致"):
            holding_days_by_symbol(mismatch, bundle)

    def test_finalizer_side_check_recomputes_from_the_ledger(self):
        bundle, _, policy, _ = run_world()
        policy["drawdown_control"] = {"history_ref": {"head_run_id": "r1", "length": 2, "sha256": "x"}}
        good = rows(("2026-09-01T06:00:00+00:00", ["1101.TW", "2330.TW"]), ("2026-09-02T06:00:00+00:00", ["1101.TW", "2330.TW"]))

        def check(provider, pol=policy):
            errors = []
            validate_held_since(pol, bundle, provider, errors)
            return errors

        self.assertEqual(check(lambda head: good), [])
        self.assertTrue(any("未提供帳本來源" in e for e in check(None)))

        def broken(head):
            raise RuntimeError("帳本損毀")

        self.assertTrue(any("無法由帳本重算" in e and "帳本損毀" in e for e in check(broken)))
        self.assertTrue(any("與帳本重算結果不一致" in e for e in check(lambda head: rows(
            ("2026-09-02T06:00:00+00:00", ["1101.TW", "2330.TW"]), ("2026-09-03T06:00:00+00:00", ["1101.TW", "2330.TW"])))))
        self.assertTrue(any("最新一筆不是" in e for e in check(lambda head: good[:1])))
        unbound = copy.deepcopy(policy)
        del unbound["position_sizing"]["exposure_rotation"]["held_since_by_symbol"]
        self.assertTrue(any("尚未綁定" in e for e in check(lambda head: good, unbound)))
        no_ref = copy.deepcopy(policy)
        no_ref["drawdown_control"] = {}
        self.assertTrue(any("history_ref" in e for e in check(lambda head: good, no_ref)))
        # 沒有閘門：不碰帳本
        plain = copy.deepcopy(policy)
        del plain["position_sizing"]["exposure_rotation"]
        self.assertEqual(check(None, plain), [])


def anchored_gate_template(bundle):
    sizing = anchored_sizing(bundle)
    sizing["exposure_rotation"] = copy.deepcopy(GATE)
    return sizing


# ---------------------------------------------------------------- 配置引擎（端到端：綁定、配置、重建、Guard）
NARROW = {"positions_by_breadth": {"concentrated": 2, "balanced": 2, "diversified": 2}}


def narrow_world(level="neutral", items=None, gate=None, bundle=None, **kwargs):
    return run_world(bundle, items, level, gate, {**NARROW, **kwargs.pop("sizing_overrides", {})}, **kwargs)


def summary(body):
    return {k: (v["shares"], v["intent"]) for k, v in orders(body).items()}


class AllocationTests(unittest.TestCase):
    def test_low_beta_hold_is_swapped_for_the_blocked_high_beta_buy(self):
        bundle, intent, policy, body = narrow_world()
        self.assertEqual(summary(body), {("2330.TW", "sell"): (1000, "exit"), ("2317.TW", "buy"): (3000, "buy")})
        self.assertEqual(flag_map(body), {
            ("PORTFOLIO", "CONVICTION_GAP_SWAP_SKIPPED:no_eligible_fill"),
            ("2330.TW", "RISK_EXPOSURE_ROTATION"), ("2317.TW", "EXPOSURE_ROTATION_FILL"),
        })
        # 交易 Agent 的意圖沒被改寫：2330 在封存的意圖裡仍是 hold；每一筆賣單都有對應的買單（不會只賣不買）。
        self.assertEqual(next(i for i in intent["items"] if i["symbol"] == "2330.TW")["intent"], "hold")
        self.assertTrue(any(side == "buy" for _, side in orders(body)))
        self.assertTrue(guard(bundle, policy, body)["passed"])
        flag = next(f for f in body["constraint_flags"] if f["code"] == "RISK_EXPOSURE_ROTATION")
        self.assertEqual(set(flag), {"symbol", "code", "detail"})

    def test_defensive_stance_does_not_trigger_and_leaves_a_skip_flag(self):
        _, _, _, body = narrow_world("defensive")
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertIn(("PORTFOLIO", "EXPOSURE_ROTATION_SKIPPED:stance_not_triggered"), flag_map(body))
        self.assertEqual(codes(body, "RISK_EXPOSURE_ROTATION"), [])

    def test_stance_without_a_floor_or_beta_at_target_does_not_swap(self):
        off = gate_with(beta_floor_by_stance={"aggressive": None, "neutral": None, "defensive": None})
        _, _, _, body = narrow_world(gate=off)
        self.assertIn(("PORTFOLIO", "EXPOSURE_ROTATION_SKIPPED:stance_not_triggered"), flag_map(body))
        # aggressive 下限 0.90：組合 beta 0.28 低於它而觸發，結果與 neutral 相同
        _, _, _, aggressive = narrow_world("aggressive")
        self.assertIn(("2330.TW", "RISK_EXPOSURE_ROTATION"), flag_map(aggressive))
        # 下限壓到組合 beta 以下 → at_target
        low = gate_with(beta_floor_by_stance={"aggressive": "0.30", "neutral": "0.25", "defensive": None})
        _, _, _, body = narrow_world(gate=low)
        self.assertIn(("PORTFOLIO", "EXPOSURE_ROTATION_SKIPPED:beta_at_target"), flag_map(body))

    def test_hold_high_is_never_swapped_out(self):
        _, _, _, body = narrow_world(items=decisions(c2330="high"))
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertEqual(codes(body, "RISK_EXPOSURE_ROTATION"), [])

    def test_victim_beta_must_be_below_the_portfolio_beta(self):
        # 1101（beta 0.5）高於組合 beta 0.28：即使信心 low 也不是受害者；2330 信心 medium 才換
        _, _, _, body = narrow_world(items=decisions(c1101="low"))
        self.assertEqual([f["symbol"] for f in codes(body, "RISK_EXPOSURE_ROTATION")], ["2330.TW"])

    def test_insufficient_holding_days_blocks_the_swap(self):
        recent = {"1101.TW": "2026-09-17T06:00:00+00:00", "2330.TW": "2026-09-17T06:00:00+00:00"}
        _, _, _, body = narrow_world(gate=gate_with(held_since_by_symbol=recent))
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertIn(("PORTFOLIO", "EXPOSURE_ROTATION_SKIPPED:no_eligible_victim"), flag_map(body))

    def test_new_buy_below_min_conviction_is_not_a_fill(self):
        _, _, _, body = narrow_world(items=decisions(fill="low"))
        self.assertEqual(codes(body, "RISK_EXPOSURE_ROTATION"), [])
        self.assertEqual(codes(body, "EXPOSURE_ROTATION_FILL"), [])

    def test_fill_with_a_low_beta_is_not_a_fill(self):
        flat = {"2330.TW": Decimal("0.2"), "1101.TW": Decimal("0.5"), "2317.TW": Decimal("0.3")}
        bundle = exposure_bundle(factors={**flat, "2317.TW": Decimal("1.3") - Decimal("0.7")})
        _, _, _, body = narrow_world(bundle=bundle)
        self.assertEqual(codes(body, "RISK_EXPOSURE_ROTATION"), [])

    def test_max_swaps_zero_style_caps_are_respected(self):
        # max_rotation_fraction 低於 2330 的權重（16.4%）→ 不換
        _, _, _, body = narrow_world(gate=gate_with(max_rotation_fraction="0.10"))
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertIn(("PORTFOLIO", "EXPOSURE_ROTATION_SKIPPED:rotation_fraction"), flag_map(body))

    def test_gap_swap_replaces_a_low_hold_with_a_high_buy_in_any_stance(self):
        bundle = exposure_bundle(cash="736000", positions={"2330.TW": 1000, "1101.TW": 1000})
        items = decisions(c1101="low", c2330="high")
        _, _, policy, body = narrow_world("defensive", items, bundle=bundle)
        self.assertIn(("1101.TW", "RISK_CONVICTION_GAP_SWAP"), flag_map(body))
        self.assertIn(("2317.TW", "CONVICTION_GAP_SWAP_FILL"), flag_map(body))
        self.assertEqual(summary(body)[("1101.TW", "sell")], (1000, "exit"))
        self.assertIn(("2317.TW", "buy"), summary(body))
        self.assertTrue(guard(bundle, policy, body)["passed"])
        # 關掉信心差換股 → 不換
        off = gate_with()
        del off["conviction_gap_swap"]
        _, _, _, plain = narrow_world("defensive", items, gate=off, bundle=bundle)
        self.assertEqual(plain["order_proposal"]["orders"], [])
        self.assertEqual(codes(plain, "CONVICTION_GAP_SWAP"), [])

    def test_tampered_binding_tables_are_rejected_by_the_rebuild(self):
        bundle, intent, policy, body = narrow_world()
        for mutate in (
            lambda p: p["position_sizing"]["hold_conviction_by_symbol"].update({"2330.TW": "high"}),
            lambda p: p["position_sizing"]["exposure_rotation"]["held_since_by_symbol"].update({"2330.TW": "2026-09-18T06:00:00+00:00"}),
            lambda p: p["position_sizing"]["exposure_rotation"].update({"max_swaps_per_run": 1, "max_rotation_fraction": "0.01"}),
        ):
            altered = copy.deepcopy(policy)
            mutate(altered)
            altered["content_sha256"] = decision_policy_sha256(altered)
            errors = DecisionPolicyValidator(bundle).validate(altered)
            rebuilt = ProposalValidator(bundle, altered, intent).validate(body) if not errors else errors
            self.assertTrue(rebuilt)

    def test_missing_hold_conviction_table_is_fail_closed(self):
        bundle, intent, policy, _ = narrow_world()
        broken = copy.deepcopy(policy)
        del broken["position_sizing"]["hold_conviction_by_symbol"]
        with self.assertRaises(DecisionToolError):
            AllocationOrderEngine(bundle, broken).run(intent)

    def test_without_the_gate_the_same_world_is_the_old_behaviour(self):
        bundle = exposure_bundle()
        intent = three_symbol_intent(bundle, decisions(c1101=None, c2330=None))
        sizing = anchored_sizing(bundle, **NARROW)
        policy = base_policy()
        policy["sector_by_symbol"]["1101.TW"] = "水泥"
        policy, body = sized(bundle, intent, stance("neutral", "middle", "concentrated"), sizing, policy,
                             position_review=copy.deepcopy(REVIEW_POLICY), liquidity_fill_rate="1", max_turnover_rate="1", **LIMITS)
        self.assertNotIn("hold_conviction_by_symbol", policy["position_sizing"])
        self.assertEqual(body["order_proposal"]["orders"], [])
        for flag in body["constraint_flags"]:
            self.assertNotIn("EXPOSURE", flag["code"])
            self.assertNotIn("GAP_SWAP", flag["code"])
            self.assertNotEqual(flag["symbol"], "PORTFOLIO")


# ---------------------------------------------------------------- 交易決策的 hold 信心與 team_inputs 2.7
class HoldConvictionTests(unittest.TestCase):
    def setUp(self):
        self.bundle = exposure_bundle()
        self.intent = three_symbol_intent(self.bundle, decisions())
        self.stance = stance("neutral", "middle", "concentrated")

    def test_apply_writes_hold_levels_only_with_the_gate(self):
        policy = base_policy()
        policy["position_sizing"] = exposure_sizing(self.bundle)
        final = apply_trade_decision(policy, self.intent, self.stance, self.bundle)
        self.assertEqual(final["position_sizing"]["hold_conviction_by_symbol"], {"1101.TW": "low", "2330.TW": "medium"})
        self.assertEqual(final["position_sizing"]["conviction_by_symbol"], {"2317.TW": "high"})

    def test_hold_without_conviction_is_rejected_under_the_gate(self):
        intent = three_symbol_intent(self.bundle, decisions(c1101=None))
        policy = base_policy()
        policy["position_sizing"] = exposure_sizing(self.bundle)
        with self.assertRaises(DecisionToolError):
            apply_trade_decision(policy, intent, self.stance, self.bundle)


    def test_trade_validator_requires_hold_levels_only_when_enabled(self):
        from test_portfolio_risk_decision import chain_inputs

        bundle, momentum, debate, intent, _, _, _, _ = chain_inputs()
        items = [dict(item) for item in intent["items"]]
        hold = next(item for item in items if item["intent"] == "hold")

        def errors(values, enabled):
            sealed = seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-x"), values)
            return TradeDecisionValidator(bundle, momentum, debate, hold_conviction=enabled).validate(sealed)

        self.assertEqual(errors(items, False), [])
        hold["conviction"] = "medium"
        self.assertTrue(any("只有 buy／add 可以給 conviction" in e for e in errors(items, False)))
        self.assertEqual(errors(items, True), [])
        hold["conviction"] = None
        self.assertTrue(errors(items, True))


class TeamInputsVersionTests27(unittest.TestCase):
    def build(self, **kwargs):
        from test_team_chain import team_world
        from test_trader import STANCE
        _, _, _, _, _, _, reports, _ = team_world()
        return build_team_inputs(reports, None, STANCE, {"s": 1}, {"p": 1}, {"r": 1}, {"k": 1}, **kwargs)

    def test_exposure_review_makes_2_7_and_needs_fundamental_metrics(self):
        team = self.build(fundamental_metrics=None, exposure_review={"x": 1})
        self.assertEqual(team["schema_version"], EXPOSURE_TEAM_VERSION)
        self.assertEqual(EXPOSURE_TEAM_VERSION, "2.7")
        self.assertEqual(team["exposure_review"], {"x": 1})
        for field in EXPOSURE_TEAM_FIELDS:
            self.assertIn(field, team)
        with self.assertRaises(DecisionToolError):
            self.build(exposure_review={"x": 1})
        with self.assertRaises(DecisionToolError):
            self.build(fundamental_metrics=None, exposure_review="bad")

    def test_without_exposure_review_2_6_is_unchanged(self):
        team = self.build(fundamental_metrics=None)
        self.assertEqual(team["schema_version"], FUNDAMENTAL_TEAM_VERSION)
        self.assertNotIn("exposure_review", team)


if __name__ == "__main__":
    unittest.main()
