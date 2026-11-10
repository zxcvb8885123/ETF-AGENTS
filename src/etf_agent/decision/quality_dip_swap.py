"""優質股第三階段：換股提名的 ``purpose: quality_dip`` 配對（``position_sizing.agent_swap.quality_dip`` 版本閘門）。

設計見 ``docs/quality_dip_plan.md`` 第 4.2、10.3 節（第三階段改走換股提名，不另開程式配對路徑）與第 10.4 節（實作紀錄與保守取捨）。
閘門只由人設定、策略反思不得改（``agent_swap`` 前綴已在 ``FORBIDDEN_PREFIXES``）；沒有 ``quality_dip`` 欄位的舊 Policy／舊提名封存／
舊 run 逐位元維持原行為。

- 與曝險階段 2 的 ``purpose`` 合用同一個列舉（多一個值 ``quality_dip``），**不另開欄位**；所以前提含 ``agent_swap.exposure``（``purpose`` 的宿主）。
- **v1（影子）**：提名可以標 ``purpose: quality_dip``，5c Validator 照樣做結構檢查並封存；**配置完全不讀 quality_dip 配對**（不會被當成 thesis 配對成交）。
- **v2（正式）**：需 ``agent_swap`` v2；配置依序取用 quality_dip 配對，另加本閘門的硬上限：每次對數、每檔買進權重 ``max_entry_weight``、
  買進金額不超過該對換出者的賣款、``turn_assessment`` 不低於 ``min_turn_assessment``、回撤下限 ``stance_floor`` 為 defensive 時整個略過；
  減碼日由 ``_agent_swap`` 整個略過。現金下限、換手上限、檔數、單股與產業上限照舊是後段上限，一律不放寬。

5c 的結構檢查（Validator，只驗結構、不判斷理由好壞；全部失敗走修正循環）：

- 換入者：thesis 配對的全部資格之外，第一輪信心不低於 ``min_new_conviction``；``dip_facts`` 整份可用、該股 ``price_pattern`` 為下跌
  （``market_explained``／``beyond_market``）、``new_adverse_facts`` 為空；``candidate`` 為 ``market_dragged`` 時 ``dip_assessment`` 必須是
  ``market_driven``；``candidate`` 為 ``oversold`` 時只有閘門寫了 ``oversold`` 區塊才可，且 ``dip_assessment`` 必須是 ``oversold_cause_unclear``；
  ``buy_basis_ids`` 至少一個是該股已採納、``claim_kind`` 不是 ``price`` 的多頭 claim（缺 ``claim_kind`` 不算）。
- 換出者：thesis 配對的換出依據規則不變（第一輪已採納的空頭論點、持股檢視警示或持股事實），另需第一輪 ``hold`` 信心為 ``low``、
  持有天數不少於本閘門的 ``min_holding_days``。
- 缺資料既不通過也不否決：條件不成立的股票仍可用 ``purpose: thesis`` 提名（照換股提名原本的規則），只是不能標 quality_dip。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Set

from .fields import DecisionToolError, decimal_value, reject_unknown_fields
from .turn_facts import TURN_ASSESSMENTS

QUALITY_DIP_SWAP_V1 = "v1"  # 影子：配置不讀 quality_dip 配對
QUALITY_DIP_SWAP_V2 = "v2"  # 正式：配置依序取用 quality_dip 配對（加本閘門的硬上限）
QUALITY_DIP_SWAP_VERSIONS = (QUALITY_DIP_SWAP_V1, QUALITY_DIP_SWAP_V2)
PURPOSE_QUALITY_DIP = "quality_dip"
QUALITY_DIP_SWAP_REQUIRED = {"version", "max_swaps_per_run", "max_entry_weight", "min_new_conviction", "min_holding_days", "min_turn_assessment"}
QUALITY_DIP_SWAP_FIELDS = QUALITY_DIP_SWAP_REQUIRED | {"oversold", "turn_assessment"}
OVERSOLD_FIELDS = {"max_swaps_per_run", "max_entry_weight"}
# 已決定（第 4.2 節、第 7.2 節第 8 項）：必須有轉向跡象，所以只允許 turn_unconfirmed 或 turn_confirmed。
MIN_TURN_ASSESSMENTS = ("turn_unconfirmed", "turn_confirmed")
QUALITY_DIP_CONVICTIONS = ("high", "medium", "low")
QUALITY_DIP_MAX_HOLDING_DAYS = 60
QUALITY_DIP_VICTIM_CONVICTION = "low"
DECLINE_PATTERNS = ("market_explained", "beyond_market")
PRICE_CLAIM_KIND = "price"
# 換入者不能以 quality_dip 提名的原因代碼（簡報與 Validator 共用）。
QUALITY_DIP_BUY_REASONS = (
    "dip_facts_unavailable", "no_dip_row", "no_decline", "new_adverse_facts", "candidate_none", "oversold_not_enabled",
    "assessment_mismatch", "conviction_below_gate", "no_non_price_bull_basis",
)
# 持股不能作為 quality_dip 換出者的原因代碼（在 thesis 換出資格之外）。
QUALITY_DIP_SELL_REASONS = ("hold_conviction_not_low", "holding_days_short_for_gate")
QUALITY_DIP_SWAP_NOTE = (
    "purpose=quality_dip 的配對：換入者是被大盤拖累（或閘門另開的超跌）候選。程式只驗結構：dip_facts 該股為下跌且查無新壞事、"
    "你的 dip_assessment 與候選標示相符（被大盤拖累＝market_driven；超跌＝oversold_cause_unclear）、buy_basis_ids 至少一個是該股已採納、"
    "claim_kind 不是 price 的多頭論點；換出者第一輪必須是 hold／low，換出依據規則與 thesis 相同。候選標示與 dip_assessment 只是背景，"
    "不是依據；不符合時改用 purpose=thesis（照原本規則）或刪除該對。程式另會限制每次對數、每檔買進權重、買進金額不超過換出賣款，"
    "並在沒有轉向跡象或回撤強制防守時不取用；這些都在配置時由程式決定，你不需要也不得輸出任何數字。"
)


def quality_dip_swap_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """``position_sizing.agent_swap.quality_dip`` 閘門；沒有（或 sizing／agent_swap 不是物件）時回 None。"""
    swap = sizing.get("agent_swap") if isinstance(sizing, Mapping) else None
    gate = swap.get("quality_dip") if isinstance(swap, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def nomination_purposes(sizing: object) -> tuple:
    """提名 ``purpose`` 的合法值：沒有 quality_dip 閘門時與曝險階段 2 相同（thesis／exposure），有時多 quality_dip。"""
    base = ("thesis", "exposure")
    return base + (PURPOSE_QUALITY_DIP,) if quality_dip_swap_gate(sizing) is not None else base


# ---------------------------------------------------------------- 閘門

def _int_in(value: object, low: int, high: Optional[int]) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= low and (high is None or value <= high)


def _ratio(value: object, label: str, errors: List[str]) -> Optional[Decimal]:
    if isinstance(value, bool) or not isinstance(value, str):
        errors.append("%s 必須是數字字串" % label)
        return None
    try:
        number = decimal_value(value, label)
    except DecisionToolError as error:
        errors.append(str(error))
        return None
    if number <= 0 or number >= 1:
        errors.append("%s 必須大於 0 且小於 1" % label)
        return None
    return number


def validate_quality_dip_swap_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """形狀、值域與版本相依。前提（agent_swap、agent_swap.exposure、quality_dip_facts、quality_dip_assessment、
    cash_stance_turn_facts）由 GATE_PREREQUISITES 檢查。全部數字必填、程式不給預設（由人設定）。"""
    swap = sizing["agent_swap"]
    gate = swap["quality_dip"]  # type: ignore[index]
    prefix = "position_sizing.agent_swap.quality_dip"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, QUALITY_DIP_SWAP_FIELDS, prefix, errors)
    missing = sorted(QUALITY_DIP_SWAP_REQUIRED - set(gate))
    if missing:
        errors.append("%s 缺少必要欄位（由人設定，程式不提供預設值）：%s" % (prefix, "、".join(missing)))
    version = gate.get("version")
    if version not in QUALITY_DIP_SWAP_VERSIONS:
        errors.append("%s.version 只允許 %s" % (prefix, "／".join(QUALITY_DIP_SWAP_VERSIONS)))
    if version == QUALITY_DIP_SWAP_V2 and isinstance(swap, Mapping) and swap.get("version") != "v2":
        errors.append("%s.version=v2 只允許用於 agent_swap.version=v2（配置才讀提名）" % prefix)
    limit = swap.get("max_swaps_per_run") if isinstance(swap, Mapping) else None
    limit = limit if _int_in(limit, 0, None) else None
    if "max_swaps_per_run" in gate and not _int_in(gate["max_swaps_per_run"], 0, limit):
        errors.append("%s.max_swaps_per_run 必須是 0～agent_swap.max_swaps_per_run 的整數（不放寬現行上限）" % prefix)
    entry = _ratio(gate["max_entry_weight"], "%s.max_entry_weight" % prefix, errors) if "max_entry_weight" in gate else None
    if "min_new_conviction" in gate and gate["min_new_conviction"] not in QUALITY_DIP_CONVICTIONS:
        errors.append("%s.min_new_conviction 只允許 %s" % (prefix, "／".join(QUALITY_DIP_CONVICTIONS)))
    if "min_holding_days" in gate and not _int_in(gate["min_holding_days"], 0, QUALITY_DIP_MAX_HOLDING_DAYS):
        errors.append("%s.min_holding_days 必須是 0～%d 的整數" % (prefix, QUALITY_DIP_MAX_HOLDING_DAYS))
    if "min_turn_assessment" in gate and gate["min_turn_assessment"] not in MIN_TURN_ASSESSMENTS:
        errors.append("%s.min_turn_assessment 只允許 %s（必須有轉向跡象）" % (prefix, "／".join(MIN_TURN_ASSESSMENTS)))
    if "turn_assessment" in gate:
        if version != QUALITY_DIP_SWAP_V2:
            errors.append("%s.turn_assessment 只用於 version=v2（由 pipeline 綁定風險 Agent 的 turn_assessment）" % prefix)
        elif gate["turn_assessment"] not in TURN_ASSESSMENTS:
            errors.append("%s.turn_assessment 只允許 %s" % (prefix, "／".join(TURN_ASSESSMENTS)))
    if "oversold" in gate:
        oversold = gate["oversold"]
        label = "%s.oversold" % prefix
        if not isinstance(oversold, Mapping):
            errors.append("%s 必須是物件" % label)
            return
        reject_unknown_fields(oversold, OVERSOLD_FIELDS, label, errors)
        missing = sorted(OVERSOLD_FIELDS - set(oversold))
        if missing:
            errors.append("%s 缺少必要欄位：%s" % (label, "、".join(missing)))
        cap = gate.get("max_swaps_per_run") if _int_in(gate.get("max_swaps_per_run"), 0, None) else None
        if "max_swaps_per_run" in oversold and not _int_in(oversold["max_swaps_per_run"], 0, cap):
            errors.append("%s.max_swaps_per_run 必須是 0～quality_dip.max_swaps_per_run 的整數（不得高於被大盤拖累候選的上限）" % label)
        if "max_entry_weight" in oversold:
            weight = _ratio(oversold["max_entry_weight"], "%s.max_entry_weight" % label, errors)
            if weight is not None and entry is not None and weight > entry:
                errors.append("%s.max_entry_weight 不得高於 quality_dip.max_entry_weight" % label)


def bind_quality_dip_turn(swap: Mapping[str, object], sizing: Mapping[str, object], cash_stance: Mapping[str, object]) -> Dict[str, object]:
    """``apply_trade_decision`` 用：v2 把風險 Agent 的 ``turn_assessment`` 綁進 ``agent_swap.quality_dip.turn_assessment``。

    ``swap`` 是目前的 agent_swap 物件（可能已綁 exposure view）；v1（影子）與沒有閘門回傳拷貝、不綁定。"""
    result = dict(swap)
    gate = quality_dip_swap_gate(sizing)
    if gate is None or gate.get("version") != QUALITY_DIP_SWAP_V2:
        return result
    turn = cash_stance.get("turn_assessment")
    if turn not in TURN_ASSESSMENTS:
        raise DecisionToolError("Policy 啟用 agent_swap.quality_dip v2，現金姿態必須給 turn_assessment（%s）" % "／".join(TURN_ASSESSMENTS))
    result["quality_dip"] = {**dict(gate), "turn_assessment": turn}
    return result


def quality_dip_swap_binding(sizing: object) -> Optional[Dict[str, object]]:
    """配置用的綁定。沒有閘門或 agent_swap 不是 v2（配置不讀提名）回 None。

    回傳 ``{"version", "purposes"}``（``purposes`` 與 ``nominated_pairs`` 同序）；v2 另有 ``turn_assessment``、``min_turn``、``cap``、
    ``entry_weight``、``oversold``（None 或 ``{"cap", "entry_weight"}``）。v2 未綁 turn_assessment 時拋錯，不靜默當成沒有轉向跡象。"""
    gate = quality_dip_swap_gate(sizing)
    swap = sizing.get("agent_swap") if isinstance(sizing, Mapping) else None
    if gate is None or not isinstance(swap, Mapping) or swap.get("version") != "v2":
        return None
    pairs = swap.get("nominated_pairs")
    if not isinstance(pairs, list):
        raise DecisionToolError("DecisionPolicy 啟用 agent_swap v2，但尚未綁定換股提名（nominated_pairs／nomination_ref）")
    purposes = [str(pair.get("purpose")) for pair in pairs]
    allowed = nomination_purposes(sizing)
    if any(purpose not in allowed for purpose in purposes):
        raise DecisionToolError("agent_swap.quality_dip 啟用時 nominated_pairs 每對必須有 purpose（%s）" % "／".join(allowed))
    if gate.get("version") == QUALITY_DIP_SWAP_V1:
        return {"version": QUALITY_DIP_SWAP_V1, "purposes": purposes}
    turn = gate.get("turn_assessment")
    if turn not in TURN_ASSESSMENTS:
        raise DecisionToolError("DecisionPolicy 啟用 agent_swap.quality_dip v2，但尚未綁定風險 Agent 的 turn_assessment")
    oversold = gate.get("oversold")
    return {
        "version": QUALITY_DIP_SWAP_V2, "purposes": purposes, "turn_assessment": str(turn),
        "min_turn": str(gate["min_turn_assessment"]), "cap": int(gate["max_swaps_per_run"]),  # type: ignore[arg-type]
        "entry_weight": decimal_value(gate["max_entry_weight"], "max_entry_weight"),
        "oversold": None if not isinstance(oversold, Mapping) else {
            "cap": int(oversold["max_swaps_per_run"]),  # type: ignore[arg-type]
            "entry_weight": decimal_value(oversold["max_entry_weight"], "oversold.max_entry_weight"),
        },
    }


def turn_allows(binding: Mapping[str, object]) -> bool:
    """風險 Agent 的 turn_assessment 不低於閘門的 min_turn_assessment（no_turn_evidence 恆不通過）。"""
    turn, minimum = str(binding["turn_assessment"]), str(binding["min_turn"])
    return turn in MIN_TURN_ASSESSMENTS and TURN_ASSESSMENTS.index(turn) >= TURN_ASSESSMENTS.index(minimum)


# ---------------------------------------------------------------- 5c 事實與結構檢查

def claim_kinds(debate: Mapping[str, object]) -> Dict[str, Optional[str]]:
    """claim_id → claim_kind（研究員沒填時為 None，不算非價格依據）。"""
    kinds: Dict[str, Optional[str]] = {}
    for packet in debate.get("packets", []):  # type: ignore[union-attr]
        for item in packet.get("items", []):
            for claim in item.get("claims", []):
                kind = claim.get("claim_kind")
                kinds[str(claim["claim_id"])] = str(kind) if isinstance(kind, str) and kind else None
    return kinds


def _dip_rows(dip_facts: Optional[Mapping[str, object]]) -> Dict[str, Mapping[str, object]]:
    if not isinstance(dip_facts, Mapping):
        return {}
    return {str(item["symbol"]).upper(): item for item in dip_facts.get("items", []) if isinstance(item, Mapping)}  # type: ignore[union-attr]


def non_price_bull_ids(basis_ids: Sequence[str], kinds: Mapping[str, Optional[str]]) -> List[str]:
    """已採納的多頭依據中 claim_kind 不是 price（且有填）的識別碼。"""
    return [identifier for identifier in basis_ids if kinds.get(identifier) not in (None, PRICE_CLAIM_KIND)]


def buy_reasons(
    symbol: str,
    conviction: object,
    basis_ids: Sequence[str],
    gate: Mapping[str, object],
    dip_facts: Optional[Mapping[str, object]],
    assessment: object,
    kinds: Mapping[str, Optional[str]],
) -> List[str]:
    """換入者不能以 quality_dip 提名的原因（空清單＝可以）；在 thesis 換入資格之外另算，不影響 thesis。"""
    reasons: List[str] = []
    row = _dip_rows(dip_facts).get(symbol)
    if not isinstance(dip_facts, Mapping) or dip_facts.get("data_status") != "available":
        reasons.append("dip_facts_unavailable")
    elif row is None:
        reasons.append("no_dip_row")
    else:
        flags = row.get("fundamental_flags") if isinstance(row.get("fundamental_flags"), Mapping) else {}
        if row.get("price_pattern") not in DECLINE_PATTERNS:
            reasons.append("no_decline")
        if not isinstance(flags, Mapping) or flags.get("new_adverse_facts"):
            reasons.append("new_adverse_facts")
        candidate = row.get("candidate")
        if candidate == "market_dragged":
            if assessment != "market_driven":
                reasons.append("assessment_mismatch")
        elif candidate == "oversold":
            if not isinstance(gate.get("oversold"), Mapping):
                reasons.append("oversold_not_enabled")
            elif assessment != "oversold_cause_unclear":
                reasons.append("assessment_mismatch")
        else:
            reasons.append("candidate_none")
    minimum = str(gate.get("min_new_conviction"))
    if conviction not in QUALITY_DIP_CONVICTIONS or minimum not in QUALITY_DIP_CONVICTIONS or (
        QUALITY_DIP_CONVICTIONS.index(str(conviction)) > QUALITY_DIP_CONVICTIONS.index(minimum)
    ):
        reasons.append("conviction_below_gate")
    if not non_price_bull_ids(basis_ids, kinds):
        reasons.append("no_non_price_bull_basis")
    return reasons


def sell_reasons(hold_conviction: object, holding_days: int, gate: Mapping[str, object]) -> List[str]:
    """持股不能作為 quality_dip 換出者的原因（thesis 換出資格之外另加）。"""
    reasons: List[str] = []
    if hold_conviction != QUALITY_DIP_VICTIM_CONVICTION:
        reasons.append("hold_conviction_not_low")
    if holding_days < int(gate["min_holding_days"]):  # type: ignore[arg-type]
        reasons.append("holding_days_short_for_gate")
    return reasons


def validate_quality_dip_pair(
    prefix: str,
    buy_ids: object,
    candidate: Optional[Mapping[str, object]],
    victim: Optional[Mapping[str, object]],
    kinds: Mapping[str, Optional[str]],
    errors: List[str],
) -> None:
    """一對 quality_dip 提名的結構檢查（候選資格已由 ``nomination_facts`` 算進 ``quality_dip_reasons``）。

    回饋用中性措辭並列兩種修法：改用 purpose=thesis（照原本規則），或刪除該對。"""
    fix = "；可改用 purpose=thesis（照換股提名原本的規則）或刪除該對"
    if candidate is not None:
        reasons = [str(code) for code in candidate.get("quality_dip_reasons", [])]  # type: ignore[union-attr]
        if reasons:
            errors.append("%s.buy_symbol %s 不符合 purpose=quality_dip 的結構條件：%s%s" % (prefix, candidate["symbol"], "、".join(reasons), fix))
        if isinstance(buy_ids, list) and not non_price_bull_ids([str(item) for item in buy_ids], kinds):
            errors.append("%s.buy_basis_ids 在 purpose=quality_dip 至少要有一個 claim_kind 不是 price 的已採納多頭論點%s" % (prefix, fix))
    if victim is not None:
        reasons = [str(code) for code in victim.get("quality_dip_reasons", [])]  # type: ignore[union-attr]
        if reasons:
            errors.append("%s.sell_symbol %s 不符合 purpose=quality_dip 的換出條件：%s%s" % (prefix, victim["symbol"], "、".join(reasons), fix))


def oversold_symbols(dip_facts: Optional[Mapping[str, object]]) -> Set[str]:
    return {symbol for symbol, row in _dip_rows(dip_facts).items() if row.get("candidate") == "oversold"}


__all__ = [
    "PURPOSE_QUALITY_DIP", "QUALITY_DIP_BUY_REASONS", "QUALITY_DIP_SELL_REASONS", "QUALITY_DIP_SWAP_NOTE", "QUALITY_DIP_SWAP_V1",
    "QUALITY_DIP_SWAP_V2", "bind_quality_dip_turn", "buy_reasons", "claim_kinds", "nomination_purposes", "non_price_bull_ids",
    "oversold_symbols", "quality_dip_swap_binding", "quality_dip_swap_gate", "sell_reasons", "turn_allows",
    "validate_quality_dip_pair", "validate_quality_dip_swap_gate",
]
