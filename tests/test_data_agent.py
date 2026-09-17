import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.data import (
    CorporateDataCollector,
    DataAgentStatusRepository,
    DataAgentService,
    Instrument,
    MarketDataDatabase,
    OfficialCorporateProvider,
    SnapshotDocument,
    SnapshotMonthlyRevenue,
)


def monthly_row(code="2330", revenue="100", missing_previous=False):
    return {
        "出表日期": "1150915",
        "資料年月": "11508",
        "公司代號": code,
        "公司名稱": "台積電" if code == "2330" else "池外公司",
        "營業收入-當月營收": revenue,
        "營業收入-上月營收": "-" if missing_previous else "90",
        "營業收入-去年當月營收": "80",
        "營業收入-上月比較增減(%)": "11.11",
        "營業收入-去年同月增減(%)": "25",
        "累計營業收入-當月累計營收": "800",
        "累計營業收入-去年累計營收": "700",
        "累計營業收入-前期比較增減(%)": "14.29",
        "備註": "",
    }


class FixtureProvider(OfficialCorporateProvider):
    def __init__(self, rows, fetched_at="2026-09-15T04:00:00+00:00"):
        super().__init__(
            source="TWSE_MOPS_MONTHLY_REVENUE",
            url="fixture://monthly-revenue",
            market="TWSE",
            document_type="monthly_revenue",
        )
        self.rows = rows
        self.fetched_at = fetched_at

    def fetch(self):
        return json.dumps(self.rows, ensure_ascii=False), self.fetched_at


class DataAgentTests(unittest.TestCase):
    def setUp(self):
        self.universe = [
            Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-07-31")
        ]

    def test_parses_twse_monthly_revenue_and_preserves_missing_as_null(self):
        provider = FixtureProvider([monthly_row(missing_previous=True)])
        records, warnings, fetched_rows = provider.parse(
            json.dumps(provider.rows, ensure_ascii=False), provider.fetched_at
        )
        self.assertEqual(fetched_rows, 1)
        self.assertEqual(warnings, [])
        self.assertEqual(records[0].document.symbol, "2330.TW")
        self.assertEqual(records[0].document.published_at, "2026-09-14T16:00:00+00:00")
        self.assertEqual(records[0].monthly_revenue.revenue_period, "2026-08")
        self.assertEqual(records[0].monthly_revenue.unit_multiplier, 1000)
        self.assertIsNone(records[0].monthly_revenue.previous_month_revenue)

    def test_parses_tpex_material_event_with_alternate_keys(self):
        provider = OfficialCorporateProvider(
            source="TPEX_MOPS_MATERIAL_EVENT",
            url="fixture://material-event",
            market="TPEX",
            document_type="material_event",
        )
        payload = json.dumps(
            [
                {
                    "Date": "1150916",
                    "發言日期": "1150915",
                    "發言時間": "70003",
                    "SecuritiesCompanyCode": "5904",
                    "CompanyName": "寶雅*",
                    "主旨": "董事會決議",
                    "符合條款": "第53款",
                    "事實發生日": "1150914",
                    "說明": "測試說明",
                }
            ],
            ensure_ascii=False,
        )
        records, warnings, _ = provider.parse(
            payload, "2026-09-15T01:00:10+00:00"
        )
        self.assertEqual(warnings, [])
        self.assertEqual(records[0].document.symbol, "5904.TWO")
        self.assertEqual(records[0].document.title, "董事會決議")
        self.assertEqual(records[0].document.published_at, "2026-09-14T23:00:03+00:00")

    def test_repeated_collection_is_idempotent_and_filters_universe(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            provider = FixtureProvider([monthly_row(), monthly_row(code="9999")])
            collector = CorporateDataCollector(database, [provider])
            first = collector.collect(self.universe)
            second = collector.collect(self.universe)
            self.assertEqual(first.stored_documents, 1)
            self.assertEqual(first.warning_count, 0)
            self.assertEqual(second.stored_documents, 0)
            self.assertEqual(second.duplicate_documents, 1)
            with database.connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM quality_issues").fetchone()[0],
                    0,
                )

    def test_parse_failures_keep_raw_payload_and_quality_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            provider = FixtureProvider([{"公司代號": "2330"}])
            result = CorporateDataCollector(database, [provider]).collect(self.universe)
            self.assertEqual(result.stored_documents, 0)
            self.assertEqual(result.warning_count, 1)
            with database.connect() as connection:
                issue = connection.execute(
                    "SELECT issue_code, severity FROM quality_issues"
                ).fetchone()
                raw_count = connection.execute(
                    "SELECT COUNT(*) FROM raw_payloads"
                ).fetchone()[0]
            self.assertEqual(issue["issue_code"], "PARSE_WARNING")
            self.assertEqual(issue["severity"], "warning")
            self.assertEqual(raw_count, 1)

    def test_correction_creates_version_without_changing_old_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            first_provider = FixtureProvider(
                [monthly_row(revenue="100")], "2026-09-15T04:00:00+00:00"
            )
            CorporateDataCollector(database, [first_provider]).collect(self.universe)
            service = DataAgentService(database)
            old_snapshot = service.build_snapshot(
                "2026-09-15T05:00:00+00:00", require_prices=False
            )

            corrected_provider = FixtureProvider(
                [monthly_row(revenue="120")], "2026-09-17T04:00:00+00:00"
            )
            CorporateDataCollector(database, [corrected_provider]).collect(self.universe)
            before_correction = service.build_snapshot(
                "2026-09-16T04:00:00+00:00", require_prices=False
            )
            after_correction = service.build_snapshot(
                "2026-09-18T04:00:00+00:00", require_prices=False
            )

            self.assertEqual(old_snapshot.documents[0].version, 1)
            self.assertEqual(before_correction.documents[0].version, 1)
            self.assertEqual(after_correction.documents[0].version, 2)
            self.assertEqual(
                after_correction.documents[0].monthly_revenue.current_revenue,
                "120",
            )
            self.assertIsInstance(after_correction.documents[0], SnapshotDocument)
            self.assertIsInstance(
                after_correction.documents[0].monthly_revenue,
                SnapshotMonthlyRevenue,
            )
            self.assertEqual(
                after_correction.as_dict()["documents"][0]["monthly_revenue"][
                    "current_revenue"
                ],
                "120",
            )
            with database.connect() as connection:
                old_links = connection.execute(
                    "SELECT document_id FROM snapshot_documents WHERE snapshot_id = ?",
                    (old_snapshot.snapshot_id,),
                ).fetchall()
                versions = connection.execute(
                    "SELECT version, supersedes_document_id FROM source_documents ORDER BY version"
                ).fetchall()
            self.assertEqual(len(old_links), 1)
            self.assertEqual([row["version"] for row in versions], [1, 2])
            self.assertIsNotNone(versions[1]["supersedes_document_id"])

            evidence_id = after_correction.documents[0].source_evidence_id
            evidence = next(
                item
                for item in after_correction.source_evidence
                if item.evidence_id == evidence_id
            )
            self.assertEqual(evidence.authority, "mops")
            self.assertEqual(evidence.data_type, "monthly_revenue")
            self.assertEqual(evidence.url, "fixture://monthly-revenue")
            self.assertEqual(evidence.published_at, "2026-09-15T00:00:00+08:00")
            self.assertEqual(evidence.fetched_at, "2026-09-17T12:00:00+08:00")

    def test_cutoff_excludes_documents_not_yet_obtained(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            provider = FixtureProvider(
                [monthly_row()], "2026-09-15T04:00:00+00:00"
            )
            CorporateDataCollector(database, [provider]).collect(self.universe)
            snapshot = DataAgentService(database).build_snapshot(
                "2026-09-15T03:59:59+00:00", require_prices=False
            )
            self.assertEqual(snapshot.documents, [])
            self.assertEqual(snapshot.source_evidence, [])

    def test_cutoff_requires_timezone(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DataAgentService(MarketDataDatabase(Path(directory) / "test.db"))
            with self.assertRaisesRegex(ValueError, "必須包含時區"):
                service.build_snapshot("2026-09-15T12:00:00")

    def test_status_repository_returns_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DataAgentStatusRepository(
                MarketDataDatabase(Path(directory) / "test.db")
            )
            status = repository.load()
            self.assertEqual(status.prices.row_count, 0)
            self.assertEqual(status.prices.symbol_count, 0)
            self.assertEqual(status.prices.sources, [])
            self.assertEqual(status.documents.versions, 0)
            self.assertEqual(status.snapshot_count, 0)
            self.assertIsNone(status.latest_collection)
            self.assertIsNone(status.latest_source_report)
            self.assertIsNone(status.latest_universe_validation)


if __name__ == "__main__":
    unittest.main()
