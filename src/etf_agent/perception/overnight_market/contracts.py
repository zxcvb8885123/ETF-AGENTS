"""隔夜市場背景契約：來源白名單、標的、訊號常數與欄位檢查。

這是現金姿態與報告用的次級背景，不是台股情緒、不是個股訊號，也不預測指數方向。
數字只由程式依 Yahoo Finance 日線計算；模型不得覆寫。
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
SOURCES = {
    SOURCE_ID: {
        "name": "Yahoo Finance 美股日線收盤（經 yfinance）",
        "hosts": {YAHOO_HOST},
        "terms_url": "https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html",
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
