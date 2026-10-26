import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from etf_agent.core import canonical_sha256
from etf_agent.decision import DecisionInputValidator, MomentumEngine, build_analyst_brief, decision_bundle_sha256
from etf_agent.decision.analysts import (
    EVENT_MARKET_VERSION, EVENT_NEWS_VERSION, AnalystReportValidator, analyst_report_names,
    no_event_item, seal_event_market_report,
)
from etf_agent.perception.company_news import CompanyNewsError, build_company_news_input, validate_pack
from etf_agent.perception.company_news.contracts import REVIEW_TABLE, SOURCES, TERMS_URL, canonical_content_id
from etf_agent.perception.company_news.tools import seal
from etf_agent.perception.market_news.repository import MarketNewsRepository

from test_portfolio_decision_agent import CUTOFF, decision_bundle


TERMS_TEXT = "人工測試規範，不代表真實核准"


def candidate(stock_id, link, title, available_at):
    content = {"source": "FINMIND_NEWS", "stock_id": stock_id, "title": title, "link": link,
               "publisher": "測試媒體", "source_time_raw": "2026-09-19 10:00:00", "published_at": None,
               "license_status": "unverified", "license_url": TERMS_URL,
               "canonical_content_id": canonical_content_id(link)}
    return {**content, "available_at": available_at, "fetched_at": available_at,
            "content_sha256": canonical_sha256(content)}


ROWS = [
    candidate("2330", "https://udn.com/news/story/1", "台積電擴產", "2026-09-19T14:00:00+00:00"),
    # 同一連結的更正版本：去重後保留較早可得的那則。
    candidate("2330", "https://udn.com/news/story/1#top", "台積電擴產（更新）", "2026-09-19T15:00:00+00:00"),
    candidate("2330", "https://www.cmoney.tw/forum/1", "網友爆料", "2026-09-19T14:00:00+00:00"),
    candidate("2317", "https://money.udn.com/money/story/2", "截止後新聞", "2026-09-20T01:00:00+00:00"),
    # 前一晚那批：只用截止前最後一批，不重複分析舊聞。
    candidate("2317", "https://ec.ltn.com.tw/article/3", "前一批舊聞", "2026-09-18T14:00:00+00:00"),
    candidate("2317", "https://ec.ltn.com.tw/article/4", "鴻海接單", "2026-09-19T14:10:00+00:00"),
]


class NewsDatabaseCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "news.db"
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE finmind_news_candidates (id INTEGER PRIMARY KEY, source TEXT, stock_id TEXT,
                title TEXT, link TEXT, publisher TEXT, source_time_raw TEXT, published_at TEXT, available_at TEXT,
                fetched_at TEXT, license_status TEXT, license_url TEXT, canonical_content_id TEXT,
                content_sha256 TEXT UNIQUE, raw_payload_id INTEGER)""")
            db.execute("""CREATE TABLE raw_payloads (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, source TEXT,
                endpoint TEXT, fetched_at TEXT, sha256 TEXT, payload_json TEXT)""")
            for row in ROWS:
                db.execute("INSERT INTO finmind_news_candidates (%s) VALUES (%s)" % (
                    ",".join(row), ",".join("?" * len(row))), list(row.values()))
                self.fetch(db, row["stock_id"], row["fetched_at"])
        self.bundle = decision_bundle()
        self.snapshot = self.bundle["snapshot"]

    @staticmethod
    def fetch(db, stock_id, fetched_at, source="FINMIND_NEWS"):
        """一次實際抓取的紀錄（含空回應）；端點格式與 FinMindNewsCollector 相同。"""
        endpoint = "https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockNews&data_id=%s" % stock_id
        db.execute("INSERT INTO raw_payloads (run_id, source, endpoint, fetched_at, sha256, payload_json) "
                   "VALUES ('run', ?, ?, ?, 'x', '{}')", (source, endpoint, fetched_at))

    def review(self, status, at="2026-09-19T12:00:00+00:00"):
        review = {"source": "FINMIND_NEWS", "license_status": status, "terms_url": TERMS_URL,
                  "usage_scope": "competition_research", "reviewed_at": at, "evidence": "測試核對"}
        terms = [{"terms_url": TERMS_URL, "available_at": at, "raw_text": TERMS_TEXT,
                  "raw_sha256": canonical_sha256(TERMS_TEXT)}]
        MarketNewsRepository(self.path).save_source_reviews([review], terms, at, registry=SOURCES, table=REVIEW_TABLE)

    def attach(self, pack):
        bundle = copy.deepcopy(self.bundle)
        bundle["company_news_input"] = pack
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        return bundle


class CompanyNewsInputTests(NewsDatabaseCase):
    def test_unapproved_late_or_restricted_review_adds_nothing(self):
        self.assertIsNone(build_company_news_input(self.path, self.snapshot))
        self.review("approved", at="2026-09-20T02:00:00+00:00")
        self.assertIsNone(build_company_news_input(self.path, self.snapshot))
        self.review("approved")
        self.assertIsNotNone(build_company_news_input(self.path, self.snapshot))
        self.review("restricted", at="2026-09-19T13:00:00+00:00")
        self.assertIsNone(build_company_news_input(self.path, self.snapshot))

    def test_window_whitelist_and_dedupe(self):
        self.review("approved")
        pack = build_company_news_input(self.path, self.snapshot)
        self.assertEqual(pack["latest_fetch_at"], "2026-09-19T15:00:00+00:00")
        self.assertEqual(pack["window_start"], "2026-09-19T03:00:00+00:00")
        self.assertEqual([(item["symbol"], item["candidate"]["title"]) for item in pack["items"]],
                         [("2317.TW", "鴻海接單"), ("2330.TW", "台積電擴產")])
        self.assertEqual(validate_pack(pack, self.bundle["snapshot_id"], CUTOFF, ["2317.TW", "2330.TW"]), pack)
        self.assertEqual(DecisionInputValidator(self.attach(pack)).validate(), [])

    def test_refetch_without_new_content_drops_previous_batch(self):
        """隔天再抓只拿到重複內容（候選去重不新增）時，最新批次是那次抓取，前一批舊聞不得再進資料包。"""
        self.review("approved")
        cutoff = "2026-09-21T13:00:00+00:00"
        snapshot = dict(self.snapshot, decision_cutoff=cutoff)
        before = build_company_news_input(self.path, snapshot)
        # 沒有新抓取時，最後一批停在 09-20 01:00，前兩晚的新聞都還在視窗內。
        self.assertEqual(before["latest_fetch_at"], "2026-09-20T01:00:00+00:00")
        self.assertEqual(len(before["items"]), 3)
        with sqlite3.connect(self.path) as db:
            # 09-21 晚間重抓：回應全是已存在的內容，候選表不新增；交易池外與截止後的抓取不算。
            self.fetch(db, "2317", "2026-09-21T12:00:00+00:00")
            self.fetch(db, "9999", "2026-09-21T12:30:00+00:00")
            self.fetch(db, "2330", "2026-09-21T14:00:00+00:00")
        pack = build_company_news_input(self.path, snapshot)
        self.assertEqual(pack["latest_fetch_at"], "2026-09-21T12:00:00+00:00")
        self.assertEqual(pack["window_start"], "2026-09-21T00:00:00+00:00")
        self.assertEqual(pack["items"], [])
        self.assertEqual(validate_pack(pack, self.bundle["snapshot_id"], cutoff, ["2317.TW", "2330.TW"]), pack)

    def test_empty_latest_batch_gives_no_items(self):
        """最後一批兩檔都是空回應時，資料包沒有新聞，不拿前一晚的舊聞補。"""
        self.review("approved")
        cutoff = "2026-09-21T13:00:00+00:00"
        with sqlite3.connect(self.path) as db:
            for stock_id in ("2317", "2330"):
                self.fetch(db, stock_id, "2026-09-21T12:00:00+00:00")
        pack = build_company_news_input(self.path, dict(self.snapshot, decision_cutoff=cutoff))
        self.assertEqual(pack["latest_fetch_at"], "2026-09-21T12:00:00+00:00")
        self.assertEqual(pack["items"], [])
        self.assertEqual(validate_pack(pack, self.bundle["snapshot_id"], cutoff, ["2317.TW", "2330.TW"]), pack)

    def test_no_fetch_record_gives_empty_window(self):
        """有候選但查不到任何抓取紀錄時視窗為空（fail-closed），不改用候選時間推算。"""
        self.review("approved")
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM raw_payloads")
        pack = build_company_news_input(self.path, self.snapshot)
        self.assertIsNone(pack["latest_fetch_at"])
        self.assertEqual(pack["items"], [])

    def test_tampered_pack_rejected(self):
        self.review("approved")
        pack = build_company_news_input(self.path, self.snapshot)
        cases = []
        title = copy.deepcopy(pack)
        title["items"][0]["candidate"]["title"] = "改寫標題"
        cases.append(title)
        late = copy.deepcopy(pack)
        late["items"][0]["available_at"] = "2026-09-20T01:00:00+00:00"
        cases.append(late)
        unapproved = copy.deepcopy(pack)
        unapproved["source_review"]["license_status"] = "unverified"
        cases.append(unapproved)
        window = copy.deepcopy(pack)
        window["window_start"] = "2026-09-17T00:55:00+00:00"
        cases.append(window)
        foreign = copy.deepcopy(pack)
        foreign["items"][0]["symbol"] = "2330.TW"
        cases.append(foreign)
        for case in cases:
            with self.assertRaises(CompanyNewsError):
                validate_pack(seal(case), self.bundle["snapshot_id"], CUTOFF, ["2317.TW", "2330.TW"])
        errors = DecisionInputValidator(self.attach(seal(title))).validate()
        self.assertTrue(any("個股新聞輸入" in error for error in errors), errors)


class EventAnalystNewsTests(NewsDatabaseCase):
    def setUp(self):
        super().setUp()
        self.review("approved")
        self.news_bundle = self.attach(build_company_news_input(self.path, self.snapshot))
        self.news_id = self.news_bundle["company_news_input"]["items"][0]["evidence_id"]
        momentum = MomentumEngine(self.news_bundle).run()
        self.brief = build_analyst_brief(self.news_bundle, momentum, "event", event_market_scope=True)

    def items(self, outlook="positive", events=None, evidence=None):
        news_2330 = self.news_bundle["company_news_input"]["items"][1]["evidence_id"]
        return [
            {"symbol": "2317.TW", "outlook": outlook, "event_outlook": outlook,
             "findings": [{"finding_id": "2317.TW:f1", "text": "依新聞標題，接單消息偏正面。",
                           "evidence_ids": [evidence or self.news_id]}],
             "data_gaps": ["新聞只有標題"],
             "events": events if events is not None else [
                 {"evidence_id": self.news_id, "materiality": "medium", "summary": "依新聞標題：接單。"}]},
            {"symbol": "2330.TW", "outlook": "neutral", "event_outlook": "neutral",
             "findings": [{"finding_id": "2330.TW:f1", "text": "依新聞標題，擴產。", "evidence_ids": [news_2330]}],
             "data_gaps": ["新聞只有標題"],
             "events": [{"evidence_id": news_2330, "materiality": "low", "summary": "依新聞標題：擴產。"}]},
        ]

    def test_brief_carries_titles_only_and_version(self):
        self.assertEqual(self.brief["report_envelope"]["schema_version"], EVENT_NEWS_VERSION)
        row = next(row for row in self.brief["symbols"] if row["symbol"] == "2317.TW")
        self.assertEqual(row["events"], [])
        self.assertEqual(set(row["news"][0]), {"evidence_id", "title", "publisher", "available_at"})

    def test_news_only_symbol_can_form_outlook_when_every_news_classified(self):
        report = seal_event_market_report(self.news_bundle, self.brief["report_envelope"], self.items())
        self.assertEqual(report["schema_version"], EVENT_NEWS_VERSION)
        self.assertEqual(AnalystReportValidator(self.news_bundle, "event").validate(report), [])
        self.assertEqual(list(analyst_report_names({"technical": {}, "fundamental": {}, "event": report})),
                         ["technical", "fundamental", "event"])

    def test_unclassified_foreign_or_wrong_version_rejected(self):
        validator = AnalystReportValidator(self.news_bundle, "event")
        missing = seal_event_market_report(self.news_bundle, self.brief["report_envelope"], self.items(events=[]))
        self.assertTrue(any("未分級全部重大訊息與新聞" in error for error in validator.validate(missing)))
        other = self.news_bundle["company_news_input"]["items"][1]["evidence_id"]
        foreign = seal_event_market_report(self.news_bundle, self.brief["report_envelope"], self.items(evidence=other))
        self.assertTrue(any("不屬於" in error or "只能引用" in error for error in validator.validate(foreign)))
        old = seal_event_market_report(self.bundle, {**self.brief["report_envelope"]}, self.items())
        self.assertEqual(old["schema_version"], EVENT_MARKET_VERSION)
        self.assertTrue(any("schema_version" in error for error in validator.validate(old)))

    def test_stale_fetch_gives_empty_window(self):
        from etf_agent.perception.company_news.tools import build_pack
        review = self.news_bundle["company_news_input"]["source_review"]
        stale = [dict(ROWS[0], available_at="2026-09-15T14:00:00+00:00")]
        pack = build_pack(stale, review, ["2330.TW"], "snapshot-decision-1", CUTOFF)
        self.assertEqual(pack["items"], [])
        self.assertEqual(validate_pack(pack, "snapshot-decision-1", CUTOFF, ["2330.TW"]), pack)

    def test_no_news_no_event_stays_unknown(self):
        item = no_event_item("2317.TW", [], news_checked=True)
        self.assertEqual(item["outlook"], "unknown")
        self.assertIn("核准媒體的個股新聞", item["data_gaps"][0])


if __name__ == "__main__":
    unittest.main()
