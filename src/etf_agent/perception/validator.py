"""MarketPerceptionResult 的覆蓋、授權、引用與聚合重算驗證。"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional

from .contracts import (
    EXPECTATION_CLASSES,
    PRICED_IN_STATES,
    RESEARCH_STATUSES,
    RESULT_STATUSES,
    PerceptionToolError,
    _parse_time,
    _required_string,
    _string_list,
)
from .tools import PerceptionDataTools


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
