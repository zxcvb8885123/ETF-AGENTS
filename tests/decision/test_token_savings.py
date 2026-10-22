import json
import unittest

from etf_agent.automation.daily_pipeline import compact_brief_json
from etf_agent.decision.analysts import no_event_item


class CompactBriefJsonTest(unittest.TestCase):
    def test_round_trips_and_puts_each_symbol_on_one_line(self):
        brief = {"role": "bull", "rules": {"max": "0.1"}, "symbols": [{"symbol": "2330.TW", "note": "台積電"}, {"symbol": "2317.TW"}]}
        text = compact_brief_json(brief)
        self.assertEqual(json.loads(text), brief)
        lines = text.splitlines()
        self.assertEqual(sum('"symbol"' in line for line in lines), 2)
        self.assertNotIn("  ", text)

    def test_empty_and_scalar_values_stay_valid(self):
        brief = {"symbols": [], "batch": None, "tags": ["a", "b"]}
        self.assertEqual(json.loads(compact_brief_json(brief)), brief)


class NoEventItemTest(unittest.TestCase):
    def test_item_is_unknown_without_findings_and_lists_availability(self):
        item = no_event_item("6789.TW", [{"name": "新聞候選", "status": "待確認"}, {"status": "未查"}])
        self.assertEqual(item["outlook"], "unknown")
        self.assertEqual(item["event_outlook"], "unknown")
        self.assertEqual(item["findings"], [])
        self.assertEqual(item["events"], [])
        self.assertEqual(item["data_gaps"][1:], ["新聞候選：待確認", "未查"])

    def test_malformed_availability_is_ignored(self):
        item = no_event_item("6789.TW", [{"name": 1, "status": None}])
        self.assertEqual(len(item["data_gaps"]), 1)


if __name__ == "__main__":
    unittest.main()
