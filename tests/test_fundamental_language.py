import unittest

from etf_agent.decision.fundamental_language import chinese_gap_explanations, explain_metric_gap, render_fundamental_report


class FundamentalLanguageTests(unittest.TestCase):
    def metric(self, key="revenue_yoy_pct", reason="COMPARABLE_FACT_NOT_REPORTED"):
        return {"metric_key": key, "status": "unavailable", "period": "2026Q2", "reason_code": reason}

    def document(self):
        return {"symbol": "2330.TW", "financial_statement": {
            "statement_type": "income_statement", "fiscal_year": 2026, "fiscal_quarter": 2,
            "period_kind": "cumulative_to_quarter", "facts": {
                "revenue": {"value_status": "provided"}, "operating_profit": {"value_status": "provided"}}}}

    def test_missing_prior_statement_is_named(self):
        texts = explain_metric_gap(self.metric(), [self.document()], "2330.TW")
        self.assertEqual(texts, ["目前分析摘要未提供2025年上半年損益表。原始資料是否已取得，需由資料端確認。"])

    def test_existing_statement_missing_fields_and_missing_prior_are_distinct(self):
        doc = self.document(); doc["financial_statement"]["facts"] = {}
        texts = chinese_gap_explanations([self.metric(), self.metric("operating_margin_pp_yoy")], [doc], "2330.TW")
        self.assertEqual(texts, ["目前分析摘要的2026年上半年損益表未提供營業收入、營業利益欄位。原始欄位是否存在，需由資料端確認。",
                                 "目前分析摘要未提供2025年上半年損益表。原始資料是否已取得，需由資料端確認。"])

    def test_raw_financial_net_income_is_unmapped_not_unavailable(self):
        import json
        doc = self.document()
        doc["financial_statement"]["industry"] = "fh"
        doc["financial_statement"]["facts"] = {}
        doc["body"] = json.dumps({"本期稅後淨利（淨損）": "97780594.00"})
        texts = explain_metric_gap(self.metric("net_income_yoy_pct"), [doc], "2330.TW")
        self.assertIn("原始損益表已有", texts[0])
        self.assertIn("尚未接入", texts[0])
        self.assertNotIn("抓不到", " ".join(texts))
        doc["body"] = json.dumps({"本期稅後淨利（淨損）": "NaN"})
        self.assertNotIn("原始損益表已有", " ".join(explain_metric_gap(self.metric("net_income_yoy_pct"), [doc], "2330.TW")))

    def test_financial_industry_needs_mapping_instead_of_generic_revenue(self):
        doc = self.document()
        doc["financial_statement"]["industry"] = "fh"
        doc["financial_statement"]["facts"] = {}
        texts = explain_metric_gap(self.metric("operating_margin_pct"), [doc], "2330.TW")
        self.assertEqual(texts, ["2026年上半年為金融業損益表。金融業專用欄位及比較方法尚未接入。"])

    def test_mapped_net_revenue_is_not_described_as_unmapped(self):
        doc = self.document()
        doc["financial_statement"]["industry"] = "fh"
        doc["financial_statement"]["facts"] = {"net_revenue": {"value_status": "provided"}}
        texts = explain_metric_gap(self.metric("operating_margin_pct"), [doc], "2330.TW")
        self.assertEqual(texts, ["2026年上半年的金融業淨收益已接入。不能直接套用一般業的營業收入及營業利益率公式。"])

    def test_nonpositive_base_is_not_described_as_missing_report(self):
        texts = explain_metric_gap(self.metric("net_income_yoy_pct", "NONPOSITIVE_COMPARISON_BASE"), [], "2330.TW")
        self.assertEqual(texts, ["2025年上半年的稅後淨利為零或負數。不能計算一般年增率。"])
        self.assertNotIn("缺少", texts[0])

    def test_unknown_keeps_no_assessment_status_and_chinese_display(self):
        item = {"symbol": "2330.TW", "outlook": "unknown", "findings": [], "data_gaps": ["缺少當期損益表。"]}
        text = render_fundamental_report({"items": [item]})
        self.assertIn("中性（資料不足，暫不判斷）", text)
        self.assertEqual(item["outlook"], "unknown")
        self.assertNotIn("unknown", text)

    def test_report_separates_reasons_cautions_and_short_gap_sentences(self):
        item = {"symbol": "2330.TW", "outlook": "positive", "findings": [
            {"text": "判斷：正面。營收與獲利均成長。"}, {"text": "理由：營收增加35.61%。"},
            {"text": "需注意：尚未確認成長來源。"}], "data_gaps": ["已有產業代碼。缺少同業比較資料。"]}
        text = render_fundamental_report({"items": [item]})
        self.assertIn("**理由：**", text)
        self.assertIn("**需注意：**", text)
        self.assertIn("- 已有產業代碼。\n- 缺少同業比較資料。", text)
        self.assertEqual(text.count("判斷：正面。"), 1)
