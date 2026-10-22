import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from etf_agent.data.finmind_schedule import FinMindScheduleError, in_filing_window, load_state, plan, record_done


class FinMindScheduleTests(unittest.TestCase):
    DATASET = "TaiwanStockFinancialStatements"

    def test_filing_windows_cover_deadlines_and_exclude_quiet_months(self):
        for day in (date(2026, 3, 31), date(2026, 5, 15), date(2026, 8, 14), date(2026, 11, 14), date(2026, 10, 22)):
            self.assertTrue(in_filing_window(day), day)
        for day in (date(2026, 1, 10), date(2026, 6, 15), date(2026, 9, 15), date(2026, 12, 20)):
            self.assertFalse(in_filing_window(day), day)

    def test_first_run_backfills_requested_years_even_outside_window(self):
        result = plan({}, self.DATASET, date(2026, 6, 15), years=3)
        self.assertEqual((result["action"], result["start"]), ("backfill", "2023-01-01"))

    def test_after_backfill_only_updates_inside_window_once_a_day(self):
        state = {self.DATASET: {"backfilled_from": "2023-01-01", "last_done": "2026-10-21"}}
        self.assertEqual(plan(state, self.DATASET, date(2026, 10, 22))["action"], "update")
        self.assertEqual(plan(state, self.DATASET, date(2026, 10, 22))["start"], "2025-01-01")
        self.assertEqual(plan(state, self.DATASET, date(2026, 12, 1))["action"], "skip")
        state[self.DATASET]["last_done"] = "2026-10-22"
        self.assertEqual(plan(state, self.DATASET, date(2026, 10, 22))["action"], "skip")
        self.assertEqual(plan(state, self.DATASET, date(2026, 12, 1), force=True)["action"], "update")

    def test_more_years_triggers_a_new_backfill(self):
        state = {self.DATASET: {"backfilled_from": "2023-01-01", "last_done": "2026-10-21"}}
        result = plan(state, self.DATASET, date(2026, 12, 1), years=5)
        self.assertEqual((result["action"], result["start"]), ("backfill", "2021-01-01"))

    def test_years_out_of_range_are_rejected(self):
        for years in (1, 6):
            with self.assertRaises(FinMindScheduleError):
                plan({}, self.DATASET, date(2026, 10, 22), years=years)

    def test_record_done_keeps_earliest_start_and_rejects_corrupt_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "state.json"
            record_done(path, self.DATASET, date(2023, 1, 1), date(2026, 10, 21))
            record_done(path, self.DATASET, date(2025, 1, 1), date(2026, 10, 22))
            entry = load_state(path)[self.DATASET]
            self.assertEqual(entry, {"backfilled_from": "2023-01-01", "last_done": "2026-10-22"})
            path.write_text("[1]", encoding="utf-8")
            with self.assertRaises(FinMindScheduleError):
                load_state(path)
            path.write_text("{壞", encoding="utf-8")
            with self.assertRaises(FinMindScheduleError):
                load_state(path)
