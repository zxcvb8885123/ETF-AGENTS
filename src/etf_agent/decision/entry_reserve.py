"""換入者先保留預算：``position_sizing.agent_swap.entry_reserve`` v1（版本閘門，只由人設定；策略反思不得改，``agent_swap`` 前綴已在禁區）。

設計見 ``docs/agent_led_offense_plan.md`` 4.6、4.11、4.12 與 ``docs/agent_led_sizing_plan.md`` 4.2、4.5。起因：2026-11-11 的 run，交易 Agent 提名兩組換股
（買 2408／6239、賣 2890／3034），換出者照賣，兩檔換入者卻因信心預算被高信心加碼先用掉而買不到一張，賣款流向 2330。

- 每一組提名必填列舉 ``size``（small／medium／large，只有列舉、不輸出數字）。份量格對成權重上限沿用 quality_dip v3 的程式常數：
  large＝人填的 ``max_entry_weight``、medium＝2/3、small＝1/3（``quality_dip_swap.size_entry_weight``）。
- 換入買進股數取最小，只縮不放：份量格權重 × NAV、該組換出者賣款淨額加「分到的格點以上現金」、單股上限餘裕、產業上限餘裕。
  quality_dip 配對仍以自己的硬上限與裁決後的份量格為權重，且買進不超過換出賣款（不吃格點以上現金），這是它既有的人設界線。
- 換入者的買進先保留，再跑一般的信心預算；換入者不再參與信心預算。
- 縮到不足一張：整組取消、換出者不賣（``AGENT_SWAP_CANCELLED:entry_reserve_lot``）。依清單先後累計換手，會超過換手上限的組從該組起取消
  （``entry_reserve_turnover``）。有 ``size`` 的組不適用 ``proceeds_to_add``：換入者沒買到就整組取消。換入者買進後剩下的賣款（例如換出者市值大於份量格）回到一般預算，可能流向其他買單。
- ``max_nominations``：提名清單長度上限（不含 quality_dip 配對，它們另有自己的名額），可以大於 ``agent_swap.max_swaps_per_run``；
  超過成交上限的組從清單尾端取消（既有的 ``swap_cap``）。

只有版本欄位與兩個人填的數字；Agent 的 ``size`` 放在提名封存內（``team_inputs.swap_nomination``），Policy 的 ``nominated_pairs`` 每組綁 ``size``，
Finalizer 由提名重算比對。不升 ``team_inputs`` 線性版本、沒有新鍵（見計畫 4.9）。沒有此欄位的舊 Policy／舊提名封存／舊 run 逐位元維持原行為。
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Mapping, Optional

from .fields import DecisionToolError, decimal_value, reject_unknown_fields
from .quality_dip_swap import SIZE_GRADES, size_entry_weight

ENTRY_RESERVE_V1 = "v1"
ENTRY_RESERVE_FIELDS = {"version", "max_entry_weight", "max_nominations"}
# 選配子欄位 ``swap_priority``（只有 {"version": "v1"}、沒有人填數字）：沒有它的舊 Policy／舊 run 逐位元維持原行為。
SWAP_PRIORITY_V1 = "v1"
ENTRY_RESERVE_OPTIONAL_FIELDS = {"swap_priority"}
ENTRY_RESERVE_MAX_NOMINATIONS = 20
ENTRY_RESERVE_NOTE = (
    "每一組換股都要填份量格 size（small 試單、medium 一般、large 把握最高；只有列舉、不輸出任何百分比）。程式把份量格對成買進權重上限，"
    "再取「該組換出者賣款淨額加分到的格點以上現金」與單股、產業上限餘裕中最小的一個；換入者的買進先保留預算，不會再被信心預算先用掉；"
    "縮到不足一張時整組取消、換出者不賣；超出份量格所需的賣款回到一般預算（可能流向其他買單）。清單可以比實際成交組數長，超過成交上限的組從清單尾端取消，"
    "所以把最想換的放前面。"
)
# 只在 position_sizing.offense.thesis_size_cap 開啟時附加（沒開時 ENTRY_RESERVE_NOTE 逐位元不變）。
THESIS_SIZE_CAP_NOTE = (
    "另外：風險 Agent 的有效等級放寬防守姿態的現金目標時，purpose=thesis 的配對份量格也受該級 max_size 限制（你填的 size 較大時由程式縮到上限，只縮不取消）；"
    "這只限制份量格，不限制動用放寬的現金。"
)


def entry_reserve_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """``agent_swap.entry_reserve``；沒有（或 agent_swap 不是 v2，配置不讀提名）回 None。"""
    swap = sizing.get("agent_swap") if isinstance(sizing, Mapping) else None
    gate = swap.get("entry_reserve") if isinstance(swap, Mapping) else None
    if not isinstance(gate, Mapping) or swap.get("version") != "v2":  # type: ignore[union-attr]
        return None
    return gate


def swap_priority_enabled(sizing: object) -> bool:
    """``agent_swap.entry_reserve.swap_priority`` v1：（1）換股試算的名額保護只計「分到至少一張」的新股（零權重回收名額者不擋換股）；
    （2）``turnover_fit`` 縮買單時，已保留預算的換股換入者排在其他買單之後才縮。只改程式內的優先順序，不改寫交易 Agent 的意圖。"""
    gate = entry_reserve_gate(sizing)
    value = gate.get("swap_priority") if gate is not None else None
    return isinstance(value, Mapping) and value.get("version") == SWAP_PRIORITY_V1


def validate_entry_reserve_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """形狀、值域與前提：agent_swap v2、agent_swap.exposure（purpose 的宿主）、quality_dip 若有只能是 v3／v4（份量格語意一致）。"""
    swap = sizing.get("agent_swap")
    prefix = "position_sizing.agent_swap.entry_reserve"
    if not isinstance(swap, Mapping) or "entry_reserve" not in swap:
        return
    gate = swap["entry_reserve"]
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, ENTRY_RESERVE_FIELDS | ENTRY_RESERVE_OPTIONAL_FIELDS, prefix, errors)
    missing = sorted(ENTRY_RESERVE_FIELDS - set(gate))
    if missing:
        errors.append("%s 缺少必要欄位（由人設定，程式不提供預設值）：%s" % (prefix, "、".join(missing)))
    if gate.get("version") != ENTRY_RESERVE_V1:
        errors.append("%s.version 只允許 %s" % (prefix, ENTRY_RESERVE_V1))
    if "swap_priority" in gate:
        priority = gate["swap_priority"]
        if not isinstance(priority, Mapping):
            errors.append("%s.swap_priority 必須是物件" % prefix)
        else:
            reject_unknown_fields(priority, {"version"}, prefix + ".swap_priority", errors)
            if priority.get("version") != SWAP_PRIORITY_V1:
                errors.append("%s.swap_priority.version 只允許 %s" % (prefix, SWAP_PRIORITY_V1))
    if swap.get("version") != "v2":
        errors.append("%s 只允許用於 agent_swap.version=v2（配置才讀提名）" % prefix)
    if not isinstance(swap.get("exposure"), Mapping):
        errors.append("%s 需要 agent_swap.exposure（每對的 purpose 與曝險簡報由它提供）" % prefix)
    quality = swap.get("quality_dip")
    if isinstance(quality, Mapping) and quality.get("version") not in ("v3", "v4"):
        errors.append("%s 與 agent_swap.quality_dip 並存時，quality_dip 必須是 v3／v4（份量格與裁決語意一致）" % prefix)
    if "max_entry_weight" in gate:
        raw = gate["max_entry_weight"]
        if isinstance(raw, bool) or not isinstance(raw, str):
            errors.append("%s.max_entry_weight 必須是數字字串" % prefix)
        else:
            try:
                value = decimal_value(raw, "max_entry_weight")
            except DecisionToolError as error:
                errors.append("%s.max_entry_weight 無效：%s" % (prefix, error))
            else:
                if value <= 0 or value >= 1:
                    errors.append("%s.max_entry_weight 必須大於 0 且小於 1" % prefix)
    nominations = gate.get("max_nominations")
    if "max_nominations" in gate:
        if isinstance(nominations, bool) or not isinstance(nominations, int) or not 1 <= nominations <= ENTRY_RESERVE_MAX_NOMINATIONS:
            errors.append("%s.max_nominations 必須是 1～%d 的整數" % (prefix, ENTRY_RESERVE_MAX_NOMINATIONS))
        else:
            fills = swap.get("max_swaps_per_run")
            if isinstance(fills, int) and not isinstance(fills, bool) and nominations < fills:
                errors.append("%s.max_nominations 不得小於 agent_swap.max_swaps_per_run（清單至少要容得下成交上限）" % prefix)


def validate_entry_reserve_limits(policy: Mapping[str, object], errors: List[str]) -> None:
    """需要 Policy 的檢查：max_entry_weight 不得高於單股上限（硬規則 10%，含 special_weight_limits 的最大值以外的一般上限）。"""
    sizing = policy.get("position_sizing")
    gate = entry_reserve_gate(sizing)
    if gate is None or not isinstance(gate.get("max_entry_weight"), str):
        return
    try:
        weight = decimal_value(gate["max_entry_weight"], "entry_reserve.max_entry_weight")
        limit = decimal_value(policy.get("max_stock_weight"), "max_stock_weight")
    except DecisionToolError:
        return
    if weight > limit:
        errors.append("position_sizing.agent_swap.entry_reserve.max_entry_weight 不得高於 max_stock_weight（單股上限）")


def pair_sizes(sizing: object) -> dict:
    """Policy 綁定的每組份量格 ``{(換入, 換出): size}``（只讀 nominated_pairs；沒有閘門回空）。缺少或不是合法列舉一律拋錯，fail-closed。"""
    gate = entry_reserve_gate(sizing)
    if gate is None:
        return {}
    pairs = sizing["agent_swap"].get("nominated_pairs")  # type: ignore[index]
    if not isinstance(pairs, list):
        raise DecisionToolError("DecisionPolicy 啟用 agent_swap.entry_reserve，但尚未綁定換股提名（nominated_pairs）")
    result = {}
    for pair in pairs:
        size = pair.get("size") if isinstance(pair, Mapping) else None
        if size not in SIZE_GRADES:
            raise DecisionToolError("agent_swap.entry_reserve 啟用時 nominated_pairs 每組必須有份量格 size（%s）" % "／".join(SIZE_GRADES))
        result[(str(pair["buy_symbol"]), str(pair["sell_symbol"]))] = str(size)
    return result


def reserve_weight(gate: Mapping[str, object], size: str) -> Decimal:
    """份量格對成的買進權重上限：人填的 max_entry_weight × 固定比例（large 1、medium 2/3、small 1/3）。"""
    return size_entry_weight(decimal_value(gate["max_entry_weight"], "entry_reserve.max_entry_weight"), size)
