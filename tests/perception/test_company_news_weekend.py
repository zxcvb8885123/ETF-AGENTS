import unittest

from etf_agent.perception.company_news.contracts import SCHEMA_VERSION, SCHEMA_VERSION_V1, CompanyNewsError
from etf_agent.perception.company_news.tools import build_pack, window_start
from etf_agent.perception.company_news.validator import validate_pack

# 2026-11-15 是週日；週日 20:40（台北）＝ 12:40 UTC。
SUNDAY_FETCH = "2026-11-15T12:40:00+00:00"
MONDAY_CUTOFF = "2026-11-15T23:46:00+00:00"
REVIEW = {"source": "FINMIND_NEWS", "license_status": "approved", "terms_url": "https://finmind.github.io/en/Disclaimer/",
          "usage_scope": "competition_research", "reviewed_at": "2026-10-25T00:00:00+00:00", "evidence": "人工測試"}


class WeekendWindowTests(unittest.TestCase):
    def test_sunday_batch_reaches_back_over_the_weekend_in_1_1_only(self):
        self.assertEqual(window_start(MONDAY_CUTOFF, SUNDAY_FETCH, SCHEMA_VERSION), "2026-11-13T00:40:00+00:00")
        self.assertEqual(window_start(MONDAY_CUTOFF, SUNDAY_FETCH, SCHEMA_VERSION_V1), "2026-11-15T00:40:00+00:00")
        self.assertEqual(window_start(MONDAY_CUTOFF, SUNDAY_FETCH), "2026-11-15T00:40:00+00:00")

    def test_weekday_batch_keeps_twelve_hours(self):
        monday_fetch = "2026-11-16T12:40:00+00:00"
        cutoff = "2026-11-16T13:00:00+00:00"
        self.assertEqual(window_start(cutoff, monday_fetch, SCHEMA_VERSION), "2026-11-16T00:40:00+00:00")

    def test_never_earlier_than_three_days_before_cutoff(self):
        self.assertEqual(window_start("2026-11-15T14:00:00+00:00", SUNDAY_FETCH, SCHEMA_VERSION), "2026-11-13T00:40:00+00:00")
        self.assertEqual(window_start("2026-11-15T13:00:00+00:00", "2026-11-15T12:50:00+00:00", SCHEMA_VERSION), "2026-11-13T00:50:00+00:00")

    def test_pack_version_decides_window_and_mismatch_is_rejected(self):
        symbols = ["2317.TW"]
        new = build_pack([], REVIEW, symbols, "s", MONDAY_CUTOFF, [SUNDAY_FETCH])
        self.assertEqual(new["schema_version"], SCHEMA_VERSION)
        self.assertEqual(validate_pack(new, "s", MONDAY_CUTOFF, symbols), new)
        old = build_pack([], REVIEW, symbols, "s", MONDAY_CUTOFF, [SUNDAY_FETCH], schema_version=SCHEMA_VERSION_V1)
        self.assertEqual(old["window_start"], "2026-11-15T00:40:00+00:00")
        self.assertEqual(validate_pack(old, "s", MONDAY_CUTOFF, symbols), old)
        # 把 1.0 的視窗套在 1.1 版號上必須被拒絕。
        from etf_agent.perception.company_news.tools import seal
        forged = seal(dict(old, schema_version=SCHEMA_VERSION))
        with self.assertRaises(CompanyNewsError):
            validate_pack(forged, "s", MONDAY_CUTOFF, symbols)


if __name__ == "__main__":
    unittest.main()
