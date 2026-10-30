import copy
import unittest
from dataclasses import replace
from decimal import Decimal

from etf_agent.data.corporate import CorporateRecord, FinancialStatement, FinancialStatementFact, SourceDocument
from etf_agent.data.fundamental_repair import (
    BD_MAPPING_VERSION,
    FIELDS,
    FINANCIAL_MAPPING_VERSION,
    MAPPING_VERSION,
    MIM_MAPPING_VERSION,
    FundamentalRepairProvider,
    OfficialCompanyProfileProvider,
    verified_prior_record,
)
import json


class FundamentalRepairTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = "2026-10-03T15:00:00+08:00"
        document = SourceDocument("TWSE_TEST", "anchor", "financial_statement", "財報", "{}",
                                  "https://openapi.twse.com.tw", self.cutoff, self.cutoff, self.cutoff, "2330.TW", "官方")
        statement = FinancialStatement("2330.TW", "income_statement", "ci", 2026, 2,
                                       "2026-01-01", "2026-06-30", "cumulative_to_quarter", "unknown",
                                       "TWD", 1000, "2026-10-03", None, "official",
                                       tuple(FinancialStatementFact(k, k, Decimal(3), "provided", "TWD", 1000) for k in FIELDS))
        self.anchor = CorporateRecord(document, financial_statement=statement)
        self.raw = {"status": 200, "data": []}
        for year in (2025, 2026):
            for end, value in (("03-31", 1000), ("06-30", 2000)):
                for field, origin in FIELDS.values():
                    self.raw["data"].append({"stock_id": "2330", "date": "%s-%s" % (year, end),
                                             "type": field, "origin_name": origin, "value": value})

    def test_normalizes_single_quarters_to_ytd_and_preserves_observation(self):
        record = verified_prior_record(self.anchor, self.raw, self.cutoff, self.cutoff)
        self.assertEqual(record.financial_statement.fiscal_year, 2025)
        self.assertEqual(record.financial_statement.period_start, "2025-01-01")
        self.assertEqual(record.financial_statement.period_end, "2025-06-30")
        self.assertEqual([fact.value for fact in record.financial_statement.facts], [Decimal(3000)] * 3)
        self.assertEqual(record.document.available_at, self.cutoff)
        self.assertIsNone(record.financial_statement.source_published_at)
        self.assertNotIn("basic_eps", [fact.metric_key for fact in record.financial_statement.facts])

    def test_future_and_naive_clock_rejected(self):
        for cutoff in ("2026-09-29T15:00:00+08:00", "2026-10-03T15:00:00"):
            with self.subTest(cutoff=cutoff), self.assertRaises(ValueError):
                verified_prior_record(self.anchor, self.raw, self.cutoff, cutoff)

    def test_missing_quarter_conflict_duplicate_stock_and_nonfinite_rejected(self):
        cases = []
        raw = copy.deepcopy(self.raw); raw["data"].pop(); cases.append(raw)
        raw = copy.deepcopy(self.raw); raw["data"][-1]["value"] += 1; cases.append(raw)
        raw = copy.deepcopy(self.raw); raw["data"].append(raw["data"][0]); cases.append(raw)
        raw = copy.deepcopy(self.raw); raw["data"][0]["stock_id"] = "1101"; cases.append(raw)
        raw = copy.deepcopy(self.raw); raw["data"][0]["value"] = "NaN"; cases.append(raw)
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                verified_prior_record(self.anchor, raw, self.cutoff, self.cutoff)

    def test_unsupported_industry_or_nonofficial_anchor_rejected(self):
        for anchor in (replace(self.anchor, financial_statement=replace(self.anchor.financial_statement, industry="other")),
                       replace(self.anchor, document=replace(self.anchor.document, source="YAHOO"))):
            with self.assertRaises(ValueError):
                verified_prior_record(anchor, self.raw, self.cutoff, self.cutoff)

    def test_provider_preserves_captures_and_stops_on_provider_failure(self):
        class Provider:
            calls = 0
            def fetch(inner, *args):
                inner.calls += 1
                if inner.calls == 2:
                    raise RuntimeError("FinMind HTTP 402；請檢查權限或額度")
                return json.dumps(self.raw)
        provider = Provider()
        repair = FundamentalRepairProvider([self.anchor] * 3, provider=provider)
        payload, fetched = repair.fetch()
        capture = json.loads(payload)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(capture["captures"]), 1)
        self.assertEqual(len(capture["failures"]), 1)
        self.assertEqual(json.loads(payload)["captures"][0]["raw"], self.raw)

    def test_analyst_brief_preserves_vendor_comparison_limit(self):
        from etf_agent.decision.analysts import build_analyst_brief
        bundle = {"bundle_id": "research", "snapshot_id": "s", "decision_cutoff": self.cutoff,
                  "bundle_sha256": "test", "snapshot": {"latest_prices": [{"symbol": "2330.TW"}],
                  "documents": [{"symbol": "2330.TW", "source": "FINMIND_VERIFIED_PRIOR"}]}}
        brief = build_analyst_brief(bundle, {}, "fundamental")
        self.assertIn("重編", brief["symbols"][0]["source_limitations"][0])

    def test_official_profile_retains_code_and_observation_time(self):
        raw = [{"公司代號": "2330", "產業別": "24", "出表日期": "1151002"}]
        records, warnings, count = OfficialCompanyProfileProvider().parse(json.dumps(raw), self.cutoff)
        self.assertEqual(count, 1)
        self.assertEqual(warnings, [])
        self.assertEqual(records[0].document.available_at, self.cutoff)
        self.assertEqual(json.loads(records[0].document.body)["industry_code"], "24")
        self.assertIsNone(json.loads(records[0].document.body)["source_published_at"])
        with self.assertRaises(ValueError):
            OfficialCompanyProfileProvider().parse(json.dumps(raw + raw), self.cutoff)

    def test_company_profile_stock_mismatch_is_not_exposed_to_analyst(self):
        from etf_agent.decision.analysts import build_analyst_brief
        bundle = {"bundle_id": "research", "snapshot_id": "s", "decision_cutoff": self.cutoff,
                  "bundle_sha256": "test", "snapshot": {"latest_prices": [{"symbol": "2330.TW"}],
                  "documents": [{"symbol": "2330.TW", "source": "TWSE_COMPANY_PROFILE",
                                 "document_type": "company_profile", "body": json.dumps({"company_code": "1101", "industry_code": "24"})}]}}
        brief = build_analyst_brief(bundle, {}, "fundamental")
        self.assertEqual(brief["symbols"][0]["company_profiles"], [])

    def test_financial_facts_reach_brief_with_evidence_and_original_units(self):
        from etf_agent.decision.analysts import build_analyst_brief
        statement = {"industry": "fh", "statement_type": "income_statement", "fiscal_year": 2026,
                     "fiscal_quarter": 2, "period_kind": "cumulative_to_quarter", "period_start": "2026-01-01",
                     "period_end": "2026-06-30", "facts": {
                         "net_income": {"value_status": "provided", "value": "97780594.00", "currency": "TWD",
                                        "unit_multiplier": 1000, "source_field": "本期稅後淨利（淨損）"},
                         "net_revenue": {"value_status": "provided", "value": "5446990", "currency": "TWD",
                                         "unit_multiplier": 1000, "source_field": "淨收益"}}}
        document = {"symbol": "2881.TW", "document_type": "financial_statement", "source_evidence_id": "financial-2881",
                    "available_at": self.cutoff, "financial_statement": statement}
        bundle = {"bundle_id": "research", "snapshot_id": "s", "decision_cutoff": self.cutoff, "bundle_sha256": "test",
                  "snapshot": {"latest_prices": [{"symbol": "2881.TW"}], "documents": [document]}}
        metrics = {"items": [{"symbol": "2881.TW", "period": "2026Q2", "metrics": []}]}
        facts = build_analyst_brief(bundle, {}, "fundamental", metrics)["symbols"][0]["financial_facts"]
        self.assertEqual([f["name"] for f in facts], ["稅後淨利", "淨收益"])
        self.assertEqual(facts[0]["value"], "97780594")
        self.assertEqual(facts[0]["unit_multiplier"], 1000)
        self.assertEqual(facts[0]["evidence_ids"], ["financial-2881"])

    def test_documents_available_after_cutoff_are_refused(self):
        from etf_agent.decision.analysts import build_analyst_brief
        from etf_agent.decision.fields import DecisionToolError
        for document_type in ("monthly_revenue", "financial_statement"):
            document = {"symbol": "2881.TW", "document_type": document_type, "source_evidence_id": "e-1",
                        "available_at": "2099-01-01T00:00:00+00:00"}
            bundle = {"bundle_id": "research", "snapshot_id": "s", "decision_cutoff": self.cutoff, "bundle_sha256": "test",
                      "snapshot": {"latest_prices": [{"symbol": "2881.TW"}], "documents": [document]}}
            with self.assertRaises(DecisionToolError, msg=document_type):
                build_analyst_brief(bundle, {}, "fundamental", {"items": []})


class FinancialIndustryRepairTests(unittest.TestCase):
    """金融業（fh／basi／bd／ins）只以稅後淨利核對；淨收益口徑未確認，不得接入。"""

    def setUp(self):
        self.cutoff = "2026-10-03T15:00:00+08:00"
        self.document = SourceDocument(
            "TWSE_MOPS_INCOME_FH", "anchor", "financial_statement", "財報", "{}",
            "https://openapi.twse.com.tw", self.cutoff, self.cutoff, self.cutoff, "2886.TW", "官方")

    def anchor(self, industry="fh", facts=("net_income", "net_revenue")):
        statement = FinancialStatement(
            "2886.TW", "income_statement", industry, 2026, 2, "2026-01-01", "2026-06-30",
            "cumulative_to_quarter", "unknown", "TWD", 1000, "2026-10-03", None, "official",
            tuple(FinancialStatementFact(k, k, Decimal(3), "provided", "TWD", 1000) for k in facts))
        return CorporateRecord(self.document, financial_statement=statement)

    def raw(self, net_income_type="IncomeAfterTax", extra_rows=()):
        rows = []
        for year in (2025, 2026):
            for end, value in (("03-31", 1000), ("06-30", 2000)):
                rows.append({"stock_id": "2886", "date": "%s-%s" % (year, end),
                             "type": net_income_type, "origin_name": "本期稅後淨利（淨損）", "value": value})
                # 淨收益也在 FinMind 的 Revenue type 下，但口徑與官方不同；補抓必須忽略它。
                rows.append({"stock_id": "2886", "date": "%s-%s" % (year, end),
                             "type": "Revenue", "origin_name": "淨收益", "value": value * 31})
        rows.extend(extra_rows)
        return {"status": 200, "data": rows}

    def test_reconciles_net_income_only_and_never_maps_net_revenue(self):
        record = verified_prior_record(self.anchor(), self.raw(), self.cutoff, self.cutoff)
        keys = [fact.metric_key for fact in record.financial_statement.facts]
        self.assertEqual(keys, ["net_income"])
        # 淨收益與營業收入都不得出現，否則下游會把它當成一般業的營收成長。
        for forbidden in ("net_revenue", "revenue", "operating_profit"):
            self.assertNotIn(forbidden, keys)
        self.assertEqual(record.financial_statement.fiscal_year, 2025)
        self.assertEqual([fact.value for fact in record.financial_statement.facts], [Decimal(3000)])

    def test_mapping_version_is_separate_from_general_industry(self):
        record = verified_prior_record(self.anchor(), self.raw(), self.cutoff, self.cutoff)
        self.assertEqual(record.financial_statement.mapping_version, FINANCIAL_MAPPING_VERSION)
        self.assertNotEqual(FINANCIAL_MAPPING_VERSION, MAPPING_VERSION)

    def test_bank_without_net_revenue_still_reconciles(self):
        record = verified_prior_record(
            self.anchor(industry="basi", facts=("net_income",)), self.raw(), self.cutoff, self.cutoff)
        self.assertEqual([fact.metric_key for fact in record.financial_statement.facts], ["net_income"])

    def test_missing_official_net_income_is_rejected(self):
        with self.assertRaises(ValueError):
            verified_prior_record(
                self.anchor(facts=("net_revenue",)), self.raw(), self.cutoff, self.cutoff)

    def test_ambiguous_net_income_types_are_rejected(self):
        extra = [{"stock_id": "2886", "date": "%s-%s" % (year, end), "type": "IncomeAfterTaxes",
                  "origin_name": "本期淨利（淨損）", "value": 9999}
                 for year in (2025, 2026) for end in ("03-31", "06-30")]
        with self.assertRaises(ValueError):
            verified_prior_record(self.anchor(), self.raw(extra_rows=extra), self.cutoff, self.cutoff)

    def test_general_industry_net_income_type_is_not_accepted_for_financials(self):
        """一般業的 IncomeAfterTaxes 不能冒充金融業的稅後淨利。"""
        with self.assertRaises(ValueError):
            verified_prior_record(
                self.anchor(), self.raw(net_income_type="IncomeAfterTaxes"), self.cutoff, self.cutoff)

    def test_mismatched_total_is_rejected(self):
        broken = self.raw()
        for row in broken["data"]:
            if row["date"] == "2026-06-30" and row["type"] == "IncomeAfterTax":
                row["value"] = 1
        with self.assertRaises(ValueError):
            verified_prior_record(self.anchor(), broken, self.cutoff, self.cutoff)

    def test_unsupported_industry_still_rejected(self):
        with self.assertRaises(ValueError):
            verified_prior_record(
                self.anchor(industry="other"), self.raw(), self.cutoff, self.cutoff)


class OtherAndBrokerIndustryRepairTests(unittest.TestCase):
    """其他業（mim）與證券期貨業（bd）的官方稅後淨利欄位名與金控／銀行不同，須各自核對。

    官方 mim 表只有「收入」「支出」，bd 表的稅後淨利叫「本期淨利（淨損）」；兩者在 FinMind
    都對應一般業的 IncomeAfterTaxes，不是金控的 IncomeAfterTax。
    """

    def setUp(self):
        self.cutoff = "2026-10-03T15:00:00+08:00"

    def anchor(self, symbol, source, industry, facts):
        document = SourceDocument(
            source, "anchor", "financial_statement", "財報", "{}",
            "https://openapi.twse.com.tw", self.cutoff, self.cutoff, self.cutoff, symbol, "官方")
        statement = FinancialStatement(
            symbol, "income_statement", industry, 2026, 2, "2026-01-01", "2026-06-30",
            "cumulative_to_quarter", "unknown", "TWD", 1000, "2026-10-03", None, "official",
            tuple(FinancialStatementFact(
                metric, metric if status == "provided" else None,
                Decimal(3) if status == "provided" else None, status, "TWD", 1000)
                for metric, status in facts))
        return CorporateRecord(document, financial_statement=statement)

    def raw(self, code, net_income_type="IncomeAfterTaxes", extra_rows=()):
        rows = []
        for year in (2025, 2026):
            for end, value in (("03-31", 1000), ("06-30", 2000)):
                rows.append({"stock_id": code, "date": "%s-%s" % (year, end),
                             "type": net_income_type, "origin_name": "本期淨利（淨損）", "value": value})
                # 其他業的 FinMind Revenue 掛在「收入」下，官方表沒有營業收入可核對，不得接入。
                rows.append({"stock_id": code, "date": "%s-%s" % (year, end),
                             "type": "Revenue", "origin_name": "收入", "value": value * 10})
        rows.extend(extra_rows)
        return {"status": 200, "data": rows}

    def mim_anchor(self, facts=(("net_income", "provided"), ("revenue", "not_reported"),
                                ("operating_profit", "not_reported"))):
        return self.anchor("2207.TW", "TWSE_MOPS_INCOME_MIM", "mim", facts)

    def bd_anchor(self, facts=(("net_income", "provided"), ("profit_before_tax", "provided"))):
        return self.anchor("6023.TWO", "TPEX_MOPS_INCOME_BD", "bd", facts)

    def test_other_industry_reconciles_net_income_only(self):
        record = verified_prior_record(self.mim_anchor(), self.raw("2207"), self.cutoff, self.cutoff)
        keys = [fact.metric_key for fact in record.financial_statement.facts]
        self.assertEqual(keys, ["net_income"])
        self.assertEqual([fact.value for fact in record.financial_statement.facts], [Decimal(3000)])
        self.assertEqual(record.financial_statement.fiscal_year, 2025)

    def test_other_industry_income_label_never_becomes_revenue(self):
        record = verified_prior_record(self.mim_anchor(), self.raw("2207"), self.cutoff, self.cutoff)
        keys = [fact.metric_key for fact in record.financial_statement.facts]
        for forbidden in ("revenue", "operating_profit"):
            self.assertNotIn(forbidden, keys)

    def test_other_industry_mapping_version_is_separate(self):
        record = verified_prior_record(self.mim_anchor(), self.raw("2207"), self.cutoff, self.cutoff)
        self.assertEqual(record.financial_statement.mapping_version, MIM_MAPPING_VERSION)
        self.assertEqual(
            len({MAPPING_VERSION, FINANCIAL_MAPPING_VERSION, MIM_MAPPING_VERSION, BD_MAPPING_VERSION}), 4)

    def test_other_industry_without_official_net_income_rejected(self):
        with self.assertRaises(ValueError):
            verified_prior_record(
                self.mim_anchor(facts=(("net_income", "not_reported"), ("revenue", "not_reported"))),
                self.raw("2207"), self.cutoff, self.cutoff)

    def test_broker_industry_reconciles_general_net_income_type(self):
        record = verified_prior_record(self.bd_anchor(), self.raw("6023"), self.cutoff, self.cutoff)
        self.assertEqual([fact.metric_key for fact in record.financial_statement.facts], ["net_income"])
        self.assertEqual(record.financial_statement.mapping_version, BD_MAPPING_VERSION)

    def test_broker_industry_rejects_holding_company_net_income_type(self):
        """金控的 IncomeAfterTax 不是官方 bd 表的欄位；缺少 bd 對應欄位時拒絕，不替代。"""
        with self.assertRaises(ValueError):
            verified_prior_record(
                self.bd_anchor(), self.raw("6023", net_income_type="IncomeAfterTax"),
                self.cutoff, self.cutoff)

    def test_broker_industry_rejects_both_net_income_types(self):
        extra = [{"stock_id": "6023", "date": "%s-%s" % (year, end), "type": "IncomeAfterTax",
                  "origin_name": "本期稅後淨利（淨損）", "value": 9999}
                 for year in (2025, 2026) for end in ("03-31", "06-30")]
        with self.assertRaises(ValueError):
            verified_prior_record(
                self.bd_anchor(), self.raw("6023", extra_rows=extra), self.cutoff, self.cutoff)

    def test_broker_industry_mismatched_total_rejected(self):
        broken = self.raw("6023")
        for row in broken["data"]:
            if row["date"] == "2026-06-30" and row["type"] == "IncomeAfterTaxes":
                row["value"] = 1
        with self.assertRaises(ValueError):
            verified_prior_record(self.bd_anchor(), broken, self.cutoff, self.cutoff)

    def test_general_industry_still_requires_all_three_metrics(self):
        """一般業不因其他業別放寬而改成只核對部分欄位。"""
        anchor = self.anchor("2330.TW", "TWSE_MOPS_INCOME_CI", "ci",
                             (("net_income", "provided"), ("operating_profit", "provided"),
                              ("revenue", "not_reported")))
        rows = []
        for year in (2025, 2026):
            for end, value in (("03-31", 1000), ("06-30", 2000)):
                for field, origin in FIELDS.values():
                    rows.append({"stock_id": "2330", "date": "%s-%s" % (year, end),
                                 "type": field, "origin_name": origin, "value": value})
        with self.assertRaises(ValueError):
            verified_prior_record(anchor, {"status": 200, "data": rows}, self.cutoff, self.cutoff)


if __name__ == "__main__":
    unittest.main()
