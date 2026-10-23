"""回放的模擬帳本與官方成交價來源。

- ``ReplayLedger``：回放自己的帳本（沿用 ``ledger`` 的整張成交、費稅與交割規則），不讀實際帳本當持股。
- ``ReplayMarket``：只讀 SQLite 官方價量（``OfficialCloseMarket`` 口徑），只看 ``as_of`` 前已抓到的列，
  成交日資料若在該日收盤前就「抓到」視為前視價格並拒絕。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core import canonical_sha256, parse_aware_time
from etf_agent.decision.fields import DecisionToolError, snapshot_execution_price
from etf_agent.ledger import AccountLedger, ExecutionSimulator, LedgerError
from etf_agent.virtual_account.daily import OFFICIAL_SOURCES, OfficialCloseMarket, add_business_days
from etf_agent.virtual_account.service import VirtualAccountError

from .contracts import ReplayError


TAIPEI = timezone(timedelta(hours=8))
SETTLEMENT_BUSINESS_DAYS = 2


def _time(value: object, field: str):
    return parse_aware_time(value, field, error=ReplayError)


class ReplayLedger:
    """回放專用帳本：起點取第一天封存的帳戶快照或指定的全現金，之後只由回放成交結轉。"""

    def __init__(self, settled_cash: object, positions: Sequence[Mapping[str, object]], account_id: str):
        try:
            self.ledger = AccountLedger({
                "settled_cash": str(settled_cash), "unsettled_cash": "0",
                "positions": [{"symbol": row["symbol"], "shares": row["shares"], "cost_basis": row["average_cost"]}
                              for row in positions],
            })
        except (LedgerError, KeyError, TypeError, ValueError) as error:
            raise ReplayError("回放起始帳戶無效：%s" % error) from error
        self.account_id = account_id

    @classmethod
    def from_account_snapshot(cls, account: Mapping[str, object]) -> "ReplayLedger":
        if Decimal(str(account.get("unsettled_cash", "0"))) != 0:
            raise ReplayError("起始帳戶快照含未交割賣款，回放無法得知交割日；請改用全現金起點（initial_cash）")
        return cls(account["settled_cash"], account.get("positions", []), "replay:%s" % account.get("account_id", "account"))

    @classmethod
    def all_cash(cls, cash: object, account_id: str = "replay:cash") -> "ReplayLedger":
        amount = Decimal(str(cash))
        if amount <= 0:
            raise ReplayError("initial_cash 必須大於 0")
        return cls(str(amount), [], account_id)

    def held_symbols(self) -> List[str]:
        return sorted(self.ledger.positions)

    def prepare(self, bundle: Mapping[str, object]) -> Dict[str, object]:
        """以該日 Snapshot 的決策用價重新估值，產生回放用 AccountSnapshot（與 prepare_day 同口徑）。"""
        cutoff = str(bundle["decision_cutoff"])
        cutoff_time = _time(cutoff, "decision_cutoff")
        self.ledger.settle(cutoff_time.astimezone(TAIPEI).date().isoformat())
        try:
            prices = {
                str(row["symbol"]).upper(): str(snapshot_execution_price(row, "latest_prices"))
                for row in bundle["snapshot"]["latest_prices"]
            }
        except (DecisionToolError, KeyError, TypeError) as error:
            raise ReplayError("Snapshot 決策用價無法解析：%s" % error) from error
        missing = [symbol for symbol in self.held_symbols() if symbol not in prices]
        if missing:
            raise ReplayError("回放持股在 %s 的 Snapshot 沒有決策用價：%s" % (cutoff, "、".join(missing)))
        trade_date = str(bundle["snapshot"].get("latest_trade_date"))
        try:
            marked = self.ledger.snapshot(prices, trade_date)
        except LedgerError as error:
            raise ReplayError("回放帳戶估值失敗：%s" % error) from error
        return {
            "account_id": self.account_id, "available_at": cutoff, "valuation_at": cutoff,
            "source_evidence_id": "replay-account:" + canonical_sha256({"marked": marked["content_sha256"], "cutoff": cutoff})[:24],
            "cash": marked["cash"], "settled_cash": marked["settled_cash"], "unsettled_cash": marked["unsettled_cash"],
            "nav": marked["nav"],
            "positions": [{"symbol": row["symbol"], "shares": row["shares"], "average_cost": row["cost_basis"]}
                          for row in marked["positions"]],
        }

    def execute(
        self, decision: Mapping[str, object], market: Mapping[str, object], rules: Mapping[str, object], settlement_date: str,
    ) -> Dict[str, object]:
        """依比賽口徑在成交日以官方均價整張成交；現金不足時由模擬器自行減張。"""
        reuse = bool(rules["reuse_sell_proceeds"])
        assumptions = {
            "lot_size": rules["lot_size"], "commission_rate": rules["commission_rate"],
            "sell_tax_rate": rules["sell_tax_rate"], "minimum_commission": rules["minimum_commission"],
            "reuse_sell_proceeds": reuse,
        }
        trade_date = str(market["trade_date"])
        try:
            self.ledger.settle(trade_date)
            execution = ExecutionSimulator(assumptions).run(decision, market, self.ledger.buying_power(reuse), str(market["execution_at"]))
            self.ledger.apply_fills(execution, settlement_date, reuse)
            self.ledger.settle(trade_date)
        except LedgerError as error:
            raise ReplayError("回放成交失敗：%s" % error) from error
        return execution

    def close(self, close_market: Mapping[str, object]) -> Dict[str, object]:
        quotes = close_market["quotes"]
        missing = [symbol for symbol in self.held_symbols() if symbol not in quotes]
        if missing:
            raise ReplayError("%s 缺少持股官方收盤價：%s" % (close_market["trade_date"], "、".join(missing)))
        try:
            return self.ledger.snapshot({symbol: row["close_price"] for symbol, row in quotes.items()}, str(close_market["trade_date"]))
        except LedgerError as error:
            raise ReplayError("回放日終估值失敗：%s" % error) from error


class ReplayMarket:
    """官方成交均價與收盤價：只看 ``as_of`` 前抓到的列；成交日資料不得早於該日收盤抓到。"""

    def __init__(self, database: Path, as_of: str):
        self.database = Path(database)
        if not self.database.is_file():
            raise ReplayError("找不到行情資料庫：%s" % self.database)
        self.as_of = _time(as_of, "as_of").isoformat()
        self.market = OfficialCloseMarket(self.database)

    @staticmethod
    def session_day(bundle: Mapping[str, object]) -> Optional[str]:
        start = ((bundle.get("trading_status_bundle") or {}).get("target_session") or {}).get("start")
        if not start:
            return None
        return _time(start, "target_session.start").astimezone(TAIPEI).date().isoformat()

    def first_trading_day_after(self, cutoff: str) -> Optional[str]:
        """cutoff 後第一個已有官方列、且該列在 ``as_of`` 前抓到的交易日（``execution_date`` 的 as_of 版本）。"""
        cutoff_time = _time(cutoff, "decision_cutoff")
        sources = [item for group in OFFICIAL_SOURCES.values() for item in group]
        connection = sqlite3.connect("file:%s?mode=ro" % self.database, uri=True)
        try:
            rows = connection.execute(
                "SELECT DISTINCT trade_date FROM daily_prices WHERE source IN (%s) AND trade_date >= ? AND fetched_at <= ?"
                " ORDER BY trade_date" % ",".join("?" * len(sources)),
                (*sources, cutoff_time.astimezone(TAIPEI).date().isoformat(), self.as_of),
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            if OfficialCloseMarket.close_at(str(row[0])) > cutoff_time:
                return str(row[0])
        return None

    def official_days(self, after: str, before: str) -> List[str]:
        """(after, before) 之間、``as_of`` 前已有官方列的交易日；用來找沒有決策的缺日。"""
        sources = [item for group in OFFICIAL_SOURCES.values() for item in group]
        connection = sqlite3.connect("file:%s?mode=ro" % self.database, uri=True)
        try:
            rows = connection.execute(
                "SELECT DISTINCT trade_date FROM daily_prices WHERE source IN (%s) AND trade_date > ? AND trade_date < ?"
                " AND fetched_at <= ? ORDER BY trade_date" % ",".join("?" * len(sources)),
                (*sources, after, before, self.as_of),
            ).fetchall()
        finally:
            connection.close()
        return [str(row[0]) for row in rows]

    def build(self, trade_date: str, symbols: Sequence[str]) -> Optional[Tuple[Dict[str, object], Dict[str, object]]]:
        """(ExecutionMarketData, CloseMarketData)；``as_of`` 前尚未到齊時 None；前視價格直接拒絕。"""
        try:
            markets = self.market.build(trade_date, list(symbols), self.as_of)
        except VirtualAccountError as error:
            raise ReplayError("%s 官方行情互相衝突：%s" % (trade_date, error)) from error
        if markets is None:
            return None
        close_at = OfficialCloseMarket.close_at(trade_date)
        for symbol, row in markets[1]["quotes"].items():
            if _time(row["fetched_at"], "%s.fetched_at" % symbol) < close_at:
                raise ReplayError("前視價格：%s 的 %s 官方收盤在該日收盤前就已抓到（%s）" % (trade_date, symbol, row["fetched_at"]))
        return markets

    @staticmethod
    def settlement_date(trade_date: str) -> str:
        """T+2 交割日，以週一至週五近似（與帳本未提供官方日曆時相同），報告標示 basis。"""
        return add_business_days(date.fromisoformat(trade_date), SETTLEMENT_BUSINESS_DAYS).isoformat()
