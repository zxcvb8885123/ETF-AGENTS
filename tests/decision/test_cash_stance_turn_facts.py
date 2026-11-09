"""cash_stance_turn_facts v1：風險 Agent 現金姿態的轉向事實與 turn_assessment 列舉（紀錄工具，不是規則）。"""

import copy
import json
import math
import tempfile
import unittest
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from etf_agent.automation.agent_runner import DailyPipelineError
from etf_agent.automation.agent_schemas import (
    CASH_GRADE_STANCE_SCHEMA, CASH_STANCE_SCHEMA, cash_stance_recency_schema, cash_stance_turn_facts_schema,
)
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.decision import MARKET_REGIME_EVIDENCE_ID, DecisionToolError, MomentumEngine
from etf_agent.decision.contracts import DecisionContext
from etf_agent.decision.sizing import POSITION_SIZING_FIELDS, validate_cash_stance
from etf_agent.decision.turn_facts import (
    TURN_ASSESSMENTS, _Pool, cash_stance_turn_facts_gate, turn_facts, validate_cash_stance_turn_facts_gate,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_analysts import bundle_with_event
from test_daily_pipeline import FakeRunner
from test_typed_evidence import typed_bundle, typed_reports

ROOT = Path(__file__).resolve().parents[2]
GATE = {"version": "v1"}
RECENCY_GATE = {"version": "v1"}
ARCHIVE = ROOT / "artifacts/daily/2026-11-09/daily-20261109-070407"


def dates(count, end=None):
    """連續 count 個假日期字串（只要可排序；回推到最後一天 2026-12-31 的前 count 天）。"""
    return ["2026-%02d-%02d" % (1 + index // 28, 1 + index % 28) for index in range(count)]


def bars(closes):
    return [
        {"trade_date": day, "open": str(value), "high": str(value + 1), "low": str(value - 1), "close": str(value), "volume": 1000}
        for day, value in zip(dates(len(closes)), closes)
    ]


def fixture(series):
    """最小 bundle：turn_facts 只看 snapshot.latest_trade_date 與 price_series。"""
    count = max(len(values) for values in series.values())
    return {
        "snapshot": {"latest_trade_date": dates(count)[-1]},
        "price_series": [{"symbol": symbol, "evidence_id": "price:" + symbol, "bars": bars(values)} for symbol, values in series.items()],
    }


def q6(value):
    return str(Decimal(value).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def overnight_bundle(typed=True):
    """不附隔夜市場的決策輸入（latest_trade_date 與行情最後一根一致）；typed 時為 typed_evidence_v1。"""
    return typed_bundle() if typed else bundle_with_event()


def stance(assessment="turn_unconfirmed", **extra):
    value = {"level": "neutral", "breadth": "balanced", "rationale": "依轉向事實紀錄。",
             "evidence_ids": [MARKET_REGIME_EVIDENCE_ID], "turn_assessment": assessment}
    value.update(extra)
    return value


def errors_of(bundle, value, gate=GATE, **kwargs):
    errors = []
    validate_cash_stance(DecisionContext(bundle), value, "cash_stance", errors, turn_facts_gate=gate, **kwargs)
    return errors


class TurnFactsGateShapeTests(unittest.TestCase):
    def test_gate_is_a_registered_human_only_field(self):
        self.assertIn("cash_stance_turn_facts", POSITION_SIZING_FIELDS)
        self.assertIn("cash_stance_turn_facts", FORBIDDEN_PREFIXES)
        self.assertEqual(cash_stance_turn_facts_gate({"cash_stance_turn_facts": GATE}), GATE)
        self.assertIsNone(cash_stance_turn_facts_gate({}))
        self.assertIsNone(cash_stance_turn_facts_gate({"cash_stance_turn_facts": "v1"}))

    def test_shape_is_closed(self):
        for bad in ("v1", {"version": "v2"}, {"version": "v1", "window": 5}, {}, None):
            errors = []
            validate_cash_stance_turn_facts_gate({"cash_stance_turn_facts": bad}, errors)
            self.assertTrue(errors, bad)
        errors = []
        validate_cash_stance_turn_facts_gate({"cash_stance_turn_facts": GATE}, errors)
        self.assertEqual(errors, [])

    def test_gate_is_independent_of_the_recency_gate(self):
        from etf_agent.decision.sizing import GATE_PREREQUISITES

        rule = next(rule for rule in GATE_PREREQUISITES if rule.field == "cash_stance_turn_facts")
        self.assertEqual(tuple(rule.requires), ())

    def test_live_config_does_not_enable_it_yet(self):
        policy = json.loads((ROOT / "config/decision_policy.json").read_text(encoding="utf-8"))
        self.assertNotIn("cash_stance_turn_facts", policy["position_sizing"])


class ShortHistoryFactsTests(unittest.TestCase):
    """6 筆行情：可手算日報酬、連漲與 MA5；MA20／regime 等資料不足者記 data_gaps。"""

    def setUp(self):
        self.facts = turn_facts(fixture({
            "A": [10, 10, 10, 10, 11, 12],
            "B": [10, 10, 10, 10, 10, 9],
            "C": [10, 10, 10, 10, 10, 10],
        }))

    def test_pool_returns_are_equal_weighted(self):
        latest = (Decimal(12) / 11 - 1 + Decimal(-1) / 10 + 0) / 3
        prior = (Decimal(11) / 10 - 1) / 3
        self.assertEqual(self.facts["pool_return_latest"], q6(latest))
        self.assertEqual(self.facts["pool_return_prior"], q6(prior))
        self.assertEqual(self.facts["pool_size"], 3)
        self.assertEqual(self.facts["as_of"], dates(6)[-1])

    def test_negative_latest_day_means_zero_up_streak(self):
        self.assertEqual(self.facts["up_streak_days"], 0)

    def test_ma5_share_uses_close_at_or_above_the_average(self):
        # A：12 >= 10.6；B：9 < 9.8；C：10 >= 10（等於算站回）
        self.assertEqual(self.facts["share_above_ma5"], "0.6667")

    def test_insufficient_history_is_listed_not_guessed(self):
        for name in ("share_above_ma20", "new_low_20d_share", "regime", "breadth_above_ma20", "breadth_above_ma60",
                     "breadth_above_ma20_5d_ago", "b20_change_5d", "bear_streak_days"):
            self.assertIsNone(self.facts[name], name)
        text = "\n".join(self.facts["data_gaps"])
        for fragment in ("share_above_ma20", "new_low_20d_share", "regime", "5 個交易日前"):
            self.assertIn(fragment, text)
        self.assertFalse(self.facts["bear_streak_truncated"])

    def test_note_says_facts_are_not_signals(self):
        self.assertIn("不是程式判定的訊號", self.facts["note"])
        self.assertIn("假反彈", self.facts["note"])

    def test_output_is_strings_ints_and_none_only(self):
        for key, value in self.facts.items():
            self.assertNotIsInstance(value, float, key)
            self.assertNotIsInstance(value, Decimal, key)

    def test_consecutive_up_days(self):
        facts = turn_facts(fixture({"A": [10, 11, 12], "B": [10, 10, 11], "C": [10, 10, 10]}))
        self.assertEqual(facts["up_streak_days"], 2)
        self.assertEqual(facts["pool_return_latest"], q6((Decimal(1) / 11 + Decimal(1) / 10) / 3))
        broken = turn_facts(fixture({"A": [10, 11, 12, 12], "B": [10, 10, 11, 11], "C": [10, 10, 10, 10]}))
        self.assertEqual(broken["up_streak_days"], 0)
        self.assertEqual(broken["pool_return_latest"], "0.000000")

    def test_single_day_has_no_returns(self):
        facts = turn_facts(fixture({"A": [10], "B": [10]}))
        self.assertIsNone(facts["pool_return_latest"])
        self.assertIsNone(facts["pool_return_prior"])
        self.assertIsNone(facts["up_streak_days"])
        self.assertTrue(facts["data_gaps"])


class MedianHistoryFactsTests(unittest.TestCase):
    """25 筆行情：A 一路上漲、B 一路下跌、C 持平；MA20 比例與新低比例可手算。"""

    def setUp(self):
        self.facts = turn_facts(fixture({
            "A": list(range(1, 26)),
            "B": list(range(25, 0, -1)),
            "C": [10] * 25,
        }))

    def test_shares_over_symbols_with_enough_bars(self):
        self.assertEqual(self.facts["share_above_ma5"], "0.6667")  # A、C 站回（C 等於均線）
        self.assertEqual(self.facts["share_above_ma20"], "0.6667")
        self.assertEqual(self.facts["new_low_20d_share"], "0.6667")  # B 與 C（收盤等於近 20 日最低）
        self.assertEqual(self.facts["pool_return_latest"], q6((Decimal(25) / 24 - 1 + Decimal(1) / 2 * -1) / 3))

    def test_regime_needs_61_bars(self):
        self.assertIsNone(self.facts["regime"])
        self.assertTrue(any("regime" in gap for gap in self.facts["data_gaps"]))

    def test_symbols_with_too_few_bars_are_left_out_of_the_denominator(self):
        series = {"A": list(range(1, 26)), "B": list(range(25, 0, -1))}
        facts = turn_facts({**fixture(series), "price_series": fixture(series)["price_series"] + [
            {"symbol": "D", "evidence_id": "price:D", "bars": bars([5, 6, 7, 8, 9])[-5:]}]})
        # D 只有 5 筆：MA5 分母 3 檔（A 站回、B 未站回、D 站回），MA20 分母 2 檔
        self.assertEqual(facts["share_above_ma5"], "0.6667")
        self.assertEqual(facts["share_above_ma20"], "0.5000")

    def test_no_price_series_rows_is_all_gaps(self):
        facts = turn_facts({"snapshot": {"latest_trade_date": "2026-01-01"}, "price_series": []})
        self.assertIsNone(facts["pool_return_latest"])
        self.assertIsNone(facts["share_above_ma5"])
        self.assertTrue(facts["data_gaps"])


class RegimeFormulaTests(unittest.TestCase):
    def test_all_falling_series_are_bear_and_streak_is_truncated_by_history(self):
        series = {symbol: [200 - index for index in range(70)] for symbol in ("A", "B", "C", "D", "E")}
        facts = turn_facts(fixture(series))
        self.assertEqual(facts["regime"], "bear")
        self.assertEqual(facts["breadth_above_ma20"], "0.0000")
        self.assertEqual(facts["breadth_above_ma60"], "0.0000")
        # 第 61 根（索引 60）起才有 regime：索引 60 至 69 共 10 天
        self.assertEqual(facts["bear_streak_days"], 10)
        self.assertTrue(facts["bear_streak_truncated"])
        self.assertTrue(any("截斷" in gap for gap in facts["data_gaps"]))

    def test_streak_counts_days_since_the_regime_left_bull(self):
        series = {symbol: list(range(100, 166)) + [50] * 4 for symbol in ("A", "B", "C", "D", "E")}
        facts = turn_facts(fixture(series))
        self.assertEqual(facts["regime"], "bear")
        self.assertEqual(facts["bear_streak_days"], 4)
        self.assertFalse(facts["bear_streak_truncated"])
        self.assertEqual(facts["new_low_20d_share"], "1.0000")

    def test_regime_leaving_bear_resets_the_streak_to_zero(self):
        series = {symbol: [200 - index for index in range(69)] + [500] for symbol in ("A", "B", "C", "D", "E")}
        facts = turn_facts(fixture(series))
        self.assertEqual(facts["regime"], "bull")
        self.assertEqual(facts["bear_streak_days"], 0)
        self.assertEqual(facts["share_above_ma5"], "1.0000")
        self.assertEqual(facts["up_streak_days"], 1)

    def series(self):
        result = {}
        for index in range(12):
            result["S%02d" % index] = [
                round(100 + 12 * math.sin(index + step / (3.0 + index % 5)) + step * (index - 6) * 0.05, 2) for step in range(90)
            ]
        return result

    def test_breadth_and_regime_are_the_momentum_engine_formula(self):
        series = self.series()
        facts = turn_facts(fixture(series))
        items = [
            MomentumEngine._calculate_symbol(symbol, {"evidence_id": "price:" + symbol, "bars": bars(values)})
            for symbol, values in sorted(series.items())
        ]
        available = [item for item in items if item["status"] == "available"]
        expected = MomentumEngine._regime(available, len(available) / len(items))
        self.assertEqual(facts["regime"], expected["regime"])
        self.assertEqual(facts["breadth_above_ma20"], str(Decimal(str(expected["breadth_above_ma20"])).quantize(Decimal("0.0001"), ROUND_HALF_UP)))
        self.assertEqual(facts["breadth_above_ma60"], str(Decimal(str(expected["breadth_above_ma60"])).quantize(Decimal("0.0001"), ROUND_HALF_UP)))

    def test_b20_change_is_today_minus_five_trading_days_ago(self):
        series = self.series()
        now = turn_facts(fixture(series))
        earlier = turn_facts(fixture({symbol: values[:-5] for symbol, values in series.items()}))
        self.assertEqual(now["breadth_above_ma20_5d_ago"], earlier["breadth_above_ma20"])
        change = Decimal(now["breadth_above_ma20"]) - Decimal(earlier["breadth_above_ma20"])
        self.assertAlmostEqual(float(now["b20_change_5d"]), float(change), delta=0.0002)

    def test_bear_streak_matches_truncated_replays(self):
        series = {symbol: list(range(100, 166)) + [50] * 4 for symbol in ("A", "B", "C", "D", "E")}
        for drop, expected in ((0, 4), (1, 3), (2, 2), (3, 1), (4, 0)):
            facts = turn_facts(fixture({symbol: values[: len(values) - drop] for symbol, values in series.items()}))
            self.assertEqual(facts["bear_streak_days"], expected, drop)

    def test_pool_history_is_point_in_time(self):
        pool = _Pool(fixture({"A": list(range(1, 11))}), dates(10)[-1])
        self.assertEqual(len(pool.history("A", dates(10)[4])), 5)


class CutoffTests(unittest.TestCase):
    def test_bar_after_the_latest_trade_date_is_rejected(self):
        value = fixture({"A": [10, 11, 12], "B": [10, 10, 10]})
        value["snapshot"]["latest_trade_date"] = dates(3)[1]
        with self.assertRaisesRegex(DecisionToolError, "晚於 snapshot.latest_trade_date"):
            turn_facts(value)

    def test_missing_trade_date_or_series_is_rejected(self):
        value = fixture({"A": [10, 11]})
        del value["snapshot"]["latest_trade_date"]
        with self.assertRaises(DecisionToolError):
            turn_facts(value)
        with self.assertRaises(DecisionToolError):
            turn_facts({"snapshot": {"latest_trade_date": "2026-01-01"}})

    def test_stale_last_bar_is_a_data_gap(self):
        value = fixture({"A": [10, 11, 12]})
        value["snapshot"]["latest_trade_date"] = "2026-12-31"
        facts = turn_facts(value)
        self.assertTrue(any("不是 snapshot.latest_trade_date" in gap for gap in facts["data_gaps"]))

    def test_validate_cash_stance_surfaces_a_cutoff_violation(self):
        bundle = overnight_bundle()
        tampered = copy.deepcopy(bundle)
        tampered["snapshot"]["latest_trade_date"] = "2000-01-01"
        errors = []
        context = DecisionContext(bundle)
        context.bundle = tampered
        validate_cash_stance(context, stance(), "cash_stance", errors, turn_facts_gate=GATE)
        self.assertTrue(any("turn_facts" in error for error in errors), errors)


class ArchivedRunTests(unittest.TestCase):
    @unittest.skipUnless((ARCHIVE / "decision_input.json").exists() and (ARCHIVE / "momentum.json").exists(), "沒有 11-09 07:00 封存")
    def test_2026_11_06_close_matches_the_archived_momentum(self):
        bundle = json.loads((ARCHIVE / "decision_input.json").read_text(encoding="utf-8"))
        regime = json.loads((ARCHIVE / "momentum.json").read_text(encoding="utf-8"))["regime_assessment"]
        facts = turn_facts(bundle)
        self.assertEqual(facts["as_of"], "2026-11-06")
        self.assertEqual(facts["pool_size"], 150)
        self.assertAlmostEqual(float(facts["share_above_ma5"]), 0.03, delta=0.01)
        self.assertAlmostEqual(float(facts["share_above_ma20"]), 0.05, delta=0.01)
        self.assertAlmostEqual(float(facts["breadth_above_ma60"]), 0.11, delta=0.01)
        self.assertAlmostEqual(float(facts["new_low_20d_share"]), 0.83, delta=0.03)
        self.assertEqual(facts["breadth_above_ma20"], str(Decimal(str(regime["breadth_above_ma20"])).quantize(Decimal("0.0001"), ROUND_HALF_UP)))
        self.assertEqual(facts["breadth_above_ma60"], str(Decimal(str(regime["breadth_above_ma60"])).quantize(Decimal("0.0001"), ROUND_HALF_UP)))
        self.assertEqual(facts["regime"], regime["regime"])
        self.assertEqual(facts["up_streak_days"], 0)
        self.assertEqual(facts["data_gaps"], [])


class TurnAssessmentValidationTests(unittest.TestCase):
    def setUp(self):
        self.bundle = overnight_bundle()

    def test_valid_stance_passes_and_field_is_required_under_the_gate(self):
        for value in TURN_ASSESSMENTS:
            self.assertEqual(errors_of(self.bundle, stance(value)), [], value)
        missing = stance()
        del missing["turn_assessment"]
        self.assertTrue(any("turn_assessment" in error for error in errors_of(self.bundle, missing)))

    def test_values_outside_the_enum_are_rejected(self):
        for bad in ("confirmed", "", None, 1, True, ["turn_confirmed"]):
            self.assertTrue(errors_of(self.bundle, stance(bad)), bad)
        self.assertEqual(set(TURN_ASSESSMENTS), {"no_turn_evidence", "turn_unconfirmed", "turn_confirmed"})

    def test_no_correspondence_with_the_stance_level_is_required(self):
        for level in ("aggressive", "neutral", "defensive"):
            for value in TURN_ASSESSMENTS:
                self.assertEqual(errors_of(self.bundle, stance(value, level=level)), [], (level, value))

    def test_gate_requires_typed_evidence(self):
        errors = errors_of(overnight_bundle(typed=False), stance())
        self.assertTrue(any("cash_stance_turn_facts" in error and "typed_evidence_v1" in error for error in errors), errors)

    def test_without_gate_the_field_is_rejected_and_validation_is_unchanged(self):
        errors = errors_of(self.bundle, stance(), gate=None)
        self.assertTrue(any("未允許欄位" in error and "turn_assessment" in error for error in errors), errors)
        plain = {k: v for k, v in stance().items() if k != "turn_assessment"}
        self.assertEqual(errors_of(self.bundle, plain, gate=None), [])

    def test_independent_of_the_recency_gate(self):
        both = []
        validate_cash_stance(DecisionContext(self.bundle), stance(), "cash_stance", both, recency_gate=RECENCY_GATE, turn_facts_gate=GATE)
        self.assertTrue(any("recency_reconciliation" in error for error in both), both)
        self.assertFalse(any("turn_assessment" in error for error in both), both)
        alone = errors_of(self.bundle, stance())
        self.assertEqual(alone, [])


class SchemaAndBriefTests(unittest.TestCase):
    def test_schema_adds_only_the_enum(self):
        for graded, base in ((False, CASH_STANCE_SCHEMA), (True, CASH_GRADE_STANCE_SCHEMA)):
            schema = cash_stance_turn_facts_schema(graded)
            self.assertEqual(schema["properties"]["turn_assessment"], {"enum": list(TURN_ASSESSMENTS)})
            self.assertEqual(schema["required"], base["required"] + ["turn_assessment"])
            self.assertEqual({k: v for k, v in schema["properties"].items() if k != "turn_assessment"}, base["properties"])
            self.assertNotIn("turn_assessment", base["properties"])
            both = cash_stance_turn_facts_schema(graded, recency=True)
            self.assertEqual(both["required"], cash_stance_recency_schema(graded)["required"] + ["turn_assessment"])
            self.assertIn("recency_reconciliation", both["properties"])

    def run_stance(self, bundle, outputs, gate, recency=None):
        momentum = MomentumEngine(bundle).run()
        reports = typed_reports(bundle)
        runner = FakeRunner({"cash_stance": outputs})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            kwargs = {}
            if gate is not None:
                kwargs["cash_stance_turn_facts"] = gate
            if recency is not None:
                kwargs["cash_stance_recency"] = recency
            try:
                result = pipeline.run_cash_stance(bundle, momentum, reports, **kwargs)
            except DailyPipelineError as error:
                result = error
            brief = json.loads((Path(directory) / "brief_cash_stance.json").read_text(encoding="utf-8")) \
                if (Path(directory) / "brief_cash_stance.json").exists() else None
        return result, brief, runner

    def test_brief_puts_turn_facts_next_to_regime_and_prompt_mentions_it(self):
        bundle = overnight_bundle()
        good = stance()
        result, brief, runner = self.run_stance(bundle, [good], GATE)
        self.assertEqual(result, good)
        keys = list(brief)
        self.assertEqual(keys[keys.index("regime_assessment") + 1], "turn_facts")
        self.assertEqual(brief["turn_facts"], turn_facts(bundle))
        self.assertIn("turn_facts", runner.prompts[0][1])
        self.assertIn("turn_assessment", runner.prompts[0][1])

    def test_without_gate_brief_and_prompt_are_unchanged(self):
        bundle = overnight_bundle()
        plain = {k: v for k, v in stance().items() if k != "turn_assessment"}
        result, brief, runner = self.run_stance(bundle, [plain], None)
        self.assertEqual(result, plain)
        self.assertNotIn("turn_facts", brief)
        self.assertNotIn("turn_facts", runner.prompts[0][1])
        self.assertNotIn("turn_assessment", runner.prompts[0][1])

    def test_invalid_output_is_corrected_through_the_retry_loop(self):
        bundle = overnight_bundle()
        result, _, runner = self.run_stance(bundle, [stance("confirmed"), stance("turn_confirmed")], GATE)
        self.assertEqual(result["turn_assessment"], "turn_confirmed")
        self.assertEqual(len(runner.prompts), 2)
        self.assertIn("turn_assessment", runner.prompts[1][1])

    def test_gate_on_untyped_bundle_stops_before_asking_the_agent(self):
        bundle = overnight_bundle(typed=False)
        momentum = MomentumEngine(bundle).run()
        runner = FakeRunner({"cash_stance": []})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            with self.assertRaisesRegex(DailyPipelineError, "typed_evidence_v1"):
                pipeline.run_cash_stance(bundle, momentum, typed_reports(typed_bundle()), cash_stance_turn_facts=GATE)
        self.assertEqual(runner.prompts, [])


class FinalizerWiringTests(unittest.TestCase):
    def world(self):
        from test_team_chain import team_world

        return team_world()

    def finalize(self, bundle, momentum, debate, trade, policy, team):
        from test_team_chain import finalize

        return finalize(bundle, momentum, debate, trade, policy, team)[1]

    def test_policy_gate_reaches_the_finalizer(self):
        from etf_agent.decision import build_team_inputs, decision_policy_sha256
        from test_trader import STANCE as TEAM_STANCE

        bundle, momentum, debate, trade, policy, _, reports, research = self.world()
        gated = copy.deepcopy(policy)
        gated["position_sizing"]["cash_stance_turn_facts"] = dict(GATE)
        gated["content_sha256"] = decision_policy_sha256(gated)
        team = build_team_inputs(reports, research, dict(TEAM_STANCE, turn_assessment="turn_unconfirmed"))
        result = self.finalize(bundle, momentum, debate, trade, gated, team)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("typed_evidence_v1" in error for error in result["errors"]), result["errors"])

    def test_field_without_policy_gate_is_rejected_by_the_finalizer(self):
        from etf_agent.decision import build_team_inputs
        from test_trader import STANCE as TEAM_STANCE

        bundle, momentum, debate, trade, policy, _, reports, research = self.world()
        team = build_team_inputs(reports, research, dict(TEAM_STANCE, turn_assessment="turn_unconfirmed"))
        result = self.finalize(bundle, momentum, debate, trade, policy, team)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("turn_assessment" in error for error in result["errors"]), result["errors"])

    def test_without_gate_or_field_the_finalizer_still_approves(self):
        bundle, momentum, debate, trade, policy, team, _, _ = self.world()
        result = self.finalize(bundle, momentum, debate, trade, policy, team)
        self.assertEqual(result["status"], "approved", result["errors"])


class TradeBindingAndReplayTests(unittest.TestCase):
    def policy_with_gate(self):
        from test_portfolio_risk_decision import policy as base_policy
        from test_position_sizing import SIZING

        base = base_policy()
        base["position_sizing"] = {**copy.deepcopy(SIZING), "cash_stance_turn_facts": dict(GATE)}
        return base

    def test_apply_trade_decision_enforces_the_enum(self):
        from etf_agent.decision import apply_trade_decision

        bundle = overnight_bundle()
        decision = {"decision_id": "trade-1", "content_sha256": "x" * 64, "items": []}
        base = self.policy_with_gate()
        plain = {k: v for k, v in stance().items() if k != "turn_assessment"}
        with self.assertRaisesRegex(DecisionToolError, "turn_assessment"):
            apply_trade_decision(base, decision, plain, bundle)
        with self.assertRaisesRegex(DecisionToolError, "turn_assessment"):
            apply_trade_decision(base, decision, stance("bounce"), bundle)
        bound = apply_trade_decision(base, decision, stance(), bundle)
        self.assertEqual(bound["position_sizing"]["cash_stance"], "neutral")
        self.assertEqual(bound["position_sizing"]["cash_stance_turn_facts"], GATE)

    def test_without_gate_trade_binding_rejects_the_field(self):
        from etf_agent.decision import apply_trade_decision
        from test_portfolio_risk_decision import policy as base_policy
        from test_position_sizing import SIZING

        bundle = overnight_bundle()
        base = base_policy()
        base["position_sizing"] = copy.deepcopy(SIZING)
        decision = {"decision_id": "trade-1", "content_sha256": "x" * 64, "items": []}
        with self.assertRaisesRegex(DecisionToolError, "未允許欄位"):
            apply_trade_decision(base, decision, stance(), bundle)

    def test_replay_ignores_the_gate_and_the_field_without_touching_legacy_notes(self):
        from etf_agent.evaluation.replay import bind_replay_policy
        from test_cash_ratio_range import STANCE as LEGACY_STANCE, chain_inputs
        from test_daily_pipeline import gateless_policy_template

        bundle, _, debate, intent, archived, _, _, _ = chain_inputs()
        template = gateless_policy_template()
        template.update(policy_id="replay-A", available_at="2026-09-19T00:00:00+00:00")
        _, plain_notes = bind_replay_policy(template, bundle, archived, intent, debate, dict(LEGACY_STANCE), [])
        self.assertNotIn("cash_stance_turn_facts", plain_notes)
        gated = copy.deepcopy(template)
        gated["position_sizing"]["cash_stance_turn_facts"] = dict(GATE)
        policy, notes = bind_replay_policy(
            gated, bundle, archived, intent, debate, dict(LEGACY_STANCE, turn_assessment="turn_confirmed"), [])
        self.assertEqual(notes["cash_stance_turn_facts"], "ignored_in_replay")
        self.assertNotIn("cash_stance_turn_facts", policy["position_sizing"])


if __name__ == "__main__":
    unittest.main()
