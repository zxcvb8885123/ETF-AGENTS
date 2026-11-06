"""Conviction tiers, cash stance and their deterministic conversion into weights.

交易 Agent 只對 buy／add 候選給出 ``high``／``medium``／``low`` 等級，風險 Agent 只給
現金姿態與持股廣度；權重由本模組依等級乘數與 ATR 波動度確定性計算，Agent 不輸出任何權重、
股數或金額。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.core import untrusted_value

from .contracts import (
    DecisionContext,
    DecisionToolError,
    decimal_value,
    reject_unknown_fields,
    required_string,
    string_list,
)


SIZING_METHOD = "conviction_volatility_v1"
# 2026-10-28 後新增 v2：以 volatility_exponent 控制低波動偏好的強度（0 為完全不看波動度）。
# v1 的語意與程式路徑完全不動，舊 Decision run 仍以原式重建。
SIZING_METHOD_V2 = "conviction_volatility_v2"
SIZING_METHODS = (SIZING_METHOD, SIZING_METHOD_V2)
# 同等級同支持數的入選順序；省略欄位即沿用 atr_low（低波動優先）。
SELECTION_TIEBREAKS = ("atr_low", "symbol", "momentum_high")
# 2026-10-19 後新增的單股硬上限整張減碼；省略此欄位的舊 Decision run 維持原重建行為。
STOCK_WEIGHT_FIT = "v1"
# 2026-10-28 後新增：持股滿檔時未持有的新股不佔名額，已持有的 add 照常入選。
# 省略此欄位的舊 Decision run 維持新股可佔滿檔名額的原重建行為。
HELD_ADD_PRIORITY = "v1"
SELECTION_RANKING = "v1"
# 2026-11-02 後新增：現金已低於姿態緩衝時，買單可動用本次賣出收入（現金不再往下掉），
# 不被緩衝整筆砍掉。省略此欄位的舊 Decision run 維持緩衝一律生效的原重建行為。
CASH_BUFFER_FLOOR = "v1"
# 2026-10-28 後新增：滿檔時本次出場釋出的名額可以給未持有的新股（換股）。省略 rotation 的
# Decision run 維持「滿檔完全不進新股」的原重建行為；名額配對全部由程式計算，Agent 不參與。
ROTATION_VERSION = "v1"
ROTATION_FIELDS = {"version", "max_swaps_per_run", "min_new_conviction", "forced_swap"}
# 2026-11-02 後新增：滿檔又沒人出場時，信心夠高的未持有新股可由程式換掉「每週論點檢視判定理由被推翻（broken）」
# 且交易 Agent 標 hold 的續抱持股（forced_swap）。名單由每日 pipeline 綁進 Policy（broken_symbols＋verdicts_ref），
# 事後重建只讀 Policy，不讀會變動的 var/ 檔；沒有名單就不換。
# 省略此欄位的 Policy／舊 Decision run 維持「只有交易 Agent 標 exit 才釋出名額」的原重建行為。
FORCED_SWAP_VERSION = "v1"
FORCED_SWAP_FIELDS = {"version", "min_new_conviction", "max_swaps_per_run", "broken_symbols", "verdicts_ref", "confirmation"}
# 2026-11-03 後新增（forced_swap.confirmation 版本閘門）：被換出的持股除了在 broken_symbols 名單內，還必須有本次交易
# Agent 採納的該股空頭 claim（adopted_claim_ids 含 bear- 開頭）；沒有即不換、記 RISK_FORCED_SWAP_UNCONFIRMED。
# 同閘門內：換出數不超過「持股數 − min_positions」與現金能支應的新股數，且被換出者可被 RISK_MIN_POSITIONS 留住。
FORCED_SWAP_CONFIRMATION = "v1"
VERDICTS_REF_FIELDS = {"verdicts_id", "as_of", "sha256"}
CONVICTION_LEVELS = ("high", "medium", "low")
# 現金姿態由積極到防守；對應的現金緩衝由 Policy 設定，必須低於競賽現金上限。
CASH_STANCES = ("aggressive", "neutral", "defensive")
# 持股廣度由集中到分散；風險 Agent 與現金姿態一起給，對應的目標檔數由 Policy 的
# positions_by_breadth 設定。省略此欄位的舊 Decision run 維持填滿 max_positions 的原重建行為。
POSITION_BREADTHS = ("concentrated", "balanced", "diversified")
# 封頂時保留 0.5% 相對緩衝：買進手續費使成交後 NAV 變小，剛好配到上限的部位會以
# 50.0009% 之類的權重觸發 Guard。全額換手時手續費影響約 0.14%，0.5% 足以涵蓋。
LIMIT_HEADROOM = Decimal("0.995")
# 2026-11-03 後新增（stock_limit_headroom 版本閘門）：比賽規則「因加碼導致超限當日即警告，價格波動超限才有
# 5 日寬限」，所以當日有 buy／add 的股票封頂改留 3%（0.97），只續抱的仍用 0.995。
# 省略此欄位的舊 Policy／舊 Decision run 一律 0.995，重建結果不變。
STOCK_LIMIT_HEADROOM = "v1"
LIMIT_HEADROOM_BUY = Decimal("0.97")
# 2026-11-03 後新增（cash_fit_order 版本閘門）：現金不足時買單改依（信心、入選名次、代號）順序取得現金，
# 且殘量低於目標股數的 cash_fit_min_fill_ratio（預設 0.5）就整筆取消（RISK_CASH_FIT），不留塵埃持股；
# 已持有的 add 不受影響、只減少增量。省略此欄位的舊 Decision run 維持依代號逐張減少的原重建行為。
CASH_FIT_ORDER = "v1"
CASH_FIT_MIN_FILL_RATIO = Decimal("0.5")
# 2026-11-06 後新增（high_conviction_cash_priority 版本閘門）：可動用的現金先全部給高信心買單。高信心新股因現金不足
# 買不到目標的 cash_fit_min_fill_ratio 時不再整筆取消，買得到幾張就買幾張；高信心買單被現金縮減後，
# 後面未持有的中、低信心新股不得動用剩下的現金（降為 0，CASH_BUFFER 旗標）；已持有股票的加碼不受影響。
# turnover_fit 縮減也同樣不取消高信心零頭。須與 cash_fit_order 同時啟用。
# 省略此欄位的舊 Policy／舊 Decision run 維持塵埃取消後現金流向後面買單的原重建行為。
HIGH_CONVICTION_CASH_PRIORITY = "v1"
# 2026-11-03 後新增（cash_ceiling_fix 版本閘門）：配置後 base 情境現金比例達競賽上限時，程式依 ATR14 由低到高
# 留住交易 Agent 標為 exit 的持股續抱（RISK_CASH_CEILING，forced_exit 不留），直到低於上限。
# 省略此欄位的舊 Decision run 維持由 Guard CASH_WEIGHT 整份拒絕的原重建行為。
CASH_CEILING_FIX = "v1"
# 2026-11-03 後新增（buy_gap_reserve 版本閘門）：決策用前日收盤、成交用當日均價，買單總額乘 (1 + buy_gap_reserve_rate)
# 計入現金需求（預設 0.03），避免跳空開高讓主辦方整批作廢。省略此欄位的舊 Decision run 不預留。
BUY_GAP_RESERVE = "v1"
BUY_GAP_RESERVE_RATE = Decimal("0.03")
# 2026-11-03 後新增（insufficient_funds_policy 版本閘門）：虛擬帳本模擬成交時，現金不足改為整批作廢
# （與主辦方相同），不再逐張減少。省略此欄位的舊 Decision run 結算維持逐張減少。
INSUFFICIENT_FUNDS_VOID_BATCH = "void_batch"
INSUFFICIENT_FUNDS_POLICIES = (INSUFFICIENT_FUNDS_VOID_BATCH,)
# 2026-11-03 後新增（sector_fit 版本閘門）：產業權重超過 max_sector_weight 時只減當日 buy／add 的增量、不賣既有持股；
# Guard 對「既有已超限且本次沒有買進」的產業容忍（記為 carried）。省略此欄位的舊 run 維持「先減買單、再賣持股」的原重建行為。
SECTOR_FIT = "v1"
# 2026-11-03 後新增（turnover_fit 版本閘門）：換手超過 max_turnover_rate 時由程式依（信心低→高、ATR 高→低）減少買單增量
# 或取消新股（換股數減少），賣單仍超過時依 ATR14 由低到高留住 exit 持股（RISK_TURNOVER）。省略此欄位的舊 run 維持
# 買單依取得現金的順序逐張減少的原重建行為。
TURNOVER_FIT = "v1"
# 2026-11-03 後新增（reentry_cooldown 版本閘門）：決策庫中最近 days 個日曆天內（目標交易時段早於本次）已出場的股票，
# 未持有時不得再買進（RISK_REENTRY_COOLDOWN）。名單由每日 pipeline 綁進 Policy（blocked_symbols＋source_run_ids），
# 事後重建只讀 Policy。省略此欄位的舊 run 不設冷卻。
REENTRY_COOLDOWN_VERSION = "v1"
REENTRY_COOLDOWN_FIELDS = {"version", "days", "blocked_symbols", "source_run_ids"}
# 2026-11-03 後新增（drawdown_derisk 版本閘門）：現金姿態為 defensive 時由程式依 ATR14 由高到低、信心低到高把持股減碼到
# 姿態現金緩衝（RISK_DRAWDOWN，一次最多 NAV × max_derisk_fraction，預設 0.25）；配置預算 ≤ 0 時旗標改為 CASH_BUFFER；
# drawdown_control 另需 release_neutral_below／release_defensive_below 兩個解除門檻（hysteresis）。省略此欄位的舊 run 不減碼。
# v2（2026-11-04，只由人設定）：整張進位不得超過單次減碼硬上限。
# v3：v2 再加「被減碼的持股不再是本次加碼候選」——減碼後的股數是上限，交易 Agent 標 add 的同一檔不得加回
#（v1／v2 會加回原持股，留下 RISK_DRAWDOWN 旗標卻沒有賣單）。v1 維持原重建行為。
DRAWDOWN_DERISK_VERSION = "v1"
DRAWDOWN_DERISK_VERSION_V2 = "v2"
DRAWDOWN_DERISK_VERSION_V3 = "v3"
DRAWDOWN_DERISK_VERSIONS = (DRAWDOWN_DERISK_VERSION, DRAWDOWN_DERISK_VERSION_V2, DRAWDOWN_DERISK_VERSION_V3)
DRAWDOWN_DERISK_FIELDS = {"version", "max_derisk_fraction"}
DRAWDOWN_DERISK_FRACTION = Decimal("0.25")
# 2026-11-05 後新增（high_conviction_cash_floor 版本閘門，只由人設定）：高信心（high）買單可把現金用到
# min(NAV × 姿態緩衝 × floor_ratio, 起始已交割現金)，medium／low 維持 cash_buffer_floor 的 min(NAV × 緩衝, 起始現金)；
# drawdown_derisk 的觸發與目標同改為 NAV × 緩衝 × floor_ratio（帶狀防守，避免隔日來回交易）；當日觸發減碼時不套用
# （減碼收入只補緩衝）。份額預算不變。前提：cash_buffer_floor 與 cash_fit_order 同時啟用、drawdown_derisk（若有）為 v3。
# 省略此欄位的舊 Policy／舊 Decision run 逐位維持原重建行為。
HIGH_CONVICTION_CASH_FLOOR = "v1"
HIGH_CONVICTION_CASH_FLOOR_FIELDS = {"version", "floor_ratio"}
# 2026-11-05 後新增（limit_scaled_conviction 版本閘門，只由人設定）：高信心候選的必要份額為
# max(原式份額, min(alpha × 單股上限, 封頂))，必要份額總和超過預算時依入選名次填到用完；剩餘預算再依原式
# 分給 medium／low（volatility_exponent = 0 時不得高於最小的 high／medium，分不完留作現金），
# 見 docs/limit_aware_conviction_weights_plan.md「α 方案」。alpha 為必要欄位（不在程式寫死預設值）。
# 前提：method 為 conviction_volatility_v2 且已啟用 selection_ranking。省略此欄位的舊 run 走原式。
LIMIT_SCALED_CONVICTION = "v1"
LIMIT_SCALED_CONVICTION_FIELDS = {"version", "alpha"}
# 2026-11-05 後新增（cash_ratio_range 版本閘門 v1，只由人設定；docs/agent_cash_ratio_range_plan.md 第一階段 C2）：
# 風險 Agent 仍只輸出列舉，在姿態內另選子等級 cash_grade（lower／middle／upper），現金比例由 grid 對應固定百分比，
# 取代 cash_buffer_by_stance 的三段階梯（後者仍必填、驗證不變，只供舊 run 回放與對照）。單日只限制現金拉高：
# 今日可選格點 ≤ max(該姿態 lower, 前次比例 anchor + max_daily_raise)，往下不限。anchor 由 pipeline 依前次決策挑選規則
# 綁進 Policy（沒有前次 run 時為本次起始現金比例，見 cash_ratio.py）。超出即拒絕，程式不夾到邊界。
# 不得與 high_conviction_cash_floor 同時開啟；有 drawdown_derisk 時須 v3。省略此欄位的舊 Policy／舊 run 逐位元不變。
CASH_RATIO_RANGE = "v1"
CASH_RATIO_RANGE_FIELDS = {"version", "grid", "max_daily_raise", "anchor"}
CASH_GRADES = ("lower", "middle", "upper")
# 人決定的防守上限 22%（跌 10% 情境現金仍離 25% 競賽上限有餘裕；與策略反思 cash_buffer_by_stance 旋鈕上限一致）。
CASH_GRID_MAX = Decimal("0.22")
MAX_DAILY_RAISE_MIN = Decimal("0.01")
MAX_DAILY_RAISE_MAX = Decimal("0.10")
CASH_ANCHOR_STATUSES = ("prior_decision", "account_cash")
CASH_ANCHOR_FIELDS = {"status", "ratio", "source"}
CASH_ANCHOR_SOURCE_FIELDS = {
    "run_id", "selection", "target_session_start", "decision_sha256", "proposal_id", "proposal_sha256",
}
_TWO_DECIMALS = re.compile(r"^0\.[0-9]{2}$")
_ANCHOR_RATIO = re.compile(r"^[01]\.[0-9]{8}$")


def validate_cash_stance(
    context: DecisionContext,
    stance: object,
    prefix: str,
    errors: List[str],
    cash_ratio_range: Optional[Mapping[str, object]] = None,
    prior_context: Optional[Mapping[str, object]] = None,
) -> None:
    """現金姿態：level 三選一、理由與共同輸入中存在的證據。

    舊 bundle（無 ``evidence_policy``）維持「市場層級可引用任一股票」的原行為；``typed_evidence_v1`` 下
    至少一個證據必須是市場層級（全市場情緒／恐懼貪婪／隔夜／市場狀態編號），個股行情不得單獨成立，
    且全市場情緒為 negative 時不得回 aggressive（一致性護欄；回撤下限另由 stance_floor 強制）。

    ``cash_ratio_range`` 是已綁定 anchor 的版本閘門物件；為 None 時欄位白名單與舊版完全相同（出現 cash_grade 即拒絕）。
    有閘門時 ``cash_grade`` 必填，且必須在該姿態今日可選的格點內；超出即拒絕、不夾到邊界。
    ``prior_context`` 是本次的前次決策背景（PriorDecisionContext）；有閘門時其前次 decision_id 也列入理由的禁用識別碼。"""
    if not isinstance(stance, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    fields = {"level", "breadth", "rationale", "evidence_ids"}
    if cash_ratio_range is not None:
        fields = fields | {"cash_grade"}
        _validate_cash_grade(cash_ratio_range, stance, prefix, errors, prior_context)
    reject_unknown_fields(stance, fields, prefix, errors)
    if stance.get("level") not in CASH_STANCES:
        errors.append("%s.level 必須是 %s" % (prefix, "／".join(CASH_STANCES)))
    if "breadth" in stance and stance["breadth"] not in POSITION_BREADTHS:
        errors.append("%s.breadth 必須是 %s" % (prefix, "／".join(POSITION_BREADTHS)))
    try:
        required_string(stance, "rationale")
        evidence = string_list(stance, "evidence_ids")
    except DecisionToolError as error:
        errors.append("%s.%s" % (prefix, error))
        return
    if not evidence:
        errors.append("%s.evidence_ids 不得為空" % prefix)
    unknown = [item for item in evidence if item not in context.evidence_symbols]
    if unknown:
        errors.append("%s 引用不存在：%s" % (prefix, ", ".join(untrusted_value(item) for item in unknown)))
    if context.evidence_policy is None:
        return
    if evidence and not set(evidence) & context.market_evidence_ids:
        errors.append(
            "%s 至少要引用一個市場層級證據（market_level_evidence_ids：全市場情緒、恐懼貪婪成分、隔夜市場或市場狀態編號）；"
            "個股行情證據只能作補充" % prefix
        )
    sentiment = context.market_sentiment or {}
    if (stance.get("level") == "aggressive" and sentiment.get("status") == "available"
            and sentiment.get("outlook") == "negative"):
        errors.append(
            "%s 全市場情緒為 negative 時，aggressive 未通過一致性護欄；請改為 neutral 或 defensive，"
            "並在 rationale 說明判斷依據" % prefix
        )


def _validate_volatility_exponent(sizing: Mapping[str, object], errors: List[str]) -> None:
    """v2 必須給 0～1 的波動度指數；v1 不得出現此欄位，確保舊 run 走原式重建。"""
    present = "volatility_exponent" in sizing
    if sizing.get("method") == SIZING_METHOD_V2:
        if not present:
            errors.append("position_sizing.volatility_exponent 是 %s 的必要欄位" % SIZING_METHOD_V2)
            return
    elif present:
        errors.append(
            "position_sizing.volatility_exponent 只允許用於 %s；%s 必須省略"
            % (SIZING_METHOD_V2, SIZING_METHOD)
        )
        return
    if not present:
        return
    try:
        exponent = decimal_value(sizing["volatility_exponent"], "position_sizing.volatility_exponent")
    except DecisionToolError as error:
        errors.append(str(error))
        return
    if exponent < 0 or exponent > 1:
        errors.append("position_sizing.volatility_exponent 必須介於 0 與 1（含）")


def _validate_execution_gates(sizing: Mapping[str, object], errors: List[str]) -> None:
    """2026-11-03 新增的執行面版本閘門：值只接受已知版本，比例參數必須與對應閘門同時出現。

    這些欄位只由人設定、策略反思不得改；省略即走舊 Decision run 的原重建路徑。"""
    for field, allowed in (
        ("cash_fit_order", CASH_FIT_ORDER),
        ("high_conviction_cash_priority", HIGH_CONVICTION_CASH_PRIORITY),
        ("cash_ceiling_fix", CASH_CEILING_FIX),
        ("stock_limit_headroom", STOCK_LIMIT_HEADROOM),
        ("buy_gap_reserve", BUY_GAP_RESERVE),
        ("sector_fit", SECTOR_FIT),
        ("turnover_fit", TURNOVER_FIT),
    ):
        if field in sizing and sizing[field] != allowed:
            errors.append("position_sizing.%s 只允許 %s；舊 Decision run 省略此欄位" % (field, allowed))
    if "reentry_cooldown" in sizing:
        cooldown = sizing["reentry_cooldown"]
        if not isinstance(cooldown, Mapping):
            errors.append("position_sizing.reentry_cooldown 必須是物件")
        else:
            reject_unknown_fields(cooldown, REENTRY_COOLDOWN_FIELDS, "position_sizing.reentry_cooldown", errors)
            if cooldown.get("version") != REENTRY_COOLDOWN_VERSION:
                errors.append("position_sizing.reentry_cooldown.version 只允許 %s" % REENTRY_COOLDOWN_VERSION)
            days = cooldown.get("days")
            if isinstance(days, bool) or not isinstance(days, int) or days < 1:
                errors.append("position_sizing.reentry_cooldown.days 必須是正整數")
            blocked, runs = cooldown.get("blocked_symbols"), cooldown.get("source_run_ids")
            if (blocked is None) != (runs is None):
                errors.append("position_sizing.reentry_cooldown.blocked_symbols 與 source_run_ids 必須同時出現")
            if blocked is not None and (
                not isinstance(blocked, list) or not all(isinstance(s, str) and s == s.upper() and s for s in blocked)
                or blocked != sorted(set(blocked))
            ):
                errors.append("position_sizing.reentry_cooldown.blocked_symbols 必須是排序且不重複的大寫代號清單")
            if runs is not None and (
                not isinstance(runs, list) or not all(isinstance(s, str) and s for s in runs) or runs != sorted(set(runs))
            ):
                errors.append("position_sizing.reentry_cooldown.source_run_ids 必須是排序且不重複的非空字串清單")
    if "drawdown_derisk" in sizing:
        derisk = sizing["drawdown_derisk"]
        if not isinstance(derisk, Mapping):
            errors.append("position_sizing.drawdown_derisk 必須是物件")
        else:
            reject_unknown_fields(derisk, DRAWDOWN_DERISK_FIELDS, "position_sizing.drawdown_derisk", errors)
            if derisk.get("version") not in DRAWDOWN_DERISK_VERSIONS:
                errors.append("position_sizing.drawdown_derisk.version 只允許 %s" % "／".join(DRAWDOWN_DERISK_VERSIONS))
            if "max_derisk_fraction" in derisk:
                try:
                    fraction = decimal_value(derisk["max_derisk_fraction"], "position_sizing.drawdown_derisk.max_derisk_fraction")
                    if fraction <= 0 or fraction > 1:
                        errors.append("position_sizing.drawdown_derisk.max_derisk_fraction 必須大於 0 且不超過 1")
                except DecisionToolError as error:
                    errors.append(str(error))
    if "insufficient_funds_policy" in sizing and sizing["insufficient_funds_policy"] not in INSUFFICIENT_FUNDS_POLICIES:
        errors.append(
            "position_sizing.insufficient_funds_policy 只允許 %s；舊 Decision run 省略此欄位"
            % "／".join(INSUFFICIENT_FUNDS_POLICIES)
        )
    if "high_conviction_cash_priority" in sizing and "cash_fit_order" not in sizing:
        errors.append("position_sizing.high_conviction_cash_priority 必須與 cash_fit_order 同時出現")
    if "cash_fit_min_fill_ratio" in sizing:
        if "cash_fit_order" not in sizing:
            errors.append("position_sizing.cash_fit_min_fill_ratio 必須與 cash_fit_order 同時出現")
        try:
            ratio = decimal_value(sizing["cash_fit_min_fill_ratio"], "position_sizing.cash_fit_min_fill_ratio")
            if ratio <= 0 or ratio > 1:
                errors.append("position_sizing.cash_fit_min_fill_ratio 必須大於 0 且不超過 1")
        except DecisionToolError as error:
            errors.append(str(error))
    if "buy_gap_reserve_rate" in sizing:
        if "buy_gap_reserve" not in sizing:
            errors.append("position_sizing.buy_gap_reserve_rate 必須與 buy_gap_reserve 同時出現")
        try:
            rate = decimal_value(sizing["buy_gap_reserve_rate"], "position_sizing.buy_gap_reserve_rate")
            if rate < 0 or rate >= 1:
                errors.append("position_sizing.buy_gap_reserve_rate 必須不小於 0 且小於 1")
        except DecisionToolError as error:
            errors.append(str(error))
    if "high_conviction_cash_floor" in sizing:
        _validate_high_conviction_cash_floor(sizing, errors)
    if "limit_scaled_conviction" in sizing:
        _validate_limit_scaled_conviction(sizing, errors)


def _ratio_in_open_unit(raw: object, label: str, errors: List[str], allow_one: bool) -> None:
    """比例參數必須是可解析的 Decimal 字串，且介於 0 與 1（allow_one 時含 1）；布林與非字串數字一律拒絕。"""
    if isinstance(raw, bool) or not isinstance(raw, str):
        errors.append("%s 必須是數字字串" % label)
        return
    try:
        value = decimal_value(raw, label)
    except DecisionToolError as error:
        errors.append(str(error))
        return
    if value <= 0 or value > 1 or (value == 1 and not allow_one):
        errors.append("%s 必須大於 0 且%s 1" % (label, "不超過" if allow_one else "小於"))


def _validate_high_conviction_cash_floor(sizing: Mapping[str, object], errors: List[str]) -> None:
    """high_conviction_cash_floor：{version: v1, floor_ratio: 0～1（不含）}；語意建立在 cash_buffer_floor 與
    cash_fit_order 之上（high 先拿現金），有 drawdown_derisk 時必須是 v3（減碼後不得加回）。"""
    gate = sizing["high_conviction_cash_floor"]
    prefix = "position_sizing.high_conviction_cash_floor"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, HIGH_CONVICTION_CASH_FLOOR_FIELDS, prefix, errors)
    if gate.get("version") != HIGH_CONVICTION_CASH_FLOOR:
        errors.append("%s.version 只允許 %s" % (prefix, HIGH_CONVICTION_CASH_FLOOR))
    if "floor_ratio" not in gate:
        errors.append("%s.floor_ratio 是必要欄位" % prefix)
    else:
        _ratio_in_open_unit(gate["floor_ratio"], "%s.floor_ratio" % prefix, errors, allow_one=False)
    if sizing.get("cash_buffer_floor") != CASH_BUFFER_FLOOR:
        errors.append("%s 必須與 cash_buffer_floor 同時啟用" % prefix)
    if sizing.get("cash_fit_order") != CASH_FIT_ORDER:
        errors.append("%s 必須與 cash_fit_order 同時啟用（高信心買單要先拿現金）" % prefix)
    derisk = sizing.get("drawdown_derisk")
    if derisk is not None and not (isinstance(derisk, Mapping) and derisk.get("version") == DRAWDOWN_DERISK_VERSION_V3):
        errors.append("%s 與 drawdown_derisk 同時使用時，drawdown_derisk.version 必須是 %s" % (prefix, DRAWDOWN_DERISK_VERSION_V3))


def _validate_limit_scaled_conviction(sizing: Mapping[str, object], errors: List[str]) -> None:
    """limit_scaled_conviction：{version: v1, alpha: 0～1（含 1）}；alpha 必填。需要 conviction_volatility_v2
    與 selection_ranking（高信心依名次填充）。"""
    gate = sizing["limit_scaled_conviction"]
    prefix = "position_sizing.limit_scaled_conviction"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, LIMIT_SCALED_CONVICTION_FIELDS, prefix, errors)
    if gate.get("version") != LIMIT_SCALED_CONVICTION:
        errors.append("%s.version 只允許 %s" % (prefix, LIMIT_SCALED_CONVICTION))
    if "alpha" not in gate:
        errors.append("%s.alpha 是必要欄位（由人設定，程式不提供預設值）" % prefix)
    else:
        _ratio_in_open_unit(gate["alpha"], "%s.alpha" % prefix, errors, allow_one=True)
    if sizing.get("method") != SIZING_METHOD_V2:
        errors.append("%s 只允許用於 %s" % (prefix, SIZING_METHOD_V2))
    if sizing.get("selection_ranking") != SELECTION_RANKING:
        errors.append("%s 必須與 selection_ranking 同時啟用（高信心依入選名次填充）" % prefix)


def high_conviction_floor_ratio(sizing: Mapping[str, object]) -> Optional[Decimal]:
    """high_conviction_cash_floor 啟用時的 floor_ratio；沒有閘門回 None（走原式）。"""
    gate = sizing.get("high_conviction_cash_floor")
    if not isinstance(gate, Mapping):
        return None
    return decimal_value(gate["floor_ratio"], "position_sizing.high_conviction_cash_floor.floor_ratio")


def limit_scaled_alpha(sizing: Mapping[str, object]) -> Optional[Decimal]:
    """limit_scaled_conviction 啟用時的 alpha；沒有閘門回 None（走原式）。"""
    gate = sizing.get("limit_scaled_conviction")
    if not isinstance(gate, Mapping):
        return None
    return decimal_value(gate["alpha"], "position_sizing.limit_scaled_conviction.alpha")


def _grid_point(raw: object, label: str) -> Decimal:
    """格點與單日上限：只接受兩位小數字串（例如 "0.15"），以 1pp 為單位；數字型、百分號、科學記號一律拒絕。"""
    if isinstance(raw, bool) or not isinstance(raw, str) or not _TWO_DECIMALS.match(raw):
        raise DecisionToolError("%s 必須是兩位小數字串（例如 \"0.15\"）" % label)
    return decimal_value(raw, label)


def cash_grade_grid(gate: Mapping[str, object]) -> Dict[str, Dict[str, Decimal]]:
    """解析 grid 為 {姿態: {子等級: Decimal}}；格式錯誤拋 DecisionToolError（呼叫端先以 validate_position_sizing 驗證）。"""
    grid = gate.get("grid")
    if not isinstance(grid, Mapping) or set(grid) != set(CASH_STANCES):
        raise DecisionToolError("position_sizing.cash_ratio_range.grid 必須剛好包含 aggressive／neutral／defensive")
    result: Dict[str, Dict[str, Decimal]] = {}
    for level in CASH_STANCES:
        points = grid[level]
        if not isinstance(points, Mapping) or set(points) != set(CASH_GRADES):
            raise DecisionToolError("position_sizing.cash_ratio_range.grid.%s 必須剛好包含 lower／middle／upper" % level)
        result[level] = {
            grade: _grid_point(points[grade], "position_sizing.cash_ratio_range.grid.%s.%s" % (level, grade))
            for grade in CASH_GRADES
        }
    return result


def cash_anchor_ratio(gate: Mapping[str, object]) -> Decimal:
    anchor = gate.get("anchor")
    if not isinstance(anchor, Mapping):
        raise DecisionToolError("position_sizing.cash_ratio_range.anchor 尚未由 pipeline 綁定")
    raw = anchor.get("ratio")
    if isinstance(raw, bool) or not isinstance(raw, str) or not _ANCHOR_RATIO.match(raw):
        raise DecisionToolError("position_sizing.cash_ratio_range.anchor.ratio 必須是八位小數字串")
    value = decimal_value(raw, "position_sizing.cash_ratio_range.anchor.ratio")
    if value > 1:
        raise DecisionToolError("position_sizing.cash_ratio_range.anchor.ratio 必須介於 0 與 1")
    return value


def cash_daily_cap(gate: Mapping[str, object], level: str) -> Decimal:
    """今日該姿態的現金上限：max(該姿態 lower, anchor + max_daily_raise)。切到更防守的姿態時永遠可選 lower。"""
    grid = cash_grade_grid(gate)
    raise_limit = _grid_point(gate.get("max_daily_raise"), "position_sizing.cash_ratio_range.max_daily_raise")
    return max(grid[level]["lower"], cash_anchor_ratio(gate) + raise_limit)


def allowed_cash_grades(gate: Mapping[str, object]) -> Dict[str, List[str]]:
    """各姿態今日可選的子等級（由低到高）；只限制往上，往下不限。"""
    grid = cash_grade_grid(gate)
    return {
        level: [grade for grade in CASH_GRADES if grid[level][grade] <= cash_daily_cap(gate, level)]
        for level in CASH_STANCES
    }


def cash_ratio_gate(sizing: Mapping[str, object]) -> Optional[Mapping[str, object]]:
    """cash_ratio_range 啟用時回傳閘門物件；沒有閘門回 None（走 cash_buffer_by_stance 原式）。"""
    gate = sizing.get("cash_ratio_range") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def _percent(value: Decimal) -> str:
    return format(value, ".0%")


def anchor_reference_tokens(
    gate: Mapping[str, object], prior_context: Optional[Mapping[str, object]] = None,
) -> List[str]:
    """理由文字不得出現的前次決策識別碼：前次比例只是背景，不是證據。

    anchor.source 記錄 run_id、decision／proposal 的 id 與雜湊；前次 decision_id 不在 anchor 結構內（不改已封存 Policy），
    改由同一份前次決策背景（``prior_context.source.decision_id``，anchor 重建驗證已要求兩者指向同一個 run）補上。"""
    anchor = gate.get("anchor")
    source = anchor.get("source") if isinstance(anchor, Mapping) else None
    tokens: List[object] = []
    if isinstance(source, Mapping):
        tokens.extend(source.get(key) for key in ("run_id", "decision_sha256", "proposal_id", "proposal_sha256"))
    if isinstance(prior_context, Mapping) and prior_context.get("status") == "available":
        prior_source = prior_context.get("source")
        if isinstance(prior_source, Mapping):
            tokens.append(prior_source.get("decision_id"))
    return [token for token in tokens if isinstance(token, str) and token.strip()]


# 前綴＋20 位十六進位摘要的識別碼（decision_id、proposal_id）；裸摘要同樣禁用。run_id 不拆：其餘部分只是日期。
_PREFIXED_DIGEST = re.compile(r"^(?:decision|proposal):([0-9a-f]{20})$")
# 邊界只看 ASCII 英數與底線：前後接中文、空白或標點仍算獨立出現，接英數（較長的另一個 ID）則不算。
_TOKEN_BOUNDARY = "[0-9A-Za-z_]"


def cited_reference_tokens(text: str, tokens: Sequence[str]) -> List[str]:
    """``text`` 中以獨立形式出現的禁用識別碼（依 ``tokens`` 順序、去重）。

    每個 token 連同「前綴＋20 位摘要」形式的裸摘要一起比對；前一字元與後一字元都不得是 ASCII 英數或底線，
    所以以前次 ID 開頭的較長 ID、或嵌在更長十六進位字串中的片段不算引用。只比對完整 token／完整摘要，
    不封鎖 run_id 片段、日期或短雜湊。pipeline、policy 綁定與 Finalizer 都經由此函式。"""
    variants: List[str] = []
    for token in tokens:
        variants.append(token)
        match = _PREFIXED_DIGEST.match(token)
        if match:
            variants.append(match.group(1))
    cited: List[str] = []
    for variant in dict.fromkeys(variants):
        pattern = "(?<!%s)%s(?!%s)" % (_TOKEN_BOUNDARY, re.escape(variant), _TOKEN_BOUNDARY)
        if re.search(pattern, text):
            cited.append(variant)
    return cited


def _validate_cash_grade(
    gate: Mapping[str, object],
    stance: Mapping[str, object],
    prefix: str,
    errors: List[str],
    prior_context: Optional[Mapping[str, object]] = None,
) -> None:
    grade = stance.get("cash_grade")
    if grade not in CASH_GRADES:
        errors.append("%s.cash_grade 必須是 %s" % (prefix, "／".join(CASH_GRADES)))
        return
    rationale = stance.get("rationale")
    if isinstance(rationale, str):
        cited = cited_reference_tokens(rationale, anchor_reference_tokens(gate, prior_context))
        if cited:
            errors.append("%s.rationale 不得引用前次決策識別碼（前次比例只是背景）：%s" % (prefix, "、".join(cited)))
    level = stance.get("level")
    if level not in CASH_STANCES:
        return
    try:
        grid = cash_grade_grid(gate)
        allowed = allowed_cash_grades(gate)
        cap = cash_daily_cap(gate, str(level))
        ratio = cash_anchor_ratio(gate)
        raise_limit = _grid_point(gate.get("max_daily_raise"), "max_daily_raise")
    except DecisionToolError as error:
        errors.append("%s 無法核對 cash_grade：%s" % (prefix, error))
        return
    if grade not in allowed[str(level)]:
        errors.append(
            "%s.cash_grade %s（%s）超過今日上限：%s 今日可選 %s（今日上限 %s = max(該姿態 lower, 前次比例 %s + 單日上限 %s)，"
            "只限制往上，往下不限）；請改選可選子等級或改變姿態，程式不會替你調整"
            % (prefix, grade, _percent(grid[str(level)][str(grade)]), level,
               "／".join("%s（%s）" % (item, _percent(grid[str(level)][item])) for item in allowed[str(level)]),
               format(cap, ".2%"), format(ratio, ".2%"), _percent(raise_limit))
        )


def _validate_cash_ratio_range(policy: Mapping[str, object], sizing: Mapping[str, object], errors: List[str]) -> None:
    """cash_ratio_range v1：格點、單日上限、anchor 形狀與前提；全部 fail-closed。"""
    gate = sizing["cash_ratio_range"]
    prefix = "position_sizing.cash_ratio_range"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, CASH_RATIO_RANGE_FIELDS, prefix, errors)
    if gate.get("version") != CASH_RATIO_RANGE:
        errors.append("%s.version 只允許 %s" % (prefix, CASH_RATIO_RANGE))
    if "high_conviction_cash_floor" in sizing:
        errors.append("%s 不得與 high_conviction_cash_floor 同時啟用（兩者都改變防守現金落點）" % prefix)
    derisk = sizing.get("drawdown_derisk")
    if derisk is not None and not (isinstance(derisk, Mapping) and derisk.get("version") == DRAWDOWN_DERISK_VERSION_V3):
        errors.append("%s 與 drawdown_derisk 同時使用時，drawdown_derisk.version 必須是 %s" % (prefix, DRAWDOWN_DERISK_VERSION_V3))
    raise_limit: Optional[Decimal] = None
    try:
        raise_limit = _grid_point(gate.get("max_daily_raise"), "%s.max_daily_raise" % prefix)
        if raise_limit < MAX_DAILY_RAISE_MIN or raise_limit > MAX_DAILY_RAISE_MAX:
            errors.append("%s.max_daily_raise 必須介於 %s 與 %s" % (prefix, MAX_DAILY_RAISE_MIN, MAX_DAILY_RAISE_MAX))
            raise_limit = None
    except DecisionToolError as error:
        errors.append(str(error))
    try:
        grid = cash_grade_grid(gate)
    except DecisionToolError as error:
        errors.append(str(error))
        grid = None
    if grid is not None:
        try:
            ceiling = decimal_value(policy.get("cash_weight_ceiling"), "cash_weight_ceiling")
        except DecisionToolError as error:
            errors.append(str(error))
            ceiling = None
        previous_upper: Optional[Decimal] = None
        for level in CASH_STANCES:
            points = [grid[level][grade] for grade in CASH_GRADES]
            if not points[0] < points[1] < points[2]:
                errors.append("%s.grid.%s 必須 lower < middle < upper（嚴格遞增）" % (prefix, level))
            if raise_limit is not None and any(b - a > raise_limit for a, b in zip(points, points[1:])):
                errors.append(
                    "%s.grid.%s 相鄰格點差距不得大於 max_daily_raise %s，否則較高的格點在單日上限下永遠選不到"
                    % (prefix, level, raise_limit)
                )
            if any(point > CASH_GRID_MAX for point in points):
                errors.append("%s.grid.%s 每個格點都不得高於 %s（防守現金上限）" % (prefix, level, CASH_GRID_MAX))
            if ceiling is not None and any(point >= ceiling for point in points):
                errors.append("%s.grid.%s 每個格點都必須低於 cash_weight_ceiling" % (prefix, level))
            if previous_upper is not None and points[0] < previous_upper:
                errors.append("%s.grid 姿態之間不得重疊：%s.lower 不得低於前一姿態的 upper" % (prefix, level))
            previous_upper = points[2]
    if "anchor" in gate:
        _validate_cash_anchor_shape(gate, prefix, errors)
    bound = "conviction_by_symbol" in sizing
    if bound and "anchor" not in gate:
        errors.append("%s 綁定交易決策時必須有 pipeline 綁定的 anchor（前次現金比例）" % prefix)
    if bound and "cash_grade" not in sizing:
        errors.append("position_sizing.cash_grade 是 cash_ratio_range 綁定交易決策時的必要欄位")
    if "cash_grade" in sizing and not bound:
        errors.append("position_sizing.cash_grade 只能與交易決策一起綁定")


def _validate_cash_anchor_shape(gate: Mapping[str, object], prefix: str, errors: List[str]) -> None:
    anchor = gate["anchor"]
    label = "%s.anchor" % prefix
    if not isinstance(anchor, Mapping):
        errors.append("%s 必須是物件" % label)
        return
    if set(anchor) != CASH_ANCHOR_FIELDS:
        errors.append("%s 欄位必須剛好是 %s" % (label, "、".join(sorted(CASH_ANCHOR_FIELDS))))
        return
    status = anchor.get("status")
    if status not in CASH_ANCHOR_STATUSES:
        errors.append("%s.status 必須是 %s" % (label, "／".join(CASH_ANCHOR_STATUSES)))
    try:
        cash_anchor_ratio(gate)
    except DecisionToolError as error:
        errors.append(str(error))
    source = anchor.get("source")
    if status == "account_cash" and source is not None:
        errors.append("%s.source 在 account_cash 時必須為 null" % label)
    if status == "prior_decision":
        if not isinstance(source, Mapping) or set(source) != CASH_ANCHOR_SOURCE_FIELDS:
            errors.append("%s.source 在 prior_decision 時必須剛好有 %s" % (label, "、".join(sorted(CASH_ANCHOR_SOURCE_FIELDS))))
        elif not all(isinstance(source[key], str) and source[key].strip() for key in CASH_ANCHOR_SOURCE_FIELDS):
            errors.append("%s.source 每個欄位都必須是非空字串" % label)


def cash_fit_min_fill_ratio(sizing: Mapping[str, object]) -> Decimal:
    """cash_fit_order 啟用時的最低成交比例；省略參數即預設 0.5。"""
    if "cash_fit_min_fill_ratio" in sizing:
        return decimal_value(sizing["cash_fit_min_fill_ratio"], "position_sizing.cash_fit_min_fill_ratio")
    return CASH_FIT_MIN_FILL_RATIO


def buy_gap_reserve_rate(sizing: Mapping[str, object]) -> Decimal:
    """buy_gap_reserve 啟用時的跳空預留比例；省略參數即預設 0.03。"""
    if "buy_gap_reserve_rate" in sizing:
        return decimal_value(sizing["buy_gap_reserve_rate"], "position_sizing.buy_gap_reserve_rate")
    return BUY_GAP_RESERVE_RATE


def drawdown_derisk_fraction(sizing: Mapping[str, object]) -> Optional[Decimal]:
    """drawdown_derisk 啟用時單次最多減碼的 NAV 比例（省略參數即預設 0.25）；沒有閘門回 None。"""
    derisk = sizing.get("drawdown_derisk")
    if not isinstance(derisk, Mapping):
        return None
    if "max_derisk_fraction" in derisk:
        return decimal_value(derisk["max_derisk_fraction"], "position_sizing.drawdown_derisk.max_derisk_fraction")
    return DRAWDOWN_DERISK_FRACTION


def drawdown_derisk_v2(sizing: Mapping[str, object]) -> bool:
    """drawdown_derisk 為 v2 以上（整張進位不得超過單次減碼硬上限）；v1 或沒有閘門回 False。"""
    derisk = sizing.get("drawdown_derisk")
    return isinstance(derisk, Mapping) and derisk.get("version") in (DRAWDOWN_DERISK_VERSION_V2, DRAWDOWN_DERISK_VERSION_V3)


def drawdown_derisk_v3(sizing: Mapping[str, object]) -> bool:
    """drawdown_derisk 為 v3（減碼後股數為上限，同檔 add 不得加回）；其餘回 False。"""
    derisk = sizing.get("drawdown_derisk")
    return isinstance(derisk, Mapping) and derisk.get("version") == DRAWDOWN_DERISK_VERSION_V3


def reentry_blocked_symbols(sizing: Mapping[str, object]) -> Optional[List[str]]:
    """reentry_cooldown 啟用且已綁定名單時回傳不得再買進的代號；沒有閘門或沒有名單回 None（不設冷卻）。"""
    cooldown = sizing.get("reentry_cooldown")
    if not isinstance(cooldown, Mapping) or not isinstance(cooldown.get("blocked_symbols"), list):
        return None
    return [str(symbol).upper() for symbol in cooldown["blocked_symbols"]]


def _validate_rotation(sizing: Mapping[str, object], errors: List[str]) -> None:
    """滿檔換股名額的政策欄位；省略即舊行為（滿檔完全不進新股）。"""
    if "rotation" not in sizing:
        return
    rotation = sizing["rotation"]
    if not isinstance(rotation, Mapping):
        errors.append("position_sizing.rotation 必須是物件")
        return
    reject_unknown_fields(rotation, ROTATION_FIELDS, "position_sizing.rotation", errors)
    if rotation.get("version") != ROTATION_VERSION:
        errors.append("position_sizing.rotation.version 只允許 %s" % ROTATION_VERSION)
    swaps = rotation.get("max_swaps_per_run")
    if isinstance(swaps, bool) or not isinstance(swaps, int) or swaps < 0:
        errors.append("position_sizing.rotation.max_swaps_per_run 必須是非負整數")
    if rotation.get("min_new_conviction") not in CONVICTION_LEVELS:
        errors.append("position_sizing.rotation.min_new_conviction 必須是 high／medium／low 之一")
    if "forced_swap" in rotation:
        forced = rotation["forced_swap"]
        if not isinstance(forced, Mapping):
            errors.append("position_sizing.rotation.forced_swap 必須是物件")
        else:
            reject_unknown_fields(forced, FORCED_SWAP_FIELDS, "position_sizing.rotation.forced_swap", errors)
            if forced.get("version") != FORCED_SWAP_VERSION:
                errors.append("position_sizing.rotation.forced_swap.version 只允許 %s" % FORCED_SWAP_VERSION)
            if forced.get("min_new_conviction") not in CONVICTION_LEVELS:
                errors.append("position_sizing.rotation.forced_swap.min_new_conviction 必須是 high／medium／low 之一")
            count = forced.get("max_swaps_per_run")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                errors.append("position_sizing.rotation.forced_swap.max_swaps_per_run 必須是正整數")
            if "confirmation" in forced and forced["confirmation"] != FORCED_SWAP_CONFIRMATION:
                errors.append(
                    "position_sizing.rotation.forced_swap.confirmation 只允許 %s；舊 Decision run 省略此欄位"
                    % FORCED_SWAP_CONFIRMATION
                )
            broken, ref = forced.get("broken_symbols"), forced.get("verdicts_ref")
            if broken is not None:
                if (not isinstance(broken, list) or not all(isinstance(s, str) and s == s.upper() and s for s in broken)
                        or broken != sorted(set(broken))):
                    errors.append("position_sizing.rotation.forced_swap.broken_symbols 必須是排序且不重複的大寫代號清單")
                if not isinstance(ref, Mapping):
                    errors.append("position_sizing.rotation.forced_swap.broken_symbols 必須附 verdicts_ref")
            if ref is not None:
                if not isinstance(ref, Mapping) or set(ref) != VERDICTS_REF_FIELDS or not all(isinstance(v, str) and v for v in ref.values()):
                    errors.append("position_sizing.rotation.forced_swap.verdicts_ref 欄位必須是 verdicts_id、as_of、sha256 的非空字串")
                elif broken is None:
                    errors.append("position_sizing.rotation.forced_swap.verdicts_ref 必須附 broken_symbols")
    # 換股名額的前提是「新股不佔滿檔名額」，否則釋出名額的計算沒有意義。
    if "held_add_priority" not in sizing:
        errors.append("position_sizing.rotation 必須與 held_add_priority 同時存在")


def validate_position_sizing(
    policy: Mapping[str, object], universe: Sequence[str], errors: List[str]
) -> None:
    sizing = policy.get("position_sizing")
    if not isinstance(sizing, Mapping):
        errors.append("DecisionPolicy.position_sizing 必須是物件")
        return
    reject_unknown_fields(
        sizing,
        {
            "method", "conviction_multipliers", "volatility_floor", "cash_buffer_by_stance",
            "cash_stance", "sizing_plan_id", "sizing_plan_sha256", "conviction_by_symbol",
            "support_by_symbol", "stock_weight_fit", "positions_by_breadth", "position_breadth",
            "held_add_priority", "rotation", "volatility_exponent", "selection_tiebreak",
            "selection_ranking", "rank_by_symbol", "selection_ranking_id", "selection_ranking_sha256",
            "cash_buffer_floor", "cash_fit_order", "cash_fit_min_fill_ratio", "high_conviction_cash_priority", "cash_ceiling_fix",
            "stock_limit_headroom", "buy_gap_reserve", "buy_gap_reserve_rate", "insufficient_funds_policy",
            "sector_fit", "turnover_fit", "reentry_cooldown", "drawdown_derisk",
            "high_conviction_cash_floor", "limit_scaled_conviction", "cash_ratio_range", "cash_grade",
        },
        "DecisionPolicy.position_sizing",
        errors,
    )
    if sizing.get("method") not in SIZING_METHODS:
        errors.append("position_sizing.method 必須為 %s" % "或".join(SIZING_METHODS))
    _validate_volatility_exponent(sizing, errors)
    _validate_execution_gates(sizing, errors)
    if "cash_ratio_range" in sizing:
        _validate_cash_ratio_range(policy, sizing, errors)
    elif "cash_grade" in sizing:
        errors.append("position_sizing.cash_grade 只允許與 cash_ratio_range 一起出現；舊 Decision run 省略此欄位")
    if "selection_tiebreak" in sizing and sizing["selection_tiebreak"] not in SELECTION_TIEBREAKS:
        errors.append(
            "position_sizing.selection_tiebreak 必須是 %s" % "／".join(SELECTION_TIEBREAKS)
        )
    if "selection_ranking" in sizing and sizing["selection_ranking"] != SELECTION_RANKING:
        errors.append("position_sizing.selection_ranking 只允許 %s；舊 Decision run 省略此欄位" % SELECTION_RANKING)
    if "stock_weight_fit" in sizing and sizing["stock_weight_fit"] != STOCK_WEIGHT_FIT:
        errors.append("position_sizing.stock_weight_fit 只允許 %s；舊 Decision run 省略此欄位" % STOCK_WEIGHT_FIT)
    if "cash_buffer_floor" in sizing and sizing["cash_buffer_floor"] != CASH_BUFFER_FLOOR:
        errors.append("position_sizing.cash_buffer_floor 只允許 %s；舊 Decision run 省略此欄位" % CASH_BUFFER_FLOOR)
    if "held_add_priority" in sizing and sizing["held_add_priority"] != HELD_ADD_PRIORITY:
        errors.append("position_sizing.held_add_priority 只允許 %s；舊 Decision run 省略此欄位" % HELD_ADD_PRIORITY)
    _validate_rotation(sizing, errors)
    multipliers = sizing.get("conviction_multipliers")
    if not isinstance(multipliers, Mapping) or set(multipliers) != set(CONVICTION_LEVELS):
        errors.append("position_sizing.conviction_multipliers 必須剛好包含 high／medium／low")
    else:
        try:
            values = [
                decimal_value(multipliers[level], "conviction_multipliers.%s" % level)
                for level in CONVICTION_LEVELS
            ]
            if any(value <= 0 for value in values):
                errors.append("position_sizing.conviction_multipliers 必須大於 0")
            if not values[0] >= values[1] >= values[2]:
                errors.append("position_sizing.conviction_multipliers 必須 high ≥ medium ≥ low")
        except DecisionToolError as error:
            errors.append(str(error))
    try:
        floor = decimal_value(sizing.get("volatility_floor"), "position_sizing.volatility_floor")
        if floor <= 0 or floor >= 1:
            errors.append("position_sizing.volatility_floor 必須介於 0 與 1（不含）")
    except DecisionToolError as error:
        errors.append(str(error))
    buffers = sizing.get("cash_buffer_by_stance")
    if not isinstance(buffers, Mapping) or set(buffers) != set(CASH_STANCES):
        errors.append("position_sizing.cash_buffer_by_stance 必須剛好包含 aggressive／neutral／defensive")
        buffers = None
    else:
        try:
            values = [decimal_value(buffers[level], "cash_buffer_by_stance.%s" % level) for level in CASH_STANCES]
            ceiling = decimal_value(policy.get("cash_weight_ceiling"), "cash_weight_ceiling")
            if any(value < 0 or value >= ceiling for value in values):
                errors.append("position_sizing.cash_buffer_by_stance 必須不小於 0 且低於 cash_weight_ceiling")
            if not values[0] <= values[1] <= values[2]:
                errors.append("position_sizing.cash_buffer_by_stance 必須 aggressive ≤ neutral ≤ defensive")
        except DecisionToolError as error:
            errors.append(str(error))
    targets = _validate_breadth_targets(policy, sizing, errors)
    tiers = sizing.get("conviction_by_symbol")
    if "position_breadth" in sizing and targets is None:
        errors.append("position_sizing.position_breadth 需要 positions_by_breadth")
    if targets is not None and tiers is not None and sizing.get("position_breadth") not in POSITION_BREADTHS:
        errors.append("position_sizing.position_breadth 必須是 %s" % "／".join(POSITION_BREADTHS))
    if targets is not None and tiers is None and "position_breadth" in sizing:
        errors.append("position_sizing.position_breadth 只能與交易決策一起綁定")
    bound = [
        field for field in ("sizing_plan_id", "sizing_plan_sha256", "cash_stance") if sizing.get(field)
    ]
    if tiers is None:
        if bound:
            errors.append("position_sizing 有交易決策綁定卻缺少 conviction_by_symbol")
        return
    if len(bound) != 3:
        errors.append("position_sizing.conviction_by_symbol 必須綁定 sizing_plan_id、sizing_plan_sha256 與 cash_stance")
    stance = sizing.get("cash_stance")
    if stance not in CASH_STANCES:
        errors.append("position_sizing.cash_stance 必須是 %s" % "／".join(CASH_STANCES))
    elif "cash_ratio_range" in sizing:
        # cash_ratio_range（版本閘門）：緩衝改由 grid[姿態][子等級] 對應，子等級必須在今日可選格點內。
        gate = sizing["cash_ratio_range"]
        grade = sizing.get("cash_grade")
        try:
            if not isinstance(gate, Mapping) or "anchor" not in gate:
                raise DecisionToolError("position_sizing.cash_ratio_range 缺少 anchor，無法核對 cash_grade")
            if grade not in CASH_GRADES:
                raise DecisionToolError("position_sizing.cash_grade 必須是 %s" % "／".join(CASH_GRADES))
            if grade not in allowed_cash_grades(gate)[str(stance)]:
                errors.append("position_sizing.cash_grade %s 不在 %s 今日可選格點內" % (grade, stance))
            if decimal_value(policy.get("cash_buffer_rate"), "cash_buffer_rate") != cash_grade_grid(gate)[str(stance)][str(grade)]:
                errors.append("cash_buffer_rate 必須等於 cash_ratio_range.grid 中 cash_stance／cash_grade 對應的現金比例")
        except DecisionToolError as error:
            errors.append(str(error))
    elif buffers is not None:
        try:
            if decimal_value(policy.get("cash_buffer_rate"), "cash_buffer_rate") != decimal_value(
                buffers[stance], "cash_buffer_by_stance.%s" % stance
            ):
                errors.append("cash_buffer_rate 必須等於 cash_stance 對應的現金緩衝")
        except DecisionToolError as error:
            errors.append(str(error))
    if not isinstance(tiers, Mapping):
        errors.append("position_sizing.conviction_by_symbol 必須是物件")
        return
    for symbol, tier in tiers.items():
        if symbol != str(symbol).upper() or symbol not in universe:
            errors.append("position_sizing.conviction_by_symbol 含交易池外或非大寫股票：%s" % symbol)
        if tier not in CONVICTION_LEVELS:
            errors.append("position_sizing.conviction_by_symbol.%s 等級無效" % symbol)
    _validate_support(sizing, tiers, errors)
    _validate_rank(sizing, tiers, errors)


def _validate_breadth_targets(
    policy: Mapping[str, object], sizing: Mapping[str, object], errors: List[str]
) -> Optional[Mapping[str, object]]:
    """positions_by_breadth：三種廣度各對應一個正整數檔數且集中 ≤ 平衡 ≤ 分散；配置時再夾在 min／max_positions 之間。"""
    if "positions_by_breadth" not in sizing:
        return None
    targets = sizing["positions_by_breadth"]
    if not isinstance(targets, Mapping) or set(targets) != set(POSITION_BREADTHS):
        errors.append("position_sizing.positions_by_breadth 必須剛好包含 concentrated／balanced／diversified")
        return None
    values = [targets[level] for level in POSITION_BREADTHS]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        errors.append("position_sizing.positions_by_breadth 必須是整數")
        return None
    if any(value < 1 for value in values):
        errors.append("position_sizing.positions_by_breadth 必須是正整數")
    if not values[0] <= values[1] <= values[2]:
        errors.append("position_sizing.positions_by_breadth 必須 concentrated ≤ balanced ≤ diversified")
    return targets


def breadth_target(sizing: Mapping[str, object]) -> Optional[int]:
    """已綁定持股廣度時回傳目標檔數；舊 policy 沒有 positions_by_breadth 時回 None（沿用 max_positions）。"""
    targets = sizing.get("positions_by_breadth")
    level = sizing.get("position_breadth")
    if not isinstance(targets, Mapping) or level not in targets:
        return None
    return int(targets[level])


def _validate_rank(sizing: Mapping[str, object], tiers: Mapping[str, object], errors: List[str]) -> None:
    ranked = [field for field in ("rank_by_symbol", "selection_ranking_id", "selection_ranking_sha256") if field in sizing]
    if not ranked:
        return
    if sizing.get("selection_ranking") != SELECTION_RANKING or len(ranked) != 3:
        errors.append("position_sizing.rank_by_symbol 需與 selection_ranking、selection_ranking_id、selection_ranking_sha256 一起出現")
        return
    rank = sizing["rank_by_symbol"]
    if not isinstance(rank, Mapping) or set(rank) != set(tiers):
        errors.append("position_sizing.rank_by_symbol 必須與 conviction_by_symbol 股票一致")
    elif any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in rank.values()):
        errors.append("position_sizing.rank_by_symbol 必須是從 1 開始的整數")


def _validate_support(sizing: Mapping[str, object], tiers: Mapping[str, object], errors: List[str]) -> None:
    support = sizing.get("support_by_symbol")
    if support is None:
        return
    if not isinstance(support, Mapping) or set(support) != set(tiers):
        errors.append("position_sizing.support_by_symbol 必須與 conviction_by_symbol 股票一致")
    elif any(isinstance(value, bool) or not isinstance(value, int) for value in support.values()):
        errors.append("position_sizing.support_by_symbol 必須是整數")


def conviction_weights(
    order: Sequence[str],
    tiers: Mapping[str, str],
    multipliers: Mapping[str, Decimal],
    volatility: Mapping[str, Decimal],
    volatility_floor: Decimal,
    limits: Mapping[str, Decimal],
    budget: Decimal,
    volatility_exponent: Optional[Decimal] = None,
) -> Dict[str, Decimal]:
    """Split budget ∝ multiplier / max(ATR%, floor) ** exponent; cap at limits and redistribute.

    ``volatility_exponent`` 為 None 時走 v1 原式（不經過 ``**``），保證舊 Decision run 逐位重建；
    0 代表完全不看波動度（同等級等權），1 與 v1 數值相同。
    """
    def _raw(symbol: str) -> Decimal:
        multiplier = multipliers[tiers[symbol]]
        floored = max(volatility[symbol], volatility_floor)
        if volatility_exponent is None:
            return multiplier / floored
        if volatility_exponent == 0:
            return multiplier
        return multiplier / (floored ** volatility_exponent)

    raw = {symbol: _raw(symbol) for symbol in order}
    weights = {symbol: Decimal("0") for symbol in order}
    remaining = list(order)
    left = max(budget, Decimal("0"))
    while remaining and left > 0:
        total = sum(raw[symbol] for symbol in remaining)
        proposed = {symbol: left * raw[symbol] / total for symbol in remaining}
        capped = [symbol for symbol in remaining if proposed[symbol] > limits[symbol]]
        if not capped:
            weights.update(proposed)
            break
        for symbol in capped:
            weights[symbol] = limits[symbol]
            left -= limits[symbol]
            remaining.remove(symbol)
    return weights


def alpha_floor_weights(
    order: Sequence[str],
    tiers: Mapping[str, str],
    multipliers: Mapping[str, Decimal],
    volatility: Mapping[str, Decimal],
    volatility_floor: Decimal,
    limits: Mapping[str, Decimal],
    budget: Decimal,
    volatility_exponent: Optional[Decimal],
    floors: Mapping[str, Decimal],
) -> Dict[str, Decimal]:
    """limit_scaled_conviction v1（α 方案）：高信心候選的份額至少為 max(原式份額, ``floors``)，其餘照原式並受跨等級約束。

    ``order`` 必須已依（信心、入選名次、代號）排序；``floors`` 只含 high 的股票（min(α × 單股上限, 封頂)）。
    公式見 docs/limit_aware_conviction_weights_plan.md「α 方案」：

    0. ``base`` = 原式（``conviction_weights``）把預算分給全部入選股；每檔 high 的必要份額 r = max(base, floor)。
    1. 沒有 high 被抬高、且（volatility_exponent = 0 時）base 已滿足 high ≥ medium ≥ low：直接回傳 base（逐位同原式）。
    2. 預算不足（Σ r ≥ 預算）：high 依名次各取 min(r, 剩餘預算)，用完為止，medium／low 為 0。
    3. 否則 high 固定為 r（不因別檔被抬高而縮小），剩餘預算依原式分給 medium／low；volatility_exponent = 0 時
       medium 不得高於最小的 high、low 不得高於最小的 medium（沒有 medium 時為最小的 high）。因這些天花板而
       分不完的預算留作現金（不違反封頂、總和不超過預算）。volatility_exponent > 0 不加天花板（文件揭露的例外）。
    """
    left = max(budget, Decimal("0"))
    highs = [symbol for symbol in order if symbol in floors]
    others = [symbol for symbol in order if symbol not in floors]
    ordered_tiers = volatility_exponent == 0

    def proportional(symbols: Sequence[str], amount: Decimal, caps: Mapping[str, Decimal]) -> Dict[str, Decimal]:
        return conviction_weights(
            symbols, tiers, multipliers, volatility, volatility_floor, caps, amount, volatility_exponent,
        )

    def tier_order_holds(shares: Mapping[str, Decimal]) -> bool:
        upper: Optional[Decimal] = None
        for level in CONVICTION_LEVELS:
            members = [shares[symbol] for symbol in order if tiers[symbol] == level]
            if not members:
                continue
            if upper is not None and max(members) > upper:
                return False
            upper = min(members)
        return True

    base = proportional(order, left, limits)
    required = {symbol: max(base[symbol], floors[symbol]) for symbol in highs}
    lifted = any(required[symbol] != base[symbol] for symbol in highs)
    if not lifted and (not ordered_tiers or tier_order_holds(base)):
        return base

    weights = {symbol: Decimal("0") for symbol in order}
    if sum((required[symbol] for symbol in highs), Decimal("0")) >= left:
        for symbol in highs:
            take = min(required[symbol], left)
            weights[symbol] = take
            left -= take
        return weights

    weights.update(required)
    rest = left - sum((required[symbol] for symbol in highs), Decimal("0"))
    caps = dict((symbol, limits[symbol]) for symbol in others)
    if ordered_tiers and highs:
        lowest_high = min(required[symbol] for symbol in highs)
        caps = {symbol: min(cap, lowest_high) for symbol, cap in caps.items()}
    shares = proportional(others, rest, caps)
    if ordered_tiers:
        mediums = [symbol for symbol in others if tiers[symbol] == "medium"]
        lows = [symbol for symbol in others if tiers[symbol] == "low"]
        if mediums and lows:
            lowest_medium = min(shares[symbol] for symbol in mediums)
            if any(shares[symbol] > lowest_medium for symbol in lows):
                # 調低 low 的天花板只會讓 medium 分到更多，一次重算即可；仍違反時 fail-closed。
                for symbol in lows:
                    caps[symbol] = min(caps[symbol], lowest_medium)
                shares = proportional(others, rest, caps)
    weights.update(shares)
    if ordered_tiers and not tier_order_holds(weights):
        raise DecisionToolError("limit_scaled_conviction 份額違反 high ≥ medium ≥ low")
    return weights
