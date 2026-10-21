import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.core.run_layout import day_from_run_id, find_run_dir, list_run_dirs, taipei_day


class RunLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_day_from_run_id_uses_taipei_date(self):
        self.assertEqual(day_from_run_id("decision-20261019T180000Z"), "2026-10-20")
        self.assertEqual(day_from_run_id("decision-20261019T020000Z"), "2026-10-19")
        self.assertEqual(day_from_run_id("daily-20261021-163158"), "2026-10-21")
        self.assertIsNone(day_from_run_id("delivery-0ae0ebf8406b52f78150a24b10880613"))

    def test_taipei_day_rejects_naive_time(self):
        with self.assertRaises(ValueError):
            taipei_day(datetime(2026, 10, 1))
        self.assertEqual(taipei_day(datetime(2026, 10, 1, 17, tzinfo=timezone.utc)), "2026-10-02")

    def test_find_run_dir_requires_exactly_one_match(self):
        self.assertIsNone(find_run_dir(self.root, "run-a"))
        (self.root / "2026-10-01" / "run-a").mkdir(parents=True)
        self.assertEqual(find_run_dir(self.root, "run-a"), self.root / "2026-10-01" / "run-a")
        (self.root / "2026-10-02" / "run-a").mkdir(parents=True)
        with self.assertRaises(RuntimeError):
            find_run_dir(self.root, "run-a", RuntimeError)

    def test_list_run_dirs_skips_hidden_and_non_date_folders(self):
        (self.root / "2026-10-01" / "run-a").mkdir(parents=True)
        (self.root / "2026-10-01" / ".tmp-run").mkdir()
        (self.root / ".superseded" / "run-b").mkdir(parents=True)
        (self.root / "2026-10-01" / "note.txt").write_text("x", encoding="utf-8")
        self.assertEqual(list_run_dirs(self.root), [self.root / "2026-10-01" / "run-a"])


class DatedStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ImmutableRunStore(self.root, schema_version="test-1", dated=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_save_places_run_in_date_folder_and_verifies(self):
        saved = self.store.save("decision-20261021T095000Z", {"a": {"x": 1}})
        self.assertEqual(saved, self.root / "2026-10-21" / "decision-20261021T095000Z")
        self.assertEqual(self.store.verify("decision-20261021T095000Z"), saved)
        self.assertEqual(self.store.save("decision-20261021T095000Z", {"a": {"x": 1}}), saved)

    def test_explicit_day_for_run_id_without_date(self):
        saved = self.store.save("delivery-abc", {"a": {"x": 1}}, day="2026-10-05")
        self.assertEqual(saved.parent.name, "2026-10-05")
        self.assertEqual(self.store.run_dir("delivery-abc"), saved)

    def test_conflicting_content_is_rejected_across_date_lookup(self):
        self.store.save("run-1", {"a": {"x": 1}}, day="2026-10-05")
        with self.assertRaises(ValueError):
            self.store.save("run-1", {"a": {"x": 2}}, day="2026-10-06")

    def test_same_run_id_in_two_dates_is_rejected(self):
        self.store.save("run-1", {"a": {"x": 1}}, day="2026-10-05")
        (self.root / "2026-10-06" / "run-1").mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.store.verify("run-1")

    def test_bad_day_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save("run-1", {"a": {"x": 1}}, day="../escape")


if __name__ == "__main__":
    unittest.main()
