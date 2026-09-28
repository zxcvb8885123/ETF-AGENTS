"""研究團隊：多頭／空頭研究員的隔離輸入、StancePacket 與 ResearchDebateBundle。

兩位研究員讀取同一份共同輸入（DecisionInputBundle、MomentumResult、四份
AnalystReport 與重大事件 ResearchResult），彼此看不到對方；每位都必須對交易池
每一檔表態。強度、論點與引用由 LLM 判斷，覆蓋、引用歸屬、ID 唯一與隔離由程式驗證。
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Set

from .analysts import ANALYSTS, AnalystReportValidator
from .contracts import (
    DecisionContext,
    DecisionToolError,
    artifact_content_sha256,
    canonical_sha256,
    reject_unknown_fields,
    required_string,
    string_list,
)
from .trade_intent import _validate_forbidden_keys


STANCE_SCHEMA_VERSION = "2.0"
STANCE_ROLES = ("bull", "bear")
STRENGTHS = ("strong", "moderate", "weak", "none")


def _symbols(bundle: Mapping[str, object]) -> List[str]:
    return sorted(str(row.get("symbol", "")).upper() for row in bundle["snapshot"].get("latest_prices", []))


def shared_input_sha256(
    bundle: Mapping[str, object],
    momentum: Mapping[str, object],
    analyst_reports: Mapping[str, Mapping[str, object]],
    research_result: Mapping[str, object],
) -> str:
    """多空兩方共同輸入的雜湊；兩方 packet 必須引用同一值。"""
    return canonical_sha256(
        {
            "bundle_hash": bundle["bundle_sha256"],
            "momentum_result_id": momentum.get("result_id"),
            "analyst_reports": {name: analyst_reports[name].get("content_sha256") for name in ANALYSTS},
            "research_result": canonical_sha256(research_result),
        }
    )


def stance_role_input_sha256(shared_sha256: str, role: str) -> str:
    if role not in STANCE_ROLES:
        raise DecisionToolError("role 必須是 bull 或 bear")
    return canonical_sha256({"shared_input_sha256": shared_sha256, "role": role, "forbidden_peer_role": "bear" if role == "bull" else "bull"})


def stance_envelope(
    bundle: Mapping[str, object], momentum: Mapping[str, object], shared_sha256: str, role: str
) -> Dict[str, object]:
    role_sha = stance_role_input_sha256(shared_sha256, role)
    return {
        "schema_version": STANCE_SCHEMA_VERSION,
        "packet_id": "%s-stance:%s" % (role, role_sha[:16]),
        "role": role,
        "bundle_id": bundle["bundle_id"],
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "bundle_hash": bundle["bundle_sha256"],
        "momentum_result_id": momentum.get("result_id"),
        "shared_input_sha256": shared_sha256,
        "dependencies": {"role_input_sha256": role_sha, "peer_packet_ids": []},
    }


def seal_stance_packet(envelope: Mapping[str, object], items: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    packet = {**dict(envelope), "status": "completed", "items": [dict(item) for item in items], "errors": []}
    packet["content_sha256"] = artifact_content_sha256(packet)
    return packet


def build_stance_brief(
    bundle: Mapping[str, object],
    momentum: Mapping[str, object],
    analyst_reports: Mapping[str, Mapping[str, object]],
    research_result: Mapping[str, object],
    role: str,
) -> Dict[str, object]:
    """重排共同輸入供研究員閱讀；兩方內容相同，只有 role 與 envelope 不同。"""
    shared = shared_input_sha256(bundle, momentum, analyst_reports, research_result)
    held = {str(item["symbol"]).upper(): item.get("shares") for item in bundle["account_snapshot"].get("positions", [])}
    momentum_status = {str(item.get("symbol", "")).upper(): item.get("status") for item in momentum.get("items", [])}
    tradability = {
        str(item.get("symbol", "")).upper(): item.get("state")
        for item in (bundle.get("tradability_assessment") or {}).get("symbols", [])
    }
    by_analyst = {
        name: {str(item["symbol"]).upper(): item for item in analyst_reports[name].get("items", [])}
        for name in ANALYSTS
    }
    research: Dict[str, List[Dict[str, object]]] = {}
    for item in research_result.get("items", []):
        research.setdefault(str(item.get("symbol", "")).upper(), []).append(
            {
                "event_id": item.get("event_id"),
                "direction": item.get("direction"),
                "research_status": item.get("research_status"),
                "prevailing_case": (item.get("debate") or {}).get("adjudication", {}).get("prevailing_case"),
                "event_summary": item.get("event_summary"),
                "impact_mechanism": item.get("impact_mechanism"),
                "evidence_ids": item.get("evidence_ids", []),
            }
        )
    symbols = []
    for symbol in _symbols(bundle):
        symbols.append(
            {
                "symbol": symbol,
                "held_shares": held.get(symbol, 0),
                "tradability_state": tradability.get(symbol),
                "momentum_status": momentum_status.get(symbol),
                "analysts": {
                    name: {
                        key: value
                        for key, value in by_analyst[name].get(symbol, {}).items()
                        if key != "symbol"
                    }
                    for name in ANALYSTS
                },
                "event_research": research.get(symbol, []),
            }
        )
    return {
        "role": role,
        "packet_envelope": stance_envelope(bundle, momentum, shared, role),
        "regime_assessment": momentum.get("regime_assessment"),
        "research_status": research_result.get("status"),
        "rules": {key: bundle["rules"].get(key) for key in ("min_positions", "max_positions", "max_stock_weight", "cash_weight_must_be_below")},
        "symbols": symbols,
    }


class StancePacketValidator:
    """驗證單一多頭或空頭 packet：隔離依賴、全交易池覆蓋、claim 與引用歸屬。"""

    ENVELOPE = {
        "schema_version", "packet_id", "role", "bundle_id", "snapshot_id", "decision_cutoff", "bundle_hash",
        "momentum_result_id", "shared_input_sha256", "dependencies", "status", "items", "errors", "content_sha256",
    }
    ITEM = {"symbol", "strength", "claims", "invalidation_conditions"}
    CLAIM = {"claim_id", "text", "evidence_ids", "finding_ids"}

    def __init__(
        self,
        bundle: Mapping[str, object],
        momentum: Mapping[str, object],
        analyst_reports: Mapping[str, Mapping[str, object]],
        research_result: Mapping[str, object],
        role: str,
        symbols: Optional[Sequence[str]] = None,
    ):
        if role not in STANCE_ROLES:
            raise DecisionToolError("role 必須是 bull 或 bear")
        if set(analyst_reports) != set(ANALYSTS):
            raise DecisionToolError("研究員需要四份分析報告：%s" % "、".join(ANALYSTS))
        for name, report in analyst_reports.items():
            errors = AnalystReportValidator(bundle, name).validate(report)
            if errors:
                raise DecisionToolError("%s 分析報告無效：%s" % (name, "；".join(errors[:5])))
        self.context = DecisionContext(bundle)
        self.role = role
        self.expected = stance_envelope(
            bundle, momentum, shared_input_sha256(bundle, momentum, analyst_reports, research_result), role
        )
        self.universe = {str(symbol).upper() for symbol in (symbols if symbols is not None else _symbols(bundle))}
        self.findings: Dict[str, str] = {
            str(finding["finding_id"]): str(item["symbol"]).upper()
            for report in analyst_reports.values()
            for item in report.get("items", [])
            for finding in item.get("findings", [])
        }

    def validate(self, packet: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        _validate_forbidden_keys(packet, "StancePacket", errors)
        reject_unknown_fields(packet, self.ENVELOPE, "StancePacket", errors)
        for field, value in self.expected.items():
            if packet.get(field) != value:
                errors.append("StancePacket.%s 與隔離輸入不一致" % field)
        if packet.get("status") != "completed":
            errors.append("只有 status=completed 的 StancePacket 可進入交易裁決")
        if packet.get("errors") != []:
            errors.append("StancePacket.errors 必須為空陣列")
        if packet.get("content_sha256") != artifact_content_sha256(packet):
            errors.append("StancePacket.content_sha256 與內容不一致")
        items = packet.get("items")
        if not isinstance(items, list):
            return errors + ["StancePacket.items 必須是陣列"]
        seen: List[str] = []
        claim_ids: Set[str] = set()
        for index, item in enumerate(items):
            prefix = "items[%d]" % index
            if not isinstance(item, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            reject_unknown_fields(item, self.ITEM, prefix, errors)
            symbol = str(item.get("symbol", "")).upper()
            seen.append(symbol)
            self._validate_item(item, symbol, prefix, claim_ids, errors)
        if len(seen) != len(set(seen)):
            errors.append("StancePacket.items 股票不得重複")
        missing = sorted(self.universe - set(seen))
        extra = sorted(set(seen) - self.universe)
        if missing:
            errors.append("StancePacket 未對全部股票表態：%s" % ", ".join(missing))
        if extra:
            errors.append("StancePacket 含本次範圍外股票：%s" % ", ".join(extra))
        return errors

    def _validate_item(
        self, item: Mapping[str, object], symbol: str, prefix: str, claim_ids: Set[str], errors: List[str]
    ) -> None:
        strength = item.get("strength")
        if strength not in STRENGTHS:
            errors.append("%s.strength 必須是 %s" % (prefix, "／".join(STRENGTHS)))
        try:
            string_list(item, "invalidation_conditions")
        except DecisionToolError as error:
            errors.append("%s.%s" % (prefix, error))
        claims = item.get("claims")
        if not isinstance(claims, list):
            errors.append("%s.claims 必須是陣列" % prefix)
            return
        if strength == "none" and claims:
            errors.append("%s strength=none 時不得有 claims" % prefix)
        if strength in {"strong", "moderate", "weak"} and not claims:
            errors.append("%s strength=%s 必須至少有一個 claim" % (prefix, strength))
        for index, claim in enumerate(claims):
            claim_prefix = "%s.claims[%d]" % (prefix, index)
            if not isinstance(claim, Mapping):
                errors.append("%s 必須是物件" % claim_prefix)
                continue
            reject_unknown_fields(claim, self.CLAIM, claim_prefix, errors)
            try:
                claim_id = required_string(claim, "claim_id")
                required_string(claim, "text")
                evidence = string_list(claim, "evidence_ids")
                finding_ids = string_list(claim, "finding_ids")
            except DecisionToolError as error:
                errors.append("%s.%s" % (claim_prefix, error))
                continue
            if not claim_id.startswith("%s-" % self.role):
                errors.append("%s.claim_id 必須以 %s- 開頭：%s" % (claim_prefix, self.role, claim_id))
            if claim_id in claim_ids:
                errors.append("%s.claim_id 重複：%s" % (claim_prefix, claim_id))
            claim_ids.add(claim_id)
            if not evidence:
                errors.append("%s.evidence_ids 不得為空" % claim_prefix)
            self.context.validate_evidence(evidence, symbol, claim_prefix, errors)
            for finding_id in finding_ids:
                owner = self.findings.get(finding_id)
                if owner is None:
                    errors.append("%s 引用不存在的 finding：%s" % (claim_prefix, finding_id))
                elif owner != symbol:
                    errors.append("%s 引用的 finding 不屬於 %s：%s" % (claim_prefix, symbol, finding_id))


def build_research_debate(
    bundle: Mapping[str, object], bull: Mapping[str, object], bear: Mapping[str, object], debate_id: str
) -> Dict[str, object]:
    debate = {
        "schema_version": STANCE_SCHEMA_VERSION,
        "debate_id": debate_id,
        "bundle_id": bundle["bundle_id"],
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "bundle_hash": bundle["bundle_sha256"],
        "shared_input_sha256": bull.get("shared_input_sha256"),
        "packets": [dict(bull), dict(bear)],
    }
    debate["content_sha256"] = artifact_content_sha256(debate)
    return debate


class ResearchDebateBundleValidator:
    """兩方 packet 各自有效、共用同一輸入、互不依賴且 claim ID 不重疊。"""

    def __init__(
        self,
        bundle: Mapping[str, object],
        momentum: Mapping[str, object],
        analyst_reports: Mapping[str, Mapping[str, object]],
        research_result: Mapping[str, object],
    ):
        self.bundle = dict(bundle)
        self.validators = {
            role: StancePacketValidator(bundle, momentum, analyst_reports, research_result, role)
            for role in STANCE_ROLES
        }
        self.shared = shared_input_sha256(bundle, momentum, analyst_reports, research_result)

    def validate(self, debate: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        reject_unknown_fields(
            debate,
            {"schema_version", "debate_id", "bundle_id", "snapshot_id", "decision_cutoff", "bundle_hash",
             "shared_input_sha256", "packets", "content_sha256"},
            "ResearchDebateBundle",
            errors,
        )
        for field in ("bundle_id", "snapshot_id", "decision_cutoff"):
            if debate.get(field) != self.bundle.get(field):
                errors.append("ResearchDebateBundle.%s 不一致" % field)
        if debate.get("bundle_hash") != self.bundle.get("bundle_sha256"):
            errors.append("ResearchDebateBundle.bundle_hash 不一致")
        if debate.get("schema_version") != STANCE_SCHEMA_VERSION:
            errors.append("ResearchDebateBundle.schema_version 必須為 %s" % STANCE_SCHEMA_VERSION)
        if debate.get("shared_input_sha256") != self.shared:
            errors.append("ResearchDebateBundle.shared_input_sha256 與共同輸入不一致")
        if debate.get("content_sha256") != artifact_content_sha256(debate):
            errors.append("ResearchDebateBundle.content_sha256 與內容不一致")
        packets = debate.get("packets")
        if not isinstance(packets, list) or [packet.get("role") for packet in packets if isinstance(packet, Mapping)] != list(STANCE_ROLES):
            return errors + ["ResearchDebateBundle.packets 必須依序剛好包含 bull 與 bear"]
        claim_sets = []
        for packet in packets:
            role = str(packet["role"])
            errors.extend("%s：%s" % (role, error) for error in self.validators[role].validate(packet))
            claim_sets.append({
                str(claim.get("claim_id"))
                for item in packet.get("items", []) if isinstance(item, Mapping)
                for claim in item.get("claims", []) if isinstance(claim, Mapping)
            })
        overlap = sorted(claim_sets[0] & claim_sets[1])
        if overlap:
            errors.append("多空 claim_id 不得重複：%s" % ", ".join(overlap))
        return errors
