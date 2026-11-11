"""「含時間條件」的文字結構檢查（只看結構、不判斷內容對錯）：一條可核對的時間條件，要有明確期限或時間點。

目前只有 ``offense.failure_condition`` 使用。交易 Agent 的 ``invalidation_conditions`` 在程式端只檢查非空字串（時間條件由 Skill 要求、
沒有程式檢查）；若要套用須另開版本閘門，否則舊決策重建會被改變，所以這裡不替它接線。

通過的是：數字加時間單位（「三個交易日內」「5 天」「兩週」）、固定的時間點用語（「下一個收盤」「隔日」「月底」「本週」）、
日期（「11/18」「2026-11-18」「11月18日」）。不通過的是只有模糊用語（「短期內」「一段時間」「日後」「之後」）或完全沒有時間的句子。
"""

from __future__ import annotations

import re
from typing import Optional

MIN_CONDITION_LENGTH = 6
_NUMBER = r"[0-9０-９一二兩三四五六七八九十百]+"
_PATTERNS = (
    # 數字加時間單位：三個交易日、5 天、兩週、一個月、三季
    re.compile(_NUMBER + r"\s*(?:個|周|週)?\s*(?:交易日|營業日|工作日|日|天|週|周|星期|個月|月|季|年|小時|個?盤)"),
    # 日期：11/18、2026-11-18、2026/11/18、11月18日
    re.compile(r"\d{1,2}\s*/\s*\d{1,2}"),
    re.compile(r"\d{4}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{1,2}"),
    re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*[日號号]"),
)
_PHRASES = (
    "收盤", "開盤", "盤前", "盤後", "盤中", "隔日", "次日", "翌日", "明日", "明天", "當日", "今日", "今天",
    "本週", "本周", "下週", "下周", "本月", "下月", "月底", "週底", "季底", "年底", "月初", "週五", "週一",
    "下一個交易日", "下個交易日", "下一交易日", "下一個盤", "下一個收盤", "次一交易日",
)


def time_condition_error(text: str) -> Optional[str]:
    """沒有問題回 None；否則回中性、並列兩種修法的說明（補上可核對的時間，或改寫成可觀察的事實／降級）。"""
    stripped = text.strip()
    if len(stripped) < MIN_CONDITION_LENGTH:
        return "太短，看不出之後可核對的失敗事實；請補上具體的觀察對象與期限，或改選較保守的等級"
    if any(pattern.search(stripped) for pattern in _PATTERNS) or any(phrase in stripped for phrase in _PHRASES):
        return None
    return (
        "看不出可核對的時間條件（模糊用語如「短期內」「一段時間」「日後」不算）；"
        "請補上明確期限或時間點（例如「兩個交易日內」「下一個收盤」「11/18 前」），或改選較保守的等級，擇一即可"
    )
