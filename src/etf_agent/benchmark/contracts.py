"""基準比較的錯誤型別、費稅設定與價格列契約。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, Mapping, Optional

from etf_agent.core import parse_decimal

TAIPEI = timezone(timedelta(hours=8))

# 帳本結算用的官方來源優先序（與 virtual_account.daily.OFFICIAL_SOURCES 同義）。
OFFICIAL_SOURCE_PRIORITY: Mapping[str, int] = {
    "TWSE_STOCK_DAY": 1,
    "TPEX_TRADING_STOCK": 1,
    "TPEX_DAILY_QUOTES_HISTORY": 2,
    "TWSE_STOCK_DAY_ALL": 3,
    "TPEX_MAINBOARD_QUOTES": 3,
}

PRICE_BASIS = "official_unadjusted"
EXECUTION_PRICE_RULE = "official_average_price"
RATE_PLACES = Decimal("0.000001")
MONEY_PLACES = Decimal("0.01")


class BenchmarkError(ValueError):
    """基準比較的輸入或資料不合法。"""


@dataclass(frozen=True)
class FeeSchedule:
    """與 config/decision_rules.json 相同的整張、手續費與證交稅設定。"""

    lot_size: int
    commission_rate: Decimal
    sell_tax_rate: Decimal
    minimum_commission: Decimal
    rules_version: str

    @classmethod
    def from_rules(cls, path: Path) -> "FeeSchedule":
        try:
            rules = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BenchmarkError("無法讀取決策規則：%s" % path) from error
        if not isinstance(rules, Mapping):
            raise BenchmarkError("決策規則頂層必須是物件")
        try:
            lot_size = int(rules["lot_size"])
        except (KeyError, TypeError, ValueError) as error:
            raise BenchmarkError("決策規則缺少有效的 lot_size") from error
        if lot_size <= 0:
            raise BenchmarkError("lot_size 必須為正整數")
        values = {}
        for field in ("commission_rate", "sell_tax_rate", "minimum_commission"):
            if field not in rules:
                raise BenchmarkError("決策規則缺少欄位：%s" % field)
            values[field] = parse_decimal(rules[field], field, error=BenchmarkError)
            if values[field] < 0:
                raise BenchmarkError("%s 不得為負數" % field)
        return cls(
            lot_size=lot_size,
            commission_rate=values["commission_rate"],
            sell_tax_rate=values["sell_tax_rate"],
            minimum_commission=values["minimum_commission"],
            rules_version=str(rules.get("version", "unspecified")),
        )

    def assumptions(self) -> Dict[str, object]:
        """交給 ledger.ExecutionSimulator 的假設，與帳本模擬成交同一套。"""
        return {
            "lot_size": self.lot_size,
            "commission_rate": str(self.commission_rate),
            "sell_tax_rate": str(self.sell_tax_rate),
            "minimum_commission": str(self.minimum_commission),
            "reuse_sell_proceeds": False,
        }

    def as_dict(self) -> Dict[str, object]:
        return {
            "lot_size": self.lot_size,
            "commission_rate": str(self.commission_rate),
            "sell_tax_rate": str(self.sell_tax_rate),
            "minimum_commission": str(self.minimum_commission),
            "rules_version": self.rules_version,
        }


@dataclass(frozen=True)
class OfficialPriceRow:
    """某標的某交易日在 as-of 時點已可得的官方日線（最新版本）。"""

    symbol: str
    trade_date: str
    close_price: Decimal
    trade_value: int
    volume_shares: int
    source: str
    fetched_at: datetime

    def average_price(self) -> Optional[Decimal]:
        """官方成交均價＝成交金額 ÷ 成交股數，四捨五入到 0.01；無成交回 None（與帳本相同）。"""
        from etf_agent.virtual_account.daily import average_price

        return average_price(self.trade_value, self.volume_shares)

    def as_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "trade_date": self.trade_date,
            "close_price": str(self.close_price),
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat(),
        }
