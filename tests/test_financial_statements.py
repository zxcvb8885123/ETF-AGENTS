import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.data import (
    CorporateDataCollector,
    DataAgentService,
    FinancialStatementCollector,
    FinancialStatementRequest,
    Instrument,
    MarketDataDatabase,
    OfficialCorporateProvider,
    SnapshotFinancialStatement,
)


def income_row(code="2330", revenue="1000"):
    return {
        "出表日期": "1150921",
        "年度": "115",
        "季別": "2",
        "公司代號": code,
        "公司名稱": "台積電",
        "營業收入": revenue,
        "營業利益（損失）": "200",
        "稅前淨利（淨損）": "180",
        "本期淨利（淨損）": "150",
        "基本每股盈餘（元）": "5.00",
    }


def balance_row(code="2330"):
    return {
        "Date": "1150921",
        "Year": "115",
        "Season": "2",
        "SecuritiesCompanyCode": code,
        "CompanyName": "台積電",
        "資產總計": "10000",
        "負債總計": "4000",
        "權益總計": "6000",
    }


class FixtureFinancialProvider(OfficialCorporateProvider):
    def __init__(
        self,
        *,
        source,
        market,
        statement_type,
        industry,
        rows,
        fetched_at="2026-09-21T04:00:00+00:00",
        mapping_version="twse-tpex-openapi-2026-09-v1",
    ):
        super().__init__(
            source=source,
            url="fixture://%s" % source,
            market=market,
            document_type="financial_statement",
            statement_type=statement_type,
            industry=industry,
            mapping_version=mapping_version,
        )
        self.rows = rows
        self.fetched_at = fetched_at

    def fetch(self):
        return json.dumps(self.rows, ensure_ascii=False), self.fetched_at


class FinancialStatementTests(unittest.TestCase):
    def setUp(self):
        self.universe = [
            Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-07-31")
        ]

    def test_parses_general_industry_income_with_explicit_mapping(self):
        provider = FixtureFinancialProvider(
            source="TWSE_MOPS_INCOME_CI",
            market="TWSE",
            statement_type="income_statement",
            industry="ci",
            rows=[income_row()],
        )
        records, warnings, row_count = provider.parse(
            json.dumps(provider.rows, ensure_ascii=False), provider.fetched_at
        )
        self.assertEqual(row_count, 1)
        self.assertEqual(warnings, [])
        statement = records[0].financial_statement
        self.assertEqual(records[0].document.symbol, "2330.TW")
        self.assertEqual(statement.fiscal_year, 2026)
        self.assertEqual(statement.fiscal_quarter, 2)
        self.assertEqual(statement.period_start, "2026-01-01")
        self.assertEqual(statement.period_end, "2026-06-30")
        self.assertEqual(statement.period_kind, "cumulative_to_quarter")
        self.assertEqual(statement.source_published_at, None)
        facts = {item.metric_key: item for item in statement.facts}
        self.assertEqual(facts["revenue"].source_field, "營業收入")
        self.assertEqual(str(facts["revenue"].value), "1000")
        self.assertEqual(facts["revenue"].unit_multiplier, 1000)
        self.assertEqual(facts["basic_eps"].unit_multiplier, 1)

    def test_financial_industry_does_not_coerce_its_interest_income_to_revenue(self):
        provider = FixtureFinancialProvider(
            source="TWSE_MOPS_INCOME_BASI",
            market="TWSE",
            statement_type="income_statement",
            industry="basi",
            rows=[
                {
                    **income_row(),
                    "利息淨收益": "999",
                    "營業收入": "",
                }
            ],
        )
        records, warnings, _ = provider.parse(
            json.dumps(provider.rows, ensure_ascii=False), provider.fetched_at
        )
        self.assertEqual(warnings, [])
        facts = {
            item.metric_key: item for item in records[0].financial_statement.facts
        }
        self.assertNotIn("revenue", facts)
        self.assertEqual(str(facts["net_income"].value), "150")

    def test_tpex_alternate_keys_and_balance_sheet_are_preserved(self):
        provider = FixtureFinancialProvider(
            source="TPEX_MOPS_BALANCE_CI",
            market="TPEX",
            statement_type="balance_sheet",
            industry="ci",
            rows=[balance_row(code="3718")],
        )
        records, warnings, _ = provider.parse(
            json.dumps(provider.rows, ensure_ascii=False), provider.fetched_at
        )
        self.assertEqual(warnings, [])
        statement = records[0].financial_statement
        self.assertEqual(records[0].document.symbol, "3718.TWO")
        self.assertEqual(statement.period_start, None)
        self.assertEqual(statement.period_kind, "point_in_time")
        facts = {item.metric_key: item for item in statement.facts}
        self.assertEqual(str(facts["total_assets"].value), "10000")
        self.assertEqual(str(facts["total_equity"].value), "6000")

    def test_nan_financial_value_is_rejected_as_a_parse_warning(self):
        provider = FixtureFinancialProvider(
            source="TWSE_MOPS_INCOME_CI",
            market="TWSE",
            statement_type="income_statement",
            industry="ci",
            rows=[income_row(revenue="NaN")],
        )
        records, warnings, _ = provider.parse(
            json.dumps(provider.rows, ensure_ascii=False), provider.fetched_at
        )
        self.assertEqual(records, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("有限值", warnings[0])

    def test_empty_industry_placeholder_is_not_treated_as_a_company_row(self):
        provider = FixtureFinancialProvider(
            source="TPEX_MOPS_INCOME_INS",
            market="TPEX",
            statement_type="income_statement",
            industry="ins",
            rows=[
                {
                    "Date": "1150921",
                    "Year": "115",
                    "Season": "2",
                    "SecuritiesCompanyCode": "",
                    "CompanyName": "",
                }
            ],
        )
        records, warnings, fetched_rows = provider.parse(
            json.dumps(provider.rows, ensure_ascii=False), provider.fetched_at
        )
        self.assertEqual(fetched_rows, 1)
        self.assertEqual(records, [])
        self.assertEqual(warnings, [])

    def test_collection_snapshot_and_correction_keep_versions_time_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            first = FixtureFinancialProvider(
                source="TWSE_MOPS_INCOME_CI",
                market="TWSE",
                statement_type="income_statement",
                industry="ci",
                rows=[income_row(revenue="1000")],
            )
            CorporateDataCollector(database, [first]).collect(self.universe)
            before = DataAgentService(database).build_snapshot(
                "2026-09-21T03:59:59+00:00", require_prices=False
            )
            after = DataAgentService(database).build_snapshot(
                "2026-09-21T04:01:00+00:00", require_prices=False
            )
            self.assertEqual(before.documents, [])
            self.assertEqual(len(after.documents), 1)
            self.assertIsInstance(
                after.documents[0].financial_statement, SnapshotFinancialStatement
            )
            self.assertEqual(
                after.documents[0].financial_statement.facts["revenue"]["value"],
                "1000",
            )
            evidence = next(
                item
                for item in after.source_evidence
                if item.evidence_id == after.documents[0].source_evidence_id
            )
            self.assertEqual(evidence.published_at, None)
            self.assertEqual(evidence.content_as_of, "2026-09-21T00:00:00+08:00")

            corrected = FixtureFinancialProvider(
                source="TWSE_MOPS_INCOME_CI",
                market="TWSE",
                statement_type="income_statement",
                industry="ci",
                rows=[income_row(revenue="1200")],
                fetched_at="2026-09-23T04:00:00+00:00",
            )
            CorporateDataCollector(database, [corrected]).collect(self.universe)
            old_cutoff = DataAgentService(database).build_snapshot(
                "2026-09-22T04:00:00+00:00", require_prices=False
            )
            new_cutoff = DataAgentService(database).build_snapshot(
                "2026-09-24T04:00:00+00:00", require_prices=False
            )
            self.assertEqual(old_cutoff.documents[0].version, 1)
            self.assertEqual(new_cutoff.documents[0].version, 2)
            self.assertEqual(
                new_cutoff.documents[0].financial_statement.facts["revenue"]["value"],
                "1200",
            )

    def test_fixed_denominator_coverage_reports_missing_statement(self):
        income = FixtureFinancialProvider(
            source="TWSE_MOPS_INCOME_CI",
            market="TWSE",
            statement_type="income_statement",
            industry="ci",
            rows=[income_row()],
        )
        balance = FixtureFinancialProvider(
            source="TWSE_MOPS_BALANCE_CI",
            market="TWSE",
            statement_type="balance_sheet",
            industry="ci",
            rows=[],
        )
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            result = FinancialStatementCollector(database, [income, balance]).collect(
                self.universe, FinancialStatementRequest(2026, 2)
            )
            self.assertEqual(result.expected_count, 2)
            self.assertEqual(result.covered_count, 1)
            self.assertEqual(result.status, "degraded")
            self.assertEqual(len(result.gaps), 1)
            self.assertEqual(result.gaps[0].statement_type, "balance_sheet")
            with database.connect() as connection:
                report_count = connection.execute(
                    "SELECT COUNT(*) FROM source_feasibility_reports"
                ).fetchone()[0]
            self.assertEqual(report_count, 1)

    def test_mapping_contract_change_creates_a_new_document_version(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            first = FixtureFinancialProvider(
                source="TWSE_MOPS_INCOME_CI",
                market="TWSE",
                statement_type="income_statement",
                industry="ci",
                rows=[income_row()],
            )
            changed_mapping = FixtureFinancialProvider(
                source="TWSE_MOPS_INCOME_CI",
                market="TWSE",
                statement_type="income_statement",
                industry="ci",
                rows=[income_row()],
                fetched_at="2026-09-22T04:00:00+00:00",
                mapping_version="twse-tpex-openapi-2026-10-v2",
            )
            CorporateDataCollector(database, [first]).collect(self.universe)
            CorporateDataCollector(database, [changed_mapping]).collect(self.universe)
            with database.connect() as connection:
                rows = connection.execute(
                    "SELECT version FROM source_documents ORDER BY version"
                ).fetchall()
            self.assertEqual([row["version"] for row in rows], [1, 2])


class LatestDueQuarterTests(unittest.TestCase):
    def test_quarter_switches_only_after_statutory_deadline(self):
        from datetime import date

        from etf_agent.data import latest_due_quarter

        cases = {
            "2026-01-10": (2025, 3),
            "2026-03-31": (2025, 3),
            "2026-04-01": (2025, 4),
            "2026-05-15": (2025, 4),
            "2026-05-16": (2026, 1),
            "2026-08-14": (2026, 1),
            "2026-08-15": (2026, 2),
            "2026-11-14": (2026, 2),
            "2026-11-15": (2026, 3),
        }
        for day, expected in cases.items():
            self.assertEqual(latest_due_quarter(date.fromisoformat(day)), expected, day)


if __name__ == "__main__":
    unittest.main()
