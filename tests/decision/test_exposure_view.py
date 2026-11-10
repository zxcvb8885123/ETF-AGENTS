"""position_sizing.exposure_view v1（Agent 主導曝險階段 1：事實與紀錄）的測試。

覆蓋（docs/agent_led_exposure_plan.md 第 6 節）：

1. 事實重算（合成行情）：組合曝險與 exposure_review 同口徑、產業權重（金融保險、缺代碼歸 unknown）、上漲日池報酬與
   turn_facts 同一個數字、持股當日貢獻＝Σ 起始權重 × 當日報酬、非持股 beta；bar 晚於最近交易日、可得時間晚於 cutoff 或缺時區
   一律拒絕；缺行情為 null 不補值；交易池沒有上漲日時該欄不可用；任一欄被改即拒絕。
2. 閘門：形狀、前提、人專用（策略反思不得改）。
3. 列舉：``exposure_view`` 必填與值域、非 keep_defensive 缺市場層級證據以專屬訊息拒絕、沒有閘門時帶欄位即拒絕、
   背景識別碼出現在理由即拒絕。
4. 每日 pipeline（正式樣板＋agent_swap v1 影子）：風險 Agent 簡報與提名簡報多附事實（背景、不可引用）、風險簡報不含交易
   Agent 的意圖與信心統計、多空研究員看不到；team_inputs 版本不變、多一個選配鍵；真實 Finalizer 重算、竄改／缺漏／無閘門即拒絕；
   配置輸出（訂單、配置、旗標）與沒有閘門時逐位元相同；沒有閘門時簡報、提示與 team_inputs 逐鍵不變。
5. 封存 run（本機才有，CI 沒有時略過）：2026-11-10 唯讀重算與計畫第 5.1 節數字比對（replay_reference_only）；沒有閘門時
   team_inputs 重建與 team_inputs_sha256 不變；開啟閘門時配置逐位元相同。

合成行情是機制測試，不是對真實行情的驗證；封存案例的證據等級是 replay_reference_only。
"""

import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Context, Decimal, localcontext
from pathlib import Path

from etf_agent.automation.agent_runner import LATER_STAGE_GLOBS
from etf_agent.automation.agent_schemas import CASH_STANCE_SCHEMA, exposure_view_schema
from etf_agent.core import canonical_sha256
from etf_agent.decision import (
    AllocationOrderEngine, DecisionFinalizer, DecisionToolError, build_exposure_review, decision_policy_sha256,
)
from etf_agent.decision.contracts import DecisionContext
from etf_agent.decision.exposure_facts import (
    EXPOSURE_VIEWS,
    FACTS_FIELDS,
    build_exposure_facts,
    exposure_reference_tokens,
    exposure_view_gate,
    nomination_exposure_brief,
    risk_exposure_brief,
    validate_exposure_facts,
    validate_exposure_view_gate,
)
from etf_agent.decision.sizing import GATE_FIELDS, POSITION_SIZING_FIELDS, validate_cash_stance, validate_gate_prerequisites
from etf_agent.decision.turn_facts import turn_facts
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

import test_swap_nomination_pipeline as swap
from test_daily_pipeline import FakeRunner, STANCE

ROOT = Path(__file__).resolve().parents[2]
GATE = {"version": "v1"}
TRADE_DATE = "2026-11-09"
CUTOFF = "2026-11-09T23:00:00+00:00"
EXPOSURE_GATE = {
    "version": "v3", "beta_window_days": 20, "beta_target_by_stance": {"aggressive": "0.8", "neutral": "0.6", "defensive": None},
    "max_swaps_per_run": 2, "min_holding_days": 5, "hold_conviction": "v1",
}
# A、B 隨市場、C 反向、D 幾乎不動；持有 A、C、D，B 是非持股。A、C 是金融保險，D 沒有產業代碼。
MULTIPLIER = {"A.TW": Decimal("1"), "B.TW": Decimal("2"), "C.TW": Decimal("-0.5"), "D.TW": Decimal("0.1")}
POSITIONS = {"A.TW": 100, "C.TW": 200, "D.TW": 300}
SECTORS = {"A.TW": "industry:17", "B.TW": "industry:24", "C.TW": "industry:17"}
ARCHIVED = ROOT / "artifacts" / "decisions" / "2026-11-10" / "decision-20261109T230435Z"
DECISIONS = ROOT / "artifacts" / "decisions"


def weekdays(end, count):
    days, day = [], date.fromisoformat(end)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    return sorted(days)


def synthetic_bundle(days=40, last=None, falling=False):
    """合成行情：市場日報酬 m_t，各股報酬 = 乘數 × m_t；``last`` 覆寫最近交易日各股報酬；``falling`` 讓每天都下跌（沒有上漲日）。"""
    dates = weekdays(TRADE_DATE, days)
    closes = {symbol: [Decimal(100)] for symbol in MULTIPLIER}
    for index in range(1, days):
        market = Decimal("0.01") * Decimal((index % 3) - 1) + Decimal("0.002")
        for symbol, multiplier in MULTIPLIER.items():
            change = multiplier * market
            if falling:
                change = -Decimal("0.004") - Decimal("0.002") * Decimal(index % 2) * multiplier.copy_abs()
            if last is not None and index == days - 1:
                change = Decimal(last[symbol])
            closes[symbol].append(closes[symbol][-1] * (Decimal(1) + change))
    series, latest = [], []
    for symbol in MULTIPLIER:
        bars = [{"trade_date": day, "close": str(close), "available_at": "%sT14:00:00+08:00" % day}
                for day, close in zip(dates, closes[symbol])]
        series.append({"symbol": symbol, "evidence_id": "price:%s" % symbol, "bars": bars})
        latest.append({"symbol": symbol, "analysis_close_price": str(closes[symbol][-1]), "source_evidence_id": "price:%s" % symbol})
    return {
        "decision_cutoff": CUTOFF,
        "evidence_policy": "typed_evidence_v1",
        "snapshot": {"latest_trade_date": TRADE_DATE, "latest_prices": latest, "documents": []},
        "price_series": series,
        "account_snapshot": {
            "cash": "20000", "positions": [{"symbol": symbol, "shares": shares} for symbol, shares in POSITIONS.items()],
        },
    }


def synthetic_policy(gate=GATE):
    sizing = {"exposure_rotation": dict(EXPOSURE_GATE)}
    if gate is not None:
        sizing["exposure_view"] = dict(gate)
    return {"position_sizing": sizing, "sector_by_symbol": dict(SECTORS)}


LAST_DAY = {"A.TW": "0.05", "B.TW": "0.08", "C.TW": "-0.02", "D.TW": "0.01"}


def holding(facts, symbol):
    return next(row for row in facts["holdings"] if row["symbol"] == symbol)


# ---------------------------------------------------------------- 1. 事實重算
class FactsTests(unittest.TestCase):
    def setUp(self):
        self.bundle = synthetic_bundle(last=LAST_DAY)
        self.policy = synthetic_policy()
        self.facts = build_exposure_facts(self.bundle, self.policy)

    def test_portfolio_is_the_exposure_review_portfolio_plus_counts(self):
        review = build_exposure_review(self.bundle, self.policy)
        portfolio = self.facts["portfolio"]
        for key, value in review["portfolio"].items():
            self.assertEqual(portfolio[key], value, key)
        self.assertEqual(portfolio["held_count"], 3)
        low = [item for item in review["items"] if item["beta"] is not None and Decimal(item["beta"]) < Decimal("0.3")]
        self.assertEqual(portfolio["low_beta_count"], len(low))
        self.assertEqual(portfolio["beta_window_days"], 20)
        for row in review["items"]:
            for key in ("weight", "beta", "up_capture", "down_capture"):
                self.assertEqual(holding(self.facts, row["symbol"])[key], row[key])
        self.assertEqual(set(self.facts), FACTS_FIELDS)
        self.assertTrue(self.facts["facts_id"].startswith("exposure-facts:"))

    def test_sector_weights_group_financials_and_mark_missing_codes_unknown(self):
        sectors = self.facts["sectors"]
        weights = {row["symbol"]: Decimal(row["weight"]) for row in self.facts["holdings"]}
        financial = sectors["financial_insurance"]
        self.assertEqual((financial["sector"], financial["count"], financial["symbols"]), ("industry:17", 2, ["A.TW", "C.TW"]))
        self.assertAlmostEqual(Decimal(financial["weight"]), weights["A.TW"] + weights["C.TW"], delta=Decimal("0.000002"))
        self.assertEqual(sectors["unknown_symbols"], ["D.TW"])
        self.assertEqual(holding(self.facts, "D.TW")["sector"], "unknown")
        ordered = [Decimal(row["weight"]) for row in sectors["items"]]
        self.assertEqual(ordered, sorted(ordered, reverse=True))

    def test_up_day_uses_the_turn_facts_pool_return_and_start_weights(self):
        up = self.facts["up_day"]
        self.assertEqual((up["status"], up["date"], up["is_latest_trade_date"], up["trading_days_ago"]), ("available", TRADE_DATE, True, 0))
        self.assertEqual(up["pool_return"], turn_facts(self.bundle)["pool_return_latest"])
        self.assertEqual(Decimal(up["pool_return"]), sum(Decimal(v) for v in LAST_DAY.values()) / 4)
        expected = sum(Decimal(holding(self.facts, s)["weight"]) * Decimal(LAST_DAY[s]) for s in POSITIONS)
        self.assertAlmostEqual(Decimal(up["holdings_contribution"]), expected, delta=Decimal("0.000003"))
        for symbol in POSITIONS:
            self.assertEqual(Decimal(holding(self.facts, symbol)["up_day_return"]), Decimal(LAST_DAY[symbol]))
        self.assertEqual(up["weight_basis"], "decision_start_weights")

    def test_latest_down_day_points_back_to_the_previous_up_day(self):
        down = dict(LAST_DAY, **{"A.TW": "-0.03", "B.TW": "-0.03", "C.TW": "-0.03", "D.TW": "-0.03"})
        facts = build_exposure_facts(synthetic_bundle(last=down), self.policy)
        up = facts["up_day"]
        self.assertEqual(up["status"], "available")
        self.assertFalse(up["is_latest_trade_date"])
        self.assertGreater(up["trading_days_ago"], 0)
        self.assertGreater(Decimal(up["pool_return"]), 0)

    def test_no_up_day_in_the_lookback_makes_the_field_unavailable(self):
        facts = build_exposure_facts(synthetic_bundle(falling=True), self.policy)
        up = facts["up_day"]
        self.assertEqual(up["status"], "unavailable")
        for key in ("date", "pool_return", "holdings_contribution"):
            self.assertIsNone(up[key], key)
        self.assertTrue(all(row["up_day_return"] is None for row in facts["holdings"]))
        self.assertTrue(any("沒有上漲日" in gap for gap in facts["data_gaps"]))

    def test_missing_bar_is_null_and_the_contribution_is_not_treated_as_zero(self):
        bundle = synthetic_bundle(last=LAST_DAY)
        row = next(r for r in bundle["price_series"] if r["symbol"] == "D.TW")
        row["bars"] = [bar for bar in row["bars"] if bar["trade_date"] != weekdays(TRADE_DATE, 2)[0]]  # 拿掉前一日
        facts = build_exposure_facts(bundle, self.policy)
        self.assertIsNone(holding(facts, "D.TW")["up_day_return"])
        self.assertIsNone(facts["up_day"]["holdings_contribution"])
        self.assertEqual(facts["up_day"]["missing_symbols"], ["D.TW"])
        self.assertIsNotNone(holding(facts, "A.TW")["up_day_return"])

    def test_non_held_betas_cover_only_symbols_not_held(self):
        rows = {row["symbol"]: row for row in self.facts["non_held"]}
        self.assertEqual(set(rows), {"B.TW"})
        self.assertEqual(rows["B.TW"]["sector"], "industry:24")
        self.assertGreater(Decimal(rows["B.TW"]["beta"]), Decimal(holding(self.facts, "A.TW")["beta"]))

    def test_late_bars_late_availability_and_missing_timezone_are_rejected(self):
        cases = {
            "bar 晚於最近交易日": lambda b: b["price_series"][0]["bars"].append(
                {"trade_date": "2026-11-10", "close": "100", "available_at": "2026-11-10T14:00:00+08:00"}),
            "可得時間晚於 cutoff": lambda b: b["price_series"][1]["bars"][-1].update(available_at="2026-11-10T08:00:00+08:00"),
            "可得時間缺時區": lambda b: b["price_series"][2]["bars"][-1].update(available_at="2026-11-09T14:00:00"),
            "cutoff 缺時區": lambda b: b.update(decision_cutoff="2026-11-09T23:00:00"),
        }
        for label, mutate in cases.items():
            bundle = synthetic_bundle(last=LAST_DAY)
            mutate(bundle)
            with self.subTest(label), self.assertRaises(DecisionToolError):
                build_exposure_facts(bundle, self.policy)

    def test_recompute_rejects_any_tampered_missing_or_gateless_facts(self):
        def check(team, policy=None):
            errors = []
            validate_exposure_facts(self.bundle, policy or self.policy, team, errors)
            return errors

        team = {"exposure_facts": self.facts}
        self.assertEqual(check(team), [])
        cases = {
            "組合 beta 被改": lambda f: f["portfolio"].update(portfolio_beta="0.900000"),
            "金融占比被改": lambda f: f["sectors"]["financial_insurance"].update(weight="0.100000"),
            "上漲日貢獻被改": lambda f: f["up_day"].update(holdings_contribution="0.050000"),
            "持股當日報酬被改": lambda f: f["holdings"][0].update(up_day_return="0.200000"),
            "新股 beta 被改": lambda f: f["non_held"][0].update(beta="3.000000"),
            "多出欄位": lambda f: f.update(signal="rebuild"),
            "facts_id 被改": lambda f: f.update(facts_id="exposure-facts:0"),
        }
        for label, mutate in cases.items():
            tampered = copy.deepcopy(self.facts)
            mutate(tampered)
            with self.subTest(label):
                self.assertTrue(check({"exposure_facts": tampered}))
        self.assertTrue(check({}))
        self.assertTrue(check(team, synthetic_policy(gate=None)))

    def test_facts_do_not_depend_on_the_trade_decision_or_bound_fields(self):
        bound = synthetic_policy()
        bound["position_sizing"].update(cash_stance="defensive", conviction_by_symbol={"B.TW": "high"})
        self.assertEqual(build_exposure_facts(self.bundle, bound), self.facts)

    def test_briefs_are_background_without_ids_and_only_list_candidate_betas(self):
        risk = risk_exposure_brief(self.facts)
        self.assertIs(risk["citable"], False)
        text = json.dumps(risk, ensure_ascii=False)
        for token in (self.facts["facts_id"], '"evidence_ids"', "non_held", "B.TW"):
            self.assertNotIn(token, text)
        nomination = nomination_exposure_brief(self.facts, ["B.TW"], {"version": "v1"}, {"version": "v1"}, {"version": "v2"})
        self.assertIs(nomination["citable"], False)
        self.assertEqual(set(nomination["candidate_betas"]), {"B.TW"})
        self.assertEqual(nomination["turn_state"]["citable"], False)
        self.assertNotIn(self.facts["facts_id"], json.dumps(nomination))
        self.assertEqual(set(nomination_exposure_brief(self.facts, [], {}, {})["candidate_betas"]), set())
        self.assertIn("不得純粹為了買別檔而賣", nomination["note"])


# ---------------------------------------------------------------- 2. 閘門
class GateTests(unittest.TestCase):
    def test_gate_is_registered_human_only_and_needs_its_prerequisites(self):
        self.assertIn("exposure_view", GATE_FIELDS)
        self.assertIn("exposure_view", POSITION_SIZING_FIELDS)
        self.assertIn("exposure_view", FORBIDDEN_PREFIXES)
        full = {"exposure_view": GATE, "exposure_rotation": {}, "cash_stance_turn_facts": {}, "market_index_facts": {}}
        errors = []
        validate_gate_prerequisites(full, errors)
        self.assertEqual([e for e in errors if "exposure_view" in e], [])
        for missing in ("exposure_rotation", "cash_stance_turn_facts", "market_index_facts"):
            sizing = {key: value for key, value in full.items() if key != missing}
            errors = []
            validate_gate_prerequisites(sizing, errors)
            self.assertTrue(any("exposure_view 必須與 %s 同時存在" % missing in e for e in errors), missing)

    def test_shape_is_closed(self):
        for gate in ({"version": "v2"}, {"version": "v1", "caps": 2}, "v1"):
            errors = []
            validate_exposure_view_gate({"exposure_view": gate}, errors)
            self.assertTrue(errors, gate)
        self.assertIsNone(exposure_view_gate({}))
        self.assertIsNone(exposure_view_gate(None))
        with self.assertRaises(DecisionToolError):
            build_exposure_facts(synthetic_bundle(), synthetic_policy(gate=None))

    def test_schema_adds_only_the_enum(self):
        schema = exposure_view_schema(CASH_STANCE_SCHEMA)
        self.assertEqual(schema["properties"]["exposure_view"], {"enum": list(EXPOSURE_VIEWS)})
        self.assertIn("exposure_view", schema["required"])
        self.assertNotIn("exposure_view", json.dumps(CASH_STANCE_SCHEMA))
        self.assertIn("exposure_facts*", LATER_STAGE_GLOBS)


class ReplayTests(unittest.TestCase):
    def test_replay_ignores_the_gate_strips_the_enum_and_allocates_the_same(self):
        import test_exposure_rotation_e2e as e2e
        from test_daily_pipeline import live_policy_template

        helper = e2e.ReplayTests()
        helper.setUp()
        plain = helper.template(live_policy_template)
        plain["position_sizing"].pop("exposure_view", None)  # 正式樣板已開啟；「沒有閘門」的對照組要先拿掉
        plain["position_sizing"].get("agent_swap", {}).pop("exposure", None)  # 子閘門依賴 exposure_view，要一併拿掉
        gated = copy.deepcopy(plain)
        gated["position_sizing"]["exposure_view"] = dict(GATE)
        plain_policy, plain_notes = helper.replay(plain)
        stance = dict(e2e.LEGACY_STANCE, exposure_view="rebuild")
        policy, notes = e2e.bind_replay_policy(gated, helper.bundle, helper.archived, helper.intent, helper.debate, stance, [])
        self.assertEqual(notes.pop("exposure_view"), "ignored_in_replay")
        self.assertEqual({k: v for k, v in notes.items() if k not in ("policy_id", "content_sha256")},
                         {k: v for k, v in plain_notes.items() if k not in ("policy_id", "content_sha256")})
        self.assertNotIn("exposure_view", plain_notes)
        self.assertEqual(policy["position_sizing"], plain_policy["position_sizing"])
        self.assertEqual(policy["content_sha256"], plain_policy["content_sha256"])


# ---------------------------------------------------------------- 3、4. 每日 pipeline 與真實 Finalizer
class GatedHarness(swap.Harness):
    """換股提名的影子夾具（正式樣板＋agent_swap v1）；``view=True`` 時在 PipelineWorld 寫好的樣板上加 exposure_view，``view=False`` 時拿掉（正式樣板已開啟）。"""

    def __init__(self, directory, view=True):
        super().__init__(directory)
        path = self.world.paths["template"]
        template = json.loads(path.read_text(encoding="utf-8"))
        if view:
            template["position_sizing"]["exposure_view"] = dict(GATE)
        else:
            template["position_sizing"].pop("exposure_view", None)  # 正式樣板已開啟；對照組明確拿掉
        path.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")


def view_outputs(view="start_rebuild"):
    value = swap.outputs(swap.NOMINATION)
    value["cash_stance"] = [dict(value["cash_stance"][0], exposure_view=view)]
    return value


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        (base / "plain").mkdir()
        (base / "gated").mkdir()
        cls.plain = GatedHarness(base / "plain", view=False)
        cls.plain_runner = FakeRunner(swap.outputs(swap.NOMINATION))
        cls.plain_result, _ = cls.plain.run(cls.plain_runner)
        cls.gated = GatedHarness(base / "gated")
        cls.runner = FakeRunner(view_outputs())
        cls.result, _ = cls.gated.run(cls.runner)
        cls.plain_art = cls.plain.artifacts()
        cls.art = cls.gated.artifacts()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def load(self, harness, name):
        return json.loads((harness.world.run_dir / ("%s.json" % name)).read_text(encoding="utf-8"))

    def test_gated_run_completes_and_seals_the_facts_without_a_version_bump(self):
        self.assertEqual(self.result.status, "completed", self.result.errors)
        self.assertEqual(self.plain_result.status, "completed", self.plain_result.errors)
        team = self.art["team_inputs"]
        self.assertEqual(team["schema_version"], self.plain_art["team_inputs"]["schema_version"])
        self.assertEqual(set(team) - set(self.plain_art["team_inputs"]), {"exposure_facts"})
        self.assertEqual(team["exposure_facts"], self.load(self.gated, "exposure_facts"))
        self.assertEqual(team["cash_stance"]["exposure_view"], "start_rebuild")
        self.assertEqual(self.art["decision"]["errors"], [])
        self.assertEqual(self.gated.validate(self.art), [])

    def test_risk_brief_has_background_facts_but_no_trade_intent_statistics(self):
        brief = self.load(self.gated, "brief_cash_stance")
        facts = brief["exposure_facts"]
        self.assertIs(facts["citable"], False)
        self.assertEqual(facts["portfolio"], self.art["team_inputs"]["exposure_facts"]["portfolio"])
        text = json.dumps(brief, ensure_ascii=False)
        self.assertNotIn(self.art["team_inputs"]["exposure_facts"]["facts_id"], text)
        for token in ("intent", "conviction", "non_held", "candidate_betas"):
            self.assertNotIn(token, text, token)
        prompt = dict(self.runner.prompts)["cash_stance"]
        self.assertIn("exposure_view", prompt)
        self.assertIn("刻意不附交易 Agent 的意圖與信心統計", prompt)
        plain = self.load(self.plain, "brief_cash_stance")
        self.assertEqual({k: v for k, v in brief.items() if k != "exposure_facts"}, plain)

    def test_nomination_brief_has_turn_index_exposure_up_day_and_candidate_betas(self):
        brief = self.load(self.gated, "brief_swap_nomination")
        block = brief["exposure_facts"]
        self.assertIs(block["citable"], False)
        for key in ("turn_facts", "market_index_facts", "portfolio", "sectors", "up_day", "holdings", "candidate_betas", "turn_state"):
            self.assertIn(key, block)
        self.assertIn("2317.TW", block["candidate_betas"])
        self.assertIn("2330.TW", block["holdings"])
        plain = self.load(self.plain, "brief_swap_nomination")
        self.assertEqual({k: v for k, v in brief.items() if k != "exposure_facts"}, plain)
        prompts = dict(self.runner.prompts)
        self.assertIn("exposure_facts", prompts["swap_nominator"])
        self.assertNotIn("exposure_facts", dict(self.plain_runner.prompts)["swap_nominator"])
        # 提名的封存與驗證規則不變：同一份提名在兩邊都被接受、內容相同。
        self.assertEqual(self.art["team_inputs"]["swap_nomination"]["nominations"],
                         self.plain_art["team_inputs"]["swap_nomination"]["nominations"])

    def test_researchers_never_see_the_facts(self):
        for name, prompt in self.runner.prompts:
            if name.startswith(("bull", "bear", "technical", "fundamental", "event", "trader")):
                self.assertNotIn("exposure_facts", prompt, name)
        for role in ("bull_b0", "bear_b0", "trader_b0"):
            self.assertNotIn("exposure_facts", json.dumps(self.load(self.gated, "brief_%s" % role), ensure_ascii=False))

    def test_allocation_is_bit_for_bit_unchanged_by_the_gate(self):
        gated, plain = self.art, self.plain_art
        self.assertEqual(gated["decision"]["status"], plain["decision"]["status"])
        self.assertEqual(gated["decision"]["orders"], plain["decision"]["orders"])
        for key in ("allocation_proposal", "order_proposal", "constraint_flags"):
            self.assertEqual(gated["proposal"][key], plain["proposal"][key], key)
        strip = lambda policy: {k: v for k, v in policy["position_sizing"].items() if k != "exposure_view"}  # noqa: E731
        self.assertEqual(strip(gated["policy"]), strip(plain["policy"]))
        self.assertEqual(gated["policy"]["cash_buffer_rate"], plain["policy"]["cash_buffer_rate"])

    def test_without_the_gate_briefs_prompts_and_team_inputs_are_unchanged(self):
        self.assertFalse((self.plain.world.run_dir / "exposure_facts.json").exists())
        for name in ("brief_cash_stance", "brief_swap_nomination", "brief_trader_b0"):
            self.assertNotIn("exposure_facts", json.dumps(self.load(self.plain, name), ensure_ascii=False), name)
        for _, prompt in self.plain_runner.prompts:
            self.assertNotIn("exposure_view", prompt)
            self.assertNotIn("exposure_facts", prompt)
        self.assertNotIn("exposure_facts", self.plain_art["team_inputs"])
        self.assertNotIn("exposure_view", self.plain_art["team_inputs"]["cash_stance"])
        self.assertEqual(self.plain.validate(self.plain_art), [])

    # -- 真實 Finalizer：fail-closed
    def assertRejected(self, errors, token):
        self.assertTrue(any(token in error for error in errors), (token, errors))

    def team(self, mutate, art=None):
        team = copy.deepcopy((art or self.art)["team_inputs"])
        mutate(team)
        return team

    def test_tampered_missing_or_gateless_facts_are_rejected(self):
        art = self.art
        tampered = self.team(lambda t: t["exposure_facts"]["portfolio"].update(portfolio_beta="0.999999"))
        self.assertRejected(self.gated.rebuild_errors(art, team_inputs=tampered), "exposure_facts 與重算結果不同")
        missing = self.team(lambda t: t.pop("exposure_facts"))
        self.assertRejected(self.gated.rebuild_errors(art, team_inputs=missing), "position_sizing.exposure_view 必須與 team_inputs.exposure_facts")
        gateless = swap.sealed(art["policy"], lambda p: p["position_sizing"].pop("exposure_view"))
        self.assertRejected(self.gated.rebuild_errors(art, policy=gateless), "position_sizing.exposure_view 必須與 team_inputs.exposure_facts")
        sneaked = self.team(lambda t: t.update(exposure_facts=art["team_inputs"]["exposure_facts"]), self.plain_art)
        self.assertRejected(self.plain.rebuild_errors(self.plain_art, team_inputs=sneaked), "position_sizing.exposure_view")

    def test_stance_enum_is_rebuilt_by_the_finalizer(self):
        art = self.art
        for label, mutate, token in (
            ("缺列舉", lambda t: t["cash_stance"].pop("exposure_view"), "exposure_view 必須是"),
            ("值域外", lambda t: t["cash_stance"].update(exposure_view="all_in"), "exposure_view 必須是"),
            ("理由寫出背景鍵名", lambda t: t["cash_stance"].update(rationale="依 exposure_facts 判斷。"), "不得寫出組合曝險背景的識別碼"),
        ):
            with self.subTest(label):
                self.assertRejected(self.gated.rebuild_errors(art, team_inputs=self.team(mutate)), token)
        sneaked = self.team(lambda t: t["cash_stance"].update(exposure_view="keep_defensive"), self.plain_art)
        self.assertRejected(self.plain.rebuild_errors(self.plain_art, team_inputs=sneaked), "exposure_view")


class StanceRuleTests(unittest.TestCase):
    """validate_cash_stance 的 exposure_view 規則（typed 決策輸入取自 pipeline 夾具）。"""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        harness = GatedHarness(Path(cls.directory.name))
        result, _ = harness.run(FakeRunner(view_outputs("keep_defensive")))
        assert result.status == "completed", result.errors
        art = harness.artifacts()
        cls.context = DecisionContext(art["decision_input"])
        cls.facts = art["team_inputs"]["exposure_facts"]
        # cash_grade 由 cash_ratio_range 另外驗證（這裡不傳該閘門），先拿掉，只看 exposure_view 的規則。
        cls.stance = {k: v for k, v in art["team_inputs"]["cash_stance"].items() if k != "cash_grade"}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def errors(self, stance, gate=GATE, facts=None):
        errors = []
        validate_cash_stance(self.context, stance, "cash_stance", errors, exposure_view_gate=gate,
                             exposure_facts=facts if facts is not None else self.facts,
                             turn_facts_gate={"version": "v1"})
        return errors

    def test_enum_is_required_and_closed(self):
        self.assertEqual(self.errors(self.stance), [])
        for view in EXPOSURE_VIEWS:
            evidence = dict(self.stance, exposure_view=view)
            self.assertEqual(self.errors(evidence), [], view)
        missing = {k: v for k, v in self.stance.items() if k != "exposure_view"}
        self.assertTrue(any("exposure_view 必須是" in e for e in self.errors(missing)))
        self.assertTrue(any("exposure_view 必須是" in e for e in self.errors(dict(self.stance, exposure_view="half"))))

    def test_rebuild_without_market_level_evidence_has_its_own_message(self):
        stock_only = [e for e in self.stance["evidence_ids"] if e not in self.context.market_evidence_ids]
        self.assertTrue(stock_only)
        for view in ("start_rebuild", "rebuild"):
            errors = self.errors(dict(self.stance, exposure_view=view, evidence_ids=stock_only))
            self.assertTrue(any("exposure_view=%s（不是 keep_defensive）" % view in e for e in errors), errors)
        errors = self.errors(dict(self.stance, exposure_view="keep_defensive", evidence_ids=stock_only))
        self.assertFalse(any("exposure_view=" in e for e in errors), errors)

    def test_field_without_the_gate_is_rejected(self):
        errors = []
        validate_cash_stance(self.context, self.stance, "cash_stance", errors, turn_facts_gate={"version": "v1"})
        self.assertEqual(errors, ["cash_stance 含未允許欄位：exposure_view"])
        errors = []
        stripped = {k: v for k, v in self.stance.items() if k != "exposure_view"}
        validate_cash_stance(self.context, stripped, "cash_stance", errors, turn_facts_gate={"version": "v1"})
        self.assertEqual(errors, [])

    def test_background_identifiers_in_the_rationale_are_rejected(self):
        self.assertEqual(exposure_reference_tokens(None), ["exposure_facts"])
        self.assertEqual(exposure_reference_tokens(self.facts), ["exposure_facts", self.facts["facts_id"]])
        for text in ("依 exposure_facts 的組合 beta。", "見 %s。" % self.facts["facts_id"]):
            errors = self.errors(dict(self.stance, rationale=text))
            self.assertTrue(any("不得寫出組合曝險背景的識別碼" in e for e in errors), text)


# ---------------------------------------------------------------- 5. 封存 run
@unittest.skipUnless((ARCHIVED / "team_inputs.json").is_file(), "封存 run 只在本機")
class ArchivedCaseTests(unittest.TestCase):
    """docs/agent_led_exposure_plan.md 第 5.1 節：2026-11-10 封存 run 唯讀重算（replay_reference_only）。

    計畫的數字取到 0.1 pp 或小數 3 位，這裡依各數字的位數容許半個進位單位；全局 Decimal 精度一律用預設 Context
    （calibration.tools 會把全域精度改成 40）。"""

    def run(self, result=None):
        with localcontext(Context()):
            return super().run(result)

    @classmethod
    def setUpClass(cls):
        with localcontext(Context()):
            load = lambda name: json.loads((ARCHIVED / ("%s.json" % name)).read_text(encoding="utf-8"))  # noqa: E731
            cls.files = {name: load(name) for name in (
                "decision_input", "team_inputs", "policy", "momentum", "debate", "intent", "decision")}
            cls.policy = copy.deepcopy(cls.files["policy"])
            cls.policy["position_sizing"]["exposure_view"] = dict(GATE)
            cls.policy["content_sha256"] = decision_policy_sha256(cls.policy)
            cls.facts = build_exposure_facts(cls.files["decision_input"], cls.policy)

    def test_case_numbers_match_the_plan(self):
        portfolio, up = self.facts["portfolio"], self.facts["up_day"]
        self.assertAlmostEqual(float(portfolio["portfolio_beta"]), 0.106, delta=0.0005)
        self.assertAlmostEqual(float(portfolio["invested_beta"]), 0.129, delta=0.0005)
        self.assertAlmostEqual(float(portfolio["low_beta_share"]), 0.635, delta=0.0005)
        self.assertEqual((portfolio["low_beta_count"], portfolio["held_count"]), (18, 27))
        self.assertAlmostEqual(float(portfolio["cash_weight"]), 0.180, delta=0.0005)
        financial = self.facts["sectors"]["financial_insurance"]
        self.assertEqual(financial["count"], 8)
        self.assertAlmostEqual(float(financial["weight"]), 0.260, delta=0.0005)
        self.assertEqual((up["date"], up["is_latest_trade_date"]), ("2026-11-09", True))
        self.assertAlmostEqual(float(up["pool_return"]), 0.0551, delta=0.00005)
        self.assertAlmostEqual(float(up["holdings_contribution"]), 0.0067, delta=0.00005)
        table = {  # 計畫表：(權重, beta, 當日報酬)
            "2412.TW": (0.070, -0.24, -0.028), "2886.TW": (0.049, -0.22, -0.013), "2880.TW": (0.041, -0.05, 0.013),
            "2891.TW": (0.030, 0.05, 0.034), "2885.TW": (0.047, 0.39, 0.037), "4904.TW": (0.054, -0.29, -0.054),
            "1216.TW": (0.041, -0.01, -0.001),
        }
        for symbol, (weight, beta, ret) in table.items():
            row = holding(self.facts, symbol)
            with self.subTest(symbol):
                self.assertAlmostEqual(float(row["weight"]), weight, delta=0.0005)
                self.assertAlmostEqual(float(row["beta"]), beta, delta=0.005)
                self.assertAlmostEqual(float(row["up_day_return"]), ret, delta=0.0005)
        self.assertEqual(self.facts["data_gaps"], [])
        non_held = {row["symbol"] for row in self.facts["non_held"]}
        self.assertTrue({"3665.TW", "6770.TW"} <= non_held)

    def test_without_the_gate_the_archived_chain_and_hash_are_unchanged(self):
        files = self.files
        self.assertNotIn("exposure_facts", files["team_inputs"])
        self.assertNotIn("exposure_view", files["team_inputs"]["cash_stance"])
        self.assertEqual(canonical_sha256(files["team_inputs"]), files["decision"]["team_inputs_sha256"])
        finalizer = DecisionFinalizer(files["decision_input"], files["policy"], files["momentum"], files["debate"],
                                      files["intent"], files["team_inputs"], DECISIONS)
        self.assertEqual(finalizer._validate_team_chain(), [])

    def test_with_the_gate_the_archived_chain_needs_and_rebuilds_the_facts(self):
        files = self.files
        stance = dict(files["team_inputs"]["cash_stance"], exposure_view="keep_defensive")
        team = dict(files["team_inputs"], exposure_facts=self.facts, cash_stance=stance)

        def chain(team_inputs):
            return DecisionFinalizer(files["decision_input"], self.policy, files["momentum"], files["debate"], files["intent"],
                                     team_inputs, DECISIONS)._validate_team_chain()

        self.assertEqual(chain(team), [])
        self.assertTrue(any("exposure_view" in error for error in chain(files["team_inputs"])))
        tampered = copy.deepcopy(team)
        tampered["exposure_facts"]["sectors"]["financial_insurance"]["weight"] = "0.100000"
        self.assertTrue(any("exposure_facts 與重算結果不同" in error for error in chain(tampered)))

    def test_allocation_is_identical_with_and_without_the_gate(self):
        files = self.files
        plain = AllocationOrderEngine(files["decision_input"], files["policy"]).run(files["intent"])
        gated = AllocationOrderEngine(files["decision_input"], self.policy).run(files["intent"])
        for key in ("allocation_proposal", "order_proposal", "constraint_flags"):
            self.assertEqual(gated[key], plain[key], key)


if __name__ == "__main__":
    unittest.main()
