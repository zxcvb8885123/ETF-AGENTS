"""Market sentiment deterministic tools."""

from __future__ import annotations

import statistics
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from .contracts import (
    CHANNEL_STATES,
    LICENSE_STATES,
    RELEVANCE_STATES,
    SENTIMENT_STANCES,
    SOURCE_CHANNELS,
    PerceptionToolError,
    _parse_time,
    _required_string,
)
from .provider import JsonPerceptionDataProvider, PerceptionDataProvider


def _round_optional(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(value, 8)


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
            if available < _parse_time(linked.get("available_at"), "evidence.available_at"):
                raise PerceptionToolError("情緒項目可得時間不得早於來源證據")
            if linked.get("channel") != "sentiment":
                raise PerceptionToolError("情緒項目必須引用 sentiment 證據")
            if linked.get("license_status") != "approved":
                raise PerceptionToolError("情緒項目只能引用 license_status=approved 的證據")
            if str(linked.get("symbol", "")).upper() != str(item.get("symbol", "")).upper():
                raise PerceptionToolError("情緒項目與證據股票不一致")

    def status(self) -> Dict[str, object]:
        sentiment_symbols = sorted(
            {str(item["symbol"]).upper() for item in self.sentiment_items}
        )
        return {
            "bundle_id": self.bundle_id,
            "snapshot_id": self.snapshot_id,
            "decision_cutoff": self.decision_cutoff,
            "source_coverage": self.coverage,
            "sentiment_item_count": len(self.sentiment_items),
            "sentiment_symbol_count": len(sentiment_symbols),
        }

    def list_covered_symbols(self) -> Dict[str, object]:
        symbols: Dict[str, Dict[str, object]] = {}
        for item in self.sentiment_items:
            symbol = str(item["symbol"]).upper()
            row = symbols.setdefault(symbol, {"symbol": symbol, "sentiment_items": 0})
            row["sentiment_items"] = int(row["sentiment_items"]) + 1
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
