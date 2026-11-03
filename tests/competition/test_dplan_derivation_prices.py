import unittest
from decimal import Decimal

from etf_agent.competition.dplan import (
    DATAPACK_PRICE_MAX_RELATIVE_DIFF, DPlanError, derivation_nav, derivation_prices, official_target_shares,
    official_target_weight,
)


def rows():
    return [
        {"symbol": "2330.TW", "execution_close_price": "1000", "analysis_close_price": "990"},
        {"symbol": "2317.TW", "execution_close_price": "100.5", "analysis_close_price": "100"},
        {"symbol": "2454.TW", "execution_close_price": None, "analysis_close_price": "1200"},
        {"symbol": "1101.TW", "execution_close_price": None, "analysis_close_price": "28"},
    ]


class DerivationPriceTests(unittest.TestCase):
    """D-Plan 換算價：持股有 DataPack 收盤價時用它，差異只記警告；沒有 DataPack 價的標的仍要 Snapshot 官方價。"""

    def test_datapack_price_wins_for_holdings_and_differences_are_warnings(self):
        prices, warnings = derivation_prices(rows(), {"2330.TW", "2317.TW"}, {"2330.TW": "1005", "2317.TW": "100.5"})
        self.assertEqual(prices, {"2330.TW": Decimal("1005"), "2317.TW": Decimal("100.5")})
        self.assertEqual(len(warnings), 1)
        self.assertIn("2330", warnings[0])
        self.assertIn("DataPack", warnings[0])
        # 與 derivation_nav 一致：換算價已等於 DataPack 價，不再因「不一致」拒絕。
        self.assertEqual(derivation_nav("999", prices, "1000", {"2330.TW": "1005", "2317.TW": "100.5"}), Decimal("1000"))

    def test_missing_snapshot_official_close_is_covered_by_datapack_price(self):
        prices, warnings = derivation_prices(rows(), {"2454.TW"}, {"2454.TW": "1210"})
        self.assertEqual(prices["2454.TW"], Decimal("1210"))
        self.assertEqual(len(warnings), 1)
        self.assertIn("缺 Snapshot 同日官方收盤價", warnings[0])

    def test_symbol_without_datapack_price_still_needs_snapshot_official_close(self):
        # 新買進的 2454 不在 DataPack 持股：Snapshot 只有調整價時照舊拒絕，不拿調整價換算。
        with self.assertRaisesRegex(DPlanError, "2454"):
            derivation_prices(rows(), {"2330.TW", "2454.TW"}, {"2330.TW": "1000"})
        with self.assertRaisesRegex(DPlanError, "1101"):
            derivation_prices(rows(), {"1101.TW"}, None)
        prices, warnings = derivation_prices(rows(), {"2330.TW"}, None)
        self.assertEqual((prices, warnings), ({"2330.TW": Decimal("1000")}, []))

    def test_large_difference_is_not_a_tolerated_discrepancy(self):
        limit = DATAPACK_PRICE_MAX_RELATIVE_DIFF
        too_far = (Decimal("1000") / (1 + limit) - Decimal("1")).quantize(Decimal("0.01"))
        with self.assertRaisesRegex(DPlanError, "超過口徑差異範圍"):
            derivation_prices(rows(), {"2330.TW"}, {"2330.TW": str(too_far)})
        for bad in ("0", "-1", "NaN", True):
            with self.assertRaises(DPlanError):
                derivation_prices(rows(), {"2330.TW"}, {"2330.TW": bad})

    def test_switching_price_source_keeps_proposed_equal_to_derived(self):
        # 匯出器用同一價格互推 target_weight 與股數，所以不論採 Snapshot 價或 DataPack 價，推導股數都等於已核准股數；
        # 差別只在主辦方用「他們的價」重算時是否仍得到同一張數，而 DataPack 價就是主辦方的價。
        nav = Decimal("992061465.58")
        checked = 0
        for shares in (1000, 29000, 458000, 1020000):
            for price in (Decimal("27.7"), Decimal("28.1"), Decimal("1005"), Decimal("72.4")):
                if Decimal(shares) * price / nav > Decimal("0.25"):
                    continue  # 超過單股上限的組合不會出現在已核准決策中
                weight = official_target_weight(shares, price, nav)
                self.assertEqual(official_target_shares(weight, price, nav), shares)
                checked += 1
        self.assertGreaterEqual(checked, 10)


if __name__ == "__main__":
    unittest.main()
