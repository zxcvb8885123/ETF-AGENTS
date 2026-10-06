"""只讀官方提供的 RSS，不擷取或轉載新聞全文。"""
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit
from etf_agent.core import canonical_sha256
from .contracts import SOURCES, MarketNewsError

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
