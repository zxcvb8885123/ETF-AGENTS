"""Point-in-time tools for market-sentiment and analyst-consensus research."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple


RESULT_STATUSES = {"completed", "degraded", "failed"}
RESEARCH_STATUSES = {"usable_secondary", "pending", "unavailable", "excluded"}
SENTIMENT_STANCES = {"positive", "negative", "neutral", "mixed"}
RELEVANCE_STATES = {"relevant", "ambiguous", "irrelevant"}
SENTIMENT_DIRECTIONS = SENTIMENT_STANCES | {"insufficient", "unavailable"}
CHANNEL_STATES = {"available", "insufficient", "unavailable"}
CONSENSUS_STATES = {"available", "insufficient", "unavailable"}
LICENSE_STATES = {"approved", "restricted", "unknown"}
SOURCE_CHANNELS = {"sentiment", "analyst_consensus"}
PRICED_IN_STATES = {
    "underappreciated",
    "aligned",
    "crowded",
    "contradicted",
    "unknown",
}
EXPECTATION_CLASSES = {"above", "below", "in_line", "unavailable"}
NUMERIC_METRICS = {"revenue", "eps", "target_price", "rating_score", "other"}


class PerceptionToolError(ValueError):
    """Raised when perception data cannot be used safely."""


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PerceptionToolError("%s 必須是包含時區的時間字串" % field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PerceptionToolError("%s 無法解析：%s" % (field, value)) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PerceptionToolError("%s 必須包含時區" % field)
    return parsed.astimezone(timezone.utc)


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PerceptionToolError("缺少必要字串欄位：%s" % field)
    return value.strip()


def _string_list(payload: Mapping[str, object], field: str) -> List[str]:
    value = payload.get(field)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise PerceptionToolError("%s 必須是字串陣列" % field)
    return [item.strip() for item in value]


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PerceptionToolError("%s 無法解析數值：%s" % (field, value)) from error
    if not parsed.is_finite():
        raise PerceptionToolError("%s 必須是有限數值" % field)
    return parsed


def _round_optional(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(value, 8)


def _median_decimal(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")


def _decimal_string(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    return "0" if text in {"-0", ""} else text


class PerceptionDataProvider(Protocol):
    """Provider boundary for licensed, versioned perception data."""

    def load_bundle(self) -> Mapping[str, object]:
        ...


class JsonPerceptionDataProvider:
    """Load a saved provider bundle without performing network access."""

    def __init__(self, path: Path):
        self.path = path

    def load_bundle(self) -> Mapping[str, object]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PerceptionToolError("無法讀取 PerceptionDataBundle：%s" % error) from error
        if not isinstance(payload, Mapping):
            raise PerceptionToolError("PerceptionDataBundle 必須是 JSON 物件")
        return payload


class PerceptionDataTools:
    """Read and aggregate one immutable, cutoff-constrained data bundle."""

    def __init__(self, bundle: Mapping[str, object]):
        self.bundle = dict(bundle)
        self.bundle_id = _required_string(self.bundle, "bundle_id")
        self.snapshot_id = _required_string(self.bundle, "snapshot_id")
        self.decision_cutoff = _required_string(self.bundle, "decision_cutoff")
        self.cutoff = _parse_time(self.decision_cutoff, "decision_cutoff")
        self._validate_bundle()
        self.coverage = [dict(item) for item in self.bundle.get("source_coverage", [])]
        self.evidence = [dict(item) for item in self.bundle.get("source_evidence", [])]
        self.sentiment_items = [
            dict(item) for item in self.bundle.get("sentiment_items", [])
        ]
        self.analyst_estimates = [
            dict(item) for item in self.bundle.get("analyst_estimates", [])
        ]
        self.by_evidence = {
            str(item["evidence_id"]): item for item in self.evidence
        }
        self.by_sentiment_item = {
            str(item["item_id"]): item for item in self.sentiment_items
        }

    @classmethod
    def from_provider(cls, provider: PerceptionDataProvider) -> "PerceptionDataTools":
        return cls(provider.load_bundle())

    @classmethod
    def from_path(cls, path: Path) -> "PerceptionDataTools":
        return cls.from_provider(JsonPerceptionDataProvider(path))

    def _validate_bundle(self) -> None:
        if self.bundle.get("schema_version") != "1.0":
            raise PerceptionToolError("PerceptionDataBundle schema_version 必須為 1.0")
        generated_at = _parse_time(self.bundle.get("generated_at"), "generated_at")
        if generated_at < self.cutoff:
            raise PerceptionToolError("generated_at 不得早於 decision_cutoff")
        for field in (
            "source_coverage",
            "source_evidence",
            "sentiment_items",
            "analyst_estimates",
        ):
            if not isinstance(self.bundle.get(field), list):
                raise PerceptionToolError("%s 必須是陣列" % field)

        coverage_keys = set()
        for index, coverage in enumerate(self.bundle["source_coverage"]):
            if not isinstance(coverage, Mapping):
                raise PerceptionToolError("source_coverage[%d] 必須是物件" % index)
            channel = _required_string(coverage, "channel")
            provider = _required_string(coverage, "provider")
            if channel not in SOURCE_CHANNELS:
                raise PerceptionToolError("source_coverage[%d].channel 無效" % index)
            if coverage.get("status") not in CHANNEL_STATES:
                raise PerceptionToolError("source_coverage[%d].status 無效" % index)
            if coverage.get("license_status") not in LICENSE_STATES:
                raise PerceptionToolError(
                    "source_coverage[%d].license_status 無效" % index
                )
            key = (channel, provider)
            if key in coverage_keys:
                raise PerceptionToolError("source_coverage 不得重複 channel/provider")
            coverage_keys.add(key)

        evidence_by_id: Dict[str, Mapping[str, object]] = {}
        for index, evidence in enumerate(self.bundle["source_evidence"]):
            if not isinstance(evidence, Mapping):
                raise PerceptionToolError("source_evidence[%d] 必須是物件" % index)
            evidence_id = _required_string(evidence, "evidence_id")
            _required_string(evidence, "symbol")
            _required_string(evidence, "source_name")
            _required_string(evidence, "source_url")
            _required_string(evidence, "content_sha256")
            if evidence.get("channel") not in SOURCE_CHANNELS:
                raise PerceptionToolError("source_evidence[%d].channel 無效" % index)
            if evidence.get("license_status") not in LICENSE_STATES:
                raise PerceptionToolError(
                    "source_evidence[%d].license_status 無效" % index
                )
            available = _parse_time(
                evidence.get("available_at"),
                "source_evidence[%d].available_at" % index,
            )
            published = _parse_time(
                evidence.get("published_at"),
                "source_evidence[%d].published_at" % index,
            )
            if published > available:
                raise PerceptionToolError("來源 published_at 不得晚於 available_at")
            if available > self.cutoff:
                raise PerceptionToolError("Bundle 包含 decision_cutoff 後才可得的來源")
            if evidence_id in evidence_by_id:
                raise PerceptionToolError("source_evidence.evidence_id 不得重複")
            evidence_by_id[evidence_id] = evidence

        seen_items = set()
        for index, item in enumerate(self.bundle["sentiment_items"]):
            if not isinstance(item, Mapping):
                raise PerceptionToolError("sentiment_items[%d] 必須是物件" % index)
            item_id = _required_string(item, "item_id")
            if item_id in seen_items:
                raise PerceptionToolError("sentiment_items.item_id 不得重複")
            seen_items.add(item_id)
            for field in (
                "symbol",
                "source_type",
                "source_name",
                "canonical_content_id",
                "text",
                "evidence_id",
            ):
                _required_string(item, field)
            available = _parse_time(
                item.get("available_at"), "sentiment_items[%d].available_at" % index
            )
            published = _parse_time(
                item.get("published_at"), "sentiment_items[%d].published_at" % index
            )
            if published > available:
                raise PerceptionToolError("情緒項目 published_at 不得晚於 available_at")
            if available > self.cutoff:
                raise PerceptionToolError("Bundle 包含 decision_cutoff 後的情緒項目")
            linked = evidence_by_id.get(str(item.get("evidence_id")))
            if linked is None:
                raise PerceptionToolError("情緒項目引用不存在的 evidence_id")
            if linked.get("channel") != "sentiment":
                raise PerceptionToolError("情緒項目必須引用 sentiment 證據")
            if linked.get("license_status") != "approved":
                raise PerceptionToolError("情緒項目只能引用 license_status=approved 的證據")
            if str(linked.get("symbol", "")).upper() != str(item.get("symbol", "")).upper():
                raise PerceptionToolError("情緒項目與證據股票不一致")

        seen_estimates = set()
        for index, estimate in enumerate(self.bundle["analyst_estimates"]):
            if not isinstance(estimate, Mapping):
                raise PerceptionToolError("analyst_estimates[%d] 必須是物件" % index)
            estimate_id = _required_string(estimate, "estimate_id")
            if estimate_id in seen_estimates:
                raise PerceptionToolError("analyst_estimates.estimate_id 不得重複")
            seen_estimates.add(estimate_id)
            for field in (
                "symbol",
                "metric",
                "forecast_period",
                "value",
                "unit",
                "contributor_id",
                "provider",
                "evidence_id",
            ):
                _required_string(estimate, field)
            if estimate.get("metric") not in NUMERIC_METRICS:
                raise PerceptionToolError("analyst_estimates[%d].metric 無效" % index)
            _decimal(estimate.get("value"), "analyst_estimates[%d].value" % index)
            available = _parse_time(
                estimate.get("available_at"),
                "analyst_estimates[%d].available_at" % index,
            )
            published = _parse_time(
                estimate.get("published_at"),
                "analyst_estimates[%d].published_at" % index,
            )
            if published > available:
                raise PerceptionToolError("分析師預估 published_at 不得晚於 available_at")
            if available > self.cutoff:
                raise PerceptionToolError("Bundle 包含 decision_cutoff 後的分析師預估")
            linked = evidence_by_id.get(str(estimate.get("evidence_id")))
            if linked is None:
                raise PerceptionToolError("分析師預估引用不存在的 evidence_id")
            if linked.get("channel") != "analyst_consensus":
                raise PerceptionToolError("分析師預估必須引用 analyst_consensus 證據")
            if linked.get("license_status") != "approved":
                raise PerceptionToolError(
                    "分析師預估只能引用 license_status=approved 的證據"
                )
            if str(linked.get("symbol", "")).upper() != str(estimate.get("symbol", "")).upper():
                raise PerceptionToolError("分析師預估與證據股票不一致")

    def status(self) -> Dict[str, object]:
        sentiment_symbols = sorted(
            {str(item["symbol"]).upper() for item in self.sentiment_items}
        )
        analyst_symbols = sorted(
            {str(item["symbol"]).upper() for item in self.analyst_estimates}
        )
        return {
            "bundle_id": self.bundle_id,
            "snapshot_id": self.snapshot_id,
            "decision_cutoff": self.decision_cutoff,
            "source_coverage": self.coverage,
            "sentiment_item_count": len(self.sentiment_items),
            "analyst_estimate_count": len(self.analyst_estimates),
            "sentiment_symbol_count": len(sentiment_symbols),
            "analyst_symbol_count": len(analyst_symbols),
        }

    def list_covered_symbols(self) -> Dict[str, object]:
        symbols: Dict[str, Dict[str, object]] = {}
        for item in self.sentiment_items:
            symbol = str(item["symbol"]).upper()
            row = symbols.setdefault(
                symbol, {"symbol": symbol, "sentiment_items": 0, "analyst_estimates": 0}
            )
            row["sentiment_items"] = int(row["sentiment_items"]) + 1
        for item in self.analyst_estimates:
            symbol = str(item["symbol"]).upper()
            row = symbols.setdefault(
                symbol, {"symbol": symbol, "sentiment_items": 0, "analyst_estimates": 0}
            )
            row["analyst_estimates"] = int(row["analyst_estimates"]) + 1
        return {
            "snapshot_id": self.snapshot_id,
            "decision_cutoff": self.decision_cutoff,
            "symbols": [symbols[key] for key in sorted(symbols)],
        }

    def get_sentiment_items(
        self, symbol: str, lookback_days: int = 28
    ) -> Dict[str, object]:
        if lookback_days <= 0 or lookback_days > 365:
            raise PerceptionToolError("lookback_days 必須介於 1 與 365")
        start = self.cutoff - timedelta(days=lookback_days)
        rows = [
            item
            for item in self.sentiment_items
            if str(item.get("symbol", "")).upper() == symbol.upper()
            and _parse_time(item.get("available_at"), "available_at") >= start
        ]
        rows.sort(key=lambda item: str(item.get("available_at", "")))
        return {
            "symbol": symbol.upper(),
            "decision_cutoff": self.decision_cutoff,
            "lookback_days": lookback_days,
            "items": rows,
        }

    def validate_labels(
        self, symbol: str, labels: object, lookback_days: int = 28
    ) -> List[Dict[str, object]]:
        if not isinstance(labels, list):
            raise PerceptionToolError("sentiment_labels 必須是陣列")
        eligible = {
            str(item["item_id"]): item
            for item in self.get_sentiment_items(symbol, lookback_days)["items"]
        }
        normalized: List[Dict[str, object]] = []
        seen = set()
        for index, label in enumerate(labels):
            if not isinstance(label, Mapping):
                raise PerceptionToolError("sentiment_labels[%d] 必須是物件" % index)
            item_id = _required_string(label, "item_id")
            if item_id in seen:
                raise PerceptionToolError("sentiment_labels.item_id 不得重複")
            seen.add(item_id)
            item = eligible.get(item_id)
            if item is None:
                raise PerceptionToolError("標籤引用不存在或超出 lookback 的 item_id：%s" % item_id)
            if str(label.get("symbol", "")).upper() != symbol.upper():
                raise PerceptionToolError("情緒標籤股票不一致：%s" % item_id)
            if label.get("evidence_id") != item.get("evidence_id"):
                raise PerceptionToolError("情緒標籤 evidence_id 不一致：%s" % item_id)
            if label.get("relevance") not in RELEVANCE_STATES:
                raise PerceptionToolError("情緒標籤 relevance 無效：%s" % item_id)
            if label.get("stance") not in SENTIMENT_STANCES:
                raise PerceptionToolError("情緒標籤 stance 無效：%s" % item_id)
            _required_string(label, "rationale")
            _required_string(label, "model_version")
            normalized.append(dict(label))
        missing = sorted(set(eligible) - seen)
        if missing:
            raise PerceptionToolError(
                "sentiment_labels 必須覆蓋視窗內全部項目，缺少：%s"
                % ", ".join(missing)
            )
        return normalized

    def aggregate_sentiment(
        self,
        symbol: str,
        labels: object,
        lookback_days: int = 28,
        min_items: int = 5,
        min_sources: int = 2,
    ) -> Dict[str, object]:
        if min_items < 1 or min_sources < 1:
            raise PerceptionToolError("min_items 與 min_sources 必須為正整數")
        normalized = self.validate_labels(symbol, labels, lookback_days)
        item_lookup = self.by_sentiment_item
        relevant = sorted(
            (item for item in normalized if item["relevance"] == "relevant"),
            key=lambda label: (
                str(item_lookup[str(label["item_id"])]["available_at"]),
                str(label["item_id"]),
            ),
        )
        canonical: Dict[str, Dict[str, object]] = {}
        for label in relevant:
            source_item = item_lookup[str(label["item_id"])]
            canonical_id = str(source_item["canonical_content_id"])
            canonical.setdefault(canonical_id, label)
        deduped = list(canonical.values())
        source_names = {
            str(item_lookup[str(label["item_id"])]["source_name"])
            for label in deduped
        }
        scores = {
            "positive": 1.0,
            "negative": -1.0,
            "neutral": 0.0,
            "mixed": 0.0,
        }
        score = statistics.mean(scores[str(label["stance"])] for label in deduped) if deduped else None
        counts = {
            stance: sum(1 for label in deduped if label["stance"] == stance)
            for stance in ("positive", "negative", "neutral", "mixed")
        }
        status = "available"
        if not deduped:
            status = "unavailable"
        elif len(deduped) < min_items or len(source_names) < min_sources:
            status = "insufficient"
        if status == "unavailable":
            direction = "unavailable"
        elif status == "insufficient":
            direction = "insufficient"
        elif counts["positive"] / len(deduped) >= 0.25 and counts["negative"] / len(deduped) >= 0.25:
            direction = "mixed"
        elif score is not None and score > 0.2:
            direction = "positive"
        elif score is not None and score < -0.2:
            direction = "negative"
        else:
            direction = "neutral"
        dispersion = None
        if deduped:
            dispersion = 1.0 - max(counts.values()) / len(deduped)

        flags: List[str] = []
        duplicate_rate = (
            1.0 - len(deduped) / len(relevant) if relevant else 0.0
        )
        if duplicate_rate > 0.3:
            flags.append("HIGH_DUPLICATE_RATE")
        source_counts: Dict[str, int] = {}
        author_counts: Dict[str, int] = {}
        for label in deduped:
            source_item = item_lookup[str(label["item_id"])]
            source_name = str(source_item["source_name"])
            source_counts[source_name] = source_counts.get(source_name, 0) + 1
            author = source_item.get("author_id")
            if author:
                author_counts[str(author)] = author_counts.get(str(author), 0) + 1
        if deduped and max(source_counts.values(), default=0) / len(deduped) > 0.7:
            flags.append("SOURCE_CONCENTRATION")
        if deduped and author_counts and max(author_counts.values()) / len(deduped) > 0.5:
            flags.append("AUTHOR_CONCENTRATION")
        if len(source_names) < min_sources and deduped:
            flags.append("LOW_SOURCE_DIVERSITY")

        recent_start = self.cutoff - timedelta(days=7)
        prior_start = self.cutoff - timedelta(days=28)
        recent = 0
        prior = 0
        for label in deduped:
            available = _parse_time(
                item_lookup[str(label["item_id"])]["available_at"], "available_at"
            )
            if available >= recent_start:
                recent += 1
            elif available >= prior_start:
                prior += 1
        prior_weekly = prior / 3.0
        attention_change = recent / prior_weekly if prior_weekly > 0 else None
        evidence_ids = sorted(
            {str(label["evidence_id"]) for label in deduped}
        )
        return {
            "status": status,
            "direction": direction,
            "score": _round_optional(score),
            "dispersion": _round_optional(dispersion),
            "item_count": len(deduped),
            "source_count": len(source_names),
            "stance_counts": counts,
            "duplicate_rate": _round_optional(duplicate_rate),
            "attention_change_7d_vs_prior_weekly": _round_optional(attention_change),
            "manipulation_flags": flags,
            "evidence_ids": evidence_ids,
            "lookback_days": lookback_days,
            "min_items": min_items,
            "min_sources": min_sources,
            "methodology": "equal_weight_deduplicated_v1",
        }

    def get_analyst_estimates(
        self,
        symbol: str,
        metric: Optional[str] = None,
        forecast_period: Optional[str] = None,
    ) -> Dict[str, object]:
        if metric is not None and metric not in NUMERIC_METRICS:
            raise PerceptionToolError("metric 不在允許清單")
        rows = [
            item
            for item in self.analyst_estimates
            if str(item.get("symbol", "")).upper() == symbol.upper()
            and (metric is None or item.get("metric") == metric)
            and (forecast_period is None or item.get("forecast_period") == forecast_period)
        ]
        rows.sort(key=lambda item: str(item.get("available_at", "")))
        return {
            "symbol": symbol.upper(),
            "metric": metric,
            "forecast_period": forecast_period,
            "decision_cutoff": self.decision_cutoff,
            "estimates": rows,
        }

    def compute_consensus_revision(
        self,
        symbol: str,
        metric: str,
        forecast_period: str,
        revision_window_days: int = 30,
    ) -> Dict[str, object]:
        if revision_window_days <= 0 or revision_window_days > 365:
            raise PerceptionToolError("revision_window_days 必須介於 1 與 365")
        rows = self.get_analyst_estimates(symbol, metric, forecast_period)["estimates"]
        if not rows:
            return {
                "status": "unavailable",
                "metric": metric,
                "forecast_period": forecast_period,
                "unit": None,
                "currency": None,
                "median": None,
                "contributor_count": 0,
                "dispersion_pct": None,
                "revision_window_days": revision_window_days,
                "prior_median": None,
                "prior_contributor_count": 0,
                "revision_pct": None,
                "evidence_ids": [],
            }
        units = {str(item["unit"]) for item in rows}
        currencies = {item.get("currency") for item in rows}
        if len(units) != 1 or len(currencies) != 1:
            raise PerceptionToolError("同一共識指標的 unit/currency 必須一致")
        current = self._latest_by_contributor(rows, self.cutoff)
        prior_cutoff = self.cutoff - timedelta(days=revision_window_days)
        prior = self._latest_by_contributor(rows, prior_cutoff)
        current_values = [_decimal(item["value"], "value") for item in current]
        prior_values = [_decimal(item["value"], "value") for item in prior]
        current_median = _median_decimal(current_values)
        prior_median = _median_decimal(prior_values) if prior_values else None
        dispersion_pct = None
        if len(current_values) >= 2 and current_median != 0:
            deviations = [abs(value - current_median) for value in current_values]
            dispersion_pct = float(
                _median_decimal(deviations) / abs(current_median) * Decimal("100")
            )
        revision_pct = None
        if prior_median is not None and prior_median != 0:
            revision_pct = float(
                (current_median / prior_median - Decimal("1")) * Decimal("100")
            )
        status = "available" if len(current) >= 2 else "insufficient"
        return {
            "status": status,
            "metric": metric,
            "forecast_period": forecast_period,
            "unit": next(iter(units)),
            "currency": next(iter(currencies)),
            "median": _decimal_string(current_median),
            "contributor_count": len(current),
            "dispersion_pct": _round_optional(dispersion_pct),
            "revision_window_days": revision_window_days,
            "prior_median": _decimal_string(prior_median) if prior_median is not None else None,
            "prior_contributor_count": len(prior),
            "revision_pct": _round_optional(revision_pct),
            "evidence_ids": sorted({str(item["evidence_id"]) for item in current}),
        }

    @staticmethod
    def _latest_by_contributor(
        rows: Iterable[Mapping[str, object]], cutoff: datetime
    ) -> List[Mapping[str, object]]:
        latest: Dict[str, Mapping[str, object]] = {}
        for row in rows:
            available = _parse_time(row.get("available_at"), "available_at")
            if available > cutoff:
                continue
            contributor = str(row["contributor_id"])
            previous = latest.get(contributor)
            if previous is None or available > _parse_time(
                previous.get("available_at"), "available_at"
            ):
                latest[contributor] = row
        return list(latest.values())

    def compare_event_expectations(
        self,
        event_result: Mapping[str, object],
        event_id: str,
        fact_name: str,
        metric: str,
        forecast_period: str,
        threshold_pct: float = 2.0,
    ) -> Dict[str, object]:
        if threshold_pct < 0:
            raise PerceptionToolError("threshold_pct 不得為負數")
        if event_result.get("schema_version") != "2.1" or event_result.get("status") != "completed":
            raise PerceptionToolError("事件研究必須是已完成的 ResearchResult 2.1")
        if event_result.get("snapshot_id") != self.snapshot_id:
            raise PerceptionToolError("事件研究與感知資料的 snapshot_id 不一致")
        if _parse_time(event_result.get("decision_cutoff"), "event decision_cutoff") != self.cutoff:
            raise PerceptionToolError("事件研究與感知資料的 decision_cutoff 不一致")
        items = event_result.get("items")
        if not isinstance(items, list):
            raise PerceptionToolError("ResearchResult.items 必須是陣列")
        event = next(
            (
                item
                for item in items
                if isinstance(item, Mapping) and item.get("event_id") == event_id
            ),
            None,
        )
        if event is None:
            raise PerceptionToolError("找不到指定 event_id")
        facts = event.get("fact_values")
        if not isinstance(facts, list):
            raise PerceptionToolError("事件缺少 fact_values")
        fact = next(
            (
                item
                for item in facts
                if isinstance(item, Mapping) and item.get("name") == fact_name
            ),
            None,
        )
        if fact is None:
            raise PerceptionToolError("找不到指定 fact_name")
        consensus = self.compute_consensus_revision(
            str(event.get("symbol", "")), metric, forecast_period
        )
        base = {
            "event_result_id": event_result.get("run_id"),
            "event_id": event_id,
            "symbol": str(event.get("symbol", "")).upper(),
            "fact_name": fact_name,
            "metric": metric,
            "forecast_period": forecast_period,
            "threshold_pct": threshold_pct,
            "actual": str(fact.get("value")),
            "unit": fact.get("unit"),
            "actual_evidence_ids": [fact.get("evidence_id")],
            "consensus_evidence_ids": consensus["evidence_ids"],
        }
        if consensus["status"] != "available" or consensus["unit"] != fact.get("unit"):
            return {
                **base,
                "status": "unavailable",
                "consensus_median": consensus["median"],
                "surprise_pct": None,
                "classification": "unavailable",
                "reason": "共識不足或實際值與共識單位不一致",
            }
        actual = _decimal(fact.get("value"), "actual")
        median = _decimal(consensus["median"], "consensus_median")
        surprise = None if median == 0 else float((actual / median - Decimal("1")) * Decimal("100"))
        if surprise is None:
            classification = "unavailable"
        elif surprise > threshold_pct:
            classification = "above"
        elif surprise < -threshold_pct:
            classification = "below"
        else:
            classification = "in_line"
        return {
            **base,
            "status": "available" if surprise is not None else "unavailable",
            "consensus_median": consensus["median"],
            "surprise_pct": _round_optional(surprise),
            "classification": classification,
            "reason": "以相同期間、單位的分析師中位數比較",
        }


class MarketPerceptionResultValidator:
    """Validate agent output and recompute all aggregate values."""

    def __init__(
        self,
        tools: PerceptionDataTools,
        event_result: Optional[Mapping[str, object]] = None,
    ):
        self.tools = tools
        self.event_result = dict(event_result) if event_result is not None else None

    def validate(self, result: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        for field in (
            "schema_version",
            "run_id",
            "snapshot_id",
            "decision_cutoff",
            "skill_version",
            "status",
        ):
            if not isinstance(result.get(field), str) or not str(result.get(field)).strip():
                errors.append("缺少必要字串欄位：%s" % field)
        if result.get("schema_version") != "1.0":
            errors.append("schema_version 必須為 1.0")
        if result.get("snapshot_id") != self.tools.snapshot_id:
            errors.append("snapshot_id 與 PerceptionDataBundle 不一致")
        try:
            if _parse_time(result.get("decision_cutoff"), "decision_cutoff") != self.tools.cutoff:
                errors.append("decision_cutoff 與 PerceptionDataBundle 不一致")
        except PerceptionToolError as error:
            errors.append(str(error))
        if result.get("status") not in RESULT_STATUSES:
            errors.append("status 必須是 completed、degraded 或 failed")
        items = result.get("items")
        if not isinstance(items, list):
            errors.append("items 必須是陣列")
            return errors
        seen = set()
        for index, item in enumerate(items):
            prefix = "items[%d]" % index
            if not isinstance(item, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                errors.append("%s.symbol 缺少必要字串" % prefix)
                continue
            if symbol in seen:
                errors.append("%s.symbol 不得重複" % prefix)
            seen.add(symbol)
            self._validate_item(item, prefix, symbol, errors)
        if result.get("status") == "completed" and result.get("errors"):
            errors.append("completed 結果不得同時包含 errors")
        return errors

    def _validate_item(
        self,
        item: Mapping[str, object],
        prefix: str,
        symbol: str,
        errors: List[str],
    ) -> None:
        if item.get("research_status") not in RESEARCH_STATUSES:
            errors.append("%s.research_status 無效" % prefix)
        if item.get("priced_in_assessment") not in PRICED_IN_STATES:
            errors.append("%s.priced_in_assessment 無效" % prefix)
        for field in ("rationale", "status_reason"):
            try:
                _required_string(item, field)
            except PerceptionToolError as error:
                errors.append("%s.%s" % (prefix, error))
        try:
            event_result_ids = _string_list(item, "event_result_ids")
            evidence_ids = _string_list(item, "evidence_ids")
            _string_list(item, "risk_flags")
        except PerceptionToolError as error:
            errors.append("%s.%s" % (prefix, error))
            event_result_ids = []
            evidence_ids = []
        for evidence_id in evidence_ids:
            linked = self.tools.by_evidence.get(evidence_id)
            if linked is None:
                errors.append("%s 引用不存在：%s" % (prefix, evidence_id))
            elif str(linked.get("symbol", "")).upper() != symbol:
                errors.append("%s 引用不屬於該股票：%s" % (prefix, evidence_id))

        labels = item.get("sentiment_labels")
        sentiment = item.get("sentiment")
        expected_sentiment: Optional[Dict[str, object]] = None
        try:
            if not isinstance(sentiment, Mapping):
                raise PerceptionToolError("sentiment 必須是物件")
            lookback_days = int(sentiment.get("lookback_days", 28))
            expected_sentiment = self.tools.aggregate_sentiment(
                symbol,
                labels,
                lookback_days=lookback_days,
                min_items=int(sentiment.get("min_items", 5)),
                min_sources=int(sentiment.get("min_sources", 2)),
            )
            if dict(sentiment) != expected_sentiment:
                errors.append("%s.sentiment 與確定性重算結果不一致" % prefix)
        except (PerceptionToolError, TypeError, ValueError) as error:
            errors.append("%s.sentiment：%s" % (prefix, error))

        metrics = item.get("consensus_metrics")
        has_available_consensus = False
        used_evidence = set(
            expected_sentiment.get("evidence_ids", []) if expected_sentiment else []
        )
        if not isinstance(metrics, list):
            errors.append("%s.consensus_metrics 必須是陣列" % prefix)
            metrics = []
        metric_keys = set()
        for metric_index, metric in enumerate(metrics):
            metric_prefix = "%s.consensus_metrics[%d]" % (prefix, metric_index)
            if not isinstance(metric, Mapping):
                errors.append("%s 必須是物件" % metric_prefix)
                continue
            key = (metric.get("metric"), metric.get("forecast_period"))
            if key in metric_keys:
                errors.append("%s metric/forecast_period 不得重複" % metric_prefix)
            metric_keys.add(key)
            try:
                expected = self.tools.compute_consensus_revision(
                    symbol,
                    _required_string(metric, "metric"),
                    _required_string(metric, "forecast_period"),
                    int(metric.get("revision_window_days", 30)),
                )
                if dict(metric) != expected:
                    errors.append("%s 與確定性重算結果不一致" % metric_prefix)
                has_available_consensus = has_available_consensus or expected["status"] == "available"
                used_evidence.update(expected["evidence_ids"])
            except (PerceptionToolError, TypeError, ValueError) as error:
                errors.append("%s：%s" % (metric_prefix, error))

        gaps = item.get("expectation_gaps")
        if not isinstance(gaps, list):
            errors.append("%s.expectation_gaps 必須是陣列" % prefix)
            gaps = []
        if gaps and self.event_result is None:
            errors.append("%s 驗證 expectation_gaps 時必須提供已驗證 ResearchResult" % prefix)
        for gap_index, gap in enumerate(gaps):
            gap_prefix = "%s.expectation_gaps[%d]" % (prefix, gap_index)
            if not isinstance(gap, Mapping):
                errors.append("%s 必須是物件" % gap_prefix)
                continue
            if gap.get("classification") not in EXPECTATION_CLASSES:
                errors.append("%s.classification 無效" % gap_prefix)
            if self.event_result is not None:
                try:
                    expected_gap = self.tools.compare_event_expectations(
                        self.event_result,
                        _required_string(gap, "event_id"),
                        _required_string(gap, "fact_name"),
                        _required_string(gap, "metric"),
                        _required_string(gap, "forecast_period"),
                        float(gap.get("threshold_pct", 2.0)),
                    )
                    if dict(gap) != expected_gap:
                        errors.append("%s 與確定性重算結果不一致" % gap_prefix)
                    used_evidence.update(expected_gap["consensus_evidence_ids"])
                    if expected_gap.get("event_result_id") not in event_result_ids:
                        errors.append("%s 未列入 event_result_ids" % gap_prefix)
                except (PerceptionToolError, TypeError, ValueError) as error:
                    errors.append("%s：%s" % (gap_prefix, error))

        if not used_evidence.issubset(set(evidence_ids)):
            errors.append("%s.evidence_ids 未包含所有情緒／共識來源" % prefix)
        sentiment_available = bool(
            expected_sentiment and expected_sentiment.get("status") == "available"
        )
        research_status = item.get("research_status")
        if research_status == "usable_secondary" and not (
            sentiment_available or has_available_consensus
        ):
            errors.append("%s usable_secondary 至少需要一個可用的情緒或共識通道" % prefix)
        if research_status == "unavailable" and (
            sentiment_available or has_available_consensus
        ):
            errors.append("%s 有可用通道時不得標記 unavailable" % prefix)


class MarketPerceptionApplicationService:
    """Facade used by the Skill CLI and future runtimes."""

    def __init__(self, tools: PerceptionDataTools):
        self.tools = tools

    @classmethod
    def from_path(cls, bundle_path: Path) -> "MarketPerceptionApplicationService":
        return cls(PerceptionDataTools.from_path(bundle_path))

    @staticmethod
    def read_json(path: Path, label: str) -> Mapping[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PerceptionToolError("無法讀取%s：%s" % (label, error)) from error
        if not isinstance(payload, Mapping):
            raise PerceptionToolError("%s必須是 JSON 物件" % label)
        return payload

    def validate_file(
        self,
        input_path: Path,
        output_path: Optional[Path] = None,
        event_result_path: Optional[Path] = None,
    ) -> Dict[str, object]:
        payload = self.read_json(input_path, " MarketPerceptionResult")
        event_result = (
            self.read_json(event_result_path, " ResearchResult")
            if event_result_path is not None
            else None
        )
        errors = MarketPerceptionResultValidator(self.tools, event_result).validate(payload)
        response: Dict[str, object] = {"valid": not errors, "errors": errors}
        if not errors and output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            response["output"] = str(output_path)
        return response

    def compare_event_file(
        self,
        event_result_path: Path,
        event_id: str,
        fact_name: str,
        metric: str,
        forecast_period: str,
        threshold_pct: float = 2.0,
    ) -> Dict[str, object]:
        event_result = self.read_json(event_result_path, " ResearchResult")
        return self.tools.compare_event_expectations(
            event_result,
            event_id,
            fact_name,
            metric,
            forecast_period,
            threshold_pct,
        )
