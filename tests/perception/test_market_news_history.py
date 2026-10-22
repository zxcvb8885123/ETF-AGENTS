import copy
import json
import tempfile
import unittest
from pathlib import Path
from etf_agent.core import canonical_sha256
from etf_agent.perception.market_news import prepare,evaluate,seal,backfill_market_news
from etf_agent.perception.market_news.history import MarketNewsHistoryProvider
from etf_agent.perception.market_news.contracts import MarketNewsError
from test_market_news import pack


def capture(source='LTN_ARCHIVE',published='2026-09-29T08:00:00+00:00'):
    raw='<script type="application/ld+json">'+json.dumps({'@type':'NewsArticle','headline':'全市場背景測試','description':'摘要不是全文','datePublished':published,'articleBody':'本欄內容不應輸出'})+'</script>'
    return {'source':source,'url':'https://ec.ltn.com.tw/article/breakingnews/123',
            'raw_text':raw,'raw_sha256':canonical_sha256(raw),'available_at':'2026-09-29T09:00:00+00:00','status':'fetched','error':''}

class MarketNewsHistoryTests(unittest.TestCase):
    def test_archive_metadata_and_current_available_time(self):
        row=capture();prepared=prepare([row],'2026-09-29T00:00:00+00:00','2026-09-29T10:00:00+00:00')
        item=prepared['items'][0]
        self.assertEqual(item['summary'],'摘要不是全文');self.assertEqual(item['content_extent'],'page_title_and_description')
        self.assertEqual(item['available_at'],row['available_at']);self.assertNotIn('articleBody',item)
        with self.assertRaises(MarketNewsError):prepare([row],'2026-09-29T00:00:00+00:00','2026-09-29T08:30:00+00:00')

    def test_archive_rejects_wrong_host_path_naive_time_and_tamper(self):
        cases=[dict(capture(),url='https://example.com/123'),dict(capture(),url='https://ec.ltn.com.tw/admin'),
               capture(published='2026-09-29T08:00:00'),dict(capture(),raw_sha256='wrong'),
               capture(published='2026-09-30T08:00:00+00:00')]
        for row in cases:
            with self.assertRaises(MarketNewsError):prepare([row],'2026-09-29T00:00:00+00:00','2026-09-29T10:00:00+00:00')

    def test_archive_requires_new_contract_and_does_not_inflate_publishers(self):
        p=pack();p['captures'].append(capture())
        with self.assertRaises(MarketNewsError):evaluate(seal(p))
        p['schema_version']='market-news-1.1'
        items=prepare(p['captures'],p['window_start'],p['decision_cutoff'])['items'];r=items[-1]
        p['labels'].append({'item_id':r['item_id'],'canonical_content_id':r['canonical_content_id'],'relevance':'market','stance':'positive','reason':'測試'})
        result=evaluate(seal(p));self.assertEqual(result['market_source_count'],2)
        self.assertEqual(result['outlook'],'unknown');self.assertEqual(result['diagnostic_outlook'],'positive')
        self.assertFalse(any('尚不能宣稱' in gap for gap in result['data_gaps']))

    def test_backfill_saves_real_capture_clock_and_immutable_artifacts(self):
        class Provider:
            def collect(self,start_date,end_date):return {'captures':[capture()],'ltn_pagination_completed':True,'listings':[]}
        with tempfile.TemporaryDirectory() as directory:
            result=backfill_market_news(Path(directory)/'runs','2026-09-29','2026-09-29',Path(directory)/'data.db',Provider())
            self.assertEqual(result['item_count'],1)
            history=json.loads((Path(result['run_path'])/'history.json').read_text())
            self.assertEqual(history['captures'][0]['available_at'],'2026-09-29T09:00:00+00:00')

    def test_provider_encodes_observed_chinese_pagination_link(self):
        seen=[]
        class Response:
            def __init__(self,url):self.url=url
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def geturl(self):return self.url
            def read(self,size):return b'<html></html>'
        def opener(request,timeout):seen.append(request.full_url);return Response(request.full_url)
        provider=MarketNewsHistoryProvider(opener)
        row=provider._capture('https://search.ltn.com.tw/list?keyword=台股&start_time=20261001&end_time=20261003&sort=date&type=all&page=2')
        self.assertEqual(row['status'],'fetched');self.assertIn('%E5%8F%B0%E8%82%A1',seen[0])
        with self.assertRaises(MarketNewsError):provider._capture('https://example.com/')

    def test_legacy_result_keeps_original_gap_and_new_report_is_scoped(self):
        old=evaluate(pack());self.assertTrue(any('尚不能宣稱' in x for x in old['data_gaps']))
        p=pack();p['schema_version']='market-news-1.1';result=evaluate(seal(p))
        self.assertFalse(any('尚不能宣稱' in x for x in result['data_gaps']))
