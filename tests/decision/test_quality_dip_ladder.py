"""優質股 v4（position_sizing.agent_swap.quality_dip version=v4）：連跌後逢低布局的梯度 ``ladder``（docs/plans/agent_led_offense_plan.md 4.13）。

覆蓋：

1. 閘門（Policy 驗證）：v4 必須有 ladder、ladder 只用於 v4、全部欄位必填且人填、no_turn_evidence 只有 v4 才允許、ladder_state 形狀；
   v1／v2／v3 逐位元維持原行為。
2. 連跌深度事實（合成行情）：連跌天數與累計跌幅、離 60 日高點回檔、本輪下跌起點、對照樣本（前瞻視窗不越過最近交易日、small_sample）；
   歷史不足／覆蓋不足只降級為 unavailable、不補值；bar 晚於最近交易日、可得時間晚於 cutoff 一律拒絕；Finalizer 整份重算、竄改即拒絕。
3. 梯度狀態：每個關閉原因（缺資料一律關閉）、轉向跡象不受回檔深度門檻限制、總抄底曝險與批數與間隔與暫停線、暫停的解除。
4. 綁定與前置檢查：turn_allows 的 no_turn_evidence、program_skip_reason 讀梯度、v4 綁定。
5. 配置（真實 AllocationOrderEngine）：梯度關閉時 quality_dip 配對全部取消且換出者不賣；開啟時與 v3 同一個結果；今天的買進權重上限超過剩餘曝險時從清單尾端取消。
6. 簡報與 Finalizer：不附上限數字的裁決簡報、提名簡報附梯度、背景識別碼、team_inputs 選配鍵與閘門雙向。

合成行情是機制測試，不是對真實行情的驗證；所有數字只放在測試內，沒有寫進 config。
"""

import copy
import json
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from etf_agent.automation.agent_runner import LATER_STAGE_GLOBS
from etf_agent.decision import DecisionToolError
from etf_agent.decision.decline_depth import (
    DECLINE_DEPTH_FACTS_KEY, SMALL_SAMPLE_MIN, build_decline_depth_facts, build_ladder_state, decline_depth_brief,
    decline_depth_reference_tokens, validate_decline_depth_facts, validate_ladder_state,
)
from etf_agent.decision.exposure import holding_weights
from etf_agent.decision.finalization import build_team_inputs
from etf_agent.decision.quality_dip_swap import (
    LADDER_CLOSED_REASONS, QUALITY_DIP_SWAP_NOTE_V4, bind_quality_dip_ladder_state, bind_quality_dip_turn, program_skip_reason,
    quality_dip_ladder_gate, quality_dip_swap_binding, quality_dip_v3_gate, turn_allows, validate_quality_dip_swap_gate,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_quality_dip_sizing import FLOOR_OK, V3, RulingWorld, bind_v3, shares
from test_agent_swap_allocation import PAIR, exposure_bundle, v2_world
from test_exposure_rotation_e2e import sold_and_bought
from test_quality_dip_swap import agent_flags, quality_sizing

ROOT = Path(__file__).resolve().parents[2]
LADDER = {
    "min_pool_drawdown": "0.10", "max_dip_exposure": "0.12", "max_tranches": 3, "min_days_between_tranches": 2,
    "pause_after_further_drop": "0.05",
}
V4 = dict(V3, version="v4", max_entry_weight="0.04", ladder=dict(LADDER), min_turn_assessment="no_turn_evidence")
TRADE_DATE = "2026-11-09"
CUTOFF = "2026-11-09T23:00:00+00:00"
SYMBOLS = ("A.TW", "B.TW", "C.TW")
DAYS = 100


def weekdays(end, count):
    days, day = [], date.fromisoformat(end)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    return sorted(days)


def uptrend_then(tail, days=DAYS):
    """前面是緩升的上升段（奇數日 +0.3%、偶數日 −0.1%），最後接 ``tail``（日報酬清單）。"""
    body = [Decimal("0.003") if index % 2 else Decimal("-0.001") for index in range(1, days - len(tail))]
    return body + [Decimal(str(value)) for value in tail]


def make_bundle(returns, positions=None, cash="800000", drop=None):
    """三檔同步的合成行情：index 1 起每天的報酬＝returns；``drop`` 是 {(symbol, index)} 的缺漏 bar。"""
    count = len(returns) + 1
    days = weekdays(TRADE_DATE, count)
    drop = drop or set()
    closes = [Decimal(100)]
    for value in returns:
        closes.append(closes[-1] * (Decimal(1) + value))
    series, latest = [], []
    for symbol in SYMBOLS:
        bars = [
            {"trade_date": day, "close": str(close), "available_at": "%sT14:00:00+08:00" % day}
            for index, (day, close) in enumerate(zip(days, closes)) if (symbol, index) not in drop
        ]
        series.append({"symbol": symbol, "evidence_id": "price:%s" % symbol, "bars": bars})
        latest.append({"symbol": symbol, "analysis_close_price": str(closes[-1]), "source_evidence_id": "price:%s" % symbol})
    positions = {"A.TW": 100, "B.TW": 1000, "C.TW": 2000} if positions is None else positions
    return {
        "decision_cutoff": CUTOFF, "evidence_policy": "typed_evidence_v1",
        "snapshot": {"latest_trade_date": TRADE_DATE, "latest_prices": latest, "documents": []},
        "price_series": series,
        "account_snapshot": {"cash": cash, "positions": [{"symbol": s, "shares": n} for s, n in positions.items()]},
    }


def held_since(bundle, mapping):
    """{symbol: 交易日索引} → 帶時區的台北時間字串。"""
    days = sorted({bar["trade_date"] for row in bundle["price_series"] for bar in row["bars"]})
    return {symbol: "%sT21:00:00+08:00" % days[min(index, len(days) - 1)] for symbol, index in mapping.items()}


def policy_for(bundle, since, gate=None, turn="no_turn_evidence"):
    quality = dict(V4 if gate is None else gate)
    quality["turn_assessment"] = turn
    return {
        "position_sizing": {
            "agent_swap": {"version": "v2", "quality_dip": quality},
            "exposure_rotation": {"held_since_by_symbol": held_since(bundle, since)},
        },
        "drawdown_control": dict(FLOOR_OK),
    }


# 十天連跌（每天 −2%）接在上升段之後：離 60 日高點回檔約 18%，高點在連跌前一天（索引 89）。
FALL = ["-0.02"] * 10
SINCE_OLD = {"A.TW": 50, "B.TW": 50, "C.TW": 50}


class FactsTests(unittest.TestCase):
    def setUp(self):
        self.bundle = make_bundle(uptrend_then(FALL))
        self.policy = policy_for(self.bundle, SINCE_OLD)
        self.facts = build_decline_depth_facts(self.bundle, self.policy)
        self.days = weekdays(TRADE_DATE, DAYS)

    def test_streak_drawdown_and_the_episode_anchor_are_computed_from_the_pool_returns(self):
        pool = self.facts["pool"]
        self.assertEqual(self.facts["data_status"], "available")
        self.assertFalse(self.facts["citable"])
        self.assertEqual(pool["down_streak_days"], 10)
        self.assertEqual(pool["streak_start_date"], self.days[90])
        self.assertEqual(pool["window_high_date"], self.days[89])  # 高點是連跌前一天，不是連跌起點
        expected = Decimal(1) - Decimal("0.98") ** 10
        self.assertLess(abs(Decimal(pool["drawdown_from_window_high"]) - expected), Decimal("0.00001"))
        self.assertLess(abs(Decimal(pool["streak_return"]) + expected), Decimal("0.00001"))
        self.assertEqual(self.facts["facts_id"][:20], "decline-depth-facts:")
        self.assertEqual(self.facts["reference"]["evidence_grade"], "replay_reference_only")

    def test_the_comparison_sample_never_looks_past_the_latest_trade_date_and_is_marked_small(self):
        after = self.facts["reference"]["after_streak"]
        self.assertEqual(after["event_dates"], [self.days[92]])  # 連跌「剛滿 3 天」的日子
        for horizon in ("3", "5"):
            self.assertEqual(after[horizon]["n"], 1)
            self.assertTrue(after[horizon]["small_sample"])
            self.assertEqual(after[horizon]["negative"], 1)
        # 事件日落在最後幾天：前瞻視窗會越過最近交易日，該事件不計入、也不補值。
        late = build_decline_depth_facts(make_bundle(uptrend_then(["0.01", "-0.02", "-0.02", "-0.02"])), self.policy)
        self.assertEqual(late["reference"]["after_streak"]["event_dates"], [self.days[99]])
        for horizon in ("3", "5"):
            self.assertEqual(late["reference"]["after_streak"][horizon]["n"], 0)
            self.assertIsNone(late["reference"]["after_streak"][horizon]["worst"])
        # 回檔達目前深度的日子都早於最近交易日（目前就是最深）：沒有可比較的樣本。
        self.assertEqual(self.facts["reference"]["after_same_depth"]["3"]["n"], 0)

    def test_enough_samples_clear_the_small_sample_flag(self):
        cycle = ["-0.01", "-0.01", "-0.01", "0.01"]
        returns = [Decimal(v) for v in (cycle * 30)[:DAYS - 1]]
        facts = build_decline_depth_facts(make_bundle(returns), self.policy)
        stats = facts["reference"]["after_streak"]["3"]
        self.assertGreaterEqual(stats["n"], SMALL_SAMPLE_MIN)
        self.assertFalse(stats["small_sample"])
        self.assertLessEqual(Decimal(stats["worst"]), Decimal(stats["median"]))
        self.assertLessEqual(Decimal(stats["median"]), Decimal(stats["best"]))

    def test_short_history_and_thin_coverage_degrade_to_unavailable_without_guessing(self):
        short = build_decline_depth_facts(make_bundle(uptrend_then(["-0.02"] * 5, days=40)), self.policy)
        self.assertEqual(short["data_status"], "unavailable")
        self.assertIsNone(short["pool"]["drawdown_from_window_high"])
        self.assertTrue(short["data_gaps"])
        # 最近三個交易日有兩檔缺 bar：覆蓋低於 80%，連跌天數與累計跌幅不可用（不當成 0）。
        thin = make_bundle(uptrend_then(FALL), drop={(s, i) for s in ("B.TW", "C.TW") for i in (97, 98)})
        facts = build_decline_depth_facts(thin, self.policy)
        self.assertEqual(facts["data_status"], "unavailable")
        self.assertIsNone(facts["pool"]["down_streak_days"])

    def test_late_bars_late_availability_and_missing_timezone_are_rejected(self):
        for mutate in (
            lambda b: b["price_series"][0]["bars"].append({"trade_date": "2026-11-10", "close": "1", "available_at": "2026-11-10T14:00:00+08:00"}),
            lambda b: b["price_series"][0]["bars"][-1].update(available_at="2026-11-10T14:00:00+08:00"),
            lambda b: b["price_series"][0]["bars"][-1].update(available_at="2026-11-09T14:00:00"),
        ):
            bundle = copy.deepcopy(self.bundle)
            mutate(bundle)
            with self.assertRaises(DecisionToolError):
                build_decline_depth_facts(bundle, self.policy)

    def test_the_facts_need_the_v4_ladder(self):
        for gate in (dict(V3), dict(V4, version="v3")):
            with self.assertRaises(DecisionToolError):
                build_decline_depth_facts(self.bundle, policy_for(self.bundle, SINCE_OLD, gate=gate))

    def test_the_finalizer_recomputes_the_whole_facts_object(self):
        def errors_for(team, policy=None):
            errors = []
            validate_decline_depth_facts(self.bundle, policy or self.policy, team, errors)
            return errors

        self.assertEqual(errors_for({DECLINE_DEPTH_FACTS_KEY: self.facts}), [])
        for mutate in (
            lambda f: f["pool"].update(down_streak_days=3),
            lambda f: f["reference"]["after_streak"]["3"].update(worst="0.5"),
            lambda f: f.update(data_status="unavailable"),
            lambda f: f.update(extra="x"),
            lambda f: f.pop("note"),
        ):
            forged = copy.deepcopy(self.facts)
            mutate(forged)
            self.assertTrue(errors_for({DECLINE_DEPTH_FACTS_KEY: forged}))
        self.assertTrue(errors_for({}))
        self.assertTrue(errors_for({DECLINE_DEPTH_FACTS_KEY: self.facts}, {"position_sizing": {}}))  # 沒有 v4 的 Policy 不得帶這個鍵
        self.assertEqual(decline_depth_reference_tokens(self.facts), [DECLINE_DEPTH_FACTS_KEY, self.facts["facts_id"]])


class LadderStateTests(unittest.TestCase):
    def state(self, since, turn="no_turn_evidence", returns=None, gate=None, **bundle_kwargs):
        bundle = make_bundle(uptrend_then(FALL) if returns is None else returns, **bundle_kwargs)
        return build_ladder_state(bundle, policy_for(bundle, since, gate=gate, turn=turn), turn), bundle

    def test_open_when_deep_enough_and_nothing_new_since_the_peak(self):
        state, _ = self.state(SINCE_OLD)
        self.assertEqual(state["status"], "open")
        self.assertIsNone(state["reason"])
        self.assertEqual((state["tranches"], Decimal(state["exposure_used"])), (0, Decimal(0)))
        self.assertEqual(Decimal(state["exposure_remaining"]), Decimal("0.12"))
        self.assertEqual(state["anchor_date"], weekdays(TRADE_DATE, DAYS)[90])  # 本輪連跌起始日晚於視窗高點日，取較晚者

    def test_without_turn_evidence_a_shallow_pool_drawdown_keeps_it_closed_but_a_turn_sign_lifts_the_depth_gate(self):
        shallow = uptrend_then(["-0.02"] * 3)  # 回檔約 6%，低於 0.10
        closed, _ = self.state(SINCE_OLD, returns=shallow)
        self.assertEqual((closed["status"], closed["reason"]), ("closed", "depth_below_gate"))
        for turn in ("turn_unconfirmed", "turn_confirmed"):
            opened, _ = self.state(SINCE_OLD, turn=turn, returns=shallow)
            self.assertEqual(opened["status"], "open", turn)  # 有轉向跡象時不受 min_pool_drawdown 限制，其餘梯度照舊

    def test_every_missing_input_closes_the_ladder(self):
        # 歷史不足 60 個交易日：facts_unavailable。
        short, _ = self.state(SINCE_OLD, returns=uptrend_then(["-0.02"] * 5, days=40))
        self.assertEqual((short["status"], short["reason"]), ("closed", "facts_unavailable"))
        # 帳本持有起點沒綁、或與帳戶持股不一致：held_since_unavailable。
        bundle = make_bundle(uptrend_then(FALL))
        for policy in (
            {"position_sizing": {"agent_swap": {"quality_dip": dict(V4)}}, "drawdown_control": dict(FLOOR_OK)},
            policy_for(bundle, {"A.TW": 50}),
        ):
            state = build_ladder_state(bundle, policy, "no_turn_evidence")
            self.assertEqual((state["status"], state["reason"]), ("closed", "held_since_unavailable"))
        with self.assertRaises(DecisionToolError):
            build_ladder_state(bundle, policy_for(bundle, SINCE_OLD), "maybe")  # turn_assessment 不在列舉內：拋錯，不當成通過

    def test_new_positions_since_the_peak_count_toward_exposure_and_tranches(self):
        since = {"A.TW": 50, "B.TW": 92, "C.TW": 95}  # 兩檔高點之後新建倉、不同日
        state, bundle = self.state(since, gate=dict(V4, ladder=dict(LADDER, max_dip_exposure="0.9", pause_after_further_drop="0.9")))
        weights, _, _ = holding_weights(bundle)
        self.assertEqual(state["tranches"], 2)
        self.assertEqual(state["last_tranche_date"], weekdays(TRADE_DATE, DAYS)[95])
        self.assertEqual(Decimal(state["exposure_used"]), (weights["B.TW"] + weights["C.TW"]).quantize(Decimal("0.000001")))
        self.assertEqual(state["status"], "open")
        self.assertLess(Decimal(state["exposure_remaining"]), Decimal("0.9"))

    def test_the_exposure_tranche_and_interval_limits_each_close_it(self):
        wide = dict(LADDER, max_dip_exposure="0.9", pause_after_further_drop="0.9")
        since = {"A.TW": 50, "B.TW": 92, "C.TW": 95}
        full, _ = self.state(since, gate=dict(V4, ladder=dict(wide, max_dip_exposure="0.01")))
        self.assertEqual(full["reason"], "exposure_full")
        tranches, _ = self.state(since, gate=dict(V4, ladder=dict(wide, max_tranches=2)))
        self.assertEqual(tranches["reason"], "tranches_full")
        # 上一批在索引 95、最近交易日索引 99：共 5 個交易日；要求至少 6 個才放行。
        interval, _ = self.state(since, gate=dict(V4, ladder=dict(wide, min_days_between_tranches=6)))
        self.assertEqual((interval["reason"], interval["days_since_last_tranche"]), ("tranche_interval", 5))
        ok, _ = self.state(since, gate=dict(V4, ladder=dict(wide, min_days_between_tranches=5)))
        self.assertEqual(ok["status"], "open")

    def test_a_further_drop_after_the_last_tranche_pauses_until_a_pool_up_day(self):
        since = {"A.TW": 50, "B.TW": 50, "C.TW": 92}  # 上一批在索引 92，參考水位是索引 91 的收盤
        wide = dict(V4, ladder=dict(LADDER, max_dip_exposure="0.9"))
        paused, _ = self.state(since, gate=wide)
        self.assertEqual((paused["status"], paused["reason"], paused["paused"]), ("closed", "paused_after_drop", True))
        self.assertGreaterEqual(Decimal(paused["further_drop"]), Decimal("0.05"))
        # 觸發之後出現一個上漲日（索引 97）就解除，即使整體仍在跌。
        released = uptrend_then(["-0.02"] * 7 + ["0.01"] + ["-0.02"] * 2)
        state, _ = self.state(since, returns=released, gate=wide)
        self.assertFalse(state["paused"])
        self.assertEqual(state["status"], "open")
        # 再跌幅度沒達暫停線：不暫停。
        mild, _ = self.state(since, gate=dict(V4, ladder=dict(LADDER, max_dip_exposure="0.9", pause_after_further_drop="0.5")))
        self.assertFalse(mild["paused"])

    def test_a_tranche_whose_reference_return_is_missing_closes_the_ladder_instead_of_passing(self):
        # 整段下跌：視窗高點在視窗第一天（索引 40），上一批就在那天；參考日（索引 39）之後的第一個日報酬缺漏（兩檔缺 bar）。
        falling = [Decimal("-0.005")] * (DAYS - 1)
        bundle = make_bundle(falling, drop={(s, 39) for s in ("B.TW", "C.TW")})
        wide = dict(V4, ladder=dict(LADDER, max_dip_exposure="0.9"))
        policy = policy_for(bundle, {"A.TW": 20, "B.TW": 20, "C.TW": 40}, gate=wide)
        state = build_ladder_state(bundle, policy, "no_turn_evidence")
        self.assertEqual((state["status"], state["reason"], state["paused"]), ("closed", "facts_unavailable", None))  # 缺 bar 使覆蓋不足：整份事實降級，梯度關閉、不放行

    def test_the_state_is_deterministic_and_validate_ladder_state_rejects_any_rewrite(self):
        bundle = make_bundle(uptrend_then(FALL))
        policy = policy_for(bundle, SINCE_OLD)
        facts = build_decline_depth_facts(bundle, policy)
        state = build_ladder_state(bundle, policy, "no_turn_evidence", facts)
        self.assertEqual(state, build_ladder_state(bundle, policy, "no_turn_evidence"))
        bound = copy.deepcopy(policy)
        bound["position_sizing"]["agent_swap"]["quality_dip"]["ladder_state"] = state

        def errors(candidate, team=None):
            problems = []
            validate_ladder_state(bundle, candidate, team if team is not None else {DECLINE_DEPTH_FACTS_KEY: facts}, problems)
            return problems

        self.assertEqual(errors(bound), [])
        for field, value in (("status", "closed"), ("exposure_remaining", "0.99"), ("tranches", 9), ("reason", "exposure_full")):
            forged = copy.deepcopy(bound)
            forged["position_sizing"]["agent_swap"]["quality_dip"]["ladder_state"][field] = value
            self.assertTrue(errors(forged), field)
        unbound = copy.deepcopy(policy)
        self.assertTrue(errors(unbound))  # 沒綁定
        stale = copy.deepcopy(bound)
        stale["position_sizing"]["agent_swap"]["quality_dip"]["turn_assessment"] = "turn_confirmed"
        self.assertTrue(errors(stale))  # 綁定時的 turn_assessment 被改


class GateTests(unittest.TestCase):
    def problems(self, version="v4", **changes):
        sizing = quality_sizing(version, "v2", **changes)
        errors: list = []
        validate_quality_dip_swap_gate(sizing, errors)
        return errors

    def v4(self, **changes):
        base = {key: value for key, value in V4.items() if key != "version"}
        base.update(changes)
        return self.problems("v4", **base)

    def test_v4_is_valid_and_alone_allows_no_turn_evidence(self):
        self.assertEqual(self.v4(), [])
        self.assertEqual(self.v4(min_turn_assessment="turn_unconfirmed"), [])
        for version in ("v1", "v2", "v3"):
            errors = self.problems(version, min_turn_assessment="no_turn_evidence")
            self.assertTrue(any("min_turn_assessment" in e for e in errors), version)

    def test_ladder_is_required_closed_and_every_field_is_human_filled(self):
        no_ladder = {key: value for key, value in V4.items() if key not in ("version", "ladder")}
        self.assertTrue(any("ladder" in e for e in self.problems("v4", **no_ladder)))
        for field in LADDER:
            ladder = dict(LADDER)
            del ladder[field]
            self.assertTrue(any(field in e for e in self.v4(ladder=ladder)), field)  # 程式不給預設
        self.assertTrue(self.v4(ladder=dict(LADDER, extra=1)))
        for field, bad in (
            ("min_pool_drawdown", "0"), ("min_pool_drawdown", "1"), ("min_pool_drawdown", 0.1), ("max_dip_exposure", "0"),
            ("pause_after_further_drop", "-0.1"), ("max_tranches", 0), ("max_tranches", 11), ("max_tranches", True),
            ("min_days_between_tranches", -1), ("min_days_between_tranches", 21),
        ):
            self.assertTrue(self.v4(ladder=dict(LADDER, **{field: bad})), (field, bad))
        self.assertEqual(self.v4(ladder=dict(LADDER, min_days_between_tranches=0)), [])
        # 總抄底曝險至少容得下一檔。
        self.assertTrue(any("max_entry_weight" in e for e in self.v4(ladder=dict(LADDER, max_dip_exposure="0.1"), max_entry_weight="0.2")))

    def test_ladder_belongs_to_v4_only(self):
        for version in ("v1", "v2", "v3"):
            errors = self.problems(version, ladder=dict(LADDER))
            self.assertTrue(any("ladder" in e for e in errors), version)

    def test_ladder_state_is_pipeline_bound_closed_and_consistent(self):
        open_state = {"status": "open", "reason": None}
        self.assertEqual(self.v4(ladder_state=dict(open_state)), [])
        self.assertTrue(self.v4(ladder_state={"status": "open", "reason": "exposure_full"}))
        self.assertTrue(self.v4(ladder_state={"status": "closed", "reason": None}))
        self.assertTrue(self.v4(ladder_state={"status": "closed", "reason": "mystery"}))
        self.assertTrue(self.v4(ladder_state={"status": "open", "reason": None, "extra": 1}))
        self.assertTrue(self.problems("v3", **{"ladder_state": dict(open_state)}))

    def test_v4_is_a_superset_of_v3(self):
        self.assertIsNotNone(quality_dip_v3_gate({"agent_swap": {"quality_dip": dict(V4)}}))
        self.assertIsNone(quality_dip_ladder_gate({"agent_swap": {"quality_dip": dict(V3)}}))
        self.assertIsNone(quality_dip_ladder_gate({"agent_swap": {"quality_dip": dict(V4, version="v3")}}))
        self.assertEqual(quality_dip_ladder_gate({"agent_swap": {"quality_dip": dict(V4)}}), LADDER)
        self.assertIn("agent_swap", FORBIDDEN_PREFIXES)  # 閘門只由人設定，策略反思不得改
        self.assertIn("decline_depth_facts*", LATER_STAGE_GLOBS)

    def test_the_live_template_is_untouched_by_this_stage(self):
        live = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        gate = live["position_sizing"]["agent_swap"]["quality_dip"]
        if gate["version"] != "v4":  # 人填正式設定之前：沒有 ladder，行為與 v3 完全相同
            self.assertNotIn("ladder", gate)
            self.assertNotIn("ladder_state", gate)


class BindingTests(unittest.TestCase):
    def test_turn_allows_accepts_every_turn_only_when_the_minimum_is_no_turn_evidence(self):
        for turn in ("no_turn_evidence", "turn_unconfirmed", "turn_confirmed"):
            self.assertTrue(turn_allows({"turn_assessment": turn, "min_turn": "no_turn_evidence"}), turn)
        self.assertFalse(turn_allows({"turn_assessment": "no_turn_evidence", "min_turn": "turn_unconfirmed"}))  # v1～v3 的舊行為不變
        self.assertTrue(turn_allows({"turn_assessment": "turn_confirmed", "min_turn": "turn_unconfirmed"}))
        self.assertFalse(turn_allows({"turn_assessment": "turn_unconfirmed", "min_turn": "turn_confirmed"}))

    def test_program_skip_reason_reads_the_bound_ladder_state(self):
        def policy(state):
            quality = dict(V4, turn_assessment="no_turn_evidence")
            if state is not None:
                quality["ladder_state"] = state
            return {"position_sizing": {"agent_swap": {"quality_dip": quality}}, "drawdown_control": dict(FLOOR_OK)}

        self.assertIsNone(program_skip_reason(policy({"status": "open", "reason": None})))
        self.assertEqual(program_skip_reason(policy({"status": "closed", "reason": "exposure_full"})), "quality_dip_ladder_exposure_full")
        with self.assertRaises(DecisionToolError):
            program_skip_reason(policy(None))  # 沒綁梯度狀態：拋錯，不靜默放行
        floor = dict(FLOOR_OK, stance_floor="defensive")  # 回撤下限與回撤資料仍先於梯度
        defensive = policy({"status": "open", "reason": None})
        defensive["drawdown_control"] = floor
        self.assertEqual(program_skip_reason(defensive), "quality_dip_stance_floor")

    def test_the_turn_binding_and_the_ladder_binding_are_separate_and_prefill_is_refused(self):
        sizing = {"agent_swap": {"version": "v2", "quality_dip": dict(V4)}}
        bound = bind_quality_dip_turn(sizing["agent_swap"], sizing, {"turn_assessment": "no_turn_evidence"})
        self.assertEqual(bound["quality_dip"]["turn_assessment"], "no_turn_evidence")
        self.assertNotIn("ladder_state", bound["quality_dip"])
        state = {"status": "open", "reason": None}
        with_state = bind_quality_dip_ladder_state(bound, state)
        self.assertEqual(with_state["quality_dip"]["ladder_state"], state)
        with self.assertRaises(DecisionToolError):
            bind_quality_dip_ladder_state(with_state, state)  # 樣板預填即拒絕
        v3 = {"quality_dip": dict(V3)}
        self.assertEqual(bind_quality_dip_ladder_state(v3, state), v3)  # v3 不綁

    def test_the_v4_binding_carries_the_state_and_the_sized_flag(self):
        sizing = {"agent_swap": {
            "version": "v2", "max_swaps_per_run": 2, "min_holding_days": 5, "exposure": {"version": "v1"},
            "quality_dip": dict(V4, turn_assessment="no_turn_evidence", ruling_ref={"ruling_id": "r", "sha256": "b" * 64, "status": "not_required"},
                                rulings=[], ladder_state={"status": "open", "reason": None, "exposure_remaining": "0.05"}),
            "nominated_pairs": [{"buy_symbol": "1101.TW", "sell_symbol": "2330.TW", "purpose": "thesis"}],
            "nomination_ref": {"nomination_id": "n", "sha256": "a" * 64, "status": "completed"},
        }}
        binding = quality_dip_swap_binding(sizing)
        self.assertEqual((binding["version"], binding["sized"]), ("v4", True))
        self.assertEqual(binding["ladder_state"]["status"], "open")
        del sizing["agent_swap"]["quality_dip"]["ladder_state"]
        with self.assertRaises(DecisionToolError):
            quality_dip_swap_binding(sizing)
        v3 = {"agent_swap": dict(sizing["agent_swap"], quality_dip=dict(V3, turn_assessment="turn_unconfirmed",
                                                                         ruling_ref={"ruling_id": "r", "sha256": "b" * 64, "status": "not_required"}, rulings=[]))}
        self.assertEqual(quality_dip_swap_binding(v3)["sized"], True)
        self.assertNotIn("ladder_state", quality_dip_swap_binding(v3))


class AllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = exposure_bundle()
        cls.intent, cls.policy, cls.plain, _ = v2_world(cls.bundle, pairs=[PAIR])

    def run_v4(self, state, turn="no_turn_evidence", **kwargs):
        gate = dict(V4, max_entry_weight="0.2", ladder=dict(LADDER, max_dip_exposure="0.9"), ladder_state=state)  # 硬上限 0.2 要小於總抄底曝險
        return bind_v3(self.bundle, self.intent, self.policy, gate=gate, turn=turn, **kwargs)

    OPEN = {"status": "open", "reason": None, "exposure_remaining": "0.200000"}

    def test_an_open_ladder_trades_exactly_like_v3(self):
        _, _, v4 = self.run_v4(self.OPEN, turn="turn_unconfirmed")
        _, _, v3 = bind_v3(self.bundle, self.intent, self.policy, turn="turn_unconfirmed", gate=dict(V3))
        self.assertEqual(shares(v4), 1000)
        self.assertEqual(v4["order_proposal"], v3["order_proposal"])
        self.assertEqual(sold_and_bought(v4), ({"2330.TW"}, {"2317.TW"}))

    def test_every_closed_reason_cancels_the_pair_and_the_victim_is_not_sold(self):
        for reason in LADDER_CLOSED_REASONS:
            with self.subTest(reason):
                _, _, body = self.run_v4({"status": "closed", "reason": reason, "exposure_remaining": "0.000000"})
                self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_ladder_%s" % reason)])
                self.assertEqual(body["order_proposal"]["orders"], [])
                self.assertEqual(sold_and_bought(body), (set(), set()))

    def test_todays_entry_weight_cap_may_not_exceed_the_remaining_dip_exposure(self):
        # large＝20% 的買進權重上限超過剩餘曝險 10%：取消；剩餘曝險容得下才成交（只看上限、不看實際成交）。
        _, _, body = self.run_v4(dict(self.OPEN, exposure_remaining="0.100000"))
        self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_ladder_exposure")])
        self.assertEqual(body["order_proposal"]["orders"], [])
        _, _, fits = self.run_v4(dict(self.OPEN, exposure_remaining="0.200000"))
        self.assertEqual(shares(fits), 1000)
        # 風險 Agent 降一格後上限是 13.33%，剩餘 15% 就容得下。
        _, _, medium = self.run_v4(dict(self.OPEN, exposure_remaining="0.150000"), ruling="downsize")
        self.assertEqual(shares(medium), 1000)

    def test_v3_has_no_ladder_check_and_an_unbound_state_is_refused(self):
        _, _, v3 = bind_v3(self.bundle, self.intent, self.policy, gate=dict(V3))
        self.assertEqual(shares(v3), 1000)
        with self.assertRaises(DecisionToolError):
            bind_v3(self.bundle, self.intent, self.policy, gate=dict(V4), turn="no_turn_evidence")


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.bundle = make_bundle(uptrend_then(FALL))
        self.policy = policy_for(self.bundle, SINCE_OLD)
        self.facts = build_decline_depth_facts(self.bundle, self.policy)
        self.state = build_ladder_state(self.bundle, self.policy, "no_turn_evidence", self.facts)

    def test_the_nomination_brief_has_the_ladder_and_the_ruling_brief_has_no_limits(self):
        nomination = decline_depth_brief(self.facts, self.state)
        self.assertFalse(nomination["citable"])
        self.assertEqual(nomination["ladder"]["status"], "open")
        self.assertIn("exposure_remaining", nomination["ladder"])
        self.assertNotIn("facts_id", json.dumps(nomination["ladder"]))
        ruling = decline_depth_brief(self.facts)
        self.assertNotIn("ladder", ruling)
        self.assertNotIn("exposure_remaining", json.dumps(ruling))
        self.assertNotIn("max_dip_exposure", json.dumps(ruling))
        for brief in (nomination, ruling):
            self.assertNotIn('"evidence_id":', json.dumps(brief))

    def test_a_shallow_pullback_brief_does_not_claim_the_pairs_are_certain_to_be_closed(self):
        shallow = build_decline_depth_facts(make_bundle(uptrend_then(["-0.002"] * 3)), self.policy)
        strict = build_ladder_state(make_bundle(uptrend_then(["-0.002"] * 3)), self.policy, "no_turn_evidence", shallow)
        lenient = build_ladder_state(make_bundle(uptrend_then(["-0.002"] * 3)), self.policy, "turn_unconfirmed", shallow)
        self.assertEqual(strict["reason"], "depth_below_gate")
        self.assertNotEqual(lenient["reason"], "depth_below_gate")
        brief = decline_depth_brief(shallow, lenient, strict)
        self.assertTrue(brief["depth_needs_turn"])
        self.assertNotIn("一定不會成交", brief["ladder_note"])
        self.assertIn("風險 Agent 判斷有轉向跡象", brief["ladder_note"])
        self.assertFalse(decline_depth_brief(self.facts, self.state, self.state)["depth_needs_turn"])

    def test_the_v4_note_replaces_the_no_turn_sentence_and_says_deep_is_not_a_reason(self):
        self.assertNotIn("沒有轉向跡象或回撤強制防守時不取用", QUALITY_DIP_SWAP_NOTE_V4)
        self.assertIn("跌很深", QUALITY_DIP_SWAP_NOTE_V4)
        self.assertIn("v3：", QUALITY_DIP_SWAP_NOTE_V4)  # v3 的份量格與裁決說明仍在

    def test_team_inputs_refuses_the_facts_without_the_ruling_key_of_2_10(self):
        with self.assertRaises(DecisionToolError) as caught:
            build_team_inputs({}, None, {}, decline_depth_facts=self.facts)
        self.assertIn("decline_depth_facts", str(caught.exception) + build_team_inputs.__doc__)
        self.assertIn("decline_depth_facts", build_team_inputs.__doc__)


class ClosedLadderRulingTests(unittest.TestCase):
    """梯度關閉時，有 quality_dip 配對的提名不呼叫風險 Agent：程式封存 skipped_by_program 與原因代碼，驗證器與 Finalizer 路徑重算後接受。"""

    CLOSED = dict(
        status="closed", reason="exposure_full", turn_assessment="turn_unconfirmed", facts_id="decline-depth-facts:x", anchor_date="2026-10-29",
        pool_drawdown="0.15", exposure_used="0.12", exposure_remaining="0.000000", tranches=3, last_tranche_date="2026-11-10",
        days_since_last_tranche=1, further_drop=None, paused=False)

    def world(self, state):
        from etf_agent.decision.quality_dip_ruling import STATUS_SKIPPED  # noqa: F401
        world = RulingWorld()
        gate = world.policy["position_sizing"]["agent_swap"]["quality_dip"]
        gate.update(version="v4", ladder=dict(LADDER, max_dip_exposure="0.9"), ladder_state=state)
        return world

    def test_a_closed_ladder_is_skipped_with_the_ladder_reason_and_the_validator_accepts_it(self):
        from etf_agent.decision.quality_dip_ruling import STATUS_SKIPPED, expected_program_status
        world = self.world(dict(self.CLOSED))
        self.assertEqual(expected_program_status(world.policy, world.nomination), (STATUS_SKIPPED, "quality_dip_ladder_exposure_full"))
        self.assertEqual(world.validator().validate(world.program(STATUS_SKIPPED, ["quality_dip_ladder_exposure_full"])), [])
        self.assertTrue(world.validator().validate(world.program(STATUS_SKIPPED, ["quality_dip_turn"])))  # 原因代碼必須是梯度的
        self.assertTrue(world.validator().validate(world.seal({"rulings": [], "defensive_reconciliation": None})))  # 不得呼叫 Agent

    def test_an_open_ladder_does_not_skip_and_an_unbound_state_raises(self):
        from etf_agent.decision.quality_dip_ruling import STATUS_SKIPPED, expected_program_status
        opened = self.world(dict(self.CLOSED, status="open", reason=None, exposure_remaining="0.1"))
        self.assertEqual(expected_program_status(opened.policy, opened.nomination), (None, None))
        self.assertTrue(any("不得封存 skipped_by_program" in e for e in opened.validator().validate(opened.program(STATUS_SKIPPED, ["x"]))))
        unbound = self.world(None)
        del unbound.policy["position_sizing"]["agent_swap"]["quality_dip"]["ladder_state"]
        with self.assertRaises(DecisionToolError):
            expected_program_status(unbound.policy, unbound.nomination)


if __name__ == "__main__":
    unittest.main()
