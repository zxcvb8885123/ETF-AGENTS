import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from etf_agent.core import canonical_sha256
from etf_agent.perception.market_news import apply_database_source_reviews, evaluate, seal
from etf_agent.perception.market_news.repository import MarketNewsRepository
from etf_agent.perception.market_news.contracts import MarketNewsError
from etf_agent.decision.event_sentiment_report import _render_market_scoped
from test_market_news import pack


class SourceReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'news.db'
        self.repo = MarketNewsRepository(self.path)
        self.pack = pack()
        self.reviews = copy.deepcopy(self.pack['source_approvals'])
        for r in self.reviews:
            r['license_status'] = 'unverified'
        self.terms = [{'terms_url':r['terms_url'],'available_at':r['reviewed_at'],
                       'raw_text':'人工測試規範，不代表真實核准',
                       'raw_sha256':canonical_sha256('人工測試規範，不代表真實核准')} for r in self.reviews]

    def test_database_restriction_overrides_packet_approval_without_losing_diagnostic(self):
        self.pack = seal(dict(self.pack,schema_version='market-news-1.1'))
        self.repo.save_source_reviews(self.reviews,self.terms,self.pack['decision_cutoff'])
        self.repo.save_source_reviews(self.reviews,self.terms,self.pack['decision_cutoff'])
        updated = apply_database_source_reviews(self.pack,self.path)
        result = evaluate(updated)
        self.assertEqual(result['outlook'],'unknown')
        self.assertEqual(result['diagnostic_outlook'],'positive')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM market_news_source_reviews').fetchone()[0],2)
        text = _render_market_scoped({'decision_cutoff':self.pack['decision_cutoff'],
                                     'market_sentiment':result,'items':[]})
        self.assertIn('來源使用範圍尚未確認，正式結果暫不提供',text)
        self.assertNotIn('資料不足，無法判斷',text)

    def test_saved_after_cutoff_not_backdated(self):
        self.repo.save_source_reviews(self.reviews,self.terms,'2026-09-30T00:00:00+00:00')
        self.assertEqual(self.repo.load_source_reviews(self.pack['decision_cutoff']),[])
        self.assertEqual(apply_database_source_reviews(self.pack,self.path),self.pack)

    def test_missing_or_modified_terms_and_future_review_rejected(self):
        with self.assertRaises(MarketNewsError):self.repo.save_source_reviews(self.reviews,[],self.pack['decision_cutoff'])
        bad = copy.deepcopy(self.terms);bad[0]['raw_text']+='修改'
        with self.assertRaises(MarketNewsError):self.repo.save_source_reviews(self.reviews,bad,self.pack['decision_cutoff'])
        with self.assertRaises(MarketNewsError):self.repo.save_source_reviews(self.reviews,self.terms,'2026-09-28T00:00:00+00:00')
        with self.assertRaises(MarketNewsError):self.repo.load_source_reviews('2026-09-29T10:00:00')

    def test_database_payload_tamper_rejected(self):
        self.repo.save_source_reviews(self.reviews,self.terms,self.pack['decision_cutoff'])
        with sqlite3.connect(self.path) as db:
            row=db.execute('SELECT review_id,payload_json FROM market_news_source_reviews LIMIT 1').fetchone()
            data=json.loads(row[1]);data['review']['license_status']='approved'
            db.execute('UPDATE market_news_source_reviews SET payload_json=? WHERE review_id=?',(json.dumps(data),row[0]))
        with self.assertRaises(MarketNewsError):self.repo.load_source_reviews(self.pack['decision_cutoff'])

    def test_real_evidence_record_is_required_even_for_approved_fixture(self):
        approvals = copy.deepcopy(self.pack['source_approvals'])
        self.repo.save_source_reviews(approvals,self.terms,self.pack['decision_cutoff'])
        self.assertEqual(evaluate(apply_database_source_reviews(self.pack,self.path))['outlook'],'positive')
        bad=copy.deepcopy(approvals);bad[0]['evidence']=''
        with self.assertRaises(MarketNewsError):self.repo.save_source_reviews(bad,self.terms,self.pack['decision_cutoff'])
