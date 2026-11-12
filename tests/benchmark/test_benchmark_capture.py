"""帳戶對基準的每日上漲／下跌捕捉：手算驗證、單側無樣本、small_sample、日期對不上與缺日略過。"""

import unittest
from decimal import Decimal

from etf_agent.benchmark import MIN_CAPTURE_DAYS, build_daily_capture

CAPITAL = Decimal("100")


def _series(navs, start=0):
    """基準序列：第 0 點為開帳點（日期 d0），之後 d1、d2…；nav 為 None 代表缺估值。"""
    return [{"trade_date": "d%d" % (start + index), "nav": None if nav is None else str(nav)} for index, nav in enumerate(navs)]


def _account(navs, start=0):
    return {"d%d" % (start + index): Decimal(str(nav)) for index, nav in enumerate(navs) if nav is not None}


class CaptureTests(unittest.TestCase):
    def test_hand_computed_up_and_down_capture(self):
        # 基準 +10%、−10%、+10%、−10%；帳戶 +12%、−10%、+10.5%、−10%。
        benchmark = _series([100, 110, 99, "108.9", "98.01"], start=0)
        account = _account([100, 112, "100.8", "111.384", "100.2456"], start=0)
        result = build_daily_capture(account, benchmark, CAPITAL)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([row["benchmark_direction"] for row in result["rows"]], ["up", "down", "up", "down"])
        self.assertEqual(result["rows"][0]["account_return"], "0.120000")
        self.assertEqual(result["rows"][0]["benchmark_return"], "0.100000")
        self.assertEqual(result["rows"][0]["difference"], "0.020000")
        up, down = result["up"], result["down"]
        self.assertEqual((up["days"], up["account_mean_return"], up["benchmark_mean_return"], up["capture"]), (2, "0.112500", "0.100000", "1.125000"))
        self.assertEqual((down["days"], down["capture"]), (2, "1.000000"))
        self.assertTrue(up["small_sample"] and down["small_sample"])
        self.assertEqual(result["flat_days"], 0)
        # 累積超額：最後一日帳戶 100.2456、基準 98.01，本金 100。
        self.assertEqual(result["cumulative"]["cumulative_excess"], "0.022356")
        self.assertEqual(result["cumulative"]["trade_date"], "d4")

    def test_no_down_day_is_unavailable_not_filled(self):
        result = build_daily_capture(_account([100, 105, 110], 0), _series([100, 102, 104], 0), CAPITAL)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["down"]["status"], "unavailable")
        self.assertIsNone(result["down"]["capture"])
        self.assertEqual(result["down"]["days"], 0)
        self.assertEqual(result["up"]["status"], "ok")

    def test_no_up_day_is_unavailable(self):
        result = build_daily_capture(_account([100, 99, 98], 0), _series([100, 99, 98], 0), CAPITAL)
        self.assertEqual(result["up"]["status"], "unavailable")
        self.assertEqual(result["down"]["capture"], "1.000000")

    def test_small_sample_flag_clears_at_threshold(self):
        navs = [100 * Decimal("1.01") ** index for index in range(MIN_CAPTURE_DAYS + 1)]
        result = build_daily_capture(dict(_account(navs, 0)), _series(navs, 0), CAPITAL)
        self.assertEqual(result["up"]["days"], MIN_CAPTURE_DAYS)
        self.assertFalse(result["up"]["small_sample"])
        short = build_daily_capture(_account(navs[:MIN_CAPTURE_DAYS], 0), _series(navs[:MIN_CAPTURE_DAYS], 0), CAPITAL)
        self.assertTrue(short["up"]["small_sample"])

    def test_flat_benchmark_day_is_excluded_from_both_sides(self):
        result = build_daily_capture(_account([100, 101, 102], 0), _series([100, 100, 101], 0), CAPITAL)
        self.assertEqual(result["flat_days"], 1)
        self.assertEqual(result["up"]["days"], 1)
        self.assertEqual(result["down"]["days"], 0)
        self.assertEqual(result["rows"][0]["benchmark_direction"], "flat")

    def test_account_missing_day_skips_that_day_and_next_without_bridging(self):
        # 帳本缺 d2：d2 與 d3（前一日缺）都略過，d1、d4 照算；不把 d1→d3 當成單日報酬。
        benchmark = _series([100, 110, 121, 133.1, 146.41], 0)
        account = _account([100, 110, None, 133, 146], 0)
        result = build_daily_capture(account, benchmark, CAPITAL)
        self.assertEqual([row["trade_date"] for row in result["rows"]], ["d1", "d4"])
        self.assertEqual([item["trade_date"] for item in result["skipped"]], ["d2", "d3"])
        self.assertIn("帳本沒有 d2", result["skipped"][0]["reason"])

    def test_benchmark_unvalued_day_is_skipped(self):
        result = build_daily_capture(_account([100, 101, 102, 103], 0), _series([100, 101, None, 103], 0), CAPITAL)
        self.assertEqual([row["trade_date"] for row in result["rows"]], ["d1"])
        self.assertEqual(len(result["skipped"]), 2)

    def test_mismatched_dates_yield_unavailable(self):
        account = {"x1": Decimal(100), "x2": Decimal(101)}
        result = build_daily_capture(account, _series([100, 101], 0), CAPITAL)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["rows"], [])
        self.assertTrue(result["skipped"])

    def test_values_are_finite_decimal_strings(self):
        result = build_daily_capture(_account([100, 112, "100.8"], 0), _series([100, 110, 99], 0), CAPITAL)
        for row in result["rows"]:
            for key in ("account_return", "benchmark_return", "difference"):
                self.assertTrue(Decimal(row[key]).is_finite())
        self.assertTrue(Decimal(result["up"]["capture"]).is_finite())


if __name__ == "__main__":
    unittest.main()
