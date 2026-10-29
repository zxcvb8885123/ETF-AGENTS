"""確定性工具：讀旋鈕值、套用提案、驗證樣板。LLM 不碰任何數字運算。"""

from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.decision.position_review import validate_position_review_policy
from etf_agent.decision.sizing import validate_position_sizing

from .contracts import (
    FORBIDDEN_PREFIXES,
    MAX_DRIFT_STEPS,
    KNOBS,
    POSITION_COUNT_KNOBS,
    SIZING_GROUPS,
    Knob,
    ReflectionError,
)


def _container(template: Dict[str, object], path: str):
    """回傳 (容器, 鍵)；位於 position_sizing 的旋鈕群與頂層欄位分開處理。"""
    head, _, tail = path.partition(".")
    if head in SIZING_GROUPS:
        sizing = template.get("position_sizing")
        if not isinstance(sizing, dict):
            raise ReflectionError("策略樣板缺少 position_sizing")
        return (sizing[head], tail) if tail else (sizing, head)
    if tail:
        return template[head], tail
    return template, head


def read_knob(template: Mapping[str, object], path: str):
    clone = copy.deepcopy(dict(template))
    try:
        container, key = _container(clone, path)
        return container[key]
    except (KeyError, TypeError) as error:
        raise ReflectionError("策略樣板缺少旋鈕 %s" % path) from error


def current_knobs(template: Mapping[str, object]) -> Dict[str, str]:
    """樣板目前的旋鈕值；樣板沒有的選配旋鈕（例如 v1 沒有 volatility_exponent、未啟用
    rotation 時沒有 max_swaps_per_run）直接略過，不列入也不報錯。"""
    values: Dict[str, str] = {}
    for path in sorted(KNOBS):
        try:
            value = read_knob(template, path)
        except ReflectionError:
            continue
        # 樣板明確寫 null 代表「未啟用」（例如 gain_alert），不是數值，不能列為旋鈕。
        if value is not None:
            values[path] = str(value)
    return values


def knob_origins(history: Sequence[Mapping[str, object]]) -> Dict[str, str]:
    """每個旋鈕的「人設定起點」：目前仍有效的套用紀錄中，最早一次套用前的值。

    回滾會把最近一次尚未回滾的套用從堆疊移除；整串都被還原的旋鈕不再有起點（回到人設定的值本身）。"""
    stack: List[Mapping[str, object]] = []
    for entry in history:
        if entry.get("action") == "apply":
            stack.append(entry)
        elif entry.get("action") == "rollback" and stack:
            stack.pop()
    origins: Dict[str, str] = {}
    for entry in stack:
        for knob, before in (entry.get("origins") or {}).items():  # type: ignore[union-attr]
            origins.setdefault(str(knob), str(before))
    return origins


def _number(knob: Knob, raw: object, label: str) -> Decimal:
    if isinstance(raw, bool):
        raise ReflectionError("%s 不是數值" % label)
    try:
        value = Decimal(str(raw))
    except InvalidOperation as error:
        raise ReflectionError("%s 不是數值：%r" % (label, raw)) from error
    if not value.is_finite():
        raise ReflectionError("%s 必須是有限數值" % label)
    if knob.kind == "int" and value != value.to_integral_value():
        raise ReflectionError("%s 必須是整數" % label)
    return value


def _check_enum(knob: Knob, knob_path: str, proposed: object, template: Mapping[str, object], origin: object) -> Dict[str, str]:
    """有序列舉旋鈕：目前值與提案都必須在選項內，單次只准移到相鄰一階，且離人設定起點不超過 MAX_DRIFT_STEPS 階。"""
    current = read_knob(template, knob_path)
    if current not in knob.choices or proposed not in knob.choices:
        raise ReflectionError("旋鈕 %s 的值必須是 %s" % (knob_path, "／".join(knob.choices)))
    if proposed == current:
        raise ReflectionError("旋鈕 %s 提案與目前值相同" % knob_path)
    position = knob.choices.index(str(proposed))
    if abs(position - knob.choices.index(str(current))) > 1:
        raise ReflectionError("旋鈕 %s 單次只准移到相鄰一階（目前 %s）" % (knob_path, current))
    if origin is not None and abs(position - knob.choices.index(str(origin))) > MAX_DRIFT_STEPS:
        raise ReflectionError("旋鈕 %s 離人設定的起點 %s 已超過 %d 階" % (knob_path, origin, MAX_DRIFT_STEPS))
    return {"current": str(current), "proposed": str(proposed)}


def check_proposal(
    knob_path: str, proposed: object, template: Mapping[str, object], rules: Mapping[str, object],
    origins: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """單筆提案的白名單、變幅、上下限、方向與累積偏移檢查；通過回傳 {current, proposed}。

    ``origins`` 是 ``knob_origins`` 的結果：旋鈕離人設定的起點最多偏離 ``MAX_DRIFT_STEPS`` 個 step。"""
    if any(knob_path == prefix or knob_path.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
        raise ReflectionError("旋鈕 %s 屬禁區，反思員不得提案" % knob_path)
    knob = KNOBS.get(knob_path)
    if knob is None:
        raise ReflectionError("旋鈕 %s 不在可調白名單" % knob_path)
    origin = (origins or {}).get(knob_path)
    if knob.kind == "enum":
        return _check_enum(knob, knob_path, proposed, template, origin)
    current = _number(knob, read_knob(template, knob_path), "目前 %s" % knob_path)
    value = _number(knob, proposed, "提案 %s" % knob_path)
    if value == current:
        raise ReflectionError("旋鈕 %s 提案與目前值相同" % knob_path)
    if abs(value - current) > knob.step:
        raise ReflectionError("旋鈕 %s 單次變幅 %s 超過上限 %s" % (knob_path, abs(value - current), knob.step))
    low, high = knob.low, knob.high
    # 檔數類整數旋鈕的範圍由競賽規則決定；其他整數旋鈕（例如換股上限）用自己的 low／high。
    if knob_path in POSITION_COUNT_KNOBS:
        low = max(low, Decimal(str(rules["min_positions"])))
        high = Decimal(str(rules["max_positions"]))
    if value < low or (high is not None and value > high):
        raise ReflectionError("旋鈕 %s 提案 %s 超出允許範圍 [%s, %s]" % (knob_path, value, low, high))
    if origin is not None and abs(value - Decimal(str(origin))) > knob.step * MAX_DRIFT_STEPS:
        raise ReflectionError("旋鈕 %s 離人設定的起點 %s 已超過 %d 個 step（%s）" % (knob_path, origin, MAX_DRIFT_STEPS, knob.step * MAX_DRIFT_STEPS))
    if knob.tighten_only and value > current:
        raise ReflectionError("旋鈕 %s 只准調低（更保守），提案 %s 高於目前 %s" % (knob_path, value, current))
    return {"current": str(current), "proposed": str(int(value) if knob.kind == "int" else value.normalize())}


def apply_proposals(
    template: Mapping[str, object], proposals: Sequence[Mapping[str, object]], rules: Mapping[str, object]
) -> Dict[str, object]:
    """回傳套用提案後的新樣板（深拷貝，不改原物件），並以既有 Policy 規則預檢。"""
    candidate = copy.deepcopy(dict(template))
    seen = set()
    for proposal in proposals:
        path = str(proposal["knob"])
        if path in seen:
            raise ReflectionError("同一旋鈕 %s 重複提案" % path)
        seen.add(path)
        checked = check_proposal(path, proposal["proposed"], template, rules)
        container, key = _container(candidate, path)
        kind = KNOBS[path].kind
        container[key] = int(checked["proposed"]) if kind == "int" else checked["proposed"]
    validate_template(candidate, rules)
    return candidate


def validate_template(template: Mapping[str, object], rules: Mapping[str, object]) -> None:
    """用 sizing 驗證規則預檢樣板；再補上 drawdown 順序與 neutral < defensive。"""
    policy = dict(template)
    policy["cash_weight_ceiling"] = rules["cash_weight_must_be_below"]
    errors: List[str] = []
    validate_position_sizing(policy, [], errors)
    if "position_review" in template:
        validate_position_review_policy(template["position_review"], errors)
    drawdown = template.get("drawdown_control")
    if isinstance(drawdown, Mapping):
        if Decimal(str(drawdown["neutral_at"])) >= Decimal(str(drawdown["defensive_at"])):
            errors.append("drawdown_control 必須 neutral_at < defensive_at")
    targets = (template.get("position_sizing") or {}).get("positions_by_breadth") or {}
    for level, count in targets.items():
        if not int(rules["min_positions"]) <= int(count) <= int(rules["max_positions"]):
            errors.append("positions_by_breadth.%s 超出 min／max_positions" % level)
    if errors:
        raise ReflectionError("策略樣板驗證失敗：" + "；".join(errors))
