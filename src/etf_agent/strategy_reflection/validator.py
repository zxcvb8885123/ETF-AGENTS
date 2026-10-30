"""反思輸出的重建驗證：欄位白名單、雜湊、樣本門檻、證據存在、旋鈕邊界與冷卻期。任一失敗整份拒絕。"""

from __future__ import annotations

from typing import Dict, List, Mapping, Sequence

from etf_agent.core import canonical_sha256

from .contracts import (
    COOLDOWN_DECISION_DAYS, KNOBS, MAX_PROPOSALS, SCHEMA_VERSION, STATUS_OBSERVE_ONLY, STATUS_PROPOSED,
    VERIFICATION_OBSERVE, ReflectionError, require_mapping,
)
from .tools import check_proposal, knob_origins
from .trial import open_trial

REFLECTION_FIELDS = {
    "schema_version", "reflection_id", "as_of", "brief_sha256", "base_policy_id", "base_policy_sha256", "decision_days",
    "status", "summary", "observations", "proposals", "citable", "content_sha256",
}
OBSERVATION_FIELDS = {"text", "evidence_ids"}
PROPOSAL_FIELDS = {"knob", "current", "proposed", "rationale", "evidence_ids"}


def reflection_sha256(reflection: Mapping[str, object]) -> str:
    return canonical_sha256({key: value for key, value in reflection.items() if key != "content_sha256"})


def _evidence(items: object, evidence: Mapping[str, str], label: str) -> None:
    if not isinstance(items, list) or not items or any(not isinstance(item, str) for item in items):
        raise ReflectionError("%s 必須引用至少一個 evidence_id" % label)
    missing = [item for item in items if item not in evidence]
    if missing:
        raise ReflectionError("%s 引用不存在的 evidence_id：%s" % (label, "、".join(missing)))


def validate_reflection(
    reflection: Mapping[str, object], brief: Mapping[str, object], template: Mapping[str, object],
    rules: Mapping[str, object], history: Sequence[Mapping[str, object]],
) -> None:
    require_mapping(reflection, "reflection")
    extra = sorted(set(reflection) - REFLECTION_FIELDS)
    if extra or REFLECTION_FIELDS - set(reflection):
        raise ReflectionError("reflection 欄位不符：多出 %s、缺少 %s" % (extra, sorted(REFLECTION_FIELDS - set(reflection))))
    if reflection["schema_version"] != SCHEMA_VERSION:
        raise ReflectionError("reflection schema_version 不符")
    if reflection["content_sha256"] != reflection_sha256(reflection):
        raise ReflectionError("reflection content_sha256 不符，內容可能被改寫")
    if reflection["brief_sha256"] != brief["brief_sha256"]:
        raise ReflectionError("reflection 與簡報雜湊不一致")
    if reflection["citable"] is not False:
        raise ReflectionError("reflection 必須標記 citable=false")
    if reflection["base_policy_sha256"] != canonical_sha256(template) or brief["base_policy_sha256"] != reflection["base_policy_sha256"]:
        raise ReflectionError("策略樣板已和反思當時不同（過期），不能套用")
    evidence: Dict[str, str] = dict(brief["evidence"])  # type: ignore[arg-type]
    observations = reflection["observations"]
    if not isinstance(observations, list):
        raise ReflectionError("observations 必須是陣列")
    for index, item in enumerate(observations):
        require_mapping(item, "observations[%d]" % index)
        if set(item) != OBSERVATION_FIELDS or not str(item["text"]).strip():
            raise ReflectionError("observations[%d] 欄位不符" % index)
        _evidence(item["evidence_ids"], evidence, "observations[%d]" % index)
    proposals = reflection["proposals"]
    if not isinstance(proposals, list):
        raise ReflectionError("proposals 必須是陣列")
    status = reflection["status"]
    if status == STATUS_OBSERVE_ONLY:
        if proposals:
            raise ReflectionError("observe_only 不得有提案")
        return
    if status != STATUS_PROPOSED:
        raise ReflectionError("status 必須是 %s 或 %s" % (STATUS_OBSERVE_ONLY, STATUS_PROPOSED))
    if not brief["gate_open"]:
        raise ReflectionError("樣本不足（決策日 %s／%s），不得提案" % (brief["decision_days"], brief["required_decision_days"]))
    if not 1 <= len(proposals) <= MAX_PROPOSALS:
        raise ReflectionError("提案數必須在 1～%d 之間" % MAX_PROPOSALS)
    if open_trial(history) is not None:
        raise ReflectionError("上一次套用的試用觀察期尚未結案，結案前不得再提案（避免還原時無法分辨是哪次變更造成的）")
    if sum(1 for item in proposals if isinstance(item, Mapping) and KNOBS.get(str(item.get("knob")), None) is not None
           and KNOBS[str(item["knob"])].verification == VERIFICATION_OBSERVE) > 1:
        raise ReflectionError("影子回放驗不了的旋鈕（停損／停利門檻）每週最多提一個")
    origins = knob_origins(history)
    seen: List[str] = []
    for index, proposal in enumerate(proposals):
        label = "proposals[%d]" % index
        require_mapping(proposal, label)
        if set(proposal) != PROPOSAL_FIELDS or not str(proposal["rationale"]).strip():
            raise ReflectionError("%s 欄位不符" % label)
        path = str(proposal["knob"])
        if path in seen:
            raise ReflectionError("%s 重複提案同一旋鈕 %s" % (label, path))
        seen.append(path)
        checked = check_proposal(path, proposal["proposed"], template, rules, origins)
        if str(proposal["current"]) != checked["current"] or str(proposal["proposed"]) != checked["proposed"]:
            raise ReflectionError("%s 的 current／proposed 與重算結果不符" % label)
        _evidence(proposal["evidence_ids"], evidence, label)
        if not any(item.startswith(("cal.h", "diag.")) for item in proposal["evidence_ids"]):
            raise ReflectionError("%s 至少要引用一則校準統計（cal.h*）或診斷統計（diag.*），只引用目前旋鈕值不算證據" % label)
        for entry in reversed(history):
            if entry.get("action") == "apply" and path in entry.get("knobs", []):
                waited = int(brief["decision_days"]) - int(entry["decision_days"])
                if waited < COOLDOWN_DECISION_DAYS:
                    raise ReflectionError("%s 旋鈕 %s 在冷卻期（再等 %d 個決策日）" % (label, path, COOLDOWN_DECISION_DAYS - waited))
                break
