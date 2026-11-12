"""優質股 v3（agent_swap.quality_dip version=v3）接入每日決策鏈與 team_inputs 2.10 的端到端測試。

設計見 docs/quality_dip_agent_sizing_plan.md。整條鏈用正式樣板（只拿掉 fixture 沒有資料的 cash_stance_recency 與 v2 互斥的舊換股路徑），
把 quality_dip 改成 v3（組數與權重上限只放在測試內，不是 config 的值），跑真實 pipeline 與真實 Finalizer：

- 提名只有 thesis 配對：裁決由程式封存 not_required、不呼叫風險 Agent；team_inputs 為 2.10，整份決策可重建。
- 封存內容、Policy 綁定被改寫或缺少 quality_dip_ruling：Finalizer 拒絕。
- 沒有 v3（正式樣板的 v2）：team_inputs 維持 2.9、沒有 quality_dip_ruling 鍵。

裁決的 completed／exhausted／unavailable／skipped 與配置效果在 tests/decision/test_quality_dip_sizing.py 以單元層級覆蓋
（fixture 的行情不會產生被大盤拖累的候選，端到端造不出 quality_dip 配對）。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.agent_runner import RISK_INDEPENDENT_DENY_GLOBS
from etf_agent.decision import decision_policy_sha256

from test_agent_swap_v2_pipeline import V2Harness
from test_daily_pipeline import STANCE, FakeRunner, PipelineWorld, live_policy_template, live_without_offense
from test_swap_nomination_pipeline import GOOD_PAIR, NOMINATION, outputs

V3_GATE = {"max_swaps_per_run": 1, "max_entry_weight": "0.2"}
THESIS_PAIR = dict(GOOD_PAIR, purpose="thesis")


def live_like_template(version):
    value = live_policy_template() if version == "v4" else live_without_offense()  # 降版對照組沒有 offense（它依賴 v4 的 entry_reserve 等）
    sizing = value["position_sizing"]
    sizing.pop("cash_stance_recency")
    sizing["rotation"].pop("forced_swap", None)
    sizing["exposure_rotation"].pop("conviction_gap_swap", None)
    value["max_turnover_rate"] = "1"
    if version == "v4":  # 正式設定原樣（v4＋ladder＋oversold，exposure_view v2）：只套 fixture 需要的調整
        return value
    sizing["agent_swap"].pop("entry_reserve", None)  # 對照組沒有份量格：提名不填 size（entry_reserve 只由 v4 正式設定樣板涵蓋）
    sizing["agent_swap"]["quality_dip"].pop("ladder", None)  # 正式設定是 v4；v3／v2 對照組要明確降版，ladder 只用於 v4
    sizing["agent_swap"]["quality_dip"]["min_turn_assessment"] = "turn_unconfirmed"  # no_turn_evidence 只有 v4＋ladder 才允許
    sizing["exposure_view"] = {"version": "v1"}  # 對照組維持舊簡報（v2 的簡報文字是正式設定的行為，由 v4 測試涵蓋）
    if version == "v3":
        sizing["agent_swap"]["quality_dip"].update(version="v3", **V3_GATE)
    else:
        # v2 對照組要明確降回 v2 形狀（v2 沒有 oversold 與 v3 的組數欄位）。
        sizing["agent_swap"]["quality_dip"].pop("oversold", None)
        sizing["agent_swap"]["quality_dip"].update(version="v2", max_swaps_per_run=1, max_entry_weight="0.03")
    return value


class Harness3(V2Harness):
    def __init__(self, directory, version="v3", template=None):
        self.world = PipelineWorld(Path(directory), approved_status=True, template=template or live_like_template(version))
        account = json.loads(self.world.paths["account"].read_text(encoding="utf-8"))
        account.update(cash="1000000", settled_cash="1000000", nav="1164000")
        self.world.paths["account"].write_text(json.dumps(account, ensure_ascii=False), encoding="utf-8")
        rules = json.loads(self.world.paths["rules"].read_text(encoding="utf-8"))
        rules["max_positions"] = 1
        self.world.paths["rules"].write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")


def stance():
    return dict(STANCE, cash_grade="middle", turn_assessment="no_turn_evidence", exposure_view="keep_defensive")


def team_outputs(version):
    # 曝險提名（agent_swap.exposure）開著時 lower_conviction_review 必填；fixture 沒有可提名的 quality_dip 目標，不得填 decline。
    pair = dict(THESIS_PAIR, size="large") if version == "v4" else THESIS_PAIR  # 正式設定有 entry_reserve：每組都要填份量格
    nomination = dict(NOMINATION, nominations=[pair], lower_conviction_review=[])
    return outputs(nomination, cash_stance=[stance()])


class QualityDipRulingPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        for name in ("v3", "v2"):
            (base / name).mkdir()
        cls.v3 = Harness3(base / "v3")
        cls.runner = FakeRunner(team_outputs("v3"))
        cls.result, _ = cls.v3.run(cls.runner)
        cls.art = cls.v3.artifacts() if cls.result.status == "completed" else {}
        cls.v2 = Harness3(base / "v2", "v2")
        cls.v2_result, _ = cls.v2.run(FakeRunner(team_outputs("v2")))
        cls.v2_art = cls.v2.artifacts() if cls.v2_result.status == "completed" else {}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_a_thesis_only_nomination_needs_no_ruling_and_the_run_is_2_10_and_rebuildable(self):
        self.assertEqual(self.result.status, "completed", self.result.errors)
        team = self.art["team_inputs"]
        self.assertEqual(team["schema_version"], "2.10")
        ruling = team["quality_dip_ruling"]
        self.assertEqual((ruling["status"], ruling["rulings"]), ("not_required", []))
        self.assertNotIn("quality_dip_ruler", [name for name, _ in self.runner.prompts])  # 不需要時不呼叫風險 Agent
        gate = self.art["policy"]["position_sizing"]["agent_swap"]["quality_dip"]
        self.assertEqual((gate["version"], gate["rulings"]), ("v3", []))
        self.assertEqual(gate["ruling_ref"]["status"], "not_required")
        self.assertEqual(gate["ruling_ref"]["sha256"], ruling["content_sha256"])
        self.assertEqual(self.v3.validate(self.art), [])
        self.assertEqual(self.v3.rebuild_errors(self.art), [])

    def test_the_thesis_pair_is_still_traded_like_in_v2(self):
        self.assertEqual(self.v2_result.status, "completed", self.v2_result.errors)
        for key in ("orders",):
            self.assertEqual(self.art["proposal"]["order_proposal"][key], self.v2_art["proposal"]["order_proposal"][key])

    def test_tampering_with_the_sealed_ruling_or_the_policy_binding_is_rejected(self):
        art = self.art
        forged_team = copy.deepcopy(art["team_inputs"])
        forged_team["quality_dip_ruling"]["rulings"] = [{"buy_symbol": "2317.TW", "sell_symbol": "2330.TW", "ruling": "approve"}]
        self.assertTrue(self.v3.rebuild_errors(art, team_inputs=forged_team))
        missing = copy.deepcopy(art["team_inputs"])
        del missing["quality_dip_ruling"]
        self.assertTrue(self.v3.rebuild_errors(art, team_inputs=missing))
        policy = copy.deepcopy(art["policy"])
        policy["position_sizing"]["agent_swap"]["quality_dip"]["ruling_ref"]["status"] = "exhausted"
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.assertTrue(self.v3.rebuild_errors(art, policy=policy))
        downgraded = copy.deepcopy(art["team_inputs"])
        downgraded["schema_version"] = "2.9"  # 版本與 v3 閘門不同步（雙向）：拒絕
        self.assertTrue(self.v3.rebuild_errors(art, team_inputs=downgraded))

    def test_without_v3_the_run_stays_2_9_and_has_no_ruling(self):
        team = self.v2_art["team_inputs"]
        self.assertEqual(team["schema_version"], "2.9")
        self.assertNotIn("quality_dip_ruling", team)
        self.assertEqual(self.v2.rebuild_errors(self.v2_art), [])
        upgraded = copy.deepcopy(team)
        upgraded["schema_version"] = "2.10"  # v2 的 run 不得宣稱 2.10
        self.assertTrue(self.v2.rebuild_errors(self.v2_art, team_inputs=upgraded))


if __name__ == "__main__":
    unittest.main()


class QualityDipRulerDenyReadsTests(unittest.TestCase):
    """裁決任務與現金姿態同為獨立判斷：能強制拒讀的執行器帶拒讀樣式，Codex 型不帶（否則被拒絕啟動）。"""

    def test_ruler_task_is_built_with_the_same_independent_deny_reads(self):
        from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            for enforces, expected in ((True, RISK_INDEPENDENT_DENY_GLOBS), (False, ())):
                runner = FakeRunner({})
                runner.enforces_deny_reads = enforces
                pipeline = DailyDecisionPipeline(root, Path(directory), runner, Path(directory) / "decisions", log=lambda _: None)
                self.assertEqual(tuple(pipeline._independent_deny_reads()), expected)
        self.assertNotIn("quality_dip_ruling*", RISK_INDEPENDENT_DENY_GLOBS)  # 裁決自己的簡報必須可讀
        self.assertIn("swap_nomination*", RISK_INDEPENDENT_DENY_GLOBS)
        self.assertIn("trade_decision*", RISK_INDEPENDENT_DENY_GLOBS)

    def test_the_ruler_task_uses_the_helper(self):
        source = (Path(__file__).resolve().parents[2] / "src/etf_agent/automation/risk_stages.py").read_text(encoding="utf-8")
        start = source.index('"quality_dip_ruler"')
        self.assertIn("deny_reads=self._independent_deny_reads()", source[start:start + 3000])
