import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from etf_agent.data.analyst_availability import analyst_availability
from etf_agent.decision.event_financial_context import event_financial_context
from etf_agent.decision.contracts import DecisionToolError
from test_fundamental_research_agent import snapshot_fixture


class EventDataAvailabilityTests(unittest.TestCase):
    def test_existing_candidate_after_cutoff_is_not_called_missing_or_promoted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data.db"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE supplemental_facts(id INTEGER, stock_id TEXT, dataset TEXT, available_at TEXT, content_sha256 TEXT, raw_payload_id INTEGER)")
                connection.execute("INSERT INTO supplemental_facts VALUES(1,'2330','TaiwanStockCashFlowsStatement','2026-09-30T12:00:00+08:00','hash',3)")
            old = analyst_availability(path, ["2330.TW"], "2026-09-29T19:00:00+08:00")
            channel = old["symbols"][0]["channels"][0]
            self.assertEqual(channel["stored_rows"], 1)
            self.assertEqual(channel["available_by_cutoff_rows"], 0)
            self.assertEqual(channel["after_cutoff_rows"], 1)
            self.assertIn("已取得", channel["status"])
            current = analyst_availability(path, ["2330.TW"], "2026-10-03T19:00:00+08:00")
            self.assertEqual(current["symbols"][0]["channels"][0]["available_by_cutoff_rows"], 1)
            self.assertIn("沒有對應候選表", current["symbols"][0]["channels"][1]["status"])

    def test_missing_database_naive_clock_and_bad_symbol_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "missing.db"
            with self.assertRaises(ValueError): analyst_availability(path, ["2330.TW"], "2026-09-29T19:00:00+08:00")
            with sqlite3.connect(path): pass
            with self.assertRaises(ValueError): analyst_availability(path, ["2330.TW"], "2026-09-29T19:00:00")
            with self.assertRaises(ValueError): analyst_availability(path, ["foreign"], "2026-09-29T19:00:00+08:00")

    def test_snapshot_financial_facts_preserve_period_unit_and_evidence(self):
        snapshot = snapshot_fixture()
        context = event_financial_context({"snapshot": snapshot})["2330.TW"]
        income = next(row for row in context if row["statement_type"] == "income_statement")
        self.assertEqual(income["fiscal_year"], 2026)
        self.assertEqual(income["evidence_id"], "financial-evidence-11")
        self.assertEqual(income["facts"][0]["unit_multiplier"], 1000)
        self.assertEqual(income["period_kind"], "cumulative_to_quarter")

    def test_future_financial_and_nonfinite_value_rejected(self):
        snapshot = snapshot_fixture()
        for mode in ("future", "nonfinite"):
            broken = copy.deepcopy(snapshot)
            doc = broken["documents"][0]
            if mode == "future": doc["available_at"] = "2026-10-03T10:00:00+08:00"
            else: doc["financial_statement"]["facts"]["revenue"]["value"] = "Infinity"
            with self.assertRaises(DecisionToolError): event_financial_context({"snapshot": broken})
