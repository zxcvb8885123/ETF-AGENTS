"""確定性工具：讀旋鈕值、套用提案、驗證樣板。LLM 不碰任何數字運算。"""

from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Mapping, Sequence

from etf_agent.decision.sizing import validate_position_sizing

from .contracts import FORBIDDEN_PREFIXES, KNOBS, SIZING_GROUPS, Knob, ReflectionError


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
    return {path: str(read_knob(template, path)) for path in sorted(KNOBS)}


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


def check_proposal(knob_path: str, proposed: object, template: Mapping[str, object], rules: Mapping[str, object]) -> Dict[str, str]:
    """單筆提案的白名單、變幅、上下限與方向檢查；通過回傳 {current, proposed}。"""
    if any(knob_path == prefix or knob_path.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
        raise ReflectionError("旋鈕 %s 屬禁區，反思員不得提案" % knob_path)
    knob = KNOBS.get(knob_path)
    if knob is None:
        raise ReflectionError("旋鈕 %s 不在可調白名單" % knob_path)
    current = _number(knob, read_knob(template, knob_path), "目前 %s" % knob_path)
    value = _number(knob, proposed, "提案 %s" % knob_path)
    if value == current:
        raise ReflectionError("旋鈕 %s 提案與目前值相同" % knob_path)
    if abs(value - current) > knob.step:
        raise ReflectionError("旋鈕 %s 單次變幅 %s 超過上限 %s" % (knob_path, abs(value - current), knob.step))
    low, high = knob.low, knob.high
    if knob.kind == "int":
        low = max(low, Decimal(str(rules["min_positions"])))
        high = Decimal(str(rules["max_positions"]))
    if value < low or (high is not None and value > high):
        raise ReflectionError("旋鈕 %s 提案 %s 超出允許範圍 [%s, %s]" % (knob_path, value, low, high))
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
        container[key] = int(checked["proposed"]) if KNOBS[path].kind == "int" else checked["proposed"]
    validate_template(candidate, rules)
    return candidate


def validate_template(template: Mapping[str, object], rules: Mapping[str, object]) -> None:
    """用 sizing 驗證規則預檢樣板；再補上 drawdown 順序與 neutral < defensive。"""
    policy = dict(template)
    policy["cash_weight_ceiling"] = rules["cash_weight_must_be_below"]
    errors: List[str] = []
    validate_position_sizing(policy, [], errors)
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
