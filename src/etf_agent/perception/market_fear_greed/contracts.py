"""台股恐懼貪婪指標契約：來源白名單、計分常數與欄位檢查。

這是本專案自建的全市場情緒指標，不是 CNN Fear & Greed Index。
分數只由程式依官方數值計算；模型不得覆寫。
"""
from etf_agent.core import canonical_sha256


class FearGreedError(ValueError):
    """恐懼貪婪輸入未通過來源、時間、授權或重算檢查。"""


PACK_VERSION = "market-fear-greed-1.0"
RESULT_VERSION = "market-fear-greed-result-1.0"
SCORE_VERSION = "fear-greed-v1"

# 每項指標以最近 WINDOW 個交易日的百分位計分（含當日）；資料不足即該項不可用。
WINDOW = 20
# 融資用 N 個交易日的餘額變化率，不用餘額水位（水位隨市值長期上升）。
MARGIN_CHANGE_DAYS = 5
# 至少要有幾項已核准指標可用，才輸出正式分數。
MIN_COMPONENTS = 2
# 指標最新資料日距離截止日超過幾個日曆日即視為過期。
MAX_STALE_DAYS = 5
# (上限, 區間, outlook)；分數 <= 上限即落入該區間。
BANDS = (
    (25, "extreme_fear", "negative"),
    (45, "fear", "negative"),
    (55, "neutral", "neutral"),
    (75, "greed", "positive"),
    (100, "extreme_greed", "positive"),
)
BAND_LABELS = {
    "extreme_fear": "極度恐懼", "fear": "恐懼", "neutral": "中性",
    "greed": "貪婪", "extreme_greed": "極度貪婪",
}

FINMIND_ENDPOINT = "https://api.finmindtrade.com/api/v4/data"
FINMIND_MARGIN_DATASET = "TaiwanStockTotalMarginPurchaseShortSale"

# direction：數值越高越貪婪為 greed，越高越恐懼為 fear。
SOURCES = {
    "TAIFEX_VIX": {
        "name": "臺指選擇權波動率指數（收盤）",
        "url_pattern": r"https://www\.taifex\.com\.tw/file/taifex/Dailydownload/vix/log2data/(\d{6})new\.txt",
        "hosts": {"www.taifex.com.tw"},
        "terms_url": "https://www.taifex.com.tw/cht/edu/userTerms",
        "direction": "fear",
    },
    "TAIFEX_PCR": {
        "name": "臺指選擇權買賣權成交量比率%",
        "url": "https://www.taifex.com.tw/data_gov/taifex_open_data.asp?data_name=PutCallRatio",
        "hosts": {"www.taifex.com.tw"},
        "terms_url": "https://data.gov.tw/dataset/11322",
        "direction": "fear",
    },
    "FINMIND_MARGIN": {
        "name": "集中市場融資餘額（金額）%d 日變化率" % MARGIN_CHANGE_DAYS,
        "url_pattern": (r"https://api\.finmindtrade\.com/api/v4/data\?dataset=" + FINMIND_MARGIN_DATASET
                        + r"&start_date=(\d{4}-\d{2}-\d{2})&end_date=(\d{4}-\d{2}-\d{2})"),
        "hosts": {"api.finmindtrade.com"},
        "terms_url": "https://finmind.github.io/en/Disclaimer/",
        "direction": "greed",
    },
}

CAPTURE_FIELDS = {"source", "url", "raw_text", "raw_sha256", "available_at", "status", "error"}
APPROVAL_FIELDS = {"source", "license_status", "terms_url", "usage_scope", "reviewed_at", "approved_by", "evidence"}
PACK_FIELDS = {"schema_version", "snapshot_id", "decision_cutoff", "captures", "source_approvals", "content_sha256"}


def seal(payload, field="content_sha256"):
    body = {k: v for k, v in payload.items() if k != field}
    return dict(body, **{field: canonical_sha256(body)})


def check_fields(value, allowed, name):
    if not isinstance(value, dict) or set(value) != set(allowed):
        raise FearGreedError(name + " 欄位不符合契約")
