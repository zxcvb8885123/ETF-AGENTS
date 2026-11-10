"""Agent 主導曝險階段 2、3（position_sizing.agent_swap.exposure）接入每日決策鏈的端到端測試。

整條鏈用正式樣板（沿用 test_agent_swap_v2_pipeline 的 v2 樣板，再加 exposure_view 與 agent_swap.exposure）跑真實 pipeline 與真實 Finalizer：

- v2（階段 3）：5c 提名的 ``purpose: exposure`` 配對連同 purpose 綁進 Policy，風險 Agent 的 ``exposure_view`` 綁進
  ``agent_swap.exposure.view``；``start_rebuild`` 時成交、``keep_defensive`` 時該對取消且換出者不賣；整份決策可被 Finalizer 重建，
  team_inputs 仍是 2.9（不升版）、選配鍵 ``exposure_facts`` 與 ``swap_nomination`` 並存。
- v1（階段 2 影子）：配置不讀 exposure 配對，訂單與旗標與沒有這些配對時相同。
- 簡報與 Schema：有閘門時提名簡報含 ``exposure_swap`` 與持股的 ``exposure_basis``、Agent 輸出多 ``purpose``／``lower_conviction_review``；
  沒有閘門時簡報、提示與封存逐鍵不變。
- fail-closed：Policy 的 view 與現金姿態不一致、purpose 被改寫、exposure_facts 缺漏、view 未綁定一律拒絕；提名驗證用盡時封存
  ``exhausted``（含空的 ``lower_conviction_review``），其餘決策照常。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.decision import DecisionToolError

from test_agent_swap_v2_pipeline import V2Harness, codes, v2_template
from test_daily_pipeline import FakeRunner, PipelineWorld
from test_swap_nomination_pipeline import outputs, sealed, trade_with_bear_basis

CAPS = {"start_rebuild": 1, "rebuild": 2}
EXPOSURE_PAIR = {
    "buy_symbol": "2317.TW", "sell_symbol": "2330.TW", "buy_basis_ids": ["bull-2317-1"], "sell_basis_ids": ["review:2330.TW:exposure"],
    "reason": "組合在反彈日幾乎沒有跟上；放掉這檔的代價是少一檔低波動持股，新股的多頭論點已採納。", "purpose": "exposure",
}
NOMINATION = {"nominations": [EXPOSURE_PAIR], "lower_conviction_review": [], "thesis_hint_responses": []}
BAD_NOMINATION = {"nominations": [dict(EXPOSURE_PAIR, purpose="rebuild")], "lower_conviction_review": [], "thesis_hint_responses": []}


def exposure_template(version="v2"):
    value = v2_template()
    sizing = value["position_sizing"]
    sizing["exposure_view"] = {"version": "v1"}
    sizing["agent_swap"]["exposure"] = (
        {"version": "v2", "max_pairs_by_view": dict(CAPS)} if version == "v2" else {"version": "v1"})
    return value


class ExposureHarness(V2Harness):
    """與 ``V2Harness`` 同一個世界，樣板另有 exposure_view 與 agent_swap.exposure。"""

    def __init__(self, directory, version="v2", tight=True):
        self.world = PipelineWorld(Path(directory), approved_status=True, template=exposure_template(version))
        account = json.loads(self.world.paths["account"].read_text(encoding="utf-8"))
        account.update(cash="1000000", settled_cash="1000000", nav="1164000")
        self.world.paths["account"].write_text(json.dumps(account, ensure_ascii=False), encoding="utf-8")
        if tight:
            rules = json.loads(self.world.paths["rules"].read_text(encoding="utf-8"))
            rules["max_positions"] = 1
            self.world.paths["rules"].write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")


def exposure_outputs(nomination=NOMINATION, view="start_rebuild", conviction=None, *more):
    value = outputs(nomination, *more)
    value["cash_stance"] = [dict(value["cash_stance"][0], exposure_view=view)]
    if conviction is not None:
        trade = trade_with_bear_basis()
        trade["items"][1].update(conviction=conviction)
        value["trader_b0"] = [trade]
    return value


class FormalRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        for name in ("rebuild", "defensive", "shadow", "plain"):
            (base / name).mkdir()
        cls.rebuild = ExposureHarness(base / "rebuild")
        cls.rebuild_runner = FakeRunner(exposure_outputs())
        cls.rebuild_result, _ = cls.rebuild.run(cls.rebuild_runner)
        cls.defensive = ExposureHarness(base / "defensive")
        cls.defensive_result, _ = cls.defensive.run(FakeRunner(exposure_outputs(view="keep_defensive")))
        cls.shadow = ExposureHarness(base / "shadow", version="v1")
        cls.shadow_runner = FakeRunner(exposure_outputs())
        cls.shadow_result, _ = cls.shadow.run(cls.shadow_runner)
        # 對照組：同一個世界、有 exposure_view 但沒有 agent_swap.exposure（提名只有 thesis 形狀，沒有 purpose）。
        cls.plain = ExposureHarness(base / "plain")
        template = json.loads(cls.plain.world.paths["template"].read_text(encoding="utf-8"))
        del template["position_sizing"]["agent_swap"]["exposure"]
        cls.plain.world.paths["template"].write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
        plain_pair = {key: value for key, value in EXPOSURE_PAIR.items() if key != "purpose"}
        plain_pair.update(sell_basis_ids=["bear-2330-1"])
        cls.plain_runner = FakeRunner(exposure_outputs({"nominations": [plain_pair], "thesis_hint_responses": []}))
        cls.plain_result, _ = cls.plain.run(cls.plain_runner)
        cls.rebuild_art = cls.rebuild.artifacts()
        cls.defensive_art = cls.defensive.artifacts()
        cls.shadow_art = cls.shadow.artifacts()
        cls.plain_art = cls.plain.artifacts()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def load(self, harness, name):
        return json.loads((harness.world.run_dir / ("%s.json" % name)).read_text(encoding="utf-8"))

    def test_all_runs_complete_and_are_rebuilt_by_the_finalizer(self):
        for label, harness, result, art in (
            ("rebuild", self.rebuild, self.rebuild_result, self.rebuild_art),
            ("defensive", self.defensive, self.defensive_result, self.defensive_art),
            ("shadow", self.shadow, self.shadow_result, self.shadow_art),
            ("plain", self.plain, self.plain_result, self.plain_art),
        ):
            with self.subTest(label):
                self.assertEqual(result.status, "completed", result.errors)
                self.assertEqual(art["decision"]["errors"], [])
                self.assertEqual(harness.validate(art), [])
                self.assertEqual(harness.rebuild_errors(art), [])

    def test_the_pair_with_its_purpose_and_the_stances_view_are_bound_into_the_policy(self):
        art = self.rebuild_art
        gate = art["policy"]["position_sizing"]["agent_swap"]
        self.assertEqual(gate["nominated_pairs"], [{"buy_symbol": "2317.TW", "sell_symbol": "2330.TW", "purpose": "exposure"}])
        self.assertEqual(gate["exposure"], {"version": "v2", "max_pairs_by_view": CAPS, "view": "start_rebuild"})
        self.assertEqual(art["team_inputs"]["cash_stance"]["exposure_view"], "start_rebuild")
        # team_inputs 仍是 2.9，選配鍵 exposure_facts 與 swap_nomination 並存；提名封存帶 purpose 與空的 lower_conviction_review。
        team = art["team_inputs"]
        self.assertEqual(team["schema_version"], "2.9")
        self.assertIn("exposure_facts", team)
        nomination = team["swap_nomination"]
        self.assertEqual((nomination["status"], nomination["lower_conviction_review"]), ("completed", []))
        self.assertEqual(nomination["nominations"][0]["purpose"], "exposure")

    def test_start_rebuild_executes_the_exposure_pair_without_rewriting_the_intent(self):
        art = self.rebuild_art
        orders = {(o["symbol"], o["side"]): o["intent"] for o in art["decision"]["orders"]}
        self.assertEqual(orders, {("2330.TW", "sell"): "exit", ("2317.TW", "buy"): "buy"})
        found = {(f["symbol"], f["code"]) for f in art["proposal"]["constraint_flags"]}
        self.assertIn(("2330.TW", "AGENT_SWAP"), found)
        victim = next(f for f in art["proposal"]["constraint_flags"] if f["code"] == "AGENT_SWAP")
        self.assertIn("目的：曝險", victim["detail"])
        for item in art["intent"]["items"]:
            self.assertEqual(item["intent"], {"2330.TW": "hold", "2317.TW": "buy"}[item["symbol"]])

    def test_the_exposure_pair_uses_the_same_orders_as_the_same_pair_nominated_for_a_thesis(self):
        self.assertEqual(self.rebuild_art["decision"]["orders"], self.plain_art["decision"]["orders"])
        self.assertEqual(self.rebuild_art["proposal"]["allocation_proposal"], self.plain_art["proposal"]["allocation_proposal"])

    def test_keep_defensive_cancels_the_pair_and_sells_nothing(self):
        art = self.defensive_art
        self.assertEqual(art["policy"]["position_sizing"]["agent_swap"]["exposure"]["view"], "keep_defensive")
        self.assertIn("AGENT_SWAP_CANCELLED:exposure_view", codes(art["proposal"]))
        self.assertFalse(codes(art["proposal"]) & {"AGENT_SWAP", "AGENT_SWAP_FILL"})
        self.assertEqual(art["decision"]["orders"], [])  # 換出者不賣；程式不補挑

    def test_the_shadow_version_does_not_read_the_exposure_pair(self):
        art = self.shadow_art
        gate = art["policy"]["position_sizing"]["agent_swap"]
        self.assertEqual(gate["exposure"], {"version": "v1"})  # 影子不綁 view
        self.assertEqual(gate["nominated_pairs"], [{"buy_symbol": "2317.TW", "sell_symbol": "2330.TW", "purpose": "exposure"}])
        self.assertFalse([code for code in codes(art["proposal"]) if code.startswith("AGENT_SWAP")])
        self.assertEqual(art["decision"]["orders"], self.defensive_art["decision"]["orders"])  # 與沒有換股時相同
        self.assertEqual(art["team_inputs"]["swap_nomination"]["nominations"][0]["purpose"], "exposure")  # 提名照常封存

    def test_the_brief_and_prompt_describe_the_purpose_rules(self):
        brief = self.load(self.rebuild, "brief_swap_nomination")
        self.assertIn("exposure_swap", brief)
        self.assertEqual(brief["exposure_swap"]["lower_conviction_symbols"], ["2330.TW"])
        holding = next(row for row in brief["holdings"] if row["symbol"] == "2330.TW")
        self.assertEqual(holding["exposure_basis"]["id"], "review:2330.TW:exposure")
        self.assertTrue(holding["exposure_basis"]["evidence_ids"])
        self.assertIn("purpose=exposure", brief["exposure_facts"]["note"])
        prompt = dict(self.rebuild_runner.prompts)["swap_nominator"]
        self.assertIn("lower_conviction_review", prompt)
        self.assertIn("purpose", prompt)
        for text in (prompt, json.dumps(brief, ensure_ascii=False)):
            self.assertNotIn("影子", text)
        # v1 與 v2 的簡報、提示完全相同：Agent 不會被告知目前是影子期。
        self.assertEqual(brief, self.load(self.shadow, "brief_swap_nomination"))
        strip = lambda text, harness: text.replace(str(harness.world.run_dir), "RUN")  # noqa: E731  提示含各自的 run 路徑
        self.assertEqual(strip(prompt, self.rebuild), strip(dict(self.shadow_runner.prompts)["swap_nominator"], self.shadow))

    def test_without_the_exposure_gate_brief_prompt_and_nomination_are_unchanged(self):
        brief = self.load(self.plain, "brief_swap_nomination")
        self.assertNotIn("exposure_swap", brief)
        for row in brief["holdings"]:
            self.assertFalse({"exposure_basis", "exposure_nominable"} & set(row))
        self.assertNotIn("purpose", brief["exposure_facts"]["note"])  # 階段 1 的背景說明逐字不變
        prompt = dict(self.plain_runner.prompts)["swap_nominator"]
        self.assertNotIn("lower_conviction_review", prompt)
        nomination = self.plain_art["team_inputs"]["swap_nomination"]
        self.assertNotIn("lower_conviction_review", nomination)
        self.assertNotIn("purpose", nomination["nominations"][0])
        self.assertEqual(self.plain_art["policy"]["position_sizing"]["agent_swap"]["nominated_pairs"],
                         [{"buy_symbol": "2317.TW", "sell_symbol": "2330.TW"}])

    # -- fail-closed：Finalizer
    def assertRejected(self, errors, token):
        self.assertTrue(any(token in error for error in errors), (token, errors))

    def rejects(self, harness, art, **replace):
        try:
            return harness.rebuild_errors(art, **replace)
        except DecisionToolError as error:
            return [str(error)]

    def test_a_view_that_differs_from_the_stance_is_rejected(self):
        art = self.rebuild_art
        tampered = sealed(art["policy"], lambda p: p["position_sizing"]["agent_swap"]["exposure"].update(view="rebuild"))
        self.assertRejected(self.rejects(self.rebuild, art, policy=tampered), "exposure.view 與現金姿態的 exposure_view 不一致")
        cheated = sealed(art["policy"], lambda p: p["position_sizing"]["agent_swap"]["exposure"].update(view="keep_defensive"))
        self.assertRejected(self.rejects(self.rebuild, art, policy=cheated), "exposure.view 與現金姿態的 exposure_view 不一致")
        stance = copy.deepcopy(art["team_inputs"])
        stance["cash_stance"]["exposure_view"] = "keep_defensive"  # 改寫姿態讓它與 Policy 不一致
        self.assertTrue(self.rejects(self.rebuild, art, team_inputs=stance))

    def test_an_unbound_view_is_rejected(self):
        art = self.rebuild_art
        unbound = sealed(art["policy"], lambda p: p["position_sizing"]["agent_swap"]["exposure"].pop("view"))
        errors = self.rejects(self.rebuild, art, policy=unbound)
        self.assertTrue(errors, errors)

    def test_the_shadow_version_must_not_carry_a_view(self):
        art = self.shadow_art
        sneaked = sealed(art["policy"], lambda p: p["position_sizing"]["agent_swap"]["exposure"].update(view="rebuild"))
        self.assertTrue(self.rejects(self.shadow, art, policy=sneaked))

    def test_a_rewritten_purpose_or_pair_in_the_policy_is_rejected(self):
        art = self.rebuild_art
        for label, mutate in {
            "purpose 被改成 thesis": lambda g: g["nominated_pairs"][0].update(purpose="thesis"),
            "purpose 被拿掉": lambda g: g["nominated_pairs"][0].pop("purpose"),
            "配對被清空": lambda g: g.update(nominated_pairs=[]),
        }.items():
            with self.subTest(label):
                tampered = sealed(art["policy"], lambda p: mutate(p["position_sizing"]["agent_swap"]))
                self.assertTrue(self.rejects(self.rebuild, art, policy=tampered), label)

    def test_missing_or_forged_exposure_facts_and_a_tampered_nomination_are_rejected(self):
        art = self.rebuild_art
        team = copy.deepcopy(art["team_inputs"])
        del team["exposure_facts"]
        self.assertRejected(self.rejects(self.rebuild, art, team_inputs=team), "exposure_facts")
        for label, mutate in {
            "purpose 改成 thesis（依據不再合格）": lambda t: t["swap_nomination"]["nominations"][0].update(purpose="thesis"),
            "曝險編號換成別檔": lambda t: t["swap_nomination"]["nominations"][0].update(sell_basis_ids=["review:2317.TW:exposure"]),
            "加上不需要的交代清單": lambda t: t["swap_nomination"].update(lower_conviction_review=[
                {"symbol": "2330.TW", "response": "kept", "reason": "x"}]),
            "拿掉交代清單": lambda t: t["swap_nomination"].pop("lower_conviction_review"),
        }.items():
            with self.subTest(label):
                tampered = copy.deepcopy(art["team_inputs"])
                mutate(tampered)
                self.assertRejected(self.rejects(self.rebuild, art, team_inputs=tampered), "swap_nomination")


class HighConvictionAndDegradedTests(unittest.TestCase):
    def test_a_high_conviction_victim_needs_only_an_empty_review_when_no_other_holding_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = ExposureHarness(directory)
            result, _ = harness.run(FakeRunner(exposure_outputs(conviction="high")))
            self.assertEqual(result.status, "completed", result.errors)
            art = harness.artifacts()
            self.assertEqual(art["team_inputs"]["swap_nomination"]["status"], "completed")
            self.assertEqual(harness.validate(art), [])

    def test_an_invalid_nomination_is_exhausted_with_an_empty_review_and_the_rest_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = ExposureHarness(directory)
            result, _ = harness.run(FakeRunner(exposure_outputs(BAD_NOMINATION, "start_rebuild", None, BAD_NOMINATION, BAD_NOMINATION)))
            self.assertEqual(result.status, "completed", result.errors)
            art = harness.artifacts()
            nomination = art["team_inputs"]["swap_nomination"]
            self.assertEqual((nomination["status"], nomination["nominations"], nomination["lower_conviction_review"]), ("exhausted", [], []))
            self.assertIn("AGENT_SWAP_SKIPPED:nomination_exhausted", codes(art["proposal"]))
            self.assertEqual(art["decision"]["orders"], [])
            self.assertEqual(harness.validate(art), [])

    def test_the_gateless_template_keeps_the_old_flow_bit_for_bit(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = V2Harness(directory)  # 沒有 exposure_view、沒有 agent_swap.exposure
            plain_pair = {key: value for key, value in EXPOSURE_PAIR.items() if key != "purpose"}
            plain_pair.update(sell_basis_ids=["bear-2330-1"])
            result, _ = harness.run(FakeRunner(outputs({"nominations": [plain_pair], "thesis_hint_responses": []})))
            self.assertEqual(result.status, "completed", result.errors)
            art = harness.artifacts()
            self.assertNotIn("exposure", art["policy"]["position_sizing"]["agent_swap"])
            self.assertNotIn("lower_conviction_review", art["team_inputs"]["swap_nomination"])
            self.assertEqual(harness.validate(art), [])


if __name__ == "__main__":
    unittest.main()
