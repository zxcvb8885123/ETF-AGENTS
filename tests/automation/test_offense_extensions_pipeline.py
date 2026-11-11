"""offense 的選配子欄位 ``thesis_size_cap`` 與 ``cash_glide``（已實作、尚未開啟）接入每日決策鏈的端到端測試。

沿用 test_offense_pipeline 的樣板（記憶體內修改，config 不動）：有欄位時 pipeline 完成、Policy 綁定 ``cash_raise_limited``（fixture 沒有前次 run，
anchor 是 account_cash，所以不滑行、記錄為 null）、Finalizer 重建乾淨、竄改綁定紀錄被拒、換股提名簡報與提示出現新句子；
沒有欄位的對照組（預設樣板）簡報、提示與 Policy 綁定逐位元沒有新鍵。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.decision import DecisionToolError, decision_policy_sha256

from test_daily_pipeline import FakeRunner
from test_offense_pipeline import template_offense
from test_quality_dip_ruling_pipeline import Harness3, team_outputs

THESIS_SENTENCE = "份量格也受該級 max_size 限制"


def template_with_fields():
    value = template_offense()
    value["position_sizing"]["offense"]["thesis_size_cap"] = {"version": "v1"}
    value["position_sizing"]["offense"]["cash_glide"] = {"version": "v1"}
    return value


class OffenseExtensionPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        for name in ("on", "off"):
            (base / name).mkdir()
        cls.on = Harness3(base / "on", template=template_with_fields())
        cls.runner = FakeRunner(team_outputs("v4"))
        cls.result, _ = cls.on.run(cls.runner)
        cls.art = cls.on.artifacts() if cls.result.status == "completed" else {}
        off_template = template_offense()  # 正式設定已含兩個欄位：對照組明確移除
        for field in ("thesis_size_cap", "cash_glide"):
            off_template["position_sizing"]["offense"].pop(field, None)
        cls.off = Harness3(base / "off", template=off_template)
        cls.off_runner = FakeRunner(team_outputs("v4"))
        cls.off_result, _ = cls.off.run(cls.off_runner)
        cls.off_art = cls.off.artifacts() if cls.off_result.status == "completed" else {}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_run_completes_and_binds_a_null_record_without_a_prior_run(self):
        self.assertEqual(self.result.status, "completed", self.result.errors)
        policy = self.art["policy"]["position_sizing"]
        self.assertIn("cash_raise_limited", policy["offense"])
        self.assertIsNone(policy["offense"]["cash_raise_limited"])
        self.assertEqual(policy["cash_ratio_range"]["anchor"]["status"], "account_cash")
        self.assertNotIn("prior_offense_loosened", policy["cash_ratio_range"]["anchor"])
        self.assertEqual(self.on.validate(self.art), [])
        self.assertEqual(self.on.rebuild_errors(self.art), [])

    def test_without_the_fields_nothing_new_appears(self):
        self.assertEqual(self.off_result.status, "completed", self.off_result.errors)
        policy = self.off_art["policy"]["position_sizing"]
        self.assertNotIn("cash_raise_limited", policy["offense"])
        self.assertNotIn("thesis_size_cap", policy["offense"])
        self.assertNotIn("prior_offense_loosened", policy["cash_ratio_range"]["anchor"])
        for task, prompt in self.off_runner.prompts:
            self.assertNotIn(THESIS_SENTENCE, prompt, task)

    def test_nomination_prompt_and_brief_mention_the_cap_only_when_the_field_is_on(self):
        prompts = dict(self.runner.prompts)
        self.assertIn(THESIS_SENTENCE, prompts["swap_nominator"])
        briefs = [path for path in (self.on.world.run_dir).glob("brief_swap_nomination.json")]
        self.assertTrue(briefs)
        brief = json.loads(briefs[0].read_text(encoding="utf-8"))
        self.assertIn(THESIS_SENTENCE, json.dumps(brief, ensure_ascii=False))
        off_briefs = list(self.off.world.run_dir.glob("brief_swap_nomination.json"))
        self.assertNotIn(THESIS_SENTENCE, json.dumps(json.loads(off_briefs[0].read_text(encoding="utf-8")), ensure_ascii=False))

    def rejected(self, **replace):
        try:
            return self.on.rebuild_errors(self.art, **replace)
        except DecisionToolError as error:
            return ["raised: %s" % error]

    def test_tampering_with_the_bound_record_or_the_anchor_flag_is_rejected(self):
        for label, mutate in {
            "偽造現金回升限制": lambda s: s["offense"].update(cash_raise_limited={"from": "0.15", "to": "0.13"}),
            "拿掉綁定紀錄": lambda s: s["offense"].pop("cash_raise_limited"),
            "anchor 偽造前次放寬旗標": lambda s: s["cash_ratio_range"]["anchor"].update(prior_offense_loosened=True),
            "拿掉 cash_glide": lambda s: s["offense"].pop("cash_glide"),
        }.items():
            policy = copy.deepcopy(self.art["policy"])
            mutate(policy["position_sizing"])
            policy["content_sha256"] = decision_policy_sha256(policy)
            self.assertTrue(self.rejected(policy=policy), label)


if __name__ == "__main__":
    unittest.main()
