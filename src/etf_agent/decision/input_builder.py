"""Assemble a point-in-time DecisionInputBundle from a ResearchSnapshot."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
from typing import Dict, List, Mapping, Optional, Sequence

from .contracts import (
    DECISION_SCHEMA_VERSION,
    DecisionInputValidator,
    DecisionToolError,
    canonical_sha256,
    decimal_value,
    decision_bundle_sha256,
    decision_rules_sha256,
)


DEFAULT_LOOKBACK_BARS = 120
_PRICE_QUANTUM = Decimal("0.0001")


class DecisionInputBuilder:
    """Combine Snapshot, stored history, rules and optional inputs without new facts.

    歷史行情取自與 Snapshot 同一 cutoff 前可得的資料；最後一根 K 線直接使用 Snapshot
    的最新行情與其證據，確保 price_series 與 Snapshot 一致。有還原收盤價的來源會以
    同一比例還原開高低價，避免與收盤價混用不同基準。
    """

    def __init__(
        self,
        snapshot: Mapping[str, object],
        rules: Mapping[str, object],
        history_rows: Sequence[Mapping[str, object]],
        research_results: Sequence[Mapping[str, object]] = (),
        trading_status: Optional[Mapping[str, object]] = None,
        account_snapshot: Optional[Mapping[str, object]] = None,
    ):
        self.snapshot = dict(snapshot)
        self.rules = dict(rules)
        self.history_rows = list(history_rows)
        self.research_results = [dict(item) for item in research_results]
        self.trading_status = dict(trading_status) if trading_status is not None else None
        self.account_snapshot = dict(account_snapshot) if account_snapshot is not None else None

    def build(self) -> Dict[str, object]:
        snapshot_id = self.snapshot.get("snapshot_id")
        cutoff = self.snapshot.get("decision_cutoff")
        if not isinstance(snapshot_id, str) or not isinstance(cutoff, str):
            raise DecisionToolError("Snapshot 缺少 snapshot_id 或 decision_cutoff")
        rules = dict(self.rules)
        rules.pop("config_sha256", None)
        rules["config_sha256"] = decision_rules_sha256(rules)
        bundle: Dict[str, object] = {
            "schema_version": DECISION_SCHEMA_VERSION,
            "bundle_id": "decision-input:%s" % snapshot_id,
            "snapshot_id": snapshot_id,
            "decision_cutoff": cutoff,
            "snapshot_sha256": canonical_sha256(self.snapshot),
            "snapshot": self.snapshot,
            "rules": rules,
            "benchmarks": [],
            "price_series": self._price_series(),
            "research_results": self.research_results,
            "perception_inputs": [],
        }
        if self.account_snapshot is not None:
            bundle["account_snapshot"] = self.account_snapshot
        if self.trading_status is not None:
            bundle["trading_status_bundle"] = self.trading_status.get("bundle")
            bundle["tradability_assessment"] = self.trading_status.get("assessment")
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        return bundle

    def validate(self, bundle: Mapping[str, object]) -> List[str]:
        """未附帳戶時為樣板，忽略帳戶欄位錯誤；帳戶由 attach-account 補入後再完整驗證。"""
        errors = DecisionInputValidator(bundle).validate()
        if "account_snapshot" in bundle:
            return errors
        return [error for error in errors if not error.startswith("account_snapshot")]

    def _price_series(self) -> List[Dict[str, object]]:
        fetched_at = {
            str(item.get("evidence_id")): item.get("fetched_at")
            for item in self.snapshot.get("source_evidence", [])
            if isinstance(item, Mapping)
        }
        history: Dict[str, List[Mapping[str, object]]] = {}
        for row in self.history_rows:
            history.setdefault(str(row["symbol"]).upper(), []).append(row)
        series: List[Dict[str, object]] = []
        for latest in self.snapshot.get("latest_prices", []):
            if not isinstance(latest, Mapping):
                continue
            symbol = str(latest.get("symbol", "")).upper()
            evidence_id = str(latest.get("source_evidence_id", ""))
            latest_date = str(latest.get("trade_date", ""))
            bars = [
                self._bar(row, str(row["fetched_at"]))
                for row in sorted(history.get(symbol, []), key=lambda row: str(row["trade_date"]))
                if str(row["trade_date"]) < latest_date
            ]
            bars.append(
                self._bar(
                    {
                        "symbol": symbol,
                        "trade_date": latest_date,
                        "open_price": latest.get("open_price"),
                        "high_price": latest.get("high_price"),
                        "low_price": latest.get("low_price"),
                        "close_price": latest.get("close_price"),
                        "adjusted_close_price": latest.get("adjusted_close_price"),
                        "volume_shares": latest.get("volume_shares"),
                    },
                    fetched_at.get(evidence_id),
                )
            )
            series.append(
                {
                    "symbol": symbol,
                    "evidence_id": evidence_id,
                    "series_sha256": canonical_sha256(bars),
                    "bars": bars,
                }
            )
        return series

    @staticmethod
    def _bar(row: Mapping[str, object], available_at: object) -> Dict[str, object]:
        symbol = str(row.get("symbol", ""))
        trade_date = str(row.get("trade_date", ""))
        label = "%s %s" % (symbol, trade_date)
        if not isinstance(available_at, str) or not available_at:
            raise DecisionToolError("%s 缺少行情可得時間" % label)
        raw: Dict[str, Decimal] = {}
        for field in ("open_price", "high_price", "low_price", "close_price"):
            if row.get(field) in (None, ""):
                # 缺少開高低價時不以收盤價補值；該股票需先補齊資料來源。
                raise DecisionToolError("%s 缺少 %s，無法建立 OHLC 行情" % (label, field))
            raw[field] = decimal_value(row.get(field), "%s.%s" % (label, field))
        adjusted = row.get("adjusted_close_price")
        if adjusted in (None, ""):
            values = {field: str(row[field]) for field in raw}
        else:
            adjusted_close = decimal_value(adjusted, "%s.adjusted_close_price" % label)
            close = raw["close_price"]
            if close <= 0:
                raise DecisionToolError("%s close_price 必須大於 0" % label)
            values = {
                field: str((value * adjusted_close / close).quantize(_PRICE_QUANTUM, ROUND_HALF_EVEN))
                for field, value in raw.items()
                if field != "close_price"
            }
            values["close_price"] = str(adjusted)
        try:
            volume = int(row.get("volume_shares"))
        except (TypeError, ValueError) as error:
            raise DecisionToolError("%s volume_shares 無效" % label) from error
        return {
            "trade_date": trade_date,
            "available_at": available_at,
            "open": values["open_price"],
            "high": values["high_price"],
            "low": values["low_price"],
            "close": values["close_price"],
            "volume": volume,
        }
