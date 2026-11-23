"""market-news-1.3：RSS 單則壞時間只丟該則並記缺口、canonical_content_id 用台北日期；1.2 以前維持整包拒絕（逐位元不變）。"""
import unittest

from etf_agent.core import canonical_sha256
from etf_agent.perception.market_news import prepare
from etf_agent.perception.market_news.contracts import SOURCES, MarketNewsError

START = '2026-11-13T00:00:00+00:00'
CUTOFF = '2026-11-16T00:00:00+00:00'
AVAILABLE = '2026-11-15T03:00:00+00:00'
SOURCE = sorted(SOURCES)[0]


def capture(pub_dates):
    config = SOURCES[SOURCE]
    host = sorted(config['hosts'])[0]
    raw = '<rss><channel>' + ''.join(
        '<item><title>新聞%d</title><description>背景</description><link>https://%s/news/%d</link><pubDate>%s</pubDate></item>'
        % (n, host, n, date) for n, date in enumerate(pub_dates)) + '</channel></rss>'
    return {'source': SOURCE, 'url': config['url'], 'raw_text': raw, 'raw_sha256': canonical_sha256(raw),
            'available_at': AVAILABLE, 'status': 'fetched', 'error': ''}


GOOD = 'Sat, 14 Nov 2026 02:00:00 +0000'
FUTURE = 'Sun, 15 Nov 2026 23:00:00 +0000'  # 晚於取得時間
NO_ZONE = 'Sat, 14 Nov 2026 02:00:00 -0000'  # RFC 2822 的 -0000：沒有時區資訊


class LenientTests(unittest.TestCase):
    def test_old_versions_still_reject_the_whole_pack(self):
        with self.assertRaises(MarketNewsError):
            prepare([capture([GOOD, FUTURE])], START, CUTOFF, merge=True)

    def test_1_3_drops_only_the_bad_item_and_records_a_gap(self):
        prepared = prepare([capture([GOOD, FUTURE, NO_ZONE])], START, CUTOFF, merge=True, lenient=True)
        self.assertEqual(len(prepared['items']), 1)
        self.assertGreaterEqual(len(prepared['item_gaps']), 1)
        self.assertEqual(prepared['data_gaps'], [])  # 不影響「可用」判斷

    def test_content_id_day_is_the_taipei_date_only_in_1_3(self):
        late_utc = 'Sat, 14 Nov 2026 17:00:00 +0000'  # 台北 11-15 01:00
        old = prepare([capture([late_utc])], START, CUTOFF, merge=True)['items'][0]['canonical_content_id']
        new = prepare([capture([late_utc])], START, CUTOFF, merge=True, lenient=True)['items'][0]['canonical_content_id']
        self.assertEqual(old, canonical_sha256({'title': '新聞0', 'day': '2026-11-14'}))
        self.assertEqual(new, canonical_sha256({'title': '新聞0', 'day': '2026-11-15'}))

    def test_old_result_has_no_item_gaps_key(self):
        self.assertNotIn('item_gaps', prepare([capture([GOOD])], START, CUTOFF, merge=True))


if __name__ == '__main__':
    unittest.main()
