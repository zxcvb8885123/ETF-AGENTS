"""換股提名（5c，agent_swap v1 影子）接入每日決策鏈與 team_inputs 2.9 的端到端測試。

整條鏈用正式樣板（只拿掉 fixture 沒有資料的 cash_stance_recency、再加 agent_swap v1）跑真實 pipeline 與真實 Finalizer：

- 影子 v1：提名被封存進 team_inputs 2.9、可被 DecisionResultValidator 重建；配置結果（訂單與旗標）與沒有閘門的對照組逐位相同。
- 沒有 agent_swap 的樣板：team_inputs 維持 2.8，不呼叫第三步。
- 降級：驗證用盡封存 exhausted、暫時性失敗封存 unavailable，其餘決策照常；續跑重現同一份提名、不再呼叫 Agent；
  安全拒絕（秘密外洩）仍整個停止。
- fail-closed：閘門與 team_inputs 版本不同步（雙向）、缺封存、提名或事實被改寫、v2 未綁定提名一律拒絕（v2 的配置見 test_agent_swap_v2_pipeline.py）。

夾具：``PipelineWorld`` 只有一檔持股 2330（持有 1000 股）與一檔新股 2317；把規則的 ``max_positions`` 調為 1 才會有名額缺口
（需要提名）。帳本 NAV 與持股歷史自建兩列（NAV 與帳戶現金配合調整），持有起點 2026-09-01，cutoff 為 2026-09-20，持有天數 19 天。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.agent_runner import AgentCall, DailyPipelineError, TransientAgentError
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline, _history_provider
from etf_agent.decision import DecisionFinalizer, DecisionRepository, DecisionResultValidator, decision_policy_sha256
from etf_agent.decision.finalization import SWAP_TEAM_FIELDS, SWAP_TEAM_VERSION, TEAM_FIELDS_BY_VERSION, build_team_inputs
from etf_agent.decision import DecisionToolError

from test_daily_pipeline import (
    BEAR, BULL, EVENT, FUNDAMENTAL, ROOT, STANCE, TECHNICAL, TRADE, FakeRunner, PipelineWorld, live_without_offense, live_policy_template, team_outputs,
)

GATE = {"version": "v1", "max_swaps_per_run": 2, "min_holding_days": 5}
NAV = [
    {"run_id": "ledger-1", "as_of": "2026-09-01T06:00:00+00:00", "nav": "1160000"},
    {"run_id": "ledger-2", "as_of": "2026-09-02T06:00:00+00:00", "nav": "1161000"},
]
HOLDINGS = [
    {"run_id": "ledger-1", "as_of": "2026-09-01T06:00:00+00:00", "symbols": ["2330.TW"]},
    {"run_id": "ledger-2", "as_of": "2026-09-02T06:00:00+00:00", "symbols": ["2330.TW"]},
]
GOOD_PAIR = {
    "buy_symbol": "2317.TW", "sell_symbol": "2330.TW", "buy_basis_ids": ["bull-2317-1"], "sell_basis_ids": ["bear-2330-1"],
    "reason": "新股的多頭論點成立；持股的空頭論點已被採納，兩邊依據各自成立。",
}
NOMINATION = {"nominations": [GOOD_PAIR], "thesis_hint_responses": []}
BAD_NOMINATION = {"nominations": [dict(GOOD_PAIR, buy_symbol="9999.TW")], "thesis_hint_responses": []}


def template(gate=GATE):
    """正式樣板（只拿掉 fixture 沒有資料的 cash_stance_recency）；``gate`` 為 None 時不加 agent_swap（對照組）。"""
    value = live_without_offense()
    value["position_sizing"].pop("cash_stance_recency")
    value["position_sizing"].pop("agent_swap", None)  # 正式樣板已含 agent_swap：先移除，再依 gate 明確加回
    value["position_sizing"].pop("exposure_view", None)  # 正式樣板已含 exposure_view（純紀錄）；整條鏈由 test_exposure_view 覆蓋
    if gate is not None:
        value["position_sizing"]["agent_swap"] = copy.deepcopy(gate)
    return value


def trade_with_bear_basis():
    """2330 續抱但採納空頭論點（換出者必須有已採納的空頭依據）；hold 也要給信心（exposure_rotation）。"""
    trade = copy.deepcopy(TRADE)
    trade["items"][1].update(conviction="medium", adopted_claim_ids=["bear-2330-1"], rejected_claim_ids=["bull-2330-1"])
    return trade


def outputs(*swap, **overrides):
    value = team_outputs(
        trader_b0=[trade_with_bear_basis()],
        cash_stance=[dict(STANCE, cash_grade="middle", turn_assessment="no_turn_evidence")],
    )
    if swap:
        value["swap_nominator"] = list(swap)
    value.update(overrides)
    return value


class Harness:
    """一個暫存世界；``tight=True`` 把 max_positions 調為 1，讓 2317 被名額卡住而需要提名。"""

    def __init__(self, directory, gate=GATE, tight=True):
        self.world = PipelineWorld(Path(directory), approved_status=True, template=template(gate))
        # 帳戶現金調為 100 萬（NAV 116.4 萬）：一對換股的成交額約 2 成，不超過換手上限 3 成。
        account = json.loads(self.world.paths["account"].read_text(encoding="utf-8"))
        account.update(cash="1000000", settled_cash="1000000", nav="1164000")
        self.world.paths["account"].write_text(json.dumps(account, ensure_ascii=False), encoding="utf-8")
        if tight:
            rules = json.loads(self.world.paths["rules"].read_text(encoding="utf-8"))
            rules["max_positions"] = 1
            self.world.paths["rules"].write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")

    def run(self, runner, resume=False, **kwargs):
        world = self.world
        pipeline = DailyDecisionPipeline(
            ROOT, world.run_dir, runner, world.repository, log=lambda _: None, resume=resume, sleep=lambda _: None, **kwargs)
        result = pipeline.run(
            world.paths["snapshot"], world.paths["account"], world.paths["trading_status"], world.paths["rules"],
            world.paths["database"], world.paths["template"], world.paths["sectors"], "decision-fixture",
            nav_history=NAV, holdings_history=HOLDINGS,
        )
        return result, pipeline

    def artifacts(self):
        run_dir = DecisionRepository(self.world.repository).verify("decision-fixture")
        names = ("decision_input", "policy", "momentum", "debate", "intent", "team_inputs", "proposal", "scenario",
                 "guard", "risk_review", "revision_history", "decision")
        return {name: json.loads((run_dir / ("%s.json" % name)).read_text(encoding="utf-8")) for name in names}

    def validate(self, art, **replace):
        art = dict(art, **replace)
        return DecisionResultValidator(
            art["decision_input"], art["policy"], art["momentum"], art["debate"], art["intent"], art["team_inputs"],
            self.world.repository, _history_provider(NAV, HOLDINGS),
        ).validate(art["proposal"], art["scenario"], art["guard"], art["risk_review"], art["revision_history"], art["decision"])


    def rebuild_errors(self, art, **replace):
        """Finalizer 的逐項重建錯誤（DecisionResultValidator 只回「與完整重建結果不一致」，看不到原因）。"""
        art = dict(art, **replace)
        result = DecisionFinalizer(
            art["decision_input"], art["policy"], art["momentum"], art["debate"], art["intent"], art["team_inputs"],
            self.world.repository, _history_provider(NAV, HOLDINGS),
        ).run(art["proposal"], art["scenario"], art["guard"], art["risk_review"], art["revision_history"])
        return result["errors"] if result["status"] == "rejected" else []


def flags(proposal):
    return sorted((flag["symbol"], flag["code"], flag.get("detail")) for flag in proposal["constraint_flags"])


def sealed(policy, mutate):
    value = copy.deepcopy(policy)
    mutate(value)
    value["content_sha256"] = decision_policy_sha256(value)
    return value


class ShadowRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        (base / "control").mkdir()
        (base / "gated").mkdir()
        cls.control = Harness(base / "control", gate=None)
        cls.control_result, _ = cls.control.run(FakeRunner(outputs()))
        cls.shadow = Harness(base / "gated")
        cls.runner = FakeRunner(outputs(NOMINATION))
        cls.shadow_result, cls.shadow_pipeline = cls.shadow.run(cls.runner)
        cls.control_art = cls.control.artifacts()
        cls.shadow_art = cls.shadow.artifacts()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_the_nomination_is_called_once_after_ranking_and_sealed_in_team_inputs_2_9(self):
        self.assertEqual(self.shadow_result.status, "completed", self.shadow_result.errors)
        names = [name for name, _ in self.runner.prompts]
        self.assertEqual(names.count("swap_nominator"), 1)
        self.assertLess(names.index("trader_b0"), names.index("swap_nominator"))
        self.assertLess(names.index("swap_nominator"), names.index("cash_stance"))
        team = self.shadow_art["team_inputs"]
        self.assertEqual(team["schema_version"], SWAP_TEAM_VERSION)
        self.assertEqual(set(team) - {"release_order", "dip_facts", "held_weight_facts"}, SWAP_TEAM_FIELDS)  # 選配鍵（正式樣板已開 v5／quality_dip）不在版本白名單
        nomination = team["swap_nomination"]
        self.assertEqual((nomination["status"], len(nomination["nominations"])), ("completed", 1))
        self.assertEqual(nomination["nominations"][0]["sell_symbol"], "2330.TW")
        self.assertEqual(json.loads((self.shadow.world.run_dir / "swap_nomination.json").read_text(encoding="utf-8")), nomination)
        self.assertTrue((self.shadow.world.run_dir / "brief_swap_nomination.json").exists())

    def test_the_brief_does_not_say_it_is_a_shadow_period(self):
        prompt = next(prompt for name, prompt in self.runner.prompts if name == "swap_nominator")
        brief = (self.shadow.world.run_dir / "brief_swap_nomination.json").read_text(encoding="utf-8")
        for text in (prompt, brief):
            self.assertNotIn("影子", text)
            self.assertNotIn("shadow", text.lower())
        self.assertIn("swap-nomination.md", prompt)

    def test_allocation_is_bit_for_bit_unchanged_by_the_shadow_gate(self):
        control, shadow = self.control_art, self.shadow_art
        self.assertEqual(self.control_result.status, "completed", self.control_result.errors)
        self.assertEqual(control["team_inputs"]["schema_version"], "2.8")
        self.assertNotIn("swap_nomination", control["team_inputs"])
        self.assertNotEqual(control["policy"]["content_sha256"], shadow["policy"]["content_sha256"])  # 閘門在 policy 內
        self.assertEqual(control["decision"]["status"], shadow["decision"]["status"])
        self.assertEqual(control["decision"]["orders"], shadow["decision"]["orders"])
        self.assertEqual(control["proposal"]["order_proposal"], shadow["proposal"]["order_proposal"])
        self.assertEqual(control["proposal"]["allocation_proposal"], shadow["proposal"]["allocation_proposal"])
        self.assertEqual(flags(control["proposal"]), flags(shadow["proposal"]))
        for _, code, _ in flags(shadow["proposal"]):
            self.assertFalse(code.startswith("AGENT_SWAP"), code)
        self.assertEqual(control["intent"]["items"], shadow["intent"]["items"])  # 交易 Agent 的原意圖沒有被改寫

    def test_the_sealed_shadow_run_is_rebuilt_by_the_finalizer(self):
        self.assertEqual(self.shadow_art["decision"]["errors"], [])
        self.assertEqual(self.shadow.validate(self.shadow_art), [])
        self.assertEqual(self.control.validate(self.control_art), [])  # 2.8 的舊 run 照舊重建

    def test_agent_calls_record_the_nomination_step(self):
        self.assertTrue(any(call["name"] == "swap_nominator" for call in self.shadow_pipeline.calls))
        self.assertIn("swap_nomination", self.shadow_result.stage_durations)

    # -- fail-closed：Finalizer
    def assertRejected(self, errors, token):
        self.assertTrue(any(token in error for error in errors), (token, errors))

    def tampered_team(self, mutate):
        team = copy.deepcopy(self.shadow_art["team_inputs"])
        mutate(team)
        return team

    def test_missing_nomination_or_wrong_version_is_rejected(self):
        art = self.shadow_art
        cases = {
            "缺封存": lambda team: team.pop("swap_nomination"),
            "封存是 null": lambda team: team.update(swap_nomination=None),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                errors = self.shadow.rebuild_errors(art, team_inputs=self.tampered_team(mutate))
                self.assertTrue(errors, label)
        # 閘門開啟但 team_inputs 停在 2.8（拿掉提名）：雙向檢查。
        stuck = self.tampered_team(lambda team: (team.pop("swap_nomination"), team.update(schema_version="2.8")))
        self.assertRejected(self.shadow.rebuild_errors(art, team_inputs=stuck), "position_sizing.agent_swap 必須與 team_inputs 2.9 同時存在")

    def test_nomination_without_the_gate_in_the_policy_is_rejected(self):
        art = self.shadow_art
        gateless = sealed(art["policy"], lambda policy: policy["position_sizing"].pop("agent_swap"))
        self.assertRejected(self.shadow.rebuild_errors(art, policy=gateless), "position_sizing.agent_swap 必須與 team_inputs 2.9 同時存在")

    def test_a_tampered_nomination_is_rejected(self):
        art = self.shadow_art
        pair = lambda team: team["swap_nomination"]["nominations"][0]  # noqa: E731
        cases = {
            "換出股改成別檔": lambda team: pair(team).update(sell_symbol="2317.TW"),
            "依據改成不存在的識別碼": lambda team: pair(team).update(sell_basis_ids=["bear-9999-1"]),
            "狀態改寫成 not_needed": lambda team: team["swap_nomination"].update(status="not_needed"),
            "多出未允許欄位": lambda team: team["swap_nomination"].update(advice="買進"),
            "信封綁別份決策": lambda team: team["swap_nomination"].update(trade_decision_sha256="0" * 64),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                errors = self.shadow.rebuild_errors(art, team_inputs=self.tampered_team(mutate))
                self.assertRejected(errors, "swap_nomination")

    def test_facts_recomputed_from_the_inputs_must_match(self):
        art = self.shadow_art
        # 換入股進了出場冷卻名單：同一份提名不再符合重算的事實。
        blocked = sealed(art["policy"], lambda policy: policy["position_sizing"]["reentry_cooldown"].update(blocked_symbols=["2317.TW"]))
        self.assertRejected(self.shadow.rebuild_errors(art, policy=blocked), "不能作為換入股")
        # 持有天數下限拉高：換出股資格不成立。
        strict = sealed(art["policy"], lambda policy: policy["position_sizing"]["agent_swap"].update(min_holding_days=60))
        self.assertRejected(self.shadow.rebuild_errors(art, policy=strict), "不能作為換出股")
        # 交易決策被改成 2330 不是 hold：換出股資格與信封綁定都不成立。
        intent = copy.deepcopy(art["intent"])
        for item in intent["items"]:
            if item["symbol"] == "2330.TW":
                item["intent"] = "no_trade"
        self.assertRejected(self.shadow.rebuild_errors(art, intent=intent), "swap_nomination")

    def test_v2_policy_without_the_bound_pairs_stops_the_rebuild(self):
        art = self.shadow_art

        def to_v2(policy):
            sizing = policy["position_sizing"]
            sizing["agent_swap"]["version"] = "v2"
            sizing["rotation"].pop("forced_swap", None)  # v2 與舊換股路徑互斥
            sizing["exposure_rotation"].pop("conviction_gap_swap", None)

        v2 = sealed(art["policy"], to_v2)
        # 與 held_since 未綁定的前例相同：重建配置的驗證器讀到未綁定的 v2 時由引擎拋 DecisionToolError，沒有任何路徑能 approved。
        with self.assertRaisesRegex(DecisionToolError, "尚未綁定換股提名"):
            self.shadow.rebuild_errors(art, policy=v2)


class NotNeededTests(unittest.TestCase):
    def test_no_slot_gap_seals_not_needed_without_calling_the_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory, tight=False)
            runner = FakeRunner(outputs())
            result, _ = harness.run(runner)
            self.assertEqual(result.status, "completed", result.errors)
            self.assertNotIn("swap_nominator", [name for name, _ in runner.prompts])
            art = harness.artifacts()
            self.assertEqual(art["team_inputs"]["schema_version"], "2.9")
            self.assertEqual(art["team_inputs"]["swap_nomination"]["status"], "not_needed")
            self.assertEqual(harness.validate(art), [])
            # 需要提名時封存 not_needed 一律拒絕：把 not_needed 換成同形狀的程式封存仍須與重算一致，這裡改寫狀態。
            forged = copy.deepcopy(art["team_inputs"])
            forged["swap_nomination"]["status"] = "exhausted"
            self.assertTrue(harness.validate(art, team_inputs=forged))

    def test_a_policy_without_agent_swap_keeps_team_inputs_2_8_and_never_runs_the_step(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory, gate=None)
            runner = FakeRunner(outputs())
            result, _ = harness.run(runner)
            self.assertEqual(result.status, "completed", result.errors)
            self.assertNotIn("swap_nominator", [name for name, _ in runner.prompts])
            self.assertFalse((harness.world.run_dir / "swap_nomination.json").exists())
            self.assertEqual(harness.artifacts()["team_inputs"]["schema_version"], "2.8")


class DegradedRunTests(unittest.TestCase):
    def baseline_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory, gate=None)
            result, _ = harness.run(FakeRunner(outputs()))
            self.assertEqual(result.status, "completed", result.errors)
            art = harness.artifacts()
            return art["decision"]["status"], art["decision"]["orders"], flags(art["proposal"])

    def test_three_invalid_outputs_seal_exhausted_and_the_rest_of_the_decision_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory)
            runner = FakeRunner(outputs(BAD_NOMINATION, BAD_NOMINATION, BAD_NOMINATION))
            result, pipeline = harness.run(runner)
            self.assertEqual(result.status, "completed", result.errors)
            self.assertEqual([name for name, _ in runner.prompts].count("swap_nominator"), 3)  # 三次嘗試，不沿用預設 2 次
            art = harness.artifacts()
            nomination = art["team_inputs"]["swap_nomination"]
            self.assertEqual((nomination["status"], nomination["nominations"]), ("exhausted", []))
            self.assertTrue(nomination["errors"])
            self.assertEqual((art["decision"]["status"], art["decision"]["orders"], flags(art["proposal"])), self.baseline_orders())
            self.assertEqual(harness.validate(art), [])
            self.assertTrue(any(call.get("first_validation_errors") for call in pipeline.calls if call["name"] == "swap_nominator"))

    def test_the_second_attempt_passing_does_not_seal_exhausted(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory)
            result, _ = harness.run(FakeRunner(outputs(BAD_NOMINATION, NOMINATION)))
            self.assertEqual(result.status, "completed", result.errors)
            self.assertEqual(harness.artifacts()["team_inputs"]["swap_nomination"]["status"], "completed")

    def test_transient_failure_after_retry_seals_unavailable(self):
        class Flaky(FakeRunner):
            def run(self, task, run_dir):
                if task.name == "swap_nominator":
                    self.prompts.append((task.name, task.prompt))
                    raise TransientAgentError("逾時")
                return super().run(task, run_dir)

        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory)
            runner = Flaky(outputs())
            result, _ = harness.run(runner)
            self.assertEqual(result.status, "completed", result.errors)
            nomination = harness.artifacts()["team_inputs"]["swap_nomination"]
            self.assertEqual((nomination["status"], nomination["nominations"]), ("unavailable", []))
            self.assertEqual(harness.artifacts()["decision"]["orders"], self.baseline_orders()[1])
            self.assertEqual(harness.validate(harness.artifacts()), [])

    def test_security_rejection_still_stops_the_whole_run(self):
        class Rejecting(FakeRunner):
            def run(self, task, run_dir):
                if task.name == "swap_nominator":
                    raise DailyPipelineError("swap_nominator 輸出含專案機密")
                return super().run(task, run_dir)

        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory)
            result, _ = harness.run(Rejecting(outputs()))
            self.assertEqual(result.status, "failed")
            self.assertTrue(any("機密" in error for error in result.errors), result.errors)
            self.assertIsNone(result.decision_status)

    def test_resume_reproduces_the_same_degraded_nomination_without_calling_the_agent(self):
        class StopAtStance(FakeRunner):
            def run(self, task, run_dir):
                if task.name == "cash_stance":
                    raise DailyPipelineError("cash_stance Agent 未產生結構化輸出：session limit")
                return super().run(task, run_dir)

        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory)
            first, _ = harness.run(StopAtStance(outputs(BAD_NOMINATION, BAD_NOMINATION, BAD_NOMINATION)))
            self.assertEqual(first.status, "failed")
            saved = json.loads((harness.world.run_dir / "swap_nomination.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "exhausted")
            self.assertTrue((harness.world.run_dir / "swap_nominator_degraded.json").exists())
            remaining = {name: value for name, value in outputs().items() if name in {"cash_stance", "review_r0"}}
            runner = FakeRunner(remaining)  # 若再呼叫 swap_nominator 會直接 AssertionError
            result, _ = harness.run(runner, resume=True)
            self.assertEqual(result.status, "completed", result.errors)
            self.assertNotIn("swap_nominator", [name for name, _ in runner.prompts])
            self.assertEqual(harness.artifacts()["team_inputs"]["swap_nomination"], saved)


class BuilderTests(unittest.TestCase):
    def test_version_table(self):
        self.assertEqual(SWAP_TEAM_VERSION, "2.9")
        self.assertEqual(TEAM_FIELDS_BY_VERSION["2.8"] | {"swap_nomination"}, SWAP_TEAM_FIELDS)
        self.assertEqual(TEAM_FIELDS_BY_VERSION[SWAP_TEAM_VERSION], SWAP_TEAM_FIELDS)

    def test_builder_requires_the_turn_state_and_keeps_older_versions_unchanged(self):
        from test_team_chain import team_world
        from test_trader import STANCE as TEAM_STANCE

        _, _, _, _, _, _, reports, research = team_world()
        with self.assertRaisesRegex(DecisionToolError, "必須與反彈參與狀態"):
            build_team_inputs(reports, research, dict(TEAM_STANCE), swap_nomination={"status": "not_needed"})
        plain = build_team_inputs(reports, research, dict(TEAM_STANCE))
        self.assertNotIn("swap_nomination", plain)
        self.assertNotEqual(plain["schema_version"], SWAP_TEAM_VERSION)


if __name__ == "__main__":
    unittest.main()
