"""只讀官方提供的 RSS，不擷取或轉載新聞全文。"""
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit
from etf_agent.core import canonical_sha256
from .contracts import SOURCES, NEWS_SOURCES, MarketNewsError

class MarketNewsRSSProvider:
    def __init__(self, opener=None):
        self.opener = opener or urllib.request.urlopen

    def capture(self, source):
        if source not in SOURCES:
            raise MarketNewsError('不支援的市場新聞來源')
        url = SOURCES[source]['url']
        raw, status, error = '', 'fetched', ''
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'ETF-Agent/0.1', 'Accept':'application/rss+xml'})
            with self.opener(request, timeout=20) as response:
                if urlsplit(response.geturl()).hostname != urlsplit(url).hostname:
                    raise MarketNewsError('新聞來源轉向未允許的主機')
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise MarketNewsError('新聞回應超過大小限制')
                raw = body.decode('utf-8-sig')
        except (OSError, ValueError) as exc:
            status, error = 'failed', str(exc)
        return {'source':source,'url':url,'raw_text':raw,'raw_sha256':canonical_sha256(raw),
                'available_at':datetime.now(timezone.utc).isoformat(),'status':status,'error':error}

    def capture_terms(self, terms_url):
        """保存來源使用規範原文作為核對依據；只接受已登錄來源的規範網址。"""
        if terms_url not in {item['terms_url'] for item in NEWS_SOURCES.values()}:
            raise MarketNewsError('不支援的來源規範網址')
        request = urllib.request.Request(terms_url, headers={'User-Agent': 'ETF-Agent/0.1'})
        with self.opener(request, timeout=20) as response:
            if urlsplit(response.geturl()).hostname != urlsplit(terms_url).hostname:
                raise MarketNewsError('來源規範轉向未允許的主機')
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise MarketNewsError('來源規範回應超過大小限制')
        raw = body.decode('utf-8-sig', errors='replace')
        if not raw.strip():
            raise MarketNewsError('來源規範內容為空')
        return {'terms_url':terms_url,'raw_text':raw,'raw_sha256':canonical_sha256(raw),
                'available_at':datetime.now(timezone.utc).isoformat()}
