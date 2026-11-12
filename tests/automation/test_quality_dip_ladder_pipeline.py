"""優質股 v4（agent_swap.quality_dip version=v4，連跌後逢低布局）接入每日決策鏈與 team_inputs 的端到端測試。

用正式設定原樣（v4＋人填的 ladder／oversold、exposure_view v2；只拿掉 fixture 沒有資料的 cash_stance_recency 與 v2 互斥的舊換股路徑），
跑真實 pipeline 與真實 Finalizer，證明正式設定下 decision run 會 completed：

- fixture 的行情沒有回檔、風險 Agent 填 no_turn_evidence：梯度 closed（depth_below_gate），整條鏈照常完成；
  team_inputs 為 2.10 並多一個選配鍵 decline_depth_facts，Policy 綁定 ladder_state，Finalizer 整份重算。
- 竄改封存的事實、Policy 綁定的梯度狀態、缺少事實鍵：Finalizer 拒絕；沒有 v4 的 v3 run 不得帶這個鍵。
- 沒有 ladder 的 v3：team_inputs 欄位與舊版相同（沒有 decline_depth_facts）。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.decision import DecisionToolError, decision_policy_sha256

from test_daily_pipeline import FakeRunner, PipelineWorld
from test_quality_dip_ruling_pipeline import V3_GATE, Harness3, live_like_template, team_outputs

LADDER = {
    "min_pool_drawdown": "0.10", "max_dip_exposure": "0.9", "max_tranches": 3, "min_days_between_tranches": 2,
    "pause_after_further_drop": "0.05",
}


def template_v4():
    """v4＋人填的 ladder／oversold、exposure_view v2，**明確移除** ladder.exposure_basis（對照組：舊的 ladder 曝險計法）。

    正式設定已含 exposure_basis quality_dip_fills（2026-11-11 offense-ext-1），原樣的樣板見 ``template_live``。"""
    value = live_like_template("v4")
    value["position_sizing"]["agent_swap"]["quality_dip"]["ladder"].pop("exposure_basis", None)
    return value


def template_live():
    """正式設定原樣（含 offense.thesis_size_cap／cash_glide 與 ladder.exposure_basis）。"""
    return live_like_template("v4")


def template_view_v1():
    value = template_v4()
    value["position_sizing"]["exposure_view"] = {"version": "v1"}
    value["position_sizing"].pop("offense", None)  # offense 需要 exposure_view v2；max_pairs_by_view 還原成沒有 probe 的兩鍵
    value["position_sizing"].pop("cash_stance_brief", None)  # 前提閘門被拿掉時，依賴它們的簡報閘門一併拿掉
    value["position_sizing"]["agent_swap"]["exposure"]["max_pairs_by_view"].pop("probe", None)
    return value


def template_fills_only(prefilled=False):
    value = template_v4()
    value["position_sizing"]["agent_swap"]["quality_dip"]["ladder"]["exposure_basis"] = "quality_dip_fills"
    if prefilled:
        value["position_sizing"]["agent_swap"]["quality_dip"]["ladder_entries"] = []
    return value


def template_v3_reserved():
    """v3 對照組也帶正式設定的 entry_reserve：對照的是 v4（ladder）的差別，不是份量格保留預算的差別。"""
    value = live_like_template("v3")
    value["position_sizing"]["agent_swap"]["entry_reserve"] = copy.deepcopy(
        template_v4()["position_sizing"]["agent_swap"]["entry_reserve"])
    return value


class Harness4(Harness3):
    def __init__(self, directory, template=template_v4):
        self.world = PipelineWorld(Path(directory), approved_status=True, template=template())
        account = json.loads(self.world.paths["account"].read_text(encoding="utf-8"))
        account.update(cash="1000000", settled_cash="1000000", nav="1164000")
        self.world.paths["account"].write_text(json.dumps(account, ensure_ascii=False), encoding="utf-8")
        rules = json.loads(self.world.paths["rules"].read_text(encoding="utf-8"))
        rules["max_positions"] = 1
        self.world.paths["rules"].write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")


class LadderPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        (base / "v4").mkdir()
        (base / "v3").mkdir()
        cls.v4 = Harness4(base / "v4")
        cls.runner = FakeRunner(team_outputs("v4"))
        cls.result, _ = cls.v4.run(cls.runner)
        cls.art = cls.v4.artifacts() if cls.result.status == "completed" else {}
        cls.v3 = Harness3(base / "v3", "v3", template=template_v3_reserved())
        cls.v3_result, _ = cls.v3.run(FakeRunner(team_outputs("v4")))
        cls.v3_art = cls.v3.artifacts() if cls.v3_result.status == "completed" else {}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_the_run_completes_with_the_facts_key_and_a_closed_bound_ladder_and_is_rebuildable(self):
        self.assertEqual(self.result.status, "completed", self.result.errors)
        team = self.art["team_inputs"]
        self.assertEqual(team["schema_version"], "2.10")
        facts = team["decline_depth_facts"]
        self.assertEqual(facts["data_status"], "available")
        self.assertEqual(facts["pool"]["drawdown_from_window_high"], "0.000000")  # fixture 的行情沒有回檔
        self.assertFalse(facts["citable"])
        gate = self.art["policy"]["position_sizing"]["agent_swap"]["quality_dip"]
        self.assertEqual(gate["version"], "v4")
        self.assertEqual(gate["ladder_state"]["status"], "closed")
        self.assertEqual(gate["ladder_state"]["facts_id"], facts["facts_id"])
        self.assertEqual(self.v4.validate(self.art), [])
        self.assertEqual(self.v4.rebuild_errors(self.art), [])

    def test_the_v4_run_trades_like_the_v3_run_when_there_is_no_quality_dip_pair(self):
        self.assertEqual(self.v3_result.status, "completed", self.v3_result.errors)
        for key in ("orders",):
            self.assertEqual(self.art["proposal"]["order_proposal"][key], self.v3_art["proposal"]["order_proposal"][key])

    def test_exposure_view_v2_switches_the_cash_stance_prompt_and_brief_note_in_the_pipeline(self):
        from etf_agent.decision.exposure_facts import RISK_NOTE, RISK_NOTE_AGENT_LED, RISK_NOTE_OFFENSE
        base = Path(self.directory.name)
        prompt = dict(self.runner.prompts)["cash_stance"]  # 正式設定是 exposure_view v2
        self.assertIn("一對都不成交", prompt)
        self.assertIn("錯過反彈", prompt)
        notes = [path.read_text(encoding="utf-8") for path in (base / "v4").rglob("brief_cash_stance.json")]
        self.assertTrue(notes)
        self.assertTrue(all(RISK_NOTE_OFFENSE in text and RISK_NOTE not in text for text in notes))  # 正式設定含 offense：簡報說明改為 offense 版
        (base / "view1").mkdir()
        runner = FakeRunner(team_outputs("v4"))
        result, _ = Harness4(base / "view1", template=template_view_v1).run(runner)
        self.assertEqual(result.status, "completed", result.errors)
        old_prompt = dict(runner.prompts)["cash_stance"]  # v1：舊提示文字
        self.assertIn("只是紀錄", old_prompt)
        self.assertNotIn("一對都不成交", old_prompt)

    def test_v3_runs_have_no_decline_depth_key_and_cannot_carry_one(self):
        team = self.v3_art["team_inputs"]
        self.assertEqual(team["schema_version"], "2.10")
        self.assertNotIn("decline_depth_facts", team)
        self.assertEqual(self.v3.rebuild_errors(self.v3_art), [])
        forged = copy.deepcopy(team)
        forged["decline_depth_facts"] = copy.deepcopy(self.art["team_inputs"]["decline_depth_facts"])
        self.assertTrue(self.v3.rebuild_errors(self.v3_art, team_inputs=forged))

    def test_tampered_missing_or_rebound_state_is_rejected(self):
        art = self.art
        forged = copy.deepcopy(art["team_inputs"])
        forged["decline_depth_facts"]["pool"]["down_streak_days"] = 7
        self.assertTrue(self.v4.rebuild_errors(art, team_inputs=forged))
        missing = copy.deepcopy(art["team_inputs"])
        del missing["decline_depth_facts"]
        self.assertTrue(self.v4.rebuild_errors(art, team_inputs=missing))
        policy = copy.deepcopy(art["policy"])
        policy["position_sizing"]["agent_swap"]["quality_dip"]["ladder_state"].update(status="open", reason=None, exposure_remaining="0.9")
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.assertTrue(self.v4.rebuild_errors(art, policy=policy))
        no_state = copy.deepcopy(art["policy"])
        del no_state["position_sizing"]["agent_swap"]["quality_dip"]["ladder_state"]
        no_state["content_sha256"] = decision_policy_sha256(no_state)
        try:
            problems = self.v4.rebuild_errors(art, policy=no_state)
        except DecisionToolError:  # 沒綁梯度狀態：配置拋錯也算拒絕，不靜默放行
            problems = ["raised"]
        self.assertTrue(problems)

    def test_quality_dip_fills_mode_binds_the_entries_fail_closed_and_the_finalizer_rebuilds_it(self):
        base = Path(self.directory.name)
        (base / "fills").mkdir()
        harness = Harness4(base / "fills", template=template_fills_only)
        result, _ = harness.run(FakeRunner(team_outputs("v4")))
        self.assertEqual(result.status, "completed", result.errors)
        art = harness.artifacts()
        gate = art["policy"]["position_sizing"]["agent_swap"]["quality_dip"]
        # 這個 harness 沒有給帳本已套用的 run 清單：讀不到就綁 null，梯度以 entries_unavailable 關閉（fail-closed）。
        self.assertIn("ladder_entries", gate)
        self.assertIsNone(gate["ladder_entries"])
        self.assertEqual((gate["ladder_state"]["status"], gate["ladder_state"]["reason"]), ("closed", "entries_unavailable"))
        self.assertEqual(gate["ladder_state"]["exposure_basis"], "quality_dip_fills")
        self.assertEqual(harness.validate(art), [])
        self.assertEqual(harness.rebuild_errors(art), [])
        forged = copy.deepcopy(art["policy"])
        forged["position_sizing"]["agent_swap"]["quality_dip"]["ladder_entries"] = [
            {"symbol": "ZZZZ.TW", "entry_date": "2026-01-02", "run_id": "decision-x"}]
        forged["content_sha256"] = decision_policy_sha256(forged)
        self.assertTrue(harness.rebuild_errors(art, policy=forged))  # 竄改清單
        absent = copy.deepcopy(art["policy"])
        del absent["position_sizing"]["agent_swap"]["quality_dip"]["ladder_entries"]
        absent["content_sha256"] = decision_policy_sha256(absent)
        self.assertTrue(harness.rebuild_errors(art, policy=absent))
        # 沒有該模式的 run 沒有清單鍵（舊行為逐位元不變）。
        self.assertNotIn("ladder_entries", self.art["policy"]["position_sizing"]["agent_swap"]["quality_dip"])
        self.assertNotIn("exposure_basis", self.art["policy"]["position_sizing"]["agent_swap"]["quality_dip"]["ladder_state"])

    def test_the_live_template_as_is_completes_and_the_finalizer_rebuilds_it_clean(self):
        """正式設定原樣（含 thesis_size_cap、cash_glide、exposure_basis）跑每日 pipeline：completed、驗證與 Finalizer 重建乾淨。"""
        base = Path(self.directory.name)
        (base / "live").mkdir()
        harness = Harness4(base / "live", template=template_live)
        result, _ = harness.run(FakeRunner(team_outputs("v4")))
        self.assertEqual(result.status, "completed", result.errors)
        art = harness.artifacts()
        offense = art["policy"]["position_sizing"]["offense"]
        self.assertEqual((offense["thesis_size_cap"], offense["cash_glide"]), ({"version": "v1"}, {"version": "v1"}))
        self.assertIn("cash_raise_limited", offense)
        self.assertEqual(art["policy"]["position_sizing"]["agent_swap"]["quality_dip"]["ladder"]["exposure_basis"], "quality_dip_fills")
        self.assertEqual(harness.validate(art), [])
        self.assertEqual(harness.rebuild_errors(art), [])

    def test_a_template_that_prefills_the_entries_is_refused(self):
        base = Path(self.directory.name)
        (base / "prefilled").mkdir()
        result, _ = Harness4(base / "prefilled", template=lambda: template_fills_only(prefilled=True)).run(FakeRunner(team_outputs("v4")))
        self.assertNotEqual(result.status, "completed")


if __name__ == "__main__":
    unittest.main()
