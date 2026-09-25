"""由同一 Snapshot／cutoff 的已驗證研究 artifact 確定性建立 ResearchReport。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Set

from .contracts import (
    DISCLAIMER,
    UPSTREAM_STATUSES,
    ResearchReportError,
    _mapping_list,
    _parse_time,
    _required_string,
    _same_cutoff,
    _string_list,
)


class ResearchReportBuilder:
    """Combine upstream artifacts without adding investment conclusions."""

    def __init__(
        self,
        snapshot: Mapping[str, object],
        research_result: Mapping[str, object],
        perception_result: Optional[Mapping[str, object]] = None,
        perception_bundle: Optional[Mapping[str, object]] = None,
    ):
        self.snapshot = dict(snapshot)
        self.research_result = dict(research_result)
        self.perception_result = (
            dict(perception_result) if perception_result is not None else None
        )
        self.perception_bundle = (
            dict(perception_bundle) if perception_bundle is not None else None
        )
        self._validate_inputs()

    def _validate_inputs(self) -> None:
        self.snapshot_id = _required_string(self.snapshot, "snapshot_id")
        self.decision_cutoff = _required_string(self.snapshot, "decision_cutoff")
        self.cutoff = _parse_time(self.decision_cutoff, "decision_cutoff")
        if self.snapshot.get("usable") is not True:
            raise ResearchReportError("ResearchSnapshot 不可用，不能建立研究報告")
        if not isinstance(self.snapshot.get("quality_flags"), list):
            raise ResearchReportError("ResearchSnapshot.quality_flags 必須是陣列")
        self.snapshot_evidence = self._evidence_map(
            _mapping_list(self.snapshot, "source_evidence"), "ResearchSnapshot"
        )
        self.snapshot_prices = _mapping_list(self.snapshot, "latest_prices")
        self.snapshot_documents = _mapping_list(self.snapshot, "documents")

        if self.research_result.get("schema_version") != "2.1":
            raise ResearchReportError("ResearchResult schema_version 必須為 2.1")
        self.research_run_id = _required_string(self.research_result, "run_id")
        if self.research_result.get("status") not in UPSTREAM_STATUSES:
            raise ResearchReportError("ResearchResult 必須是 completed 或 degraded")
        self._require_same_version(self.research_result, "ResearchResult")
        self.research_items = _mapping_list(self.research_result, "items")
        self._validate_research_items()

        if (self.perception_result is None) != (self.perception_bundle is None):
            raise ResearchReportError(
                "MarketPerceptionResult 與 PerceptionDataBundle 必須同時提供"
            )
        self.perception_run_id: Optional[str] = None
        self.perception_items: List[Dict[str, object]] = []
        self.perception_evidence: Dict[str, Dict[str, object]] = {}
        if self.perception_result is not None and self.perception_bundle is not None:
            if self.perception_result.get("schema_version") != "1.0":
                raise ResearchReportError("MarketPerceptionResult schema_version 必須為 1.0")
            if self.perception_result.get("status") not in UPSTREAM_STATUSES:
                raise ResearchReportError(
                    "MarketPerceptionResult 必須是 completed 或 degraded"
                )
            if self.perception_bundle.get("schema_version") != "1.0":
                raise ResearchReportError("PerceptionDataBundle schema_version 必須為 1.0")
            self._require_same_version(
                self.perception_result, "MarketPerceptionResult"
            )
            self._require_same_version(
                self.perception_bundle, "PerceptionDataBundle"
            )
            self.perception_run_id = _required_string(
                self.perception_result, "run_id"
            )
            self.perception_items = _mapping_list(
                self.perception_result, "items"
            )
            self.perception_evidence = self._evidence_map(
                _mapping_list(self.perception_bundle, "source_evidence"),
                "PerceptionDataBundle",
            )
            self._validate_perception_items()

    @staticmethod
    def _evidence_map(
        rows: Sequence[Mapping[str, object]], label: str
    ) -> Dict[str, Dict[str, object]]:
        result: Dict[str, Dict[str, object]] = {}
        for index, row in enumerate(rows):
            evidence_id = _required_string(row, "evidence_id")
            if evidence_id in result:
                raise ResearchReportError(
                    "%s.source_evidence[%d] evidence_id 重複" % (label, index)
                )
            result[evidence_id] = dict(row)
        return result

    def _require_same_version(
        self, payload: Mapping[str, object], label: str
    ) -> None:
        if payload.get("snapshot_id") != self.snapshot_id:
            raise ResearchReportError("%s.snapshot_id 與 Snapshot 不一致" % label)
        if not _same_cutoff(payload.get("decision_cutoff"), self.decision_cutoff):
            raise ResearchReportError("%s.decision_cutoff 與 Snapshot 不一致" % label)

    def _validate_research_items(self) -> None:
        seen = set()
        for index, item in enumerate(self.research_items):
            prefix = "ResearchResult.items[%d]" % index
            event_id = _required_string(item, "event_id")
            symbol = _required_string(item, "symbol").upper()
            for field in (
                "event_type",
                "published_at",
                "event_summary",
                "direction",
                "impact_mechanism",
                "research_status",
                "status_reason",
            ):
                _required_string(item, field)
            key = (event_id, symbol)
            if key in seen:
                raise ResearchReportError("%s 事件與股票組合重複" % prefix)
            seen.add(key)
            evidence_ids = _string_list(item, "evidence_ids")
            counter_ids = _string_list(item, "counter_evidence_ids")
            self._require_snapshot_evidence(evidence_ids + counter_ids, symbol, prefix)
            facts = _mapping_list(item, "fact_values")
            fact_ids = [_required_string(fact, "evidence_id") for fact in facts]
            self._require_snapshot_evidence(fact_ids, symbol, prefix)
            price = item.get("price_confirmation")
            if not isinstance(price, Mapping):
                raise ResearchReportError("%s.price_confirmation 必須是物件" % prefix)
            price_id = price.get("source_evidence_id")
            if price.get("status") != "unavailable":
                if not isinstance(price_id, str) or price_id not in self.snapshot_evidence:
                    raise ResearchReportError("%s 行情引用不存在" % prefix)
            debate = item.get("debate")
            if not isinstance(debate, Mapping):
                raise ResearchReportError("%s.debate 必須是物件" % prefix)
            for case_name in ("bull_case", "bear_case"):
                case = debate.get(case_name)
                if not isinstance(case, Mapping):
                    raise ResearchReportError("%s.debate.%s 必須是物件" % (prefix, case_name))
                _required_string(case, "thesis")
            adjudication = debate.get("adjudication")
            if not isinstance(adjudication, Mapping):
                raise ResearchReportError("%s.debate.adjudication 必須是物件" % prefix)
            _required_string(adjudication, "prevailing_case")
            _required_string(adjudication, "rationale")

    def _require_snapshot_evidence(
        self, evidence_ids: Sequence[str], symbol: str, prefix: str
    ) -> None:
        for evidence_id in evidence_ids:
            linked = self.snapshot_evidence.get(evidence_id)
            if linked is None:
                raise ResearchReportError("%s 引用不存在：%s" % (prefix, evidence_id))
            linked_symbol = linked.get("symbol")
            if linked_symbol and str(linked_symbol).upper() != symbol:
                raise ResearchReportError(
                    "%s 引用不屬於該股票：%s" % (prefix, evidence_id)
                )

    def _validate_perception_items(self) -> None:
        seen = set()
        for index, item in enumerate(self.perception_items):
            prefix = "MarketPerceptionResult.items[%d]" % index
            symbol = _required_string(item, "symbol").upper()
            for field in (
                "research_status",
                "priced_in_assessment",
                "rationale",
                "status_reason",
            ):
                _required_string(item, field)
            if symbol in seen:
                raise ResearchReportError("%s.symbol 不得重複" % prefix)
            seen.add(symbol)
            evidence_ids = _string_list(item, "evidence_ids")
            for evidence_id in evidence_ids:
                linked = self.perception_evidence.get(evidence_id)
                if linked is None:
                    raise ResearchReportError(
                        "%s 引用不存在於 PerceptionDataBundle：%s"
                        % (prefix, evidence_id)
                    )
                if str(linked.get("symbol", "")).upper() != symbol:
                    raise ResearchReportError(
                        "%s 引用不屬於該股票：%s" % (prefix, evidence_id)
                    )
            event_result_ids = _string_list(item, "event_result_ids")
            if event_result_ids and self.research_run_id not in event_result_ids:
                raise ResearchReportError(
                    "%s.event_result_ids 未引用目前 ResearchResult" % prefix
                )
            if not isinstance(item.get("sentiment"), Mapping):
                raise ResearchReportError("%s.sentiment 必須是物件" % prefix)
            _mapping_list(item, "consensus_metrics")
            _mapping_list(item, "expectation_gaps")
            _string_list(item, "risk_flags")

    def build(
        self,
        report_id: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> Dict[str, object]:
        report_id = report_id or str(uuid.uuid4())
        generated_at = generated_at or datetime.now(timezone.utc).isoformat()
        if not report_id.strip():
            raise ResearchReportError("report_id 不得為空")
        if _parse_time(generated_at, "generated_at") < self.cutoff:
            raise ResearchReportError("generated_at 不得早於 decision_cutoff")

        events_by_symbol: Dict[str, List[Dict[str, object]]] = {}
        for item in self.research_items:
            symbol = str(item["symbol"]).upper()
            events_by_symbol.setdefault(symbol, []).append(item)
        perception_by_symbol = {
            str(item["symbol"]).upper(): item for item in self.perception_items
        }
        symbols = sorted(set(events_by_symbol) | set(perception_by_symbol))
        selected_sources: Dict[str, Dict[str, object]] = {}
        companies = [
            self._build_company(
                symbol,
                events_by_symbol.get(symbol, []),
                perception_by_symbol.get(symbol),
                selected_sources,
            )
            for symbol in symbols
        ]

        event_counts = {
            status: sum(
                1
                for item in self.research_items
                if item.get("research_status") == status
            )
            for status in ("candidate", "pending", "excluded")
        }
        perception_available = self.perception_result is not None
        missing_data: List[str] = []
        if not perception_available:
            missing_data.append("MARKET_PERCEPTION_UNAVAILABLE")
        if not self.research_items:
            missing_data.append("NO_EVENT_RESEARCH_ITEMS")
        quality_flags = [str(item) for item in self.snapshot.get("quality_flags", [])]
        report_status = "completed"
        if (
            quality_flags
            or self.research_result.get("status") == "degraded"
            or not perception_available
            or (
                self.perception_result is not None
                and self.perception_result.get("status") == "degraded"
            )
            or any(company.get("missing_data") for company in companies)
        ):
            report_status = "degraded"

        return {
            "schema_version": "1.0",
            "report_id": report_id,
            "generated_at": generated_at,
            "snapshot_id": self.snapshot_id,
            "decision_cutoff": self.decision_cutoff,
            "status": report_status,
            "title": "每日研究報告",
            "input_refs": {
                "research_run_id": self.research_run_id,
                "perception_run_id": self.perception_run_id,
            },
            "data_quality": {
                "snapshot_usable": True,
                "quality_flags": quality_flags,
                "universe_version": self.snapshot.get("universe_version"),
                "latest_trade_date": self.snapshot.get("latest_trade_date"),
                "universe_size": int(self.snapshot.get("universe_size", 0)),
                "latest_price_symbols": int(
                    self.snapshot.get("latest_price_symbols", len(self.snapshot_prices))
                ),
                "document_count": len(self.snapshot_documents),
                "source_evidence_count": len(self.snapshot_evidence),
            },
            "coverage": {
                "event_item_count": len(self.research_items),
                "event_status_counts": event_counts,
                "perception": "available" if perception_available else "unavailable",
                "company_count": len(companies),
            },
            "companies": companies,
            "sources": [selected_sources[key] for key in sorted(selected_sources)],
            "missing_data": missing_data,
            "limitations": [
                "市場情緒與分析師共識只作次級研究訊號。",
                "本報告沒有配置、風控、回測或成交資訊。",
                DISCLAIMER,
            ],
        }

    def _build_company(
        self,
        symbol: str,
        events: Sequence[Mapping[str, object]],
        perception: Optional[Mapping[str, object]],
        selected_sources: Dict[str, Dict[str, object]],
    ) -> Dict[str, object]:
        price = next(
            (
                item
                for item in self.snapshot_prices
                if str(item.get("symbol", "")).upper() == symbol
            ),
            None,
        )
        latest_price: Optional[Dict[str, object]] = None
        company_evidence_ids: Set[str] = set()
        if price is not None:
            price_evidence_id = price.get("source_evidence_id")
            latest_price = {
                "trade_date": price.get("trade_date"),
                "analysis_close_price": price.get("analysis_close_price"),
                "source_evidence_id": price_evidence_id,
            }
            if isinstance(price_evidence_id, str):
                company_evidence_ids.add(price_evidence_id)
                self._select_snapshot_source(price_evidence_id, selected_sources)

        report_events: List[Dict[str, object]] = []
        combined_risk_flags: Set[str] = set()
        for item in sorted(events, key=lambda row: str(row.get("published_at", ""))):
            evidence_ids = _string_list(item, "evidence_ids")
            counter_ids = _string_list(item, "counter_evidence_ids")
            facts = _mapping_list(item, "fact_values")
            debate = item.get("debate")
            if not isinstance(debate, Mapping):
                raise ResearchReportError("事件 debate 必須是物件")
            bull = debate.get("bull_case")
            bear = debate.get("bear_case")
            adjudication = debate.get("adjudication")
            if not all(isinstance(value, Mapping) for value in (bull, bear, adjudication)):
                raise ResearchReportError("事件 debate 缺少 bull／bear／adjudication")
            risk_flags = _string_list(item, "risk_flags")
            combined_risk_flags.update(risk_flags)
            all_event_ids = set(evidence_ids + counter_ids)
            all_event_ids.update(
                _required_string(fact, "evidence_id") for fact in facts
            )
            price_confirmation = item.get("price_confirmation")
            if isinstance(price_confirmation, Mapping):
                price_id = price_confirmation.get("source_evidence_id")
                if isinstance(price_id, str):
                    all_event_ids.add(price_id)
            for evidence_id in all_event_ids:
                company_evidence_ids.add(evidence_id)
                self._select_snapshot_source(evidence_id, selected_sources)
            report_events.append(
                {
                    "event_id": item.get("event_id"),
                    "event_type": item.get("event_type"),
                    "published_at": item.get("published_at"),
                    "event_summary": item.get("event_summary"),
                    "direction": item.get("direction"),
                    "impact_mechanism": item.get("impact_mechanism"),
                    "research_status": item.get("research_status"),
                    "status_reason": item.get("status_reason"),
                    "facts": facts,
                    "bull_thesis": bull.get("thesis"),
                    "bear_thesis": bear.get("thesis"),
                    "adjudication": dict(adjudication),
                    "price_confirmation": dict(price_confirmation)
                    if isinstance(price_confirmation, Mapping)
                    else {},
                    "evidence_ids": evidence_ids,
                    "counter_evidence_ids": counter_ids,
                    "risk_flags": risk_flags,
                    "uncertainties": _string_list(item, "uncertainties"),
                    "invalidation_signals": _string_list(
                        item, "invalidation_signals"
                    ),
                }
            )

        perception_report: Dict[str, object]
        if perception is None:
            perception_report = {
                "status": "unavailable",
                "research_status": "unavailable",
                "sentiment": None,
                "consensus_metrics": [],
                "expectation_gaps": [],
                "priced_in_assessment": "unknown",
                "rationale": "未提供 MarketPerceptionResult。",
                "risk_flags": ["MARKET_PERCEPTION_UNAVAILABLE"],
                "evidence_ids": [],
            }
            combined_risk_flags.add("MARKET_PERCEPTION_UNAVAILABLE")
        else:
            perception_ids = _string_list(perception, "evidence_ids")
            for evidence_id in perception_ids:
                company_evidence_ids.add(evidence_id)
                self._select_perception_source(evidence_id, selected_sources)
            perception_risks = _string_list(perception, "risk_flags")
            combined_risk_flags.update(perception_risks)
            sentiment = perception.get("sentiment")
            if not isinstance(sentiment, Mapping):
                raise ResearchReportError("MarketPerceptionResult.sentiment 必須是物件")
            perception_report = {
                "status": "available",
                "research_status": perception.get("research_status"),
                "sentiment": dict(sentiment),
                "consensus_metrics": _mapping_list(
                    perception, "consensus_metrics"
                ),
                "expectation_gaps": _mapping_list(
                    perception, "expectation_gaps"
                ),
                "priced_in_assessment": perception.get(
                    "priced_in_assessment"
                ),
                "rationale": perception.get("rationale"),
                "risk_flags": perception_risks,
                "evidence_ids": perception_ids,
            }

        missing_data: List[str] = []
        if price is None:
            missing_data.append("LATEST_PRICE_UNAVAILABLE")
        if not events:
            missing_data.append("EVENT_RESEARCH_UNAVAILABLE")
        if perception is None:
            missing_data.append("MARKET_PERCEPTION_UNAVAILABLE")
        else:
            sentiment = perception_report.get("sentiment")
            if not isinstance(sentiment, Mapping) or sentiment.get("status") != "available":
                missing_data.append("SENTIMENT_INSUFFICIENT_OR_UNAVAILABLE")
            consensus = perception_report.get("consensus_metrics", [])
            if not any(
                isinstance(metric, Mapping) and metric.get("status") == "available"
                for metric in consensus
            ):
                missing_data.append("ANALYST_CONSENSUS_INSUFFICIENT_OR_UNAVAILABLE")
        return {
            "symbol": symbol,
            "latest_price": latest_price,
            "events": report_events,
            "perception": perception_report,
            "risk_flags": sorted(combined_risk_flags),
            "missing_data": missing_data,
            "source_evidence_ids": sorted(company_evidence_ids),
        }

    def _select_snapshot_source(
        self, evidence_id: str, selected: Dict[str, Dict[str, object]]
    ) -> None:
        source = self.snapshot_evidence.get(evidence_id)
        if source is None:
            raise ResearchReportError("Snapshot 來源引用不存在：%s" % evidence_id)
        normalized = {
            "evidence_id": evidence_id,
            "domain": "snapshot",
            "symbol": source.get("symbol"),
            "source": source.get("source"),
            "authority": source.get("authority"),
            "data_type": source.get("data_type"),
            "url": source.get("url"),
            "published_at": source.get("published_at"),
            "available_at": source.get("fetched_at"),
            "content_as_of": source.get("content_as_of"),
        }
        self._select_source(normalized, selected)

    def _select_perception_source(
        self, evidence_id: str, selected: Dict[str, Dict[str, object]]
    ) -> None:
        source = self.perception_evidence.get(evidence_id)
        if source is None:
            raise ResearchReportError("Perception 來源引用不存在：%s" % evidence_id)
        normalized = {
            "evidence_id": evidence_id,
            "domain": "perception",
            "symbol": source.get("symbol"),
            "source": source.get("source_name"),
            "authority": None,
            "data_type": source.get("channel"),
            "url": source.get("source_url"),
            "published_at": source.get("published_at"),
            "available_at": source.get("available_at"),
            "content_as_of": None,
        }
        self._select_source(normalized, selected)

    @staticmethod
    def _select_source(
        source: Dict[str, object], selected: Dict[str, Dict[str, object]]
    ) -> None:
        evidence_id = str(source["evidence_id"])
        existing = selected.get(evidence_id)
        if existing is not None and existing != source:
            raise ResearchReportError("跨資料域 evidence_id 衝突：%s" % evidence_id)
        selected[evidence_id] = source
