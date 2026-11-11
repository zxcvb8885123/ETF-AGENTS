"""agent_swap.entry_reserve v1（已實作、尚未開啟）接入每日決策鏈的端到端測試。

用 v3 對照組樣板（正式設定降版、不含 ladder）加人填的 entry_reserve，跑真實 pipeline 與 Finalizer：

- 每組提名帶 size（Agent 只填列舉），Policy 的 nominated_pairs 綁定同一個 size，team_inputs 維持 agent_swap 的 2.9（entry_reserve 不升版）、沒有新鍵。
- 竄改 Policy 綁定的 size（或拿掉）：Finalizer 重建拒絕。
- 缺 size 的提名在驗證階段一律退回，用盡後提名 exhausted，不依提名換股。
- 沒有 entry_reserve 的對照組：Policy 綁定沒有 size。
"""

import copy
import tempfile
import unittest
from pathlib import Path

from etf_agent.decision import DecisionToolError, decision_policy_sha256

from test_daily_pipeline import FakeRunner
from test_quality_dip_ruling_pipeline import THESIS_PAIR, Harness3, live_like_template, team_outputs
from test_swap_nomination_pipeline import NOMINATION, outputs
from test_quality_dip_ruling_pipeline import stance

RESERVE = {"version": "v1", "max_entry_weight": "0.1", "max_nominations": 8}  # 正式設定的成交上限是 5，清單上限須不小於它


def template_reserve():
    value = live_like_template("v3")
    sizing = value["position_sizing"]
    sizing["agent_swap"]["quality_dip"].pop("ruling_ref", None)
    sizing["agent_swap"].pop("quality_dip", None)  # 純 entry_reserve：quality_dip 另有自己的 size，這裡不並存
    sizing.pop("quality_dip_facts", None)
    sizing.pop("quality_dip_assessment", None)
    sizing.pop("event_direction", None)
    sizing["agent_swap"]["entry_reserve"] = dict(RESERVE)
    return value


def reserve_outputs(size="large", repeat=1):
    pair = dict(THESIS_PAIR, size=size) if size else dict(THESIS_PAIR)
    return outputs(*[dict(NOMINATION, nominations=[pair], lower_conviction_review=[]) for _ in range(repeat)], cash_stance=[stance()])


class EntryReservePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        for name in ("sized", "unsized", "plain"):
            (base / name).mkdir()
        cls.sized = Harness3(base / "sized", template=template_reserve())
        cls.result, _ = cls.sized.run(FakeRunner(reserve_outputs("large")))
        cls.art = cls.sized.artifacts() if cls.result.status == "completed" else {}
        cls.unsized = Harness3(base / "unsized", template=template_reserve())
        cls.unsized_result, _ = cls.unsized.run(FakeRunner(reserve_outputs(None, repeat=6)))
        cls.plain = Harness3(base / "plain", "v3")
        cls.plain_result, _ = cls.plain.run(FakeRunner(team_outputs("v3")))
        cls.plain_art = cls.plain.artifacts() if cls.plain_result.status == "completed" else {}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_the_run_completes_binds_the_size_and_is_rebuildable_without_a_new_team_key(self):
        self.assertEqual(self.result.status, "completed", self.result.errors)
        swap = self.art["policy"]["position_sizing"]["agent_swap"]
        self.assertEqual(swap["entry_reserve"], RESERVE)
        self.assertTrue(swap["nominated_pairs"])
        self.assertTrue(all(pair.get("size") == "large" for pair in swap["nominated_pairs"]))
        team = self.art["team_inputs"]
        self.assertEqual(team["schema_version"], "2.9")  # agent_swap 的基準版；entry_reserve 不升版
        self.assertNotIn("entry_reserve", team)
        self.assertEqual(self.sized.validate(self.art), [])
        self.assertEqual(self.sized.rebuild_errors(self.art), [])

    def test_a_forged_or_missing_policy_size_is_rejected(self):
        for change in ("medium", None):
            policy = copy.deepcopy(self.art["policy"])
            pairs = policy["position_sizing"]["agent_swap"]["nominated_pairs"]
            for pair in pairs:
                if change:
                    pair["size"] = change
                else:
                    del pair["size"]
            policy["content_sha256"] = decision_policy_sha256(policy)
            try:
                problems = self.sized.rebuild_errors(self.art, policy=policy)
            except DecisionToolError:
                problems = ["raised"]
            self.assertTrue(problems, change)

    def test_a_nomination_without_size_never_becomes_a_swap(self):
        if self.unsized_result.status == "completed":
            art = self.unsized.artifacts()
            ref = art["policy"]["position_sizing"]["agent_swap"]["nomination_ref"]
            self.assertNotEqual(ref["status"], "completed")
            self.assertNotIn("AGENT_SWAP", {f["code"] for f in art["proposal"]["constraint_flags"]})
        else:
            self.assertTrue(self.unsized_result.errors)  # 流程失敗也是 fail-closed，不得靠預設 size 放行

    def test_the_plain_run_has_no_size_binding(self):
        self.assertEqual(self.plain_result.status, "completed", self.plain_result.errors)
        swap = self.plain_art["policy"]["position_sizing"]["agent_swap"]
        self.assertNotIn("entry_reserve", swap)
        self.assertFalse(any("size" in pair for pair in swap.get("nominated_pairs", []) if pair.get("purpose") != "quality_dip"))


if __name__ == "__main__":
    unittest.main()
