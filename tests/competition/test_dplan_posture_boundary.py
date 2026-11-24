"""net_exposure_intent 的 C12 邊界：context 預判口徑（含滑價 gross_amount、提案 NAV）與官方口徑（前日收盤價、官方 NAV）不同時，以官方口徑為準。"""

import unittest
from decimal import Decimal

from etf_agent.competition import DPlanError
from etf_agent.competition.dplan import DPlanExporter

NAV = Decimal("1000000000")
PRICES = {"1000.TW": Decimal("100"), "2000.TW": Decimal("100")}


def orders(sell_pct, buy_pct):
    return [
        {"ticker": "1000", "side": "SELL", "shares": int(NAV * Decimal(sell_pct) / 100 / PRICES["1000.TW"]), "decision_ref": "D1"},
        {"ticker": "2000", "side": "BUY", "shares": int(NAV * Decimal(buy_pct) / 100 / PRICES["2000.TW"]), "decision_ref": "D2"},
    ]


def view(intent):
    return {"basis_refs": ["O1"], "posture": {"net_exposure_intent": intent, "target_cash_pct_range": [0.10, 0.15]}}


class PostureBoundaryTests(unittest.TestCase):
    def validate(self, intent, rows):
        plan = {"market_view": view(intent)}
        DPlanExporter._validate_official_posture(
            plan, {"nav": str(NAV), "cash": "100000000"}, {"rules": {}}, rows, PRICES, {}, NAV)

    def test_hold_with_net_sell_just_over_two_percent_is_rejected_by_c12(self):
        rows = orders("30", "27.99")  # 淨賣出 2.01% NAV
        with self.assertRaisesRegex(DPlanError, "C12"):
            self.validate("hold", rows)

    def test_reconcile_replaces_a_hold_that_c12_would_reject(self):
        rows = orders("30", "27.99")
        fixed = DPlanExporter._reconcile_exposure_intent(view("hold"), rows, PRICES, NAV)
        self.assertEqual(fixed["posture"]["net_exposure_intent"], "reduce")
        self.validate("reduce", rows)  # 改後的口徑通過 C12
        self.assertEqual(view("hold")["posture"]["net_exposure_intent"], "hold")  # 不改輸入

    def test_reconcile_keeps_a_prediction_that_already_passes_c12(self):
        rows = orders("30", "28.01")  # 淨賣出 1.99%，hold 合法
        kept = DPlanExporter._reconcile_exposure_intent(view("hold"), rows, PRICES, NAV)
        self.assertEqual(kept["posture"]["net_exposure_intent"], "hold")
        self.validate("hold", rows)
        # increase 也符合（買大於賣）時不被改成別的值，舊 D-Plan 重建結果不變。
        buy_heavy = orders("10", "10.5")
        self.assertEqual(DPlanExporter._reconcile_exposure_intent(view("increase"), buy_heavy, PRICES, NAV)["posture"]["net_exposure_intent"], "increase")


if __name__ == "__main__":
    unittest.main()
