"""優質股第三階段：換股提名的 ``purpose: quality_dip`` 配對（``position_sizing.agent_swap.quality_dip`` 版本閘門）。

設計見 ``docs/plans/quality_dip_plan.md`` 第 4.2、10.3 節（第三階段改走換股提名，不另開程式配對路徑）與第 10.4 節（實作紀錄與保守取捨）。
閘門只由人設定、策略反思不得改（``agent_swap`` 前綴已在 ``FORBIDDEN_PREFIXES``）；沒有 ``quality_dip`` 欄位的舊 Policy／舊提名封存／
舊 run 逐位元維持原行為。

- 與曝險階段 2 的 ``purpose`` 合用同一個列舉（多一個值 ``quality_dip``），**不另開欄位**；所以前提含 ``agent_swap.exposure``（``purpose`` 的宿主）。
- **v1（影子）**：提名可以標 ``purpose: quality_dip``，5c Validator 照樣做結構檢查並封存；**配置完全不讀 quality_dip 配對**（不會被當成 thesis 配對成交）。
- **v2（正式）**：需 ``agent_swap`` v2；配置依序取用 quality_dip 配對，另加本閘門的硬上限：每次對數、每檔買進權重 ``max_entry_weight``、
  買進金額不超過該對換出者的賣款、``turn_assessment`` 不低於 ``min_turn_assessment``、回撤下限 ``stance_floor`` 為 defensive 時整個略過；
  減碼日由 ``_agent_swap`` 整個略過。現金下限、換手上限、檔數、單股與產業上限照舊是後段上限，一律不放寬。

- **v3（組數與份量由 Agent 提出、風險 Agent 裁決；設計見 ``docs/plans/quality_dip_agent_sizing_plan.md``）**：需 ``agent_swap`` v2；``max_swaps_per_run``
  與 ``max_entry_weight`` 改為人設的**絕對硬上限**（quality_dip 組數另計，不占用 ``agent_swap.max_swaps_per_run``）；交易 Agent 在每個
  quality_dip 配對填份量格列舉 ``size``（small／medium／large），風險 Agent 另做一次裁決（approve／downsize／reject，只能縮不能放），
  程式把有效份量格對成買進權重上限（large＝硬上限，medium 為其 2/3，small 為其 1/3）。裁決沒有完成（用盡、暫時性失敗、程式前置檢查不過）
  一律取消全部 quality_dip 配對，不退回 v2 行為。v1、v2 的程式分支原樣保留，供舊 Policy 與舊 run 重建。

- **v4（連跌後逢低布局，梯度 ``ladder``；設計見 ``docs/plans/agent_led_offense_plan.md`` 4.13、7.3）**：v3 的超集（份量格、風險 Agent 裁決、組數另計全部照舊），
  另有人填的 ``ladder`` 區塊與 pipeline 綁定的 ``ladder_state``。有 ``ladder`` 時 ``min_turn_assessment`` 才可填 ``no_turn_evidence``：沒有轉向跡象
  也能抄底，但交易池離 60 日高點回檔須達 ``min_pool_drawdown``；有轉向跡象（turn_unconfirmed 以上）時不受該門檻限制。梯度另限制總抄底曝險、
  批數、批次間隔與上一批之後再跌的暫停線（``decline_depth.build_ladder_state``，缺資料一律不放行）。數字全由人填，程式不給預設；
  沒有 ``ladder`` 的 v1／v2／v3 與舊 run 逐位元不變。
  ``ladder.exposure_basis``（選配列舉，已實作、尚未開啟）填 ``quality_dip_fills`` 時，總抄底曝險與批數只計 purpose=quality_dip 的成交
  （成交清單 ``ladder_entries`` 由 pipeline 從決策庫取得並綁進 Policy，讀不到即以 ``entries_unavailable`` 關閉）；缺席時照舊計所有新建倉。

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

import copy
from datetime import date
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Set

from .fields import DecisionToolError, decimal_value, reject_unknown_fields
from .turn_facts import TURN_ASSESSMENTS

QUALITY_DIP_SWAP_V1 = "v1"  # 影子：配置不讀 quality_dip 配對
QUALITY_DIP_SWAP_V2 = "v2"  # 正式：配置依序取用 quality_dip 配對（加本閘門的硬上限）
QUALITY_DIP_SWAP_V3 = "v3"  # 正式：硬上限＋交易 Agent 的份量格＋風險 Agent 的裁決；組數另計
QUALITY_DIP_SWAP_V4 = "v4"  # 正式：v3 的超集＋梯度 ladder（連跌中沒有轉向跡象也能分批抄底，全部上限人填）
QUALITY_DIP_SWAP_VERSIONS = (QUALITY_DIP_SWAP_V1, QUALITY_DIP_SWAP_V2, QUALITY_DIP_SWAP_V3, QUALITY_DIP_SWAP_V4)
# 份量格、風險 Agent 裁決、組數另計、team_inputs 2.10 的版本家族（v4 是 v3 的超集，這些行為完全相同）。
QUALITY_DIP_SIZED_VERSIONS = (QUALITY_DIP_SWAP_V3, QUALITY_DIP_SWAP_V4)
# v2 起綁定風險 Agent turn_assessment 的版本。
QUALITY_DIP_TURN_BOUND_VERSIONS = (QUALITY_DIP_SWAP_V2, QUALITY_DIP_SWAP_V3, QUALITY_DIP_SWAP_V4)
# v3：份量格與裁決（Agent 只給列舉，不輸出數字）。份量格對成權重上限的比例是版本內的程式常數（待人決定第 5 項已選 A）：
# large＝max_entry_weight、medium＝2/3、small＝1/3；以 Decimal 計算，不用浮點。
SIZE_GRADES = ("small", "medium", "large")
SIZE_FRACTIONS = {"large": (1, 1), "medium": (2, 3), "small": (1, 3)}
RULING_APPROVE, RULING_DOWNSIZE, RULING_REJECT = "approve", "downsize", "reject"
RULINGS = (RULING_APPROVE, RULING_DOWNSIZE, RULING_REJECT)
RULING_STATUSES = ("completed", "not_required", "skipped_by_program", "exhausted", "unavailable")
RULING_REF_FIELDS = {"ruling_id", "sha256", "status"}
# v3 的 quality_dip 組數另計（不占 agent_swap.max_swaps_per_run），所以不能以 agent_swap 的上限當範圍；這裡只是結構性的合理範圍（人填的數字必須落在其中）。
QUALITY_DIP_V3_MAX_SWAPS = 10
PURPOSE_QUALITY_DIP = "quality_dip"
QUALITY_DIP_SWAP_REQUIRED = {"version", "max_swaps_per_run", "max_entry_weight", "min_new_conviction", "min_holding_days", "min_turn_assessment"}
QUALITY_DIP_SWAP_FIELDS = QUALITY_DIP_SWAP_REQUIRED | {"oversold", "turn_assessment", "ruling_ref", "rulings", "ladder", "ladder_state", "ladder_entries"}
# v4 的梯度（全部人填、全部必填、程式不給預設）；ladder_state 只能由 pipeline 綁定（decline_depth.build_ladder_state）。
LADDER_RATIO_FIELDS = ("min_pool_drawdown", "max_dip_exposure", "pause_after_further_drop")
LADDER_INT_FIELDS = {"max_tranches": (1, 10), "min_days_between_tranches": (0, 20)}
LADDER_FIELDS = set(LADDER_RATIO_FIELDS) | set(LADDER_INT_FIELDS)
# 選配：``exposure_basis``（人填列舉）。缺席＝總抄底曝險與批數照舊計所有新建倉；``quality_dip_fills``＝只計 purpose=quality_dip 的成交，
# 成交清單 ``ladder_entries`` 由 pipeline 從決策庫確定性取得並綁進 Policy（做法同 reentry_cooldown，Finalizer 只重算、不重掃決策庫）。
LADDER_EXPOSURE_BASIS_FILLS = "quality_dip_fills"
LADDER_EXPOSURE_BASES = (LADDER_EXPOSURE_BASIS_FILLS,)
LADDER_OPTIONAL_FIELDS = {"exposure_basis"}
LADDER_ENTRY_FIELDS = {"symbol", "entry_date", "run_id"}
LADDER_STATE_FIELDS = {
    "status", "reason", "turn_assessment", "facts_id", "anchor_date", "pool_drawdown", "exposure_used", "exposure_remaining",
    "tranches", "last_tranche_date", "days_since_last_tranche", "further_drop", "paused",
    "exposure_basis",  # 選配：只有 ladder.exposure_basis 啟用時才有
}
LADDER_STATUSES = ("open", "closed")
LADDER_CLOSED_REASONS = (
    "facts_unavailable", "held_since_unavailable", "depth_below_gate", "exposure_full", "tranches_full", "tranche_interval",
    "paused_after_drop", "pause_unavailable",
    "entries_unavailable",  # 只有 ladder.exposure_basis=quality_dip_fills 才會出現：成交清單讀不到或與持股不一致
)
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
QUALITY_DIP_SWAP_NOTE_V3 = (
    QUALITY_DIP_SWAP_NOTE
    + " v3：每個 purpose=quality_dip 的配對另填份量格 size（small＝試單、medium、large＝把握最高）；組數就是你列出的 quality_dip 配對數，"
    "不超過簡報的 max_quality_dip_pairs（與其他配對的上限分開計）。風險 Agent 稍後會對每一組裁決（同意、降一格或否決，只能縮不能放），"
    "程式再把份量格對成買進權重上限；你不得輸出任何百分比或數字。若簡報顯示有可提名的 quality_dip 換入者與換出者、而你沒有提名任何 quality_dip 配對，"
    "必須用 quality_dip_decline 交代原因（列舉加一句文字）；其他情況不得填。"
)
QUALITY_DIP_SWAP_NOTE_V4 = QUALITY_DIP_SWAP_NOTE_V3.replace(
    "並在沒有轉向跡象或回撤強制防守時不取用",
    "並在回撤強制防守時不取用；v4 另有人填的抄底梯度（簡報 decline_depth_facts.ladder）：沒有轉向跡象時須交易池離高點回檔夠深，"
    "且總抄底曝險、批數、批次間隔與上一批之後再跌的暫停線任一到線，當天 quality_dip 配對一律不成交（status 為 closed 時不要白費力氣提名；僅因回檔深度不足時 depth_needs_turn 為 true，仍可能在風險 Agent 判斷有轉向跡象時成交）",
) + (
    " 連跌深度事實（decline_depth_facts，背景、不可引用、理由也不要寫出這個鍵名）只說明交易池連跌多深、本視窗內過去類似情形之後發生什麼"
    "（樣本很少、不是預測）；這不是買進理由：換入者仍須有自己的非價格多頭論點與相符的 dip_assessment，不得因為「跌很深」就提名。"
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


def quality_dip_v3_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """``agent_swap.quality_dip`` 為 v3 或 v4（v4 是 v3 的超集：份量格、裁決、組數另計、team_inputs 2.10 完全相同）時回閘門，
    否則 None（沒有閘門、v1、v2 的程式路徑都不經過 v3 分支）。"""
    gate = quality_dip_swap_gate(sizing)
    return gate if gate is not None and gate.get("version") in QUALITY_DIP_SIZED_VERSIONS else None


def quality_dip_ladder_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """``agent_swap.quality_dip`` 為 v4 時回其 ``ladder`` 區塊，否則 None（沒有閘門、v1～v3 的程式路徑都不經過 ladder）。"""
    gate = quality_dip_swap_gate(sizing)
    if gate is None or gate.get("version") != QUALITY_DIP_SWAP_V4:
        return None
    ladder = gate.get("ladder")
    return ladder if isinstance(ladder, Mapping) else None


def ladder_counts_quality_dip_fills_only(sizing: object) -> bool:
    """v4 且 ``ladder.exposure_basis`` 為 ``quality_dip_fills``：總抄底曝險與批數只計 quality_dip 成交；否則（含欄位缺席）False，行為與舊版逐位元相同。"""
    ladder = quality_dip_ladder_gate(sizing)
    return ladder is not None and ladder.get("exposure_basis") == LADDER_EXPOSURE_BASIS_FILLS


def size_after_ruling(size: object, ruling: object) -> Optional[str]:
    """交易 Agent 的份量格經風險 Agent 裁決後的有效份量格：approve 不變、downsize 降一格、reject 回 None（取消該對）。

    風險 Agent 只能縮不能放：這裡沒有任何升級的路徑；small 不能再降（Validator 已拒絕，這裡再擋一次，不靜默當成 reject）。"""
    if size not in SIZE_GRADES or ruling not in RULINGS:
        raise DecisionToolError("份量格或裁決不是合法列舉：size=%s、ruling=%s" % (size, ruling))
    if ruling == RULING_REJECT:
        return None
    if ruling == RULING_APPROVE:
        return str(size)
    index = SIZE_GRADES.index(str(size))
    if index == 0:
        raise DecisionToolError("份量格 small 不能再降一格（只能 approve 或 reject）")
    return SIZE_GRADES[index - 1]


def size_entry_weight(base: Decimal, size: str) -> Decimal:
    """份量格對成的買進權重上限：硬上限 × 固定比例（Decimal，確定性；不高於 base）。"""
    numerator, denominator = SIZE_FRACTIONS[size]
    return base * Decimal(numerator) / Decimal(denominator)


def program_skip_reason(policy: Mapping[str, object]) -> Optional[str]:
    """quality_dip 的程式前置條件（人設的界線，不是 Agent 的判斷）；不成立回原因代碼，成立回 None。

    與配置（allocation._agent_swap）的整體條件同一口徑：回撤下限 stance_floor 為 defensive 或帳本歷史不可用、風險 Agent 的
    turn_assessment 低於 min_turn_assessment。v2 的配置另有「賣款可再用」一項，這裡不重複（它由配置自行取消，且不影響裁決是否呼叫）。
    pipeline 以它決定要不要呼叫裁決步驟，Finalizer 以它重算裁決狀態應該是 skipped_by_program 還是其他。需已綁定本次的
    drawdown_control 與 turn_assessment，缺少時拋錯（fail-closed），不靜默當成通過。"""
    sizing = policy.get("position_sizing")
    gate = quality_dip_swap_gate(sizing)
    if gate is None:
        raise DecisionToolError("DecisionPolicy 沒有 agent_swap.quality_dip，不應檢查前置條件")
    control = policy.get("drawdown_control")
    if not isinstance(control, Mapping) or control.get("status") not in ("available", "unavailable"):
        raise DecisionToolError("agent_swap.quality_dip 需要已綁定本次回撤的 drawdown_control（stance_floor）")
    turn = gate.get("turn_assessment")
    if turn not in TURN_ASSESSMENTS:
        raise DecisionToolError("DecisionPolicy 啟用 agent_swap.quality_dip，但尚未綁定風險 Agent 的 turn_assessment")
    if control.get("status") != "available":
        return "quality_dip_drawdown_unavailable"
    if control.get("stance_floor") == "defensive":
        return "quality_dip_stance_floor"
    if not turn_allows({"turn_assessment": turn, "min_turn": gate["min_turn_assessment"]}):
        return "quality_dip_turn"
    if gate.get("version") == QUALITY_DIP_SWAP_V4:
        # v4：梯度的程式判定（decline_depth.build_ladder_state，已由 pipeline 綁進 Policy、Finalizer 重算）；沒有綁定即拋錯，不靜默放行。
        state = gate.get("ladder_state")
        if not isinstance(state, Mapping) or state.get("status") not in LADDER_STATUSES:
            raise DecisionToolError("DecisionPolicy 啟用 agent_swap.quality_dip v4，但尚未綁定梯度狀態 ladder_state")
        if state["status"] != "open":
            return "quality_dip_ladder_%s" % state.get("reason")
    return None


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
    if version in QUALITY_DIP_TURN_BOUND_VERSIONS and isinstance(swap, Mapping) and swap.get("version") != "v2":
        errors.append("%s.version=%s 只允許用於 agent_swap.version=v2（配置才讀提名）" % (prefix, version))
    limit = swap.get("max_swaps_per_run") if isinstance(swap, Mapping) else None
    limit = limit if _int_in(limit, 0, None) else None
    if version in QUALITY_DIP_SIZED_VERSIONS:
        # v3／v4 的組數另計（待人決定第 7 項已選 B）：不以 agent_swap.max_swaps_per_run 為上限，只要求落在結構性的合理範圍內。
        if "max_swaps_per_run" in gate and not _int_in(gate["max_swaps_per_run"], 0, QUALITY_DIP_V3_MAX_SWAPS):
            errors.append("%s.max_swaps_per_run 必須是 0～%d 的整數（v3 的組數另計，不占 agent_swap.max_swaps_per_run）" % (
                prefix, QUALITY_DIP_V3_MAX_SWAPS))
    elif "max_swaps_per_run" in gate and not _int_in(gate["max_swaps_per_run"], 0, limit):
        errors.append("%s.max_swaps_per_run 必須是 0～agent_swap.max_swaps_per_run 的整數（不放寬現行上限）" % prefix)
    entry = _ratio(gate["max_entry_weight"], "%s.max_entry_weight" % prefix, errors) if "max_entry_weight" in gate else None
    if "min_new_conviction" in gate and gate["min_new_conviction"] not in QUALITY_DIP_CONVICTIONS:
        errors.append("%s.min_new_conviction 只允許 %s" % (prefix, "／".join(QUALITY_DIP_CONVICTIONS)))
    if "min_holding_days" in gate and not _int_in(gate["min_holding_days"], 0, QUALITY_DIP_MAX_HOLDING_DAYS):
        errors.append("%s.min_holding_days 必須是 0～%d 的整數" % (prefix, QUALITY_DIP_MAX_HOLDING_DAYS))
    if "min_turn_assessment" in gate:
        # 只有 v4（且有 ladder）才可填 no_turn_evidence：沒有轉向跡象也能抄底，由 ladder 的回檔深度門檻把關；v1～v3 必須有轉向跡象。
        allowed_turns = TURN_ASSESSMENTS if version == QUALITY_DIP_SWAP_V4 and isinstance(gate.get("ladder"), Mapping) else MIN_TURN_ASSESSMENTS
        if gate["min_turn_assessment"] not in allowed_turns:
            errors.append("%s.min_turn_assessment 只允許 %s%s" % (
                prefix, "／".join(allowed_turns),
                "（必須有轉向跡象；no_turn_evidence 只有 v4 且有 ladder 才允許）" if allowed_turns == MIN_TURN_ASSESSMENTS else ""))
    if "turn_assessment" in gate:
        if version not in QUALITY_DIP_TURN_BOUND_VERSIONS:
            errors.append("%s.turn_assessment 只用於 version=v2／v3／v4（由 pipeline 綁定風險 Agent 的 turn_assessment）" % prefix)
        elif gate["turn_assessment"] not in TURN_ASSESSMENTS:
            errors.append("%s.turn_assessment 只允許 %s" % (prefix, "／".join(TURN_ASSESSMENTS)))
    _validate_ruling_binding(gate, version, prefix, errors)
    _validate_ladder(gate, version, entry, prefix, errors)
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


def _validate_ladder(
    gate: Mapping[str, object], version: object, entry: Optional[Decimal], prefix: str, errors: List[str],
) -> None:
    """v4 的 ``ladder``（人填、全部必填、不給預設）與 ``ladder_state``（只能由 pipeline 綁定）。v1～v3 出現任一個即拒絕。"""
    ladder, state = gate.get("ladder"), gate.get("ladder_state")
    if version != QUALITY_DIP_SWAP_V4:
        if ladder is not None or state is not None or "ladder_entries" in gate:
            errors.append("%s.ladder／ladder_state／ladder_entries 只用於 version=v4（ladder 由人填，其餘只能由 pipeline 綁定）" % prefix)
        return
    _validate_ladder_entries(gate, ladder, prefix, errors)
    label = "%s.ladder" % prefix
    if not isinstance(ladder, Mapping):
        errors.append("%s 必須是物件（v4 必填；全部數字由人設定，程式不提供預設值）" % label)
    else:
        reject_unknown_fields(ladder, LADDER_FIELDS | LADDER_OPTIONAL_FIELDS, label, errors)
        if "exposure_basis" in ladder and ladder["exposure_basis"] not in LADDER_EXPOSURE_BASES:
            errors.append("%s.exposure_basis 只允許 %s（缺席＝照舊計所有新建倉）" % (label, "／".join(LADDER_EXPOSURE_BASES)))
        missing = sorted(LADDER_FIELDS - set(ladder))
        if missing:
            errors.append("%s 缺少必要欄位（由人設定，程式不提供預設值）：%s" % (label, "、".join(missing)))
        ratios = {name: _ratio(ladder[name], "%s.%s" % (label, name), errors) for name in LADDER_RATIO_FIELDS if name in ladder}
        for name, (low, high) in LADDER_INT_FIELDS.items():
            if name in ladder and not _int_in(ladder[name], low, high):
                errors.append("%s.%s 必須是 %d～%d 的整數" % (label, name, low, high))
        exposure = ratios.get("max_dip_exposure")
        if exposure is not None and entry is not None and exposure < entry:
            errors.append("%s.max_dip_exposure 不得低於 quality_dip.max_entry_weight（總抄底曝險至少要容得下一檔）" % label)
    if state is None:
        return
    slabel = "%s.ladder_state" % prefix
    if not isinstance(state, Mapping):
        errors.append("%s 必須是物件（只能由 pipeline 綁定）" % slabel)
        return
    reject_unknown_fields(state, LADDER_STATE_FIELDS, slabel, errors)
    if state.get("status") not in LADDER_STATUSES:
        errors.append("%s.status 只允許 %s" % (slabel, "／".join(LADDER_STATUSES)))
    elif state["status"] == "open" and state.get("reason") is not None:
        errors.append("%s.status 為 open 時 reason 必須是 null" % slabel)
    elif state["status"] == "closed" and state.get("reason") not in LADDER_CLOSED_REASONS:
        errors.append("%s.reason 只允許 %s" % (slabel, "／".join(LADDER_CLOSED_REASONS)))


def _validate_ladder_entries(gate: Mapping[str, object], ladder: object, prefix: str, errors: List[str]) -> None:
    """``ladder_entries``（只能由 pipeline 綁定）：只用於 ``ladder.exposure_basis=quality_dip_fills``；清單（每筆 symbol、entry_date、run_id）
    或 null（決策庫讀不到，梯度會以 entries_unavailable 關閉）。與持股、帳本日期的一致性由 ``build_ladder_state`` 與 Finalizer 檢查。"""
    if "ladder_entries" not in gate:
        return
    label = "%s.ladder_entries" % prefix
    if not (isinstance(ladder, Mapping) and ladder.get("exposure_basis") == LADDER_EXPOSURE_BASIS_FILLS):
        errors.append("%s 只用於 ladder.exposure_basis=%s（只能由 pipeline 綁定）" % (label, LADDER_EXPOSURE_BASIS_FILLS))
        return
    entries = gate["ladder_entries"]
    if entries is None:
        return
    if not isinstance(entries, list):
        errors.append("%s 必須是清單或 null（null＝決策庫讀不到）" % label)
        return
    seen = set()
    for index, entry in enumerate(entries):
        item = "%s[%d]" % (label, index)
        if not isinstance(entry, Mapping) or set(entry) != LADDER_ENTRY_FIELDS or not all(isinstance(v, str) and v for v in entry.values()):
            errors.append("%s 欄位必須剛好是 symbol、entry_date、run_id 的非空字串" % item)
            continue
        try:
            date.fromisoformat(entry["entry_date"])
        except ValueError:
            errors.append("%s.entry_date 必須是 YYYY-MM-DD" % item)
        if entry["symbol"] in seen:
            errors.append("%s.symbol 重複：%s" % (item, entry["symbol"]))
        seen.add(entry["symbol"])


def _validate_ruling_binding(gate: Mapping[str, object], version: object, prefix: str, errors: List[str]) -> None:
    """v3 的 ``ruling_ref``／``rulings``（只能由 pipeline 綁定）：兩者一起出現、只用於 v3、形狀固定；狀態不是 completed 時 rulings 必須為空。"""
    ref, rulings = gate.get("ruling_ref"), gate.get("rulings")
    if ref is None and rulings is None:
        return
    if version not in QUALITY_DIP_SIZED_VERSIONS:
        errors.append("%s.ruling_ref／rulings 只用於 version=v3／v4（只能由 pipeline 綁定）" % prefix)
        return
    if ref is None or rulings is None:
        errors.append("%s.ruling_ref 與 rulings 必須一起出現（只能由 pipeline 綁定）" % prefix)
        return
    status = None
    if not isinstance(ref, Mapping) or set(ref) != RULING_REF_FIELDS or not all(isinstance(v, str) and v for v in ref.values()):
        errors.append("%s.ruling_ref 欄位必須是 ruling_id、sha256、status 的非空字串" % prefix)
    elif ref["status"] not in RULING_STATUSES:
        errors.append("%s.ruling_ref.status 只允許 %s" % (prefix, "／".join(RULING_STATUSES)))
    else:
        status = ref["status"]
    if not isinstance(rulings, list) or not all(item in RULINGS for item in rulings):
        errors.append("%s.rulings 必須是 %s 的清單（與提名中 quality_dip 配對同序）" % (prefix, "／".join(RULINGS)))
    elif status is not None and status != "completed" and rulings:
        errors.append("%s.ruling_ref.status 不是 completed 時 rulings 必須為空" % prefix)


def bind_quality_dip_turn(swap: Mapping[str, object], sizing: Mapping[str, object], cash_stance: Mapping[str, object]) -> Dict[str, object]:
    """``apply_trade_decision`` 用：v2／v3／v4 把風險 Agent 的 ``turn_assessment`` 綁進 ``agent_swap.quality_dip.turn_assessment``。

    ``swap`` 是目前的 agent_swap 物件（可能已綁 exposure view）；v1（影子）與沒有閘門回傳拷貝、不綁定。"""
    result = dict(swap)
    gate = quality_dip_swap_gate(sizing)
    if gate is None or gate.get("version") not in QUALITY_DIP_TURN_BOUND_VERSIONS:
        return result
    turn = cash_stance.get("turn_assessment")
    if turn not in TURN_ASSESSMENTS:
        raise DecisionToolError("Policy 啟用 agent_swap.quality_dip v2／v3／v4，現金姿態必須給 turn_assessment（%s）" % "／".join(TURN_ASSESSMENTS))
    result["quality_dip"] = {**dict(gate), "turn_assessment": turn}
    return result


def bind_quality_dip_ladder_state(swap: Mapping[str, object], state: Mapping[str, object]) -> Dict[str, object]:
    """``apply_trade_decision`` 用：v4 把程式算的梯度狀態（``decline_depth.build_ladder_state``）綁進 ``agent_swap.quality_dip.ladder_state``。
    樣板預填即拒絕（只能由程式綁定）；沒有 v4 或沒有 quality_dip 回傳拷貝。"""
    result = dict(swap)
    gate = result.get("quality_dip")
    if not isinstance(gate, Mapping) or gate.get("version") != QUALITY_DIP_SWAP_V4:
        return result
    if "ladder_state" in gate:
        raise DecisionToolError("agent_swap.quality_dip.ladder_state 只能由程式綁定，樣板不得預填")
    result["quality_dip"] = {**dict(gate), "ladder_state": dict(state)}
    return result


def bind_quality_dip_ladder_entries(
    policy: Mapping[str, object], entries: Optional[Sequence[Mapping[str, str]]],
) -> Dict[str, object]:
    """pipeline 用（做法同 ``bind_reentry_cooldown``）：``ladder.exposure_basis=quality_dip_fills`` 時把 quality_dip 成交清單
    綁進 ``agent_swap.quality_dip.ladder_entries``（``None``＝決策庫讀不到，梯度 fail-closed）。樣板預填即拒絕；沒有該模式原樣回傳拷貝。
    呼叫端須重算 content_sha256。"""
    bound = copy.deepcopy(dict(policy))
    sizing = bound.get("position_sizing")
    if not ladder_counts_quality_dip_fills_only(sizing):
        return bound
    gate = sizing["agent_swap"]["quality_dip"]  # type: ignore[index]
    if "ladder_entries" in gate:
        raise DecisionToolError("agent_swap.quality_dip.ladder_entries 只能由程式綁定，樣板不得預填")
    gate["ladder_entries"] = None if entries is None else [dict(entry) for entry in entries]
    return bound


def quality_dip_swap_binding(sizing: object) -> Optional[Dict[str, object]]:
    """配置用的綁定。沒有閘門或 agent_swap 不是 v2（配置不讀提名）回 None。

    回傳 ``{"version", "purposes"}``（``purposes`` 與 ``nominated_pairs`` 同序）；v2／v3 另有 ``turn_assessment``、``min_turn``、``cap``、
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
        raise DecisionToolError("DecisionPolicy 啟用 agent_swap.quality_dip v2／v3／v4，但尚未綁定風險 Agent 的 turn_assessment")
    oversold = gate.get("oversold")
    binding: Dict[str, object] = {
        "version": str(gate.get("version")), "purposes": purposes, "turn_assessment": str(turn),
        "min_turn": str(gate["min_turn_assessment"]), "cap": int(gate["max_swaps_per_run"]),  # type: ignore[arg-type]
        "entry_weight": decimal_value(gate["max_entry_weight"], "max_entry_weight"),
        "oversold": None if not isinstance(oversold, Mapping) else {
            "cap": int(oversold["max_swaps_per_run"]),  # type: ignore[arg-type]
            "entry_weight": decimal_value(oversold["max_entry_weight"], "oversold.max_entry_weight"),
        },
    }
    binding["sized"] = gate.get("version") in QUALITY_DIP_SIZED_VERSIONS
    if binding["sized"]:
        binding.update(_v3_binding(gate, pairs, purposes))
    if gate.get("version") == QUALITY_DIP_SWAP_V4:
        state = gate.get("ladder_state")
        if not isinstance(state, Mapping) or state.get("status") not in LADDER_STATUSES:
            raise DecisionToolError("DecisionPolicy 啟用 agent_swap.quality_dip v4，但尚未綁定梯度狀態 ladder_state")
        binding["ladder_state"] = dict(state)
    return binding


def _v3_binding(gate: Mapping[str, object], pairs: Sequence[Mapping[str, object]], purposes: Sequence[str]) -> Dict[str, object]:
    """v3 另有：每對的份量格 ``sizes``（非 quality_dip 為 None）、裁決狀態 ``ruling_status`` 與每對的裁決 ``rulings``（與 pairs 同序，
    只有 quality_dip 配對且狀態為 completed 才有值）。沒有綁定裁決或內容與提名不一致一律拋錯，不靜默當成通過。"""
    ref, rulings = gate.get("ruling_ref"), gate.get("rulings")
    if not isinstance(ref, Mapping) or not isinstance(rulings, list):
        raise DecisionToolError("DecisionPolicy 啟用 agent_swap.quality_dip v3，但尚未綁定風險 Agent 的裁決（ruling_ref／rulings）")
    status = str(ref.get("status"))
    quality_index = [index for index, purpose in enumerate(purposes) if purpose == PURPOSE_QUALITY_DIP]
    sizes: List[Optional[str]] = []
    for pair, purpose in zip(pairs, purposes):
        size = pair.get("size")
        if purpose == PURPOSE_QUALITY_DIP and size not in SIZE_GRADES:
            raise DecisionToolError("agent_swap.quality_dip v3 的 quality_dip 配對必須有份量格 size（%s）" % "／".join(SIZE_GRADES))
        sizes.append(str(size) if purpose == PURPOSE_QUALITY_DIP else None)
    if status == "not_required" and quality_index:
        raise DecisionToolError("裁決狀態為 not_required，但提名含 quality_dip 配對")
    by_pair: List[Optional[str]] = [None] * len(pairs)
    if status == "completed":
        if len(rulings) != len(quality_index):
            raise DecisionToolError("裁決筆數與提名中的 quality_dip 配對數不一致")
        for index, ruling in zip(quality_index, rulings):
            by_pair[index] = str(ruling)
    elif rulings:
        raise DecisionToolError("裁決狀態不是 completed 時 rulings 必須為空")
    return {"sizes": sizes, "ruling_status": status, "rulings": by_pair}


def turn_allows(binding: Mapping[str, object]) -> bool:
    """風險 Agent 的 turn_assessment 不低於閘門的 min_turn_assessment（no_turn_evidence 恆不通過）。"""
    turn, minimum = str(binding["turn_assessment"]), str(binding["min_turn"])
    if minimum == TURN_ASSESSMENTS[0]:
        # 只有 v4（有 ladder）才可能填 no_turn_evidence：任何合法的 turn_assessment 都不低於它；回檔深度由梯度另行把關。
        return turn in TURN_ASSESSMENTS
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
    "PURPOSE_QUALITY_DIP", "QUALITY_DIP_BUY_REASONS", "QUALITY_DIP_SWAP_V3", "QUALITY_DIP_SWAP_V4", "QUALITY_DIP_SWAP_NOTE_V4", "QUALITY_DIP_SIZED_VERSIONS",
    "LADDER_STATUSES", "bind_quality_dip_ladder_entries", "bind_quality_dip_ladder_state", "ladder_counts_quality_dip_fills_only", "quality_dip_ladder_gate", "RULINGS", "RULING_STATUSES", "SIZE_GRADES",
    "program_skip_reason", "quality_dip_v3_gate", "size_after_ruling", "size_entry_weight", "QUALITY_DIP_SELL_REASONS", "QUALITY_DIP_SWAP_NOTE", "QUALITY_DIP_SWAP_V1",
    "QUALITY_DIP_SWAP_V2", "bind_quality_dip_turn", "buy_reasons", "claim_kinds", "nomination_purposes", "non_price_bull_ids",
    "oversold_symbols", "quality_dip_swap_binding", "quality_dip_swap_gate", "sell_reasons", "turn_allows",
    "validate_quality_dip_pair", "validate_quality_dip_swap_gate",
]
