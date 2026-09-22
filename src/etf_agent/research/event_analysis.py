"""Deterministic, point-in-time tools for the Event Research Agent."""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from etf_agent.data import MarketDataDatabase


DIRECTIONS = {"positive", "negative", "mixed", "neutral", "uncertain"}
RESEARCH_STATUSES = {"candidate", "pending", "excluded"}
RESULT_STATUSES = {"completed", "degraded", "failed"}
EVIDENCE_QUALITY = {"verified", "partial", "insufficient"}
NOVELTY_CLASSES = {"new", "update", "repeat", "correction", "unclear"}
MATERIALITY_LEVELS = {"high", "medium", "low", "unknown"}
HORIZON_STATES = {"within_competition", "after_competition", "unknown"}
DEBATE_OUTCOMES = {"bull", "bear", "balanced", "indeterminate"}
PRICE_RESPONSE_STATES = {"confirmed", "unconfirmed", "contradicted", "unavailable"}
SUBAGENT_ROLES = {"fact", "bull", "bear", "adjudicator"}
EVENT_TYPES = {
    "monthly_revenue",
    "earnings",
    "guidance",
    "material_event",
    "corporate_action",
    "contract",
    "m_and_a",
    "management",
    "product",
    "regulatory",
    "industry",
    "macro",
    "other",
}


class ResearchToolError(ValueError):
    """Raised when a research tool cannot safely answer the request."""


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ResearchToolError("%s 必須是包含時區的時間字串" % field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ResearchToolError("%s 無法解析：%s" % (field, value)) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchToolError("%s 必須包含時區" % field)
    return parsed.astimezone(timezone.utc)


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ResearchToolError("缺少必要字串欄位：%s" % field)
    return value.strip()


def _string_list(payload: Mapping[str, object], field: str) -> List[str]:
    value = payload.get(field)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ResearchToolError("%s 必須是非空字串陣列" % field)
    return [item.strip() for item in value]


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ResearchToolError("無法解析數值：%s" % value) from error


class HistoricalPriceFeatureRepository:
    """Read point-in-time prices and calculate reproducible research features."""

    def __init__(self, database: MarketDataDatabase):
        self.database = database

    def get_features(
        self,
        symbol: str,
        decision_cutoff: str,
        latest_trade_date: Optional[str],
        event_published_at: Optional[str] = None,
    ) -> Dict[str, object]:
        if not self.database.path.exists():
            raise ResearchToolError("行情資料庫不存在：%s" % self.database.path)
        cutoff = _parse_time(decision_cutoff, "decision_cutoff").isoformat()
        if not latest_trade_date:
            raise ResearchToolError("Snapshot 缺少 latest_trade_date")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                WITH eligible AS (
                    SELECT daily_prices.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY symbol, trade_date
                               ORDER BY CASE source
                                   WHEN 'TPEX_TRADING_STOCK' THEN 1
                                   WHEN 'TWSE_STOCK_DAY' THEN 1
                                   WHEN 'TWSE_STOCK_DAY_ALL' THEN 2
                                   WHEN 'TPEX_MAINBOARD_QUOTES' THEN 2
                                   WHEN 'YAHOO_FINANCE' THEN 3
                                   ELSE 9
                               END
                           ) AS source_rank
                    FROM daily_prices
                    WHERE symbol = ?
                      AND trade_date <= ?
                      AND fetched_at <= ?
                )
                SELECT * FROM eligible
                WHERE source_rank = 1
                ORDER BY trade_date DESC
                LIMIT 80
                """,
                (symbol.upper(), latest_trade_date, cutoff),
            ).fetchall()
        if not rows:
            raise ResearchToolError("找不到截止時間前的行情：%s" % symbol.upper())
        ordered = list(reversed(rows))
        closes = [_decimal(row["adjusted_close_price"] or row["close_price"]) for row in ordered]
        volumes = [int(row["volume_shares"]) for row in ordered]
        returns = [
            float(closes[index] / closes[index - 1] - Decimal("1"))
            for index in range(1, len(closes))
            if closes[index - 1] != 0
        ]
        latest = ordered[-1]
        latest_close = closes[-1]
        one_day = returns[-1] if returns else None
        ten_day = (
            float(latest_close / closes[-11] - Decimal("1"))
            if len(closes) >= 11 and closes[-11] != 0
            else None
        )
        high = _decimal(latest["high_price"] or latest["close_price"])
        low = _decimal(latest["low_price"] or latest["close_price"])
        close_location = (
            float((latest_close - low) / (high - low)) if high > low else None
        )
        prior_volumes = volumes[-21:-1]
        median_volume = statistics.median(prior_volumes) if prior_volumes else None
        volume_ratio = (
            volumes[-1] / median_volume if median_volume and median_volume > 0 else None
        )
        downside = [value for value in returns[-20:] if value < 0]
        downside_volatility = (
            statistics.pstdev(downside) if len(downside) >= 2 else None
        )
        true_ranges: List[float] = []
        for index in range(max(1, len(ordered) - 14), len(ordered)):
            row = ordered[index]
            row_high = _decimal(row["high_price"] or row["close_price"])
            row_low = _decimal(row["low_price"] or row["close_price"])
            previous = closes[index - 1]
            true_ranges.append(
                float(max(row_high - row_low, abs(row_high - previous), abs(row_low - previous)))
            )
        atr_14 = statistics.mean(true_ranges) if true_ranges else None
        result = {
            "symbol": symbol.upper(),
            "as_of": str(latest["trade_date"]),
            "source": str(latest["source"]),
            "observations": len(ordered),
            "analysis_close_price": str(latest_close),
            "return_1d": _round_optional(one_day),
            "return_10d": _round_optional(ten_day),
            "close_location": _round_optional(close_location),
            "volume_ratio_20d_median": _round_optional(volume_ratio),
            "atr_14": _round_optional(atr_14),
            "atr_14_pct": _round_optional(
                atr_14 / float(latest_close) if atr_14 is not None and latest_close else None
            ),
            "downside_volatility_20d": _round_optional(downside_volatility),
            "decision_cutoff": cutoff,
        }
        if event_published_at:
            result.update(
                self._event_relative_features(ordered, closes, volumes, event_published_at)
            )
        return result

    @staticmethod
    def _event_relative_features(
        rows: Sequence[Mapping[str, object]],
        closes: Sequence[Decimal],
        volumes: Sequence[int],
        event_published_at: str,
    ) -> Dict[str, object]:
        published = _parse_time(event_published_at, "event_published_at").astimezone(
            ZoneInfo("Asia/Taipei")
        )
        same_day_eligible = published.hour < 13 or (
            published.hour == 13 and published.minute < 30
        )
        event_index: Optional[int] = None
        for index, row in enumerate(rows):
            trade_date = str(row["trade_date"])
            if trade_date > published.date().isoformat() or (
                same_day_eligible and trade_date == published.date().isoformat()
            ):
                event_index = index
                break
        if event_index is None:
            return {
                "event_trade_date": None,
                "pre_event_return_5d": None,
                "event_day_return": None,
                "post_event_return_to_cutoff": None,
                "event_volume_ratio_20d_median": None,
            }
        previous_index = event_index - 1
        pre_index = event_index - 6
        pre_event_return = (
            float(closes[previous_index] / closes[pre_index] - Decimal("1"))
            if previous_index >= 0 and pre_index >= 0 and closes[pre_index] != 0
            else None
        )
        event_day_return = (
            float(closes[event_index] / closes[previous_index] - Decimal("1"))
            if previous_index >= 0 and closes[previous_index] != 0
            else None
        )
        post_event_return = (
            float(closes[-1] / closes[previous_index] - Decimal("1"))
            if previous_index >= 0 and closes[previous_index] != 0
            else None
        )
        prior_volumes = list(volumes[max(0, event_index - 20):event_index])
        median_volume = statistics.median(prior_volumes) if prior_volumes else None
        event_volume_ratio = (
            volumes[event_index] / median_volume
            if median_volume and median_volume > 0
            else None
        )
        return {
            "event_trade_date": str(rows[event_index]["trade_date"]),
            "pre_event_return_5d": _round_optional(pre_event_return),
            "event_day_return": _round_optional(event_day_return),
            "post_event_return_to_cutoff": _round_optional(post_event_return),
            "event_volume_ratio_20d_median": _round_optional(event_volume_ratio),
        }


def _round_optional(value: Optional[float]) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return round(value, 8)


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


class ResearchResultValidator:
    """Validate LLM-produced research against one snapshot and its evidence."""

    def __init__(
        self,
        snapshot: Mapping[str, object],
        price_repository: Optional[HistoricalPriceFeatureRepository] = None,
    ):
        self.snapshot = dict(snapshot)
        self.price_repository = price_repository
        self.cutoff = _parse_time(self.snapshot.get("decision_cutoff"), "decision_cutoff")
        self.documents = [
            dict(item)
            for item in self.snapshot.get("documents", [])
            if isinstance(item, Mapping)
        ]
        self.prices = [
            dict(item)
            for item in self.snapshot.get("latest_prices", [])
            if isinstance(item, Mapping)
        ]
        self.by_evidence: Dict[str, Tuple[str, Dict[str, object]]] = {}
        for document in self.documents:
            evidence_id = document.get("source_evidence_id")
            if evidence_id:
                self.by_evidence[str(evidence_id)] = ("document", document)
        for price in self.prices:
            evidence_id = price.get("source_evidence_id")
            if evidence_id:
                self.by_evidence[str(evidence_id)] = ("price", price)

    def validate(self, result: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        if self.snapshot.get("usable") is not True:
            errors.append("Snapshot 不可用，不能產生 ResearchResult")
        for field in ("schema_version", "run_id", "snapshot_id", "decision_cutoff", "skill_version", "status"):
            if not isinstance(result.get(field), str) or not str(result.get(field)).strip():
                errors.append("缺少必要字串欄位：%s" % field)
        if result.get("schema_version") != "2.1":
            errors.append("schema_version 必須為 2.1")
        if result.get("snapshot_id") != self.snapshot.get("snapshot_id"):
            errors.append("snapshot_id 與輸入 Snapshot 不一致")
        try:
            if _parse_time(result.get("decision_cutoff"), "decision_cutoff") != self.cutoff:
                errors.append("decision_cutoff 與輸入 Snapshot 不一致")
        except ResearchToolError as error:
            errors.append(str(error))
        if result.get("status") not in RESULT_STATUSES:
            errors.append("status 必須是 completed、degraded 或 failed")
        items = result.get("items")
        if not isinstance(items, list):
            errors.append("items 必須是陣列")
            return errors
        seen: set = set()
        for index, item in enumerate(items):
            prefix = "items[%d]" % index
            if not isinstance(item, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            self._validate_item(item, prefix, errors, seen)
        if result.get("status") == "completed" and result.get("errors"):
            errors.append("completed 結果不得同時包含 errors")
        return errors

    def _validate_item(
        self,
        item: Mapping[str, object],
        prefix: str,
        errors: List[str],
        seen: set,
    ) -> None:
        required = (
            "event_id", "symbol", "event_type", "published_at", "event_summary",
            "direction", "impact_mechanism", "research_status", "status_reason",
        )
        values: Dict[str, str] = {}
        for field in required:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append("%s.%s 缺少必要字串" % (prefix, field))
            else:
                values[field] = value.strip()
        key = (values.get("event_id"), values.get("symbol"))
        if key in seen:
            errors.append("%s 重複事件與股票組合" % prefix)
        seen.add(key)
        if values.get("event_type") not in EVENT_TYPES:
            errors.append("%s.event_type 不在允許清單" % prefix)
        if values.get("direction") not in DIRECTIONS:
            errors.append("%s.direction 不在允許清單" % prefix)
        if values.get("research_status") not in RESEARCH_STATUSES:
            errors.append("%s.research_status 不在允許清單" % prefix)
        try:
            if _parse_time(item.get("published_at"), "%s.published_at" % prefix) > self.cutoff:
                errors.append("%s 使用了 decision_cutoff 之後的事件" % prefix)
        except ResearchToolError as error:
            errors.append(str(error))
        lists: Dict[str, List[str]] = {}
        for field in ("evidence_ids", "counter_evidence_ids", "risk_flags", "uncertainties", "invalidation_signals"):
            try:
                values_list = _string_list(item, field)
            except ResearchToolError as error:
                errors.append("%s.%s" % (prefix, error))
                values_list = []
            lists[field] = values_list
            if field == "evidence_ids":
                self._validate_evidence(values_list, item, prefix, errors)
            elif field == "counter_evidence_ids":
                self._validate_related_references(values_list, item, prefix, errors)
        assessment = self._validate_assessment(item, prefix, errors)
        debate = self._validate_debate(item, prefix, errors)
        self._validate_research_process(item, prefix, errors)
        if values.get("research_status") == "candidate":
            self._validate_candidate_gates(
                values, assessment, debate, lists, item, prefix, errors
            )
        facts = item.get("fact_values")
        if not isinstance(facts, list):
            errors.append("%s.fact_values 必須是陣列" % prefix)
        else:
            for fact_index, fact in enumerate(facts):
                self._validate_fact(fact, "%s.fact_values[%d]" % (prefix, fact_index), errors)
        price_confirmation = item.get("price_confirmation")
        if not isinstance(price_confirmation, Mapping):
            errors.append("%s.price_confirmation 必須是物件" % prefix)
        else:
            state = price_confirmation.get("status")
            if state not in PRICE_RESPONSE_STATES:
                errors.append("%s.price_confirmation.status 不在允許清單" % prefix)
            if state != "unavailable":
                evidence_id = price_confirmation.get("source_evidence_id")
                linked = self.by_evidence.get(str(evidence_id)) if evidence_id else None
                if linked is None or linked[0] != "price":
                    errors.append("%s.price_confirmation 必須引用 Snapshot 行情證據" % prefix)
                elif str(linked[1].get("symbol", "")).upper() != str(item.get("symbol", "")).upper():
                    errors.append("%s.price_confirmation 引用不屬於該股票" % prefix)
                elif self.price_repository is not None:
                    try:
                        expected = self.price_repository.get_features(
                            str(item.get("symbol", "")).upper(),
                            str(self.snapshot.get("decision_cutoff")),
                            self.snapshot.get("latest_trade_date"),
                            str(item.get("published_at")),
                        )
                        expected["source_evidence_id"] = evidence_id
                        self._compare_price_confirmation(
                            price_confirmation, expected, prefix, errors
                        )
                    except ResearchToolError as error:
                        errors.append("%s.price_confirmation 無法重算：%s" % (prefix, error))

    @staticmethod
    def _validate_research_process(
        item: Mapping[str, object], prefix: str, errors: List[str]
    ) -> None:
        process = item.get("research_process")
        if not isinstance(process, Mapping):
            errors.append("%s.research_process 必須是物件" % prefix)
            return
        packet_ids: List[str] = []
        for field in (
            "fact_packet_id",
            "bull_packet_id",
            "bear_packet_id",
            "adjudication_packet_id",
        ):
            try:
                packet_ids.append(_required_string(process, field))
            except ResearchToolError as error:
                errors.append("%s.research_process.%s" % (prefix, error))
        if len(packet_ids) == 4 and len(set(packet_ids)) != 4:
            errors.append("%s.research_process 的四個 packet_id 必須不同" % prefix)
        if process.get("independence") != "bull_bear_independent":
            errors.append(
                "%s.research_process.independence 必須是 bull_bear_independent"
                % prefix
            )

    def _validate_assessment(
        self, item: Mapping[str, object], prefix: str, errors: List[str]
    ) -> Dict[str, object]:
        assessment = item.get("assessment")
        if not isinstance(assessment, Mapping):
            errors.append("%s.assessment 必須是物件" % prefix)
            return {}
        quality = assessment.get("evidence_quality")
        if quality not in EVIDENCE_QUALITY:
            errors.append("%s.assessment.evidence_quality 不在允許清單" % prefix)
        novelty = assessment.get("novelty")
        if not isinstance(novelty, Mapping):
            errors.append("%s.assessment.novelty 必須是物件" % prefix)
            novelty = {}
        else:
            if novelty.get("classification") not in NOVELTY_CLASSES:
                errors.append("%s.assessment.novelty.classification 不在允許清單" % prefix)
            for field in ("rationale",):
                try:
                    _required_string(novelty, field)
                except ResearchToolError as error:
                    errors.append("%s.assessment.novelty.%s" % (prefix, error))
            try:
                _string_list(novelty, "prior_event_ids")
            except ResearchToolError as error:
                errors.append("%s.assessment.novelty.%s" % (prefix, error))
        frames = assessment.get("reference_frames")
        if not isinstance(frames, list):
            errors.append("%s.assessment.reference_frames 必須是陣列" % prefix)
            frames = []
        for frame_index, frame in enumerate(frames):
            frame_prefix = "%s.assessment.reference_frames[%d]" % (prefix, frame_index)
            if not isinstance(frame, Mapping):
                errors.append("%s 必須是物件" % frame_prefix)
                continue
            for field in ("kind", "unit"):
                try:
                    _required_string(frame, field)
                except ResearchToolError as error:
                    errors.append("%s.%s" % (frame_prefix, error))
            if not isinstance(frame.get("is_market_expectation"), bool):
                errors.append("%s.is_market_expectation 必須是布林值" % frame_prefix)
            try:
                evidence_ids = _string_list(frame, "evidence_ids")
                self._validate_related_references(evidence_ids, item, frame_prefix, errors)
            except ResearchToolError as error:
                errors.append("%s.%s" % (frame_prefix, error))
        materiality = assessment.get("materiality")
        if not isinstance(materiality, Mapping):
            errors.append("%s.assessment.materiality 必須是物件" % prefix)
            materiality = {}
        else:
            if materiality.get("level") not in MATERIALITY_LEVELS:
                errors.append("%s.assessment.materiality.level 不在允許清單" % prefix)
            if materiality.get("horizon") not in HORIZON_STATES:
                errors.append("%s.assessment.materiality.horizon 不在允許清單" % prefix)
            for field in ("affected_metrics", "causal_chain"):
                if not isinstance(materiality.get(field), list):
                    errors.append("%s.assessment.materiality.%s 必須是陣列" % (prefix, field))
            try:
                _required_string(materiality, "rationale")
            except ResearchToolError as error:
                errors.append("%s.assessment.materiality.%s" % (prefix, error))
            chain = materiality.get("causal_chain", [])
            if isinstance(chain, list):
                for link_index, link in enumerate(chain):
                    link_prefix = "%s.assessment.materiality.causal_chain[%d]" % (prefix, link_index)
                    if not isinstance(link, Mapping):
                        errors.append("%s 必須是物件" % link_prefix)
                        continue
                    for field in ("from", "to", "relationship"):
                        try:
                            _required_string(link, field)
                        except ResearchToolError as error:
                            errors.append("%s.%s" % (link_prefix, error))
                    if link.get("support") not in {"fact", "inference"}:
                        errors.append("%s.support 必須是 fact 或 inference" % link_prefix)
                    try:
                        refs = _string_list(link, "evidence_ids")
                        self._validate_related_references(refs, item, link_prefix, errors)
                    except ResearchToolError as error:
                        errors.append("%s.%s" % (link_prefix, error))
        return dict(assessment)

    def _validate_debate(
        self, item: Mapping[str, object], prefix: str, errors: List[str]
    ) -> Dict[str, object]:
        debate = item.get("debate")
        if not isinstance(debate, Mapping):
            errors.append("%s.debate 必須是物件" % prefix)
            return {}
        for case_name in ("bull_case", "bear_case"):
            case = debate.get(case_name)
            case_prefix = "%s.debate.%s" % (prefix, case_name)
            if not isinstance(case, Mapping):
                errors.append("%s 必須是物件" % case_prefix)
                continue
            try:
                _required_string(case, "thesis")
                refs = _string_list(case, "evidence_ids")
                _string_list(case, "assumptions")
                _string_list(case, "failure_conditions")
                self._validate_related_references(refs, item, case_prefix, errors)
            except ResearchToolError as error:
                errors.append("%s.%s" % (case_prefix, error))
        adjudication = debate.get("adjudication")
        if not isinstance(adjudication, Mapping):
            errors.append("%s.debate.adjudication 必須是物件" % prefix)
        else:
            if adjudication.get("prevailing_case") not in DEBATE_OUTCOMES:
                errors.append("%s.debate.adjudication.prevailing_case 不在允許清單" % prefix)
            try:
                _required_string(adjudication, "rationale")
                _string_list(adjudication, "surviving_claims")
                _string_list(adjudication, "rejected_claims")
                _string_list(adjudication, "unresolved_questions")
            except ResearchToolError as error:
                errors.append("%s.debate.adjudication.%s" % (prefix, error))
        return dict(debate)

    @staticmethod
    def _validate_candidate_gates(
        values: Mapping[str, str],
        assessment: Mapping[str, object],
        debate: Mapping[str, object],
        lists: Mapping[str, List[str]],
        item: Mapping[str, object],
        prefix: str,
        errors: List[str],
    ) -> None:
        if values.get("direction") not in {"positive", "negative"}:
            errors.append("%s candidate 必須有 positive 或 negative 方向" % prefix)
        if not lists.get("invalidation_signals"):
            errors.append("%s candidate 必須提供 invalidation_signals" % prefix)
        if assessment.get("evidence_quality") != "verified":
            errors.append("%s candidate 必須有 verified 證據品質" % prefix)
        novelty = assessment.get("novelty", {})
        if not isinstance(novelty, Mapping) or novelty.get("classification") in {"repeat", "unclear", None}:
            errors.append("%s candidate 必須確認事件不是重複或不明舊聞" % prefix)
        frames = assessment.get("reference_frames", [])
        if not isinstance(frames, list) or not frames:
            errors.append("%s candidate 必須至少有一個比較或預期基準" % prefix)
        elif not any(
            isinstance(frame, Mapping) and frame.get("is_market_expectation") is True
            for frame in frames
        ) and "NO_MARKET_EXPECTATION" not in lists.get("risk_flags", []):
            errors.append("%s 無市場預期資料時必須標記 NO_MARKET_EXPECTATION" % prefix)
        materiality = assessment.get("materiality", {})
        if not isinstance(materiality, Mapping) or materiality.get("level") not in {"high", "medium"}:
            errors.append("%s candidate 的 materiality 必須是 high 或 medium" % prefix)
        if not isinstance(materiality, Mapping) or materiality.get("horizon") != "within_competition":
            errors.append("%s candidate 必須說明影響位於比賽期間內" % prefix)
        if not isinstance(materiality, Mapping) or not materiality.get("causal_chain"):
            errors.append("%s candidate 必須提供財務傳導鏈" % prefix)
        adjudication = debate.get("adjudication", {})
        prevailing = adjudication.get("prevailing_case") if isinstance(adjudication, Mapping) else None
        expected = "bull" if values.get("direction") == "positive" else "bear"
        if prevailing != expected:
            errors.append("%s candidate 方向必須與多空裁決一致" % prefix)
        price = item.get("price_confirmation", {})
        if not isinstance(price, Mapping) or price.get("status") in {"contradicted", "unavailable", None}:
            errors.append("%s candidate 不得缺少行情或與價格反應矛盾" % prefix)

    def _validate_evidence(
        self,
        evidence_ids: Sequence[str],
        item: Mapping[str, object],
        prefix: str,
        errors: List[str],
    ) -> None:
        if not evidence_ids:
            errors.append("%s.evidence_ids 不得為空" % prefix)
            return
        matching_document = False
        for evidence_id in evidence_ids:
            linked = self.by_evidence.get(evidence_id)
            if linked is None:
                errors.append("%s 引用不存在：%s" % (prefix, evidence_id))
                continue
            kind, payload = linked
            if str(payload.get("symbol", "")).upper() != str(item.get("symbol", "")).upper():
                errors.append("%s 引用 %s 不屬於該股票" % (prefix, evidence_id))
            if kind == "document" and payload.get("external_id") == item.get("event_id"):
                matching_document = True
                if payload.get("published_at") != item.get("published_at"):
                    errors.append("%s.published_at 與事件來源不一致" % prefix)
        if not matching_document:
            errors.append("%s.event_id 必須對應至少一個正式文件引用" % prefix)

    def _validate_related_references(
        self,
        evidence_ids: Sequence[str],
        item: Mapping[str, object],
        prefix: str,
        errors: List[str],
    ) -> None:
        for evidence_id in evidence_ids:
            linked = self.by_evidence.get(evidence_id)
            if linked is None:
                errors.append("%s 反證引用不存在：%s" % (prefix, evidence_id))
            elif str(linked[1].get("symbol", "")).upper() != str(item.get("symbol", "")).upper():
                errors.append("%s 反證引用 %s 不屬於該股票" % (prefix, evidence_id))

    @staticmethod
    def _compare_price_confirmation(
        actual: Mapping[str, object],
        expected: Mapping[str, object],
        prefix: str,
        errors: List[str],
    ) -> None:
        allowed = {
            "status",
            "interpretation",
            "as_of",
            "analysis_close_price",
            "return_1d",
            "return_10d",
            "close_location",
            "volume_ratio_20d_median",
            "atr_14",
            "atr_14_pct",
            "downside_volatility_20d",
            "event_trade_date",
            "pre_event_return_5d",
            "event_day_return",
            "post_event_return_to_cutoff",
            "event_volume_ratio_20d_median",
            "source_evidence_id",
        }
        unknown = set(actual) - allowed
        if unknown:
            errors.append(
                "%s.price_confirmation 含未核准欄位：%s"
                % (prefix, ", ".join(sorted(unknown)))
            )
        for field, value in actual.items():
            if field in allowed and field not in {"status", "interpretation"} and value != expected.get(field):
                errors.append(
                    "%s.price_confirmation.%s 與確定性工具結果不一致"
                    % (prefix, field)
                )

    def _validate_fact(
        self, fact: object, prefix: str, errors: List[str]
    ) -> None:
        if not isinstance(fact, Mapping):
            errors.append("%s 必須是物件" % prefix)
            return
        try:
            _required_string(fact, "name")
            value = _required_string(fact, "value")
            _required_string(fact, "unit")
            _required_string(fact, "period")
            evidence_id = _required_string(fact, "evidence_id")
        except ResearchToolError as error:
            errors.append("%s.%s" % (prefix, error))
            return
        linked = self.by_evidence.get(evidence_id)
        if linked is None or linked[0] != "document":
            errors.append("%s.evidence_id 必須引用正式文件" % prefix)
            return
        haystack = _flatten_strings(linked[1])
        if value not in haystack:
            errors.append("%s.value 無法在引用文件中核對：%s" % (prefix, value))


class ResearchDebateValidator:
    """Validate one event's fact → independent bull/bear → adjudication bundle."""

    def __init__(self, snapshot: Mapping[str, object]):
        self.snapshot = dict(snapshot)
        self.cutoff = _parse_time(self.snapshot.get("decision_cutoff"), "decision_cutoff")
        self.by_evidence: Dict[str, Tuple[str, Dict[str, object]]] = {}
        for kind, rows in (
            ("document", self.snapshot.get("documents", [])),
            ("price", self.snapshot.get("latest_prices", [])),
        ):
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, Mapping) and row.get("source_evidence_id"):
                    self.by_evidence[str(row["source_evidence_id"])] = (kind, dict(row))

    def validate(self, bundle: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        for field in ("run_id", "snapshot_id", "decision_cutoff", "event_id", "symbol"):
            try:
                _required_string(bundle, field)
            except ResearchToolError as error:
                errors.append(str(error))
        if bundle.get("schema_version") != "1.0":
            errors.append("debate bundle schema_version 必須為 1.0")
        if bundle.get("snapshot_id") != self.snapshot.get("snapshot_id"):
            errors.append("debate bundle snapshot_id 與 Snapshot 不一致")
        try:
            if _parse_time(bundle.get("decision_cutoff"), "decision_cutoff") != self.cutoff:
                errors.append("debate bundle decision_cutoff 與 Snapshot 不一致")
        except ResearchToolError as error:
            errors.append(str(error))
        packets = bundle.get("packets")
        if not isinstance(packets, list):
            errors.append("packets 必須是陣列")
            return errors
        by_role: Dict[str, Mapping[str, object]] = {}
        by_id: Dict[str, Mapping[str, object]] = {}
        for index, packet in enumerate(packets):
            prefix = "packets[%d]" % index
            if not isinstance(packet, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            packet_id = str(packet.get("packet_id", "")).strip()
            role = str(packet.get("role", "")).strip()
            if not packet_id:
                errors.append("%s.packet_id 缺少必要字串" % prefix)
            elif packet_id in by_id:
                errors.append("%s.packet_id 重複：%s" % (prefix, packet_id))
            else:
                by_id[packet_id] = packet
            if role not in SUBAGENT_ROLES:
                errors.append("%s.role 不在允許清單" % prefix)
            elif role in by_role:
                errors.append("%s.role 重複：%s" % (prefix, role))
            else:
                by_role[role] = packet
            if packet.get("event_id") != bundle.get("event_id"):
                errors.append("%s.event_id 與 bundle 不一致" % prefix)
            if str(packet.get("symbol", "")).upper() != str(bundle.get("symbol", "")).upper():
                errors.append("%s.symbol 與 bundle 不一致" % prefix)
            self._validate_packet(packet, prefix, errors)
        missing = SUBAGENT_ROLES - set(by_role)
        if missing:
            errors.append("缺少子 Agent 角色：%s" % ", ".join(sorted(missing)))
            return errors
        fact_id = str(by_role["fact"].get("packet_id"))
        bull_id = str(by_role["bull"].get("packet_id"))
        bear_id = str(by_role["bear"].get("packet_id"))
        if by_role["fact"].get("input_packet_ids") != []:
            errors.append("fact 子 Agent 不得依賴其他 Agent packet")
        for role in ("bull", "bear"):
            if by_role[role].get("input_packet_ids") != [fact_id]:
                errors.append("%s 子 Agent 必須只讀 fact packet，以維持多空獨立" % role)
        adjudicator_inputs = by_role["adjudicator"].get("input_packet_ids")
        if not isinstance(adjudicator_inputs, list) or set(adjudicator_inputs) != {
            fact_id, bull_id, bear_id
        }:
            errors.append("adjudicator 必須讀取 fact、bull、bear 三個 packet")
        return errors

    def _validate_packet(
        self, packet: Mapping[str, object], prefix: str, errors: List[str]
    ) -> None:
        try:
            inputs = _string_list(packet, "input_packet_ids")
            evidence_ids = _string_list(packet, "evidence_ids")
        except ResearchToolError as error:
            errors.append("%s.%s" % (prefix, error))
            inputs, evidence_ids = [], []
        del inputs
        matching_event = False
        for evidence_id in evidence_ids:
            linked = self.by_evidence.get(evidence_id)
            if linked is None:
                errors.append("%s 引用不存在：%s" % (prefix, evidence_id))
                continue
            if str(linked[1].get("symbol", "")).upper() != str(packet.get("symbol", "")).upper():
                errors.append("%s 引用 %s 不屬於該股票" % (prefix, evidence_id))
            if linked[0] == "document" and linked[1].get("external_id") == packet.get("event_id"):
                matching_event = True
        if packet.get("role") == "fact" and not matching_event:
            errors.append("%s fact packet 必須引用事件正式文件" % prefix)
        output = packet.get("output")
        if not isinstance(output, Mapping):
            errors.append("%s.output 必須是物件" % prefix)
            return
        role = packet.get("role")
        string_fields: Tuple[str, ...] = ()
        list_fields: Tuple[str, ...] = ()
        if role == "fact":
            string_fields = ("event_summary",)
            list_fields = ("verified_facts", "reference_frames", "open_questions")
        elif role == "bull":
            string_fields = ("thesis",)
            list_fields = (
                "causal_chain", "assumptions", "catalysts",
                "failure_conditions", "unresolved_questions",
            )
        elif role == "bear":
            string_fields = ("thesis",)
            list_fields = (
                "causal_chain", "assumptions", "risks",
                "failure_conditions", "unresolved_questions",
            )
        elif role == "adjudicator":
            string_fields = ("rationale", "status_reason")
            list_fields = (
                "surviving_claims", "rejected_claims", "unresolved_questions",
            )
            if output.get("prevailing_case") not in DEBATE_OUTCOMES:
                errors.append("%s.output.prevailing_case 不在允許清單" % prefix)
            if output.get("direction") not in DIRECTIONS:
                errors.append("%s.output.direction 不在允許清單" % prefix)
            if output.get("research_status") not in RESEARCH_STATUSES:
                errors.append("%s.output.research_status 不在允許清單" % prefix)
        for field in string_fields:
            try:
                _required_string(output, field)
            except ResearchToolError as error:
                errors.append("%s.output.%s" % (prefix, error))
        for field in list_fields:
            if not isinstance(output.get(field), list):
                errors.append("%s.output.%s 必須是陣列" % (prefix, field))
        if role == "fact":
            for fact_index, fact in enumerate(output.get("verified_facts", [])):
                self._validate_fact_output(
                    fact,
                    "%s.output.verified_facts[%d]" % (prefix, fact_index),
                    errors,
                )
            for frame_index, frame in enumerate(output.get("reference_frames", [])):
                frame_prefix = "%s.output.reference_frames[%d]" % (prefix, frame_index)
                if not isinstance(frame, Mapping):
                    errors.append("%s 必須是物件" % frame_prefix)
                    continue
                if not isinstance(frame.get("is_market_expectation"), bool):
                    errors.append("%s.is_market_expectation 必須是布林值" % frame_prefix)
                try:
                    refs = _string_list(frame, "evidence_ids")
                except ResearchToolError as error:
                    errors.append("%s.%s" % (frame_prefix, error))
                    continue
                for evidence_id in refs:
                    if evidence_id not in self.by_evidence:
                        errors.append("%s 引用不存在：%s" % (frame_prefix, evidence_id))

    def _validate_fact_output(
        self, fact: object, prefix: str, errors: List[str]
    ) -> None:
        if not isinstance(fact, Mapping):
            errors.append("%s 必須是物件" % prefix)
            return
        try:
            _required_string(fact, "name")
            value = _required_string(fact, "value")
            _required_string(fact, "unit")
            _required_string(fact, "period")
            evidence_id = _required_string(fact, "evidence_id")
        except ResearchToolError as error:
            errors.append("%s.%s" % (prefix, error))
            return
        linked = self.by_evidence.get(evidence_id)
        if linked is None or linked[0] != "document":
            errors.append("%s.evidence_id 必須引用正式文件" % prefix)
        elif value not in _flatten_strings(linked[1]):
            errors.append("%s.value 無法在引用文件中核對：%s" % (prefix, value))


def _flatten_strings(value: object) -> str:
    if isinstance(value, Mapping):
        return "\n".join(_flatten_strings(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(_flatten_strings(item) for item in value)
    return "" if value is None else str(value)


class EventResearchApplicationService:
    """Stable facade used by the Skill CLI and future runtimes."""

    def __init__(self, tools: SnapshotResearchTools):
        self.tools = tools

    @classmethod
    def from_paths(cls, snapshot_path: Path, database_path: Path):
        return cls(SnapshotResearchTools.from_paths(snapshot_path, database_path))

    def validate_file(self, input_path: Path, output_path: Optional[Path] = None) -> Dict[str, object]:
        try:
            payload = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ResearchToolError("無法讀取 ResearchResult：%s" % error) from error
        errors = self.tools.validate_result(payload)
        response: Dict[str, object] = {"valid": not errors, "errors": errors}
        if not errors and output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            response["output"] = str(output_path)
        return response

    def validate_debate_file(
        self, input_path: Path, output_path: Optional[Path] = None
    ) -> Dict[str, object]:
        try:
            payload = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ResearchToolError("無法讀取 DebateBundle：%s" % error) from error
        errors = self.tools.validate_debate_bundle(payload)
        response: Dict[str, object] = {"valid": not errors, "errors": errors}
        if not errors and output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            response["output"] = str(output_path)
        return response
