"""全市場 RSS 研究契約；使用權未確認的內容只供診斷。"""
from etf_agent.core import canonical_sha256

class MarketNewsError(ValueError):
    """市場新聞未通過來源、時間或覆蓋檢查。"""

SOURCES = {
    'CNA_FINANCE': {'url': 'https://feeds.feedburner.com/rsscna/finance',
                    'terms_url': 'https://www.cna.com.tw/about/rss.aspx',
                    'publisher': '中央通訊社', 'hosts': {'www.cna.com.tw'}},
    'LTN_BUSINESS': {'url': 'https://news.ltn.com.tw/rss/business.xml',
                     'terms_url': 'https://service.ltn.com.tw/RSS',
                     'publisher': '自由時報', 'hosts': {'ec.ltn.com.tw', 'news.ltn.com.tw'}},
    # Yahoo股市 RSS 只留約 4～5 小時、偶有無標題項目（略過）；條款：個人與非營利、非商業用途免費，須標示「Yahoo股市」。
    'YAHOO_TW_MARKET': {'url': 'https://tw.stock.yahoo.com/rss?category=tw-market',
                        'terms_url': 'https://tw.stock.yahoo.com/rss-help',
                        'publisher': 'Yahoo股市', 'hosts': {'tw.stock.yahoo.com', 'tw.news.yahoo.com'}, 'skip_untitled': True},
    'YAHOO_INTL_MARKETS': {'url': 'https://tw.stock.yahoo.com/rss?category=intl-markets',
                           'terms_url': 'https://tw.stock.yahoo.com/rss-help',
                           'publisher': 'Yahoo股市', 'hosts': {'tw.stock.yahoo.com', 'tw.news.yahoo.com'}, 'skip_untitled': True},
}

ARCHIVE_SOURCES = {
    'CNA_ARCHIVE': {'publisher': '中央通訊社', 'hosts': {'www.cna.com.tw'},
                    'path_pattern': r'/news/[a-z]+/\d{12}\.aspx',
                    'terms_url': 'https://www.cna.com.tw/about/rss.aspx'},
    'LTN_ARCHIVE': {'publisher': '自由時報', 'hosts': {'ec.ltn.com.tw'},
                    'path_pattern': r'/article/(?:breakingnews|paper)/\d+',
                    'terms_url': 'https://service.ltn.com.tw/RSS'},
}
NEWS_SOURCES = {**SOURCES, **ARCHIVE_SOURCES}

def seal(payload, field='content_sha256'):
    body = {k:v for k,v in payload.items() if k != field}
    return dict(body, **{field: canonical_sha256(body)})

def check_fields(value, allowed, name):
    if not isinstance(value, dict) or set(value) != set(allowed):
        raise MarketNewsError(name + ' 欄位不符合契約')
