import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.core import canonical_json
from etf_agent.data.corporate_collector import _store_record
from etf_agent.data.corporate_provider import OfficialCorporateProvider
from etf_agent.data.database import MarketDataDatabase
from etf_agent.data.industry_peer import (
    IndustryPeerCollector,
    IndustryPeerError,
    IndustryPeerRepository,
    PeerSource,
    build_industry_name_map,
    load_peer_sources,
    parse_peer_payload,
    verify_industry_name_map,
    verify_pool_against_official,
)
from etf_agent.data.universe import Instrument

FETCHED_AT = "2026-11-10T03:00:00+00:00"
TWSE = PeerSource("TWSE_INDUSTRY_EPS", "TWSE", "https://example.test/t187ap14_L", "approved", "t", "e")
TPEX = PeerSource("TPEX_INDUSTRY_EPS", "TPEX", "https://example.test/t187ap14_O", "approved", "t", "e")


def twse_row(code="2308", **overrides):
    row = {
        "出表日期": "1151110", "年度": "115", "季別": "3", "公司代號": code, "公司名稱": "台達電子工業股份有限公司",
        "產業別": "電子零組件業", "基本每股盈餘(元)": "26.64", "普通股每股面額": "新台幣 10.0000元",
        "營業收入": "539667536.00", "營業利益": "91607475.00", "營業外收入及支出": "8151455.00",
        "稅後淨利": "77061840.00",
    }
    row.update(overrides)
    return row


def tpex_row(code="6488", **overrides):
    row = {
        "Date": "1151109", "Year": "115", "季別": "3", "SecuritiesCompanyCode": code, "CompanyName": "環球晶圓股份有限公司",
        "產業別": "半導體業", "基本每股盈餘": "15.00", "普通股每股面額": "新台幣 10.0000元",
        "營業收入": "44069861.00", "營業利益": "4351759.00", "營業外收入及支出": "4269884.00", "稅後淨利": "7179871.00",
    }
    row.update(overrides)
    return row


def dump(rows):
    return json.dumps(rows, ensure_ascii=False)


class ParsePeerPayloadTest(unittest.TestCase):
    def test_parses_roc_dates_thousand_units_negative_and_missing_values(self):
        rows, warnings = parse_peer_payload(
            dump([twse_row(), twse_row("1213", 營業利益="-37049.00", 營業外收入及支出="--", 稅後淨利="")]), TWSE, FETCHED_AT)
        self.assertEqual(warnings, [])
        first, second = rows
        self.assertEqual(first.content_date, "2026-11-10")
        self.assertEqual(first.published_at, "2026-11-09T16:00:00+00:00")  # Asia/Taipei 零時
        self.assertEqual((first.fiscal_year, first.fiscal_quarter), (2026, 3))
        self.assertEqual(first.revenue, Decimal("539667536.00"))
        self.assertEqual(second.operating_profit, Decimal("-37049.00"))
        self.assertIsNone(second.non_operating_income)
        self.assertIsNone(second.net_income)

    def test_tpex_uses_its_own_field_names(self):
        rows, _ = parse_peer_payload(dump([tpex_row()]), TPEX, FETCHED_AT)
        self.assertEqual(rows[0].company_code, "6488")
        self.assertEqual(rows[0].basic_eps, Decimal("15.00"))
        self.assertEqual(rows[0].content_date, "2026-11-09")

    def test_content_hash_ignores_report_date_but_not_values(self):
        first, _ = parse_peer_payload(dump([twse_row()]), TWSE, FETCHED_AT)
        same, _ = parse_peer_payload(dump([twse_row(出表日期="1151111")]), TWSE, "2026-11-11T03:00:00+00:00")
        changed, _ = parse_peer_payload(dump([twse_row(營業收入="539667537.00")]), TWSE, FETCHED_AT)
        self.assertEqual(first[0].content_sha256(), same[0].content_sha256())
        self.assertNotEqual(first[0].content_sha256(), changed[0].content_sha256())

    def test_fetched_after_cutoff_is_rejected(self):
        with self.assertRaisesRegex(IndustryPeerError, "晚於決策截止"):
            parse_peer_payload(dump([twse_row()]), TWSE, FETCHED_AT, "2026-11-10T02:59:59+00:00")
        parse_peer_payload(dump([twse_row()]), TWSE, FETCHED_AT, FETCHED_AT)

    def test_naive_times_are_rejected(self):
        with self.assertRaisesRegex(IndustryPeerError, "時區"):
            parse_peer_payload(dump([twse_row()]), TWSE, "2026-11-10T03:00:00")
        with self.assertRaisesRegex(IndustryPeerError, "時區"):
            parse_peer_payload(dump([twse_row()]), TWSE, FETCHED_AT, "2026-11-10T04:00:00")

    def test_future_report_date_is_rejected_per_row(self):
        rows, warnings = parse_peer_payload(dump([twse_row(), twse_row("1213", 出表日期="1151112")]), TWSE, FETCHED_AT)
        self.assertEqual([row.company_code for row in rows], ["2308"])
        self.assertEqual(len(warnings), 1)

    def test_missing_field_wrong_type_and_bad_values_skip_the_row(self):
        missing = twse_row("1101")
        del missing["營業收入"]
        bad_rows = [
            missing,
            twse_row("1102", 營業收入=539667536),       # 型別錯誤
            twse_row("1103", 營業收入="NaN"),            # 非有限
            twse_row("1104", 營業收入="Infinity"),
            twse_row("1105", 營業收入="abc"),
            twse_row("1106", 出表日期="115-11-10"),
            twse_row("1107", 季別="5"),
            twse_row("1108", 年度="2026"),
            twse_row("1109", 產業別=" "),
            "not an object",
        ]
        rows, warnings = parse_peer_payload(dump([twse_row()] + bad_rows), TWSE, FETCHED_AT)
        self.assertEqual([row.company_code for row in rows], ["2308"])
        self.assertEqual(len(warnings), len(bad_rows))

    def test_duplicate_company_code_rejects_whole_response(self):
        with self.assertRaisesRegex(IndustryPeerError, "重複"):
            parse_peer_payload(dump([twse_row(), twse_row()]), TWSE, FETCHED_AT)

    def test_empty_non_list_invalid_json_and_all_bad_rows_are_rejected(self):
        for payload in ("[]", "{}", "not json", dump([twse_row(營業收入="NaN")])):
            with self.assertRaises(IndustryPeerError):
                parse_peer_payload(payload, TWSE, FETCHED_AT)


class SourceRegistrationTest(unittest.TestCase):
    def test_repository_config_registers_both_sources_as_approved(self):
        sources = load_peer_sources(Path(__file__).resolve().parents[2] / "config" / "industry_peer_sources.json")
        self.assertEqual({item.market: item.license_status for item in sources}, {"TWSE": "approved", "TPEX": "approved"})

    def test_invalid_registration_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            path.write_text(json.dumps({"sources": [{"source": "X", "market": "TWSE", "url": "u"}]}), encoding="utf-8")
            with self.assertRaises(IndustryPeerError):
                load_peer_sources(path)


class RepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = MarketDataDatabase(Path(self.directory.name) / "etf.db")
        self.repository = IndustryPeerRepository(self.database)
        self.repository.initialize()

    def count(self, sql):
        with self.database.connect() as connection:
            return connection.execute(sql).fetchone()[0]


class CollectorTest(RepositoryTestCase):
    def test_rerun_is_idempotent_and_changes_create_new_version(self):
        payloads = {"TWSE_INDUSTRY_EPS": dump([twse_row()])}
        collector = IndustryPeerCollector(self.repository, [TWSE], lambda source: (payloads[source.source_id], FETCHED_AT))
        first = collector.collect()[0]
        second = collector.collect()[0]
        self.assertEqual((first.stored_rows, first.duplicate_rows), (1, 0))
        self.assertEqual((second.stored_rows, second.duplicate_rows), (0, 1))
        self.assertEqual(self.count("SELECT COUNT(*) FROM industry_peer_stats"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM raw_payloads WHERE source = 'TWSE_INDUSTRY_EPS'"), 1)
        payloads["TWSE_INDUSTRY_EPS"] = dump([twse_row(營業收入="539667537.00")])
        later = IndustryPeerCollector(self.repository, [TWSE], lambda s: (payloads[s.source_id], "2026-11-11T03:00:00+00:00"))
        self.assertEqual(later.collect()[0].stored_rows, 1)
        self.assertEqual(self.count("SELECT MAX(version) FROM industry_peer_stats"), 2)

    def test_older_fetch_does_not_overwrite_newer_version(self):
        IndustryPeerCollector(self.repository, [TWSE], lambda s: (dump([twse_row()]), "2026-11-11T03:00:00+00:00")).collect()
        stale = IndustryPeerCollector(
            self.repository, [TWSE], lambda s: (dump([twse_row(營業收入="1.00")]), FETCHED_AT)).collect()[0]
        self.assertEqual((stale.stored_rows, stale.duplicate_rows), (0, 1))
        self.assertEqual(self.count("SELECT COUNT(*) FROM industry_peer_stats"), 1)

    def test_available_at_is_the_fetch_time_and_not_backfilled(self):
        IndustryPeerCollector(self.repository, [TWSE], lambda s: (dump([twse_row()]), FETCHED_AT)).collect()
        with self.database.connect() as connection:
            row = connection.execute("SELECT available_at, fetched_at, published_at FROM industry_peer_stats").fetchone()
        self.assertEqual((row["available_at"], row["fetched_at"]), (FETCHED_AT, FETCHED_AT))
        self.assertEqual(row["published_at"], "2026-11-09T16:00:00+00:00")
        self.assertEqual(self.repository.latest_rows(as_of="2026-11-10T02:59:59+00:00"), [])
        self.assertEqual(len(self.repository.latest_rows(as_of=FETCHED_AT)), 1)

    def test_one_endpoint_failure_degrades_without_stopping_the_other(self):
        def fetch(source):
            if source.market == "TWSE":
                raise OSError("連線逾時")
            return dump([tpex_row()]), FETCHED_AT

        outcomes = IndustryPeerCollector(self.repository, [TWSE, TPEX], fetch).collect()
        self.assertEqual([item.status for item in outcomes], ["failed", "stored"])
        self.assertIn("連線逾時", outcomes[0].reason)
        self.assertEqual(self.count("SELECT COUNT(*) FROM industry_peer_stats WHERE market = 'TPEX'"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM industry_peer_stats WHERE market = 'TWSE'"), 0)

    def test_bad_response_is_a_failed_outcome_not_an_exception(self):
        outcomes = IndustryPeerCollector(
            self.repository, [TWSE, TPEX],
            lambda s: (dump([twse_row(), twse_row()]) if s.market == "TWSE" else dump([tpex_row()]), FETCHED_AT)).collect()
        self.assertEqual([item.status for item in outcomes], ["failed", "stored"])

    def test_unapproved_source_is_not_collected(self):
        blocked = PeerSource("TWSE_INDUSTRY_EPS", "TWSE", "u", "unverified", "t", "e")
        called = []
        outcomes = IndustryPeerCollector(self.repository, [blocked], lambda s: called.append(s) or ("[]", FETCHED_AT)).collect()
        self.assertEqual(outcomes[0].status, "skipped")
        self.assertEqual(called, [])

    def test_collecting_does_not_touch_existing_document_tables(self):
        before = self.count("SELECT COUNT(*) FROM source_documents")
        IndustryPeerCollector(self.repository, [TWSE], lambda s: (dump([twse_row()]), FETCHED_AT)).collect()
        self.assertEqual(self.count("SELECT COUNT(*) FROM source_documents"), before)
        self.assertEqual(self.count("SELECT COUNT(*) FROM snapshot_documents"), 0)


def instruments():
    return [
        Instrument("2308.TW", "2308", "台達電", "TWSE", "2026-09-14"),
        Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14"),
        Instrument("6488.TWO", "6488", "環球晶", "TPEX", "2026-09-14"),
    ]


def peer_rows(*rows, source="TWSE_INDUSTRY_EPS"):
    result = []
    for row in rows:
        result.append({
            "source": source, "market": row.get("market", "TWSE"), "company_code": row["code"],
            "industry_name": row["industry"], "fiscal_year": 2026, "fiscal_quarter": 3, "version": 1,
            "revenue": row.get("revenue", "100.00"), "operating_profit": row.get("operating_profit", "10.00"),
            "net_income": row.get("net_income", "8.00"), "unit_multiplier": 1000,
            "content_sha256": "x" * 64,
        })
    return result


class NameMapTest(unittest.TestCase):
    def build(self, rows, sectors=None):
        sectors = sectors or {"2308.TW": "industry:28", "2330.TW": "industry:24", "6488.TWO": "industry:24"}
        return build_industry_name_map(instruments(), sectors, rows, "sector:test")

    def test_one_to_one_mapping_is_built_per_market(self):
        artifact = self.build(peer_rows(
            {"code": "2308", "industry": "電子零組件業"}, {"code": "2330", "industry": "半導體業"},
            {"code": "6488", "industry": "半導體業", "market": "TPEX"}))
        self.assertEqual(artifact["conflicts"], [])
        self.assertEqual(artifact["mapping"]["TWSE"]["24"]["name"], "半導體業")
        self.assertEqual(artifact["mapping"]["TPEX"]["24"]["name"], "半導體業")
        self.assertEqual(artifact["status"], "completed")
        verify_industry_name_map(artifact)

    def test_one_code_with_two_names_is_rejected_and_reported(self):
        artifact = self.build(peer_rows(
            {"code": "2308", "industry": "電子零組件業"}, {"code": "2330", "industry": "半導體業"}),
            {"2308.TW": "industry:24", "2330.TW": "industry:24"})
        self.assertNotIn("24", artifact["mapping"]["TWSE"])
        self.assertEqual(artifact["conflicts"][0]["kind"], "code_multiple_names")
        self.assertEqual(sorted(artifact["conflicts"][0]["names"]), ["半導體業", "電子零組件業"])
        self.assertEqual(artifact["status"], "degraded")

    def test_one_name_with_two_codes_is_rejected_and_reported(self):
        artifact = self.build(peer_rows(
            {"code": "2308", "industry": "半導體業"}, {"code": "2330", "industry": "半導體業"}),
            {"2308.TW": "industry:28", "2330.TW": "industry:24"})
        self.assertEqual(artifact["mapping"]["TWSE"], {})
        self.assertEqual(artifact["conflicts"][0]["kind"], "name_multiple_codes")

    def test_cross_market_name_difference_is_reported_not_merged(self):
        artifact = self.build(peer_rows(
            {"code": "2330", "industry": "金融保險業"}, {"code": "6488", "industry": "金融業", "market": "TPEX"}))
        self.assertEqual(artifact["conflicts"], [])
        self.assertEqual(artifact["cross_market_name_differences"],
                         [{"code": "24", "TWSE": "金融保險業", "TPEX": "金融業"}])

    def test_unmapped_symbols_are_listed_not_guessed(self):
        artifact = self.build(peer_rows({"code": "2330", "industry": "半導體業"}))
        self.assertEqual({item["symbol"] for item in artifact["unmapped_pool_symbols"]}, {"2308.TW", "6488.TWO"})

    def test_version_is_stable_and_tamper_is_detected(self):
        rows = peer_rows({"code": "2330", "industry": "半導體業"})
        first = self.build(rows)
        again = build_industry_name_map(instruments(), {"2308.TW": "industry:28", "2330.TW": "industry:24",
                                                        "6488.TWO": "industry:24"}, rows, "sector:test",
                                        [{"fetched_at": "different"}])
        self.assertEqual(first["version"], again["version"])
        first["mapping"]["TWSE"]["24"]["name"] = "改過"
        with self.assertRaisesRegex(IndustryPeerError, "不符"):
            verify_industry_name_map(first)

    def test_repository_artifact_if_present_is_self_consistent(self):
        path = Path(__file__).resolve().parents[2] / "data" / "industry_name_map.json"
        if path.exists():
            verify_industry_name_map(json.loads(path.read_text(encoding="utf-8")))


class VerificationTest(RepositoryTestCase):
    def store_official(self, industry="ci", code="2308", year=2026, quarter=3, **overrides):
        row = {
            "出表日期": "1151110", "年度": "115" if year == 2026 else "114", "季別": str(quarter), "公司代號": code,
            "公司名稱": "x", "營業收入": "100.00", "營業利益（損失）": "10.00",
            "稅前淨利（淨損）": "9.00", "本期淨利（淨損）": "8.00", "基本每股盈餘（元）": "1.00",
        }
        row.update(overrides)
        market = "TWSE"
        provider = OfficialCorporateProvider("TWSE_MOPS_INCOME_%s" % industry.upper(), "https://x.test", market,
                                             "financial_statement", "income_statement", industry)
        records, _, _ = provider.parse(canonical_json([row]), FETCHED_AT)
        with self.database.connect() as connection:
            self.database.upsert_instruments(connection, instruments(), True)
            _store_record(connection, records[0], self.raw(connection))

    def raw(self, connection):
        connection.execute("INSERT OR IGNORE INTO collection_runs(run_id, source, started_at, status) VALUES ('r', 'T', 'now', 'success')")
        return self.database.insert_raw_payload(connection, "r", "T", "u", FETCHED_AT, "[]")

    def verify(self, rows, universe=None):
        return verify_pool_against_official(self.database, universe or instruments()[:1], rows, "2026-11-10T04:00:00+00:00")

    def test_exact_match_is_verified(self):
        self.store_official()
        result = self.verify(peer_rows({"code": "2308", "industry": "電子零組件業"}))
        self.assertEqual((result["verified"], result["unavailable"], result["not_comparable"]), (1, 0, 0))
        self.assertEqual(result["by_symbol"]["2308.TW"]["status"], "verified")

    def test_one_dollar_difference_is_rejected(self):
        self.store_official()
        # t187ap14 單位千元：差 0.001 千元＝1 元
        result = self.verify(peer_rows({"code": "2308", "industry": "電子零組件業", "revenue": "100.001"}))
        detail = result["by_symbol"]["2308.TW"]
        self.assertEqual((detail["status"], detail["reason"]), ("unavailable", "value_mismatch"))
        self.assertEqual(detail["metrics"]["revenue"]["status"], "mismatch")
        self.assertEqual(detail["metrics"]["operating_profit"]["status"], "match")
        self.assertEqual(result["value_mismatch"], 1)

    def test_different_period_is_unavailable(self):
        self.store_official(year=2025, quarter=3)
        detail = self.verify(peer_rows({"code": "2308", "industry": "電子零組件業"}))["by_symbol"]["2308.TW"]
        self.assertEqual((detail["status"], detail["reason"]), ("unavailable", "period_mismatch"))

    def test_missing_values_are_unavailable_not_filled(self):
        self.store_official(**{"營業利益（損失）": ""})
        detail = self.verify(peer_rows({"code": "2308", "industry": "電子零組件業"}))["by_symbol"]["2308.TW"]
        self.assertEqual(detail["reason"], "official_not_reported")
        self.store_official(code="2330", **{"營業收入": "100.00"})
        rows = peer_rows({"code": "2330", "industry": "半導體業"})
        rows[0]["net_income"] = None
        detail = self.verify(rows, instruments()[1:2])["by_symbol"]["2330.TW"]
        self.assertEqual(detail["reason"], "peer_value_missing")

    def test_no_peer_row_and_no_official_statement(self):
        self.assertEqual(self.verify([])["by_symbol"]["2308.TW"]["reason"], "no_peer_row")
        detail = self.verify(peer_rows({"code": "2308", "industry": "電子零組件業"}))["by_symbol"]["2308.TW"]
        self.assertEqual(detail["reason"], "no_official_statement")

    def test_financial_industry_is_not_comparable_not_a_failure(self):
        self.store_official(industry="fh")
        result = self.verify(peer_rows({"code": "2308", "industry": "金融保險業"}))
        self.assertEqual((result["verified"], result["unavailable"], result["not_comparable"]), (0, 0, 1))
        self.assertEqual(result["value_mismatch"], 0)

    def test_financial_name_without_official_statement_is_not_comparable(self):
        result = self.verify(peer_rows({"code": "2308", "industry": "金融保險業"}))
        self.assertEqual(result["not_comparable"], 1)

    def test_latest_official_version_is_used(self):
        self.store_official(營業收入="99.00")
        self.store_official()
        result = self.verify(peer_rows({"code": "2308", "industry": "電子零組件業"}))
        self.assertEqual(result["verified"], 1)

    def test_does_not_modify_existing_tables(self):
        self.store_official()
        before = self.count("SELECT COUNT(*) FROM source_documents")
        self.verify(peer_rows({"code": "2308", "industry": "電子零組件業"}))
        self.assertEqual(self.count("SELECT COUNT(*) FROM source_documents"), before)


if __name__ == "__main__":
    unittest.main()
