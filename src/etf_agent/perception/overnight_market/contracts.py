"""隔夜市場背景契約：來源白名單、標的、訊號常數與欄位檢查。

這是現金姿態與報告用的次級背景，不是台股情緒、不是個股訊號，也不預測指數方向。
數字只由程式依 Yahoo Finance 日線與期交所官網夜盤報價計算；模型不得覆寫。
``tools.historical_signals`` 只給離線回測（research/cross_market_study）用，不在決策鏈。
"""
from etf_agent.core import canonical_sha256


class OvernightMarketError(ValueError):
    """隔夜市場輸入未通過來源、時間、授權或重算檢查。"""


PACK_VERSION = "overnight-market-1.0"
RESULT_VERSION = "overnight-market-result-1.0"
SIGNAL_VERSION = "overnight-v1"

# 取日線的回看日曆日數；足夠算 20 日百分位（需要 21 個收盤）。
LOOKBACK_CALENDAR_DAYS = 50
# 近 N 個交易日的單日報酬計百分位（含最新一個時段）。
PERCENTILE_WINDOW = 20
# overnight-v1 門檻（百分比）。
NEGATIVE_AT_OR_BELOW = -1.5
POSITIVE_AT_OR_ABOVE = 1.0
MIXED_MIN_ABS = 0.75
# 主訊號：兩者都必須可用，取累計漲跌幅平均。
MAIN_SYMBOLS = ("^SOX", "^GSPC")
PROXY_SYMBOLS = ("EWT", "TSM")

YAHOO_HOST = "query1.finance.yahoo.com"
YAHOO_CHART_PREFIX = "https://%s/v8/finance/chart/" % YAHOO_HOST

SOURCE_ID = "YAHOO_FINANCE"
TAIFEX_SOURCE_ID = "TAIFEX_MIS"
TAIFEX_HOST = "mis.taifex.com.tw"
TAIFEX_QUOTE_URL = "https://%s/futures/api/getQuoteList" % TAIFEX_HOST
SOURCES = {
    SOURCE_ID: {
        "name": "Yahoo Finance 美股日線收盤（經 yfinance）",
        "hosts": {YAHOO_HOST},
        "terms_url": "https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html",
    },
    TAIFEX_SOURCE_ID: {
        "name": "期交所官網即時行情（台指期盤後）",
        "hosts": {TAIFEX_HOST},
        "terms_url": "https://www.taifex.com.tw/cht/edu/userTerms",
    },
}

# role：us_index 為美股指數；taiwan_proxy 為在美國掛牌、隔夜反映台股的代理。
SYMBOLS = {
    "^GSPC": {"name": "S&P 500 指數", "role": "us_index"},
    "^IXIC": {"name": "NASDAQ 綜合指數", "role": "us_index"},
    "^SOX": {"name": "費城半導體指數", "role": "us_index"},
    "EWT": {"name": "iShares MSCI Taiwan ETF（美股掛牌）", "role": "taiwan_proxy"},
    "TSM": {"name": "台積電 ADR（美股掛牌）", "role": "taiwan_proxy"},
}

# overnight-market-1.1：加入日韓指數、VIX 與台指期夜盤，並算出方向一致性。1.0 的資料包與結果維持原重建行為。
PACK_VERSION_V11 = "overnight-market-1.1"
RESULT_VERSION_V11 = "overnight-market-result-1.1"
SIGNAL_VERSION_V11 = "overnight-v1.1"
SYMBOLS_V11 = dict(SYMBOLS, **{
    "^N225": {"name": "日經 225 指數", "role": "asia_index"},
    "^KS11": {"name": "韓國 KOSPI 指數", "role": "asia_index"},
    "^VIX": {"name": "CBOE 波動率指數 VIX", "role": "volatility"},
})
NIGHT_SYMBOL = "TXF_NIGHT"
NIGHT_NAME = "台指期近月盤後（相對前一日日盤結算價）"
# 方向一致性：|漲跌幅| 低於此值算持平；至少 AGREEMENT_MIN_TOTAL 個方向訊號、且同向比例達 AGREEMENT_RATIO 才算一致。
DIRECTION_DEADBAND = 0.30
AGREEMENT_MIN_TOTAL = 3
AGREEMENT_RATIO = 0.75

OUTLOOK_LABELS = {"negative": "偏負面", "neutral": "中性", "positive": "偏正面", "mixed": "訊號分歧", "unknown": "未知"}

CAPTURE_FIELDS = {"source", "symbol", "url", "raw_text", "raw_sha256", "available_at", "status", "error"}
APPROVAL_FIELDS = {"source", "license_status", "terms_url", "usage_scope", "reviewed_at", "approved_by", "evidence"}
PACK_FIELDS = {"schema_version", "snapshot_id", "decision_cutoff", "latest_trade_date", "captures",
               "source_approvals", "content_sha256"}


def seal(payload, field="content_sha256"):
    body = {k: v for k, v in payload.items() if k != field}
    return dict(body, **{field: canonical_sha256(body)})


def check_fields(value, allowed, name):
    if not isinstance(value, dict) or set(value) != set(allowed):
        raise OvernightMarketError(name + " 欄位不符合契約")
