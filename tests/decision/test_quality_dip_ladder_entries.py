"""抄底梯度 ``ladder.exposure_basis=quality_dip_fills``（C1，已實作、尚未開啟）：總抄底曝險與批數只計 quality_dip 成交。

覆蓋：

1. 閘門：``exposure_basis`` 值域、``ladder_entries`` 只用於該模式且只能由 pipeline 綁定（形狀、重複、null）；v1～v3 出現即拒絕。
2. 梯度狀態：缺席時與舊版逐位元相同（state 沒有新鍵、所有新建倉照舊計）；新模式只讀綁定清單；清單缺席、null、與持股或帳本不一致
   一律以 ``entries_unavailable`` 關閉；其他 thesis／exposure 新建倉不占額度、不讓隔天的批次間隔關閉。
3. 綁定：樣板預填即拒絕、沒有該模式原樣回傳、null 綁定。
4. 決策庫掃描（真實 ``DecisionRepository`` 封存）：只取已套用、approved、quality_dip 配對且 proposal 有買單與 AGENT_SWAP_FILL、仍持有且
   持有起點不早於該 run 截止日的股票；任何讀取失敗回 None。
5. Finalizer：``validate_ladder_state`` 竄改清單（刪筆、改日期、加未持有的股票、未綁定）一律拒絕；提名簡報說明只計抄底成交。
6. 配置回歸（真實 ``AllocationOrderEngine``）：舊口徑下其他目的的新建倉讓梯度關閉；新模式梯度開啟，兩組 quality_dip 成交。
"""

import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.decision import DecisionToolError
from etf_agent.decision.decline_depth import (
    AGENT_SWAP_FILL_FLAG, build_decline_depth_facts, build_ladder_state, decline_depth_brief, scan_quality_dip_entries,
    validate_ladder_state, DECLINE_DEPTH_FACTS_KEY,
)
from etf_agent.decision.exposure import holding_weights
from etf_agent.decision.finalization import DecisionRepository
from etf_agent.decision.quality_dip_swap import (
    LADDER_CLOSED_REASONS, bind_quality_dip_ladder_entries, ladder_counts_quality_dip_fills_only, validate_quality_dip_swap_gate,
)

from test_quality_dip_ladder import (
    DAYS, FALL, LADDER, SINCE_OLD, TRADE_DATE, V4, make_bundle, policy_for, uptrend_then, weekdays,
)
from test_quality_dip_swap import quality_sizing

FILLS = dict(LADDER, exposure_basis="quality_dip_fills")
GATE = dict(V4, ladder=FILLS)
WIDE = dict(FILLS, max_dip_exposure="0.9", pause_after_further_drop="0.9")


def entry(symbol, day_index, run_id="decision-20261106T000000Z"):
    return {"symbol": symbol, "entry_date": weekdays(TRADE_DATE, DAYS)[day_index], "run_id": run_id}


def with_entries(bundle, since, entries, ladder=None, turn="no_turn_evidence", **extra):
    gate = dict(GATE if ladder is None else dict(V4, ladder=ladder), **extra)
    policy = policy_for(bundle, since, gate=gate, turn=turn)
    if entries is not ...:
        policy["position_sizing"]["agent_swap"]["quality_dip"]["ladder_entries"] = entries
    return policy


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

    def test_exposure_basis_is_optional_and_only_accepts_the_known_value(self):
        self.assertEqual(self.v4(), [])
        self.assertEqual(self.v4(ladder=FILLS), [])
        for bad in ("all_positions", "", 1, None):
            self.assertTrue(self.v4(ladder=dict(LADDER, exposure_basis=bad)), bad)

    def test_ladder_entries_need_the_mode_and_have_a_strict_shape(self):
        good = [entry("B.TW", 92)]
        self.assertEqual(self.v4(ladder=FILLS, ladder_entries=good), [])
        self.assertEqual(self.v4(ladder=FILLS, ladder_entries=None), [])  # null＝決策庫讀不到
        self.assertEqual(self.v4(ladder=FILLS, ladder_entries=[]), [])
        self.assertTrue(self.v4(ladder_entries=good))  # 沒有該模式不得有清單
        for bad in (
            "x", [{"symbol": "B.TW"}], [dict(good[0], extra="x")], [dict(good[0], entry_date="2026-13-40")],
            [dict(good[0], run_id="")], good + good, ["B.TW"],
        ):
            self.assertTrue(self.v4(ladder=FILLS, ladder_entries=bad), bad)

    def test_older_versions_refuse_the_new_fields(self):
        for version in ("v1", "v2", "v3"):
            self.assertTrue(self.problems(version, ladder_entries=[]), version)

    def test_the_new_reason_exists_and_the_mode_flag_is_false_by_default(self):
        self.assertIn("entries_unavailable", LADDER_CLOSED_REASONS)
        self.assertFalse(ladder_counts_quality_dip_fills_only({"agent_swap": {"quality_dip": dict(V4)}}))
        self.assertFalse(ladder_counts_quality_dip_fills_only({}))
        self.assertTrue(ladder_counts_quality_dip_fills_only({"agent_swap": {"quality_dip": dict(GATE)}}))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.bundle = make_bundle(uptrend_then(FALL))
        # B、C 是高點之後的新建倉（不同日），但都不是 quality_dip 成交。
        self.since = {"A.TW": 50, "B.TW": 92, "C.TW": 95}

    def state(self, entries, since=None, ladder=None, **kwargs):
        policy = with_entries(self.bundle, since or self.since, entries, ladder=ladder, **kwargs)
        return build_ladder_state(self.bundle, policy, "no_turn_evidence"), policy

    def test_absent_basis_is_bitwise_the_old_behaviour(self):
        plain = dict(WIDE)
        plain.pop("exposure_basis")
        old = build_ladder_state(self.bundle, policy_for(self.bundle, self.since, gate=dict(V4, ladder=plain)), "no_turn_evidence")
        self.assertNotIn("exposure_basis", old)  # state 逐鍵與舊版相同
        self.assertEqual(old["tranches"], 2)  # 舊口徑：所有新建倉都算
        self.assertEqual(old["last_tranche_date"], weekdays(TRADE_DATE, DAYS)[95])
        # 缺席時即使 Policy 帶了（不該有的）清單也完全不讀。
        polluted = policy_for(self.bundle, self.since, gate=dict(V4, ladder=plain))
        polluted["position_sizing"]["agent_swap"]["quality_dip"]["ladder_entries"] = []
        self.assertEqual(build_ladder_state(self.bundle, polluted, "no_turn_evidence"), old)

    def test_only_quality_dip_fills_count_toward_exposure_tranches_and_the_interval(self):
        state, _ = self.state([], ladder=WIDE)
        self.assertEqual((state["status"], state["reason"]), ("open", None))
        self.assertEqual((state["tranches"], Decimal(state["exposure_used"])), (0, Decimal(0)))
        self.assertIsNone(state["last_tranche_date"])
        self.assertEqual(Decimal(state["exposure_remaining"]), Decimal("0.9"))
        self.assertEqual(state["exposure_basis"], "quality_dip_fills")
        # 同樣的持股在舊口徑下，最近一批在 2 個交易日內：關閉。
        plain = dict(WIDE, min_days_between_tranches=3)
        plain.pop("exposure_basis")
        closed = build_ladder_state(self.bundle, policy_for(self.bundle, {"A.TW": 50, "B.TW": 92, "C.TW": 99}, gate=dict(V4, ladder=plain)), "no_turn_evidence")
        self.assertEqual((closed["status"], closed["reason"]), ("closed", "tranche_interval"))
        new, _ = self.state([], since={"A.TW": 50, "B.TW": 92, "C.TW": 99}, ladder=dict(WIDE, min_days_between_tranches=3))
        self.assertEqual(new["status"], "open")

    def test_a_bound_fill_counts_with_the_same_formulas(self):
        state, _ = self.state([entry("B.TW", 92)], ladder=WIDE)
        weights, _, _ = holding_weights(self.bundle)
        self.assertEqual(state["tranches"], 1)
        self.assertEqual(state["last_tranche_date"], weekdays(TRADE_DATE, DAYS)[92])
        self.assertEqual(Decimal(state["exposure_used"]), weights["B.TW"].quantize(Decimal("0.000001")))
        caps = dict(WIDE, max_dip_exposure=format(Decimal(state["exposure_used"]) - Decimal("0.00001"), "f"))
        full, _ = self.state([entry("B.TW", 92)], ladder=caps)
        self.assertEqual((full["status"], full["reason"]), ("closed", "exposure_full"))
        interval, _ = self.state([entry("C.TW", 95)], ladder=dict(WIDE, min_days_between_tranches=10))
        self.assertEqual((interval["status"], interval["reason"]), ("closed", "tranche_interval"))
        full_tranches, _ = self.state([entry("B.TW", 92), entry("C.TW", 95)], ladder=dict(WIDE, max_tranches=2))
        self.assertEqual((full_tranches["status"], full_tranches["reason"]), ("closed", "tranches_full"))

    def test_every_unavailable_or_inconsistent_list_closes_with_entries_unavailable(self):
        for label, entries in (
            ("absent", ...), ("null", None), ("not_held", [entry("Z.TW", 92)]),
            ("date_mismatch", [entry("B.TW", 93)]), ("duplicate", [entry("B.TW", 92), entry("B.TW", 92)]),
        ):
            state, _ = self.state(entries, ladder=WIDE)
            self.assertEqual((state["status"], state["reason"]), ("closed", "entries_unavailable"), label)
            self.assertEqual(Decimal(state["exposure_remaining"]), 0, label)

    def test_missing_held_since_still_closes_with_its_own_reason_first(self):
        policy = with_entries(self.bundle, {"A.TW": 50}, [entry("A.TW", 50)], ladder=WIDE)
        state = build_ladder_state(self.bundle, policy, "no_turn_evidence")
        self.assertEqual(state["reason"], "held_since_unavailable")

    def test_the_nomination_brief_says_only_quality_dip_fills_count(self):
        policy = with_entries(self.bundle, self.since, [], ladder=WIDE)
        facts = build_decline_depth_facts(self.bundle, policy)
        state = build_ladder_state(self.bundle, policy, "turn_unconfirmed", facts)
        brief = decline_depth_brief(facts, state, build_ladder_state(self.bundle, policy, "no_turn_evidence", facts))
        self.assertIn("只計 purpose=quality_dip 的成交", brief["ladder_note"])
        old = policy_for(self.bundle, self.since)
        old_facts = build_decline_depth_facts(self.bundle, old)
        old_state = build_ladder_state(self.bundle, old, "turn_unconfirmed", old_facts)
        self.assertNotIn("只計 purpose=quality_dip 的成交", decline_depth_brief(old_facts, old_state, old_state)["ladder_note"])


class BindingTests(unittest.TestCase):
    def policy(self, gate):
        return {"position_sizing": {"agent_swap": {"version": "v2", "quality_dip": copy.deepcopy(gate)}}}

    def test_the_binding_writes_the_list_and_refuses_a_prefilled_template(self):
        entries = [entry("B.TW", 92)]
        bound = bind_quality_dip_ladder_entries(self.policy(GATE), entries)
        self.assertEqual(bound["position_sizing"]["agent_swap"]["quality_dip"]["ladder_entries"], entries)
        self.assertIsNone(bind_quality_dip_ladder_entries(self.policy(GATE), None)["position_sizing"]["agent_swap"]["quality_dip"]["ladder_entries"])
        with self.assertRaises(DecisionToolError):
            bind_quality_dip_ladder_entries(self.policy(dict(GATE, ladder_entries=[])), entries)

    def test_without_the_mode_the_policy_is_returned_unchanged(self):
        for policy in (self.policy(V4), {"position_sizing": {}}, {}):
            self.assertEqual(bind_quality_dip_ladder_entries(policy, [entry("B.TW", 92)]), policy)

    def test_the_input_policy_is_not_mutated(self):
        policy = self.policy(GATE)
        before = copy.deepcopy(policy)
        bind_quality_dip_ladder_entries(policy, [])
        self.assertEqual(policy, before)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundle = make_bundle(uptrend_then(FALL))
        self.bundle["account_snapshot"]["account_id"] = "acct-1"
        self.days = weekdays(TRADE_DATE, DAYS)
        self.since = {"A.TW": 50, "B.TW": 92, "C.TW": 95}
        self.policy = with_entries(self.bundle, self.since, ..., ladder=WIDE)
        self.run_id = "decision-%sT000000Z" % self.days[91].replace("-", "")

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, run_id=None, pairs=None, flags=("B.TW", "C.TW"), buys=("B.TW", "C.TW"), status="approved", account="acct-1", day=91, decision_proposal=None):
        run_id = run_id or self.run_id
        cutoff = "%sT12:00:00+00:00" % self.days[day]
        pairs = pairs if pairs is not None else [
            {"buy_symbol": "B.TW", "sell_symbol": "X.TW", "purpose": "quality_dip", "size": "large"},
            {"buy_symbol": "C.TW", "sell_symbol": "Y.TW", "purpose": "thesis", "size": "small"},
        ]
        bundle = {"decision_cutoff": cutoff, "account_snapshot": {"account_id": account}}
        proposal = {
            "proposal_id": "proposal:%s" % run_id,
            "constraint_flags": [{"symbol": symbol, "code": AGENT_SWAP_FILL_FLAG} for symbol in flags],
            "order_proposal": {"orders": [{"symbol": symbol, "side": "buy"} for symbol in buys]},
        }
        decision = {"status": status, "decision_cutoff": cutoff, "proposal_id": decision_proposal or proposal["proposal_id"]}
        policy = {"position_sizing": {"agent_swap": {"version": "v2", "nominated_pairs": pairs}}}
        DecisionRepository(self.root).save(
            run_id, {"decision": decision, "decision_input": bundle, "policy": policy, "proposal": proposal}, day=self.days[day])
        return run_id

    def scan(self, applied, policy=None, root=...):
        return scan_quality_dip_entries(self.root if root is ... else root, self.bundle, policy or self.policy, applied)

    def test_only_quality_dip_fills_that_are_still_held_are_returned(self):
        run_id = self.archive()
        self.assertEqual(self.scan([run_id]), [entry("B.TW", 92, run_id)])  # C 是 thesis 配對，不算

    def test_a_pair_without_a_fill_flag_or_a_buy_order_is_not_a_fill(self):
        self.assertEqual(self.scan([self.archive(flags=())]), [])
        self.assertEqual(self.scan([self.archive(run_id="decision-20261107T000001Z", buys=())]), [])

    def test_positions_no_longer_held_or_held_since_before_the_run_are_dropped(self):
        run_id = self.archive()
        gone = with_entries(self.bundle, {"A.TW": 50, "C.TW": 95}, ..., ladder=WIDE)
        self.bundle["account_snapshot"]["positions"] = [p for p in self.bundle["account_snapshot"]["positions"] if p["symbol"] != "B.TW"]
        self.assertEqual(self.scan([run_id], gone), [])
        self.bundle = make_bundle(uptrend_then(FALL))
        self.bundle["account_snapshot"]["account_id"] = "acct-1"
        older = with_entries(self.bundle, {"A.TW": 50, "B.TW": 60, "C.TW": 95}, ..., ladder=WIDE)  # B 在該 run 之前就持有
        self.assertEqual(self.scan([run_id], older), [])

    def test_runs_that_are_not_approved_other_accounts_or_not_earlier_are_skipped(self):
        self.assertEqual(self.scan([self.archive(status="rejected")]), [])
        self.assertEqual(self.scan([self.archive(run_id="decision-20261107T000002Z", account="other")]), [])
        late = self.archive(run_id="decision-20261109T000003Z", day=99)  # cutoff 不早於本次
        self.assertEqual(self.scan([late]), [])

    def test_unreadable_unverifiable_or_unknown_runs_return_none(self):
        run_id = self.archive()
        self.assertIsNone(self.scan([run_id], root=None))
        self.assertIsNone(self.scan(None))  # 帳本沒給已套用的 run 清單
        self.assertIsNone(self.scan(["decision-20261101T000000Z"]))  # 決策庫沒有這個 run
        self.assertIsNone(self.scan([run_id], root=self.root / "missing"))
        path = next(self.root.rglob("proposal.json"))
        path.write_text(path.read_text(encoding="utf-8").replace("buy", "sell"), encoding="utf-8")  # manifest 雜湊不符
        self.assertIsNone(self.scan([run_id]))

    def test_a_proposal_that_is_not_the_decisions_is_refused(self):
        self.assertIsNone(self.scan([self.archive(decision_proposal="proposal:other")]))

    def test_no_held_since_binding_returns_none(self):
        run_id = self.archive()
        self.assertIsNone(self.scan([run_id], policy_for(self.bundle, {"A.TW": 50}, gate=GATE)))

    def test_scan_result_feeds_the_state_end_to_end(self):
        run_id = self.archive()
        entries = self.scan([run_id])
        bound = bind_quality_dip_ladder_entries(self.policy, entries)
        state = build_ladder_state(self.bundle, bound, "no_turn_evidence")
        self.assertEqual((state["status"], state["tranches"]), ("open", 1))
        unreadable = bind_quality_dip_ladder_entries(self.policy, self.scan(None))
        closed = build_ladder_state(self.bundle, unreadable, "no_turn_evidence")
        self.assertEqual((closed["status"], closed["reason"]), ("closed", "entries_unavailable"))


class FinalizerTests(unittest.TestCase):
    def setUp(self):
        self.bundle = make_bundle(uptrend_then(FALL))
        self.since = {"A.TW": 50, "B.TW": 92, "C.TW": 95}
        self.policy = with_entries(self.bundle, self.since, [entry("B.TW", 92)], ladder=WIDE)
        self.facts = build_decline_depth_facts(self.bundle, self.policy)
        state = build_ladder_state(self.bundle, self.policy, "no_turn_evidence", self.facts)
        self.policy["position_sizing"]["agent_swap"]["quality_dip"]["ladder_state"] = state

    def errors(self, policy):
        problems: list = []
        validate_ladder_state(self.bundle, policy, {DECLINE_DEPTH_FACTS_KEY: self.facts}, problems)
        return problems

    def forged(self, **changes):
        policy = copy.deepcopy(self.policy)
        policy["position_sizing"]["agent_swap"]["quality_dip"].update(changes)
        return policy

    def test_a_consistent_binding_passes(self):
        self.assertEqual(self.errors(self.policy), [])

    def test_tampering_with_the_entries_is_rejected_even_if_the_state_is_rebuilt_from_them(self):
        for label, entries in (
            ("dropped", []), ("date", [entry("B.TW", 93)]), ("not_held", [entry("B.TW", 92), entry("Z.TW", 94)]),
        ):
            forged = self.forged(ladder_entries=entries)
            self.assertTrue(self.errors(forged), label)
            if label == "dropped":
                continue  # 單純刪筆而連 state 一起重算，Finalizer 不重掃決策庫就看不出來（信任層級同 reentry_cooldown）；見下一個測試的說明
            rebuilt = build_ladder_state(self.bundle, forged, "no_turn_evidence", self.facts)
            forged["position_sizing"]["agent_swap"]["quality_dip"]["ladder_state"] = rebuilt
            self.assertTrue(self.errors(forged), "%s（連 ladder_state 一起重算也不行）" % label)

    def test_dropping_a_fill_so_the_state_stays_open_is_rejected(self):
        forged = self.forged(ladder_entries=[])
        self.assertTrue(self.errors(forged))  # state 與重算不一致

    def test_an_unbound_list_is_rejected(self):
        forged = copy.deepcopy(self.policy)
        del forged["position_sizing"]["agent_swap"]["quality_dip"]["ladder_entries"]
        self.assertTrue(any("尚未綁定" in message for message in self.errors(forged)))

    def test_a_null_list_is_consistent_only_with_the_closed_state(self):
        forged = self.forged(ladder_entries=None)
        self.assertTrue(self.errors(forged))  # 原本 open 的 state 與重算（closed）不一致
        forged["position_sizing"]["agent_swap"]["quality_dip"]["ladder_state"] = build_ladder_state(
            self.bundle, forged, "no_turn_evidence", self.facts)
        self.assertEqual(self.errors(forged), [])  # fail-closed 的 null 本身合法
        self.assertEqual(forged["position_sizing"]["agent_swap"]["quality_dip"]["ladder_state"]["reason"], "entries_unavailable")

    def test_the_exposure_basis_cannot_be_dropped_to_change_the_counting(self):
        forged = copy.deepcopy(self.policy)
        ladder = forged["position_sizing"]["agent_swap"]["quality_dip"]["ladder"]
        ladder.pop("exposure_basis")
        self.assertTrue(self.errors(forged))  # 舊口徑重算出的 state 與綁定的不同


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "artifacts" / "decisions" / "2026-11-11" / "decision-20261111T001308Z"
QUALITY_PAIRS = [
    {"buy_symbol": "1590.TW", "sell_symbol": "1216.TW", "purpose": "quality_dip", "size": "large"},
    {"buy_symbol": "2059.TW", "sell_symbol": "1402.TW", "purpose": "quality_dip", "size": "large"},
]
EXPOSURE_PAIRS = [
    {"buy_symbol": "2408.TW", "sell_symbol": "2890.TW", "purpose": "exposure", "size": "medium"},
    {"buy_symbol": "6239.TW", "sell_symbol": "3034.TW", "purpose": "exposure", "size": "medium"},
]
PROBE = {
    "exposure_view": "probe", "offense_basis": ["washout", "breadth_thrust"], "failure_condition": "兩個交易日內交易池收盤跌破前低",
    "defensive_reconciliation": "現金仍守防守格點附近，只小量試單補回參與度",
}


class Scenario1111Tests(unittest.TestCase):
    """2026-11-11 的封存輸入（真實 Snapshot、帳本持有起點、交易 Agent 意圖）＋目前的正式樣板，只在記憶體內加 exposure_basis。

    封存或樣板不在、或樣板不是 quality_dip v4＋offense 時略過（這是情境回歸，不是樣板的驗證）。"""

    @classmethod
    def setUpClass(cls):
        import json

        if not ARCHIVE.is_dir():
            raise unittest.SkipTest("沒有 2026-11-11 的決策封存")
        load = lambda name: json.loads((ARCHIVE / ("%s.json" % name)).read_text(encoding="utf-8"))
        cls.bundle, cls.old, cls.intent, cls.debate, cls.team = (
            load("decision_input"), load("policy"), load("intent"), load("debate"), load("team_inputs"))
        cls.template = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        sizing = cls.template.get("position_sizing", {})
        quality = (sizing.get("agent_swap") or {}).get("quality_dip") or {}
        if quality.get("version") != "v4" or "offense" not in sizing:
            raise unittest.SkipTest("目前的樣板不是 quality_dip v4＋offense")

    def policy(self, fills_only, pairs, patch):
        from etf_agent.decision import apply_trade_decision, decision_policy_sha256
        from etf_agent.decision.selection_ranking import apply_selection_ranking

        from test_daily_pipeline import without_stop_exit

        # 情境回歸只比 ladder 曝險計法：stop_exit（正式設定 2026-11-11 17:49 起）會把封存持股中的 3034／8046 程式兜底賣出，
        # 釋出資金先流向 2330 高信心加碼、擠掉換股買進，與本測試要比的曝險計法無關；明確移除它並把 position_review 降回 v1。
        template = without_stop_exit(copy.deepcopy(self.template))
        ladder = template["position_sizing"]["agent_swap"]["quality_dip"]["ladder"]
        ladder.pop("exposure_basis", None)
        # 情境回歸只比 ladder 曝險計法：明確移除 offense 的兩個選配子欄位（正式設定已開，但它們的 anchor 旗標需要前次 run，這裡沒有）。
        for field in ("thesis_size_cap", "cash_glide"):
            template["position_sizing"]["offense"].pop(field, None)
        if fills_only:
            ladder["exposure_basis"] = "quality_dip_fills"
        policy = copy.deepcopy(self.old)
        for key in template:
            if key not in ("position_sizing", "drawdown_control", "available_at"):
                policy[key] = copy.deepcopy(template[key])
        control = dict(self.old["drawdown_control"])
        control.update(template["drawdown_control"])
        policy["drawdown_control"] = control
        old_sizing, sizing = self.old["position_sizing"], copy.deepcopy(template["position_sizing"])
        rotation = sizing["exposure_rotation"]
        rotation["held_since_by_symbol"] = old_sizing["exposure_rotation"].get("held_since_by_symbol")
        if "holding_days_by_symbol" in old_sizing["exposure_rotation"]:
            rotation["holding_days_by_symbol"] = old_sizing["exposure_rotation"]["holding_days_by_symbol"]
        for key in ("cash_ratio_range", "reentry_cooldown", "drawdown_derisk", "rotation"):
            sizing[key] = copy.deepcopy(old_sizing[key])
        for key in ("thesis_hint_symbols", "verdicts_ref"):
            if key in old_sizing["agent_swap"]:
                sizing["agent_swap"][key] = old_sizing["agent_swap"][key]
        sizing["agent_swap"]["nominated_pairs"] = copy.deepcopy(pairs)
        sizing["agent_swap"]["nomination_ref"] = old_sizing["agent_swap"]["nomination_ref"]
        policy["position_sizing"] = sizing
        policy.pop("content_sha256", None)
        if fills_only:
            policy = bind_quality_dip_ladder_entries(policy, [])  # 歷史上沒有 quality_dip 成交
        policy["content_sha256"] = decision_policy_sha256(policy)
        stance = {key: value for key, value in dict(self.team["cash_stance"], **patch).items() if value is not None}
        bound = apply_trade_decision(policy, self.intent, stance, self.bundle, self.debate, self.team.get("prior_decision_context"))
        bound = apply_selection_ranking(bound, self.team["selection_ranking"])
        quality = bound["position_sizing"]["agent_swap"]["quality_dip"]
        quality["ruling_ref"] = {"ruling_id": "quality-dip-ruling:scenario", "sha256": "0" * 64, "status": "completed"}
        quality["rulings"] = ["approve"] * sum(1 for pair in pairs if pair["purpose"] == "quality_dip")
        bound["content_sha256"] = decision_policy_sha256(bound)
        return bound

    def run_engine(self, fills_only, pairs, patch=None):
        from etf_agent.decision import AllocationOrderEngine, DecisionPolicyValidator

        policy = self.policy(fills_only, pairs, patch or {})
        self.assertEqual(DecisionPolicyValidator(self.bundle).validate(policy), [])
        return policy, AllocationOrderEngine(self.bundle, policy).run(self.intent)

    def test_today_every_new_position_closes_the_ladder_but_the_new_basis_keeps_it_open(self):
        today, body = self.run_engine(False, QUALITY_PAIRS)
        state = today["position_sizing"]["agent_swap"]["quality_dip"]["ladder_state"]
        self.assertEqual((state["status"], state["reason"]), ("closed", "tranche_interval"))
        self.assertFalse([o for o in body["order_proposal"]["orders"] if o["side"] == "buy" and o["symbol"] in ("1590.TW", "2059.TW")])
        new, _ = self.run_engine(True, QUALITY_PAIRS)
        state = new["position_sizing"]["agent_swap"]["quality_dip"]["ladder_state"]
        self.assertEqual((state["status"], state["reason"], state["tranches"]), ("open", None, 0))
        self.assertEqual((Decimal(state["exposure_used"]), Decimal(state["exposure_remaining"])), (Decimal(0), Decimal("0.12")))

    def assert_fills(self, body, pairs):
        orders = {(o["symbol"], o["side"]): o for o in body["order_proposal"]["orders"]}
        for pair in pairs:
            buy, sell = orders[(pair["buy_symbol"], "buy")], orders[(pair["sell_symbol"], "sell")]
            self.assertLessEqual(Decimal(str(buy["gross_amount"])), Decimal(str(sell["gross_amount"])), pair)  # 買進不超過換出者賣款
            self.assertEqual(sell["intent"], "exit")

    def test_keep_defensive_fills_both_quality_dip_pairs_within_their_proceeds(self):
        _, body = self.run_engine(True, QUALITY_PAIRS)
        self.assert_fills(body, QUALITY_PAIRS)

    def test_probe_two_exposure_and_two_quality_dip_pairs_all_fill_together(self):
        _, body = self.run_engine(True, EXPOSURE_PAIRS + QUALITY_PAIRS, PROBE)
        self.assert_fills(body, QUALITY_PAIRS)
        orders = {(o["symbol"], o["side"]) for o in body["order_proposal"]["orders"]}
        for pair in EXPOSURE_PAIRS:
            self.assertIn((pair["buy_symbol"], "buy"), orders)
            self.assertIn((pair["sell_symbol"], "sell"), orders)


if __name__ == "__main__":
    unittest.main()
