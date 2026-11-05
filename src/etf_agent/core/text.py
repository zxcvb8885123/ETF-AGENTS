"""外來文字進入錯誤訊息前的清理。

驗證錯誤會原樣附到 Agent 的重試提示詞；Agent 輸出（不存在的 evidence_id 等）是不受信任的資料，
拼進錯誤前必須限長、移除換行與控制字元，並以引號標示為資料，避免偽造新的指示或錯誤條目。
"""

from __future__ import annotations

import re
import unicodedata

TRUNCATED_MARK = "…(已截斷)"
UNTRUSTED_VALUE_LIMIT = 80
FEEDBACK_LINE_LIMIT = 1000


def _flatten(text: str) -> str:
    """把每段連續的控制、格式與行／段分隔字元換成一個空白（Unicode 類別 C*、Zl、Zp）。"""
    chars = []
    previous_blank = False
    for char in text:
        category = unicodedata.category(char)
        if category[0] == "C" or category in {"Zl", "Zp"}:
            if not previous_blank:
                chars.append(" ")
            previous_blank = True
        else:
            chars.append(char)
            previous_blank = False
    return "".join(chars)


def untrusted_value(value: object, limit: int = UNTRUSTED_VALUE_LIMIT) -> str:
    """把 Agent 提供的單一值轉成可放進錯誤訊息的資料表示：去控制字元、限長、以 repr 引號包住。"""
    if not isinstance(value, str):
        return single_line(repr(value), limit)
    flat = _flatten(value)
    if len(flat) <= limit:
        return repr(flat)
    return repr(flat[:limit]) + TRUNCATED_MARK


def single_line(text: str, limit: int = FEEDBACK_LINE_LIMIT) -> str:
    """把一則錯誤訊息壓成單行並限長，作為重試回饋的最後防線（不取代拼接處的 ``untrusted_value``）。"""
    flat = re.sub(r" {2,}", " ", _flatten(str(text))).strip()
    if len(flat) <= limit:
        return flat
    return flat[:limit] + TRUNCATED_MARK
