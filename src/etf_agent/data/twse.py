import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, List, Optional, Tuple


@dataclass(frozen=True)
class DailyPrice:
    symbol: str
    code: str
    name: str
    trade_date: str
    open_price: Optional[Decimal]
    high_price: Optional[Decimal]
    low_price: Optional[Decimal]
    close_price: Decimal
    change: Optional[Decimal]
    volume_shares: int
    trade_value: int
    transactions: int


def parse_roc_date(value: str) -> str:
    digits = "".join(character for character in str(value) if character.isdigit())
    if len(digits) == 7:
        year = int(digits[:3]) + 1911
        month = int(digits[3:5])
        day = int(digits[5:7])
    elif len(digits) == 8:
        year = int(digits[:4])
        month = int(digits[4:6])
        day = int(digits[6:8])
    else:
        raise ValueError("無法解析交易日期：%r" % value)
    return datetime(year, month, day).date().isoformat()


def parse_decimal(value: object) -> Optional[Decimal]:
    text = str(value or "").strip().replace(",", "")
    if text in {"", "-", "--", "---"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise ValueError("無法解析數值：%r" % value) from error


def parse_integer(value: object) -> int:
    number = parse_decimal(value)
    if number is None:
        return 0
    if number != number.to_integral_value():
        raise ValueError("預期整數但取得：%r" % value)
    return int(number)


class TwseDailyProvider:
    source_name = "TWSE_STOCK_DAY_ALL"

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
            raise ValueError("TWSE 回應不是有效 JSON") from error
        if not isinstance(raw_rows, list):
            raise ValueError("TWSE 回應格式錯誤：預期 JSON 陣列")

        prices: List[DailyPrice] = []
        warnings: List[str] = []
        for index, row in enumerate(raw_rows):
            try:
                if not isinstance(row, dict):
                    raise ValueError("資料列不是物件")
                code = str(row["Code"]).strip()
                close_price = parse_decimal(row.get("ClosingPrice"))
                if not code:
                    raise ValueError("股票代碼為空")
                if close_price is None:
                    warnings.append("%s 無收盤價，已略過" % code)
                    continue
                prices.append(
                    DailyPrice(
                        symbol=code.upper() + ".TW",
                        code=code.upper(),
                        name=str(row.get("Name") or "").strip(),
                        trade_date=parse_roc_date(str(row["Date"])),
                        open_price=parse_decimal(row.get("OpeningPrice")),
                        high_price=parse_decimal(row.get("HighestPrice")),
                        low_price=parse_decimal(row.get("LowestPrice")),
                        close_price=close_price,
                        change=parse_decimal(row.get("Change")),
                        volume_shares=parse_integer(row.get("TradeVolume")),
                        trade_value=parse_integer(row.get("TradeValue")),
                        transactions=parse_integer(row.get("Transaction")),
                    )
                )
            except (KeyError, ValueError) as error:
                warnings.append("第 %d 筆資料略過：%s" % (index + 1, error))
        if not prices:
            raise ValueError("TWSE 回應沒有可用的行情資料")
        return prices, warnings
