"""校準旁觀者的常數、錯誤型別與欄位解析。

本套件只讀已封存的 Decision run 與官方價量，不被決策、配置或風控路徑匯入，
也不回寫任何 Decision artifact。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping, Sequence

SCHEMA_VERSION = "1.0"
HORIZONS = (1, 5, 20)
# 每個 horizon 至少要有這麼多個「完整的」決策日，才輸出平均報酬與 Rank IC。
MIN_DECISION_DAYS = 10
# 一個決策日內，有有效報酬的股票占全池比例至少這麼高，該日才算完整。
MIN_DAY_COVERAGE = Decimal("0.8")
CALENDAR_BASIS = "official_price_rows"
CONVICTION_SCORE = {"high": 3, "medium": 2, "low": 1}
STRENGTH_SCORE = {"strong": 3, "moderate": 2, "weak": 1, "none": 0}
SIGNAL_INTENTS = ("buy", "add")


class CalibrationError(ValueError):
    """校準輸入不合法、封存被竄改或價格資料互相衝突。"""


def require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CalibrationError("%s 必須是物件" % label)
    return value


def require_sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise CalibrationError("%s 必須是陣列" % label)
    return value
