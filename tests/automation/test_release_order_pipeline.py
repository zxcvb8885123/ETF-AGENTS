"""回撤減碼的釋出順序（5d，drawdown_derisk v5）接入每日決策鏈與 team_inputs 選配鍵 ``release_order`` 的端到端測試。

整條鏈用正式樣板（拿掉 fixture 沒有資料的 cash_stance_recency，以及與本測試無關的 agent_swap、exposure_view，再把 drawdown_derisk 改成 v5）跑真實 pipeline 與真實 Finalizer：

- 兩檔持股都是第一輪 hold：交易 Agent 在入選排序之後給順序、封存進 team_inputs（版本不升）、綁進 Policy、可被 Finalizer 重建；
  沒有減碼的日子配置與 v4 對照組逐位相同。
- 可減碼持股少於兩檔：程式封存 ``not_needed``，不呼叫 Agent。
- 降級：驗證用盡封存 exhausted（三次嘗試）、暫時性失敗封存 unavailable，其餘決策照常；續跑重現同一份順序、不再呼叫 Agent；
  安全拒絕仍整個停止。
- fail-closed：閘門與 team_inputs 鍵不同步（雙向）、缺封存、順序或綁定被改寫一律拒絕；v4 的舊樣板不呼叫第四步、team_inputs 逐鍵不變。

夾具：沿用換股提名的世界，但帳戶另持 2317（1000 股），所以第一輪 hold 的 2330 與 2317 都可減碼；2317 在這個世界沒有買進意圖。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.agent_runner import DailyPipelineError, TransientAgentError
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline, _history_provider
from etf_agent.decision.contracts import artifact_content_sha256
from etf_agent.decision import DecisionFinalizer, DecisionRepository, DecisionResultValidator, decision_policy_sha256
from etf_agent.decision.finalization import RANKING_TEAM_VERSIONS, build_team_inputs
from etf_agent.decision import DecisionToolError

from test_daily_pipeline import (
    BEAR, BULL, EVENT, FUNDAMENTAL, ROOT, STANCE, TECHNICAL, TRADE, FakeRunner, PipelineWorld, live_without_offense, team_outputs,
)

NAV = [
    {"run_id": "ledger-1", "as_of": "2026-09-01T06:00:00+00:00", "nav": "1160000"},
    {"run_id": "ledger-2", "as_of": "2026-09-02T06:00:00+00:00", "nav": "1161000"},
]
HOLDINGS = [
    {"run_id": "ledger-1", "as_of": "2026-09-01T06:00:00+00:00", "symbols": ["2317.TW", "2330.TW"]},
    {"run_id": "ledger-2", "as_of": "2026-09-02T06:00:00+00:00", "symbols": ["2317.TW", "2330.TW"]},
]
RELEASE = {"sequence": [
    {"symbol": "2330.TW", "basis_ids": ["bear-2330-1"], "reason": "空頭論點已被採納，先放掉。"},
    {"symbol": "2317.TW", "basis_ids": ["review:2317.TW:position"], "reason": "持股事實顯示仍在成本之上，最後才放掉。"},
]}
BAD_RELEASE = {"sequence": [dict(RELEASE["sequence"][0], symbol="9999.TW"), RELEASE["sequence"][1]]}


def template(version="v5"):
    """正式樣板（拿掉 fixture 沒有資料的 cash_stance_recency 與已開影子的 agent_swap）；drawdown_derisk 改成 ``version``。"""
    value = live_without_offense()
    sizing = value["position_sizing"]
    sizing.pop("cash_stance_recency")
    sizing.pop("agent_swap", None)
    sizing.pop("exposure_view", None)  # 正式樣板已含 exposure_view（純紀錄）；整條鏈由 test_exposure_view 覆蓋
    sizing.pop("cash_stance_brief", None)  # 前提閘門被拿掉時，依賴它們的簡報閘門一併拿掉
    sizing["drawdown_derisk"] = dict(sizing["drawdown_derisk"], version=version)
    return value


def trade_two_holds():
    """2330 續抱但採納空頭論點、2317（持股）續抱；hold 也要給信心（exposure_rotation）。"""
    trade = copy.deepcopy(TRADE)
    trade["items"][0] = {
        "symbol": "2317.TW", "intent": "hold", "conviction": "medium", "rationale": "多頭論點成立，續抱。",
        "adopted_claim_ids": ["bull-2317-1"], "rejected_claim_ids": [], "unresolved_questions": [], "invalidation_conditions": [],
    }
    trade["items"][1].update(conviction="medium", adopted_claim_ids=["bear-2330-1"], rejected_claim_ids=["bull-2330-1"])
    return trade


def trade_single_holding():
    """只持 2330 的世界：2317 買進、2330 續抱（採納空頭論點；hold 也要給信心）。"""
    trade = copy.deepcopy(TRADE)
    trade["items"][1].update(conviction="medium", adopted_claim_ids=["bear-2330-1"], rejected_claim_ids=["bull-2330-1"])
    return trade


def outputs(*release, trade=None, **overrides):
    value = team_outputs(
        trader_b0=[trade or trade_two_holds()],
        cash_stance=[dict(STANCE, cash_grade="middle", turn_assessment="no_turn_evidence")],
    )
    if release:
        value["release_order"] = list(release)
    value.update(overrides)
    return value


class Harness:
    def __init__(self, directory, version="v5", two_holdings=True, cash="888000", shares=1000):
        self.world = PipelineWorld(Path(directory), approved_status=True, template=template(version))
        account = json.loads(self.world.paths["account"].read_text(encoding="utf-8"))
        if two_holdings:
            account["positions"] = [dict(row, shares=shares) for row in account["positions"]] + [
                {"symbol": "2317.TW", "shares": shares, "average_cost": "100"}]
            account.update(cash=cash, settled_cash=cash, nav=str(int(cash) + 276 * shares))  # 行情：2330=164、2317=112
        else:
            account.update(cash="1000000", settled_cash="1000000", nav="1164000")  # 與帳本 NAV 歷史配合，避免觸發回撤下限
        self.world.paths["account"].write_text(json.dumps(account, ensure_ascii=False), encoding="utf-8")
        self.holdings = HOLDINGS if two_holdings else [dict(row, symbols=["2330.TW"]) for row in HOLDINGS]
        # 帳本 NAV 歷史跟著帳戶 NAV 走（避免意外觸發回撤下限）。
        nav = int(account["nav"])
        self.nav = [dict(row, nav=str(nav - 4000 + 1000 * index)) for index, row in enumerate(NAV)]

    def run(self, runner, resume=False, **kwargs):
        world = self.world
        pipeline = DailyDecisionPipeline(
            ROOT, world.run_dir, runner, world.repository, log=lambda _: None, resume=resume, sleep=lambda _: None, **kwargs)
        result = pipeline.run(
            world.paths["snapshot"], world.paths["account"], world.paths["trading_status"], world.paths["rules"],
            world.paths["database"], world.paths["template"], world.paths["sectors"], "decision-fixture",
            nav_history=self.nav, holdings_history=self.holdings,
        )
        return result, pipeline

    def artifacts(self):
        run_dir = DecisionRepository(self.world.repository).verify("decision-fixture")
        names = ("decision_input", "policy", "momentum", "debate", "intent", "team_inputs", "proposal", "scenario",
                 "guard", "risk_review", "revision_history", "decision")
        return {name: json.loads((run_dir / ("%s.json" % name)).read_text(encoding="utf-8")) for name in names}

    def finalizer(self, art, **replace):
        art = dict(art, **replace)
        return DecisionFinalizer(
            art["decision_input"], art["policy"], art["momentum"], art["debate"], art["intent"], art["team_inputs"],
            self.world.repository, _history_provider(self.nav, self.holdings),
        ), art

    def validate(self, art, **replace):
        art = dict(art, **replace)
        return DecisionResultValidator(
            art["decision_input"], art["policy"], art["momentum"], art["debate"], art["intent"], art["team_inputs"],
            self.world.repository, _history_provider(self.nav, self.holdings),
        ).validate(art["proposal"], art["scenario"], art["guard"], art["risk_review"], art["revision_history"], art["decision"])

    def rebuild_errors(self, art, **replace):
        """Finalizer 的逐項重建錯誤（DecisionResultValidator 只回「與完整重建結果不一致」，看不到原因）。"""
        finalizer, art = self.finalizer(art, **replace)
        result = finalizer.run(art["proposal"], art["scenario"], art["guard"], art["risk_review"], art["revision_history"])
        return result["errors"] if result["status"] == "rejected" else []


def flags(proposal):
    return sorted((flag["symbol"], flag["code"], flag.get("detail")) for flag in proposal["constraint_flags"])


def sealed(policy, mutate):
    value = copy.deepcopy(policy)
    mutate(value)
    value["content_sha256"] = decision_policy_sha256(value)
    return value


def has(errors, token):
    return any(token in error for error in errors)


class V5RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        (base / "control").mkdir()
        (base / "gated").mkdir()
        cls.control = Harness(base / "control", version="v4")
        cls.control_runner = FakeRunner(outputs())
        cls.control_result, _ = cls.control.run(cls.control_runner)
        cls.gated = Harness(base / "gated")
        cls.runner = FakeRunner(outputs(RELEASE))
        cls.result, cls.pipeline = cls.gated.run(cls.runner)
        cls.control_art = cls.control.artifacts()
        cls.art = cls.gated.artifacts()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_the_order_is_called_once_after_ranking_and_sealed_without_bumping_the_team_version(self):
        self.assertEqual(self.result.status, "completed", self.result.errors)
        names = [name for name, _ in self.runner.prompts]
        self.assertEqual(names.count("release_order"), 1)
        self.assertLess(names.index("trader_b0"), names.index("release_order"))
        self.assertLess(names.index("release_order"), names.index("cash_stance"))
        team = self.art["team_inputs"]
        self.assertIn(team["schema_version"], RANKING_TEAM_VERSIONS)
        self.assertEqual(team["schema_version"], self.control_art["team_inputs"]["schema_version"])  # 選配鍵：不升版本
        self.assertEqual(set(team) - set(self.control_art["team_inputs"]), {"release_order"})
        release = team["release_order"]
        self.assertEqual((release["status"], [row["symbol"] for row in release["sequence"]]), ("completed", ["2330.TW", "2317.TW"]))
        derisk = self.art["policy"]["position_sizing"]["drawdown_derisk"]
        self.assertEqual(derisk["release_symbols"], ["2330.TW", "2317.TW"])
        self.assertEqual(derisk["release_ref"]["sha256"], release["content_sha256"])
        saved = json.loads((self.gated.world.run_dir / "release_order.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, release)
        self.assertTrue((self.gated.world.run_dir / "brief_release_order.json").exists())

    def test_the_brief_has_no_stance_and_the_prompt_names_the_reference(self):
        prompt = next(prompt for name, prompt in self.runner.prompts if name == "release_order")
        brief = (self.gated.world.run_dir / "brief_release_order.json").read_text(encoding="utf-8")
        self.assertIn("release-order.md", prompt)
        for token in ("defensive", "cash_stance", "shortfall"):
            self.assertNotIn(token, brief)

    def test_a_day_without_derisk_is_bit_for_bit_the_same_as_the_v4_control(self):
        control, gated = self.control_art, self.art
        self.assertEqual(self.control_result.status, "completed", self.control_result.errors)
        self.assertNotIn("release_order", control["team_inputs"])
        self.assertNotEqual(control["policy"]["content_sha256"], gated["policy"]["content_sha256"])
        self.assertEqual(control["decision"]["orders"], gated["decision"]["orders"])
        self.assertEqual(control["proposal"]["order_proposal"], gated["proposal"]["order_proposal"])
        self.assertEqual(flags(control["proposal"]), flags(gated["proposal"]))
        self.assertEqual(control["intent"]["items"], gated["intent"]["items"])  # 交易 Agent 的原意圖沒有被改寫
        self.assertNotIn("release_order", [name for name, _ in self.control_runner.prompts])  # v4 不呼叫第四步
        self.assertFalse((self.control.world.run_dir / "release_order.json").exists())

    def test_the_sealed_run_is_rebuilt_by_the_finalizer_and_the_old_run_still_is(self):
        self.assertEqual(self.art["decision"]["errors"], [])
        self.assertEqual(self.gated.validate(self.art), [])
        self.assertEqual(self.control.validate(self.control_art), [])

    def test_agent_calls_and_stage_durations_record_the_step(self):
        self.assertTrue(any(call["name"] == "release_order" for call in self.pipeline.calls))
        self.assertIn("release_order", self.result.stage_durations)

    # -- fail-closed：Finalizer
    def tampered_team(self, mutate):
        team = copy.deepcopy(self.art["team_inputs"])
        mutate(team)
        return team

    def test_gate_and_team_key_must_exist_together(self):
        art = self.art
        errors = self.gated.rebuild_errors(art, team_inputs=self.tampered_team(lambda team: team.pop("release_order")))
        self.assertTrue(has(errors, "drawdown_derisk v5 必須與 team_inputs.release_order 同時存在"), errors)
        errors = self.control.rebuild_errors(self.control_art, team_inputs=dict(self.control_art["team_inputs"], release_order=self.art["team_inputs"]["release_order"]))
        self.assertTrue(has(errors, "drawdown_derisk v5 必須與 team_inputs.release_order 同時存在"), errors)
        v4 = sealed(art["policy"], lambda policy: policy["position_sizing"]["drawdown_derisk"].update(version="v4") or [
            policy["position_sizing"]["drawdown_derisk"].pop(field) for field in ("release_symbols", "release_ref")])
        errors = self.gated.rebuild_errors(art, policy=v4)
        self.assertTrue(has(errors, "drawdown_derisk v5 必須與 team_inputs.release_order 同時存在"), errors)

    def test_a_policy_without_the_bound_order_is_rejected(self):
        art = self.art

        def unbind(policy):
            for field in ("release_symbols", "release_ref"):
                policy["position_sizing"]["drawdown_derisk"].pop(field)

        # 未綁定的 v5 在重建配置時由引擎拋 DecisionToolError，沒有任何路徑能 approved。
        with self.assertRaisesRegex(DecisionToolError, "尚未綁定釋出順序"):
            self.gated.rebuild_errors(art, policy=sealed(art["policy"], unbind))

    def test_a_tampered_order_or_binding_is_rejected(self):
        art = self.art
        entry = lambda team: team["release_order"]["sequence"][0]  # noqa: E731
        cases = {
            "代號改成別檔": lambda team: entry(team).update(symbol="2317.TW"),
            "依據改成不存在的識別碼": lambda team: entry(team).update(basis_ids=["bear-9999-1"]),
            "依據改成已否決的 claim": lambda team: entry(team).update(basis_ids=["bull-2330-1"]),
            "順序被反轉": lambda team: team["release_order"].update(sequence=list(reversed(team["release_order"]["sequence"]))),
            "狀態改寫成 not_needed": lambda team: team["release_order"].update(status="not_needed"),
            "多出未允許欄位": lambda team: team["release_order"].update(advice="賣出"),
            "信封綁別份決策": lambda team: team["release_order"].update(trade_decision_sha256="0" * 64),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                errors = self.gated.rebuild_errors(art, team_inputs=self.tampered_team(mutate))
                self.assertTrue(has(errors, "release_order"), (label, errors))
        # Policy 綁定被改成與封存不同的順序。
        rebound = sealed(art["policy"], lambda policy: policy["position_sizing"]["drawdown_derisk"].update(release_symbols=["2317.TW", "2330.TW"]))
        self.assertTrue(has(self.gated.rebuild_errors(art, policy=rebound), "release_symbols 與 team_inputs.release_order 不一致"))

    def test_facts_recomputed_from_the_inputs_must_match(self):
        art = self.art
        intent = copy.deepcopy(art["intent"])
        for item in intent["items"]:
            if item["symbol"] == "2317.TW":
                item.update(intent="exit", conviction=None, adopted_claim_ids=["review:2317.TW:position"], rejected_claim_ids=["bull-2317-1"])
        intent["content_sha256"] = artifact_content_sha256(intent)  # 雜湊一致的改寫：2317 變成第一輪出場，可減碼集合只剩 2330
        errors = self.gated.rebuild_errors(art, intent=intent)
        self.assertTrue(has(errors, "team_inputs.release_order"), errors)


class NotNeededTests(unittest.TestCase):
    def test_a_single_holding_seals_not_needed_without_calling_the_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory, two_holdings=False)
            runner = FakeRunner(outputs(trade=trade_single_holding()))
            result, _ = harness.run(runner)
            self.assertEqual(result.status, "completed", result.errors)
            self.assertNotIn("release_order", [name for name, _ in runner.prompts])
            art = harness.artifacts()
            self.assertEqual(art["team_inputs"]["release_order"]["status"], "not_needed")
            self.assertEqual(art["policy"]["position_sizing"]["drawdown_derisk"]["release_ref"]["status"], "not_needed")
            self.assertEqual(harness.validate(art), [])
            forged = copy.deepcopy(art["team_inputs"])
            forged["release_order"]["status"] = "exhausted"
            self.assertTrue(harness.validate(art, team_inputs=forged))


class V4UnchangedTests(unittest.TestCase):
    def test_a_v4_template_never_runs_the_step_and_keeps_the_team_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory, version="v4")
            runner = FakeRunner(outputs())
            result, _ = harness.run(runner)
            self.assertEqual(result.status, "completed", result.errors)
            self.assertNotIn("release_order", [name for name, _ in runner.prompts])
            self.assertNotIn("release_order", harness.artifacts()["team_inputs"])


class DegradedRunTests(unittest.TestCase):
    def baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory, version="v4")
            result, _ = harness.run(FakeRunner(outputs()))
            self.assertEqual(result.status, "completed", result.errors)
            art = harness.artifacts()
            return art["decision"]["status"], art["decision"]["orders"], flags(art["proposal"])

    def test_three_invalid_outputs_seal_exhausted_and_the_rest_of_the_decision_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory)
            runner = FakeRunner(outputs(BAD_RELEASE, BAD_RELEASE, BAD_RELEASE))
            result, pipeline = harness.run(runner)
            self.assertEqual(result.status, "completed", result.errors)
            self.assertEqual([name for name, _ in runner.prompts].count("release_order"), 3)  # 三次嘗試，不沿用預設 2 次
            art = harness.artifacts()
            release = art["team_inputs"]["release_order"]
            self.assertEqual((release["status"], release["sequence"]), ("exhausted", []))
            self.assertTrue(release["errors"])
            self.assertEqual((art["decision"]["status"], art["decision"]["orders"], flags(art["proposal"])), self.baseline())
            self.assertEqual(harness.validate(art), [])
            self.assertTrue(any(call.get("first_validation_errors") for call in pipeline.calls if call["name"] == "release_order"))

    def test_the_second_attempt_passing_does_not_seal_exhausted(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory)
            result, pipeline = harness.run(FakeRunner(outputs(BAD_RELEASE, RELEASE)))
            self.assertEqual(result.status, "completed", result.errors)
            self.assertEqual(harness.artifacts()["team_inputs"]["release_order"]["status"], "completed")
            # 驗證回饋用中性措辭並列兩種修法。
            prompts = [call for call in pipeline.calls if call["name"] == "release_order"]
            self.assertEqual(len(prompts), 2)

    def test_the_feedback_to_the_agent_is_neutral_and_lists_both_remedies(self):
        class Capture(FakeRunner):
            pass

        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory)
            runner = Capture(outputs(BAD_RELEASE, RELEASE))
            harness.run(runner)
            retry = [prompt for name, prompt in runner.prompts if name == "release_order"][1]
            self.assertIn("請從順序中移除，或改列其他持股", retry)

    def test_transient_failure_after_retry_seals_unavailable(self):
        class Flaky(FakeRunner):
            def run(self, task, run_dir):
                if task.name == "release_order":
                    self.prompts.append((task.name, task.prompt))
                    raise TransientAgentError("逾時")
                return super().run(task, run_dir)

        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory)
            result, _ = harness.run(Flaky(outputs()))
            self.assertEqual(result.status, "completed", result.errors)
            art = harness.artifacts()
            release = art["team_inputs"]["release_order"]
            self.assertEqual((release["status"], release["sequence"]), ("unavailable", []))
            self.assertEqual(art["decision"]["orders"], self.baseline()[1])
            self.assertEqual(harness.validate(art), [])

    def test_security_rejection_still_stops_the_whole_run(self):
        class Rejecting(FakeRunner):
            def run(self, task, run_dir):
                if task.name == "release_order":
                    raise DailyPipelineError("release_order 輸出含專案機密")
                return super().run(task, run_dir)

        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory)
            result, _ = harness.run(Rejecting(outputs()))
            self.assertEqual(result.status, "failed")
            self.assertTrue(any("機密" in error for error in result.errors), result.errors)
            self.assertIsNone(result.decision_status)

    def test_resume_reproduces_the_same_degraded_order_without_calling_the_agent(self):
        class StopAtStance(FakeRunner):
            def run(self, task, run_dir):
                if task.name == "cash_stance":
                    raise DailyPipelineError("cash_stance Agent 未產生結構化輸出：session limit")
                return super().run(task, run_dir)

        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory)
            first, _ = harness.run(StopAtStance(outputs(BAD_RELEASE, BAD_RELEASE, BAD_RELEASE)))
            self.assertEqual(first.status, "failed")
            saved = json.loads((harness.world.run_dir / "release_order.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "exhausted")
            self.assertTrue((harness.world.run_dir / "release_order_degraded.json").exists())
            remaining = {name: value for name, value in outputs().items() if name in {"cash_stance", "review_r0"}}
            runner = FakeRunner(remaining)  # 若再呼叫 release_order 會直接 AssertionError
            result, _ = harness.run(runner, resume=True)
            self.assertEqual(result.status, "completed", result.errors)
            self.assertNotIn("release_order", [name for name, _ in runner.prompts])
            self.assertEqual(harness.artifacts()["team_inputs"]["release_order"], saved)

    def test_resume_reuses_a_completed_order_without_calling_the_agent(self):
        class StopAtStance(FakeRunner):
            def run(self, task, run_dir):
                if task.name == "cash_stance":
                    raise DailyPipelineError("cash_stance Agent 未產生結構化輸出：session limit")
                return super().run(task, run_dir)

        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(directory)
            first, _ = harness.run(StopAtStance(outputs(RELEASE)))
            self.assertEqual(first.status, "failed")
            saved = json.loads((harness.world.run_dir / "release_order.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "completed")
            remaining = {name: value for name, value in outputs().items() if name in {"cash_stance", "review_r0"}}
            runner = FakeRunner(remaining)
            result, _ = harness.run(runner, resume=True)
            self.assertEqual(result.status, "completed", result.errors)
            self.assertNotIn("release_order", [name for name, _ in runner.prompts])
            self.assertEqual(harness.artifacts()["team_inputs"]["release_order"], saved)


class BuilderTests(unittest.TestCase):
    def test_builder_requires_the_ranking_and_leaves_other_versions_unchanged(self):
        from test_team_chain import team_world
        from test_trader import STANCE as TEAM_STANCE

        _, _, _, _, _, _, reports, research = team_world()
        with self.assertRaisesRegex(DecisionToolError, "需要入選排序"):
            build_team_inputs(reports, research, dict(TEAM_STANCE), release_order={"status": "not_needed"})
        plain = build_team_inputs(reports, research, dict(TEAM_STANCE))
        self.assertNotIn("release_order", plain)


if __name__ == "__main__":
    unittest.main()


class DeriskDayTests(unittest.TestCase):
    """防守姿態且現金低於緩衝的日子：Finalizer 以 Policy 綁定的順序重建整份配置；順序不同，賣的股票不同。"""

    @staticmethod
    def derisk_outputs(*release):
        stance = dict(STANCE, level="defensive", cash_grade="lower", turn_assessment="no_turn_evidence")  # 現金比例 15%
        return outputs(*release, cash_stance=[stance])

    def run_day(self, *release, version="v5"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        harness = Harness(directory.name, version=version, cash="30000", shares=3000)
        result, _ = harness.run(FakeRunner(self.derisk_outputs(*release)))
        self.assertEqual(result.status, "completed", result.errors)
        art = harness.artifacts()
        self.assertEqual(harness.validate(art), [])
        return harness, art

    @staticmethod
    def sold(art):
        return [(o["symbol"], o["shares"]) for o in art["decision"]["orders"] if o["side"] == "sell"]

    def test_the_agent_order_decides_who_is_sold_and_the_finalizer_rebuilds_it(self):
        first_2330 = self.run_day(RELEASE)[1]
        first_2317 = self.run_day({"sequence": list(reversed(RELEASE["sequence"]))})[1]
        for art, first in ((first_2330, "2330.TW"), (first_2317, "2317.TW")):
            derisk = [f["symbol"] for f in art["proposal"]["constraint_flags"] if f["code"] == "RISK_DRAWDOWN"]
            self.assertTrue(derisk, first)
            self.assertEqual(derisk[0], first)
            self.assertIn("依交易 Agent 的釋出順序", next(f["detail"] for f in art["proposal"]["constraint_flags"] if f["code"] == "RISK_DRAWDOWN"))
        self.assertNotEqual(self.sold(first_2330), self.sold(first_2317))

    def test_a_degraded_order_falls_back_to_the_v4_order_with_a_flag_and_the_rest_is_unchanged(self):
        harness, art = self.run_day(BAD_RELEASE, BAD_RELEASE, BAD_RELEASE)
        _, control = self.run_day(version="v4")
        self.assertEqual(art["team_inputs"]["release_order"]["status"], "exhausted")
        self.assertEqual(art["decision"]["orders"], control["decision"]["orders"])  # 退回 ATR 順序：訂單與 v4 逐位相同
        marker = [(f["symbol"], f["code"]) for f in art["proposal"]["constraint_flags"] if f["code"].startswith("DERISK_ORDER_SKIPPED")]
        self.assertEqual(marker, [("PORTFOLIO", "DERISK_ORDER_SKIPPED:order_exhausted")])

    def test_a_forged_derisk_binding_changes_the_rebuilt_orders_and_is_rejected(self):
        harness, art = self.run_day(RELEASE)
        rebound = sealed(art["policy"], lambda policy: policy["position_sizing"]["drawdown_derisk"].update(
            release_symbols=["2317.TW", "2330.TW"]))
        errors = harness.rebuild_errors(art, policy=rebound)
        self.assertTrue(has(errors, "release_symbols 與 team_inputs.release_order 不一致"), errors)
        self.assertTrue(harness.validate(art, policy=rebound))
