"""有界讀取公開證交所原始回應，不修改來源核准狀態。"""

import json
import urllib.request
from datetime import datetime, timezone, date
from urllib.parse import urlsplit

from etf_agent.core import canonical_sha256
from .contracts import SOURCES, MarketProbeError


class OfficialMarketProbeProvider:
    def __init__(self, opener=None):
        self.opener = opener or urllib.request.urlopen

    def capture(self, source, trade_date):
        if source not in SOURCES:
            raise MarketProbeError("不支援的市場來源")
        day = date.fromisoformat(trade_date)
        url = SOURCES[source].format(date=day.strftime("%Y%m%d"))
        raw, error, status = "", "", "fetched"
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ETF-Agent/0.1"})
            with self.opener(request, timeout=15) as response:
                if urlsplit(response.geturl()).hostname not in {"openapi.twse.com.tw", "www.twse.com.tw"}:
                    raise MarketProbeError("市場來源轉向未允許的主機")
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise MarketProbeError("市場原始回應過大")
                raw = body.decode("utf-8-sig")
            payload = json.loads(raw)
            if not isinstance(payload, (list, dict)):
                raise MarketProbeError("市場來源未提供資料表")
        except (OSError, ValueError) as exc:
            status, error = "failed", str(exc)
        return {"source": source, "url": url, "requested_trade_date": trade_date,
                "available_at": datetime.now(timezone.utc).isoformat(), "status": status,
                "raw_text": raw, "raw_sha256": canonical_sha256(raw), "error": error,
                "license_status": "unverified"}
