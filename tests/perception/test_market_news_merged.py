import copy
import unittest
from etf_agent.core import canonical_sha256
from etf_agent.perception.market_news import seal, prepare, evaluate
from etf_agent.perception.market_news.contracts import SOURCES, MarketNewsError

CUTOFF = '2026-11-16T00:00:00+00:00'
START = '2026-11-13T00:00:00+00:00'


def capture(source, links, available_at, status='fetched'):
    config = SOURCES[source]
    host = sorted(config['hosts'])[0]
    raw = '<rss><channel>' + ''.join(
        '<item><title>%s新聞%d</title><description>市場背景</description><link>https://%s/news/%d</link><pubDate>Sat, 14 Nov 2026 02:00:00 +0000</pubDate></item>' % (source, n, host, n)
        for n in links) + '</channel></rss>'
    if status == 'failed':
        raw = ''
    return {'source': source, 'url': config['url'], 'raw_text': raw, 'raw_sha256': canonical_sha256(raw),
            'available_at': available_at, 'status': status, 'error': '' if status == 'fetched' else 'x'}


def merged_pack(captures, schema='market-news-1.2'):
    prepared = prepare(captures, START, CUTOFF, merge=True)
    labels = [{'item_id': r['item_id'], 'canonical_content_id': r['canonical_content_id'], 'relevance': 'market',
               'stance': 'neutral', 'reason': '測試'} for r in prepared['items']]
    return seal({'schema_version': schema, 'snapshot_id': 's', 'decision_cutoff': CUTOFF, 'window_start': START,
                 'captures': captures, 'labels': labels,
                 'synthesis': {'outlook': 'neutral', 'findings': [{'text': '測試', 'evidence_ids': [labels[0]['item_id']]}]},
                 'source_approvals': [{'source': s, 'license_status': 'approved', 'terms_url': c['terms_url'],
                                       'usage_scope': 'competition_research', 'reviewed_at': '2026-11-15T00:00:00+00:00',
                                       'evidence': '測試核准'} for s, c in SOURCES.items()]})


class MergedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.captures = []
        for source in sorted(SOURCES):
            # 兩次抓取：舊抓取有 0~2，新抓取只剩 2~4（0、1 已被 RSS 擠掉）。
            self.captures.append(capture(source, [0, 1, 2], '2026-11-14T03:00:00+00:00'))
            self.captures.append(capture(source, [2, 3, 4], '2026-11-15T03:00:00+00:00'))

    def test_merged_pack_keeps_items_dropped_from_later_rss_and_dedupes(self):
        prepared = prepare(self.captures, START, CUTOFF, merge=True)
        self.assertEqual(len(prepared['items']), 5 * len(SOURCES))
        shared = [r for r in prepared['items'] if r['link'].endswith('/news/2')]
        self.assertTrue(all(r['available_at'] == '2026-11-14T03:00:00+00:00' for r in shared))
        result = evaluate(merged_pack(self.captures))
        self.assertEqual(result['item_count'], 5 * len(SOURCES))

    def test_old_schema_still_rejects_duplicate_source_captures(self):
        with self.assertRaises(MarketNewsError):
            prepare(self.captures, START, CUTOFF)
        with self.assertRaises(MarketNewsError):
            evaluate(merged_pack(self.captures, schema='market-news-1.1'))

    def test_capture_after_cutoff_is_rejected(self):
        late = copy.deepcopy(self.captures)
        late.append(capture('CNA_FINANCE', [9], '2026-11-16T01:00:00+00:00'))
        with self.assertRaises(MarketNewsError):
            prepare(late, START, CUTOFF, merge=True)

    def test_failed_capture_is_gap_only_when_source_never_fetched(self):
        mixed = self.captures + [capture('CNA_FINANCE', [], '2026-11-15T09:00:00+00:00', status='failed')]
        self.assertEqual(prepare(mixed, START, CUTOFF, merge=True)['data_gaps'], [])
        only_failed = [capture('CNA_FINANCE', [], '2026-11-15T09:00:00+00:00', status='failed')]
        self.assertEqual(len(prepare(only_failed, START, CUTOFF, merge=True)['data_gaps']), 1)

    def test_tampered_capture_is_rejected(self):
        bad = copy.deepcopy(self.captures)
        bad[0]['raw_text'] += ' '
        with self.assertRaises(MarketNewsError):
            prepare(bad, START, CUTOFF, merge=True)


class YahooSourceTests(unittest.TestCase):
    def yahoo_capture(self, items_xml):
        raw = '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>Yahoo股市</title>' + items_xml + '</channel></rss>'
        config = SOURCES['YAHOO_TW_MARKET']
        return {'source': 'YAHOO_TW_MARKET', 'url': config['url'], 'raw_text': raw, 'raw_sha256': canonical_sha256(raw),
                'available_at': '2026-11-15T03:00:00+00:00', 'status': 'fetched', 'error': ''}

    def test_untitled_yahoo_item_is_skipped_not_fatal(self):
        xml = ('<item><title/><link>https://tw.news.yahoo.com/a-1.html</link><pubDate>Sat, 14 Nov 2026 02:00:00 GMT</pubDate></item>'
               '<item><title><![CDATA[費半重挫3.43% 台積電ADR跌]]></title><link>https://tw.stock.yahoo.com/news/b-2.html</link>'
               '<pubDate>Sat, 14 Nov 2026 03:00:00 GMT</pubDate><description><![CDATA[美股收黑]]></description></item>')
        prepared = prepare([self.yahoo_capture(xml)], START, CUTOFF, merge=True)
        self.assertEqual([r['title'] for r in prepared['items']], ['費半重挫3.43% 台積電ADR跌'])
        self.assertEqual(prepared['items'][0]['publisher'], 'Yahoo股市')

    def test_untitled_item_still_rejected_for_sources_without_skip_flag(self):
        cna = capture('CNA_FINANCE', [0], '2026-11-15T03:00:00+00:00')
        raw = cna['raw_text'].replace('CNA_FINANCE新聞0', '')
        cna = dict(cna, raw_text=raw, raw_sha256=canonical_sha256(raw))
        with self.assertRaises(MarketNewsError):
            prepare([cna], START, CUTOFF, merge=True)

    def test_yahoo_link_on_other_host_is_rejected(self):
        xml = '<item><title>標題</title><link>https://evil.example.com/x.html</link><pubDate>Sat, 14 Nov 2026 02:00:00 GMT</pubDate></item>'
        with self.assertRaises(MarketNewsError):
            prepare([self.yahoo_capture(xml)], START, CUTOFF, merge=True)


if __name__ == '__main__':
    unittest.main()
