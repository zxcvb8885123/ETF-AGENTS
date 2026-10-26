"""FinMind 個股新聞進事件分析的契約：來源、媒體白名單、查詢視窗與欄位。

個股新聞只進公司事件通道，不得代表全市場情緒；標題是不受信任的資料。
"""
import urllib.parse

from etf_agent.core import canonical_sha256


class CompanyNewsError(ValueError):
    """個股新聞未通過來源核准、時間、白名單或完整性檢查。"""


SCHEMA_VERSION = "company-news-1.0"
SOURCE = "FINMIND_NEWS"
TERMS_URL = "https://finmind.github.io/en/Disclaimer/"
REVIEW_TABLE = "company_news_source_reviews"
# 與 market_news.repository 共用核對規則；registry 只登錄 FinMind，不混入全市場新聞來源。
SOURCES = {SOURCE: {"terms_url": TERMS_URL, "publisher": "FinMind 個股新聞"}}
# 只用截止前最後一次抓取的那批（FinMind 平日 22:00 抓一次，一批約一小時內完成）；
# 最後一批早於截止前 3 天時視為排程中斷，不拿舊聞補。
BATCH_HOURS = 12
WINDOW_DAYS = 3
EVIDENCE_PREFIX = "news:finmind:"

# FinMind 的 publisher 字串不一致（同一家有多種寫法），以連結網域判斷。
# 只收正規新聞媒體；網友貼文（CMoney）、論壇（聚財網）、券商行銷與聚合站不收。
PUBLISHER_POLICY_VERSION = "company_news_publishers_v1"
PUBLISHER_HOSTS = frozenset({
    "udn.com", "money.udn.com",
    "ec.ltn.com.tw", "stock.ltn.com.tw", "news.ltn.com.tw",
    "www.ctee.com.tw", "www.chinatimes.com",
    "m.cnyes.com", "news.cnyes.com",
    "www.moneydj.com", "www.businesstoday.com.tw",
    "finance.ettoday.net", "news.tvbs.com.tw", "inews.setn.com", "www.ttv.com.tw",
    "finance.technews.tw", "tw.news.yahoo.com", "www.cna.com.tw", "www.mirrormedia.mg",
})

# 收集時寫入 content_sha256 的原始欄位（data.supplemental_sources.FinMindNewsCollector）。
CANDIDATE_FIELDS = (
    "source", "stock_id", "title", "link", "publisher", "source_time_raw", "published_at",
    "license_status", "license_url", "canonical_content_id",
)
ITEM_FIELDS = {"evidence_id", "symbol", "available_at", "content_sha256", "candidate"}
PACK_FIELDS = {
    "schema_version", "source", "snapshot_id", "decision_cutoff", "latest_fetch_at", "window_start", "window_days",
    "publisher_policy", "source_review", "items", "content_sha256",
}
REVIEW_FIELDS = {"source", "license_status", "terms_url", "usage_scope", "reviewed_at", "evidence"}


def publisher_policy():
    return {"version": PUBLISHER_POLICY_VERSION, "hosts": sorted(PUBLISHER_HOSTS)}


def evidence_id(content_sha256):
    return EVIDENCE_PREFIX + str(content_sha256)[:16]


def link_host(link):
    return urllib.parse.urlparse(str(link)).hostname


def outlet(link):
    """媒體品牌：去掉子網域（money.udn.com 與 udn.com 同一家；.com.tw 保留三段）。"""
    labels = str(link_host(link) or "").split(".")
    return ".".join(labels[-3:] if labels[-2:] == ["com", "tw"] else labels[-2:])


def canonical_content_id(link):
    """與收集端相同：去掉 fragment 後的連結雜湊。"""
    parsed = urllib.parse.urlparse(str(link))
    return canonical_sha256({"link": urllib.parse.urlunparse(parsed._replace(fragment=""))})


def check_fields(value, allowed, name):
    if not isinstance(value, dict) or set(value) != set(allowed):
        raise CompanyNewsError(name + " 欄位不符合契約")
