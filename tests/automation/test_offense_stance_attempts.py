"""offense 啟用時風險 Agent 現金姿態的修正循環（第一次加兩次帶回饋的重試，共三次）與簡報提醒。

沒有 offense 的 Policy 仍是兩次嘗試（行為不變）；提示與驗證回饋的新增句子只出現在 offense 啟用時。
"""

import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.risk_stages import (
    OFFENSE_FIELD_REMINDER, TURN_ASSESSMENT_PROMPT_AGENT_LED, TURN_ASSESSMENT_PROMPT_OFFENSE,
)
from test_daily_pipeline import FakeRunner
from test_offense_pipeline import template_offense, template_without_offense
from test_quality_dip_ruling_pipeline import Harness3, team_outputs

# keep_defensive 殘留了只有 probe／進攻才該填的欄位（改等級時沒清空）。
STALE = {"offense_basis": ["washout", "breadth_thrust"], "failure_condition": "三個交易日內再創新低即失敗", "defensive_reconciliation": "殘留的說明"}


def outputs(*stances):
    out = team_outputs("v4")
    base = out["cash_stance"][0]
    out["cash_stance"] = [dict(base, **stance) for stance in stances]
    return out


class OffenseStanceAttemptsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)

    def harness(self, name, template):
        (self.base / name).mkdir()
        return Harness3(self.base / name, template=template)

    def stance_calls(self, runner):
        return [prompt for name, prompt in runner.prompts if name == "cash_stance"]

    def test_third_attempt_is_accepted_when_the_first_two_leave_stale_fields(self):
        harness = self.harness("on", template_offense())
        runner = FakeRunner(outputs(STALE, STALE, {}))
        result, _ = harness.run(runner)
        self.assertEqual(result.status, "completed", result.errors)
        calls = self.stance_calls(runner)
        self.assertEqual(len(calls), 3)
        self.assertIn(OFFENSE_FIELD_REMINDER, calls[0])  # 簡報提示本身就有中性提醒
        self.assertIn("提醒：改等級時請清空不適用的欄位", calls[1])  # 驗證回饋也有
        self.assertIn("提醒：改等級時請清空不適用的欄位", calls[2])
        self.assertEqual(harness.validate(harness.artifacts()), [])

    def test_a_third_failure_is_still_fail_closed(self):
        harness = self.harness("on", template_offense())
        runner = FakeRunner(outputs(STALE, STALE, STALE))
        result, _ = harness.run(runner)
        self.assertNotEqual(result.status, "completed")
        self.assertEqual(len(self.stance_calls(runner)), 3)

    def test_policy_without_offense_keeps_two_attempts_and_the_old_prompt(self):
        harness = self.harness("off", template_without_offense())
        runner = FakeRunner(outputs({"level": "bogus"}, {"level": "bogus"}, {}))
        result, _ = harness.run(runner)
        self.assertNotEqual(result.status, "completed")
        calls = self.stance_calls(runner)
        self.assertEqual(len(calls), 2)  # 第三次不會被呼叫
        for prompt in calls:
            self.assertNotIn(OFFENSE_FIELD_REMINDER, prompt)
            self.assertNotIn("提醒：改等級時請清空", prompt)

    def test_offense_prompt_describes_the_current_turn_assessment_effect_and_the_cash_return_cost(self):
        harness = self.harness("on", template_offense())
        runner = FakeRunner(outputs({}))
        result, _ = harness.run(runner)
        self.assertEqual(result.status, "completed", result.errors)
        prompt = self.stance_calls(runner)[0]
        self.assertIn(TURN_ASSESSMENT_PROMPT_OFFENSE, prompt)
        self.assertNotIn(TURN_ASSESSMENT_PROMPT_AGENT_LED, prompt)
        self.assertNotIn("Policy 有為 turn_unconfirmed", prompt)  # 舊句：現金放寬由 turn_assessment 決定，offense 下不成立
        self.assertIn("回到 keep_defensive", prompt)
        self.assertIn("賣股補回現金", prompt)
        self.assertIn("錯過", prompt)  # 兩種錯誤成本並列
        self.assertIn("被套", prompt)


if __name__ == "__main__":
    unittest.main()
