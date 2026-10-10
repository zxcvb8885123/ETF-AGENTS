import copy
import unittest

from etf_agent.decision import DecisionToolError, render_trader_report
from test_trader import world, decision, items


class TraderReportTests(unittest.TestCase):
    def test_only_integrated_results_are_rendered(self):
        bundle, momentum, _, debate = world()
        trade = decision(bundle, debate, items())
        text = render_trader_report(bundle, momentum, debate, trade)
        self.assertIn("結論：買進", text)
        self.assertIn(trade["items"][0]["rationale"], text)
        self.assertNotIn("採納的論點", text)
        self.assertNotIn("多頭研究", text)
        for packet in debate["packets"]:
            for item in packet["items"]:
                for claim in item["claims"]:
                    self.assertNotIn(claim["text"], text)
                    self.assertNotIn(claim["claim_id"], text)

    def test_modified_decision_is_not_presented_as_valid(self):
        bundle, momentum, _, debate = world()
        trade = copy.deepcopy(decision(bundle, debate, items()))
        trade["items"][0]["rationale"] = "遭改寫的整合理由"
        with self.assertRaises(DecisionToolError):
            render_trader_report(bundle, momentum, debate, trade)

    def test_reading_copy_hides_ids_preserves_numbers_and_original_json(self):
        bundle, momentum, _, debate = world()
        rows = items()
        rows[1]["rationale"] = (
            "多頭支持價格回升（bull-2330-1）。bear-2330-1 指出仍有風險。"
            "支持強度為 weak。資料中的 12.34% 不代表未來報酬。"
            "目前選擇續抱，不加碼。"
        )
        trade = decision(bundle, debate, rows)
        before = copy.deepcopy(trade)
        text = render_trader_report(bundle, momentum, debate, trade)
        self.assertNotIn("bull-", text)
        self.assertNotIn("bear-", text)
        self.assertNotIn("weak", text)
        self.assertIn("風險分析指出仍有風險", text)
        self.assertIn("12.34%", text)
        self.assertIn("目前選擇續抱，不加碼", text)
        self.assertIn("\n\n資料中的", text)
        self.assertEqual(trade, before)


if __name__ == "__main__":
    unittest.main()
