"""MarketPerceptionResult 的覆蓋、授權、引用與聚合重算驗證。"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional

from .contracts import (
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

    def __init__(self, tools: PerceptionDataTools):
        self.tools = tools

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
            _string_list(item, "event_result_ids")
            evidence_ids = _string_list(item, "evidence_ids")
            _string_list(item, "risk_flags")
        except PerceptionToolError as error:
            errors.append("%s.%s" % (prefix, error))
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

        # 舊版結果帶的共識欄位：空陣列照舊接受；有內容時已無工具可重算，拒絕。
        for field in ("consensus_metrics", "expectation_gaps"):
            if field in item and item[field] != []:
                errors.append("%s.%s 已停用，舊封存只接受空陣列" % (prefix, field))
        used_evidence = set(
            expected_sentiment.get("evidence_ids", []) if expected_sentiment else []
        )
        if not used_evidence.issubset(set(evidence_ids)):
            errors.append("%s.evidence_ids 未包含所有情緒來源" % prefix)
        sentiment_available = bool(
            expected_sentiment and expected_sentiment.get("status") == "available"
        )
        research_status = item.get("research_status")
        if research_status == "usable_secondary" and not sentiment_available:
            errors.append("%s usable_secondary 需要可用的情緒通道" % prefix)
        if research_status == "unavailable" and sentiment_available:
            errors.append("%s 有可用通道時不得標記 unavailable" % prefix)
