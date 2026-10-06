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
