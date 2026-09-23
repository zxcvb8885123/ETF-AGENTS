"""Build an auditable, non-transactional research report from validated artifacts."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple


REPORT_STATUSES = {"completed", "degraded"}
UPSTREAM_STATUSES = {"completed", "degraded"}
FORBIDDEN_REPORT_FIELDS = {
    "decision",
    "decisions",
    "order",
    "orders",
    "target_weight",
    "target_weights",
    "shares",
}
DISCLAIMER = "本報告僅整理已驗證研究資料，不是投資建議，且不包含權重、股數或訂單。"


class ResearchReportError(ValueError):
    """Raised when a report would lose provenance or mix incompatible inputs."""


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ResearchReportError("%s 必須是包含時區的時間字串" % field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ResearchReportError("%s 無法解析：%s" % (field, value)) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchReportError("%s 必須包含時區" % field)
    return parsed.astimezone(timezone.utc)


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ResearchReportError("缺少必要字串欄位：%s" % field)
    return value.strip()


def _string_list(payload: Mapping[str, object], field: str) -> List[str]:
    value = payload.get(field)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ResearchReportError("%s 必須是字串陣列" % field)
    return [item.strip() for item in value]


def _mapping_list(payload: Mapping[str, object], field: str) -> List[Dict[str, object]]:
    value = payload.get(field)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ResearchReportError("%s 必須是物件陣列" % field)
    return [dict(item) for item in value]


def _same_cutoff(left: object, right: object) -> bool:
    return _parse_time(left, "decision_cutoff") == _parse_time(right, "decision_cutoff")


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


class ResearchReportValidator:
    """Rebuild the report and reject any altered narrative or number."""

    def __init__(self, builder: ResearchReportBuilder):
        self.builder = builder

    def validate(self, report: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        if report.get("schema_version") != "1.0":
            errors.append("schema_version 必須為 1.0")
        if report.get("status") not in REPORT_STATUSES:
            errors.append("status 必須是 completed 或 degraded")
        for field in ("report_id", "generated_at", "snapshot_id", "decision_cutoff"):
            if not isinstance(report.get(field), str) or not str(report.get(field)).strip():
                errors.append("缺少必要字串欄位：%s" % field)
        forbidden = FORBIDDEN_REPORT_FIELDS.intersection(report.keys())
        if forbidden:
            errors.append("ResearchReport 不得包含交易欄位：%s" % ", ".join(sorted(forbidden)))
        try:
            expected = self.builder.build(
                report_id=str(report.get("report_id", "")),
                generated_at=str(report.get("generated_at", "")),
            )
            difference = _first_difference(expected, report)
            if difference is not None:
                errors.append("ResearchReport 與確定性重建結果不一致：%s" % difference)
        except ResearchReportError as error:
            errors.append(str(error))
        return errors


def _first_difference(expected: object, actual: object, path: str = "$") -> Optional[str]:
    if type(expected) is not type(actual):
        return "%s 型別不同" % path
    if isinstance(expected, Mapping):
        expected_keys = set(expected)
        actual_keys = set(actual)
        if expected_keys != actual_keys:
            return "%s 欄位不同" % path
        for key in sorted(expected_keys):
            difference = _first_difference(expected[key], actual[key], "%s.%s" % (path, key))
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return "%s 長度不同" % path
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
            difference = _first_difference(
                expected_item, actual_item, "%s[%d]" % (path, index)
            )
            if difference is not None:
                return difference
        return None
    if expected != actual:
        return "%s 值不同" % path
    return None


class ResearchReportMarkdownRenderer:
    """Render only fields already present in a validated ResearchReport."""

    def render(self, report: Mapping[str, object]) -> str:
        lines = [
            "# %s" % _md_text(report.get("title")),
            "",
            "- 報告 ID：`%s`" % _md_code(report.get("report_id")),
            "- 資料截止：`%s`" % _md_code(report.get("decision_cutoff")),
            "- Snapshot：`%s`" % _md_code(report.get("snapshot_id")),
            "- 狀態：`%s`" % _md_code(report.get("status")),
            "",
            "> %s" % _md_text(DISCLAIMER),
            "",
            "## 資料品質",
            "",
        ]
        quality = report.get("data_quality", {})
        if not isinstance(quality, Mapping):
            raise ResearchReportError("ResearchReport.data_quality 必須是物件")
        lines.extend(
            [
                "- 最近交易日：`%s`" % _md_code(quality.get("latest_trade_date")),
                "- 交易池數量：%s" % _md_text(quality.get("universe_size")),
                "- 最新行情覆蓋：%s" % _md_text(quality.get("latest_price_symbols")),
                "- 文件數量：%s" % _md_text(quality.get("document_count")),
                "- 品質旗標：%s"
                % _md_list(quality.get("quality_flags"), empty="無"),
                "",
                "## 研究覆蓋",
                "",
            ]
        )
        coverage = report.get("coverage", {})
        if not isinstance(coverage, Mapping):
            raise ResearchReportError("ResearchReport.coverage 必須是物件")
        lines.extend(
            [
                "- 事件研究數量：%s" % _md_text(coverage.get("event_item_count")),
                "- 市場情緒／分析師資料：`%s`"
                % _md_code(coverage.get("perception")),
                "- 公司數量：%s" % _md_text(coverage.get("company_count")),
                "",
            ]
        )
        companies = report.get("companies")
        if not isinstance(companies, list):
            raise ResearchReportError("ResearchReport.companies 必須是陣列")
        for company in companies:
            if not isinstance(company, Mapping):
                raise ResearchReportError("ResearchReport.company 必須是物件")
            lines.extend(self._render_company(company))
        lines.extend(["## 來源", ""])
        sources = report.get("sources")
        if not isinstance(sources, list):
            raise ResearchReportError("ResearchReport.sources 必須是陣列")
        if not sources:
            lines.append("- 無被引用來源。")
        for source in sources:
            if not isinstance(source, Mapping):
                raise ResearchReportError("ResearchReport.source 必須是物件")
            lines.append(
                "- `%s`｜%s｜%s｜URL `%s`"
                % (
                    _md_code(source.get("evidence_id")),
                    _md_text(source.get("source") or "未知來源"),
                    _md_text(source.get("data_type") or "未知類型"),
                    _md_code(source.get("url") or "unavailable"),
                )
            )
        lines.extend(["", "## 限制", ""])
        for limitation in report.get("limitations", []):
            lines.append("- %s" % _md_text(limitation))
        missing = report.get("missing_data", [])
        if missing:
            lines.extend(
                ["", "## 全域缺漏", "", "- %s" % _md_list(missing, empty="無")]
            )
        return "\n".join(lines).rstrip() + "\n"

    def _render_company(self, company: Mapping[str, object]) -> List[str]:
        lines = ["## %s" % _md_text(company.get("symbol")), ""]
        price = company.get("latest_price")
        if isinstance(price, Mapping):
            lines.append(
                "- 最新分析價格：%s（%s，證據 `%s`）"
                % (
                    _md_text(price.get("analysis_close_price")),
                    _md_text(price.get("trade_date")),
                    _md_code(price.get("source_evidence_id")),
                )
            )
        else:
            lines.append("- 最新分析價格：unavailable")
        events = company.get("events", [])
        if not isinstance(events, list):
            raise ResearchReportError("company.events 必須是陣列")
        lines.extend(["", "### 事件研究", ""])
        if not events:
            lines.append("- unavailable")
        for event in events:
            if not isinstance(event, Mapping):
                raise ResearchReportError("company.event 必須是物件")
            lines.extend(
                [
                    "#### %s" % _md_text(event.get("event_id")),
                    "",
                    "- 摘要：%s" % _md_text(event.get("event_summary")),
                    "- 方向／狀態：`%s`／`%s`"
                    % (
                        _md_code(event.get("direction")),
                        _md_code(event.get("research_status")),
                    ),
                    "- 影響機制：%s" % _md_text(event.get("impact_mechanism")),
                    "- 多方：%s" % _md_text(event.get("bull_thesis")),
                    "- 空方：%s" % _md_text(event.get("bear_thesis")),
                ]
            )
            adjudication = event.get("adjudication", {})
            if isinstance(adjudication, Mapping):
                lines.append(
                    "- 裁決：`%s`；%s"
                    % (
                        _md_code(adjudication.get("prevailing_case")),
                        _md_text(adjudication.get("rationale")),
                    )
                )
            facts = event.get("facts", [])
            if isinstance(facts, list) and facts:
                lines.extend(["", "| 事實 | 數值 | 單位 | 期間 | 證據 |", "| --- | ---: | --- | --- | --- |"])
                for fact in facts:
                    if isinstance(fact, Mapping):
                        lines.append(
                            "| %s | %s | %s | %s | `%s` |"
                            % (
                                _md_text(fact.get("name")),
                                _md_text(fact.get("value")),
                                _md_text(fact.get("unit")),
                                _md_text(fact.get("period")),
                                _md_code(fact.get("evidence_id")),
                            )
                        )
                lines.append("")
            lines.append(
                "- 風險：%s" % _md_list(event.get("risk_flags", []), empty="無")
            )
            lines.append(
                "- 失效條件：%s"
                % _md_list(event.get("invalidation_signals", []), empty="無")
            )
            lines.append("")
        perception = company.get("perception")
        lines.extend(["### 市場情緒與分析師共識", ""])
        if not isinstance(perception, Mapping) or perception.get("status") == "unavailable":
            lines.append("- unavailable：未提供可用的市場情緒或分析師研究結果。")
        else:
            sentiment = perception.get("sentiment", {})
            if isinstance(sentiment, Mapping):
                lines.append(
                    "- 情緒：`%s`／`%s`，樣本 %s、來源 %s"
                    % (
                        _md_code(sentiment.get("status")),
                        _md_code(sentiment.get("direction")),
                        _md_text(sentiment.get("item_count")),
                        _md_text(sentiment.get("source_count")),
                    )
                )
            lines.append(
                "- 已反映判讀：`%s`"
                % _md_code(perception.get("priced_in_assessment"))
            )
            lines.append("- 說明：%s" % _md_text(perception.get("rationale")))
            metrics = perception.get("consensus_metrics", [])
            if isinstance(metrics, list):
                for metric in metrics:
                    if isinstance(metric, Mapping):
                        revision = metric.get("revision_pct")
                        revision_text = (
                            "unavailable" if revision is None else "%s%%" % _md_text(revision)
                        )
                        lines.append(
                            "- 共識 %s %s：中位數 %s %s，貢獻者 %s，修正 %s"
                            % (
                                _md_text(metric.get("metric")),
                                _md_text(metric.get("forecast_period")),
                                _md_text(metric.get("median")),
                                _md_text(metric.get("unit")),
                                _md_text(metric.get("contributor_count")),
                                revision_text,
                            )
                        )
        lines.extend(
            [
                "",
                "### 風險與缺漏",
                "",
                "- 風險旗標：%s"
                % _md_list(company.get("risk_flags", []), empty="無"),
                "- 缺漏：%s" % _md_list(company.get("missing_data", []), empty="無"),
                "",
            ]
        )
        return lines


def _md_text(value: object) -> str:
    if value is None:
        return "unavailable"
    text = " ".join(str(value).splitlines()).strip()
    for character in ("\\", "`", "*", "_", "{", "}", "[", "]", "<", ">", "#", "|"):
        text = text.replace(character, "\\" + character)
    return text or "unavailable"


def _md_code(value: object) -> str:
    if value is None:
        return "unavailable"
    return " ".join(str(value).replace("`", "'").splitlines()).strip() or "unavailable"


def _md_list(value: object, empty: str) -> str:
    if not isinstance(value, list) or not value:
        return empty
    return "、".join(_md_text(item) for item in value)


class ResearchReportApplicationService:
    """File-oriented facade for the research-report Skill CLI."""

    @staticmethod
    def read_json(path: Path, label: str) -> Mapping[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ResearchReportError("無法讀取 %s：%s" % (label, error)) from error
        if not isinstance(payload, Mapping):
            raise ResearchReportError("%s 必須是 JSON 物件" % label)
        return payload

    @classmethod
    def from_paths(
        cls,
        snapshot_path: Path,
        research_path: Path,
        perception_path: Optional[Path] = None,
        perception_bundle_path: Optional[Path] = None,
    ) -> "ResearchReportApplicationService":
        if (perception_path is None) != (perception_bundle_path is None):
            raise ResearchReportError(
                "--perception 與 --perception-bundle 必須同時提供"
            )
        snapshot = cls.read_json(snapshot_path, "ResearchSnapshot")
        research = cls.read_json(research_path, "ResearchResult")
        perception = (
            cls.read_json(perception_path, "MarketPerceptionResult")
            if perception_path is not None
            else None
        )
        bundle = (
            cls.read_json(perception_bundle_path, "PerceptionDataBundle")
            if perception_bundle_path is not None
            else None
        )
        return cls(ResearchReportBuilder(snapshot, research, perception, bundle))

    def __init__(self, builder: ResearchReportBuilder):
        self.builder = builder
        self.renderer = ResearchReportMarkdownRenderer()

    def build_files(
        self,
        json_output: Path,
        markdown_output: Path,
        report_id: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> Dict[str, object]:
        report = self.builder.build(report_id=report_id, generated_at=generated_at)
        errors = ResearchReportValidator(self.builder).validate(report)
        if errors:
            raise ResearchReportError("報告建立後驗證失敗：%s" % "; ".join(errors))
        markdown = self.renderer.render(report)
        json_output.parent.mkdir(parents=True, exist_ok=True)
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        markdown_output.write_text(markdown, encoding="utf-8")
        return {
            "valid": True,
            "report_id": report["report_id"],
            "status": report["status"],
            "json_output": str(json_output),
            "markdown_output": str(markdown_output),
        }

    def validate_file(self, input_path: Path) -> Dict[str, object]:
        report = self.read_json(input_path, "ResearchReport")
        errors = ResearchReportValidator(self.builder).validate(report)
        return {"valid": not errors, "errors": errors}
