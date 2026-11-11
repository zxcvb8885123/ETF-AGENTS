"""換股提名（階段 3 的 5c）：交易 Agent 在入選排序之後、風險 Agent 之前，提名「用哪檔新股換掉哪檔持股」。

設計見 ``docs/agent_nominated_rotation_plan.md``。本模組只含純函式與 Validator（不碰 pipeline、不呼叫 Agent）：

- 信封與封存：``nomination_envelope``／``seal_swap_nomination``／``program_nomination``，做法同 ``selection_ranking``；
  ``status`` 有四種：``completed``（Agent 提名，含空清單）、``not_needed``（程式判定不需要，不呼叫 Agent）、
  ``exhausted``（驗證用盡）、``unavailable``（暫時性失敗），後兩者由程式封存、``nominations`` 為空。
- 事實：``nomination_facts`` 以同一份 bundle、Policy、交易決策、排序、持股檢視與曝險檢視確定性算出可提名的新股與持股、
  可引用的依據、對數上限與必須回應的論點提示；簡報、``not_needed`` 判定與 Validator 共用它，Finalizer 才能整份重算。
- 靜態檢查（2.6 A 段）：結構、新股與持股資格、依據引用、論點提示回應、保守現金與換手下界。動態可行性（名額、單股權重、
  產業、Active Share 等）留到配置，不在這裡檢查。
- 論點提示綁定：``bind_thesis_hints``／``validate_thesis_hint_binding``，做法同 ``thesis_verdicts.bind_forced_swap``。

Agent 只輸出代號、依據識別碼、列舉與文字，不輸出任何數字；所有數字（對數上限、成本與現金估計）都由程式計算。
v1（影子）與 v2（正式）的簡報完全相同，函式不讀 ``agent_swap.version``，Agent 不會被告知目前是影子期。

``position_sizing.agent_swap.exposure``（Agent 主導曝險階段 2、3，版本閘門，見 ``exposure_swap``）啟用時：每對另有列舉 ``purpose``
（``thesis``／``exposure``），封存另有 ``lower_conviction_review``；``purpose: exposure`` 的換出依據是曝險事實編號
``review:<代號>:exposure``（取自 ``team_inputs.exposure_facts``，不要求第一輪已採納）。沒有該閘門時全部行為與舊版逐位元相同。
"""

from __future__ import annotations

import copy
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .contracts import (
    DecisionToolError,
    artifact_content_sha256,
    decimal_value,
    reject_unknown_fields,
    required_string,
    snapshot_execution_price,
    untradable_symbols,
    validate_forbidden_keys,
)
from .allocation import commission_fee, execution_price, sell_tax
from .exposure import holding_days_by_symbol
from .position_review import position_ids_by_symbol, review_ids_by_symbol
from .prior_decision import prior_reference_tokens
from .exposure_facts import EXPOSURE_FACTS_KEY, exposure_reference_tokens
from .exposure_swap import (
    HIGH_CONVICTION, LOWER_CONVICTIONS, EXPOSURE_BASIS_NOTE, EXPOSURE_SWAP_NOTE, PURPOSE_EXPOSURE, PURPOSE_THESIS, PURPOSES, exposure_basis_by_symbol,
    exposure_basis_id, exposure_swap_gate, is_exposure_basis_id, validate_lower_conviction_review,
)
from .quality_dip_swap import (
    PURPOSE_QUALITY_DIP, QUALITY_DIP_SWAP_NOTE, QUALITY_DIP_SWAP_NOTE_V3, QUALITY_DIP_SWAP_NOTE_V4, SIZE_GRADES, buy_reasons as quality_dip_buy_reasons, claim_kinds,
    nomination_purposes, quality_dip_ladder_gate, quality_dip_swap_gate, quality_dip_v3_gate, sell_reasons as quality_dip_sell_reasons, validate_quality_dip_pair,
)
from .decline_depth import DECLINE_DEPTH_FACTS_KEY, decline_depth_reference_tokens
from .entry_reserve import ENTRY_RESERVE_NOTE, THESIS_SIZE_CAP_NOTE, entry_reserve_gate
from .offense import offense_gate, thesis_size_cap_enabled
from .dip_facts import DIP_FACTS_KEY, dip_reference_tokens
from .selection_ranking import rank_by_symbol
from .sizing import (
    CONVICTION_LEVELS,
    allowed_cash_grades,
    cash_grade_grid,
    cash_ratio_gate,
    reentry_blocked_symbols,
)
from .trader import REDUCING, claims_by_symbol


SWAP_NOMINATION_VERSION = "1.0"
STATUS_COMPLETED = "completed"
STATUS_NOT_NEEDED = "not_needed"
STATUS_EXHAUSTED = "exhausted"
STATUS_UNAVAILABLE = "unavailable"
STATUSES = (STATUS_COMPLETED, STATUS_NOT_NEEDED, STATUS_EXHAUSTED, STATUS_UNAVAILABLE)
PROGRAM_STATUSES = (STATUS_NOT_NEEDED, STATUS_EXHAUSTED, STATUS_UNAVAILABLE)
ENVELOPE_FIELDS = {
    "schema_version", "nomination_id", "bundle_id", "snapshot_id", "decision_cutoff", "bundle_hash",
    "trade_decision_id", "trade_decision_sha256", "selection_ranking_id", "selection_ranking_sha256",
    "status", "nominations", "thesis_hint_responses", "no_nomination_reason", "errors", "content_sha256",
}
# Agent 的輸出只有這三個鍵；其餘一律拒絕（封存時以 ``x_`` 前綴保留下來讓 Validator 看見，不會覆蓋信封欄位）。
AGENT_OUTPUT_FIELDS = {"nominations", "thesis_hint_responses", "no_nomination_reason"}
NOMINATION_FIELDS = {"buy_symbol", "sell_symbol", "buy_basis_ids", "sell_basis_ids", "reason"}
# agent_swap.exposure 啟用時多的欄位（只在閘門啟用時才允許；沒有閘門出現即拒絕）。
EXPOSURE_ENVELOPE_FIELDS = {"lower_conviction_review"}
EXPOSURE_NOMINATION_FIELDS = {"purpose"}
EXPOSURE_AGENT_OUTPUT_FIELDS = {"lower_conviction_review"}
# agent_swap.quality_dip v3（組數與份量由 Agent 提出）多的欄位：每個 quality_dip 配對的份量格 size、頂層的 quality_dip_decline。
SIZED_ENVELOPE_FIELDS = {"quality_dip_decline"}
SIZED_NOMINATION_FIELDS = {"size"}
SIZED_AGENT_OUTPUT_FIELDS = {"quality_dip_decline"}
DECLINE_FIELDS = {"reason_kind", "reason"}
DECLINE_REASON_KINDS = ("no_suitable_victim", "prefer_thesis_pairs", "market_not_ready", "candidates_not_convincing")
HINT_RESPONSE_FIELDS = {"symbol", "response", "reason"}
HINT_RESPONSES = ("nominated", "kept")
MAX_DEGRADED_ERRORS = 10
PROGRAM_REASONS = {
    STATUS_NOT_NEEDED: "沒有需要換股的名額缺口或沒有可提名的配對，由程式直接封存，不呼叫 Agent。",
    STATUS_EXHAUSTED: "提名的確定性驗證在嘗試用盡後仍未通過，由程式封存為空清單；本次不換股。",
    STATUS_UNAVAILABLE: "提名的 Agent 呼叫在暫時性失敗重試後仍失敗，由程式封存為空清單；本次不換股。",
}
# 新股不可提名的原因代碼、持股不可提名的原因代碼（簡報與 Validator 共用）。
CANDIDATE_REASONS = ("not_tradable", "reentry_cooldown", "no_price", "conviction_below_threshold", "no_bull_basis")
VICTIM_REASONS = ("not_tradable", "holding_days_short", "no_bear_basis")
# agent_swap.exposure 啟用時，持股能否以曝險理由提名（不要求空頭依據）的原因代碼。
EXPOSURE_VICTIM_REASONS = ("not_tradable", "holding_days_short", "no_exposure_facts", "no_hold_conviction")
MONEY = Decimal("1")  # 簡報顯示用的整元進位；費稅與成交價公式與配置共用 allocation 的函式
RATE_QUANTUM = Decimal("0.000001")
BRIEF_NOTE = (
    "以下事實全部由程式計算。每一對提名都要有換入股自己已採納的多頭論點、換出股自己已採納的空頭論點或持股檢視事實；"
    "不得引用第一輪已否決的 claim，也不得純粹為了買別檔而賣。名額、權重、張數與現金一律由程式計算，background 區塊沒有 evidence_id，"
    "只能參考，不可放進依據，文字欄位不得寫出前次決策識別碼。"
)


# ---------------------------------------------------------------- 信封與封存

def nomination_envelope(
    bundle: Mapping[str, object], trade_decision: Mapping[str, object], ranking: Mapping[str, object], nomination_id: str,
) -> Dict[str, object]:
    return {
        "schema_version": SWAP_NOMINATION_VERSION,
        "nomination_id": nomination_id,
        "bundle_id": bundle["bundle_id"],
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "bundle_hash": bundle["bundle_sha256"],
        "trade_decision_id": trade_decision["decision_id"],
        "trade_decision_sha256": trade_decision["content_sha256"],
        "selection_ranking_id": ranking["ranking_id"],
        "selection_ranking_sha256": ranking["content_sha256"],
    }


def _seal(body: Dict[str, object]) -> Dict[str, object]:
    body["content_sha256"] = artifact_content_sha256(body)
    return body


def seal_swap_nomination(
    envelope: Mapping[str, object], raw: object, exposure: bool = False, sized: bool = False, reserve: bool = False,
) -> Dict[str, object]:
    """把 Agent 的原始輸出補上信封並封存為 ``completed``。

    這個函式不會因 Agent 輸出的形狀而拋錯（``build`` 必須是全函式）：任何不符的內容原樣保留，交給 Validator 回報；
    Agent 多給的頂層鍵以 ``x_`` 前綴保留（不會覆蓋信封欄位），Validator 會以「含未允許欄位」拒絕。

    ``exposure``（agent_swap.exposure 啟用）時另保留 ``lower_conviction_review``；沒有閘門時它和其他多給的鍵一樣以 ``x_`` 前綴保留並被拒絕。

    ``sized``（agent_swap.quality_dip v3）時另保留 ``quality_dip_decline``，並把 Codex strict schema 的「不適用」值還原成缺席：
    配對的 ``size`` 為空字串、``quality_dip_decline`` 的 ``reason_kind`` 為 ``not_applicable`` 且 ``reason`` 為空字串。
    其他任何值原樣保留，交給 Validator 判斷（所以「不該填而填」指的是填了非空值）。

    ``reserve``（agent_swap.entry_reserve）時每組都必須有 ``size``：空字串（strict schema 的「不適用」值）同樣還原成缺席，
    由 Validator 以「缺少份量格」退回，不會被當成合法的值。"""
    output = raw if isinstance(raw, Mapping) else {}
    agent_fields = (
        AGENT_OUTPUT_FIELDS | (EXPOSURE_AGENT_OUTPUT_FIELDS if exposure else set()) | (SIZED_AGENT_OUTPUT_FIELDS if sized else set()))
    body: Dict[str, object] = {
        **dict(envelope),
        "status": STATUS_COMPLETED,
        "nominations": copy.deepcopy(output.get("nominations")),
        "thesis_hint_responses": copy.deepcopy(output.get("thesis_hint_responses")),
        "errors": [],
    }
    if exposure:
        body["lower_conviction_review"] = copy.deepcopy(output.get("lower_conviction_review"))
    if sized or reserve:
        if isinstance(body["nominations"], list):
            for entry in body["nominations"]:
                if isinstance(entry, dict) and entry.get("size") == "":
                    del entry["size"]
    if sized:
        decline = copy.deepcopy(output.get("quality_dip_decline"))
        if not (decline is None or (isinstance(decline, Mapping) and decline.get("reason_kind") == "not_applicable"
                                    and decline.get("reason") == "")):
            body["quality_dip_decline"] = decline
    if "no_nomination_reason" in output:
        reason = output["no_nomination_reason"]
        # Codex strict schema 要求欄位一律輸出：有提名時 Agent 以空字串表示「不適用」，封存時省略該鍵（與沒有輸出等價）。
        # nominations 為空時的空字串原樣保留，由 Validator 以「必須是非空字串」退回。
        if not (reason == "" and isinstance(output.get("nominations"), list) and output["nominations"]):
            body["no_nomination_reason"] = copy.deepcopy(reason)
    for key in sorted(str(key) for key in output if str(key) not in agent_fields):
        body["x_%s" % key] = copy.deepcopy(output[key])
    if not isinstance(raw, Mapping):
        body["x_output_not_object"] = True
    return _seal(body)


def program_nomination(
    envelope: Mapping[str, object], status: str, errors: Sequence[str] = (), exposure: bool = False,
) -> Dict[str, object]:
    """程式封存的提名：``not_needed``（不呼叫 Agent）、``exhausted``、``unavailable``；清單恆為空（``exposure`` 時
    ``lower_conviction_review`` 也恆為空陣列）。"""
    if status not in PROGRAM_STATUSES:
        raise DecisionToolError("程式只能封存 %s" % "／".join(PROGRAM_STATUSES))
    body: Dict[str, object] = {
        **dict(envelope),
        "status": status,
        "nominations": [],
        "thesis_hint_responses": [],
        "no_nomination_reason": PROGRAM_REASONS[status],
        "errors": [str(error) for error in errors][:MAX_DEGRADED_ERRORS],
    }
    if exposure:
        body["lower_conviction_review"] = []
    return _seal(body)


def nominations_of(nomination: Optional[Mapping[str, object]]) -> List[Dict[str, object]]:
    """配置可使用的提名（依 Agent 的先後）：只有 ``completed`` 才有，其餘（含 None）一律空清單。"""
    if not isinstance(nomination, Mapping) or nomination.get("status") != STATUS_COMPLETED:
        return []
    return [dict(item) for item in nomination.get("nominations") or [] if isinstance(item, Mapping)]


# ---------------------------------------------------------------- 事實

def _gate(policy: Mapping[str, object]) -> Mapping[str, object]:
    sizing = policy.get("position_sizing")
    gate = sizing.get("agent_swap") if isinstance(sizing, Mapping) else None
    if not isinstance(gate, Mapping):
        raise DecisionToolError("DecisionPolicy 沒有 position_sizing.agent_swap，不應建立換股提名")
    return gate


def _held_shares(bundle: Mapping[str, object]) -> Dict[str, int]:
    return {
        str(position["symbol"]).upper(): int(position["shares"])
        for position in bundle["account_snapshot"]["positions"] if int(position.get("shares", 0)) > 0
    }


def _roles(
    debate: Mapping[str, object], position_review: Optional[Mapping[str, object]],
) -> Dict[str, Dict[str, str]]:
    """每檔可引用的識別碼 → 角色（bull／bear／review／position），組法同 ``TradeDecisionValidator``。"""
    roles = claims_by_symbol(debate)
    for symbol, flags in review_ids_by_symbol(position_review).items():
        roles.setdefault(symbol, {}).update({review: "review" for review in flags})
    for symbol, identifier in position_ids_by_symbol(position_review).items():
        roles.setdefault(symbol, {})[identifier] = "position"
    return roles


def _claim_texts(debate: Mapping[str, object]) -> Dict[str, str]:
    texts: Dict[str, str] = {}
    for packet in debate.get("packets", []):
        for item in packet.get("items", []):
            for claim in item.get("claims", []):
                texts[str(claim["claim_id"])] = str(claim.get("text", ""))
    return texts


def _decimal_text(value: Decimal) -> str:
    return str(value.quantize(RATE_QUANTUM))


def nomination_facts(
    bundle: Mapping[str, object],
    policy: Mapping[str, object],
    trade_decision: Mapping[str, object],
    ranking: Mapping[str, object],
    debate: Mapping[str, object],
    position_review: Optional[Mapping[str, object]],
    exposure_review: Optional[Mapping[str, object]],
    exposure_facts: Optional[Mapping[str, object]] = None,
    dip_facts: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """提名的全部確定性事實；簡報、``not_needed`` 判定與 Validator 共用。缺任何必要輸入拋 ``DecisionToolError``（fail-closed）。

    ``dip_facts`` 是 ``team_inputs.dip_facts``（已由 Finalizer 整份重算）；只有 ``agent_swap.quality_dip`` 啟用時才讀，此時缺少即拋錯，
    新股與持股另附 ``quality_dip_reasons``／``quality_dip_nominable``（不改變 thesis 的資格與 ``not_needed`` 判定）。沒有該閘門時完全不讀。

    ``exposure_facts`` 是 ``team_inputs.exposure_facts``（已由 Finalizer 整份重算）；只有 ``agent_swap.exposure`` 啟用時才讀，
    此時缺少即拋錯。沒有該閘門時完全不讀，回傳內容與舊版逐鍵相同。

    ``policy`` 是 5c 時的基礎 Policy：reentry_cooldown 名單、持有天數（exposure_rotation.held_since_by_symbol）與
    agent_swap 綁定都已由 pipeline 綁入；姿態、廣度與信心等級要到風險 Agent 之後才綁，這裡不讀。"""
    gate = _gate(policy)
    sizing = policy["position_sizing"]
    rotation = sizing.get("rotation")
    if not isinstance(rotation, Mapping):
        raise DecisionToolError("換股提名需要 position_sizing.rotation")
    if not isinstance(position_review, Mapping) or not isinstance(exposure_review, Mapping):
        raise DecisionToolError("換股提名需要持股檢視與曝險檢視")
    held = _held_shares(bundle)
    prices = {str(row["symbol"]).upper() for row in bundle["snapshot"]["latest_prices"]}
    untradable = untradable_symbols(bundle)
    blocked = set(reentry_blocked_symbols(sizing) or [])
    ranks = rank_by_symbol(ranking)
    roles = _roles(debate, position_review)
    days = holding_days_by_symbol(policy, bundle)
    min_new = str(rotation["min_new_conviction"])
    if min_new not in CONVICTION_LEVELS:
        raise DecisionToolError("rotation.min_new_conviction 無效")
    min_days = int(gate["min_holding_days"])
    exposure = {str(item["symbol"]).upper(): item for item in exposure_review.get("items", []) if isinstance(item, Mapping)}
    items = {str(item["symbol"]).upper(): item for item in trade_decision.get("items", []) if isinstance(item, Mapping)}

    def adopted(symbol: str, wanted: Set[str]) -> List[str]:
        item = items[symbol]
        return sorted(
            str(claim) for claim in item.get("adopted_claim_ids", [])
            if roles.get(symbol, {}).get(str(claim)) in wanted
        )

    candidates: List[Dict[str, object]] = []
    for symbol in sorted(items):
        item = items[symbol]
        if item.get("intent") != "buy" or symbol in held:
            continue
        conviction = str(item.get("conviction"))
        basis = adopted(symbol, {"bull"})
        reasons = [
            code for code, failed in (
                ("not_tradable", symbol in untradable),
                ("reentry_cooldown", symbol in blocked),
                ("no_price", symbol not in prices),
                ("conviction_below_threshold", conviction not in CONVICTION_LEVELS or CONVICTION_LEVELS.index(conviction) > CONVICTION_LEVELS.index(min_new)),
                ("no_bull_basis", not basis),
            ) if failed
        ]
        candidates.append({
            "symbol": symbol, "conviction": conviction, "rank": ranks.get(symbol), "rationale": str(item.get("rationale", "")),
            "basis_ids": basis, "reasons": reasons, "nominable": not reasons,
        })
    candidates.sort(key=lambda row: (
        CONVICTION_LEVELS.index(row["conviction"]) if row["conviction"] in CONVICTION_LEVELS else len(CONVICTION_LEVELS),
        row["rank"] if isinstance(row["rank"], int) else 10 ** 6, row["symbol"],
    ))

    exposure_gate = exposure_swap_gate(sizing)
    exposure_basis: Dict[str, Dict[str, object]] = {}
    if exposure_gate is not None:
        if not isinstance(exposure_facts, Mapping):
            raise DecisionToolError("agent_swap.exposure 啟用時換股提名需要 team_inputs.exposure_facts")
        exposure_basis = exposure_basis_by_symbol(bundle, exposure_facts)
    quality_gate = quality_dip_swap_gate(sizing)
    kinds: Dict[str, Optional[str]] = {}
    if quality_gate is not None:
        if not isinstance(dip_facts, Mapping):
            raise DecisionToolError("agent_swap.quality_dip 啟用時換股提名需要 team_inputs.dip_facts")
        kinds = claim_kinds(debate)
        for row in candidates:
            symbol = str(row["symbol"])
            quality = quality_dip_buy_reasons(
                symbol, row["conviction"], row["basis_ids"], quality_gate, dip_facts, items[symbol].get("dip_assessment"), kinds)  # type: ignore[arg-type]
            row["quality_dip_reasons"] = quality
            row["quality_dip_nominable"] = bool(row["nominable"]) and not quality

    victims: List[Dict[str, object]] = []
    for symbol in sorted(held):
        item = items.get(symbol)
        if item is None or item.get("intent") != "hold":
            continue
        basis = adopted(symbol, {"bear", "review", "position"})
        reasons = [
            code for code, failed in (
                ("not_tradable", symbol in untradable),
                ("holding_days_short", days[symbol] < min_days),
                ("no_bear_basis", not basis),
            ) if failed
        ]
        facts = exposure.get(symbol, {})
        row: Dict[str, object] = {
            "symbol": symbol, "hold_conviction": item.get("conviction"), "weight": facts.get("weight"),
            "holding_days": days[symbol], "rationale": str(item.get("rationale", "")),
            "basis_ids": basis, "reasons": reasons, "nominable": not reasons,
        }
        if exposure_gate is not None:
            # purpose=exposure：不要求空頭依據，換出依據是程式算的曝險事實編號；hold 信心必須在三級內（用來決定交代順序）。
            exposure_reasons = [
                code for code, failed in (
                    ("not_tradable", symbol in untradable),
                    ("holding_days_short", days[symbol] < min_days),
                    ("no_exposure_facts", symbol not in exposure_basis),
                    ("no_hold_conviction", item.get("conviction") not in CONVICTION_LEVELS),
                ) if failed
            ]
            row.update({
                "exposure_basis_id": exposure_basis_id(symbol), "exposure_reasons": exposure_reasons,
                "exposure_nominable": not exposure_reasons,
            })
        if quality_gate is not None:
            # purpose=quality_dip：thesis 換出資格（含空頭依據）之外，另需 hold／low 與本閘門的持有天數。
            quality = quality_dip_sell_reasons(item.get("conviction"), days[symbol], quality_gate)
            row.update({"quality_dip_reasons": quality, "quality_dip_nominable": not reasons and not quality})
        victims.append(row)

    exits = sorted(
        symbol for symbol in held
        if items.get(symbol, {}).get("intent") in {"exit", "forced_exit"} and symbol not in untradable
    )
    max_positions = int(policy["max_positions"])
    min_positions = int(policy["min_positions"])
    breadth = sizing.get("positions_by_breadth")
    narrowest = min(int(value) for value in breadth.values()) if isinstance(breadth, Mapping) and breadth else max_positions
    target = max(min(narrowest, max_positions), min_positions)
    free_slots = max(0, target - len(held))
    pair_cap = min(int(gate["max_swaps_per_run"]), max(0, int(rotation["max_swaps_per_run"]) - len(exits)))
    reserve_extra: Dict[str, object] = {}
    reserve_gate = entry_reserve_gate(sizing)
    if reserve_gate is not None:
        # agent_swap.entry_reserve：提名清單上限改為 max_nominations（可大於成交上限，尾端先取消）；成交上限另列 fill_cap。
        reserve_extra = {"fill_cap": pair_cap}
        pair_cap = int(reserve_gate["max_nominations"])  # type: ignore[arg-type]
    hint_symbols = [str(symbol) for symbol in gate.get("thesis_hint_symbols") or []]
    required_hints = sorted(
        symbol for symbol in hint_symbols if symbol in held and items.get(symbol, {}).get("intent") == "hold"
    )
    nominable_buys = [row["symbol"] for row in candidates if row["nominable"]]
    nominable_sells = [row["symbol"] for row in victims if row["nominable"]]
    exposure_extra: Dict[str, object] = {}
    if exposure_gate is not None:
        exposure_sells = [row["symbol"] for row in victims if row["exposure_nominable"]]
        exposure_extra = {
            "exposure_nominable_sells": exposure_sells,
            "lower_conviction_symbols": [
                row["symbol"] for row in victims if row["exposure_nominable"] and row["hold_conviction"] in LOWER_CONVICTIONS],
            "high_conviction_exposure_symbols": [
                row["symbol"] for row in victims if row["exposure_nominable"] and row["hold_conviction"] == HIGH_CONVICTION],
        }
    sized_extra: Dict[str, object] = {}
    v3_gate = quality_dip_v3_gate(sizing)
    if v3_gate is not None:
        # agent_swap.quality_dip v3：quality_dip 組數另計（待人決定第 7 項 B），有自己的名額；不受 rotation.max_swaps_per_run 與第一輪出場數牽動，
        # 實際成交仍受換手、現金下限、檔數與單股上限等後段限制。v1／v2 的事實逐鍵不變。
        sized_extra = {
            "quality_dip_pair_cap": int(v3_gate["max_swaps_per_run"]),  # type: ignore[arg-type]
            "quality_dip_nominable_buys": [row["symbol"] for row in candidates if row["quality_dip_nominable"]],
            "quality_dip_nominable_sells": [row["symbol"] for row in victims if row["quality_dip_nominable"]],
        }
    return {
        **exposure_extra,
        **sized_extra,
        **reserve_extra,
        "candidates": candidates,
        "victims": victims,
        "first_round_exits": exits,
        "held_positions": len(held),
        "max_positions": max_positions,
        "min_positions": min_positions,
        "positions_by_breadth": dict(breadth) if isinstance(breadth, Mapping) else None,
        "conservative_free_slots": free_slots,
        "pair_cap": pair_cap,
        "min_new_conviction": min_new,
        "min_holding_days": min_days,
        "required_hint_responses": required_hints,
        "nominable_buys": nominable_buys,
        "nominable_sells": nominable_sells,
    }


def needs_nomination(facts: Mapping[str, object]) -> bool:
    """是否需要呼叫 Agent（2.7）：可提名新股多於「保守空名額 ＋ 第一輪出場數」、至少一檔可提名持股、對數上限大於 0。

    5c 時廣度未知，空名額用最窄的廣度估計（持股 26 檔、之後選 concentrated 22 檔時實際空名額為 0），才不會漏呼叫。"""
    buys = facts["nominable_buys"]
    # agent_swap.exposure 啟用時，只能以曝險理由讓位的持股（沒有空頭依據）也算可提名持股。
    sells = facts["nominable_sells"] or facts.get("exposure_nominable_sells")
    return (
        len(buys) > int(facts["conservative_free_slots"]) + len(facts["first_round_exits"])  # type: ignore[arg-type]
        and bool(sells) and int(facts["pair_cap"]) + int(facts.get("quality_dip_pair_cap", 0)) > 0  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- 成本與現金估計（保守下界，不要求逐位等於配置）

class SwapEstimator:
    """5c 的保守現金與換手估計：第一輪賣單、每對提名的賣出淨額與新股一張含費成本。

    費率、滑價、整張口徑取自 Policy；現金緩衝取「今日可選最高現金格點」（沒有 ``cash_ratio_range`` 時取各姿態緩衝的最大值），
    換手用 Policy 的基礎 ``max_turnover_rate``（反彈放寬的有效狀態到風險 Agent 之後才綁定，這裡不採用）。"""

    def __init__(self, bundle: Mapping[str, object], policy: Mapping[str, object], trade_decision: Mapping[str, object]):
        self.policy = policy
        sizing = policy["position_sizing"]
        account = bundle["account_snapshot"]
        latest = {str(row["symbol"]).upper(): row for row in bundle["snapshot"]["latest_prices"]}
        self.prices = {symbol: snapshot_execution_price(row, symbol) for symbol, row in latest.items()}
        self.held = _held_shares(bundle)
        for symbol in self.held:
            if symbol not in self.prices:
                raise DecisionToolError("持股 %s 缺少同 Snapshot 的行情，無法估計換股成本" % symbol)
        self.settled_cash = decimal_value(account["settled_cash"], "account_snapshot.settled_cash")
        self.nav = decimal_value(account["cash"], "account_snapshot.cash") + sum(
            (Decimal(shares) * self.prices[symbol] for symbol, shares in self.held.items()), Decimal(0))
        if self.nav <= 0:
            raise DecisionToolError("依 cutoff 行情重算的 NAV 必須大於 0")
        self.lot = int(policy["lot_size"])
        self.commission_rate = decimal_value(policy["commission_rate"], "commission_rate")
        self.tax_rate = decimal_value(policy["sell_tax_rate"], "sell_tax_rate")
        self.minimum_commission = decimal_value(policy["minimum_commission"], "minimum_commission")
        self.slippage_bps = decimal_value(policy["slippage_bps"], "slippage_bps")
        self.trim_fraction = decimal_value(policy["trim_fraction"], "trim_fraction")
        self.turnover_cap = decimal_value(policy["max_turnover_rate"], "max_turnover_rate")
        self.reuse = bool(policy["reuse_sell_proceeds"])
        self.gap_reserve = Decimal(0)
        if "buy_gap_reserve" in sizing:
            self.gap_reserve = decimal_value(sizing.get("buy_gap_reserve_rate", "0.03"), "buy_gap_reserve_rate")
        self.cash_ratio = self._highest_cash_ratio(sizing)
        untradable = untradable_symbols(bundle)
        self.first_round_sells: Dict[str, int] = {}
        for item in trade_decision.get("items", []):
            symbol = str(item["symbol"]).upper()
            if symbol not in self.held or symbol in untradable:
                continue
            shares = self.held[symbol]
            if item.get("intent") in {"exit", "forced_exit"}:
                self.first_round_sells[symbol] = shares
            elif item.get("intent") == "trim":
                trimmed = int((Decimal(shares) * self.trim_fraction / Decimal(self.lot)).to_integral_value(rounding=ROUND_FLOOR)) * self.lot
                if trimmed > 0:
                    self.first_round_sells[symbol] = trimmed

    @staticmethod
    def _highest_cash_ratio(sizing: Mapping[str, object]) -> Decimal:
        gate = cash_ratio_gate(sizing)
        if gate is not None:
            grid, allowed = cash_grade_grid(gate), allowed_cash_grades(gate)
            return max(grid[level][grade] for level in allowed for grade in allowed[level])
        buffers = sizing.get("cash_buffer_by_stance")
        if not isinstance(buffers, Mapping) or not buffers:
            raise DecisionToolError("無法決定今日可選最高現金比例")
        return max(decimal_value(value, "cash_buffer_by_stance") for value in buffers.values())

    def _execution(self, symbol: str, side: str) -> Decimal:
        return execution_price(self.prices[symbol], side, self.slippage_bps)

    def _commission(self, gross: Decimal) -> Decimal:
        return commission_fee(gross, self.commission_rate, self.minimum_commission)

    def gross(self, symbol: str, side: str, shares: int) -> Decimal:
        return self._execution(symbol, side) * Decimal(shares)

    def sell_net(self, symbol: str, shares: int) -> Decimal:
        gross = self.gross(symbol, "sell", shares)
        return gross - self._commission(gross) - sell_tax(gross, "sell", self.tax_rate)

    def buy_one_lot_cost(self, symbol: str) -> Decimal:
        gross = self.gross(symbol, "buy", self.lot)
        return (gross + self._commission(gross)) * (Decimal(1) + self.gap_reserve)

    def usable_cash(self) -> Decimal:
        """起始可用現金：已交割現金扣掉最高現金格點的緩衝，不為負（現金已低於緩衝時只剩賣款可用）。"""
        return max(Decimal(0), self.settled_cash - self.nav * self.cash_ratio)

    def affordable_lots(self, symbol: str) -> int:
        cost = self.buy_one_lot_cost(symbol)
        cash = self.usable_cash()
        if self.reuse:
            cash += sum((self.sell_net(sold, shares) for sold, shares in self.first_round_sells.items()), Decimal(0))
        return int(cash // cost) if cost > 0 else 0

    def check_pairs(self, pairs: Sequence[Tuple[str, str]]) -> List[str]:
        """依清單先後累計每對「賣出淨額 − 新股一張含費成本」，並檢查換手下界；回傳錯誤（空清單為通過）。"""
        errors: List[str] = []
        cash = self.usable_cash()
        gross = Decimal(0)
        if self.reuse:
            cash += sum((self.sell_net(sold, shares) for sold, shares in self.first_round_sells.items()), Decimal(0))
        gross += sum((self.gross(sold, "sell", shares) for sold, shares in self.first_round_sells.items()), Decimal(0))
        for index, (buy, sell) in enumerate(pairs):
            if sell not in self.held or buy not in self.prices:
                continue  # 資格錯誤由其他檢查回報
            if self.reuse:
                cash += self.sell_net(sell, self.held[sell])
            cash -= self.buy_one_lot_cost(buy)
            gross += self.gross(sell, "sell", self.held[sell]) + self.gross(buy, "buy", self.lot)
            if cash < 0:
                errors.append(
                    "nominations[%d]（買 %s／賣 %s）依序累計後，以今日可選最高現金格點估計仍買不起新股一張" % (index, buy, sell))
                break
        if gross > self.nav * self.turnover_cap:
            errors.append(
                "提名連同第一輪賣單的成交額估計超過換手上限 %s" % format(self.turnover_cap, ".0%"))
        return errors


# ---------------------------------------------------------------- 簡報

def swap_nomination_brief(
    bundle: Mapping[str, object],
    policy: Mapping[str, object],
    trade_decision: Mapping[str, object],
    ranking: Mapping[str, object],
    debate: Mapping[str, object],
    position_review: Mapping[str, object],
    exposure_review: Mapping[str, object],
    verdicts: Optional[Mapping[str, object]] = None,
    exposure_facts: Optional[Mapping[str, object]] = None,
    dip_facts: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """5c 給交易 Agent 的簡報（全部由程式算）。不讀 ``agent_swap.version``：影子與正式的簡報完全相同。

    ``agent_swap.quality_dip`` 啟用時（需 ``dip_facts``）新股與持股多附 ``quality_dip_nominable``／``quality_dip_blocked_reasons``，
    新股另附候選標示背景 ``dip``（``citable=false``），頂層另有 ``quality_dip_swap`` 區塊；不讀 quality_dip 的版本，影子與正式相同。

    ``agent_swap.exposure`` 啟用時（需 ``exposure_facts``）持股多附 ``exposure_nominable``、``exposure_blocked_reasons`` 與可引用的
    ``exposure_basis``（曝險事實編號、事實與行情 evidence_id），另有頂層 ``exposure_swap`` 區塊說明 ``purpose`` 與逐檔交代的規則；
    沒有該閘門時簡報與舊版逐鍵相同。

    ``verdicts`` 是 ``thesis_verdicts.load_thesis_verdicts`` 的結果；只有其雜湊與 Policy 綁定的 ``verdicts_ref`` 一致時，
    才附上提示持股的檢視摘要（背景，``citable=false``），否則只列代號。"""
    facts = nomination_facts(bundle, policy, trade_decision, ranking, debate, position_review, exposure_review, exposure_facts, dip_facts)
    exposure_gate = exposure_swap_gate(policy.get("position_sizing"))
    quality_gate = quality_dip_swap_gate(policy.get("position_sizing"))
    dip_rows = {
        str(item["symbol"]).upper(): item for item in (dip_facts or {}).get("items", []) if isinstance(item, Mapping)  # type: ignore[union-attr]
    } if quality_gate is not None else {}
    trade_items = {str(item["symbol"]).upper(): item for item in trade_decision.get("items", []) if isinstance(item, Mapping)}
    exposure_basis = exposure_basis_by_symbol(bundle, exposure_facts) if exposure_gate is not None and exposure_facts is not None else {}
    estimator = SwapEstimator(bundle, policy, trade_decision)
    texts = _claim_texts(debate)
    reviews = {str(item["symbol"]).upper(): item for item in position_review.get("items", []) if isinstance(item, Mapping)}
    exposure = {str(item["symbol"]).upper(): item for item in exposure_review.get("items", []) if isinstance(item, Mapping)}
    roles = _roles(debate, position_review)

    def basis_row(symbol: str, identifier: str) -> Dict[str, object]:
        role = roles[symbol][identifier]
        row: Dict[str, object] = {"id": identifier, "kind": role}
        if role in {"bull", "bear"}:
            row["text"] = texts.get(identifier, "")
        elif role == "review":
            row["text"] = "持股檢視警示：%s（程式事實，不是賣出指令）" % identifier.rsplit(":", 1)[-1]
        else:
            item = reviews.get(symbol, {})
            row["facts"] = {key: item.get(key) for key in (
                "unrealized_return", "retrace_from_high_5d", "retrace_from_high_20d", "relative_to_pool_5d", "relative_to_pool_20d") if key in item}
        return row

    candidates = []
    for row in facts["candidates"]:  # type: ignore[union-attr]
        symbol = str(row["symbol"])
        entry: Dict[str, object] = {
            "symbol": symbol, "conviction": row["conviction"], "rank_in_tier": row["rank"], "trader_rationale": row["rationale"],
            "nominable": row["nominable"], "blocked_reasons": row["reasons"],
            "buy_basis": [basis_row(symbol, identifier) for identifier in row["basis_ids"]],
        }
        if row["nominable"]:
            entry["one_lot_cost"] = str(estimator.buy_one_lot_cost(symbol).quantize(MONEY, rounding=ROUND_CEILING))
        if quality_gate is not None:
            dip_row = dip_rows.get(symbol, {})
            entry["quality_dip_nominable"] = row["quality_dip_nominable"]
            entry["quality_dip_blocked_reasons"] = row["quality_dip_reasons"]
            entry["dip"] = {
                "citable": False, "candidate": dip_row.get("candidate"), "price_pattern": dip_row.get("price_pattern"),
                "dip_assessment": trade_items.get(symbol, {}).get("dip_assessment"),
            }
        candidates.append(entry)
    holdings = []
    for row in facts["victims"]:  # type: ignore[union-attr]
        symbol = str(row["symbol"])
        facts_row = exposure.get(symbol, {})
        entry_row: Dict[str, object] = {
            "symbol": symbol, "hold_conviction": row["hold_conviction"], "weight": row["weight"],
            "holding_days": row["holding_days"], "trader_rationale": row["rationale"],
            "nominable": row["nominable"], "blocked_reasons": row["reasons"],
            "sell_basis": [basis_row(symbol, identifier) for identifier in row["basis_ids"]],
            "background": {
                "citable": False, "beta": facts_row.get("beta"), "up_capture": facts_row.get("up_capture"),
                "down_capture": facts_row.get("down_capture"),
            },
        }
        if exposure_gate is not None:
            entry_row["exposure_nominable"] = row["exposure_nominable"]
            entry_row["exposure_blocked_reasons"] = row["exposure_reasons"]
            if symbol in exposure_basis:
                entry_row["exposure_basis"] = exposure_basis[symbol]
        if quality_gate is not None:
            entry_row["quality_dip_nominable"] = row["quality_dip_nominable"]
            entry_row["quality_dip_blocked_reasons"] = row["quality_dip_reasons"]
        holdings.append(entry_row)
    gate = _gate(policy)
    ref = gate.get("verdicts_ref")
    summaries: Dict[str, str] = {}
    if isinstance(verdicts, Mapping) and isinstance(ref, Mapping) and verdicts.get("sha256") == ref.get("sha256"):
        summaries = {str(item["symbol"]).upper(): str(item["summary"]) for item in verdicts.get("items", []) if item.get("verdict") == "broken"}
    hints = [
        {"symbol": symbol, "citable": False, **({"weekly_review_summary": summaries[symbol]} if symbol in summaries else {})}
        for symbol in facts["required_hint_responses"]  # type: ignore[union-attr]
    ]
    cheapest = [estimator.affordable_lots(str(row["symbol"])) for row in facts["candidates"] if row["nominable"]]  # type: ignore[union-attr]
    exposure_block: Dict[str, object] = {}
    if exposure_gate is not None:
        exposure_block = {"exposure_swap": {
            "note": EXPOSURE_SWAP_NOTE, "basis_note": EXPOSURE_BASIS_NOTE,
            "lower_conviction_symbols": facts["lower_conviction_symbols"],
            "high_conviction_symbols": facts["high_conviction_exposure_symbols"],
        }}
    if quality_gate is not None:
        exposure_block["quality_dip_swap"] = {
            "note": QUALITY_DIP_SWAP_NOTE, "oversold_enabled": isinstance(quality_gate.get("oversold"), Mapping),
        }
        if quality_dip_v3_gate(policy.get("position_sizing")) is not None:
            # v3：份量格的名稱與意思、「最多可提名幾組 quality_dip」（待人決定第 8 項 A）；不給任何權重百分比。v1／v2 的簡報逐鍵不變。
            exposure_block["quality_dip_swap"] = {
                **exposure_block["quality_dip_swap"],
                "note": QUALITY_DIP_SWAP_NOTE_V4 if quality_dip_ladder_gate(policy.get("position_sizing")) is not None else QUALITY_DIP_SWAP_NOTE_V3,
                "max_quality_dip_pairs": facts["quality_dip_pair_cap"],
                "size_grades": {
                    "small": "試單：論點成立但你想先小額進場", "medium": "中等份量", "large": "把握最高：你願意用程式允許的最大份量",
                },
            }
    if entry_reserve_gate(policy.get("position_sizing")) is not None:
        # entry_reserve：份量格對每一組都適用（不只 quality_dip）；清單長度上限與實際成交上限分開列。不給任何權重百分比。
        exposure_block["entry_reserve"] = {
            "note": ENTRY_RESERVE_NOTE + (THESIS_SIZE_CAP_NOTE if thesis_size_cap_enabled(offense_gate(policy.get("position_sizing"))) else ""),
            "max_nominations": facts["pair_cap"], "fill_limit": facts["fill_cap"],
            "size_grades": {"small": "試單：論點成立但你想先小額進場", "medium": "中等份量", "large": "把握最高：你願意用程式允許的最大份量"},
        }
    return {
        **exposure_block,
        "note": BRIEF_NOTE,
        "pair_limit": facts["pair_cap"],
        "min_new_conviction": facts["min_new_conviction"],
        "min_holding_days": facts["min_holding_days"],
        "candidates": candidates,
        "holdings": holdings,
        "thesis_hints": hints,
        "background": {
            "citable": False,
            "held_positions": facts["held_positions"],
            "max_positions": facts["max_positions"],
            "min_positions": facts["min_positions"],
            "positions_by_breadth": facts["positions_by_breadth"],
            "breadth_note": "今天的持股廣度由風險 Agent 稍後決定；上表只是各廣度對應的目標檔數。",
            "first_round_exit_count": len(facts["first_round_exits"]),  # type: ignore[arg-type]
            "candidate_count": len(facts["nominable_buys"]),  # type: ignore[arg-type]
            "cash_note": "以今日可選最高現金格點估計，最多能負擔的新股張數（只是提示，最後以配置為準）。",
            "affordable_lots_estimate": max(cheapest) if cheapest else 0,
        },
    }


# ---------------------------------------------------------------- Validator

def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _id_list(value: object, label: str, errors: List[str]) -> List[str]:
    if not isinstance(value, list) or not value or not all(_text(item) for item in value):
        errors.append("%s 必須是非空的字串清單" % label)
        return []
    if len(set(value)) != len(value):
        errors.append("%s 不得重複" % label)
    return [str(item) for item in value]


class SwapNominationValidator:
    """提名必須綁定同一份交易決策與排序，且依 ``status`` 分別驗證：

    - ``not_needed``：程式以同一規則重算必須「不需要」，形狀是固定的空清單；反過來，需要時封存 ``not_needed`` 一律拒絕。
    - ``exhausted``／``unavailable``：只在「需要」時才可能發生；形狀是固定的空清單加非空 ``errors``（Finalizer 無法證明 Agent
      真的失敗，只能核對形狀與配置沒有使用任何提名）。
    - ``completed``：Agent 的提名，逐項做 2.6 A 段靜態檢查。
    """

    def __init__(
        self,
        bundle: Mapping[str, object],
        policy: Mapping[str, object],
        trade_decision: Mapping[str, object],
        ranking: Mapping[str, object],
        debate: Mapping[str, object],
        position_review: Optional[Mapping[str, object]],
        exposure_review: Optional[Mapping[str, object]],
        prior_context: Optional[Mapping[str, object]] = None,
        exposure_facts: Optional[Mapping[str, object]] = None,
        dip_facts: Optional[Mapping[str, object]] = None,
        decline_depth_facts: Optional[Mapping[str, object]] = None,
    ):
        self.bundle = dict(bundle)
        self.policy = policy
        self.trade = dict(trade_decision)
        self.ranking = dict(ranking)
        self.debate = debate
        self.position_review = position_review
        self.exposure_review = exposure_review
        self.prior_tokens = prior_reference_tokens(prior_context)
        # agent_swap.exposure（版本閘門）：有閘門時另需 team_inputs.exposure_facts；沒有閘門時完全不讀。
        self.exposure_gate = exposure_swap_gate(policy.get("position_sizing")) if isinstance(policy, Mapping) else None
        self.exposure_facts = exposure_facts
        if self.exposure_gate is not None:
            self.prior_tokens = set(self.prior_tokens) | set(exposure_reference_tokens(exposure_facts))
        # agent_swap.quality_dip（版本閘門）：另需 team_inputs.dip_facts；候選標示背景的識別碼不得出現在理由。沒有閘門時完全不讀。
        self.quality_gate = quality_dip_swap_gate(policy.get("position_sizing")) if isinstance(policy, Mapping) else None
        self.dip_facts = dip_facts
        self.sized = quality_dip_v3_gate(policy.get("position_sizing")) is not None if isinstance(policy, Mapping) else False
        # agent_swap.entry_reserve：每組必填份量格 size；沒有閘門時完全不讀。
        self.reserve = entry_reserve_gate(policy.get("position_sizing")) is not None if isinstance(policy, Mapping) else False
        if self.quality_gate is not None:
            self.prior_tokens = set(self.prior_tokens) | set(dip_reference_tokens(dip_facts))
            self.kinds = claim_kinds(debate)
            if quality_dip_ladder_gate(policy.get("position_sizing")) is not None:
                # v4：連跌深度事實是背景（沒有 evidence_id），它的鍵名與識別碼不得出現在理由。
                self.prior_tokens = set(self.prior_tokens) | set(decline_depth_reference_tokens(decline_depth_facts))

    def validate(self, nomination: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        if not isinstance(nomination, Mapping):
            return ["SwapNomination 必須是物件"]
        validate_forbidden_keys(nomination, "SwapNomination", errors)
        reject_unknown_fields(
            nomination, ENVELOPE_FIELDS | (EXPOSURE_ENVELOPE_FIELDS if self.exposure_gate is not None else set())
            | (SIZED_ENVELOPE_FIELDS if self.sized else set()), "SwapNomination", errors)
        try:
            expected = nomination_envelope(self.bundle, self.trade, self.ranking, str(nomination.get("nomination_id")))
        except KeyError as error:
            return errors + ["SwapNomination 無法建立信封：缺少 %s" % error]
        for field, value in expected.items():
            if nomination.get(field) != value:
                errors.append("SwapNomination.%s 與交易決策或入選排序不一致" % field)
        try:
            required_string(nomination, "nomination_id")
        except DecisionToolError as error:
            errors.append("SwapNomination.%s" % error)
        if nomination.get("content_sha256") != artifact_content_sha256(nomination):
            errors.append("SwapNomination.content_sha256 與內容不一致")
        status = nomination.get("status")
        if status not in STATUSES:
            return errors + ["SwapNomination.status 必須是 %s" % "／".join(STATUSES)]
        try:
            facts = nomination_facts(
                self.bundle, self.policy, self.trade, self.ranking, self.debate, self.position_review, self.exposure_review,
                self.exposure_facts, self.dip_facts)
        except (DecisionToolError, KeyError, TypeError, ValueError) as error:
            return errors + ["SwapNomination 無法以共同輸入重算事實：%s" % error]
        needed = needs_nomination(facts)
        if status == STATUS_NOT_NEEDED and needed:
            errors.append("本次需要提名（可提名新股多於保守空名額、且有可提名持股與對數額度），不得封存 not_needed")
        if status != STATUS_NOT_NEEDED and not needed:
            errors.append("本次不需要提名，必須由程式封存 not_needed，不得有 %s" % status)
        if status in PROGRAM_STATUSES:
            self._validate_program_shape(nomination, str(status), errors)
            return errors
        self._validate_completed(nomination, facts, errors)
        return errors

    def _validate_program_shape(self, nomination: Mapping[str, object], status: str, errors: List[str]) -> None:
        if nomination.get("nominations") != [] or nomination.get("thesis_hint_responses") != []:
            errors.append("status=%s 的 nominations 與 thesis_hint_responses 必須是空陣列" % status)
        if nomination.get("no_nomination_reason") != PROGRAM_REASONS[status]:
            errors.append("status=%s 的 no_nomination_reason 必須是程式固定文字" % status)
        if self.exposure_gate is not None and nomination.get("lower_conviction_review") != []:
            errors.append("status=%s 的 lower_conviction_review 必須是空陣列" % status)
        if self.sized and "quality_dip_decline" in nomination:
            errors.append("status=%s 不得有 quality_dip_decline" % status)
        problems = nomination.get("errors")
        if status == STATUS_NOT_NEEDED:
            if problems != []:
                errors.append("status=not_needed 的 errors 必須為空陣列")
        elif (not isinstance(problems, list) or not problems or len(problems) > MAX_DEGRADED_ERRORS
              or not all(_text(item) for item in problems)):
            errors.append("status=%s 的 errors 必須是 1～%d 筆非空字串" % (status, MAX_DEGRADED_ERRORS))

    def _validate_completed(self, nomination: Mapping[str, object], facts: Mapping[str, object], errors: List[str]) -> None:
        if nomination.get("errors") != []:
            errors.append("SwapNomination.errors 在 status=completed 必須為空陣列")
        nominations = nomination.get("nominations")
        responses = nomination.get("thesis_hint_responses")
        if not isinstance(nominations, list):
            errors.append("SwapNomination.nominations 必須是陣列")
            nominations = []
        if not isinstance(responses, list):
            errors.append("SwapNomination.thesis_hint_responses 必須是陣列")
            responses = []
        if not self.sized and len(nominations) > int(facts["pair_cap"]):  # type: ignore[arg-type]
            errors.append("提名對數 %d 超過本次上限 %d" % (len(nominations), int(facts["pair_cap"])))  # type: ignore[arg-type]
        exposure = self.exposure_gate is not None
        if nominations:
            if "no_nomination_reason" in nomination:
                errors.append("有提名時不得出現 no_nomination_reason")
        elif not _text(nomination.get("no_nomination_reason")):
            errors.append("nominations 為空時 no_nomination_reason 必須是非空字串")
        texts: List[Tuple[str, object]] = []
        if "no_nomination_reason" in nomination:
            texts.append(("no_nomination_reason", nomination["no_nomination_reason"]))
        roles = _roles(self.debate, self.position_review)
        candidates = {str(row["symbol"]): row for row in facts["candidates"]}  # type: ignore[union-attr]
        victims = {str(row["symbol"]): row for row in facts["victims"]}  # type: ignore[union-attr]
        items = {str(item["symbol"]).upper(): item for item in self.trade.get("items", [])}
        pairs: List[Tuple[str, str]] = []
        purposed: List[Tuple[str, str, str]] = []
        buys: List[str] = []
        sells: List[str] = []
        for index, entry in enumerate(nominations):
            prefix = "nominations[%d]" % index
            if not isinstance(entry, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            reject_unknown_fields(
                entry, NOMINATION_FIELDS | (EXPOSURE_NOMINATION_FIELDS if exposure else set())
                | (SIZED_NOMINATION_FIELDS if self.sized or self.reserve else set()), prefix, errors)
            purpose = PURPOSE_THESIS
            if exposure:
                allowed = nomination_purposes(self.policy.get("position_sizing"))
                if entry.get("purpose") not in allowed:
                    errors.append("%s.purpose 必須是 %s" % (prefix, "／".join(allowed)))
                else:
                    purpose = str(entry["purpose"])
            buy, sell = entry.get("buy_symbol"), entry.get("sell_symbol")
            for label, symbol in (("buy_symbol", buy), ("sell_symbol", sell)):
                if not _text(symbol) or symbol != str(symbol).upper():
                    errors.append("%s.%s 必須是大寫代號字串" % (prefix, label))
            if not (_text(buy) and _text(sell)):
                continue
            buy, sell = str(buy), str(sell)
            if buy == sell:
                errors.append("%s 換入與換出不得是同一檔：%s" % (prefix, buy))
            self._check_buy(prefix, buy, candidates, items, errors)
            self._check_sell(prefix, sell, victims, items, errors, purpose == PURPOSE_EXPOSURE)
            buys.append(buy)
            sells.append(sell)
            pairs.append((buy, sell))
            purposed.append((buy, sell, purpose))
            buy_ids = entry.get("buy_basis_ids")
            if exposure and isinstance(buy_ids, list) and any(is_exposure_basis_id(item) for item in buy_ids):
                errors.append("%s.buy_basis_ids 不得放曝險事實編號（它只能作為 purpose=exposure 的換出依據）" % prefix)
            self._check_basis(prefix, "buy_basis_ids", buy_ids, buy, {"bull"}, candidates, roles, items, errors)
            self._check_sell_basis(prefix, entry.get("sell_basis_ids"), sell, purpose, victims, roles, items, errors)
            if purpose == PURPOSE_QUALITY_DIP:
                # 優質股第三階段：thesis 的全部規則之外另驗結構條件（候選標示、dip_assessment、非價格多頭論點、換出者 hold／low）。
                validate_quality_dip_pair(prefix, buy_ids, candidates.get(buy), victims.get(sell), self.kinds, errors)
            if self.reserve:
                # entry_reserve：每一組（不只 quality_dip）都必須有份量格列舉 size（缺席與空字串等價，封存時已還原成缺席，所以缺 size 會在這裡退回）。
                if entry.get("size") not in SIZE_GRADES:
                    errors.append("%s.size 必須是 %s（entry_reserve 啟用時每一組都要填份量格）" % (prefix, "／".join(SIZE_GRADES)))
            elif self.sized:
                # v3：quality_dip 配對必須有份量格列舉 size，其他 purpose 不得有（缺席與空字串等價，封存時已還原成缺席）。
                if purpose == PURPOSE_QUALITY_DIP and entry.get("size") not in SIZE_GRADES:
                    errors.append("%s.size 在 purpose=quality_dip 必須是 %s" % (prefix, "／".join(SIZE_GRADES)))
                elif purpose != PURPOSE_QUALITY_DIP and "size" in entry:
                    errors.append("%s.size 只能用於 purpose=quality_dip" % prefix)
            if not _text(entry.get("reason")):
                errors.append("%s.reason 必須是非空字串" % prefix)
            else:
                texts.append((prefix + ".reason", entry["reason"]))
        for label, symbols in (("buy_symbol", buys), ("sell_symbol", sells)):
            repeated = sorted({symbol for symbol in symbols if symbols.count(symbol) > 1})
            if repeated:
                errors.append("%s 在清單中只能出現一次：%s" % (label, ", ".join(repeated)))
        if self.sized:
            self._validate_sized_counts(nomination, facts, purposed, texts, errors)
        if exposure:
            validate_lower_conviction_review(
                nomination.get("lower_conviction_review"), purposed,
                [str(symbol) for symbol in facts.get("lower_conviction_symbols", [])],  # type: ignore[union-attr]
                [str(symbol) for symbol in facts.get("high_conviction_exposure_symbols", [])],  # type: ignore[union-attr]
                set(sells), texts, errors,
            )
        errors.extend(self._validate_hints(responses, facts, set(sells), texts))
        for label, value in texts:
            cited = sorted(token for token in self.prior_tokens if isinstance(value, str) and token in value)
            if cited:
                errors.append("%s 不得引用前次決策背景（%s）；它不是本次證據" % (label, ", ".join(cited)))
        if pairs and not errors:
            try:
                checked = pairs
                if self.reserve:
                    # 超過成交上限的尾端組在配置時本來就會取消：現金與換手的保守估計只看會成交的前段（plain 取 fill_cap、quality_dip 取自己的名額）。
                    checked, plain_used, quality_used = [], 0, 0
                    quality_cap = int(facts.get("quality_dip_pair_cap", 0))  # type: ignore[arg-type]
                    for (buy, sell), (_, _, purpose) in zip(pairs, purposed):
                        if purpose == PURPOSE_QUALITY_DIP:
                            quality_used += 1
                            if quality_used <= quality_cap:
                                checked.append((buy, sell))
                        else:
                            plain_used += 1
                            if plain_used <= int(facts["fill_cap"]):  # type: ignore[arg-type]
                                checked.append((buy, sell))
                errors.extend(SwapEstimator(self.bundle, self.policy, self.trade).check_pairs(checked))
            except (DecisionToolError, KeyError, TypeError, ValueError) as error:
                errors.append("無法估計提名的保守現金與換手：%s" % error)

    def _validate_sized_counts(
        self, nomination: Mapping[str, object], facts: Mapping[str, object], purposed: Sequence[Tuple[str, str, str]],
        texts: List[Tuple[str, object]], errors: List[str],
    ) -> None:
        """v3：quality_dip 配對以自己的名額計（``quality_dip_pair_cap``），其餘配對仍以 ``pair_cap`` 計；
        沒有 quality_dip 配對、卻有可提名的換入者與換出者時必須交代原因（``quality_dip_decline``，純紀錄、程式不依它做任何事）。"""
        quality = [item for item in purposed if item[2] == PURPOSE_QUALITY_DIP]
        plain = len(purposed) - len(quality)
        if plain > int(facts["pair_cap"]):  # type: ignore[arg-type]
            errors.append("提名對數（不含 quality_dip）%d 超過本次上限 %d" % (plain, int(facts["pair_cap"])))  # type: ignore[arg-type]
        if len(quality) > int(facts["quality_dip_pair_cap"]):  # type: ignore[arg-type]
            errors.append("quality_dip 提名對數 %d 超過本次上限 %d" % (len(quality), int(facts["quality_dip_pair_cap"])))  # type: ignore[arg-type]
        has_targets = bool(facts["quality_dip_nominable_buys"]) and bool(facts["quality_dip_nominable_sells"])
        decline = nomination.get("quality_dip_decline")
        required = has_targets and not quality and int(facts["quality_dip_pair_cap"]) > 0  # type: ignore[arg-type]
        if "quality_dip_decline" not in nomination:
            if required:
                errors.append("簡報顯示有可提名的 quality_dip 換入者與換出者、而清單沒有任何 quality_dip 配對時，必須填 quality_dip_decline")
            return
        if not required:
            errors.append(
                "quality_dip_decline 只在「有可提名的 quality_dip 換入者與換出者、而清單沒有任何 quality_dip 配對」時才能填；其他情況不得出現")
            return
        if not isinstance(decline, Mapping):
            errors.append("quality_dip_decline 必須是物件")
            return
        reject_unknown_fields(decline, DECLINE_FIELDS, "quality_dip_decline", errors)
        if decline.get("reason_kind") not in DECLINE_REASON_KINDS:
            errors.append("quality_dip_decline.reason_kind 必須是 %s" % "／".join(DECLINE_REASON_KINDS))
        if not _text(decline.get("reason")):
            errors.append("quality_dip_decline.reason 必須是非空字串")
        else:
            texts.append(("quality_dip_decline.reason", decline["reason"]))

    @staticmethod
    def _check_buy(prefix: str, symbol: str, candidates: Mapping[str, Mapping[str, object]], items: Mapping[str, Mapping[str, object]], errors: List[str]) -> None:
        row = candidates.get(symbol)
        if row is None:
            item = items.get(symbol)
            why = "不在交易池" if item is None else "第一輪不是 buy 或已持有（intent=%s）" % item.get("intent")
            errors.append("%s.buy_symbol %s 不能作為換入股：%s" % (prefix, symbol, why))
        elif not row["nominable"]:
            errors.append("%s.buy_symbol %s 不能作為換入股：%s" % (prefix, symbol, "、".join(row["reasons"])))  # type: ignore[arg-type]

    @staticmethod
    def _check_sell(
        prefix: str, symbol: str, victims: Mapping[str, Mapping[str, object]], items: Mapping[str, Mapping[str, object]],
        errors: List[str], exposure: bool = False,
    ) -> None:
        row = victims.get(symbol)
        if row is None:
            item = items.get(symbol)
            why = "不在交易池" if item is None else "未持有或第一輪不是 hold（intent=%s）" % item.get("intent")
            errors.append("%s.sell_symbol %s 不能作為換出股：%s" % (prefix, symbol, why))
        elif exposure:
            if not row["exposure_nominable"]:
                errors.append("%s.sell_symbol %s 不能作為曝險換出股：%s" % (prefix, symbol, "、".join(row["exposure_reasons"])))  # type: ignore[arg-type]
        elif not row["nominable"]:
            errors.append("%s.sell_symbol %s 不能作為換出股：%s" % (prefix, symbol, "、".join(row["reasons"])))  # type: ignore[arg-type]

    def _check_sell_basis(
        self, prefix: str, value: object, symbol: str, purpose: str, victims: Mapping[str, Mapping[str, object]],
        roles: Mapping[str, Mapping[str, str]], items: Mapping[str, Mapping[str, object]], errors: List[str],
    ) -> None:
        """換出依據。沒有 ``agent_swap.exposure`` 閘門時與舊版相同（曝險事實編號只會被當成不存在的識別碼拒絕）。

        purpose=exposure：必須含該換出股自己的曝險事實編號；其餘依據（若有）仍須是第一輪已採納的空頭論點、持股檢視警示或持股事實，
        曝險事實編號本身不要求第一輪採納。purpose=thesis：曝險事實編號一律不可用。"""
        wanted = {"bear", "review", "position"}
        if self.exposure_gate is None or not isinstance(value, list):
            self._check_basis(prefix, "sell_basis_ids", value, symbol, wanted, victims, roles, items, errors)
            return
        label = "%s.sell_basis_ids" % prefix
        exposure_ids = [item for item in value if is_exposure_basis_id(item)]
        rest = [item for item in value if not is_exposure_basis_id(item)]
        if purpose != PURPOSE_EXPOSURE:  # thesis 與 quality_dip 的換出依據規則相同
            if exposure_ids:
                errors.append("%s 不得放曝險事實編號（只能用於 purpose=exposure）：%s" % (label, ", ".join(sorted(map(str, exposure_ids)))))
            self._check_basis(prefix, "sell_basis_ids", rest, symbol, wanted, victims, roles, items, errors)
            return
        own = exposure_basis_id(symbol)
        if own not in exposure_ids:
            errors.append("%s 在 purpose=exposure 必須含 %s 自己的曝險事實編號 %s" % (label, symbol, own))
        others = sorted(str(item) for item in exposure_ids if item != own)
        if others:
            errors.append("%s 引用別檔的曝險事實編號：%s" % (label, ", ".join(others)))
        if len(set(exposure_ids)) != len(exposure_ids):
            errors.append("%s 不得重複" % label)
        if rest:
            self._check_basis(prefix, "sell_basis_ids", rest, symbol, wanted, victims, roles, items, errors)

    @staticmethod
    def _check_basis(
        prefix: str, label: str, value: object, symbol: str, wanted: Set[str], rows: Mapping[str, Mapping[str, object]],
        roles: Mapping[str, Mapping[str, str]], items: Mapping[str, Mapping[str, object]], errors: List[str],
    ) -> None:
        identifiers = _id_list(value, "%s.%s" % (prefix, label), errors)
        allowed = set(rows[symbol]["basis_ids"]) if symbol in rows else set()  # type: ignore[arg-type]
        item = items.get(symbol, {})
        rejected = set(item.get("rejected_claim_ids", []))
        own = roles.get(symbol, {})
        owned_elsewhere = {identifier for other, mapping in roles.items() if other != symbol for identifier in mapping}
        for identifier in identifiers:
            if identifier in allowed:
                continue
            if identifier in rejected:
                why = "第一輪已否決，不能作為依據"
            elif identifier in own and own[identifier] not in wanted:
                why = "角色是 %s，不是可引用的%s依據" % (own[identifier], "多頭" if "bull" in wanted else "空頭或持股")
            elif identifier in own:
                why = "第一輪沒有採納，不能作為依據"
            elif identifier in owned_elsewhere:
                why = "屬於其他股票"
            else:
                why = "不存在"
            errors.append("%s.%s 的 %s 不可引用（%s）：%s" % (prefix, label, symbol, why, identifier))

    def _validate_hints(
        self, responses: Sequence[object], facts: Mapping[str, object], sold: Set[str], texts: List[Tuple[str, object]],
    ) -> List[str]:
        errors: List[str] = []
        required = [str(symbol) for symbol in facts["required_hint_responses"]]  # type: ignore[union-attr]
        seen: List[str] = []
        for index, entry in enumerate(responses):
            prefix = "thesis_hint_responses[%d]" % index
            if not isinstance(entry, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            reject_unknown_fields(entry, HINT_RESPONSE_FIELDS, prefix, errors)
            symbol, response = entry.get("symbol"), entry.get("response")
            if not _text(symbol):
                errors.append("%s.symbol 必須是非空字串" % prefix)
                continue
            seen.append(str(symbol))
            if response not in HINT_RESPONSES:
                errors.append("%s.response 必須是 %s" % (prefix, "／".join(HINT_RESPONSES)))
            elif response == "nominated" and symbol not in sold:
                errors.append("%s 回應 nominated 的 %s 必須是某一對的 sell_symbol" % (prefix, symbol))
            elif response == "kept" and symbol in sold:
                errors.append("%s 回應 kept 的 %s 不得出現在 sell_symbol" % (prefix, symbol))
            if not _text(entry.get("reason")):
                errors.append("%s.reason 必須是非空字串" % prefix)
            else:
                texts.append((prefix + ".reason", entry["reason"]))
        repeated = sorted({symbol for symbol in seen if seen.count(symbol) > 1})
        if repeated:
            errors.append("thesis_hint_responses 每檔只能回應一次：%s" % ", ".join(repeated))
        missing = sorted(set(required) - set(seen))
        extra = sorted(set(seen) - set(required))
        if missing:
            errors.append("thesis_hint_responses 漏回應論點提示：%s" % ", ".join(missing))
        if extra:
            errors.append("thesis_hint_responses 含不需回應的股票：%s" % ", ".join(extra))
        return errors


# ---------------------------------------------------------------- 論點提示綁定

def _expected_binding(verdicts: Optional[Mapping[str, object]]) -> Optional[Tuple[List[str], Dict[str, str]]]:
    if verdicts is None:
        return None
    broken = sorted({str(item["symbol"]).upper() for item in verdicts["items"] if item["verdict"] == "broken"})  # type: ignore[union-attr]
    if not broken:
        return None
    return broken, {"verdicts_id": str(verdicts["verdicts_id"]), "as_of": str(verdicts["as_of"]), "sha256": str(verdicts["sha256"])}


def bind_thesis_hints(policy: Mapping[str, object], verdicts: Optional[Mapping[str, object]]) -> Dict[str, object]:
    """把「論點被推翻」名單綁進 Policy 的 ``agent_swap``（只在樣板啟用 agent_swap 時；做法同 ``bind_forced_swap``）。

    樣板不得預填：綁定時先清掉既有的 ``thesis_hint_symbols``／``verdicts_ref``，再依結論重綁；沒有可用結論或沒有 broken 時不綁
    （提示名單為空）。重建只讀 Policy。回傳新的 policy 物件，呼叫端須重算 content_sha256。"""
    bound = copy.deepcopy(dict(policy))
    sizing = bound.get("position_sizing")
    gate = sizing.get("agent_swap") if isinstance(sizing, dict) else None
    if not isinstance(gate, dict):
        return bound
    gate.pop("thesis_hint_symbols", None)
    gate.pop("verdicts_ref", None)
    expected = _expected_binding(verdicts)
    if expected is not None:
        gate["thesis_hint_symbols"], gate["verdicts_ref"] = expected
    return bound


def validate_thesis_hint_binding(policy: Mapping[str, object], verdicts: Optional[Mapping[str, object]]) -> List[str]:
    """以重讀的論點檢視結論重算綁定並與 Policy 比對：樣板預填、缺漏或與結論不符一律回錯誤。沒有 agent_swap 的 Policy 不檢查。"""
    sizing = policy.get("position_sizing")
    gate = sizing.get("agent_swap") if isinstance(sizing, Mapping) else None
    if not isinstance(gate, Mapping):
        return []
    expected = _expected_binding(verdicts)
    actual = (gate.get("thesis_hint_symbols"), gate.get("verdicts_ref")) if "thesis_hint_symbols" in gate or "verdicts_ref" in gate else None
    if expected is None and actual is not None:
        return ["position_sizing.agent_swap 的 thesis_hint_symbols／verdicts_ref 與論點檢視結論不符（結論沒有可用的 broken 項目，不得預填）"]
    if expected is not None and (actual is None or list(actual[0] or []) != expected[0] or actual[1] != expected[1]):  # type: ignore[arg-type]
        return ["position_sizing.agent_swap 的 thesis_hint_symbols／verdicts_ref 與重讀的論點檢視結論不一致"]
    return []


# ---------------------------------------------------------------- 閘門與 team_inputs 2.9

SWAP_NOMINATION_KEY = "swap_nomination"


def agent_swap_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """``position_sizing.agent_swap`` 閘門；沒有（或 sizing 不是物件）時回 None。"""
    gate = sizing.get("agent_swap") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def _expected_pair_binding(
    nomination: Mapping[str, object], with_purpose: bool = False, with_size: bool = False, with_size_all: bool = False,
) -> Dict[str, object]:
    """v2 在 Policy 綁定的提名內容：依 Agent 先後的配對與封存識別（只有 completed 才有配對）。

    ``with_purpose``（agent_swap.exposure 啟用）時每對另綁 ``purpose``；沒有閘門時與舊版逐位元相同。
    ``with_size``（agent_swap.quality_dip v3）時 quality_dip 配對另綁份量格 ``size``；``with_size_all``（agent_swap.entry_reserve）時每一組都綁。"""
    return {
        "nominated_pairs": [
            {"buy_symbol": str(item["buy_symbol"]).upper(), "sell_symbol": str(item["sell_symbol"]).upper(),
             **({"purpose": str(item["purpose"])} if with_purpose else {}),
             **({"size": str(item["size"])} if with_size_all or (with_size and item.get("purpose") == PURPOSE_QUALITY_DIP) else {})}
            for item in nominations_of(nomination)
        ],
        "nomination_ref": {
            "nomination_id": str(nomination["nomination_id"]), "sha256": str(nomination["content_sha256"]),
            "status": str(nomination["status"]),
        },
    }


def apply_swap_nomination(policy: Mapping[str, object], nomination: Mapping[str, object]) -> Dict[str, object]:
    """把提名配對綁進 Policy 的 ``agent_swap``（只在 v2；做法同 ``apply_selection_ranking``）：樣板不得預填，先清掉再綁；
    v1（影子）與沒有閘門的 Policy 原樣回傳（配置不讀提名）。呼叫端須重算 content_sha256。"""
    gate = agent_swap_gate(policy.get("position_sizing"))
    if gate is None or gate.get("version") != "v2":
        return dict(policy)
    result = copy.deepcopy(dict(policy))
    bound = result["position_sizing"]["agent_swap"]
    bound.pop("nominated_pairs", None)
    bound.pop("nomination_ref", None)
    bound.update(_expected_pair_binding(
        nomination, exposure_swap_gate(policy.get("position_sizing")) is not None,
        quality_dip_v3_gate(policy.get("position_sizing")) is not None,
        entry_reserve_gate(policy.get("position_sizing")) is not None))
    if isinstance(bound.get("quality_dip"), dict):
        # v3 的裁決綁定要到風險 Agent 之後才由 pipeline 綁（apply_quality_dip_ruling）；樣板不得預填，這裡先清掉。
        bound["quality_dip"].pop("ruling_ref", None)
        bound["quality_dip"].pop("rulings", None)
        bound["quality_dip"].pop("ladder_state", None)
    result.pop("content_sha256", None)
    return result


def validate_team_swap_nomination(
    bundle: Mapping[str, object],
    policy: Mapping[str, object],
    trade_decision: Mapping[str, object],
    debate: Mapping[str, object],
    team: Mapping[str, object],
    errors: List[str],
) -> None:
    """Finalizer 對 team_inputs 2.9 的 ``swap_nomination`` 整份重建驗證（信封、雜湊、事實重算與 2.6 A 段靜態檢查）。

    只讀 team_inputs 內已由其他檢查驗證過的入選排序、持股檢視、曝險檢視與前次決策背景；缺任何一項即拒絕。"""
    nomination = team.get(SWAP_NOMINATION_KEY)
    if not isinstance(nomination, Mapping):
        errors.append("team_inputs.swap_nomination 必須是物件")
        return
    ranking, review, exposure = team.get("selection_ranking"), team.get("position_review"), team.get("exposure_review")
    if not all(isinstance(value, Mapping) for value in (ranking, review, exposure)):
        errors.append("team_inputs.swap_nomination 需要同一份入選排序、持股檢視與曝險檢視才能重建")
        return
    prior = team.get("prior_decision_context")
    try:
        facts = team.get(EXPOSURE_FACTS_KEY)
        dip = team.get(DIP_FACTS_KEY)
        problems = SwapNominationValidator(
            bundle, policy, trade_decision, ranking, debate, review, exposure,  # type: ignore[arg-type]
            prior_context=prior if isinstance(prior, Mapping) else None,
            exposure_facts=facts if isinstance(facts, Mapping) else None,
            dip_facts=dip if isinstance(dip, Mapping) else None,
            decline_depth_facts=team.get(DECLINE_DEPTH_FACTS_KEY) if isinstance(team.get(DECLINE_DEPTH_FACTS_KEY), Mapping) else None,
        ).validate(nomination)
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        problems = ["無法重建驗證：%s" % error]
    errors.extend("team_inputs.swap_nomination：%s" % problem for problem in problems)
    # v2：Policy 綁定的配對與提名封存必須完全一致（配置只讀 Policy）；v1（影子）不得綁定（sizing 驗證已拒絕預填）。
    gate = agent_swap_gate(policy.get("position_sizing"))
    if gate is not None and gate.get("version") == "v2":
        try:
            expected = _expected_pair_binding(
                nomination, exposure_swap_gate(policy.get("position_sizing")) is not None,
                quality_dip_v3_gate(policy.get("position_sizing")) is not None,
                entry_reserve_gate(policy.get("position_sizing")) is not None)
        except (KeyError, TypeError, ValueError) as error:
            errors.append("team_inputs.swap_nomination 無法比對 Policy 綁定：%s" % error)
            return
        for field, value in expected.items():
            if gate.get(field) != value:
                errors.append("DecisionPolicy.position_sizing.agent_swap.%s 與 team_inputs.swap_nomination 不一致" % field)
