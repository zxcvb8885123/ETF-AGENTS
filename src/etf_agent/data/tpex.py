"""TPEx latest mainboard quote provider."""

import json
import urllib.request
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from .twse import DailyPrice, parse_decimal, parse_integer, parse_roc_date


def parse_optional_decimal(
    value: object, code: str, field: str, warnings: List[str]
):
    try:
        return parse_decimal(value)
    except ValueError:
        warnings.append(
            "%s 的 %s 不是數值（%r），已保存為 null" % (code, field, value)
        )
        return None


class TpexDailyProvider:
    source_name = "TPEX_MAINBOARD_QUOTES"
    market = "TPEX"

    def __init__(
        self,
        url: str,
        timeout_seconds: int = 30,
        user_agent: str = "ETF-Agent-AICUP-2026/0.1",
        opener: Optional[Callable[..., object]] = None,
    ):
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.opener = opener or urllib.request.urlopen

    def fetch(self) -> Tuple[str, str]:
        request = urllib.request.Request(
            self.url,
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
        )
        with self.opener(request, timeout=self.timeout_seconds) as response:
            payload = response.read().decode("utf-8-sig")
        fetched_at = datetime.now(timezone.utc).isoformat()
        return payload, fetched_at

    @staticmethod
    def parse(payload: str) -> Tuple[List[DailyPrice], List[str]]:
        try:
            raw_rows = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ValueError("TPEx 回應不是有效 JSON") from error
        if not isinstance(raw_rows, list):
            raise ValueError("TPEx 回應格式錯誤：預期 JSON 陣列")

        prices: List[DailyPrice] = []
        warnings: List[str] = []
        for index, row in enumerate(raw_rows):
            try:
                if not isinstance(row, dict):
                    raise ValueError("資料列不是物件")
                code = str(row["SecuritiesCompanyCode"]).strip().upper()
                close_price = parse_decimal(row.get("Close"))
                if not code:
                    raise ValueError("股票代碼為空")
                if close_price is None:
                    warnings.append("%s 無收盤價，已略過" % code)
                    continue
                prices.append(
                    DailyPrice(
                        symbol=code + ".TWO",
                        code=code,
                        name=str(row.get("CompanyName") or "").strip(),
                        trade_date=parse_roc_date(str(row["Date"])),
                        open_price=parse_optional_decimal(
                            row.get("Open"), code, "Open", warnings
                        ),
                        high_price=parse_optional_decimal(
                            row.get("High"), code, "High", warnings
                        ),
                        low_price=parse_optional_decimal(
                            row.get("Low"), code, "Low", warnings
                        ),
                        close_price=close_price,
                        change=parse_optional_decimal(
                            row.get("Change"), code, "Change", warnings
                        ),
                        volume_shares=parse_integer(row.get("TradingShares")),
                        trade_value=parse_integer(row.get("TransactionAmount")),
                        transactions=parse_integer(row.get("TransactionNumber")),
                    )
                )
            except (KeyError, ValueError) as error:
                warnings.append("第 %d 筆資料略過：%s" % (index + 1, error))
        if not prices:
            raise ValueError("TPEx 回應沒有可用的行情資料")
        return prices, warnings
