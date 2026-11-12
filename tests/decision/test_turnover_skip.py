"""agent_swap.entry_reserve.turnover_skip v1（版本閘門）：選配子欄位的形狀驗證與判斷函式。

沒有該欄位的舊 Policy 逐位元維持原行為（第一個超過換手上限的組之後全部取消）；欄位只有 version，沒有人填數字。
"""

import unittest

from etf_agent.decision.entry_reserve import turnover_skip_enabled

from test_entry_reserve import reserve_errors, reserve_sizing


class TurnoverSkipGateTests(unittest.TestCase):
    def test_absent_field_is_disabled_and_valid(self):
        sizing = reserve_sizing()
        self.assertFalse(turnover_skip_enabled(sizing))
        self.assertEqual(reserve_errors(sizing), [])

    def test_v1_enables_it(self):
        sizing = reserve_sizing()
        sizing["agent_swap"]["entry_reserve"]["turnover_skip"] = {"version": "v1"}
        self.assertTrue(turnover_skip_enabled(sizing))
        self.assertEqual(reserve_errors(sizing), [])

    def test_bad_shapes_are_rejected_and_never_enable(self):
        for label, value in {"版本值域外": {"version": "v2"}, "缺版本": {}, "多餘欄位": {"version": "v1", "x": 1},
                             "不是物件": "v1", "布林": True}.items():
            sizing = reserve_sizing()
            sizing["agent_swap"]["entry_reserve"]["turnover_skip"] = value
            self.assertTrue(reserve_errors(sizing), label)
            if label != "多餘欄位":
                self.assertFalse(turnover_skip_enabled(sizing), label)

    def test_needs_the_entry_reserve_gate(self):
        sizing = reserve_sizing()
        sizing["agent_swap"]["version"] = "v1"
        sizing["agent_swap"]["entry_reserve"]["turnover_skip"] = {"version": "v1"}
        self.assertFalse(turnover_skip_enabled(sizing))

    def test_live_template_enables_it(self):
        import json
        from pathlib import Path
        live = json.loads((Path(__file__).resolve().parents[2] / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        self.assertTrue(turnover_skip_enabled(live["position_sizing"]))


if __name__ == "__main__":
    unittest.main()
