"""position_sizing.offense v1（轉向後進攻，已實作、尚未開啟）接入每日決策鏈與 team_inputs 的端到端測試。

以 config/decision_policy.json 原樣為基礎（記憶體內修改：加 offense 區塊、拿掉 rebound_participation 的 defensive_cash_buffer、
調整 agent_swap.exposure.max_pairs_by_view），跑真實 pipeline 與真實 Finalizer。fixture 沒有足夠行情，進攻事實多為 unavailable，
所以風險 Agent 填 keep_defensive：證明 decision run completed、team_inputs 帶 offense_facts（2.10 不升版）、Policy 綁定有效等級、
Finalizer 驗證與重建乾淨、竄改／缺漏／無閘門帶鍵一律拒絕；沒有 offense 的對照組 team_inputs 沒有該鍵。
測試用數值只放在測試內，沒有寫進 config。

ProbePipelineTests 另以記憶體內的 entry_reserve.max_entry_weight 0.3 補足 probe 端到端成交（有效等級 probe 綁進 Policy、買賣單成交、
意圖不被改寫、Finalizer 重建乾淨、竄改有效等級／份量格／事實被拒），以及 offense_facts 須 team_inputs 2.9 以上的版本要求。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.decision import MARKET_REGIME_EVIDENCE_ID, DecisionToolError, decision_policy_sha256
from etf_agent.perception.market_fear_greed.service import _store

from test_daily_pipeline import FakeRunner
from test_exposure_swap_pipeline import EXPOSURE_PAIR
from test_market_fear_greed import approvals, captures
from test_quality_dip_ruling_pipeline import Harness3, live_like_template, team_outputs
from test_swap_nomination_pipeline import HOLDINGS, NAV, ROOT

OFFENSE = {
    "version": "v1",
    "by_view": {
        "probe": {"defensive_cash_buffer": "0.10", "max_exposure_pairs": 2, "max_size": "medium", "beta_stop": "0.40"},
        "start_rebuild": {"defensive_cash_buffer": "0.08", "max_exposure_pairs": 4, "max_size": "large", "beta_stop": "0.55"},
        "rebuild": {"defensive_cash_buffer": "0.08", "max_exposure_pairs": 5, "max_size": "large", "beta_stop": "0.70"},
    },
    "offense_off_drawdown": "0.12",
}


def template_offense():
    value = live_like_template("v4")
    sizing = value["position_sizing"]
    for row in sizing["rebound_participation"]["by_state"].values():
        row.pop("defensive_cash_buffer", None)
    sizing["agent_swap"]["exposure"]["max_pairs_by_view"] = {"probe": 2, "start_rebuild": 4, "rebuild": 5}
    sizing["agent_swap"]["entry_reserve"]["max_entry_weight"] = "0.2"  # fixture 的價格下 0.06 買不到一張；只放測試樣板
    sizing["offense"] = copy.deepcopy(OFFENSE)
    return value


def template_without_offense():
    value = live_like_template("v4")  # 正式設定已含 offense：對照組拿掉它並還原兩鍵的 max_pairs_by_view
    sizing = value["position_sizing"]
    sizing.pop("offense", None)
    sizing["agent_swap"]["exposure"]["max_pairs_by_view"] = {"start_rebuild": 3, "rebuild": 5}
    return value


class OffensePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        (base / "on").mkdir()
        (base / "off").mkdir()
        cls.on = Harness3(base / "on", template=template_offense())
        cls.runner = FakeRunner(team_outputs("v4"))
        cls.result, _ = cls.on.run(cls.runner)
        cls.art = cls.on.artifacts() if cls.result.status == "completed" else {}
        cls.off = Harness3(base / "off", template=template_without_offense())
        cls.off_result, _ = cls.off.run(FakeRunner(team_outputs("v4")))
        cls.off_art = cls.off.artifacts() if cls.off_result.status == "completed" else {}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_run_completes_with_facts_and_bound_view_and_is_rebuildable(self):
        self.assertEqual(self.result.status, "completed", self.result.errors)
        team = self.art["team_inputs"]
        self.assertEqual(team["schema_version"], "2.10")
        facts = team["offense_facts"]
        self.assertFalse(facts["citable"])
        gate = self.art["policy"]["position_sizing"]["offense"]
        self.assertEqual(gate["view"], "keep_defensive")
        self.assertEqual(gate["effective_view"], "keep_defensive")
        self.assertEqual(self.art["policy"]["position_sizing"]["agent_swap"]["exposure"]["view"], "keep_defensive")
        self.assertEqual(self.on.validate(self.art), [])
        self.assertEqual(self.on.rebuild_errors(self.art), [])

    def test_probe_is_offered_to_the_risk_agent(self):
        prompt = dict(self.runner.prompts)["cash_stance"]
        self.assertIn("probe", prompt)
        self.assertIn("錯過反彈", prompt)

    def test_run_without_offense_has_no_key_and_cannot_carry_one(self):
        self.assertEqual(self.off_result.status, "completed", self.off_result.errors)
        team = self.off_art["team_inputs"]
        self.assertNotIn("offense_facts", team)
        forged = copy.deepcopy(team)
        forged["offense_facts"] = copy.deepcopy(self.art["team_inputs"]["offense_facts"])
        self.assertTrue(self.off.rebuild_errors(self.off_art, team_inputs=forged))

    def test_tampered_missing_or_rebound_state_is_rejected(self):
        forged = copy.deepcopy(self.art["team_inputs"])
        forged["offense_facts"]["facts_id"] = "offense-facts:" + "0" * 20
        self.assertTrue(self.on.rebuild_errors(self.art, team_inputs=forged))
        missing = copy.deepcopy(self.art["team_inputs"])
        del missing["offense_facts"]
        self.assertTrue(self.on.rebuild_errors(self.art, team_inputs=missing))
        policy = copy.deepcopy(self.art["policy"])
        policy["position_sizing"]["offense"].update(effective_view="rebuild", suppressed_reason=None)
        policy["position_sizing"]["agent_swap"]["exposure"]["view"] = "rebuild"
        policy["content_sha256"] = decision_policy_sha256(policy)
        try:
            problems = self.on.rebuild_errors(self.art, policy=policy)
        except DecisionToolError:
            problems = ["raised"]
        self.assertTrue(problems)


class FearGreedHarness(Harness3):
    """與 Harness3 同一個世界，另帶恐懼貪婪資料包（提供非新聞的市場層級證據與情緒類事實）。"""

    def __init__(self, directory, fear_greed, template=None):
        super().__init__(directory, template=template or template_offense())
        self.fear_greed = fear_greed

    def run(self, runner, **kwargs):
        world = self.world
        pipeline = DailyDecisionPipeline(ROOT, world.run_dir, runner, world.repository, log=lambda _: None, sleep=lambda _: None)
        result = pipeline.run(
            world.paths["snapshot"], world.paths["account"], world.paths["trading_status"], world.paths["rules"],
            world.paths["database"], world.paths["template"], world.paths["sectors"], "decision-fixture",
            nav_history=NAV, holdings_history=HOLDINGS,
            fear_greed_root=self.fear_greed[0], fear_greed_approvals_path=self.fear_greed[1])
        return result, pipeline


def probe_outputs(evidence, **stance):
    """風險 Agent 填 probe（事實齊備：washout、breadth_thrust 皆 available）；提名一組 purpose=exposure 的配對。"""
    out = team_outputs("v4")
    out["swap_nominator"] = [{"nominations": [dict(EXPOSURE_PAIR, size="large")], "lower_conviction_review": [], "thesis_hint_responses": []}]
    value = dict(
        out["cash_stance"][0], exposure_view="probe", turn_assessment="no_turn_evidence", evidence_ids=[MARKET_REGIME_EVIDENCE_ID, evidence],
        offense_basis=["washout", "breadth_thrust"], failure_condition="三個交易日內交易池再創新低即視為這次試單失敗",
        defensive_reconciliation="")
    value.update(stance)
    out["cash_stance"] = [value]
    return out


class ProbePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        for name in ("base", "probe", "bad", "rebuild", "fill"):
            (base / name).mkdir()
        cls.fear_greed = (base / "fg", base / "fg.json")
        _store(cls.fear_greed[0]).save("fear-greed-20260919T120000Z-fixture", {"captures": {"items": captures()}})
        cls.fear_greed[1].write_text(json.dumps({"sources": approvals()}), encoding="utf-8")
        probe_base = FearGreedHarness(base / "base", cls.fear_greed)
        outs = team_outputs("v4")  # keep_defensive 的基準跑：只為了讀出恐懼貪婪資料包的證據編號
        outs["cash_stance"] = [dict(outs["cash_stance"][0], offense_basis=[], failure_condition="", defensive_reconciliation="")]
        probe_base.run(FakeRunner(outs))
        bundle = json.loads((probe_base.world.run_dir / "decision_input.json").read_text(encoding="utf-8"))
        cls.evidence = bundle["market_fear_greed_input"]["result"]["components"][0]["evidence_id"]
        cls.harness = FearGreedHarness(base / "probe", cls.fear_greed)
        cls.runner = FakeRunner(probe_outputs(cls.evidence))
        cls.result, _ = cls.harness.run(cls.runner)
        cls.art = cls.harness.artifacts() if cls.result.status == "completed" else {}
        cls.rebuild_harness = FearGreedHarness(base / "rebuild", cls.fear_greed)
        cls.rebuild_result, _ = cls.rebuild_harness.run(FakeRunner(probe_outputs(
            cls.evidence, exposure_view="start_rebuild", turn_assessment="turn_unconfirmed", offense_basis=[], failure_condition="")))
        cls.rebuild_art = cls.rebuild_harness.artifacts() if cls.rebuild_result.status == "completed" else {}
        # probe 端到端成交：只在記憶體內把 entry_reserve.max_entry_weight 調到 0.3（medium＝2/3＝0.2 的買進權重上限），
        # 讓 probe 的份量格縮到 medium 後仍買得到整張、出場後現金也不超過上限；config 不動。
        fill_template = template_offense()
        fill_template["position_sizing"]["agent_swap"]["entry_reserve"]["max_entry_weight"] = "0.3"
        cls.fill_harness = FearGreedHarness(base / "fill", cls.fear_greed, template=fill_template)
        cls.fill_result, _ = cls.fill_harness.run(FakeRunner(probe_outputs(cls.evidence)))
        cls.fill_art = cls.fill_harness.artifacts() if cls.fill_result.status == "completed" else {}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_probe_run_completes_and_binds_the_effective_view(self):
        self.assertEqual(self.result.status, "completed", self.result.errors)
        gate = self.art["policy"]["position_sizing"]["offense"]
        self.assertEqual((gate["view"], gate["effective_view"], gate["suppressed_reason"]), ("probe", "probe", None))
        self.assertEqual(self.art["policy"]["position_sizing"]["agent_swap"]["exposure"]["view"], "probe")
        stance = self.art["team_inputs"]["cash_stance"]
        self.assertEqual(stance["offense_basis"], ["washout", "breadth_thrust"])
        self.assertNotIn("defensive_reconciliation", stance)  # 空字串封存時還原成缺席
        statuses = {name: row["status"] for name, row in self.art["team_inputs"]["offense_facts"]["categories"].items()}
        self.assertEqual((statuses["washout"], statuses["breadth_thrust"]), ("available", "available"))

    def test_probe_in_this_fixture_does_not_sell_anyone_when_the_pair_cannot_be_filled(self):
        found = {(f["symbol"], f["code"]) for f in self.art["proposal"]["constraint_flags"]}
        self.assertIn(("PORTFOLIO", "OFFENSE:probe"), found)
        self.assertTrue(any(code.startswith("AGENT_SWAP_CANCELLED") for _, code in found), found)
        self.assertEqual(self.art["decision"]["orders"], [])  # 整組取消：換出者不賣、不會只賣不買（份量格縮格的細節見 test_offense_allocation）

    def test_start_rebuild_trades_the_exposure_pair_and_flags_say_so(self):
        self.assertEqual(self.rebuild_result.status, "completed", self.rebuild_result.errors)
        art = self.rebuild_art
        gate = art["policy"]["position_sizing"]["offense"]
        self.assertEqual((gate["effective_view"], gate["suppressed_reason"]), ("start_rebuild", None))
        orders = {(o["symbol"], o["side"]): o["intent"] for o in art["decision"]["orders"]}
        self.assertEqual(orders, {("2330.TW", "sell"): "exit", ("2317.TW", "buy"): "buy"})
        found = {(f["symbol"], f["code"]) for f in art["proposal"]["constraint_flags"]}
        self.assertIn(("PORTFOLIO", "OFFENSE:start_rebuild"), found)
        for item in art["intent"]["items"]:
            self.assertEqual(item["intent"], {"2330.TW": "hold", "2317.TW": "buy"}[item["symbol"]])  # 意圖不被改寫
        self.assertEqual(self.rebuild_harness.validate(art), [])
        self.assertEqual(self.rebuild_harness.rebuild_errors(art), [])

    def rejected(self, harness, art, **replace):
        """Finalizer 重建錯誤；Policy 自相矛盾而拋錯同樣算拒絕。"""
        try:
            return harness.rebuild_errors(art, **replace)
        except DecisionToolError as error:
            return ["raised: %s" % error]

    def sealed_policy(self, mutate):
        policy = copy.deepcopy(self.fill_art["policy"])
        mutate(policy["position_sizing"])
        policy["content_sha256"] = decision_policy_sha256(policy)
        return policy

    def test_probe_trades_the_exposure_pair_end_to_end_and_is_rebuildable(self):
        self.assertEqual(self.fill_result.status, "completed", self.fill_result.errors)
        art = self.fill_art
        gate = art["policy"]["position_sizing"]["offense"]
        self.assertEqual((gate["view"], gate["effective_view"], gate["suppressed_reason"]), ("probe", "probe", None))
        self.assertEqual(art["policy"]["position_sizing"]["agent_swap"]["exposure"]["view"], "probe")
        facts = art["team_inputs"]["offense_facts"]
        self.assertEqual({name: facts["categories"][name]["status"] for name in ("washout", "breadth_thrust")},
                         {"washout": "available", "breadth_thrust": "available"})
        orders = {(o["symbol"], o["side"]): (o["intent"], o["shares"]) for o in art["decision"]["orders"]}
        self.assertEqual(orders, {("2330.TW", "sell"): ("exit", 1000), ("2317.TW", "buy"): ("buy", 2000)})  # 賣出者整檔、買進 medium 份量（2000 股）
        found = {(f["symbol"], f["code"]) for f in art["proposal"]["constraint_flags"]}
        self.assertIn(("PORTFOLIO", "OFFENSE:probe"), found)
        self.assertIn(("2317.TW", "OFFENSE_SIZE_CAPPED"), found)  # 交易 Agent 填 large、probe 上限 medium：只縮不放
        self.assertIn(("2317.TW", "AGENT_SWAP_FILL"), found)
        self.assertFalse(any(code.startswith("AGENT_SWAP_CANCELLED") for _, code in found), found)
        for item in art["intent"]["items"]:
            self.assertEqual(item["intent"], {"2330.TW": "hold", "2317.TW": "buy"}[item["symbol"]])  # 意圖不被改寫
        self.assertEqual(self.fill_harness.validate(art), [])
        self.assertEqual(self.fill_harness.rebuild_errors(art), [])

    def test_probe_fill_rejects_a_tampered_effective_view_size_limit_or_facts(self):
        art, harness = self.fill_art, self.fill_harness

        def upgrade(sizing):
            sizing["offense"].update(effective_view="rebuild", view="rebuild")
            sizing["agent_swap"]["exposure"]["view"] = "rebuild"
        self.assertTrue(self.rejected(harness, art, policy=self.sealed_policy(upgrade)))  # 升級有效等級（與現金姿態重算不符）
        self.assertTrue(self.rejected(harness, art, policy=self.sealed_policy(
            lambda s: s["offense"].update(effective_view="keep_defensive", suppressed_reason="facts_unavailable"))))  # 假裝被壓回
        self.assertTrue(self.rejected(harness, art, policy=self.sealed_policy(
            lambda s: s["agent_swap"]["nominated_pairs"][0].update(size="small"))))  # 份量格與封存的提名不一致
        self.assertTrue(self.rejected(harness, art, policy=self.sealed_policy(
            lambda s: s["offense"]["by_view"]["probe"].update(max_size="large"))))  # 放大份量格上限：重算的配置不同
        forged = copy.deepcopy(art["team_inputs"])
        forged["swap_nomination"]["nominations"][0]["size"] = "small"
        self.assertTrue(self.rejected(harness, art, team_inputs=forged))  # 竄改提名的份量格
        for category, row in (("washout", {"status": "unavailable"}), ("breadth_thrust", {"status": "unavailable"})):
            forged = copy.deepcopy(art["team_inputs"])
            forged["offense_facts"]["categories"][category] = row
            self.assertTrue(self.rejected(harness, art, team_inputs=forged), category)  # 竄改事實：整份重算不符
        forged = copy.deepcopy(art["team_inputs"])
        forged["cash_stance"]["offense_basis"] = ["washout", "index_reclaim"]  # index_reclaim 在這個 fixture 沒有資料
        self.assertTrue(self.rejected(harness, art, team_inputs=forged))

    def test_offense_facts_need_team_inputs_2_9_or_later(self):
        from etf_agent.decision import build_team_inputs

        art, harness = self.fill_art, self.fill_harness
        team = art["team_inputs"]
        with self.assertRaises(DecisionToolError):  # 組裝端：沒有換股提名（2.9）就不能帶 offense_facts
            build_team_inputs(team["analyst_reports"], None, team["cash_stance"], offense_facts=team["offense_facts"])
        for version in ("2.0", "2.1", "2.5", "2.6", "2.7", "2.8"):
            old = copy.deepcopy(team)
            old["schema_version"] = version
            self.assertTrue(self.rejected(harness, art, team_inputs=old), version)  # 舊版 run 帶 offense_facts：拒絕
        old = copy.deepcopy(team)
        old["schema_version"] = "2.8"
        for key in ("swap_nomination", "quality_dip_ruling", "decline_depth_facts"):
            del old[key]  # 欄位集合完全符合 2.8，只剩 offense_facts 是多的
        errors = self.rejected(harness, art, team_inputs=old)
        self.assertTrue(any("offense_facts 只能用於 2.9 以上" in error for error in errors), errors)
        unknown = copy.deepcopy(team)
        unknown["schema_version"] = "2.99"
        self.assertTrue(self.rejected(harness, art, team_inputs=unknown))

    def test_prompt_and_brief_for_the_risk_agent(self):
        prompt = dict(self.runner.prompts)["cash_stance"]
        self.assertIn("probe", prompt)
        brief = json.loads((self.harness.world.run_dir / "brief_cash_stance.json").read_text(encoding="utf-8"))
        self.assertIn("offense_facts", brief)
        self.assertFalse(brief["offense_facts"]["citable"])
        self.assertNotIn("intents", json.dumps(brief))

    def test_finalizer_rebuilds_and_rejects_tampering(self):
        self.assertEqual(self.harness.validate(self.art), [])
        self.assertEqual(self.harness.rebuild_errors(self.art), [])
        upgraded = copy.deepcopy(self.art["policy"])
        upgraded["position_sizing"]["offense"]["effective_view"] = "rebuild"
        upgraded["position_sizing"]["agent_swap"]["exposure"]["view"] = "rebuild"
        upgraded["content_sha256"] = decision_policy_sha256(upgraded)
        try:
            problems = self.harness.rebuild_errors(self.art, policy=upgraded)
        except DecisionToolError:  # Policy 本身自相矛盾：拋錯同樣是拒絕
            problems = ["raised"]
        self.assertTrue(problems)
        upgraded["position_sizing"]["offense"]["view"] = "rebuild"  # 同步改 view：不再自相矛盾，但與現金姿態重算不符
        upgraded["content_sha256"] = decision_policy_sha256(upgraded)
        try:
            problems = self.harness.rebuild_errors(self.art, policy=upgraded)
        except DecisionToolError:
            problems = ["raised"]
        self.assertTrue(problems)
        no_basis = copy.deepcopy(self.art["team_inputs"])
        del no_basis["cash_stance"]["offense_basis"]
        self.assertTrue(self.harness.rebuild_errors(self.art, team_inputs=no_basis))
        forged = copy.deepcopy(self.art["team_inputs"])
        forged["offense_facts"]["categories"]["washout"] = {"status": "unavailable"}
        self.assertTrue(self.harness.rebuild_errors(self.art, team_inputs=forged))

    def test_an_inconsistent_probe_is_rejected_by_the_validator(self):
        outputs = probe_outputs(self.evidence, turn_assessment="turn_confirmed")  # probe 不得配 turn_confirmed
        outputs["cash_stance"] = outputs["cash_stance"] * 4  # 修正循環用盡仍不一致
        harness = FearGreedHarness(Path(self.directory.name) / "bad", self.fear_greed)
        result, _ = harness.run(FakeRunner(outputs))
        self.assertNotEqual(result.status, "completed")
        self.assertTrue(any("exposure_view=probe" in error or "turn_assessment" in error for error in result.errors), result.errors)


class ReplayAndGateKeyTests(unittest.TestCase):
    def test_replay_ignores_offense_and_allocates_like_without_it(self):
        import test_exposure_rotation_e2e as e2e
        from etf_agent.decision import AllocationOrderEngine
        helper = e2e.ReplayTests()
        helper.setUp()
        gated = helper.template(template_offense)  # 完整有效的樣板（含 offense 的全部前提）；回放在前提驗證之前略過 offense
        plain = copy.deepcopy(gated)
        del plain["position_sizing"]["offense"]
        plain["position_sizing"]["agent_swap"]["exposure"]["max_pairs_by_view"] = {"start_rebuild": 3, "rebuild": 5}
        plain_policy, plain_notes = helper.replay(plain)
        policy, notes = helper.replay(gated)
        self.assertEqual(notes["offense"], "ignored_in_replay")
        self.assertEqual({k: v for k, v in notes.items() if k not in ("policy_id", "content_sha256", "offense")},
                         {k: v for k, v in plain_notes.items() if k not in ("policy_id", "content_sha256")})
        self.assertNotIn("offense", policy["position_sizing"])
        body = AllocationOrderEngine(helper.bundle, policy).run(helper.intent)
        plain_body = AllocationOrderEngine(helper.bundle, plain_policy).run(helper.intent)
        for key in ("allocation_proposal", "order_proposal", "constraint_flags"):
            self.assertEqual(body[key], plain_body[key], key)


class GateKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OffensePipelineTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        OffensePipelineTests.tearDownClass()

    def test_a_policy_without_the_gate_cannot_carry_the_facts_key_and_vice_versa(self):
        art, on = OffensePipelineTests.art, OffensePipelineTests.on
        no_gate = copy.deepcopy(art["policy"])
        del no_gate["position_sizing"]["offense"]
        no_gate["position_sizing"]["agent_swap"]["exposure"]["view"] = "keep_defensive"
        no_gate["position_sizing"]["agent_swap"]["exposure"]["max_pairs_by_view"].pop("probe")
        no_gate["content_sha256"] = decision_policy_sha256(no_gate)
        try:
            problems = on.rebuild_errors(art, policy=no_gate)
        except DecisionToolError:
            problems = ["raised"]
        self.assertTrue(problems)  # 有 team_inputs.offense_facts 卻沒有閘門
        old = copy.deepcopy(art["team_inputs"])
        del old["offense_facts"]
        self.assertTrue(on.rebuild_errors(art, team_inputs=old))  # 有閘門卻沒有 offense_facts


if __name__ == "__main__":
    unittest.main()
