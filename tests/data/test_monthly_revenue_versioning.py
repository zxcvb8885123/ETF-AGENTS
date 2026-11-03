"""月營收版本化：出表日期變動不產生新版本、published_at 不漂移、舊資料列不變。"""

import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.data import (
    CorporateDataCollector,
    DataAgentService,
    Instrument,
    MarketDataDatabase,
    OfficialCorporateProvider,
)
from etf_agent.data.corporate_collector import _legacy_content_sha256
from etf_agent.data.monthly_revenue_identity import (
    comparable_monthly_revenue_body,
    monthly_revenue_content_sha256,
)


def monthly_row(report_date="1151011", revenue="5914588", note="-", code="1504"):
    return {
        "出表日期": report_date,
        "資料年月": "11509",
        "公司代號": code,
        "公司名稱": "東元",
        "產業別": "電機機械",
        "營業收入-當月營收": revenue,
        "營業收入-上月營收": "5408315",
        "營業收入-去年當月營收": "5202947",
        "營業收入-上月比較增減(%)": "9.361011701426415",
        "營業收入-去年同月增減(%)": "13.677652299744741",
        "累計營業收入-當月累計營收": "47639807",
        "累計營業收入-去年累計營收": "43758727",
        "累計營業收入-前期比較增減(%)": "8.86927080854066",
        "備註": note,
    }


class FixtureProvider(OfficialCorporateProvider):
    def __init__(self, rows, fetched_at):
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


def document_rows(database):
    with database.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM source_documents ORDER BY version"
            ).fetchall()
        ]


def observation_count(database):
    with database.connect() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM source_document_observations"
        ).fetchone()[0]


class MonthlyRevenueVersioningTests(unittest.TestCase):
    def setUp(self):
        self.universe = [Instrument("1504.TW", "1504", "東元", "TWSE", "2026-07-31")]
        self.directory = tempfile.TemporaryDirectory()
        self.database = MarketDataDatabase(Path(self.directory.name) / "test.db")

    def tearDown(self):
        self.directory.cleanup()

    def collect(self, row, fetched_at):
        return CorporateDataCollector(
            self.database, [FixtureProvider([row], fetched_at)]
        ).collect(self.universe)

    def insert_legacy_version(self, row, fetched_at):
        """模擬修正前的收集器：published_at 取出表日期、雜湊含整列與 published_at。"""
        provider = FixtureProvider([row], fetched_at)
        record = provider.parse(json.dumps([row], ensure_ascii=False), fetched_at)[0][0]
        document = record.document
        legacy_sha256 = _legacy_content_sha256(record)
        self.database.initialize()
        with self.database.connect() as connection:
            self.database.upsert_instruments(connection, self.universe, True)
            run_id = "legacy-%s" % fetched_at
            connection.execute(
                "INSERT INTO collection_runs(run_id, source, started_at, status) "
                "VALUES (?, 'CORPORATE_DATA', ?, 'success')",
                (run_id, fetched_at),
            )
            raw_payload_id = self.database.insert_raw_payload(
                connection, run_id, provider.source, provider.url, fetched_at,
                json.dumps([row], ensure_ascii=False),
            )
            previous = connection.execute(
                "SELECT id, version FROM source_documents WHERE source = ? AND external_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (document.source, document.external_id),
            ).fetchone()
            version = int(previous["version"]) + 1 if previous else 1
            connection.execute(
                "INSERT INTO source_document_observations(source, external_id, content_sha256, observed_at) "
                "VALUES (?, ?, ?, ?)",
                (document.source, document.external_id, legacy_sha256, fetched_at),
            )
            cursor = connection.execute(
                """
                INSERT INTO source_documents(
                    source, external_id, version, document_type, title, body,
                    source_url, published_at, available_at, fetched_at,
                    content_sha256, raw_payload_id, supersedes_document_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document.source, document.external_id, version, document.document_type,
                    document.title, document.body, document.source_url, document.published_at,
                    fetched_at, fetched_at, legacy_sha256, raw_payload_id,
                    int(previous["id"]) if previous else None,
                ),
            )
            document_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO document_instruments(document_id, symbol, evidence) VALUES (?, ?, ?)",
                (document_id, document.symbol, document.evidence),
            )
            revenue = record.monthly_revenue
            connection.execute(
                """
                INSERT INTO monthly_revenues(
                    document_id, symbol, revenue_period, currency, unit_multiplier,
                    current_revenue, previous_month_revenue, previous_year_revenue,
                    mom_pct, yoy_pct, cumulative_revenue,
                    previous_year_cumulative_revenue, cumulative_yoy_pct, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id, revenue.symbol, revenue.revenue_period, revenue.currency,
                    revenue.unit_multiplier, str(revenue.current_revenue),
                    str(revenue.previous_month_revenue), str(revenue.previous_year_revenue),
                    str(revenue.mom_pct), str(revenue.yoy_pct), str(revenue.cumulative_revenue),
                    str(revenue.previous_year_cumulative_revenue), str(revenue.cumulative_yoy_pct),
                    revenue.note,
                ),
            )
        return legacy_sha256

    def test_same_revenue_with_drifting_report_date_keeps_one_version(self):
        first = self.collect(monthly_row("1151011"), "2026-10-12T12:00:19+00:00")
        second = self.collect(monthly_row("1151012"), "2026-10-13T12:00:23+00:00")
        third = self.collect(monthly_row("1151013"), "2026-10-14T12:00:25+00:00")
        self.assertEqual(first.stored_documents, 1)
        self.assertEqual((second.stored_documents, second.duplicate_documents), (0, 1))
        self.assertEqual((third.stored_documents, third.duplicate_documents), (0, 1))
        rows = document_rows(self.database)
        self.assertEqual([row["version"] for row in rows], [1])
        # published_at 固定為首次觀測時的出表日期 00:00（台北），不漂到 10/12、10/13。
        self.assertEqual(rows[0]["published_at"], "2026-10-10T16:00:00+00:00")
        # available_at 是首次抓到的時間，不得被往前回填。
        self.assertEqual(rows[0]["available_at"], "2026-10-12T12:00:19+00:00")
        self.assertEqual(rows[0]["fetched_at"], "2026-10-12T12:00:19+00:00")
        # 每次觀測仍記錄，供亂序保護。
        self.assertEqual(observation_count(self.database), 3)
        # 首版 body 保留整列原文（含當時的出表日期）。
        self.assertEqual(json.loads(rows[0]["body"])["出表日期"], "1151011")

    def test_changed_revenue_creates_new_version_and_keeps_first_published_at(self):
        self.collect(monthly_row("1151011", revenue="5914588"), "2026-10-12T12:00:00+00:00")
        self.collect(monthly_row("1151012", revenue="5914588"), "2026-10-13T12:00:00+00:00")
        result = self.collect(monthly_row("1151020", revenue="5920000"), "2026-10-21T12:00:00+00:00")
        self.assertEqual(result.stored_documents, 1)
        rows = document_rows(self.database)
        self.assertEqual([row["version"] for row in rows], [1, 2])
        self.assertEqual(rows[1]["supersedes_document_id"], rows[0]["id"])
        # 更正版沿用首版 published_at，不取新的出表日期（1151020 → 10/19 16:00Z）。
        self.assertEqual(rows[1]["published_at"], rows[0]["published_at"])
        self.assertEqual(rows[1]["published_at"], "2026-10-10T16:00:00+00:00")
        # 更正何時被取得由 available_at／fetched_at 表達，等於抓取時間、不回填。
        self.assertEqual(rows[1]["available_at"], "2026-10-21T12:00:00+00:00")
        self.assertEqual(rows[1]["fetched_at"], "2026-10-21T12:00:00+00:00")
        self.assertNotEqual(rows[0]["content_sha256"], rows[1]["content_sha256"])

        service = DataAgentService(self.database)
        before = service.build_snapshot("2026-10-20T00:00:00+00:00", require_prices=False)
        after = service.build_snapshot("2026-10-22T00:00:00+00:00", require_prices=False)
        self.assertEqual(before.documents[0].version, 1)
        self.assertEqual(before.documents[0].monthly_revenue.current_revenue, "5914588")
        self.assertEqual(after.documents[0].version, 2)
        self.assertEqual(after.documents[0].monthly_revenue.current_revenue, "5920000")
        evidence = next(
            item for item in after.source_evidence
            if item.evidence_id == after.documents[0].source_evidence_id
        )
        self.assertEqual(evidence.content_as_of, "2026-10-11T00:00:00+08:00")
        self.assertEqual(evidence.fetched_at, "2026-10-21T20:00:00+08:00")

    def test_note_change_is_a_content_change(self):
        self.collect(monthly_row("1151011", note="-"), "2026-10-12T12:00:00+00:00")
        result = self.collect(monthly_row("1151012", note="含子公司"), "2026-10-13T12:00:00+00:00")
        self.assertEqual(result.stored_documents, 1)
        self.assertEqual([row["version"] for row in document_rows(self.database)], [1, 2])

    def test_legacy_rows_remain_unchanged_and_same_numbers_do_not_add_version(self):
        # 模擬資料庫既有的 v1、v2：數字相同、出表日期不同、published_at 已漂移、舊公式雜湊。
        self.insert_legacy_version(monthly_row("1151011"), "2026-10-12T12:00:19+00:00")
        self.insert_legacy_version(monthly_row("1151012"), "2026-10-13T12:00:23+00:00")
        before_rows = document_rows(self.database)
        self.assertEqual([row["version"] for row in before_rows], [1, 2])
        self.assertEqual(before_rows[0]["published_at"], "2026-10-10T16:00:00+00:00")
        self.assertEqual(before_rows[1]["published_at"], "2026-10-11T16:00:00+00:00")
        service = DataAgentService(self.database)
        old_cutoff = "2026-10-13T13:00:00+00:00"
        old_snapshot = service.build_snapshot(old_cutoff, require_prices=False)
        old_view = [
            (doc.version, doc.published_at, doc.available_at, doc.content_sha256, doc.body)
            for doc in old_snapshot.documents
        ]
        self.assertEqual(old_view[0][0], 2)

        # 新邏輯抓到同樣數字、新的出表日期：不新增 v3。
        result = self.collect(monthly_row("1151013"), "2026-10-14T12:00:25+00:00")
        self.assertEqual((result.stored_documents, result.duplicate_documents), (0, 1))
        after_rows = document_rows(self.database)
        # 既有資料列逐欄位不變（不改寫、不刪除、不補雜湊）。
        self.assertEqual(after_rows, before_rows)
        # 舊 cutoff 的快照重建結果不變。
        replay = service.build_snapshot(old_cutoff, require_prices=False)
        self.assertEqual(
            [
                (doc.version, doc.published_at, doc.available_at, doc.content_sha256, doc.body)
                for doc in replay.documents
            ],
            old_view,
        )
        # 數字真的更正才有 v3，且 published_at 沿用首版（legacy v1）的值，不是 v2 漂移後的值。
        result = self.collect(monthly_row("1151020", revenue="5920000"), "2026-10-21T12:00:00+00:00")
        self.assertEqual(result.stored_documents, 1)
        rows = document_rows(self.database)
        self.assertEqual([row["version"] for row in rows], [1, 2, 3])
        self.assertEqual(rows[:2], before_rows)
        self.assertEqual(rows[2]["published_at"], "2026-10-10T16:00:00+00:00")
        self.assertEqual(rows[2]["available_at"], "2026-10-21T12:00:00+00:00")
        self.assertEqual(rows[2]["supersedes_document_id"], before_rows[1]["id"])
        latest = service.build_snapshot("2026-10-22T00:00:00+00:00", require_prices=False)
        self.assertEqual(latest.documents[0].version, 3)
        self.assertEqual(latest.documents[0].monthly_revenue.current_revenue, "5920000")

    def test_legacy_observation_still_blocks_out_of_order_older_content(self):
        # 舊收集器留下的觀測：100 於 9/15、120 於 9/17，且 9/20 又看過 120（舊公式雜湊）。
        self.insert_legacy_version(monthly_row("1150915", revenue="100"), "2026-09-15T04:00:00+00:00")
        legacy_120 = self.insert_legacy_version(monthly_row("1150917", revenue="120"), "2026-09-17T04:00:00+00:00")
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO source_document_observations(source, external_id, content_sha256, observed_at) "
                "VALUES ('TWSE_MOPS_MONTHLY_REVENUE', 'monthly-revenue:1504:2026-09', ?, ?)",
                (legacy_120, "2026-09-20T04:00:00+00:00"),
            )
        # 亂序送達、抓取時間較早的 100 不得蓋過 120。
        result = self.collect(monthly_row("1150919", revenue="100"), "2026-09-19T04:00:00+00:00")
        self.assertEqual(result.stored_documents, 0)
        self.assertEqual([row["version"] for row in document_rows(self.database)], [1, 2])

    def test_out_of_order_older_content_with_new_hashes_does_not_rewind(self):
        for revenue, report_date, fetched_at in (
            ("100", "1150915", "2026-09-15T04:00:00+00:00"),
            ("120", "1150917", "2026-09-17T04:00:00+00:00"),
            ("120", "1150920", "2026-09-20T04:00:00+00:00"),
            ("100", "1150919", "2026-09-19T04:00:00+00:00"),
        ):
            self.collect(monthly_row(report_date, revenue=revenue), fetched_at)
        self.assertEqual([row["version"] for row in document_rows(self.database)], [1, 2])

    def test_comparable_body_ignores_production_fields_and_formatting(self):
        row = monthly_row("1151016")
        compact = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        spaced = json.dumps(dict(row, 出表日期="1151017"), ensure_ascii=False, sort_keys=True)
        self.assertEqual(comparable_monthly_revenue_body(compact), comparable_monthly_revenue_body(spaced))
        self.assertNotIn("出表日期", comparable_monthly_revenue_body(compact))
        title = "東元 2026-09 月營收"
        self.assertEqual(
            monthly_revenue_content_sha256("monthly_revenue", title, compact),
            monthly_revenue_content_sha256("monthly_revenue", title, spaced),
        )
        tpex = json.dumps(dict(row, Date="1151017"), ensure_ascii=False)
        self.assertNotIn("Date", comparable_monthly_revenue_body(tpex))
        changed = json.dumps(dict(row, **{"營業收入-當月營收": "1"}), ensure_ascii=False)
        self.assertNotEqual(
            monthly_revenue_content_sha256("monthly_revenue", title, compact),
            monthly_revenue_content_sha256("monthly_revenue", title, changed),
        )
        # 非 JSON 物件的 body 原樣回傳，不猜測。
        self.assertEqual(comparable_monthly_revenue_body("not json"), "not json")
        self.assertEqual(comparable_monthly_revenue_body("[1,2]"), "[1,2]")

    def test_material_event_keeps_legacy_hash_and_published_at(self):
        provider = OfficialCorporateProvider(
            source="TWSE_MOPS_MATERIAL_EVENT",
            url="fixture://material-event",
            market="TWSE",
            document_type="material_event",
        )
        row = {
            "出表日期": "1151012",
            "發言日期": "1151011",
            "發言時間": "180000",
            "公司代號": "1504",
            "公司名稱": "東元",
            "主旨": "董事會決議",
            "符合條款": "第53款",
            "事實發生日": "1151011",
            "說明": "測試",
        }
        payload = json.dumps([row], ensure_ascii=False)
        fetched_at = "2026-10-12T12:00:00+00:00"
        record = provider.parse(payload, fetched_at)[0][0]
        provider.fetch = lambda: (payload, fetched_at)
        CorporateDataCollector(self.database, [provider]).collect(self.universe)
        rows = document_rows(self.database)
        self.assertEqual(rows[0]["content_sha256"], _legacy_content_sha256(record))
        self.assertEqual(rows[0]["published_at"], record.document.published_at)
        self.assertEqual(rows[0]["published_at"], "2026-10-11T10:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
