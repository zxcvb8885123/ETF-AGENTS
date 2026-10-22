import copy
import json
import tempfile
import unittest
from pathlib import Path
from etf_agent.core import canonical_sha256
from etf_agent.perception.market_news import seal,prepare,evaluate,validate_pair,attach_market_news
from etf_agent.perception.market_news.contracts import SOURCES,MarketNewsError
from etf_agent.perception.market_news.repository import MarketNewsRepository
from etf_agent.decision.analysts import seal_event_market_report,report_envelope,AnalystReportValidator
from etf_agent.decision.contracts import DecisionInputValidator
from test_analysts import bundle_with_event,event_items


def pack(snapshot_id='test',cutoff='2026-09-29T10:00:00+00:00'):
    # 僅供契約驗證的人工 RSS，核准狀態不是實際來源授權。
    captures=[]
    for source,config in SOURCES.items():
        host=sorted(config['hosts'])[0]
        raw='<rss><channel>'+''.join('<item><title>%s市場新聞%d</title><description>市場背景</description><link>https://%s/news/%d</link><pubDate>Tue, 29 Sep 2026 08:00:00 +0000</pubDate></item>'%(source,n,host,n) for n in range(3))+'</channel></rss>'
        captures.append({'source':source,'url':config['url'],'raw_text':raw,'raw_sha256':canonical_sha256(raw),
                         'available_at':'2026-09-29T09:00:00+00:00','status':'fetched','error':''})
    prepared=prepare(captures,'2026-09-29T00:00:00+00:00',cutoff)
    labels=[{'item_id':r['item_id'],'canonical_content_id':r['canonical_content_id'],'relevance':'market','stance':'positive','reason':'人工測試判讀'} for r in prepared['items']]
    return seal({'schema_version':'market-news-1.0','snapshot_id':snapshot_id,'decision_cutoff':cutoff,
                 'window_start':'2026-09-29T00:00:00+00:00','captures':captures,'labels':labels,
                 'synthesis':{'outlook':'positive','findings':[{'text':'人工測試市場看法','evidence_ids':[labels[0]['item_id']]}]},
                 'source_approvals':[{'source':s,'license_status':'approved','terms_url':c['terms_url'],'usage_scope':'competition_research',
                                     'reviewed_at':'2026-09-29T09:00:00+00:00','evidence':'人工測試核准，非真實授權'} for s,c in SOURCES.items()]})

class MarketNewsTests(unittest.TestCase):
    def test_approved_global_input_is_rebuilt_and_attached_once(self):
        bundle=bundle_with_event()
        # 使用 fixture 的真實 cutoff 前，將 RSS 時間整体移至更早的 fixture 日。
        p=pack();old=p['decision_cutoff'];new=bundle['decision_cutoff']
        day=new[:10]
        text=json.dumps(p).replace('2026-09-29',day)
        p=json.loads(text);p['snapshot_id']=bundle['snapshot_id'];p['decision_cutoff']=new
        # fixture cutoff 必須晚於 RSS 取得時間；若該 fixture 為早上，提前來源到前一天。
        p['captures']=[dict(c,available_at=day+'T00:00:00+00:00',raw_text=c['raw_text'].replace('29 Sep 2026 08:00:00','28 Sep 2026 00:00:00')) for c in p['captures']]
        # 本測試改用獨立 bundle cutoff，所有 Snapshot 時間保持原值會被完整 validator 拒絕；
        # 這裡透過已存在的 fixture 時鐘定位合法資料。
        from datetime import datetime,timedelta
        from etf_agent.core import parse_aware_time
        cutoff=parse_aware_time(new,'cutoff'); prior=cutoff-timedelta(hours=1)
        for c in p['captures']:
            import email.utils
            pub=email.utils.format_datetime(prior-timedelta(hours=1))
            import re
            c['raw_text']=re.sub('<pubDate>.*?</pubDate>','<pubDate>'+pub+'</pubDate>',c['raw_text'])
            c['available_at']=prior.isoformat();c['raw_sha256']=canonical_sha256(c['raw_text'])
        p['window_start']=(prior-timedelta(days=1)).isoformat()
        prepared=prepare(p['captures'],p['window_start'],new)
        p['labels']=[dict(l,canonical_content_id=r['canonical_content_id']) for l,r in zip(p['labels'],prepared['items'])]
        for a in p['source_approvals']:a['reviewed_at']=prior.isoformat()
        p=seal(p);result=evaluate(p)
        self.assertEqual(result['outlook'],'positive')
        bundle=attach_market_news(bundle,{'bundle':p,'result':result})
        self.assertEqual(DecisionInputValidator(bundle).validate(),[])
        rows=[dict(r,event_outlook=r['outlook']) for r in event_items()]
        report=seal_event_market_report(bundle,report_envelope(bundle,'event','global-news-test'),rows)
        self.assertEqual(report['market_sentiment']['outlook'],'positive')
        self.assertEqual(AnalystReportValidator(bundle,'event').validate(report),[])

    def test_unverified_sources_keep_diagnostic_but_never_formal_direction(self):
        p=pack()
        for a in p['source_approvals']:a['license_status']='unverified'
        result=evaluate(seal(p))
        self.assertEqual(result['outlook'],'unknown');self.assertEqual(result['findings'],[])
        self.assertEqual(result['diagnostic_outlook'],'positive')

    def test_future_capture_publication_and_naive_time_rejected(self):
        for mutation in ('future_capture','future_publication','naive'):
            p=pack();c=p['captures'][0]
            if mutation=='future_capture':c['available_at']='2026-09-30T09:00:00+00:00'
            if mutation=='future_publication':c['raw_text']=c['raw_text'].replace('29 Sep','30 Sep');c['raw_sha256']=canonical_sha256(c['raw_text'])
            if mutation=='naive':c['available_at']='2026-09-29T09:00:00'
            with self.assertRaises(MarketNewsError):evaluate(seal(p))

    def test_missing_label_bad_source_and_rehashed_result_rejected(self):
        p=pack();p['labels'].pop()
        with self.assertRaises(MarketNewsError):evaluate(seal(p))
        p=pack();p['captures'][0]['url']='https://example.org/'
        with self.assertRaises(MarketNewsError):evaluate(seal(p))
        p=pack();result=evaluate(p);result['outlook']='negative';result=seal(result)
        with self.assertRaises(MarketNewsError):validate_pair({'bundle':p,'result':result},p['snapshot_id'],p['decision_cutoff'])

    def test_company_news_cannot_support_whole_market_and_sample_shortfall(self):
        p=pack();p['labels'][0]['relevance']='company'
        with self.assertRaises(MarketNewsError):evaluate(seal(p))
        p['synthesis']['findings'][0]['evidence_ids']=[p['labels'][1]['item_id']]
        for row in p['labels'][3:]:row['relevance']='company'
        self.assertEqual(evaluate(seal(p))['status'],'unavailable')

    def test_raw_tamper_xml_entity_and_duplicate_content_conflicts(self):
        p=pack();p['captures'][0]['raw_text']+=' '
        with self.assertRaises(MarketNewsError):evaluate(seal(p))
        p=pack();c=p['captures'][0];c['raw_text']='<!DOCTYPE rss>'+c['raw_text'];c['raw_sha256']=canonical_sha256(c['raw_text'])
        with self.assertRaises(MarketNewsError):evaluate(seal(p))
        p=pack();p['source_approvals'][0]['usage_scope']='personal_reading'
        with self.assertRaises(MarketNewsError):evaluate(seal(p))

    def test_repository_appends_versions_without_duplicate_write(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'news.db';repo=MarketNewsRepository(path)
            captures=pack()['captures'];repo.save_captures(captures);repo.save_captures(captures)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM market_news_captures').fetchone()[0],2)
            rows=copy.deepcopy(captures);rows[0]['available_at']='2026-09-29T09:01:00+00:00';repo.save_captures(rows)
            with sqlite3.connect(path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM market_news_captures').fetchone()[0],3)
