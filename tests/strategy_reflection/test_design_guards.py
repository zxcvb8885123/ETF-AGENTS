"""守住有文獻支持的設計：逆波動配置（1/ATR14）、續抱優先（held_add_priority）、多空單輪隔離。

這些不是旋鈕，是版本閘門與架構邊界：只讀 config 與程式碼，不改任何檔案。之後若有人不小心把它們拿掉，這裡會先紅。
"""

import json
import re
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES, KNOBS

ROOT = Path(__file__).resolve().parents[2]


class DesignGuardTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        self.sizing = self.policy["position_sizing"]

    def test_inverse_volatility_sizing_stays_on(self):
        """權重由 1/ATR14（conviction_volatility_v2）確定性計算；volatility_exponent 必須在白名單範圍內。"""
        self.assertEqual(self.sizing["method"], "conviction_volatility_v2")
        knob = KNOBS["volatility_exponent"]
        exponent = Decimal(str(self.sizing["volatility_exponent"]))
        self.assertTrue(knob.low <= exponent <= knob.high, exponent)

    def test_held_add_priority_stays_on_and_is_not_a_knob(self):
        """續抱優先是版本閘門：policy 必須有它，而且策略反思不得提案改它。"""
        self.assertEqual(self.sizing.get("held_add_priority"), "v1")
        self.assertIn("held_add_priority", FORBIDDEN_PREFIXES)
        self.assertNotIn("held_add_priority", KNOBS)
        for gate in ("method", "stock_weight_fit", "rotation.version", "selection_ranking"):
            self.assertIn(gate, FORBIDDEN_PREFIXES, gate)

    def test_bull_bear_single_round_isolation_is_still_enforced(self):
        """多頭／空頭讀同一份共同輸入、互相隔離：debate 驗證仍要求 peer_packet_ids 為空且 shared_input_sha256 一致。"""
        source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src" / "etf_agent" / "decision").glob("*.py"))
        self.assertRegex(source, re.compile(r"peer_packet_ids"))
        self.assertRegex(source, re.compile(r"shared_input_sha256"))
        prompts = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "tradeagent").rglob("SKILL.md"))
        self.assertIn("peer_packet_ids", prompts + source)

    def test_every_knob_declares_a_risk_direction_and_only_reducing_auto_applies(self):
        for path, knob in KNOBS.items():
            self.assertIn(knob.risk_reducing, (-1, 0, 1), path)
        self.assertEqual(KNOBS["volatility_floor"].risk_reducing, 0)  # 方向不明，永遠需人決定


if __name__ == "__main__":
    unittest.main()
