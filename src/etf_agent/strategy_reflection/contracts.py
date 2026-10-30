"""策略反思旁觀者的常數、可調旋鈕與錯誤型別。

本套件讀校準報告與策略樣板，提出旋鈕調整；不被決策、配置或風控路徑匯入。
Agent 只輸出提案 JSON，是否生效、改多少由確定性程式依下列邊界決定。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Mapping, NamedTuple, Optional, Tuple

SCHEMA_VERSION = "1.0"
# 這些 horizon 任一為 ok 才有提案資格；20 日 horizon 在樣本少時不可能 ok，不能要求。
GATE_HORIZONS = ("1", "5")
# 同一旋鈕套用後，要再累積這麼多個新決策日才可再調（與校準門檻分開命名）。
COOLDOWN_DECISION_DAYS = 10
# 套用後的試用觀察期（以新決策日計）。比賽初賽只到 11/27，窗口刻意短：一輪「提案→套用→觀察」約兩週。
TRIAL_DAYS = 5
TRIAL_EXTENSION_DAYS = 3
# 試用前後比較的基準窗口：套用前最近幾個交易日的「帳戶日報酬 − 等權基準日報酬」；少於下限不下結論。
BASELINE_DAYS = 10
MIN_BASELINE_DAYS = 4
# 只有「變差」超過 max(0.5 倍基準期標準差, 此下限) 才算明確變差，避免用雜訊來回震盪。
TRIAL_FLOOR = Decimal("0.002")
# 任一旋鈕離「人設定的起點」最多偏離幾個 step，比賽全程累積。
MAX_DRIFT_STEPS = 2
# 連續幾次試用被還原，就自動關掉自動套用（改回人工）。
AUTO_DISABLE_REVERTS = 2
SWITCH_PATH = "config/strategy_reflection.json"
VERIFICATION_REPLAY = "replay"  # 影子回放能重算（只改確定性配置路徑）
VERIFICATION_OBSERVE = "observe"  # 只影響 LLM 判斷，回放驗不了；必須走試用觀察期，且每週最多一個
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
    choices: Tuple[str, ...] = ()  # kind=enum：有序選項（由低到高），單次只准移到相鄰一階
    verification: str = "replay"


def _knobs() -> Dict[str, Knob]:
    knobs: Dict[str, Knob] = {}
    for level in ("high", "medium", "low"):
        knobs["conviction_multipliers.%s" % level] = Knob(
            "conviction_multipliers.%s" % level, "decimal", Decimal("0.10"), Decimal("0.1"), Decimal("3"))
    for stance in ("aggressive", "neutral", "defensive"):
        # 上限 0.22 是競賽現金建議上限，不得由反思員放寬。
        knobs["cash_buffer_by_stance.%s" % stance] = Knob(
            "cash_buffer_by_stance.%s" % stance, "decimal", Decimal("0.02"), Decimal("0"), Decimal("0.22"))
    for breadth in ("concentrated", "balanced", "diversified"):
        knobs["positions_by_breadth.%s" % breadth] = Knob(
            "positions_by_breadth.%s" % breadth, "int", Decimal("1"), Decimal("1"), None)
    for name in ("neutral_at", "defensive_at"):
        knobs["drawdown_control.%s" % name] = Knob(
            "drawdown_control.%s" % name, "decimal", Decimal("0.01"), Decimal("0.02"), None, tighten_only=True)
    knobs["volatility_floor"] = Knob("volatility_floor", "decimal", Decimal("0.005"), Decimal("0.005"), Decimal("0.05"))
    knobs["trim_fraction"] = Knob("trim_fraction", "decimal", Decimal("0.05"), Decimal("0.1"), Decimal("1"))
    # 低波動偏好的強度（0 完全不看波動度、1 與 v1 相同）；反思員依校準報告調整，單次最多 0.10。
    knobs["volatility_exponent"] = Knob(
        "volatility_exponent", "decimal", Decimal("0.10"), Decimal("0"), Decimal("1"))
    # 一天最多幾個出場名額可給未持有的新股；上限 10 與競賽換手成本相稱，單次最多改 1 檔。
    knobs["rotation.max_swaps_per_run"] = Knob(
        "rotation.max_swaps_per_run", "int", Decimal("1"), Decimal("0"), Decimal("10"))
    # 出場釋出的名額只給信心不低於此等級的新股；有序列舉，單次移一階（回放可重算）。
    knobs["rotation.min_new_conviction"] = Knob(
        "rotation.min_new_conviction", "enum", Decimal("1"), Decimal("0"), None, choices=("low", "medium", "high"))
    # 停損／停利警示門檻只影響交易 Agent 的判斷，影子回放驗不了，所以走試用觀察期；gain_alert 啟用與否由人決定。
    knobs["position_review.loss_alert"] = Knob(
        "position_review.loss_alert", "decimal", Decimal("0.01"), Decimal("0.05"), Decimal("0.20"), verification="observe")
    knobs["position_review.retrace_alert_20d"] = Knob(
        "position_review.retrace_alert_20d", "decimal", Decimal("0.01"), Decimal("0.06"), Decimal("0.25"), verification="observe")
    return knobs


KNOBS: Dict[str, Knob] = _knobs()
# 位於 position_sizing 之下的旋鈕群；其餘在樣板頂層。
SIZING_GROUPS = (
    "conviction_multipliers", "cash_buffer_by_stance", "positions_by_breadth", "volatility_floor",
    "volatility_exponent", "rotation",
)
# 以 min_positions／max_positions 夾範圍的整數旋鈕群（檔數類）；其他整數旋鈕用自己的 low／high。
POSITION_COUNT_KNOBS = tuple("positions_by_breadth.%s" % level for level in ("concentrated", "balanced", "diversified"))
# 絕不可由反思員提案的欄位（硬性規則、競賽限制、模擬假設、樣板識別）。
FORBIDDEN_PREFIXES = (
    "lot_size", "commission_rate", "sell_tax_rate", "minimum_commission", "max_stock_weight", "special_weight_limits",
    "max_sector_weight", "min_positions", "max_positions", "cash_weight_ceiling", "minimum_active_share",
    "reuse_sell_proceeds", "slippage_bps", "stress_", "liquidity_fill_rate", "max_turnover_rate", "max_revisions",
    "cash_buffer_rate", "default_target_weight", "policy_id", "available_at", "content_sha256", "method", "stock_weight_fit",
    # 版本閘門只由人設定：這些不是調整幅度，而是切換程式路徑（與 method、stock_weight_fit 同性質）。
    # 低波動偏好強度 volatility_exponent 與換股上限 rotation.max_swaps_per_run 是正式旋鈕，見 KNOBS。
    "held_add_priority", "rotation.version", "position_review.version", "position_review.gain_alert",
    "selection_ranking", "selection_tiebreak", "rank_by_symbol",
)


def require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReflectionError("%s 必須是物件" % label)
    return value
