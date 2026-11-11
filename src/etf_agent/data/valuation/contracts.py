"""估值收集（V1）的契約：常數、錯誤型別與欄位解析。

V1 只收集與稽核，不進決策、不進簡報。上市股限定；上櫃端點尚未確認，一律不收。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from etf_agent.core import parse_decimal

SCHEMA_VERSION = "1.0"
MAPPING_VERSION = "twse-bwibbu-finmind-per-v1"
OFFICIAL_SOURCE = "TWSE_BWIBBU_ALL"
OFFICIAL_ENDPOINT = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
FINMIND_SOURCE = "FINMIND_PER"
FINMIND_DATASET = "TaiwanStockPER"
# 使用者 2026-11-10 核准：只用於 V1 收集與稽核，不進決策與簡報。
LICENSE_STATUS = "approved"
LICENSE_SCOPE = "valuation_v1_collection_and_audit_only"
LICENSE_APPROVED_ON = "2026-11-10"
OFFICIAL_FIELDS = ("Date", "Code", "PEratio", "PBratio", "DividendYield")
FINMIND_FIELDS = ("date", "stock_id", "PER", "PBR", "dividend_yield")
METRICS = ("per", "pbr", "dividend_yield")
# 預設只補最近幾天；第一次建立歷史請用 --history-start 明確指定。
DEFAULT_HISTORY_DAYS = 14
TAIPEI = timezone(timedelta(hours=8))
QUANT = Decimal("0.01")

# 原因碼
REASON_OFFICIAL_MISSING = "OFFICIAL_MISSING_SYMBOL"
REASON_FINMIND_MISSING = "FINMIND_MISSING_DATE"
REASON_FINMIND_UNAVAILABLE = "FINMIND_UNAVAILABLE"
REASON_FINMIND_BAD_ROW = "FINMIND_BAD_ROW"
REASON_MISMATCH = "VALUE_MISMATCH"
REASON_NOT_PROVIDED = "NOT_PROVIDED"
REASON_NONPOSITIVE = "NONPOSITIVE"
REASON_OFFICIAL_BAD_ROW = "OFFICIAL_BAD_ROW"


class ValuationError(ValueError):
    """估值收集的契約或驗證錯誤。"""


def parse_roc_date(value: object) -> date:
    """民國日期 ``1151109`` → 2026-11-09；格式或日曆不合法即拒絕。"""
    text = str(value).strip() if value is not None else ""
    if not re.fullmatch(r"[0-9]{7}", text):
        raise ValuationError("民國日期必須是 7 位數字（YYYMMDD）：%r" % (value,))
    try:
        return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:]))
    except ValueError as error:
        raise ValuationError("民國日期不是合法日期：%s" % text) from error


def normalize_metric(metric: str, value: object):
    """回傳 ``(Decimal 或 None, 原因碼或 None)``。

    空字串或缺值 → ``NOT_PROVIDED``；PER／PBR 為 0 或負 → ``NONPOSITIVE``（不當成 0，也不當成便宜）。
    殖利率 0 是合法數值。數值一律量化到兩位小數供核對。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, REASON_NOT_PROVIDED
    number = parse_decimal(value, metric, error=ValuationError, reject_bool=True)
    if metric in ("per", "pbr") and number <= 0:
        return None, REASON_NONPOSITIVE
    if metric == "dividend_yield" and number < 0:
        raise ValuationError("dividend_yield 不得為負：%s" % value)
    return number.quantize(QUANT, rounding=ROUND_HALF_UP), None


def taipei_date(timestamp: datetime) -> date:
    return timestamp.astimezone(TAIPEI).date()
