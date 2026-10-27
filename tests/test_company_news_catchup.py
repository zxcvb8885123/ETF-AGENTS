import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from etf_agent.data import MarketDataDatabase
from etf_agent.data.supplemental_sources import SupplementalRepository

TAIPEI = timezone(timedelta(hours=8))


class CountCollectedDaysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repository = SupplementalRepository(MarketDataDatabase(Path(self.tmp.name) / "t.db"))

    def save(self, fetched_at, source="FINMIND_NEWS"):
        self.repository.save(source, "https://example.test", "{}", fetched_at, "finmind_news_candidates", [])

    def test_counts_runs_by_taipei_day_including_empty_ones(self):
        self.save("2026-10-27T12:40:00+00:00")
        self.save("2026-10-27T13:10:00+00:00")
        self.assertEqual(self.repository.count_collected_days("FINMIND_NEWS", date(2026, 10, 27), TAIPEI), 2)

    def test_taipei_day_boundary_and_other_days_excluded(self):
        self.save("2026-10-26T16:30:00+00:00")  # 台北 10/27 00:30
        self.save("2026-10-26T15:30:00+00:00")  # 台北 10/26 23:30
        self.assertEqual(self.repository.count_collected_days("FINMIND_NEWS", date(2026, 10, 27), TAIPEI), 1)

    def test_other_sources_not_counted(self):
        self.save("2026-10-27T12:40:00+00:00", source="OTHER")
        self.assertEqual(self.repository.count_collected_days("FINMIND_NEWS", date(2026, 10, 27), TAIPEI), 0)


if __name__ == "__main__":
    unittest.main()
