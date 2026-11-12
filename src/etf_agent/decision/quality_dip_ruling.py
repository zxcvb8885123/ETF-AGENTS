"""優質股 v3 的風險 Agent 裁決（``position_sizing.agent_swap.quality_dip`` v3）。

設計見 ``docs/plans/quality_dip_agent_sizing_plan.md`` 第 3.3、4.3、4.4 節。流程：交易 Agent 在換股提名（5c）提名 ``purpose: quality_dip`` 配對並給
份量格 ``size`` → 風險 Agent 給現金姿態（簡報不變）→ **本模組的裁決步驟**（風險 Agent 對每一組 quality_dip 配對給 approve／downsize／reject）→ 配置。

本模組只含純函式與 Validator（不碰 pipeline、不呼叫 Agent），做法同 ``swap_nomination``：

- 信封與封存：``ruling_envelope``／``seal_quality_dip_ruling``／``program_ruling``。``status`` 有五種：``completed``（Agent 裁決）、
  ``not_required``（提名沒有 quality_dip 配對，不呼叫 Agent）、``skipped_by_program``（程式前置檢查不過，不呼叫 Agent）、
  ``exhausted``（驗證用盡）、``unavailable``（暫時性失敗），後四者由程式封存、``rulings`` 為空。
- 前置檢查：``expected_program_status`` 是純函式；pipeline 用它決定要不要呼叫 Agent，Finalizer 用它重算狀態並拒絕任何不一致
  （做法同 ``needs_nomination``↔``not_needed``）。
- Validator：只驗結構（涵蓋、順序、列舉、證據、綁定），不判斷理由好壞。風險 Agent 只能縮不能放：列舉裡沒有升級，``small`` 不能再降。
- 綁定：``apply_quality_dip_ruling`` 把 ``ruling_ref``／``rulings`` 綁進 Policy，配置只讀 Policy；Finalizer 以 team_inputs 2.10 的裁決整份重算比對。

裁決沒有完成（用盡、暫時性失敗、前置檢查不過）一律取消全部 quality_dip 配對、換出者不賣，**不會退回 v2「不經裁決直接成交」的行為**。
Agent 只輸出代號、列舉、證據識別碼與文字，不輸出任何數字；所有數字（權重上限、股數）由程式計算。
"""

from __future__ import annotations

import copy
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core.text import untrusted_value

from .contracts import DecisionToolError, artifact_content_sha256, reject_unknown_fields, validate_forbidden_keys
from .decline_depth import DECLINE_DEPTH_FACTS_KEY, decline_depth_brief, decline_depth_reference_tokens
from .dip_facts import DIP_FACTS_KEY, dip_reference_tokens
from .exposure_facts import EXPOSURE_FACTS_KEY, exposure_reference_tokens, risk_exposure_brief
from .input_context import DecisionContext
from .prior_decision import prior_reference_tokens
from .quality_dip_swap import (
    PURPOSE_QUALITY_DIP, RULING_APPROVE, RULING_DOWNSIZE, RULING_REJECT, RULING_STATUSES, RULINGS, SIZE_GRADES,
    program_skip_reason, quality_dip_v3_gate,
)
from .swap_nomination import nominations_of

QUALITY_DIP_RULING_VERSION = "1.0"
RULING_KEY = "quality_dip_ruling"  # team_inputs 2.10 的必要鍵
STATUS_COMPLETED = "completed"
STATUS_NOT_REQUIRED = "not_required"
STATUS_SKIPPED = "skipped_by_program"
STATUS_EXHAUSTED = "exhausted"
STATUS_UNAVAILABLE = "unavailable"
PROGRAM_STATUSES = (STATUS_NOT_REQUIRED, STATUS_SKIPPED, STATUS_EXHAUSTED, STATUS_UNAVAILABLE)
DEGRADED_STATUSES = (STATUS_EXHAUSTED, STATUS_UNAVAILABLE)
REASON_KINDS = ("market_risk", "portfolio_concentration", "timing")
ENVELOPE_FIELDS = {
    "schema_version", "ruling_id", "bundle_id", "snapshot_id", "decision_cutoff", "bundle_hash",
    "nomination_id", "nomination_sha256", "cash_stance_sha256",
    "status", "rulings", "evidence_ids", "defensive_reconciliation", "errors", "content_sha256",
}
# Agent 的輸出只有這三個鍵；其餘一律拒絕（封存時以 ``x_`` 前綴保留，讓 Validator 看見，不覆蓋信封欄位）。
AGENT_OUTPUT_FIELDS = {"rulings", "evidence_ids", "defensive_reconciliation"}
RULING_FIELDS = {"buy_symbol", "sell_symbol", "ruling", "reason_kind", "reason"}
MAX_DEGRADED_ERRORS = 10
PROGRAM_REASONS = {
    STATUS_NOT_REQUIRED: "提名沒有 purpose=quality_dip 的配對，不需要裁決，由程式直接封存，不呼叫 Agent。",
    STATUS_SKIPPED: "quality_dip 的程式前置條件不成立（回撤下限、帳本歷史或轉向門檻），不呼叫裁決，全部 quality_dip 配對由程式取消。",
    STATUS_EXHAUSTED: "裁決的確定性驗證在嘗試用盡後仍未通過，由程式封存為空清單；全部 quality_dip 配對取消、換出者不賣。",
    STATUS_UNAVAILABLE: "裁決的 Agent 呼叫在暫時性失敗重試後仍失敗，由程式封存為空清單；全部 quality_dip 配對取消、換出者不賣。",
}
# 風險 Agent 文字欄位不得出現的背景區塊鍵名（背景不是證據；facts_id 與前次 run 識別碼另由各自的 tokens 函式提供）。
BACKGROUND_KEYS = ("turn_facts", "turn_state", "market_index_facts", EXPOSURE_FACTS_KEY, DIP_FACTS_KEY, "dip_background")
RULING_NOTE = (
    "以下是交易 Agent 在換股提名提出的 purpose=quality_dip 配對（每組附份量格 size 與理由）。你只能對每一組給 approve（照交易 Agent 的份量格）、"
    "downsize（份量降一格；small 不能再降）或 reject（否決這一組）：不能升級、不能新增或更換配對、不能改順序；downsize／reject 必須附 reason_kind 與理由。"
    "你不重判個股論點的真假，也不新增個股事實，只依市場與組合風險裁決。程式另有人設的硬上限與全部後段限制，你不得輸出任何百分比或數字。"
    "背景區塊沒有 evidence_id，不可放進依據，文字欄位不得寫出區塊鍵名或識別碼。"
)


# ---------------------------------------------------------------- 信封與封存

def stance_sha256(stance: Mapping[str, object]) -> str:
    """現金姿態沒有自己的 content_sha256（team_inputs.cash_stance 是原始輸出），裁決綁定它的標準雜湊。"""
    return artifact_content_sha256(dict(stance))


def ruling_envelope(
    bundle: Mapping[str, object], nomination: Mapping[str, object], stance: Mapping[str, object], ruling_id: str,
) -> Dict[str, object]:
    return {
        "schema_version": QUALITY_DIP_RULING_VERSION,
        "ruling_id": ruling_id,
        "bundle_id": bundle["bundle_id"],
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "bundle_hash": bundle["bundle_sha256"],
        "nomination_id": nomination["nomination_id"],
        "nomination_sha256": nomination["content_sha256"],
        "cash_stance_sha256": stance_sha256(stance),
    }


def _seal(body: Dict[str, object]) -> Dict[str, object]:
    body["content_sha256"] = artifact_content_sha256(body)
    return body


def seal_quality_dip_ruling(envelope: Mapping[str, object], raw: object) -> Dict[str, object]:
    """把 Agent 的原始輸出補上信封並封存為 ``completed``。全函式：任何形狀不符的內容原樣保留，交給 Validator 回報。

    Codex strict schema 要求欄位一律輸出，所以「不適用」以固定值表示並在封存時還原成缺席：``approve`` 的 ``reason_kind`` 為 ``none``、
    ``defensive_reconciliation`` 為空字串。其他任何值（含 downsize／reject 填 none）原樣保留，由 Validator 判斷。"""
    output = raw if isinstance(raw, Mapping) else {}
    body: Dict[str, object] = {
        **dict(envelope),
        "status": STATUS_COMPLETED,
        "rulings": copy.deepcopy(output.get("rulings")),
        "evidence_ids": copy.deepcopy(output.get("evidence_ids")),
        "errors": [],
    }
    if isinstance(body["rulings"], list):
        for entry in body["rulings"]:
            if isinstance(entry, dict) and entry.get("ruling") == RULING_APPROVE and entry.get("reason_kind") == "none":
                del entry["reason_kind"]
    reconciliation = output.get("defensive_reconciliation")
    if reconciliation not in (None, ""):
        body["defensive_reconciliation"] = copy.deepcopy(reconciliation)
    for key in sorted(str(key) for key in output if str(key) not in AGENT_OUTPUT_FIELDS):
        body["x_%s" % key] = copy.deepcopy(output[key])
    if not isinstance(raw, Mapping):
        body["x_output_not_object"] = True
    return _seal(body)


def program_ruling(envelope: Mapping[str, object], status: str, errors: Sequence[str] = ()) -> Dict[str, object]:
    """程式封存的裁決：``not_required``、``skipped_by_program``、``exhausted``、``unavailable``；清單恆為空。

    ``skipped_by_program`` 的 ``errors`` 必須恰為一筆前置條件代碼（Finalizer 重算比對）；``not_required`` 為空；後兩者為 1～10 筆非空字串。"""
    if status not in PROGRAM_STATUSES:
        raise DecisionToolError("程式只能封存 %s" % "／".join(PROGRAM_STATUSES))
    body: Dict[str, object] = {
        **dict(envelope),
        "status": status,
        "rulings": [],
        "evidence_ids": [],
        "errors": [str(error) for error in errors][:MAX_DEGRADED_ERRORS],
    }
    return _seal(body)


# ---------------------------------------------------------------- 前置檢查與配對

def quality_pairs(nomination: Optional[Mapping[str, object]]) -> List[Dict[str, object]]:
    """提名中的 purpose=quality_dip 配對（依 Agent 先後；只有 completed 的提名才有配對）。"""
    return [dict(item) for item in nominations_of(nomination) if item.get("purpose") == PURPOSE_QUALITY_DIP]


def expected_program_status(policy: Mapping[str, object], nomination: Optional[Mapping[str, object]]) -> Tuple[Optional[str], Optional[str]]:
    """程式前置檢查：回傳 (狀態, 原因)。狀態為 None 表示需要呼叫 Agent。

    - 提名沒有 quality_dip 配對（含提名為 exhausted／unavailable／not_needed）：``not_required``。
    - 回撤下限為 defensive 或帳本歷史不可用、turn_assessment 低於 ``min_turn_assessment``：``skipped_by_program``（原因為代碼）。
    需已綁定本次的 drawdown_control 與 turn_assessment（``program_skip_reason`` 缺少時拋錯，fail-closed）。"""
    if not quality_pairs(nomination):
        return STATUS_NOT_REQUIRED, None
    reason = program_skip_reason(policy)
    return (STATUS_SKIPPED, reason) if reason is not None else (None, None)


# ---------------------------------------------------------------- 簡報

def _claim_texts(debate: Mapping[str, object]) -> Dict[str, str]:
    texts: Dict[str, str] = {}
    for packet in debate.get("packets", []):  # type: ignore[union-attr]
        for item in packet.get("items", []):
            for claim in item.get("claims", []):
                texts[str(claim["claim_id"])] = str(claim.get("text", ""))
    return texts


def _basis(identifiers: object, texts: Mapping[str, str]) -> List[Dict[str, object]]:
    return [
        {"id": str(identifier), "text": texts.get(str(identifier)) or "（持股檢視事實：%s）" % str(identifier).rsplit(":", 1)[-1]}
        for identifier in (identifiers if isinstance(identifiers, list) else [])
    ]


def quality_dip_ruling_brief(
    bundle: Mapping[str, object],
    trade_decision: Mapping[str, object],
    debate: Mapping[str, object],
    exposure_review: Mapping[str, object],
    nomination: Mapping[str, object],
    stance: Mapping[str, object],
    exposure_facts: Mapping[str, object],
    dip_facts: Optional[Mapping[str, object]],
    turn: Mapping[str, object],
    index: Mapping[str, object],
    turn_state: Optional[Mapping[str, object]] = None,
    agent_led: bool = False,
    decline_depth_facts: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """給風險 Agent 的裁決簡報（全部由程式算）。只有 quality_dip 配對與其背景：不附其他股票的意圖與信心統計、thesis 與 exposure 配對、任何上限百分比。

    ``turn``／``index`` 是 ``turn_facts(bundle)``／``market_index_facts(bundle)`` 的結果（背景、不可引用）。"""
    texts = _claim_texts(debate)
    items = {str(item["symbol"]).upper(): item for item in trade_decision.get("items", []) if isinstance(item, Mapping)}
    reviews = {str(item["symbol"]).upper(): item for item in exposure_review.get("items", []) if isinstance(item, Mapping)}
    dip_rows = {
        str(row["symbol"]).upper(): row for row in (dip_facts or {}).get("items", []) if isinstance(row, Mapping)  # type: ignore[union-attr]
    }
    holdings = {str(row["symbol"]).upper(): row for row in exposure_facts.get("holdings", [])}  # type: ignore[union-attr]
    non_held = {str(row["symbol"]).upper(): row for row in exposure_facts.get("non_held", [])}  # type: ignore[union-attr]
    pairs: List[Dict[str, object]] = []
    for pair in quality_pairs(nomination):
        buy, sell = str(pair["buy_symbol"]).upper(), str(pair["sell_symbol"]).upper()
        buy_row, sell_row = non_held.get(buy, {}), holdings.get(sell, {})
        pairs.append({
            "buy_symbol": buy, "sell_symbol": sell, "size": pair.get("size"), "trader_reason": pair.get("reason"),
            "buy_basis": _basis(pair.get("buy_basis_ids"), texts), "sell_basis": _basis(pair.get("sell_basis_ids"), texts),
            "buy": {
                "conviction": items.get(buy, {}).get("conviction"), "trader_rationale": items.get(buy, {}).get("rationale"),
                "dip": {
                    "citable": False, "candidate": dip_rows.get(buy, {}).get("candidate"),
                    "price_pattern": dip_rows.get(buy, {}).get("price_pattern"), "dip_assessment": items.get(buy, {}).get("dip_assessment"),
                },
                "beta": {key: buy_row.get(key) for key in ("beta", "up_capture", "down_capture", "sector")},
            },
            "sell": {
                "hold_conviction": items.get(sell, {}).get("conviction"), "trader_rationale": items.get(sell, {}).get("rationale"),
                "weight": reviews.get(sell, {}).get("weight"), "sector": sell_row.get("sector"),
                "beta": {key: reviews.get(sell, {}).get(key) for key in ("beta", "up_capture", "down_capture")},
            },
        })
    context = DecisionContext(bundle)
    price_evidence = sorted(
        {str(row["source_evidence_id"]) for row in bundle["snapshot"]["latest_prices"] if row.get("source_evidence_id")}  # type: ignore[index]
    )
    brief: Dict[str, object] = {
        "note": RULING_NOTE,
        "pairs": pairs,
        "cash_stance": {key: stance[key] for key in ("level", "cash_grade", "breadth", "turn_assessment", "exposure_view") if key in stance},
        "turn_facts": {"citable": False, **dict(turn)},
        "market_index_facts": {"citable": False, **dict(index)},
        EXPOSURE_FACTS_KEY: risk_exposure_brief(exposure_facts, agent_led=agent_led),
        "market_level_evidence_ids": sorted(context.market_evidence_ids),
        "citable_evidence_ids": sorted(set(price_evidence) | context.market_evidence_ids),
    }
    if turn_state is not None:
        brief["turn_state"] = {"citable": False, **dict(turn_state)}
    if decline_depth_facts is not None:
        # v4：連跌深度事實（背景、不可引用）；不附梯度的上限與剩餘額度（裁決簡報不附任何上限數字）。
        brief[DECLINE_DEPTH_FACTS_KEY] = decline_depth_brief(decline_depth_facts)
    return brief


# ---------------------------------------------------------------- Validator

def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _numbers(value: object, path: str, found: List[str]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        found.append(path)
    elif isinstance(value, Mapping):
        for key, child in value.items():
            _numbers(child, "%s.%s" % (path, key), found)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _numbers(child, "%s[%d]" % (path, index), found)


class QualityDipRulingValidator:
    """裁決必須綁定同一份提名與現金姿態，且依 ``status`` 分別驗證；任何不一致一律拒絕。"""

    def __init__(
        self,
        bundle: Mapping[str, object],
        policy: Mapping[str, object],
        nomination: Mapping[str, object],
        stance: Mapping[str, object],
        prior_context: Optional[Mapping[str, object]] = None,
        exposure_facts: Optional[Mapping[str, object]] = None,
        dip_facts: Optional[Mapping[str, object]] = None,
        decline_depth_facts: Optional[Mapping[str, object]] = None,
    ):
        self.bundle = dict(bundle)
        self.policy = policy
        self.nomination = nomination
        self.stance = stance
        self.context = DecisionContext(bundle)
        tokens = list(BACKGROUND_KEYS) + prior_reference_tokens(prior_context) + exposure_reference_tokens(exposure_facts) + dip_reference_tokens(dip_facts)
        if decline_depth_facts is not None:
            tokens += decline_depth_reference_tokens(decline_depth_facts)
        self.tokens = sorted(set(tokens))

    def validate(self, ruling: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        if not isinstance(ruling, Mapping):
            return ["QualityDipRuling 必須是物件"]
        validate_forbidden_keys(ruling, "QualityDipRuling", errors)
        reject_unknown_fields(ruling, ENVELOPE_FIELDS, "QualityDipRuling", errors)
        if quality_dip_v3_gate(self.policy.get("position_sizing")) is None:
            return errors + ["QualityDipRuling 只用於 position_sizing.agent_swap.quality_dip version=v3"]
        try:
            expected = ruling_envelope(self.bundle, self.nomination, self.stance, str(ruling.get("ruling_id")))
        except KeyError as error:
            return errors + ["QualityDipRuling 無法建立信封：缺少 %s" % error]
        for field, value in expected.items():
            if ruling.get(field) != value:
                errors.append("QualityDipRuling.%s 與提名或現金姿態不一致" % field)
        if not _text(ruling.get("ruling_id")):
            errors.append("QualityDipRuling.ruling_id 必須是非空字串")
        if ruling.get("content_sha256") != artifact_content_sha256(ruling):
            errors.append("QualityDipRuling.content_sha256 與內容不一致")
        status = ruling.get("status")
        if status not in RULING_STATUSES:
            return errors + ["QualityDipRuling.status 必須是 %s" % "／".join(RULING_STATUSES)]
        try:
            program_status, reason = expected_program_status(self.policy, self.nomination)
        except DecisionToolError as error:
            return errors + ["QualityDipRuling 無法重算前置檢查：%s" % error]
        if status == STATUS_NOT_REQUIRED and program_status != STATUS_NOT_REQUIRED:
            errors.append("提名有 quality_dip 配對，不得封存 not_required")
        if status == STATUS_SKIPPED and program_status != STATUS_SKIPPED:
            errors.append("程式前置條件成立（或沒有 quality_dip 配對），不得封存 skipped_by_program")
        if program_status is not None and status != program_status:
            errors.append("本次必須由程式封存 %s（%s），不得有 %s" % (program_status, reason or "沒有 quality_dip 配對", status))
        if status in PROGRAM_STATUSES:
            self._validate_program_shape(ruling, str(status), reason, errors)
            return errors
        self._validate_completed(ruling, errors)
        return errors

    def _validate_program_shape(self, ruling: Mapping[str, object], status: str, reason: Optional[str], errors: List[str]) -> None:
        if ruling.get("rulings") != [] or ruling.get("evidence_ids") != []:
            errors.append("status=%s 的 rulings 與 evidence_ids 必須是空陣列" % status)
        if "defensive_reconciliation" in ruling:
            errors.append("status=%s 不得有 defensive_reconciliation" % status)
        problems = ruling.get("errors")
        if status == STATUS_NOT_REQUIRED:
            if problems != []:
                errors.append("status=not_required 的 errors 必須為空陣列")
        elif status == STATUS_SKIPPED:
            if problems != [reason]:
                errors.append("status=skipped_by_program 的 errors 必須恰為前置條件代碼 %s" % reason)
        elif (not isinstance(problems, list) or not problems or len(problems) > MAX_DEGRADED_ERRORS
              or not all(_text(item) for item in problems)):
            errors.append("status=%s 的 errors 必須是 1～%d 筆非空字串" % (status, MAX_DEGRADED_ERRORS))

    def _validate_completed(self, ruling: Mapping[str, object], errors: List[str]) -> None:
        if ruling.get("errors") != []:
            errors.append("QualityDipRuling.errors 在 status=completed 必須為空陣列")
        if self.context.evidence_policy is None:
            errors.append("QualityDipRuling 只能用於 evidence_policy=typed_evidence_v1 的決策輸入")
        numbers: List[str] = []
        _numbers({key: ruling.get(key) for key in AGENT_OUTPUT_FIELDS}, "QualityDipRuling", numbers)
        if numbers:
            errors.append("QualityDipRuling 不得輸出任何數字（只給列舉與文字）：%s" % ", ".join(numbers[:5]))
        pairs = quality_pairs(self.nomination)
        rulings = ruling.get("rulings")
        texts: List[Tuple[str, object]] = []
        if not isinstance(rulings, list):
            errors.append("QualityDipRuling.rulings 必須是陣列")
            rulings = []
        if len(rulings) != len(pairs):
            errors.append("rulings 必須剛好涵蓋提名中的每一組 quality_dip 配對各一次（提名 %d 組、裁決 %d 組）" % (len(pairs), len(rulings)))
        kept = 0
        for index, entry in enumerate(rulings):
            prefix = "rulings[%d]" % index
            if not isinstance(entry, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            reject_unknown_fields(entry, RULING_FIELDS, prefix, errors)
            pair = pairs[index] if index < len(pairs) else None
            if pair is not None and (
                    entry.get("buy_symbol") != pair["buy_symbol"] or entry.get("sell_symbol") != pair["sell_symbol"]):
                errors.append("%s 的代號與提名第 %d 組 quality_dip 配對不符（須同序：買 %s／賣 %s）" % (
                    prefix, index + 1, pair["buy_symbol"], pair["sell_symbol"]))
            verdict = entry.get("ruling")
            if verdict not in RULINGS:
                errors.append("%s.ruling 必須是 %s" % (prefix, "／".join(RULINGS)))
            else:
                kept += int(verdict != RULING_REJECT)
                if verdict == RULING_DOWNSIZE and pair is not None and pair.get("size") == SIZE_GRADES[0]:
                    errors.append("%s：份量格 small 不能再降一格，只能 approve 或 reject" % prefix)
                needs_reason_kind = verdict in (RULING_DOWNSIZE, RULING_REJECT)
                if needs_reason_kind and entry.get("reason_kind") not in REASON_KINDS:
                    errors.append("%s.reason_kind 在 %s 必須是 %s" % (prefix, verdict, "／".join(REASON_KINDS)))
                if not needs_reason_kind and "reason_kind" in entry:
                    errors.append("%s.reason_kind 只用於 downsize／reject" % prefix)
            if not _text(entry.get("reason")):
                errors.append("%s.reason 必須是非空字串" % prefix)
            else:
                texts.append((prefix + ".reason", entry["reason"]))
        self._validate_evidence(ruling.get("evidence_ids"), errors)
        self._validate_reconciliation(ruling, kept, texts, errors)
        for label, value in texts:
            cited = sorted(token for token in self.tokens if isinstance(value, str) and token in value)
            if cited:
                errors.append("%s 不得引用背景區塊或前次決策識別碼（%s）；背景不是證據" % (label, ", ".join(cited)))

    def _validate_evidence(self, value: object, errors: List[str]) -> None:
        if not isinstance(value, list) or not value or not all(_text(item) for item in value):
            errors.append("QualityDipRuling.evidence_ids 必須是非空字串陣列")
            return
        unknown = [item for item in value if item not in self.context.evidence_symbols]
        if unknown:
            errors.append("QualityDipRuling.evidence_ids 引用不存在：%s" % ", ".join(untrusted_value(item) for item in unknown))
        if not set(value) & self.context.market_evidence_ids:
            errors.append("QualityDipRuling.evidence_ids 至少要引用一個市場層級證據（market_level_evidence_ids）；個股行情只能作補充")

    def _validate_reconciliation(self, ruling: Mapping[str, object], kept: int, texts: List[Tuple[str, object]], errors: List[str]) -> None:
        """本次現金姿態為 defensive 或 exposure_view 為 keep_defensive、而且有任何一組不是 reject 時，必須說明為什麼在防守看法下仍放行；
        其他情況（含全部 reject）不得填。只驗有沒有填，不判斷理由好壞，也不依防守看法自動取消任何一組。"""
        defensive = self.stance.get("level") == "defensive" or self.stance.get("exposure_view") == "keep_defensive"
        required = defensive and kept > 0
        present = "defensive_reconciliation" in ruling
        if required and not _text(ruling.get("defensive_reconciliation")):
            errors.append("現金姿態為 defensive 或 exposure_view 為 keep_defensive 時，放行任何一組都必須填 defensive_reconciliation")
        elif present and not required:
            errors.append("defensive_reconciliation 只在防守看法下仍放行至少一組時才能填")
        elif present:
            texts.append(("defensive_reconciliation", ruling["defensive_reconciliation"]))


# ---------------------------------------------------------------- 綁定與 team_inputs 2.10

def apply_quality_dip_ruling(policy: Mapping[str, object], ruling: Mapping[str, object]) -> Dict[str, object]:
    """把裁決綁進 Policy 的 ``agent_swap.quality_dip``（只在 v3；做法同 ``apply_swap_nomination``）：樣板不得預填，先清掉再綁；
    沒有 v3 閘門的 Policy 原樣回傳（拷貝）。``rulings`` 與提名中 quality_dip 配對同序，只有 completed 才有值。呼叫端須重算 content_sha256。"""
    if quality_dip_v3_gate(policy.get("position_sizing")) is None:
        return dict(policy)
    result = copy.deepcopy(dict(policy))
    gate = result["position_sizing"]["agent_swap"]["quality_dip"]
    gate.pop("ruling_ref", None)
    gate.pop("rulings", None)
    gate["ruling_ref"] = {"ruling_id": str(ruling["ruling_id"]), "sha256": str(ruling["content_sha256"]), "status": str(ruling["status"])}
    gate["rulings"] = [str(entry["ruling"]) for entry in ruling.get("rulings") or []] if ruling.get("status") == STATUS_COMPLETED else []
    result.pop("content_sha256", None)
    return result


def validate_team_quality_dip_ruling(
    bundle: Mapping[str, object], policy: Mapping[str, object], team: Mapping[str, object], errors: List[str],
) -> None:
    """Finalizer 對 team_inputs 2.10 的 ``quality_dip_ruling`` 整份重建驗證（信封、雜湊、前置檢查重算、結構檢查）並比對 Policy 綁定。

    讀同一份 team_inputs 內的換股提名、現金姿態、前次決策背景、曝險事實與 dip_facts；缺任何一項即拒絕。"""
    ruling = team.get(RULING_KEY)
    if not isinstance(ruling, Mapping):
        errors.append("team_inputs.quality_dip_ruling 必須是物件")
        return
    nomination, stance = team.get("swap_nomination"), team.get("cash_stance")
    if not isinstance(nomination, Mapping) or not isinstance(stance, Mapping):
        errors.append("team_inputs.quality_dip_ruling 需要同一份換股提名與現金姿態才能重建")
        return
    prior, facts, dip = team.get("prior_decision_context"), team.get(EXPOSURE_FACTS_KEY), team.get(DIP_FACTS_KEY)
    try:
        problems = QualityDipRulingValidator(
            bundle, policy, nomination, stance, prior_context=prior if isinstance(prior, Mapping) else None,
            exposure_facts=facts if isinstance(facts, Mapping) else None, dip_facts=dip if isinstance(dip, Mapping) else None,
            decline_depth_facts=team.get(DECLINE_DEPTH_FACTS_KEY) if isinstance(team.get(DECLINE_DEPTH_FACTS_KEY), Mapping) else None,
        ).validate(ruling)
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        problems = ["無法重建驗證：%s" % error]
    errors.extend("team_inputs.quality_dip_ruling：%s" % problem for problem in problems)
    gate = quality_dip_v3_gate(policy.get("position_sizing"))
    if gate is None:
        return
    try:
        expected = {
            "ruling_ref": {"ruling_id": str(ruling["ruling_id"]), "sha256": str(ruling["content_sha256"]), "status": str(ruling["status"])},
            "rulings": [str(entry["ruling"]) for entry in ruling.get("rulings") or []] if ruling.get("status") == STATUS_COMPLETED else [],
        }
    except (KeyError, TypeError, ValueError) as error:
        errors.append("team_inputs.quality_dip_ruling 無法比對 Policy 綁定：%s" % error)
        return
    for field, value in expected.items():
        if gate.get(field) != value:
            errors.append("DecisionPolicy.position_sizing.agent_swap.quality_dip.%s 與 team_inputs.quality_dip_ruling 不一致" % field)


__all__ = [
    "AGENT_OUTPUT_FIELDS", "DEGRADED_STATUSES", "PROGRAM_STATUSES", "QUALITY_DIP_RULING_VERSION", "QualityDipRulingValidator", "REASON_KINDS",
    "RULING_KEY", "STATUS_COMPLETED", "STATUS_EXHAUSTED", "STATUS_NOT_REQUIRED", "STATUS_SKIPPED", "STATUS_UNAVAILABLE",
    "apply_quality_dip_ruling", "expected_program_status", "program_ruling", "quality_dip_ruling_brief", "quality_pairs",
    "ruling_envelope", "seal_quality_dip_ruling", "stance_sha256", "validate_team_quality_dip_ruling",
]
