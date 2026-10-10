import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.core import canonical_sha256
from etf_agent.decision.analysts import (
    AnalystReportValidator, build_analyst_brief, report_envelope,
    seal_event_market_report, seal_report, analyst_report_names,
)
from etf_agent.decision.event_sentiment_report import render_event_sentiment_report
from etf_agent.decision.contracts import DecisionToolError
from etf_agent.data.market_probe.contracts import MarketProbeError, SOURCES
from etf_agent.data.market_probe.tools import market_context_facts
from etf_agent.data.market_probe.service import capture_market_context
from test_analysts import bundle_with_event, event_items


def report(bundle):
    rows = [dict(row, event_outlook=row['outlook']) for row in event_items()]
    return seal_event_market_report(bundle, report_envelope(bundle, 'event', 'market-test'), rows)


class EventMarketScopeTests(unittest.TestCase):
    def test_market_once_and_company_event_keeps_own_direction(self):
        bundle = bundle_with_event()
        output = report(bundle)
        self.assertEqual(AnalystReportValidator(bundle, 'event').validate(output), [])
        self.assertEqual(output['schema_version'], '2.2')
        self.assertEqual(output['market_sentiment']['outlook'], 'unknown')
        self.assertEqual(output['items'][1]['outlook'], 'positive')
        self.assertTrue(all('sentiment' not in row for row in output['items']))
        text = render_event_sentiment_report(bundle, output)
        self.assertEqual(text.count('## 整體市場情緒'), 1)
        self.assertIn('資料不足，無法判斷', text)

    def test_brief_does_not_treat_company_sentiment_as_whole_market(self):
        bundle = bundle_with_event()
        brief = build_analyst_brief(bundle, {}, 'event', event_market_scope=True)
        self.assertEqual(brief['report_envelope']['schema_version'], '2.2')
        self.assertIn('market_sentiment', brief)
        self.assertTrue(all('sentiment' not in row for row in brief['symbols']))

    def test_both_stock_researchers_receive_same_market_channel_once(self):
        from etf_agent.decision import MomentumEngine, build_stance_brief
        from test_analysts import technical_items, report as analyst_report
        bundle = bundle_with_event()
        reports = {
            'technical': analyst_report(bundle, 'technical', technical_items()),
            'fundamental': analyst_report(bundle, 'fundamental', [
                {'symbol': row['symbol'], 'outlook': 'unknown', 'findings': [], 'data_gaps': ['缺少財報']}
                for row in technical_items()
            ]),
            'event': report(bundle),
        }
        momentum = MomentumEngine(bundle).run()
        bull = build_stance_brief(bundle, momentum, reports, {}, 'bull')
        bear = build_stance_brief(bundle, momentum, reports, {}, 'bear')
        self.assertEqual(bull['market_sentiment'], reports['event']['market_sentiment'])
        self.assertEqual(bull['market_sentiment'], bear['market_sentiment'])
        self.assertEqual(bull['packet_envelope']['shared_input_sha256'], bear['packet_envelope']['shared_input_sha256'])
        self.assertEqual(bull['market_sentiment']['status'], 'unavailable')
        self.assertEqual(bull['market_sentiment']['findings'], [])
        self.assertTrue(all('market_sentiment' not in row and 'sentiment' not in row for row in bull['symbols']))
        self.assertEqual(bull['symbols'][1]['analysts']['event']['outlook'], 'positive')

    def test_changed_market_even_when_rehashed_rejected(self):
        bundle = bundle_with_event()
        for field, value in [('outlook', 'neutral'), ('snapshot_id', 'wrong'), ('decision_cutoff', '2026-10-01T00:00:00+00:00')]:
            output = report(bundle)
            output['market_sentiment'][field] = value
            output['content_sha256'] = canonical_sha256({k:v for k,v in output.items() if k != 'content_sha256'})
            self.assertTrue(AnalystReportValidator(bundle, 'event').validate(output))
            with self.assertRaises(DecisionToolError): render_event_sentiment_report(bundle, output)

    def test_company_cannot_include_sentiment_or_use_price_evidence(self):
        bundle = bundle_with_event()
        output = report(bundle)
        rows = copy.deepcopy(output['items'])
        rows[1]['sentiment'] = {}
        with self.assertRaises(DecisionToolError): seal_event_market_report(bundle, report_envelope(bundle,'event','x'),rows)
        output['items'][1]['findings'][0]['evidence_ids'] = ['price-2330']
        output['content_sha256'] = canonical_sha256({k:v for k,v in output.items() if k != 'content_sha256'})
        self.assertTrue(AnalystReportValidator(bundle,'event').validate(output))

    def test_company_result_cannot_disagree_with_event_result(self):
        bundle = bundle_with_event()
        output = report(bundle)
        output['items'][1]['outlook'] = 'negative'
        output['content_sha256'] = canonical_sha256({k:v for k,v in output.items() if k != 'content_sha256'})
        self.assertTrue(any('公司結果' in e for e in AnalystReportValidator(bundle,'event').validate(output)))

    def test_approved_company_perception_does_not_promote_market_channel(self):
        from etf_agent.perception import MarketPerceptionApplicationService, PerceptionDataTools
        from etf_agent.decision.contracts import decision_bundle_sha256
        from test_sentiment_analyst_agent import perception_bundle, sentiment_labels
        bundle = bundle_with_event()
        data = perception_bundle()
        data['snapshot_id'] = bundle['snapshot_id']
        data['decision_cutoff'] = bundle['decision_cutoff']
        result = MarketPerceptionApplicationService(PerceptionDataTools(data)).build_result(
            ['2330.TW','2317.TW'], sentiment_labels(), 'market-scope-test')
        bundle['perception_inputs'] = [{'bundle':data,'result':result}]
        bundle['bundle_sha256'] = decision_bundle_sha256(bundle)
        output = report(bundle)
        self.assertEqual(AnalystReportValidator(bundle,'event').validate(output), [])
        self.assertEqual(output['market_sentiment']['outlook'],'unknown')
        self.assertEqual(output['market_sentiment']['findings'],[])


class MarketProbeTests(unittest.TestCase):
    def capture(self):
        raw = json.dumps([{'Date':'1151002','TAIEX':'48475.74','Change':'122.25','TradeValue':'938222196327'}])
        return {'source':'TWSE_FMTQIK','url':SOURCES['TWSE_FMTQIK'], 'requested_trade_date':'2026-10-02',
                'status':'fetched','raw_text':raw,'raw_sha256':canonical_sha256(raw),
                'available_at':'2026-10-03T09:00:00+00:00','error':'','license_status':'unverified'}

    def test_actual_shape_parsed_without_sentiment_direction(self):
        output=market_context_facts([self.capture()], '2026-10-03T10:00:00+00:00')
        self.assertEqual(output['facts'][0]['values']['加權指數'], '48475.74')
        self.assertFalse(output['formal_sentiment_available'])
        self.assertNotIn('outlook',output)

    def test_time_hash_duplicate_and_source_rejected(self):
        for mode in ('future','naive','hash','source','url'):
            capture=self.capture()
            if mode=='future':capture['available_at']='2026-10-04T00:00:00+00:00'
            elif mode=='naive':capture['available_at']='2026-10-03T09:00:00'
            elif mode=='hash':capture['raw_text']+='changed'
            elif mode=='source':capture['source']='OTHER'
            else:capture['url']='https://example.org'
            with self.assertRaises(MarketProbeError):market_context_facts([capture],'2026-10-03T10:00:00+00:00')
        with self.assertRaises(MarketProbeError):market_context_facts([self.capture(),self.capture()],'2026-10-03T10:00:00+00:00')

    def test_future_content_and_nonfinite_values_do_not_produce_partial_facts(self):
        for field,value in [('Date','1151004'),('TAIEX','NaN')]:
            capture=self.capture();rows=json.loads(capture['raw_text']);rows[0][field]=value
            capture['raw_text']=json.dumps(rows);capture['raw_sha256']=canonical_sha256(capture['raw_text'])
            output=market_context_facts([capture],'2026-10-03T10:00:00+00:00')
            self.assertEqual(output['facts'],[])
            self.assertEqual(len(output['failures']),1)

    def test_failed_sources_saved_without_fake_zero_or_market_label(self):
        capture=self.capture();capture.update(status='failed',error='HTTP 403',raw_text='',raw_sha256=canonical_sha256(''))
        output=market_context_facts([capture],'2026-10-03T10:00:00+00:00')
        self.assertEqual(output['facts'],[])
        self.assertIn('HTTP 403',output['failures'][0]['reason'])

    def test_capture_service_verifies_saved_originals(self):
        test=self
        class Provider:
            def capture(self, source, trade_date):
                item=test.capture()
                item.update(source=source,url=SOURCES[source].format(date='20261002'))
                if source!='TWSE_FMTQIK':item.update(status='failed',error='fixture unavailable')
                return item
        with tempfile.TemporaryDirectory() as directory:
            output=capture_market_context('2026-10-02',Path(directory),Provider())
            self.assertEqual(output['facts'],1)
            self.assertEqual(len(output['failures']),2)
            self.assertEqual(len(json.loads((Path(output['run_dir'])/'captures.json').read_text())['items']),3)
