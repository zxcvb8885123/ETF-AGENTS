"""每日帳本推進：以官方收盤價結算前一份決策，再建立下一次決策前狀態。

競賽口徑：決策在收盤後至隔日 08:55 前提交，於下一個交易日以收盤價成交；
手續費、證交稅、整張單位與賣款可否再用皆沿用 Decision run 內已驗證的 rules。
只使用 TWSE／TPEx 官方未還原收盤價，不以 Yahoo 等還原價格成交。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.decision.contracts import parse_time
from etf_agent.decision.finalization import DecisionRepository

from .service import VirtualAccountError, VirtualAccountRepository, VirtualAccountService

TAIPEI = timezone(timedelta(hours=8))
CLOSE_TIME = "13:30:00"
OFFICIAL_SOURCES: Mapping[str, Tuple[str, ...]] = {
    # 同一市場依優先序取第一筆。
    "TWSE": ("TWSE_STOCK_DAY", "TWSE_STOCK_DAY_ALL"),
    "TPEX": ("TPEX_TRADING_STOCK", "TPEX_MAINBOARD_QUOTES"),
}


def _market(symbol: str) -> str:
    return "TPEX" if symbol.upper().endswith(".TWO") else "TWSE"


def add_business_days(day: date, count: int) -> date:
    """以週一至週五近似 T+N 交割日；國定假日不在此計入。"""
    current = day
    remaining = count
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def find_decision_run(decision_repository: Path, account_snapshot: Mapping[str, object]) -> Optional[str]:
    """找出使用本次 prepare AccountSnapshot 的封存 Decision run；多筆即拒絕。"""
    root = Path(decision_repository)
    if not root.is_dir():
        return None
    repository = DecisionRepository(root)
    matches: List[str] = []
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir() or run_dir.name.startswith(".") or not (run_dir / "decision_input.json").exists():
            continue
        try:
            bundle = json.loads((run_dir / "decision_input.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(bundle, Mapping) and bundle.get("account_snapshot") == account_snapshot:
            repository.verify(run_dir.name)
            matches.append(run_dir.name)
    if len(matches) > 1:
        raise VirtualAccountError("多個 Decision run 使用同一 AccountSnapshot：%s" % "、".join(matches))
    return matches[0] if matches else None


class OfficialCloseMarket:
    """從 SQLite 讀取官方收盤價，產生模擬成交與日終行情輸入。"""

    def __init__(self, database: Path):
        self.database = Path(database)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect("file:%s?mode=ro" % self.database, uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def execution_date(self, cutoff: str) -> Optional[str]:
        """決策 cutoff 後第一個已有官方收盤資料的交易日。"""
        cutoff_time = parse_time(cutoff, "decision_cutoff")
        sources = [item for group in OFFICIAL_SOURCES.values() for item in group]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT trade_date FROM daily_prices WHERE source IN (%s) AND trade_date >= ? ORDER BY trade_date"
                % ",".join("?" * len(sources)),
                (*sources, cutoff_time.astimezone(TAIPEI).date().isoformat()),
            ).fetchall()
        for row in rows:
            if self.close_at(row["trade_date"]) > cutoff_time:
                return str(row["trade_date"])
        return None

    @staticmethod
    def close_at(trade_date: str) -> datetime:
        return datetime.fromisoformat("%sT%s+08:00" % (trade_date, CLOSE_TIME))

    def quotes(self, trade_date: str) -> Dict[str, Mapping[str, object]]:
        result: Dict[str, Mapping[str, object]] = {}
        with self._connect() as connection:
            for market, sources in OFFICIAL_SOURCES.items():
                for source in reversed(sources):  # 高優先來源最後寫入以覆蓋
                    for row in connection.execute(
                        """
                        SELECT daily_prices.symbol, daily_prices.close_price, daily_prices.volume_shares,
                               daily_prices.source, daily_prices.fetched_at, raw_payloads.sha256 AS raw_sha256
                        FROM daily_prices JOIN raw_payloads ON raw_payloads.id = daily_prices.raw_payload_id
                        WHERE daily_prices.trade_date = ? AND daily_prices.source = ?
                        """,
                        (trade_date, source),
                    ):
                        if _market(row["symbol"]) == market:
                            result[str(row["symbol"]).upper()] = dict(row)
        return result

    def build(self, trade_date: str, required_symbols: Sequence[str]) -> Optional[Tuple[Dict[str, object], Dict[str, object]]]:
        """回傳 (ExecutionMarketData, CloseMarketData)；所需市場尚未公布時回傳 None。"""
        rows = self.quotes(trade_date)
        published = {_market(symbol) for symbol in rows}
        if not rows or not {_market(symbol) for symbol in required_symbols} <= published:
            return None
        execution_at = self.close_at(trade_date).isoformat()
        execution_quotes: Dict[str, object] = {}
        close_quotes: Dict[str, object] = {}
        fetched: List[datetime] = []
        for symbol, row in sorted(rows.items()):
            lots = int(row["volume_shares"]) // 1000
            evidence = {"source": row["source"], "fetched_at": row["fetched_at"], "raw_sha256": row["raw_sha256"]}
            execution_quotes[symbol] = {"tradable": lots > 0, "available_lots": lots, "execution_price": str(row["close_price"]), **evidence}
            close_quotes[symbol] = {"close_price": str(row["close_price"]), **evidence}
            fetched.append(parse_time(row["fetched_at"], "%s.fetched_at" % symbol))
        execution = {
            "price_basis": "unadjusted", "price_rule": "official_close", "trade_date": trade_date,
            "execution_at": execution_at, "available_at": execution_at, "quotes": execution_quotes,
        }
        close = {
            "price_basis": "unadjusted", "trade_date": trade_date,
            "available_at": max(fetched).isoformat(), "quotes": close_quotes,
        }
        return execution, close


class DailyAccountRunner:
    """每日推進帳本；每一步都交由 VirtualAccountService 完整驗證。"""

    def __init__(self, repository: VirtualAccountRepository, database: Path, decision_repository: Path, settlement_days: int = 2):
        self.repository = repository
        self.service = VirtualAccountService(repository)
        self.market = OfficialCloseMarket(database)
        self.decision_repository = Path(decision_repository)
        self.settlement_days = settlement_days

    def settle(self) -> Dict[str, object]:
        """若最新狀態是已有 Decision run 的 prepare，則以執行日收盤價結算。"""
        current = self.repository.latest()
        state = current["state"]
        provenance = state.get("provenance", {})
        if provenance.get("type") != "prepared":
            return {"status": "nothing_pending", "run_id": current["run_id"]}
        decision_run_id = find_decision_run(self.decision_repository, provenance.get("account_snapshot"))
        if decision_run_id is None:
            return {"status": "no_decision", "run_id": current["run_id"]}
        trade_date = self.market.execution_date(state["as_of"])
        if trade_date is None:
            return {"status": "waiting_for_close_data", "run_id": current["run_id"], "decision_run_id": decision_run_id}
        decision = json.loads((self.decision_repository / decision_run_id / "decision.json").read_text(encoding="utf-8"))
        symbols = sorted({str(row["symbol"]).upper() for row in state.get("positions", [])}
                         | {str(row.get("symbol", "")).upper() for row in decision.get("orders", [])})
        markets = self.market.build(trade_date, symbols)
        if markets is None:
            return {"status": "waiting_for_close_data", "run_id": current["run_id"], "decision_run_id": decision_run_id, "trade_date": trade_date}
        missing = [row["symbol"] for row in state.get("positions", []) if str(row["symbol"]).upper() not in markets[1]["quotes"]]
        if missing:
            raise VirtualAccountError("%s 缺少持股官方收盤價：%s" % (trade_date, "、".join(missing)))
        run_dir = self.repository.root / "inputs" / ("close-" + trade_date)
        run_dir.mkdir(parents=True, exist_ok=True)
        execution_path, close_path = run_dir / "execution_market.json", run_dir / "close_market.json"
        for path, payload in ((execution_path, markets[0]), (close_path, markets[1])):
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        settlement = add_business_days(date.fromisoformat(trade_date), self.settlement_days).isoformat()
        result = self.service.apply_decision(
            self.decision_repository, decision_run_id, execution_path, close_path,
            settlement, ("close-%s-%s" % (trade_date, decision_run_id))[:80],
        )
        return {"status": "settled", "run_id": result["run_id"], "decision_run_id": decision_run_id,
                "trade_date": trade_date, "settlement_date": settlement, "nav": result["state"]["nav"],
                "fills": len(result["transition"]["execution"]["fills"]),
                "unfilled": len(result["transition"]["execution"]["unfilled_orders"])}

    def run(self, snapshot_path: Path, prepare_run_id: str, account_output: Path) -> Dict[str, object]:
        settled = self.settle()
        if settled["status"] == "waiting_for_close_data":
            # 已有待成交決策時不得建立新的 prepare，否則會覆蓋尚未結算的決策。
            return {"settle": settled, "prepare": {"status": "blocked_by_pending_decision"}}
        prepared = self.service.prepare_day(snapshot_path, prepare_run_id)
        account_output.parent.mkdir(parents=True, exist_ok=True)
        account_output.write_text(json.dumps(prepared["account_snapshot"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"settle": settled, "prepare": {"status": "prepared", "run_id": prepared["run_id"],
                                               "nav": prepared["state"]["nav"], "account_snapshot": str(account_output)}}
