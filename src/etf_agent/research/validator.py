"""ResearchResult 與 Fact／Bull／Bear／Adjudicator 辯論的完整驗證。"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import (
    DEBATE_OUTCOMES,
    DIRECTIONS,
    EVENT_TYPES,
    EVIDENCE_QUALITY,
    HORIZON_STATES,
    MATERIALITY_LEVELS,
    NOVELTY_CLASSES,
    PRICE_RESPONSE_STATES,
    RESEARCH_STATUSES,
    RESULT_STATUSES,
    SUBAGENT_ROLES,
    ResearchToolError,
    _parse_time,
    _required_string,
    _string_list,
)
from .repository import HistoricalPriceFeatureRepository


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
