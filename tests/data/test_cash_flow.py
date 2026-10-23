import copy
import json
import unittest

from etf_agent.core import canonical_json
from etf_agent.data.cash_flow import (
    CASH_FLOW_DOCUMENT_TYPE, CASH_FLOW_SOURCE, FIELDS, VerifiedCashFlowProvider, verified_cash_flow_record,
)
from etf_agent.decision.cash_flow_context import cash_flow_context
from etf_agent.decision.contracts import DecisionToolError
from test_fundamental_research_agent import fact, snapshot_fixture


OBSERVED = "2026-10-22T10:00:00+00:00"


def anchor(**changes):
    row = {"symbol": "2330.TW", "industry": "ci", "fiscal_year": 2026, "fiscal_quarter": 2,
           "period_end": "2026-06-30", "period_kind": "cumulative_to_quarter", "currency": "TWD",
           "source": "TWSE_MOPS_INCOME_CI", "external_id": "income:2330", "available_at": "2026-09-21T08:00:00+00:00",
           "content_sha256": "anchor-sha", "profit_before_tax": "1550", "unit_multiplier": 1000, "value_status": "provided"}
    row.update(changes)
    return row


def rows(pretax="1550000", operating="1482000", capex="-846000", available_at="2026-09-30T06:00:00+00:00"):
    values = {"operating_cash_flow": operating, "capital_expenditure": capex, "pretax_income": pretax}
    return [{"stock_id": "2330", "period_end": "2026-06-30", "fact_type": FIELDS[key][0], "origin_name": FIELDS[key][1],
             "value": value, "available_at": available_at, "content_sha256": "fact-%s" % key, "raw_payload_id": 7}
            for key, value in values.items() if value is not None]


class VerifiedCashFlowRecordTests(unittest.TestCase):
    def test_matching_pretax_income_creates_document_at_latest_available_time(self):
        record = verified_cash_flow_record(anchor(), rows(), OBSERVED)
        document = record.document
        self.assertEqual((document.source, document.document_type, document.symbol),
                         (CASH_FLOW_SOURCE, CASH_FLOW_DOCUMENT_TYPE, "2330.TW"))
        # 可得時間是本次核對的觀測時間，不回填到官方基準或 FinMind 事實較早的時間。
        self.assertEqual((document.published_at, document.available_at, document.fetched_at), (OBSERVED,) * 3)
        body = json.loads(document.body)
        self.assertEqual(body["facts"]["operating_cash_flow"]["value"], "1482000")
        self.assertEqual(body["official_anchor"]["profit_before_tax_twd"], "1550000")

    def test_mismatch_wrong_stock_period_or_missing_field_is_rejected(self):
        cases = [rows(pretax="1550001"), rows(operating=None)]
        wrong_stock = rows(); wrong_stock[0]["stock_id"] = "2317"; cases.append(wrong_stock)
        wrong_period = rows(); wrong_period[0]["period_end"] = "2026-03-31"; cases.append(wrong_period)
        renamed = rows(); renamed[0]["origin_name"] = "營業活動之淨現金流入"; cases.append(renamed)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                verified_cash_flow_record(anchor(), case, OBSERVED)

    def test_conflicting_values_at_same_time_are_rejected_but_later_revision_wins(self):
        conflict = rows() + [dict(rows()[0], value="1", content_sha256="other")]
        with self.assertRaisesRegex(ValueError, "多個不同數值"):
            verified_cash_flow_record(anchor(), conflict, OBSERVED)
        revised = rows() + [dict(rows()[0], value="1500000", available_at="2026-10-01T00:00:00+00:00")]
        body = json.loads(verified_cash_flow_record(anchor(), revised, OBSERVED).document.body)
        self.assertEqual(body["facts"]["operating_cash_flow"]["value"], "1500000")

    def test_data_after_observation_or_unsupported_anchor_is_rejected(self):
        with self.assertRaises(ValueError):
            verified_cash_flow_record(anchor(), rows(available_at="2026-10-23T00:00:00+00:00"), OBSERVED)
        with self.assertRaises(ValueError):
            verified_cash_flow_record(anchor(available_at="2026-10-23T00:00:00+00:00"), rows(), OBSERVED)
        for changes in ({"industry": "fh"}, {"period_kind": "point_in_time"}, {"value_status": "not_reported"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                verified_cash_flow_record(anchor(**changes), rows(), OBSERVED)

    def test_missing_capex_is_kept_as_not_reported(self):
        body = json.loads(verified_cash_flow_record(anchor(), rows(capex=None), OBSERVED).document.body)
        self.assertEqual(body["facts"]["capital_expenditure"], {
            "value": None, "source_field": None, "origin_name": FIELDS["capital_expenditure"][1], "value_status": "not_reported"})

    def test_provider_parse_skips_failures_with_warnings(self):
        payload = canonical_json({"observed_at": OBSERVED, "captures": [
            {"anchor": anchor(), "rows": rows()},
            {"anchor": anchor(symbol="2317.TW"), "rows": []},
        ]})
        records, warnings, count = VerifiedCashFlowProvider("unused.db", ["2330.TW"]).parse(payload, OBSERVED)
        self.assertEqual([record.document.symbol for record in records], ["2330.TW"])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(count, 3)


def cash_flow_snapshot(pretax="1550000", operating="1482000", capex="-846000"):
    snapshot = snapshot_fixture()
    snapshot["documents"][0]["financial_statement"]["facts"]["profit_before_tax"] = fact("1550")
    record = verified_cash_flow_record(anchor(), rows(pretax="1550000", operating=operating, capex=capex), OBSERVED)
    body = json.loads(record.document.body)
    body["facts"]["pretax_income"]["value"] = pretax
    snapshot["documents"].append({
        "document_id": 99, "source": CASH_FLOW_SOURCE, "external_id": record.document.external_id, "version": 1,
        "document_type": CASH_FLOW_DOCUMENT_TYPE, "symbol": "2330.TW", "title": record.document.title,
        "body": json.dumps(body, ensure_ascii=False), "source_url": record.document.source_url,
        "published_at": record.document.published_at, "available_at": record.document.available_at,
        "content_sha256": "cash-flow-sha", "source_evidence_id": "document:99",
    })
    return snapshot


class CashFlowContextTests(unittest.TestCase):
    def test_free_cash_flow_and_quality_ratio_are_computed_in_python(self):
        context = cash_flow_context({"snapshot": cash_flow_snapshot()})["2330.TW"]
        self.assertEqual(context["status"], "available")
        self.assertEqual(context["period"], "2026Q2")
        self.assertEqual(context["free_cash_flow"], "636000")
        self.assertEqual(context["operating_cash_flow_to_pretax_income_pct"], "95.61")
        self.assertEqual(context["evidence_ids"], ["document:99"])

    def test_pretax_not_matching_snapshot_official_is_unavailable(self):
        context = cash_flow_context({"snapshot": cash_flow_snapshot(pretax="1")})["2330.TW"]
        self.assertEqual(context["status"], "unavailable")
        snapshot = cash_flow_snapshot()
        del snapshot["documents"][0]["financial_statement"]["facts"]["profit_before_tax"]
        self.assertEqual(cash_flow_context({"snapshot": snapshot})["2330.TW"]["status"], "unavailable")

    def test_missing_capex_leaves_free_cash_flow_empty_with_note(self):
        context = cash_flow_context({"snapshot": cash_flow_snapshot(capex=None)})["2330.TW"]
        self.assertIsNone(context["free_cash_flow"])
        self.assertTrue(context["notes"])

    def test_broken_or_mismatched_document_fails_closed(self):
        snapshot = cash_flow_snapshot()
        snapshot["documents"][-1]["body"] = "not json"
        with self.assertRaises(DecisionToolError):
            cash_flow_context({"snapshot": snapshot})
        snapshot = cash_flow_snapshot()
        snapshot["documents"][-1]["symbol"] = "2317.TW"
        with self.assertRaisesRegex(DecisionToolError, "誤配"):
            cash_flow_context({"snapshot": snapshot})

    def test_no_cash_flow_documents_returns_nothing(self):
        self.assertEqual(cash_flow_context({"snapshot": copy.deepcopy(snapshot_fixture())}), {})


if __name__ == "__main__":
    unittest.main()
