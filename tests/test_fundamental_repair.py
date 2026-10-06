import copy
import unittest
from dataclasses import replace
from decimal import Decimal

from etf_agent.data.corporate import CorporateRecord, FinancialStatement, FinancialStatementFact, SourceDocument
from etf_agent.data.fundamental_repair import FIELDS, FundamentalRepairProvider, OfficialCompanyProfileProvider, verified_prior_record
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
        for anchor in (replace(self.anchor, financial_statement=replace(self.anchor.financial_statement, industry="basi")),
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
                    "financial_statement": statement}
        bundle = {"bundle_id": "research", "snapshot_id": "s", "decision_cutoff": self.cutoff, "bundle_sha256": "test",
                  "snapshot": {"latest_prices": [{"symbol": "2881.TW"}], "documents": [document]}}
        metrics = {"items": [{"symbol": "2881.TW", "period": "2026Q2", "metrics": []}]}
        facts = build_analyst_brief(bundle, {}, "fundamental", metrics)["symbols"][0]["financial_facts"]
        self.assertEqual([f["name"] for f in facts], ["稅後淨利", "淨收益"])
        self.assertEqual(facts[0]["value"], "97780594")
        self.assertEqual(facts[0]["unit_multiplier"], 1000)
        self.assertEqual(facts[0]["evidence_ids"], ["financial-2881"])


if __name__ == "__main__":
    unittest.main()
