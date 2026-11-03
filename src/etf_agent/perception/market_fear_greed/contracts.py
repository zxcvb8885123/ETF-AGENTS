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
# 1.1／v2：加入「融資水位」第 4 項（融資金額 ÷ 加權指數的長窗口百分位）。
# 資料包是否為 1.1 由抓取是否含 TWSE_TAIEX 決定；沒有的舊抓取與舊封存維持 1.0／v1 逐位元重建。
PACK_VERSION_V11 = "market-fear-greed-1.1"
RESULT_VERSION_V11 = "market-fear-greed-result-1.1"
SCORE_VERSION_V2 = "fear-greed-v2"
# 1.2／v3：不再抓 Put/Call 比（政府資料只有約 20 個交易日，常因差一天而不可用），其餘同 1.1。
PACK_VERSION_V12 = "market-fear-greed-1.2"
RESULT_VERSION_V12 = "market-fear-greed-result-1.2"
SCORE_VERSION_V3 = "fear-greed-v3"
PACK_VERSIONS = (PACK_VERSION, PACK_VERSION_V11, PACK_VERSION_V12)
# 融資水位用最近 LEVEL_WINDOW 個交易日的「融資金額 ÷ 加權指數」百分位。
LEVEL_WINDOW = 250
LEVEL_SOURCE = "FINMIND_MARGIN_LEVEL"
LEVEL_NAME = "集中市場融資金額÷加權指數（百萬元／點）%d 日百分位" % LEVEL_WINDOW
# 融資水位的輸入：融資金額（沿用 FINMIND_MARGIN）與證交所加權指數。
LEVEL_INPUTS = ("FINMIND_MARGIN", "TWSE_TAIEX")
TWSE_ENDPOINT = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"

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

# 只在 1.1 資料包使用；不放進 SOURCES，舊的 1.0 重建與測試因此完全不受影響。
EXTRA_SOURCES = {
    "TWSE_TAIEX": {
        "name": "證交所發行量加權股價指數（收盤）",
        "url_pattern": r"https://www\.twse\.com\.tw/rwd/zh/afterTrading/FMTQIK\?date=(\d{8})&response=json",
        "hosts": {"www.twse.com.tw"},
        "terms_url": "https://www.twse.com.tw/zh/",
        "direction": "greed",
    },
}


def sources_for(pack_version):
    if pack_version == PACK_VERSION_V12:
        return dict({k: v for k, v in SOURCES.items() if k != "TAIFEX_PCR"}, **EXTRA_SOURCES)
    return dict(SOURCES, **EXTRA_SOURCES) if pack_version == PACK_VERSION_V11 else SOURCES


def all_sources():
    return dict(SOURCES, **EXTRA_SOURCES)


CAPTURE_FIELDS = {"source", "url", "raw_text", "raw_sha256", "available_at", "status", "error"}
APPROVAL_FIELDS = {"source", "license_status", "terms_url", "usage_scope", "reviewed_at", "approved_by", "evidence"}
PACK_FIELDS = {"schema_version", "snapshot_id", "decision_cutoff", "captures", "source_approvals", "content_sha256"}


def seal(payload, field="content_sha256"):
    body = {k: v for k, v in payload.items() if k != field}
    return dict(body, **{field: canonical_sha256(body)})


def check_fields(value, allowed, name):
    if not isinstance(value, dict) or set(value) != set(allowed):
        raise FearGreedError(name + " 欄位不符合契約")
