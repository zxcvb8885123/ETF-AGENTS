"""高風險股上限：``position_sizing.high_risk_cap`` v1（版本閘門，只由人設定；策略反思不得改）。

起因：2026-11-12～11-13 的換股把低波動持股換成高 beta 電子股，之後大盤一週跌約兩成，換入者平均多跌約 8 個百分點。
計畫見 ``docs/plans/risk_bucket_cap_plan.md``（``market_regime_front_plan.md`` 階段 1 的精簡版）。

- **不預測大盤**：三種現金姿態用同一組人填的數字。只降波動，不提高期望報酬。
- **風險桶（程式計算）**：beta（``exposure_rotation.beta_window_days``，對交易池等權，``BetaModel``）達 ``high.beta_at_least``
  或 ATR14 占比達 ``high.atr_pct_at_least`` 即為高風險；**缺 beta 或 ATR 一律當高風險**（不補值）。
- **上限**：成交後高風險股總權重不超過 ``max_high_risk_weight``；當天新買進（原本未持有）的高風險股不超過
  ``max_new_high_risk_entries`` 檔。
- **只擋買、不賣**：只縮減或取消買單（含換股的換入者、一般買進與加碼），不碰賣單、不改寫交易 Agent 的意圖。
  換入者被取消時，換股復原迴圈以原因 ``high_risk_cap`` 取消該對、換出者不賣。
- **硬規則優先**：縮減若會讓期末現金達競賽上限，或讓持股少於 ``min_positions``，就停止縮減並留
  ``HIGH_RISK_CAP_RELAXED:<原因>``，不讓 Guard 因本閘門拒絕。
- ``scope``：``all`` 每天套用；``defensive`` 只在風險 Agent 本次綁定的現金姿態（``position_sizing.cash_stance``）為
  ``defensive`` 時套用（「何時踩煞車」交給 Agent 既有的列舉判斷，人只填踩多重），姿態尚未綁定即拋錯、不猜成非防守。
- ``mode=record`` 只計算、只在 ``PORTFOLIO`` 旗標寫下「若開啟會怎麼縮」，配置完全不變；``mode=enforce`` 才真的縮。
- 釋出的現金留在現金，不再分配。沒有此欄位的舊 Policy／舊 run 逐位元維持原行為；Finalizer 與 ProposalValidator
  以同一份 Policy 整份重跑配置引擎，所以不需要另存事實。
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Mapping, Optional

from .fields import DecisionToolError, decimal_value, reject_unknown_fields

HIGH_RISK_CAP_V1 = "v1"
HIGH_RISK_CAP_MODES = ("record", "enforce")
HIGH_RISK_CAP_SCOPES = ("all", "defensive")
HIGH_RISK_CAP_FIELDS = frozenset({"version", "mode", "scope", "high", "max_high_risk_weight", "max_new_high_risk_entries"})
HIGH_RISK_THRESHOLD_FIELDS = frozenset({"beta_at_least", "atr_pct_at_least"})
HIGH_RISK_CAP_CODE = "RISK_HIGH_RISK_CAP"
HIGH_RISK_CAP_RELAXED_CODE = "HIGH_RISK_CAP_RELAXED"
HIGH_RISK_CAP_RECORD_CODE = "HIGH_RISK_CAP_RECORD"
HIGH_RISK_CAP_STATUS_CODE = "HIGH_RISK_CAP_STATUS"
HIGH_RISK_CAP_CANCEL_REASON = "high_risk_cap"
# beta 門檻的合理上界（只擋明顯打錯的數字，不是建議值）。
MAX_BETA_THRESHOLD = Decimal("5")


def high_risk_cap_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """啟用時回傳閘門物件；沒有（或形狀不合，已被驗證拒絕）回 None。"""
    gate = sizing.get("high_risk_cap") if isinstance(sizing, Mapping) else None
    if isinstance(gate, Mapping) and gate.get("version") == HIGH_RISK_CAP_V1:
        return gate
    return None


def _ratio(raw: object, label: str, errors: List[str], upper: Decimal) -> Optional[Decimal]:
    if isinstance(raw, bool) or not isinstance(raw, str):
        errors.append("%s 必須是數字字串（數值由人填，程式不設預設）" % label)
        return None
    try:
        value = decimal_value(raw, label)
    except DecisionToolError as error:
        errors.append(str(error))
        return None
    if value <= 0 or value > upper:
        errors.append("%s 必須大於 0 且不高於 %s" % (label, upper))
        return None
    return value


def validate_high_risk_cap_gate(policy: Mapping[str, object], errors: List[str]) -> None:
    sizing = policy.get("position_sizing") or {}
    gate = sizing.get("high_risk_cap")  # type: ignore[union-attr]
    prefix = "position_sizing.high_risk_cap"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, set(HIGH_RISK_CAP_FIELDS), prefix, errors)
    for field in sorted(HIGH_RISK_CAP_FIELDS):
        if field not in gate:
            errors.append("%s 缺少 %s（數值由人填，程式不設預設）" % (prefix, field))
    if gate.get("version") != HIGH_RISK_CAP_V1:
        errors.append("%s.version 只允許 %s" % (prefix, HIGH_RISK_CAP_V1))
    if "mode" in gate and gate.get("mode") not in HIGH_RISK_CAP_MODES:
        errors.append("%s.mode 只允許 %s" % (prefix, "、".join(HIGH_RISK_CAP_MODES)))
    if "scope" in gate and gate.get("scope") not in HIGH_RISK_CAP_SCOPES:
        errors.append("%s.scope 只允許 %s" % (prefix, "、".join(HIGH_RISK_CAP_SCOPES)))
    high = gate.get("high")
    if "high" in gate:
        if not isinstance(high, Mapping):
            errors.append("%s.high 必須是物件" % prefix)
        else:
            reject_unknown_fields(high, set(HIGH_RISK_THRESHOLD_FIELDS), prefix + ".high", errors)
            for field in sorted(HIGH_RISK_THRESHOLD_FIELDS):
                if field not in high:
                    errors.append("%s.high 缺少 %s（數值由人填，程式不設預設）" % (prefix, field))
            if "beta_at_least" in high:
                _ratio(high["beta_at_least"], prefix + ".high.beta_at_least", errors, MAX_BETA_THRESHOLD)
            if "atr_pct_at_least" in high:
                _ratio(high["atr_pct_at_least"], prefix + ".high.atr_pct_at_least", errors, Decimal(1))
    if "max_high_risk_weight" in gate:
        _ratio(gate["max_high_risk_weight"], prefix + ".max_high_risk_weight", errors, Decimal(1))
    if "max_new_high_risk_entries" in gate:
        entries = gate["max_new_high_risk_entries"]
        if isinstance(entries, bool) or not isinstance(entries, int) or entries < 0:
            errors.append("%s.max_new_high_risk_entries 必須是 0 以上的整數" % prefix)
