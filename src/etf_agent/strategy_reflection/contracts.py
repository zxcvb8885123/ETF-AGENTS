"""策略反思旁觀者的常數、可調旋鈕與錯誤型別。

本套件讀校準報告與策略樣板，提出旋鈕調整；不被決策、配置或風控路徑匯入。
Agent 只輸出提案 JSON，是否生效、改多少由確定性程式依下列邊界決定。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Mapping, NamedTuple, Optional

SCHEMA_VERSION = "1.0"
# 這些 horizon 任一為 ok 才有提案資格；20 日 horizon 在樣本少時不可能 ok，不能要求。
GATE_HORIZONS = ("1", "5")
# 同一旋鈕套用後，要再累積這麼多個新決策日才可再調（與校準門檻分開命名）。
COOLDOWN_DECISION_DAYS = 10
MAX_PROPOSALS = 3
STATUS_OBSERVE_ONLY = "observe_only"
STATUS_PROPOSED = "proposed"
POLICY_ROOT = "var/strategy_reflection"
# 一次套用後的政策檔案由程式改寫，只改這個欄位集合；其餘欄位原樣保留。
POLICY_PATH = "config/decision_policy.json"


class ReflectionError(ValueError):
    """反思輸入不合法、提案越界、封存被竄改或回放不通過。"""


class Knob(NamedTuple):
    path: str
    kind: str  # decimal | int
    step: Decimal  # 單次最大變幅
    low: Decimal
    high: Optional[Decimal]  # None 表示上限由程式依規則決定
    tighten_only: bool = False  # 只准調低（更保守）


def _knobs() -> Dict[str, Knob]:
    knobs: Dict[str, Knob] = {}
    for level in ("high", "medium", "low"):
        knobs["conviction_multipliers.%s" % level] = Knob(
            "conviction_multipliers.%s" % level, "decimal", Decimal("0.25"), Decimal("0.1"), Decimal("3"))
    for stance in ("aggressive", "neutral", "defensive"):
        # 上限 0.22 是競賽現金建議上限，不得由反思員放寬。
        knobs["cash_buffer_by_stance.%s" % stance] = Knob(
            "cash_buffer_by_stance.%s" % stance, "decimal", Decimal("0.03"), Decimal("0"), Decimal("0.22"))
    for breadth in ("concentrated", "balanced", "diversified"):
        knobs["positions_by_breadth.%s" % breadth] = Knob(
            "positions_by_breadth.%s" % breadth, "int", Decimal("2"), Decimal("1"), None)
    for name in ("neutral_at", "defensive_at"):
        knobs["drawdown_control.%s" % name] = Knob(
            "drawdown_control.%s" % name, "decimal", Decimal("0.01"), Decimal("0.02"), None, tighten_only=True)
    knobs["volatility_floor"] = Knob("volatility_floor", "decimal", Decimal("0.005"), Decimal("0.005"), Decimal("0.05"))
    knobs["trim_fraction"] = Knob("trim_fraction", "decimal", Decimal("0.1"), Decimal("0.1"), Decimal("1"))
    return knobs


KNOBS: Dict[str, Knob] = _knobs()
# 位於 position_sizing 之下的旋鈕群；其餘在樣板頂層。
SIZING_GROUPS = ("conviction_multipliers", "cash_buffer_by_stance", "positions_by_breadth", "volatility_floor")
# 絕不可由反思員提案的欄位（硬性規則、競賽限制、模擬假設、樣板識別）。
FORBIDDEN_PREFIXES = (
    "lot_size", "commission_rate", "sell_tax_rate", "minimum_commission", "max_stock_weight", "special_weight_limits",
    "max_sector_weight", "min_positions", "max_positions", "cash_weight_ceiling", "minimum_active_share",
    "reuse_sell_proceeds", "slippage_bps", "stress_", "liquidity_fill_rate", "max_turnover_rate", "max_revisions",
    "cash_buffer_rate", "default_target_weight", "policy_id", "available_at", "content_sha256", "method", "stock_weight_fit",
)


def require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReflectionError("%s 必須是物件" % label)
    return value
