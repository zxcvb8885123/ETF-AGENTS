"""restricted_hold：D-Plan 匯出的單股上限與 Guard 同一口徑，只放行股數不變、不可交易的持股。"""

import unittest
from decimal import Decimal

from etf_agent.competition import DPlanError
from etf_agent.competition.dplan import DPlanExporter
from etf_agent.decision.risk import restricted_carried_symbols

NAV = Decimal("1000000")
PRICES = {"1000.TW": Decimal("100"), "2000.TW": Decimal("100")}
# 1000.TW 漲價後 10.2%，2000.TW 5%；兩檔都不下單。
TARGET = {"1000.TW": 1020, "2000.TW": 500}


def snapshot(blocked=(), allowed=("1000.TW", "2000.TW")):
    return {"tradable_symbols": [s for s in allowed if s not in blocked], "not_tradable_symbols": list(blocked)}


class RestrictedHoldExportTests(unittest.TestCase):
    def setUp(self):
        self.plan = {"market_view": {"posture": {"net_exposure_intent": "hold", "target_cash_pct_range": [0.10, 0.20]}}}
        self.account = {"nav": str(NAV), "cash": "150000"}

    def validate(self, carried, target=TARGET):
        DPlanExporter._validate_official_posture(
            self.plan, self.account, {"rules": {}}, [], PRICES, target, NAV, carried)

    def test_blocked_unchanged_holding_is_carried(self):
        carried = restricted_carried_symbols({}, snapshot(blocked=("1000.TW",)), TARGET, TARGET)
        self.assertEqual(carried, {"1000.TW"})
        self.validate(carried)  # 不拋錯

    def test_gate_off_still_rejects(self):
        with self.assertRaisesRegex(DPlanError, "單一持股上限"):
            self.validate(set())

    def test_restricted_with_share_change_is_not_carried(self):
        held = {"1000.TW": 900, "2000.TW": 500}
        carried = restricted_carried_symbols({}, snapshot(blocked=("1000.TW",)), held, TARGET)
        self.assertEqual(carried, set())
        with self.assertRaisesRegex(DPlanError, "單一持股上限"):
            self.validate(carried)

    def test_tradable_overweight_is_not_carried(self):
        carried = restricted_carried_symbols({}, snapshot(), TARGET, TARGET)
        self.assertEqual(carried, set())
        with self.assertRaisesRegex(DPlanError, "單一持股上限"):
            self.validate(carried)


if __name__ == "__main__":
    unittest.main()
