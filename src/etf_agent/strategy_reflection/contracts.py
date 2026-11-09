"""策略反思旁觀者的常數、可調旋鈕與錯誤型別。

本套件讀校準報告與策略樣板，提出旋鈕調整；不被決策、配置或風控路徑匯入。
Agent 只輸出提案 JSON，是否生效、改多少由確定性程式依下列邊界決定。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Mapping, NamedTuple, Optional, Tuple

from etf_agent.calibration.contracts import REPLAY_EVIDENCE_GRADE, REPLAY_NOTICE  # noqa: F401  （回放類報告共用的證據等級）

SCHEMA_VERSION = "1.0"
# 這些 horizon 任一為 ok 才有提案資格；20 日 horizon 在樣本少時不可能 ok，不能要求。
GATE_HORIZONS = ("1", "5")
# 同一旋鈕套用後，要再累積這麼多個新決策日才可再調（與校準門檻分開命名）。
COOLDOWN_DECISION_DAYS = 10
# 套用後的試用觀察期（以新決策日計）。比賽初賽只到 11/27，窗口刻意短：一輪「提案→套用→觀察」約兩週。
TRIAL_DAYS = 5
TRIAL_EXTENSION_DAYS = 3
# 試用期最長日曆天：基準資料不可用等原因讓結算一直 pending 時，超過就以 timeout 結案（保留變更、標需人工），不永久凍結反思迴圈。
TRIAL_MAX_CALENDAR_DAYS = 15
# 試用前後比較的基準窗口：套用前最近幾個交易日的「帳戶日報酬 − 等權基準日報酬」；少於下限不下結論。
BASELINE_DAYS = 10
MIN_BASELINE_DAYS = 4
# 只有「變差」超過 max(TRIAL_Z 倍的前後平均差標準誤, 此下限) 才算明確變差，避免用雜訊來回震盪。
# 標準誤 = 基準期樣本標準差 × sqrt(1/試用天數 + 1/基準天數)；舊門檻（0.5 倍標準差）只約 0.9 個標準誤，
# 無真實差異時仍有近兩成機率誤判變差而還原。
TRIAL_Z = Decimal("2")
# 絕對下限 0.0005（日超額 5 bp）：原本 0.002 等於要求 5 個觀察日平均每天輸等權基準 0.2%（一週 1%），
# 基準期幾乎只在標準誤項很大時才會被判 worse，5 日觀察期實際上觸發不了；降到 5 bp 後，基準期平靜
# （標準誤項小）時每天輸 5 bp 以上就算明確變差，統計檢定（TRIAL_Z 倍標準誤）仍是主要門檻。
TRIAL_FLOOR = Decimal("0.0005")
# 任一旋鈕離「人設定的起點」最多偏離幾個 step，比賽全程累積。
MAX_DRIFT_STEPS = 2
# 連續幾次試用被還原，就自動關掉自動套用（改回人工）。
AUTO_DISABLE_REVERTS = 2
# 影子回放：比的是「對等權基準的超額」而不是絕對 NAV（否則自動套用等於在賭市場 beta）。
# 候選策略整段的超額至少要比目前策略高出這個比例才算「有改善」；提案與回放用同一段歷史（樣本內），
# 持平不是證據；這個下限只擋「看不出差別」，不是獲利證明。
REPLAY_MIN_GAIN = Decimal("0.0001")
# 回放期間切成這麼多段不重疊子區間，候選在每一段都不得劣於等權基準、也不得劣於目前策略；
# 成交日不足以切出這麼多段（每段至少一個成交日）就不通過，不以單段結果放行。
REPLAY_MIN_SEGMENTS = 2
REPLAY_BENCHMARK_ID = "universe_equal_weight"
# 等權基準每個相鄰交易日至少要有這個比例的交易池股票兩天都有官方收盤價，否則基準不可用、回放閘門不通過。
REPLAY_BENCHMARK_MIN_COVERAGE = Decimal("0.8")
# 回放只固定已封存的 Agent 意圖重算確定性配置：模型對過去日期可能有訓練資料記憶（前視偏誤），
# 所以回放結果不構成對 LLM 判斷的前瞻驗證；證據等級 REPLAY_EVIDENCE_GRADE／REPLAY_NOTICE 沿用 calibration 的常數
# （頂部匯入），所有回放類報告都標同一個值。
# 旋鈕變更的風險方向：只有「降風險」方向可由程式自動套用；加風險或方向不明的提案只封存並標 needs_human，
# 需人用明確指令（apply-pending --allow-risk-increase <reflection_id>）才套用。
RISK_REDUCING = "risk_reducing"
RISK_INCREASING = "risk_increasing"
RISK_AMBIGUOUS = "ambiguous"
NEEDS_HUMAN_ACTION = "needs_human"
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
    # 哪個方向是「降風險」：+1 調高降風險、-1 調低降風險、0 語意不明（任何方向都要人決定）。
    risk_reducing: int = 0


def _knobs() -> Dict[str, Knob]:
    knobs: Dict[str, Knob] = {}
    for level in ("high", "medium", "low"):
        # 信心乘數越低，單檔集中度與整體曝險越低：調低＝降風險。
        knobs["conviction_multipliers.%s" % level] = Knob(
            "conviction_multipliers.%s" % level, "decimal", Decimal("0.10"), Decimal("0.1"), Decimal("3"), risk_reducing=-1)
    for stance in ("aggressive", "neutral", "defensive"):
        # 上限 0.22 是競賽現金建議上限，不得由反思員放寬。現金緩衝越高曝險越低：調高＝降風險。
        knobs["cash_buffer_by_stance.%s" % stance] = Knob(
            "cash_buffer_by_stance.%s" % stance, "decimal", Decimal("0.02"), Decimal("0"), Decimal("0.22"), risk_reducing=+1)
    for breadth in ("concentrated", "balanced", "diversified"):
        # 檔數越多越分散、單檔權重越低：調高＝降風險。
        knobs["positions_by_breadth.%s" % breadth] = Knob(
            "positions_by_breadth.%s" % breadth, "int", Decimal("1"), Decimal("1"), None, risk_reducing=+1)
    for name in ("neutral_at", "defensive_at"):
        # 回撤門檻越低越早進入防守：調低＝降風險（本來就只准調低）。
        knobs["drawdown_control.%s" % name] = Knob(
            "drawdown_control.%s" % name, "decimal", Decimal("0.01"), Decimal("0.02"), None, tighten_only=True, risk_reducing=-1)
    # 波動下限同時影響低波動股的權重上限與整體分散度，調高調低都說不上是單純的降風險：方向不明，一律要人決定。
    knobs["volatility_floor"] = Knob("volatility_floor", "decimal", Decimal("0.005"), Decimal("0.005"), Decimal("0.05"), risk_reducing=0)
    # 減碼比例越高，減碼時曝險降得越快：調高＝降風險。
    knobs["trim_fraction"] = Knob("trim_fraction", "decimal", Decimal("0.05"), Decimal("0.1"), Decimal("1"), risk_reducing=+1)
    # 低波動偏好的強度（0 完全不看波動度、1 與 v1 相同）；反思員依校準報告調整，單次最多 0.10。
    # 指數越高越偏向低波動股（逆波動配置越強）：調高＝降風險。
    knobs["volatility_exponent"] = Knob(
        "volatility_exponent", "decimal", Decimal("0.10"), Decimal("0"), Decimal("1"), risk_reducing=+1)
    # 一天最多幾個出場名額可給未持有的新股；上限 10 與競賽換手成本相稱，單次最多改 1 檔。換股越少換手越低：調低＝降風險。
    knobs["rotation.max_swaps_per_run"] = Knob(
        "rotation.max_swaps_per_run", "int", Decimal("1"), Decimal("0"), Decimal("10"), risk_reducing=-1)
    # 出場釋出的名額只給信心不低於此等級的新股；有序列舉，單次移一階（回放可重算）。門檻越高換股越少：調高＝降風險。
    knobs["rotation.min_new_conviction"] = Knob(
        "rotation.min_new_conviction", "enum", Decimal("1"), Decimal("0"), None, choices=("low", "medium", "high"), risk_reducing=+1)
    # 停損／停利警示門檻只影響交易 Agent 的判斷，影子回放驗不了，所以走試用觀察期；gain_alert 啟用與否由人決定。
    # 警示門檻越低越早提醒檢視：調低＝降風險。
    knobs["position_review.loss_alert"] = Knob(
        "position_review.loss_alert", "decimal", Decimal("0.01"), Decimal("0.05"), Decimal("0.20"), verification="observe", risk_reducing=-1)
    knobs["position_review.retrace_alert_20d"] = Knob(
        "position_review.retrace_alert_20d", "decimal", Decimal("0.01"), Decimal("0.06"), Decimal("0.25"), verification="observe", risk_reducing=-1)
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
    "held_add_priority", "rotation.version", "rotation.forced_swap", "position_review.version", "position_review.gain_alert",
    "selection_ranking", "selection_tiebreak", "rank_by_symbol", "cash_buffer_floor",
    # 2026-11-03 新增的執行面閘門與其比例參數（買單順序與塵埃取消、現金上限補救、單股封頂空間、跳空預留、
    # 結算現金不足整批作廢）：都切換程式路徑或對應主辦方結算口徑，只由人設定。
    "cash_fit_order", "cash_fit_min_fill_ratio", "high_conviction_cash_priority", "cash_ceiling_fix", "stock_limit_headroom", "buy_gap_reserve",
    "insufficient_funds_policy", "held_weight_budget",
    # 2026-11-03 新增的風控閘門：產業上限只擋增量、換手上限主動縮減、出場冷卻、回撤防守減碼與解除門檻、強制換股需空頭證據。
    "sector_fit", "turnover_fit", "reentry_cooldown", "drawdown_derisk",
    "drawdown_control.release_neutral_below", "drawdown_control.release_defensive_below",
    # 2026-11-05 新增：高信心現金下限（含 floor_ratio）與依單股上限填充高信心份額（含 alpha），只由人設定。
    "high_conviction_cash_floor", "limit_scaled_conviction",
    # 2026-11-05 新增：姿態內現金子等級（grid、max_daily_raise 與 pipeline 綁定的 anchor）與綁定的 cash_grade，只由人設定；
    # 閘門開啟時 cash_buffer_by_stance 旋鈕不再影響配置，apply-pending 一律標 needs_human（見 service.apply_pending）。
    "cash_ratio_range", "cash_grade",
    # 2026-11-09 新增：風險 Agent 現金姿態的資料新舊時間表與 recency_reconciliation 列舉，切換程式路徑，只由人設定。
    "cash_stance_recency",
)


def require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReflectionError("%s 必須是物件" % label)
    return value
