"""CLI 應用服務的各驗證路徑共用 TradeDecisionValidator 建構：由 policy 推導 dip_assessment 等旗標（比照 Finalizer）。"""
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.decision.service import PortfolioDecisionApplicationService

from test_analysts import bundle_with_event

POLICY_WITH_DIP = {"position_sizing": {"quality_dip_facts": {"version": "v1"}, "quality_dip_assessment": {"version": "v1"}}}


class ServiceTradeValidatorTests(unittest.TestCase):
    def setUp(self):
        self.service = PortfolioDecisionApplicationService(bundle_with_event())

    def test_dip_assessment_is_derived_from_the_policy(self):
        validator = self.service._trade_validator({}, {}, POLICY_WITH_DIP)
        self.assertTrue(validator.dip_assessment)
        self.assertFalse(self.service._trade_validator({}, {}, {"position_sizing": {}}).dip_assessment)
        self.assertFalse(self.service._trade_validator({}, {}, None).dip_assessment)

    def test_team_inputs_beside_the_policy_supply_dip_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text("{}", encoding="utf-8")
            (Path(directory) / "team_inputs.json").write_text(json.dumps({"dip_facts": {"marker": 1}}), encoding="utf-8")
            validator = self.service._trade_validator({}, {}, POLICY_WITH_DIP, policy_path)
        self.assertEqual(validator.dip_facts, {"marker": 1})


if __name__ == "__main__":
    unittest.main()
