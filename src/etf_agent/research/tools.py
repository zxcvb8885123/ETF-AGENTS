"""Deterministic, point-in-time tools for the Event Research Agent."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from etf_agent.data import MarketDataDatabase
from .contracts import ResearchToolError, _parse_time, _required_string
from .repository import HistoricalPriceFeatureRepository
from .validator import ResearchDebateValidator, ResearchResultValidator


class SnapshotResearchTools:
    """Read-only research tools constrained to one immutable snapshot."""

    def __init__(
        self,
        snapshot: Mapping[str, object],
        price_repository: Optional[HistoricalPriceFeatureRepository] = None,
    ):
        self.snapshot = dict(snapshot)
        self.price_repository = price_repository
        self.snapshot_id = _required_string(self.snapshot, "snapshot_id")
        self.decision_cutoff = _required_string(self.snapshot, "decision_cutoff")
        self.cutoff_time = _parse_time(self.decision_cutoff, "decision_cutoff")
        if self.snapshot.get("usable") is not True:
            raise ResearchToolError(
                "Snapshot 不可用：%s" % self.snapshot.get("quality_flags", [])
            )
        documents = self.snapshot.get("documents", [])
        prices = self.snapshot.get("latest_prices", [])
        evidence = self.snapshot.get("source_evidence", [])
        if not isinstance(documents, list) or not isinstance(prices, list):
            raise ResearchToolError("Snapshot documents/latest_prices 格式錯誤")
        if not isinstance(evidence, list) or not evidence:
            raise ResearchToolError(
                "Snapshot 缺少 source_evidence；請用目前版本的 Data Agent 重新建立快照"
            )
        self.documents = [dict(item) for item in documents if isinstance(item, Mapping)]
        self.prices = [dict(item) for item in prices if isinstance(item, Mapping)]
        self.evidence = [dict(item) for item in evidence if isinstance(item, Mapping)]
        self._evidence_ids = {
            str(item.get("evidence_id")) for item in self.evidence if item.get("evidence_id")
        }

    @classmethod
    def from_paths(cls, snapshot_path: Path, database_path: Path):
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ResearchToolError("無法讀取 Snapshot：%s" % error) from error
        return cls(
            snapshot,
            HistoricalPriceFeatureRepository(MarketDataDatabase(database_path)),
        )

    def status(self) -> Dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "decision_cutoff": self.decision_cutoff,
            "usable": True,
            "quality_flags": list(self.snapshot.get("quality_flags", [])),
            "universe_size": int(self.snapshot.get("universe_size", 0)),
            "latest_trade_date": self.snapshot.get("latest_trade_date"),
            "document_count": len(self.documents),
            "evidence_count": len(self.evidence),
        }

    def list_events(
        self,
        *,
        symbol: Optional[str] = None,
        document_type: Optional[str] = None,
        lookback_days: int = 45,
        limit: int = 100,
    ) -> List[Dict[str, object]]:
        if lookback_days <= 0 or limit <= 0 or limit > 1000:
            raise ResearchToolError("lookback_days 必須為正數，limit 必須介於 1～1000")
        earliest = self.cutoff_time - timedelta(days=lookback_days)
        selected: List[Dict[str, object]] = []
        for document in self.documents:
            if symbol and str(document.get("symbol", "")).upper() != symbol.upper():
                continue
            if document_type and document.get("document_type") != document_type:
                continue
            published = _parse_time(document.get("published_at"), "published_at")
            if not earliest <= published <= self.cutoff_time:
                continue
            selected.append(self._event_summary(document))
        selected.sort(key=lambda item: (str(item["published_at"]), str(item["event_id"])), reverse=True)
        return selected[:limit]

    def read_source(self, evidence_id: str) -> Dict[str, object]:
        for document in self.documents:
            if document.get("source_evidence_id") == evidence_id:
                return {"kind": "document", "document": document, "evidence": self._evidence(evidence_id)}
        for price in self.prices:
            if price.get("source_evidence_id") == evidence_id:
                return {"kind": "price", "price": price, "evidence": self._evidence(evidence_id)}
        raise ResearchToolError("Snapshot 找不到 evidence_id：%s" % evidence_id)

    def get_company_facts(self, symbol: str, limit: int = 30) -> Dict[str, object]:
        rows = [
            document
            for document in self.documents
            if str(document.get("symbol", "")).upper() == symbol.upper()
        ]
        rows.sort(key=lambda item: str(item.get("published_at", "")), reverse=True)
        return {
            "symbol": symbol.upper(),
            "decision_cutoff": self.decision_cutoff,
            "documents": [self._event_summary(item, include_facts=True) for item in rows[:limit]],
        }

    def find_related_events(
        self,
        symbol: str,
        event_id: Optional[str] = None,
        limit: int = 30,
    ) -> Dict[str, object]:
        rows = [
            document
            for document in self.documents
            if str(document.get("symbol", "")).upper() == symbol.upper()
            and document.get("external_id") != event_id
        ]
        rows.sort(key=lambda item: str(item.get("published_at", "")), reverse=True)
        return {
            "symbol": symbol.upper(),
            "excluded_event_id": event_id,
            "related_events": [self._event_summary(item, include_facts=True) for item in rows[:limit]],
        }

    def get_price_features(
        self, symbol: str, event_published_at: Optional[str] = None
    ) -> Dict[str, object]:
        price = next(
            (
                item
                for item in self.prices
                if str(item.get("symbol", "")).upper() == symbol.upper()
            ),
            None,
        )
        if price is None:
            raise ResearchToolError("Snapshot 沒有此股票的最新行情：%s" % symbol.upper())
        if self.price_repository is None:
            raise ResearchToolError("未設定歷史行情 Repository")
        result = self.price_repository.get_features(
            symbol.upper(),
            self.decision_cutoff,
            self.snapshot.get("latest_trade_date"),
            event_published_at,
        )
        result["source_evidence_id"] = price.get("source_evidence_id")
        return result

    def analyze_event_context(self, evidence_id: str) -> Dict[str, object]:
        """Build a deterministic dossier; it supplies context, not an investment verdict."""
        source = self.read_source(evidence_id)
        if source["kind"] != "document":
            raise ResearchToolError("analyze-event-context 只接受正式文件 evidence_id")
        document = dict(source["document"])
        symbol = str(document.get("symbol", "")).upper()
        event_id = str(document.get("external_id", ""))
        related = self.find_related_events(symbol, event_id, limit=30)["related_events"]
        same_type = [
            item
            for item in related
            if item.get("document_type") == document.get("document_type")
        ]
        version = int(document.get("version") or 1)
        novelty = {
            "deterministic_hint": "correction" if version > 1 else "new",
            "version": version,
            "prior_same_type_count": len(same_type),
            "prior_event_ids": [str(item.get("event_id")) for item in same_type[:12]],
            "note": "此欄只描述版本與既有文件；語意上的新資訊仍須由研究者核對原文。",
        }
        reference_frames: List[Dict[str, object]] = []
        revenue = document.get("monthly_revenue")
        if isinstance(revenue, Mapping):
            period = revenue.get("revenue_period")
            unit = "TWD x %s" % revenue.get("unit_multiplier", 1)
            reference_frames.extend(
                [
                    {
                        "kind": "prior_month",
                        "period": period,
                        "actual": revenue.get("current_revenue"),
                        "reference": revenue.get("previous_month_revenue"),
                        "difference_pct": revenue.get("mom_pct"),
                        "unit": unit,
                        "is_market_expectation": False,
                        "evidence_ids": [evidence_id],
                    },
                    {
                        "kind": "prior_year_same_month",
                        "period": period,
                        "actual": revenue.get("current_revenue"),
                        "reference": revenue.get("previous_year_revenue"),
                        "difference_pct": revenue.get("yoy_pct"),
                        "unit": unit,
                        "is_market_expectation": False,
                        "evidence_ids": [evidence_id],
                    },
                    {
                        "kind": "prior_year_cumulative",
                        "period": period,
                        "actual": revenue.get("cumulative_revenue"),
                        "reference": revenue.get("previous_year_cumulative_revenue"),
                        "difference_pct": revenue.get("cumulative_yoy_pct"),
                        "unit": unit,
                        "is_market_expectation": False,
                        "evidence_ids": [evidence_id],
                    },
                ]
            )
        try:
            price_features: Optional[Dict[str, object]] = self.get_price_features(
                symbol, str(document.get("published_at"))
            )
        except ResearchToolError:
            price_features = None
        return {
            "event": self._event_summary(document, include_facts=True),
            "source": source,
            "reference_frames": reference_frames,
            "market_expectation_available": any(
                frame["is_market_expectation"] for frame in reference_frames
            ),
            "novelty_context": novelty,
            "related_events": related,
            "price_features": price_features,
            "research_warning": (
                "MoM／YoY 是歷史比較，不是市場預期差；不得只靠正負號判定方向。"
            ),
        }

    def validate_result(self, result: Mapping[str, object]) -> List[str]:
        return ResearchResultValidator(
            self.snapshot, price_repository=self.price_repository
        ).validate(result)

    def validate_debate_bundle(self, bundle: Mapping[str, object]) -> List[str]:
        return ResearchDebateValidator(self.snapshot).validate(bundle)

    def _evidence(self, evidence_id: str) -> Dict[str, object]:
        return next(
            item for item in self.evidence if item.get("evidence_id") == evidence_id
        )

    def _event_summary(
        self, document: Mapping[str, object], include_facts: bool = False
    ) -> Dict[str, object]:
        result: Dict[str, object] = {
            "event_id": document.get("external_id"),
            "symbol": document.get("symbol"),
            "document_type": document.get("document_type"),
            "title": document.get("title"),
            "published_at": document.get("published_at"),
            "available_at": document.get("available_at"),
            "source": document.get("source"),
            "source_evidence_id": document.get("source_evidence_id"),
            "version": document.get("version"),
        }
        if include_facts and document.get("monthly_revenue") is not None:
            result["monthly_revenue"] = document.get("monthly_revenue")
        return result
