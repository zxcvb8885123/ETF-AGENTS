"""Conviction tiers, cash stance and their deterministic conversion into weights.

交易 Agent 只對 buy／add 候選給出 ``high``／``medium``／``low`` 等級，風險 Agent 只給
現金姿態與持股廣度；權重由本模組依等級乘數與 ATR 波動度確定性計算，Agent 不輸出任何權重、
股數或金額。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence

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
DRAWDOWN_DERISK_VERSION = "v1"
DRAWDOWN_DERISK_FIELDS = {"version", "max_derisk_fraction"}
DRAWDOWN_DERISK_FRACTION = Decimal("0.25")


def validate_cash_stance(context: DecisionContext, stance: object, prefix: str, errors: List[str]) -> None:
    """現金姿態：level 三選一、理由與共同輸入中存在的證據。

    舊 bundle（無 ``evidence_policy``）維持「市場層級可引用任一股票」的原行為；``typed_evidence_v1`` 下
    至少一個證據必須是市場層級（全市場情緒／恐懼貪婪／隔夜／市場狀態編號），個股行情不得單獨成立，
    且全市場情緒為 negative 時不得回 aggressive（一致性護欄；回撤下限另由 stance_floor 強制）。"""
    if not isinstance(stance, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(stance, {"level", "breadth", "rationale", "evidence_ids"}, prefix, errors)
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
        errors.append("%s 引用不存在：%s" % (prefix, ", ".join(unknown)))
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
            if derisk.get("version") != DRAWDOWN_DERISK_VERSION:
                errors.append("position_sizing.drawdown_derisk.version 只允許 %s" % DRAWDOWN_DERISK_VERSION)
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
            "cash_buffer_floor", "cash_fit_order", "cash_fit_min_fill_ratio", "cash_ceiling_fix",
            "stock_limit_headroom", "buy_gap_reserve", "buy_gap_reserve_rate", "insufficient_funds_policy",
            "sector_fit", "turnover_fit", "reentry_cooldown", "drawdown_derisk",
        },
        "DecisionPolicy.position_sizing",
        errors,
    )
    if sizing.get("method") not in SIZING_METHODS:
        errors.append("position_sizing.method 必須為 %s" % "或".join(SIZING_METHODS))
    _validate_volatility_exponent(sizing, errors)
    _validate_execution_gates(sizing, errors)
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
