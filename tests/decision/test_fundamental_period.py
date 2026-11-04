"""基本面期別版本閘門：舊 bundle 維持全池 max(period)，新 bundle 逐檔取各自最新可得的一季。"""

import copy
import unittest

from etf_agent.decision import (
    DecisionInputBuilder,
    DecisionInputValidator,
    DecisionToolError,
    MomentumEngine,
    canonical_sha256,
    decision_bundle_sha256,
)
from etf_agent.decision.analysts import (
    FUNDAMENTAL_METRICS_PER_SYMBOL_VERSION,
    build_analyst_brief,
    compute_fundamental_metrics,
    fundamental_period_policy,
    select_fundamental_periods,
    validate_fundamental_metrics,
)
from etf_agent.decision.input_validator import (
    FUNDAMENTAL_PERIOD_PER_SYMBOL,
    FUNDAMENTAL_PERIOD_POLICY_FIELD,
)
from etf_agent.fundamentals import FundamentalToolError

from test_decision_input_builder import builder_inputs
from test_portfolio_decision_agent import decision_bundle


def fact(value):
    return {"source_field": "fixture", "value": value, "value_status": "provided", "currency": "TWD", "unit_multiplier": 1000}


INCOME_FACTS = {"revenue": fact("100"), "operating_profit": fact("20"), "net_income": fact("12")}
PRIOR_INCOME_FACTS = {"revenue": fact("80"), "operating_profit": fact("8"), "net_income": fact("8")}
BALANCE_FACTS = {"total_assets": fact("500"), "total_liabilities": fact("200")}


def statement(document_id, symbol, statement_type, year, quarter, facts, available_at="2026-09-19T01:00:00+00:00"):
    month_end = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}[quarter]
    income = statement_type == "income_statement"
    return {
        "document_id": document_id,
        "source": "TWSE_MOPS_%s" % statement_type.upper(),
        "external_id": "fixture:%d" % document_id,
        "version": 1,
        "document_type": "financial_statement",
        "symbol": symbol,
        "title": "fixture financial statement",
        "body": "fixture",
        "source_url": "fixture://financial/%d" % document_id,
        "published_at": "2026-09-19T00:00:00+00:00",
        "available_at": available_at,
        "content_sha256": "financial-sha-%d" % document_id,
        "source_evidence_id": "document:%d" % document_id,
        "financial_statement": {
            "statement_type": statement_type,
            "industry": "ci",
            "fiscal_year": year,
            "fiscal_quarter": quarter,
            "period_start": "%d-01-01" % year if income else None,
            "period_end": "%d-%s" % (year, month_end),
            "period_kind": "cumulative_to_quarter" if income else "point_in_time",
            "reporting_scope": "consolidated",
            "currency": "TWD",
            "unit_multiplier": 1000,
            "reported_at": "2026-09-19T00:00:00+00:00",
            "source_published_at": None,
            "mapping_version": "fixture-v1",
            "facts": facts,
        },
    }


def complete_period(start_id, symbol, year, quarter, with_prior=True):
    """某檔某季兩表齊全（可選附去年同季損益表）。"""
    documents = [
        statement(start_id, symbol, "income_statement", year, quarter, INCOME_FACTS),
        statement(start_id + 1, symbol, "balance_sheet", year, quarter, BALANCE_FACTS),
    ]
    if with_prior:
        documents.append(statement(start_id + 2, symbol, "income_statement", year - 1, quarter, PRIOR_INCOME_FACTS))
    return documents


def bundle_with_statements(documents, policy=FUNDAMENTAL_PERIOD_PER_SYMBOL):
    """2330 與 2317 兩檔的決策輸入；policy=None 代表沒有期別欄位的舊 bundle。"""
    bundle = decision_bundle()
    snapshot = bundle["snapshot"]
    for document in documents:
        snapshot["source_evidence"].append({
            "evidence_id": document["source_evidence_id"], "source": document["source"],
            "authority": "mops", "data_type": "financial_statement",
            "url": document["source_url"],
            "content_as_of": document["published_at"],
            "fetched_at": document["available_at"],
            "content_sha256": document["content_sha256"],
            "raw_payload_id": document["document_id"],
        })
        snapshot["documents"].append(document)
    if policy is not None:
        bundle[FUNDAMENTAL_PERIOD_POLICY_FIELD] = policy
    bundle["snapshot_sha256"] = canonical_sha256(snapshot)
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def staggered_documents():
    """2330 已公布 2026Q3；2317 最新只到 2026Q2（兩檔都有去年同季損益表）。"""
    return complete_period(11, "2330.TW", 2026, 3) + complete_period(21, "2317.TW", 2026, 2)


def by_symbol(metrics):
    return {item["symbol"]: item for item in metrics["items"]}


class LegacyPoolPeriodTests(unittest.TestCase):
    """沒有期別欄位的舊 bundle：全池統一用最新期別，提早公布的公司會讓其他公司被標為缺資料。"""

    def test_pool_max_period_marks_laggards_missing(self):
        metrics = compute_fundamental_metrics(bundle_with_statements(staggered_documents(), policy=None))
        self.assertEqual(metrics["schema_version"], "1.0")
        self.assertTrue(metrics["metrics_id"].endswith(":2026Q3"))
        self.assertNotIn("period_by_symbol", metrics)
        items = by_symbol(metrics)
        self.assertEqual(items["2330.TW"]["status"], "completed")
        self.assertEqual(items["2317.TW"]["status"], "unavailable")
        self.assertEqual(items["2317.TW"]["period"], "2026Q3")
        self.assertEqual(
            {gap["reason_code"] for gap in items["2317.TW"]["missing_data"]},
            {"CURRENT_STATEMENT_MISSING", "COMPARISON_STATEMENT_MISSING"},
        )

    def test_legacy_policy_reader_returns_none(self):
        self.assertIsNone(fundamental_period_policy(bundle_with_statements(staggered_documents(), policy=None)))

    def test_no_statements_returns_none_in_both_modes(self):
        self.assertIsNone(compute_fundamental_metrics(bundle_with_statements([], policy=None)))
        self.assertIsNone(compute_fundamental_metrics(bundle_with_statements([])))
        self.assertEqual(select_fundamental_periods(bundle_with_statements([])), {})


class PerSymbolPeriodTests(unittest.TestCase):
    def test_each_symbol_uses_its_own_latest_complete_period(self):
        bundle = bundle_with_statements(staggered_documents())
        metrics = compute_fundamental_metrics(bundle)
        self.assertEqual(metrics["schema_version"], FUNDAMENTAL_METRICS_PER_SYMBOL_VERSION)
        self.assertEqual(metrics["period_policy"], FUNDAMENTAL_PERIOD_PER_SYMBOL)
        self.assertEqual(metrics["status"], "completed")
        items = by_symbol(metrics)
        self.assertEqual((items["2330.TW"]["status"], items["2330.TW"]["period"], items["2330.TW"]["comparison_period"]),
                         ("completed", "2026Q3", "2025Q3"))
        self.assertEqual((items["2317.TW"]["status"], items["2317.TW"]["period"], items["2317.TW"]["comparison_period"]),
                         ("completed", "2026Q2", "2025Q2"))
        self.assertEqual(metrics["period_by_symbol"]["2317.TW"]["selection"], "symbol_latest_complete")
        self.assertEqual([group["period"] for group in metrics["period_groups"]], ["2026Q2", "2026Q3"])
        self.assertEqual(metrics["period_groups"][0]["symbols"], ["2317.TW"])
        self.assertEqual([item["symbol"] for item in metrics["items"]], ["2317.TW", "2330.TW"])
        # 每檔都算得出年增率，不再只因別家提早公布就剩月營收。
        for item in metrics["items"]:
            available = {metric["metric_key"] for metric in item["metrics"] if metric["status"] == "available"}
            self.assertIn("revenue_yoy_pct", available, item["symbol"])

    def test_prefers_latest_complete_period_over_newer_partial_statement(self):
        documents = staggered_documents() + [
            statement(31, "2317.TW", "income_statement", 2026, 3, INCOME_FACTS),
        ]
        metrics = compute_fundamental_metrics(bundle_with_statements(documents))
        chosen = metrics["period_by_symbol"]["2317.TW"]
        self.assertEqual((chosen["period"], chosen["selection"]), ("2026Q2", "symbol_latest_complete"))
        self.assertEqual(by_symbol(metrics)["2317.TW"]["status"], "completed")

    def test_partial_only_symbol_uses_latest_any_and_keeps_missing_reason(self):
        documents = complete_period(11, "2330.TW", 2026, 3) + [
            statement(31, "2317.TW", "income_statement", 2026, 2, INCOME_FACTS),
        ]
        metrics = compute_fundamental_metrics(bundle_with_statements(documents))
        chosen = metrics["period_by_symbol"]["2317.TW"]
        self.assertEqual((chosen["period"], chosen["selection"]), ("2026Q2", "symbol_latest_any"))
        item = by_symbol(metrics)["2317.TW"]
        # 只有損益表：營業利益率算得出，負債比與年增率算不出，由既有計算器判為 degraded，缺口原因照實保留。
        self.assertEqual(item["status"], "degraded")
        self.assertEqual(
            {metric["metric_key"] for metric in item["metrics"] if metric["status"] == "available"},
            {"operating_margin_pct"},
        )
        self.assertIn(
            {"scope": "current", "statement_type": "balance_sheet", "reason_code": "CURRENT_STATEMENT_MISSING"},
            item["missing_data"],
        )

    def test_symbol_without_statements_falls_back_to_pool_max_and_stays_unavailable(self):
        metrics = compute_fundamental_metrics(bundle_with_statements(complete_period(11, "2330.TW", 2026, 3)))
        chosen = metrics["period_by_symbol"]["2317.TW"]
        self.assertEqual((chosen["period"], chosen["selection"]), ("2026Q3", "pool_max_fallback"))
        self.assertEqual(by_symbol(metrics)["2317.TW"]["status"], "unavailable")
        self.assertEqual(metrics["status"], "degraded")
        # 兩檔同期別時只有一組 bundle。
        self.assertEqual(len(metrics["period_groups"]), 1)
        self.assertEqual(metrics["period_groups"][0]["symbols"], ["2317.TW", "2330.TW"])

    def test_unknown_policy_value_fails_closed(self):
        bundle = bundle_with_statements(staggered_documents(), policy="per_symbol_latest_available_v9")
        with self.assertRaises(DecisionToolError):
            compute_fundamental_metrics(bundle)
        errors = DecisionInputValidator(bundle).validate()
        self.assertTrue(any(FUNDAMENTAL_PERIOD_POLICY_FIELD in error for error in errors), errors)

    def test_statement_available_after_cutoff_is_rejected(self):
        documents = complete_period(11, "2330.TW", 2026, 3) + [
            statement(31, "2317.TW", "income_statement", 2026, 3, INCOME_FACTS, available_at="2026-09-21T00:00:00+00:00"),
            statement(32, "2317.TW", "balance_sheet", 2026, 3, BALANCE_FACTS, available_at="2026-09-21T00:00:00+00:00"),
        ]
        with self.assertRaises(FundamentalToolError):
            compute_fundamental_metrics(bundle_with_statements(documents))

    def test_brief_tells_analyst_which_period_each_symbol_uses(self):
        bundle = bundle_with_statements(staggered_documents())
        metrics = compute_fundamental_metrics(bundle)
        brief = build_analyst_brief(bundle, MomentumEngine(bundle).run(), "fundamental", metrics)
        rows = {row["symbol"]: row for row in brief["symbols"]}
        self.assertEqual(rows["2317.TW"]["financial_period"]["period"], "2026Q2")
        self.assertEqual(rows["2330.TW"]["financial_period"]["period"], "2026Q3")
        self.assertEqual({row["financial_status"] for row in brief["symbols"]}, {"completed"})
        legacy_bundle = bundle_with_statements(staggered_documents(), policy=None)
        legacy_brief = build_analyst_brief(
            legacy_bundle, MomentumEngine(legacy_bundle).run(), "fundamental", compute_fundamental_metrics(legacy_bundle)
        )
        self.assertTrue(all("financial_period" not in row for row in legacy_brief["symbols"]))


class ValidateFundamentalMetricsTests(unittest.TestCase):
    def setUp(self):
        self.bundle = bundle_with_statements(staggered_documents())
        self.metrics = compute_fundamental_metrics(self.bundle)

    def test_recomputed_payload_passes_in_both_modes(self):
        self.assertEqual(validate_fundamental_metrics(self.bundle, self.metrics), [])
        legacy = bundle_with_statements(staggered_documents(), policy=None)
        self.assertEqual(validate_fundamental_metrics(legacy, compute_fundamental_metrics(legacy)), [])
        empty = bundle_with_statements([])
        self.assertEqual(validate_fundamental_metrics(empty, None), [])

    def test_tampered_value_or_period_is_rejected(self):
        tampered = copy.deepcopy(self.metrics)
        for item in tampered["items"]:
            for metric in item["metrics"]:
                if metric["status"] == "available":
                    metric["value"] = "999"
                    break
        self.assertTrue(validate_fundamental_metrics(self.bundle, tampered))
        moved = copy.deepcopy(self.metrics)
        moved["period_by_symbol"]["2317.TW"]["period"] = "2026Q3"
        self.assertTrue(validate_fundamental_metrics(self.bundle, moved))

    def test_mode_mismatch_and_missing_payload_are_rejected(self):
        legacy = bundle_with_statements(staggered_documents(), policy=None)
        self.assertTrue(validate_fundamental_metrics(legacy, self.metrics))
        self.assertTrue(validate_fundamental_metrics(self.bundle, None))
        self.assertTrue(validate_fundamental_metrics(bundle_with_statements([]), self.metrics))


class InputBuilderPolicyTests(unittest.TestCase):
    def test_new_bundles_carry_the_policy_and_pass_validation(self):
        snapshot, rules, history, account = builder_inputs()
        bundle = DecisionInputBuilder(snapshot, rules, history, account_snapshot=account).build()
        self.assertEqual(bundle[FUNDAMENTAL_PERIOD_POLICY_FIELD], FUNDAMENTAL_PERIOD_PER_SYMBOL)
        self.assertEqual(DecisionInputValidator(bundle).validate(), [])

    def test_none_keeps_legacy_bundle_shape(self):
        snapshot, rules, history, account = builder_inputs()
        bundle = DecisionInputBuilder(
            snapshot, rules, history, account_snapshot=account, fundamental_period_policy=None
        ).build()
        self.assertNotIn(FUNDAMENTAL_PERIOD_POLICY_FIELD, bundle)
        self.assertEqual(DecisionInputValidator(bundle).validate(), [])

    def test_unknown_policy_is_rejected_at_construction(self):
        snapshot, rules, history, account = builder_inputs()
        with self.assertRaises(DecisionToolError):
            DecisionInputBuilder(snapshot, rules, history, account_snapshot=account, fundamental_period_policy="v0")


if __name__ == "__main__":
    unittest.main()
