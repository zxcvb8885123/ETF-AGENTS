"""簡報說真話（``position_sizing.exposure_view`` v2，agent-led offense 階段 1）的測試。

- v1 與沒有閘門：風險 Agent 簡報的曝險說明與提示文字逐位元沿用舊常數。
- v2：改用與現行行為一致的文字（exposure_view 決定曝險配對、turn_assessment 影響現金目標與 quality_dip），
  並列追高與錯過兩種錯誤成本，不再寫「只是紀錄」「不要為了追上大盤而升級」。
- v2 需要 agent_swap v2 與 agent_swap.exposure v2；只有版本欄位，Schema 與 team_inputs 欄位不變。
"""

import unittest

from etf_agent.automation import risk_stages
from etf_agent.decision.exposure_facts import (
    RISK_NOTE,
    RISK_NOTE_AGENT_LED,
    exposure_view_agent_led,
    risk_exposure_brief,
    validate_exposure_view_gate,
)

FACTS = {"as_of": "2026-11-10", "portfolio": {}, "sectors": {}, "up_day": {}, "data_gaps": []}


def sizing(version, swap_version="v2", exposure_version="v2"):
    return {
        "exposure_view": {"version": version},
        "agent_swap": {"version": swap_version, "exposure": {"version": exposure_version}},
    }


class BriefingTruthTests(unittest.TestCase):
    def test_without_v2_the_old_texts_are_used_bit_for_bit(self):
        for value in (None, {}, {"exposure_view": {"version": "v1"}}):
            self.assertFalse(exposure_view_agent_led(value))
        self.assertEqual(risk_exposure_brief(FACTS)["note"], RISK_NOTE)
        self.assertEqual(risk_exposure_brief(FACTS, agent_led=False)["note"], RISK_NOTE)
        self.assertIn("只是紀錄", risk_stages.EXPOSURE_VIEW_PROMPT)
        self.assertIn("不要為了追上大盤而升級", risk_stages.TURN_STATE_PROMPT_V2)

    def test_v2_uses_the_truthful_texts_with_both_error_costs(self):
        self.assertTrue(exposure_view_agent_led(sizing("v2")))
        self.assertEqual(risk_exposure_brief(FACTS, agent_led=True)["note"], RISK_NOTE_AGENT_LED)
        for text in (risk_stages.EXPOSURE_VIEW_PROMPT_AGENT_LED, risk_stages.TURN_ASSESSMENT_PROMPT_AGENT_LED):
            self.assertIn("追高", text + RISK_NOTE_AGENT_LED)
            self.assertIn("錯過", text)
            self.assertNotIn("只是紀錄你認為組合", text)
            self.assertNotIn("不要為了追上大盤而升級", text)
        self.assertIn("一對都不成交", risk_stages.EXPOSURE_VIEW_PROMPT_AGENT_LED)
        self.assertIn("defensive_cash_buffer", risk_stages.TURN_ASSESSMENT_PROMPT_AGENT_LED)
        self.assertNotIn("不改現金姿態、現金比例、持股廣度或換股", risk_stages.EXPOSURE_VIEW_PROMPT_AGENT_LED)

    def test_v2_needs_agent_swap_v2_and_exposure_v2(self):
        errors = []
        validate_exposure_view_gate(sizing("v2"), errors)
        self.assertEqual(errors, [])
        for bad in (sizing("v2", swap_version="v1"), sizing("v2", exposure_version="v1"), {"exposure_view": {"version": "v2"}}):
            errors = []
            validate_exposure_view_gate(bad, errors)
            self.assertTrue(any("需要 agent_swap v2" in e for e in errors), bad)
        errors = []
        validate_exposure_view_gate({"exposure_view": {"version": "v3"}}, errors)
        self.assertTrue(errors)
        errors = []
        validate_exposure_view_gate(sizing("v1", swap_version="v1", exposure_version="v1"), errors)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
