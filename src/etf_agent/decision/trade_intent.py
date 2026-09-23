"""Validation for independent buy, sell, and trade-adjudication packets."""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Sequence, Set, Tuple

from .contracts import (
    DECISION_SCHEMA_VERSION,
    DecisionContext,
    DecisionToolError,
    parse_time,
    required_string,
    string_list,
    canonical_sha256,
    artifact_content_sha256,
)
from .momentum import MomentumResultValidator


PACKET_STATUSES = {"completed", "degraded", "failed"}
BUY_INTENTS = {"buy", "add", "watch", "exclude"}
SELL_INTENTS = {"hold", "trim", "exit", "forced_exit"}
FINAL_INTENTS = {
    "buy",
    "add",
    "hold",
    "trim",
    "exit",
    "forced_exit",
    "exclude",
    "no_trade",
}
HORIZONS = {"short", "medium", "uncertain"}
THESIS_STATES = {"intact", "weakened", "invalidated", "unavailable"}
FORBIDDEN_DECISION_KEYS = {
    "allocation",
    "allocation_pct",
    "cash_amount",
    "commission",
    "estimated_fee",
    "target_weight",
    "target_percentage",
    "weight",
    "shares",
    "quantity",
    "units",
    "lots",
    "lot_count",
    "notional",
    "position_size",
    "position_value",
    "fee",
    "fees",
    "tax",
    "transaction_tax",
    "order",
    "orders",
    "order_side",
    "order_type",
}


def _validate_forbidden_keys(
    value: object, path: str, errors: List[str]
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_DECISION_KEYS:
                errors.append(
                    "%s 不得包含配置、股數、費稅或訂單欄位：%s" % (path, key)
                )
            _validate_forbidden_keys(child, "%s.%s" % (path, key), errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_forbidden_keys(child, "%s[%d]" % (path, index), errors)


def _reject_unknown_keys(
    payload: Mapping[str, object],
    allowed: Set[str],
    prefix: str,
    errors: List[str],
) -> None:
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        errors.append("%s 含未允許欄位：%s" % (prefix, ", ".join(unknown)))


def build_role_input_artifact(
    bundle: Mapping[str, object],
    momentum_result: Mapping[str, object],
    role: str,
) -> Dict[str, object]:
    """Build a peer-free artifact to hand to one Buy or Sell execution context."""
    if role not in {"buy", "sell"}:
        raise DecisionToolError("role 必須是 buy 或 sell")
    context = DecisionContext(bundle)
    momentum_errors = MomentumResultValidator(context.bundle).validate(momentum_result)
    if momentum_errors:
        raise DecisionToolError("MomentumResult 驗證失敗：" + "；".join(momentum_errors))
    body: Dict[str, object] = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "role": role,
        "decision_bundle_id": context.bundle_id,
        "bundle_hash": context.bundle_hash,
        "momentum_result_id": momentum_result.get("result_id"),
        "decision_bundle": context.bundle,
        "momentum_result": dict(momentum_result),
        "forbidden_peer_role": "sell" if role == "buy" else "buy",
    }
    body["role_input_sha256"] = canonical_sha256(body)
    return body


class _IntentPacketValidator:
    role = ""
    allowed_intents: Set[str] = set()

    def __init__(
        self,
        bundle: Mapping[str, object],
        momentum_result: Mapping[str, object],
    ):
        self.context = DecisionContext(bundle)
        self.momentum_result = dict(momentum_result)
        self.momentum_by_symbol = {
            str(item.get("symbol", "")).upper(): dict(item)
            for item in momentum_result.get("items", [])
            if isinstance(item, Mapping)
        }

    def validate(self, packet: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        _validate_forbidden_keys(packet, "%sIntentPacket" % self.role.title(), errors)
        momentum_errors = MomentumResultValidator(self.context.bundle).validate(
            self.momentum_result
        )
        errors.extend(momentum_errors)
        self._validate_envelope(packet, errors)
        items = packet.get("items")
        if not isinstance(items, list):
            errors.append("items 必須是陣列")
            return errors
        seen: Set[str] = set()
        claim_ids: Set[str] = set()
        for index, item in enumerate(items):
            prefix = "items[%d]" % index
            if not isinstance(item, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            self._validate_item(item, prefix, errors, seen, claim_ids)
        self._validate_coverage(seen, errors)
        if packet.get("status") == "completed" and packet.get("errors"):
            errors.append("completed packet 不得同時包含 errors")
        return errors

    def _validate_envelope(
        self, packet: Mapping[str, object], errors: List[str]
    ) -> None:
        _reject_unknown_keys(
            packet,
            {
                "schema_version",
                "packet_id",
                "role",
                "bundle_id",
                "snapshot_id",
                "decision_cutoff",
                "bundle_hash",
                "momentum_result_id",
                "status",
                "content_sha256",
                "dependencies",
                "items",
                "errors",
            },
            "%sIntentPacket" % self.role.title(),
            errors,
        )
        for field in (
            "schema_version",
            "packet_id",
            "role",
            "bundle_id",
            "snapshot_id",
            "decision_cutoff",
            "bundle_hash",
            "momentum_result_id",
            "status",
            "content_sha256",
        ):
            try:
                required_string(packet, field)
            except DecisionToolError as error:
                errors.append(str(error))
        if packet.get("schema_version") != DECISION_SCHEMA_VERSION:
            errors.append("schema_version 必須為 %s" % DECISION_SCHEMA_VERSION)
        if packet.get("content_sha256") != artifact_content_sha256(packet):
            errors.append("content_sha256 與 IntentPacket 內容不一致")
        if packet.get("role") != self.role:
            errors.append("role 必須為 %s" % self.role)
        if packet.get("bundle_id") != self.context.bundle_id:
            errors.append("bundle_id 與 DecisionInputBundle 不一致")
        if packet.get("snapshot_id") != self.context.snapshot_id:
            errors.append("snapshot_id 與 DecisionInputBundle 不一致")
        if packet.get("bundle_hash") != self.context.bundle_hash:
            errors.append("bundle_hash 與 DecisionInputBundle 不一致")
        if packet.get("momentum_result_id") != self.momentum_result.get("result_id"):
            errors.append("momentum_result_id 不一致")
        try:
            if parse_time(packet.get("decision_cutoff"), "decision_cutoff") != parse_time(
                self.context.decision_cutoff, "bundle.decision_cutoff"
            ):
                errors.append("decision_cutoff 與 DecisionInputBundle 不一致")
        except DecisionToolError as error:
            errors.append(str(error))
        if packet.get("status") not in PACKET_STATUSES:
            errors.append("status 必須是 completed、degraded 或 failed")
        elif packet.get("status") != "completed":
            errors.append("只有 status=completed 的 packet 可以進入買賣裁決")
        dependencies = packet.get("dependencies")
        if not isinstance(dependencies, Mapping):
            errors.append("dependencies 必須是物件")
        else:
            _reject_unknown_keys(
                dependencies,
                {
                    "decision_bundle_id",
                    "momentum_result_id",
                    "role_input_sha256",
                    "peer_packet_ids",
                },
                "dependencies",
                errors,
            )
            if dependencies.get("decision_bundle_id") != self.context.bundle_id:
                errors.append("dependencies.decision_bundle_id 不一致")
            if dependencies.get("momentum_result_id") != self.momentum_result.get(
                "result_id"
            ):
                errors.append("dependencies.momentum_result_id 不一致")
            expected_role_input = build_role_input_artifact(
                self.context.bundle, self.momentum_result, self.role
            )["role_input_sha256"]
            if dependencies.get("role_input_sha256") != expected_role_input:
                errors.append("dependencies.role_input_sha256 不一致")
            try:
                peers = string_list(dependencies, "peer_packet_ids")
                if peers:
                    errors.append("Buy 與 Sell packet 不得讀取對方 packet")
            except DecisionToolError as error:
                errors.append("dependencies.%s" % error)
        try:
            string_list(packet, "errors")
        except DecisionToolError as error:
            errors.append(str(error))

    def _validate_item(
        self,
        item: Mapping[str, object],
        prefix: str,
        errors: List[str],
        seen: Set[str],
        claim_ids: Set[str],
    ) -> None:
        symbol = str(item.get("symbol", "")).upper()
        common_allowed = {
            "symbol",
            "intent",
            "rationale",
            "status_reason",
            "horizon",
            "evidence_ids",
            "risk_flags",
            "invalidation_conditions",
            "claims",
        }
        role_allowed = (
            common_allowed | {"catalyst_summary"}
            if self.role == "buy"
            else common_allowed | {"thesis_status"}
        )
        _reject_unknown_keys(item, role_allowed, prefix, errors)
        if not symbol:
            errors.append("%s.symbol 缺少必要字串" % prefix)
            return
        if symbol in seen:
            errors.append("%s.symbol 不得重複" % prefix)
        seen.add(symbol)
        if symbol not in self.context.universe:
            errors.append("%s.symbol 不在 DecisionInputBundle 交易池" % prefix)
        intent = item.get("intent")
        if intent not in self.allowed_intents:
            errors.append("%s.intent 不在允許清單" % prefix)
        for field in ("rationale", "status_reason"):
            try:
                required_string(item, field)
            except DecisionToolError as error:
                errors.append("%s.%s" % (prefix, error))
        if item.get("horizon") not in HORIZONS:
            errors.append("%s.horizon 不在允許清單" % prefix)
        lists: Dict[str, List[str]] = {}
        for field in ("evidence_ids", "risk_flags", "invalidation_conditions"):
            try:
                lists[field] = string_list(item, field)
            except DecisionToolError as error:
                errors.append("%s.%s" % (prefix, error))
                lists[field] = []
        if not lists["evidence_ids"]:
            errors.append("%s.evidence_ids 不得為空" % prefix)
        self.context.validate_evidence(
            lists["evidence_ids"], symbol, "%s.evidence_ids" % prefix, errors
        )
        if intent in {"buy", "add", "trim", "exit"} and not lists[
            "invalidation_conditions"
        ]:
            errors.append("%s 必須提供 invalidation_conditions" % prefix)
        claims = item.get("claims")
        if not isinstance(claims, list) or not claims:
            errors.append("%s.claims 必須是非空陣列" % prefix)
            return
        item_claim_evidence: Set[str] = set()
        for claim_index, claim in enumerate(claims):
            claim_prefix = "%s.claims[%d]" % (prefix, claim_index)
            if not isinstance(claim, Mapping):
                errors.append("%s 必須是物件" % claim_prefix)
                continue
            _reject_unknown_keys(
                claim,
                {"claim_id", "text", "evidence_ids"},
                claim_prefix,
                errors,
            )
            try:
                claim_id = required_string(claim, "claim_id")
                required_string(claim, "text")
            except DecisionToolError as error:
                errors.append("%s.%s" % (claim_prefix, error))
                claim_id = ""
            if claim_id in claim_ids:
                errors.append("%s.claim_id 不得重複" % claim_prefix)
            claim_ids.add(claim_id)
            try:
                evidence_ids = string_list(claim, "evidence_ids")
            except DecisionToolError as error:
                errors.append("%s.%s" % (claim_prefix, error))
                evidence_ids = []
            if not evidence_ids:
                errors.append("%s.evidence_ids 不得為空" % claim_prefix)
            item_claim_evidence.update(evidence_ids)
            self.context.validate_evidence(
                evidence_ids, symbol, "%s.evidence_ids" % claim_prefix, errors
            )
        if not item_claim_evidence.issubset(set(lists["evidence_ids"])):
            errors.append("%s.evidence_ids 未包含 claims 的全部證據" % prefix)
        self._validate_role_item(item, symbol, intent, prefix, errors)

    def _validate_role_item(
        self,
        item: Mapping[str, object],
        symbol: str,
        intent: object,
        prefix: str,
        errors: List[str],
    ) -> None:
        raise NotImplementedError

    def _validate_coverage(self, seen: Set[str], errors: List[str]) -> None:
        return None


class BuyIntentPacketValidator(_IntentPacketValidator):
    role = "buy"
    allowed_intents = BUY_INTENTS

    def _validate_role_item(
        self,
        item: Mapping[str, object],
        symbol: str,
        intent: object,
        prefix: str,
        errors: List[str],
    ) -> None:
        try:
            required_string(item, "catalyst_summary")
        except DecisionToolError as error:
            errors.append("%s.%s" % (prefix, error))
        if intent == "buy" and symbol in self.context.held_symbols:
            errors.append("%s 已持有股票不得使用 buy，應使用 add" % prefix)
        if intent == "add" and symbol not in self.context.held_symbols:
            errors.append("%s 未持有股票不得使用 add，應使用 buy" % prefix)
        momentum = self.momentum_by_symbol.get(symbol)
        if intent in {"buy", "add"} and (
            momentum is None or momentum.get("status") != "available"
        ):
            errors.append("%s buy/add 必須有可用 MomentumResult" % prefix)


class SellIntentPacketValidator(_IntentPacketValidator):
    role = "sell"
    allowed_intents = SELL_INTENTS

    def _validate_role_item(
        self,
        item: Mapping[str, object],
        symbol: str,
        intent: object,
        prefix: str,
        errors: List[str],
    ) -> None:
        if symbol not in self.context.held_symbols:
            errors.append("%s Sell packet 只能包含目前持股" % prefix)
        if item.get("thesis_status") not in THESIS_STATES:
            errors.append("%s.thesis_status 不在允許清單" % prefix)

    def _validate_coverage(self, seen: Set[str], errors: List[str]) -> None:
        missing = sorted(self.context.held_symbols - seen)
        extra = sorted(seen - self.context.held_symbols)
        if missing:
            errors.append("Sell packet 未覆蓋全部持股：%s" % ", ".join(missing))
        if extra:
            errors.append("Sell packet 包含非持股：%s" % ", ".join(extra))


class TradeDebateValidator:
    """Validate common inputs and peer-free role-artifact dependencies."""

    def __init__(
        self,
        bundle: Mapping[str, object],
        momentum_result: Mapping[str, object],
    ):
        self.context = DecisionContext(bundle)
        self.momentum_result = dict(momentum_result)

    def validate(self, debate: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        _reject_unknown_keys(
            debate,
            {
                "schema_version",
                "debate_id",
                "bundle_id",
                "snapshot_id",
                "decision_cutoff",
                "bundle_hash",
                "momentum_result_id",
                "content_sha256",
                "packets",
            },
            "TradeDebateBundle",
            errors,
        )
        for field in (
            "schema_version",
            "debate_id",
            "bundle_id",
            "snapshot_id",
            "decision_cutoff",
            "bundle_hash",
            "momentum_result_id",
            "content_sha256",
        ):
            try:
                required_string(debate, field)
            except DecisionToolError as error:
                errors.append(str(error))
        if debate.get("schema_version") != DECISION_SCHEMA_VERSION:
            errors.append("schema_version 必須為 %s" % DECISION_SCHEMA_VERSION)
        if debate.get("content_sha256") != artifact_content_sha256(debate):
            errors.append("content_sha256 與 TradeDebateBundle 內容不一致")
        expected = {
            "bundle_id": self.context.bundle_id,
            "snapshot_id": self.context.snapshot_id,
            "bundle_hash": self.context.bundle_hash,
            "momentum_result_id": self.momentum_result.get("result_id"),
        }
        for field, value in expected.items():
            if debate.get(field) != value:
                errors.append("%s 不一致" % field)
        try:
            if parse_time(debate.get("decision_cutoff"), "decision_cutoff") != parse_time(
                self.context.decision_cutoff, "bundle.decision_cutoff"
            ):
                errors.append("decision_cutoff 不一致")
        except DecisionToolError as error:
            errors.append(str(error))
        packets = debate.get("packets")
        if not isinstance(packets, list) or len(packets) != 2:
            errors.append("packets 必須剛好包含 Buy 與 Sell 兩個 packet")
            return errors
        by_role: Dict[str, Mapping[str, object]] = {}
        for packet in packets:
            if not isinstance(packet, Mapping):
                errors.append("packets 每一項都必須是物件")
                continue
            role = str(packet.get("role", ""))
            if role in by_role:
                errors.append("packets role 不得重複：%s" % role)
            by_role[role] = packet
        if set(by_role) != {"buy", "sell"}:
            errors.append("packets 必須包含 role=buy 與 role=sell")
            return errors
        errors.extend(
            "buy_packet：%s" % error
            for error in BuyIntentPacketValidator(
                self.context.bundle, self.momentum_result
            ).validate(by_role["buy"])
        )
        errors.extend(
            "sell_packet：%s" % error
            for error in SellIntentPacketValidator(
                self.context.bundle, self.momentum_result
            ).validate(by_role["sell"])
        )
        packet_ids = [str(packet.get("packet_id", "")) for packet in by_role.values()]
        if len(set(packet_ids)) != 2 or not all(packet_ids):
            errors.append("Buy 與 Sell packet_id 必須存在且不同")
        claim_ids: Set[str] = set()
        for role, packet in by_role.items():
            for item in packet.get("items", []):
                if not isinstance(item, Mapping):
                    continue
                for claim in item.get("claims", []):
                    if not isinstance(claim, Mapping) or not claim.get("claim_id"):
                        continue
                    claim_id = str(claim["claim_id"])
                    if claim_id in claim_ids:
                        errors.append("Buy／Sell claim_id 必須全域唯一：%s" % claim_id)
                    claim_ids.add(claim_id)
        return errors


class TradeIntentResultValidator:
    """Validate adjudication without allowing new symbols, claims, or numbers."""

    def __init__(
        self,
        bundle: Mapping[str, object],
        momentum_result: Mapping[str, object],
        debate: Mapping[str, object],
    ):
        self.context = DecisionContext(bundle)
        self.momentum_result = dict(momentum_result)
        self.debate = dict(debate)

    def validate(self, result: Mapping[str, object]) -> List[str]:
        errors = TradeDebateValidator(
            self.context.bundle, self.momentum_result
        ).validate(self.debate)
        if errors:
            return ["TradeDebateBundle：%s" % error for error in errors]
        self._validate_envelope(result, errors)
        _validate_forbidden_keys(result, "TradeIntentResult", errors)
        packets = {
            str(packet["role"]): packet
            for packet in self.debate["packets"]
            if isinstance(packet, Mapping)
        }
        source_items: Dict[str, List[Mapping[str, object]]] = {}
        for packet in packets.values():
            for item in packet.get("items", []):
                if isinstance(item, Mapping):
                    source_items.setdefault(str(item.get("symbol", "")).upper(), []).append(item)
        items = result.get("items")
        if not isinstance(items, list):
            errors.append("items 必須是陣列")
            return errors
        seen: Set[str] = set()
        for index, item in enumerate(items):
            prefix = "items[%d]" % index
            if not isinstance(item, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            self._validate_item(item, source_items, prefix, seen, errors)
        missing = sorted(set(source_items) - seen)
        extra = sorted(seen - set(source_items))
        if missing:
            errors.append("TradeIntentResult 未裁決全部輸入股票：%s" % ", ".join(missing))
        if extra:
            errors.append("TradeIntentResult 新增輸入不存在的股票：%s" % ", ".join(extra))
        if result.get("status") == "completed" and result.get("errors"):
            errors.append("completed 結果不得同時包含 errors")
        return errors

    def _validate_envelope(
        self, result: Mapping[str, object], errors: List[str]
    ) -> None:
        _reject_unknown_keys(
            result,
            {
                "schema_version",
                "result_id",
                "bundle_id",
                "snapshot_id",
                "decision_cutoff",
                "bundle_hash",
                "momentum_result_id",
                "debate_id",
                "source_packet_ids",
                "status",
                "content_sha256",
                "items",
                "errors",
            },
            "TradeIntentResult",
            errors,
        )
        for field in (
            "schema_version",
            "result_id",
            "bundle_id",
            "snapshot_id",
            "decision_cutoff",
            "bundle_hash",
            "momentum_result_id",
            "debate_id",
            "status",
            "content_sha256",
        ):
            try:
                required_string(result, field)
            except DecisionToolError as error:
                errors.append(str(error))
        if result.get("schema_version") != DECISION_SCHEMA_VERSION:
            errors.append("schema_version 必須為 %s" % DECISION_SCHEMA_VERSION)
        if result.get("content_sha256") != artifact_content_sha256(result):
            errors.append("content_sha256 與 TradeIntentResult 內容不一致")
        expected = {
            "bundle_id": self.context.bundle_id,
            "snapshot_id": self.context.snapshot_id,
            "bundle_hash": self.context.bundle_hash,
            "momentum_result_id": self.momentum_result.get("result_id"),
            "debate_id": self.debate.get("debate_id"),
        }
        for field, value in expected.items():
            if result.get(field) != value:
                errors.append("%s 不一致" % field)
        try:
            if parse_time(result.get("decision_cutoff"), "decision_cutoff") != parse_time(
                self.context.decision_cutoff, "bundle.decision_cutoff"
            ):
                errors.append("decision_cutoff 不一致")
        except DecisionToolError as error:
            errors.append(str(error))
        if result.get("status") not in PACKET_STATUSES:
            errors.append("status 必須是 completed、degraded 或 failed")
        elif result.get("status") != "completed":
            errors.append("只有 status=completed 的 TradeIntentResult 可以交給下游")
        try:
            sources = string_list(result, "source_packet_ids")
        except DecisionToolError as error:
            errors.append(str(error))
            sources = []
        expected_sources = {
            str(packet.get("packet_id")) for packet in self.debate.get("packets", [])
        }
        if set(sources) != expected_sources or len(sources) != 2:
            errors.append("source_packet_ids 必須剛好引用 Buy 與 Sell packet")
        try:
            string_list(result, "errors")
        except DecisionToolError as error:
            errors.append(str(error))

    def _validate_item(
        self,
        item: Mapping[str, object],
        source_items: Mapping[str, List[Mapping[str, object]]],
        prefix: str,
        seen: Set[str],
        errors: List[str],
    ) -> None:
        symbol = str(item.get("symbol", "")).upper()
        _reject_unknown_keys(
            item,
            {
                "symbol",
                "intent",
                "rationale",
                "status_reason",
                "evidence_ids",
                "adopted_claim_ids",
                "rejected_claim_ids",
                "unresolved_questions",
                "invalidation_conditions",
            },
            prefix,
            errors,
        )
        if not symbol:
            errors.append("%s.symbol 缺少必要字串" % prefix)
            return
        if symbol in seen:
            errors.append("%s.symbol 不得重複" % prefix)
        seen.add(symbol)
        if item.get("intent") not in FINAL_INTENTS:
            errors.append("%s.intent 不在允許清單" % prefix)
        held = symbol in self.context.held_symbols
        if held and item.get("intent") == "buy":
            errors.append("%s 已持股不得裁決為 buy" % prefix)
        if not held and item.get("intent") in {
            "add",
            "hold",
            "trim",
            "exit",
            "forced_exit",
        }:
            errors.append("%s 非持股不得使用持股型 intent" % prefix)
        for field in ("rationale", "status_reason"):
            try:
                required_string(item, field)
            except DecisionToolError as error:
                errors.append("%s.%s" % (prefix, error))
        source_claims: Set[str] = set()
        source_evidence: Set[str] = set()
        claim_evidence: Dict[str, Set[str]] = {}
        claim_intents: Dict[str, str] = {}
        source_intents: Set[str] = set()
        for source in source_items.get(symbol, []):
            if source.get("intent"):
                source_intents.add(str(source["intent"]))
            source_evidence.update(str(value) for value in source.get("evidence_ids", []))
            for claim in source.get("claims", []):
                if isinstance(claim, Mapping) and claim.get("claim_id"):
                    claim_id = str(claim["claim_id"])
                    source_claims.add(claim_id)
                    claim_intents[claim_id] = str(source.get("intent", ""))
                    claim_evidence[claim_id] = {
                        str(value) for value in claim.get("evidence_ids", [])
                    }
        try:
            evidence_ids = string_list(item, "evidence_ids")
            adopted = string_list(item, "adopted_claim_ids")
            rejected = string_list(item, "rejected_claim_ids")
            string_list(item, "unresolved_questions")
            string_list(item, "invalidation_conditions")
        except DecisionToolError as error:
            errors.append("%s.%s" % (prefix, error))
            return
        if not set(evidence_ids).issubset(source_evidence):
            errors.append("%s.evidence_ids 新增 Buy／Sell packet 沒有的證據" % prefix)
        self.context.validate_evidence(
            evidence_ids, symbol, "%s.evidence_ids" % prefix, errors
        )
        adopted_set = set(adopted)
        rejected_set = set(rejected)
        if adopted_set & rejected_set:
            errors.append("%s 同一 claim 不得同時採納與否決" % prefix)
        if adopted_set | rejected_set != source_claims:
            errors.append("%s 必須裁決 Buy／Sell 的全部 claim_id，且不得新增" % prefix)
        required_evidence: Set[str] = set()
        for claim_id in adopted_set:
            required_evidence.update(claim_evidence.get(claim_id, set()))
        if not required_evidence.issubset(set(evidence_ids)):
            errors.append("%s.evidence_ids 未包含全部採納 claims 的證據" % prefix)
        intent = str(item.get("intent", ""))
        supporting_intents = {
            claim_intents.get(claim_id, "") for claim_id in adopted_set
        }
        if intent in {"buy", "add", "trim", "exit", "forced_exit"} and intent not in supporting_intents:
            errors.append("%s 可執行 intent 必須採納同方向來源 claim" % prefix)
        self._validate_intent_against_sources(
            intent, source_intents, held, prefix, errors
        )

    @staticmethod
    def _validate_intent_against_sources(
        intent: str,
        source_intents: Set[str],
        held: bool,
        prefix: str,
        errors: List[str],
    ) -> None:
        if intent == "buy" and "buy" not in source_intents:
            errors.append("%s 不得將未被 Buy packet 提出的股票升級為 buy" % prefix)
        if intent == "add" and "add" not in source_intents:
            errors.append("%s 不得將未被 Buy packet 提出的持股升級為 add" % prefix)
        if intent in {"trim", "exit", "forced_exit"} and intent not in source_intents:
            errors.append("%s 不得產生 Sell packet 未提出的退出強度" % prefix)
        if held and intent == "hold" and not (
            source_intents & {"hold", "add", "trim", "exit", "forced_exit"}
        ):
            errors.append("%s hold 缺少持股來源意圖" % prefix)
