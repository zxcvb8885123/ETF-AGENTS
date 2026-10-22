"""有界讀取期交所與 FinMind 原始回應；只保存，不解讀、不核准來源。"""
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from etf_agent.core import canonical_sha256
from .contracts import FINMIND_ENDPOINT, FINMIND_MARGIN_DATASET, SOURCES, FearGreedError

MAX_BYTES = 2_000_000


def vix_url(month):
    return "https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/%snew.txt" % month


def margin_url(start, end):
    return FINMIND_ENDPOINT + "?" + urllib.parse.urlencode(
        {"dataset": FINMIND_MARGIN_DATASET, "start_date": start.isoformat(), "end_date": end.isoformat()})


class FearGreedProvider:
    def __init__(self, opener=None, token=None):
        self.opener = opener or urllib.request.urlopen
        self.token = token if token is not None else os.environ.get("FINMIND_TOKEN", "")

    def capture(self, source, url, allow_empty=False):
        """抓一份原始回應；失敗時保留原因，不拋出，讓其他來源照常計算。

        ``allow_empty`` 只給當月 VIX 月檔：月初尚未有交易日時期交所回傳空檔，不算失敗。
        """
        raw, error, status = "", "", "fetched"
        try:
            headers = {"User-Agent": "ETF-Agent/0.1"}
            if source == "FINMIND_MARGIN":
                if not self.token:
                    raise FearGreedError("缺少 FINMIND_TOKEN")
                headers["Authorization"] = "Bearer " + self.token
            with self.opener(urllib.request.Request(url, headers=headers), timeout=30) as response:
                if urlsplit(response.geturl()).hostname not in SOURCES[source]["hosts"]:
                    raise FearGreedError("來源轉向未允許的主機")
                body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise FearGreedError("原始回應過大")
            if not body and not allow_empty:
                raise FearGreedError("原始回應為空")
            # 期交所 VIX 檔是 Big5（cp950）；Put/Call 與 FinMind 為 UTF-8。
            raw = body.decode("cp950") if source == "TAIFEX_VIX" else body.decode("utf-8-sig")
        except (OSError, ValueError) as exc:
            status, error, raw = "failed", str(exc), ""
        return {"source": source, "url": url, "raw_text": raw, "raw_sha256": canonical_sha256(raw),
                "available_at": datetime.now(timezone.utc).isoformat(), "status": status, "error": error}

    def capture_all(self, today, vix_months=3, margin_days=120):
        """今天往回 vix_months 個月的 VIX 月檔、Put/Call 開放資料與 FinMind 融資。"""
        months, year, month = [], today.year, today.month
        for _ in range(vix_months):
            months.append("%04d%02d" % (year, month))
            year, month = (year, month - 1) if month > 1 else (year - 1, 12)
        start = date.fromordinal(today.toordinal() - margin_days)
        current = months[0]
        rows = [self.capture("TAIFEX_VIX", vix_url(m), allow_empty=m == current) for m in sorted(months)]
        rows.append(self.capture("TAIFEX_PCR", SOURCES["TAIFEX_PCR"]["url"]))
        rows.append(self.capture("FINMIND_MARGIN", margin_url(start, today)))
        return rows
