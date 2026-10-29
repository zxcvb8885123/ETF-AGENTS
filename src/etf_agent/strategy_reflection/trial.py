"""試用觀察期（確定性）：套用後觀察新決策日的表現，明確變差才還原。

影子回放只能重放已封存的 Agent 輸出，驗不了停損／停利門檻這類影響 LLM 判斷的旋鈕；就算回放驗得了的旋鈕，
也只是「必要條件，不是獲利證明」。所以每次套用都附一段試用期：比較套用前後「帳戶日報酬 − 等權基準日報酬」。

這是護欄，不是證明：比賽初賽只有 24 個交易日，前後各只有幾天，雜訊遠大於多數旋鈕的效果。因此只有
**明確變差**（差距超過基準期標準差的一半與 ``TRIAL_FLOOR`` 中較大者）才還原；看不出差別就保留。
本模組只含純函式，不讀寫任何檔案。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence

from .contracts import (
    BASELINE_DAYS, KNOBS, MIN_BASELINE_DAYS, TRIAL_EXTENSION_DAYS, TRIAL_FLOOR, TRIAL_DAYS, VERIFICATION_OBSERVE,
    VERIFICATION_REPLAY,
)

EQUAL_WEIGHT_ID = "universe_equal_weight"


def trial_verification(knobs: Sequence[str]) -> str:
    """一次套用只要含任何一個回放驗不了的旋鈕，整次就走最嚴格的 observe 規則。"""
    return VERIFICATION_OBSERVE if any(KNOBS[knob].verification == VERIFICATION_OBSERVE for knob in knobs if knob in KNOBS) else VERIFICATION_REPLAY


def open_trial(history: Sequence[Mapping[str, object]]) -> Optional[Mapping[str, object]]:
    """尚未結案的試用：最近一次仍有效（未被回滾）的套用，帶 ``trial`` 且沒有 final 的結案紀錄。

    旋鈕套用（``apply``，由 ``rollback`` 還原）與教訓套用（``lessons_apply``，由 ``lessons_revert`` 還原）各有一個堆疊；
    兩種變更不會同時在試用（套用前會檢查），所以回傳其中最新的未結案者。"""
    stacks: Dict[str, List[Mapping[str, object]]] = {"apply": [], "lessons_apply": []}
    undo = {"rollback": "apply", "lessons_revert": "lessons_apply"}
    settled = set()
    for entry in history:
        action = str(entry.get("action"))
        if action in stacks:
            stacks[action].append(entry)
        elif action in undo and stacks[undo[action]]:
            stacks[undo[action]].pop()
        elif action == "trial_result" and entry.get("final"):
            settled.add(str(entry.get("of")))
    open_entries = [entry for stack in stacks.values() for entry in stack
                    if entry.get("trial") and str(entry.get("reflection_id")) not in settled]
    return max(open_entries, key=lambda entry: str(entry["applied_at"])) if open_entries else None


def extensions(history: Sequence[Mapping[str, object]], reflection_id: str) -> int:
    return sum(1 for entry in history if entry.get("action") == "trial_result" and entry.get("of") == reflection_id and entry.get("result") == "extended")


def consecutive_reverts(history: Sequence[Mapping[str, object]]) -> int:
    """最近連續幾次試用結案都是還原（中間遇到保留就歸零）。"""
    count = 0
    for entry in reversed(history):
        if entry.get("action") == "trial_result" and entry.get("final"):
            if entry.get("result") != "reverted":
                break
            count += 1
    return count


def excess_series(comparison: Mapping[str, object], benchmark_id: str = EQUAL_WEIGHT_ID) -> Dict[str, Decimal]:
    """帳戶與等權基準逐日報酬之差。兩邊 NAV 都存在的相鄰交易日才算；基準不可用時回空字典。"""
    if comparison.get("status") != "ok":
        return {}
    bench = next((item for item in comparison.get("benchmarks", []) if item.get("benchmark_id") == benchmark_id and item.get("status") == "ok"), None)
    if bench is None:
        return {}
    account = {str(point["trade_date"]): Decimal(str(point["nav"])) for point in comparison["account"]["series"]}  # type: ignore[index]
    benchmark = {str(point["trade_date"]): Decimal(str(point["nav"])) for point in bench["series"] if point.get("nav") is not None}
    # 以帳戶的交易日序列為準：相鄰兩個交易日兩邊都有 NAV 才算一筆日超額報酬。基準中間缺一天時，
    # 不能把跨多日的累積報酬當成單日報酬，所以跳過該筆（寧可少一筆也不混入錯誤的值）。
    dates = sorted(account)
    result: Dict[str, Decimal] = {}
    for previous, current in zip(dates, dates[1:]):
        if previous in benchmark and current in benchmark and account[previous] > 0 and benchmark[previous] > 0:
            result[current] = account[current] / account[previous] - benchmark[current] / benchmark[previous]
    return result


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _stdev(values: Sequence[Decimal]) -> Decimal:
    center = _mean(values)
    return (sum(((value - center) ** 2 for value in values), Decimal(0)) / Decimal(len(values))).sqrt()


def evaluate_trial(entry: Mapping[str, object], excess: Mapping[str, Decimal], extended: bool = False) -> Dict[str, object]:
    """比較套用前後的平均日超額報酬。

    套用日（含）之前最近 ``BASELINE_DAYS`` 個交易日是基準期；套用發生在決策之後（週六早上，或平日 09:00 之後），
    之後的第一個決策才讀到新策略，其成交日已經在套用日之後，所以套用日之後的每個交易日都算試用期。
    至少要有 ``TRIAL_DAYS``（延長一次再加 ``TRIAL_EXTENSION_DAYS``）個交易日才下結論。

    判定：明顯變差（低於 -門檻）→ ``worse``；明顯變好（高於 +門檻）→ ``not_worse``；落在 ±門檻之內＝看不出差別
    → ``inconclusive``（由呼叫端延長一次，仍看不出差別時，回放驗得了的旋鈕保留、驗不了的還原）。"""
    opened_on = str(entry["applied_at"])[:10]
    dates = sorted(excess)
    split = sum(1 for date in dates if date <= opened_on)
    before = [excess[date] for date in dates[max(0, split - BASELINE_DAYS):split]]
    after = [excess[date] for date in dates[split:]]
    need = TRIAL_DAYS + (TRIAL_EXTENSION_DAYS if extended else 0)
    body: Dict[str, object] = {"before_days": len(before), "after_days": len(after), "needed_after_days": need}
    if len(after) < need:
        return {**body, "verdict": "pending"}
    if len(before) < MIN_BASELINE_DAYS:
        return {**body, "verdict": "inconclusive", "reason": "套用前的基準期只有 %d 個交易日（至少 %d）" % (len(before), MIN_BASELINE_DAYS)}
    delta = _mean(after) - _mean(before)
    threshold = max(_stdev(before) / 2, TRIAL_FLOOR)
    body.update(mean_before=str(_mean(before).quantize(Decimal("0.000001"))), mean_after=str(_mean(after).quantize(Decimal("0.000001"))),
                delta=str(delta.quantize(Decimal("0.000001"))), threshold=str(threshold.quantize(Decimal("0.000001"))))
    if delta < -threshold:
        return {**body, "verdict": "worse"}
    return {**body, "verdict": "not_worse" if delta > threshold else "inconclusive", "reason": "平均日超額變化在門檻之內，看不出差別" if abs(delta) <= threshold else None}
