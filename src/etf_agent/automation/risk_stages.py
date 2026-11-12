"""每日決策鏈的風險階段：配置前現金姿態與每次修正的風險審查。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.decision import (
    MARKET_REGIME_EVIDENCE_ID, DecisionToolError, MomentumEngine, RiskReviewValidator, artifact_content_sha256, canonical_sha256,
    liquidity_exposure, sector_exposure,
)
from etf_agent.decision.contracts import DecisionContext
from etf_agent.decision.drawdown import stance_meets_floor
from etf_agent.decision.recency import evidence_recency
from etf_agent.decision.benchmark_facts import risk_benchmark_brief
from etf_agent.decision.market_index import market_index_facts
from etf_agent.decision.exposure_facts import exposure_view_agent_led, risk_exposure_brief
from etf_agent.decision.offense import cash_glide_brief, normalize_offense_stance, offense_brief
from etf_agent.decision.cash_stance_brief import (
    BRIEF_PROMPT, REFERENCE_PHRASE, REFERENCE_PHRASE_OMITTED, company_events_summary, offense_reference_omitted, reorder_brief,
)
from etf_agent.decision.turn_facts import turn_facts
from etf_agent.decision.quality_dip_ruling import (
    DEGRADED_STATUSES, STATUS_NOT_REQUIRED, STATUS_SKIPPED, QualityDipRulingValidator, expected_program_status, program_ruling,
    quality_dip_ruling_brief, ruling_envelope, seal_quality_dip_ruling,
)
from etf_agent.decision.risk import CASH_BUFFER_CEILING_MARGIN
from etf_agent.decision.market_scope import market_sentiment_channel, overnight_market_channel
from etf_agent.decision.sizing import (
    CASH_STANCES, POSITION_BREADTHS, allowed_cash_grades, cash_grade_grid, cash_ratio_gate, revision_bounds_gate, validate_cash_stance,
)

from .agent_runner import RISK_INDEPENDENT_DENY_GLOBS, AgentTask, DailyPipelineError
from .agent_schemas import (
    CASH_GRADE_STANCE_SCHEMA, CASH_STANCE_SCHEMA, QUALITY_DIP_RULING_SCHEMA, REVIEW_SCHEMA, _SHARED_RULES, cash_stance_recency_schema,
    cash_stance_turn_facts_schema, exposure_view_schema, offense_stance_schema,
)

# 閘門段落只說「本次簡報含哪個欄位」並指向 cash-stance.md 的對應節；規則本身只寫在那裡（與 validate_cash_stance 同步）。
OVERNIGHT_PROMPT = (
    "overnight_market 是隔夜市場背景，只作現金姿態背景之一，用法見 cash-stance.md「overnight_market」節。"
)


TYPED_STANCE_PROMPT = (
    "摘要含 market_level_evidence_ids：evidence_ids 至少一個市場層級證據，全市場情緒 negative 時姿態為 neutral 或 defensive，"
    "見 cash-stance.md「market_level_evidence_ids」節。"
)


SOFT_FLOOR_PROMPT = (
    "摘要含 high_conviction_cash_floor_by_stance（各姿態的高信心現金下限），把該姿態的實際現金落點寫進 rationale，"
    "見 cash-stance.md 對應節。"
)


def _revision_bounds_text(policy: Mapping[str, object]) -> str:
    """revision_bounds 啟用時附上修正動作的下限與步距；沒有閘門回空字串（提示文字逐字不變）。"""
    gate = revision_bounds_gate(policy.get("position_sizing") or {})
    if gate is None:
        return ""
    return (
        "revise 的 reduce_max_stock_weight 之 value 不得低於 %s、reduce_turnover_limit 之 value 不得低於 %s，且都必須是 %s 的整數倍"
        "（special_weight_limits 內的股票不受 reduce_max_stock_weight 影響，要降低它請用 remove_candidate 或 increase_cash_buffer）。"
        % (gate.get("min_max_stock_weight"), gate.get("min_max_turnover_rate"), gate.get("step"))
    )


def _soft_floors(
    cash_buffers: Optional[Mapping[str, object]], gate: Optional[Mapping[str, object]]
) -> Optional[Dict[str, str]]:
    """high_conviction_cash_floor 啟用時，各姿態的高信心現金下限（緩衝 × floor_ratio）；沒有閘門或缺緩衝回 None。"""
    if not cash_buffers or not isinstance(gate, Mapping) or "floor_ratio" not in gate:
        return None
    try:
        ratio = Decimal(str(gate["floor_ratio"]))
        return {stance: str(Decimal(str(value)) * ratio) for stance, value in cash_buffers.items()}
    except InvalidOperation:
        raise DailyPipelineError("position_sizing.high_conviction_cash_floor.floor_ratio 無法解析")


RECENCY_PROMPT = (
    "摘要開頭含 evidence_recency（程式依內容時間分組的證據時間表）：另輸出 recency_reconciliation（列舉），"
    "並在最新一組有證據時至少引用其中一個，規則見 cash-stance.md「evidence_recency」節。"
)


MARKET_INDEX_PROMPT = (
    "摘要含 market_index_facts（程式依證交所加權指數算的當日收盤、漲跌、近 5／20 日報酬與離高點回檔，是背景事實、"
    "沒有 evidence_id、不可引用）：判斷現金姿態時一併參考，但單日漲跌不代表趨勢，不要只憑它改變姿態。規則見 cash-stance.md「market_index_facts」節。"
)

BENCHMARK_PROMPT = (
    "摘要含 benchmark_facts（程式算的 0050 近 1／5 日報酬、持股對 0050 的當日貢獻差、前幾大成分的權重與組合權重差額、組合對 0050 的 beta，"
    "是背景事實、沒有 evidence_id、不可引用，理由也不要寫出這個鍵名或其區塊鍵名；缺資料的區塊標 unavailable、不補值）：判斷現金姿態時一併參考。"
    "這是事實不是目標：規則上單股上限 10%（2330 為 25%），帳戶無法複製 0050 的集中度；落後 0050 可能是錯過反彈、也可能只是不追高，"
    "兩種錯誤都有成本，不要只憑與 0050 的差距改變姿態。規則見 cash-stance.md「benchmark_facts」節。"
)

TURN_FACTS_PROMPT = (
    "摘要含 turn_facts（程式依行情算的交易池轉向事實，是事實不是訊號）：另輸出 turn_assessment"
    "（no_turn_evidence／turn_unconfirmed／turn_confirmed），只是紀錄你怎麼看，不要求與現金姿態一致，不影響現金格點；"
    "若摘要另附程式狀態機結果，你的列舉才會與程式狀態取較保守者、決定曝險換股是否放寬。規則見 cash-stance.md「turn_facts」節。"
)


TURN_STATE_PROMPT_V2 = (
    "摘要另含 turn_state（v2：程式整理的最近幾日事實與資料品質，沒有狀態、沒有門檻、沒有證據編號、不可引用）：你的 turn_assessment 是"
    "唯一決定防守姿態下曝險換股、現金比例與換手上限是否放寬的依據（turn_unconfirmed 對映候選、turn_confirmed 對映確認），"
    "程式只在 data_status 為 unavailable 或回撤下限為 defensive 時把它壓回不放寬；regime 不是 bear 不會擋。"
    "反彈可能失敗，證據不足就填 no_turn_evidence 或 turn_unconfirmed，不要為了追上大盤而升級。規則見 cash-stance.md「turn_state」節。"
)


TURN_STATE_PROMPT = (
    "摘要另含 turn_state（程式狀態機的結果，只是背景，沒有證據編號、不可引用）：你的 turn_assessment 與它取較保守者，"
    "才決定防守姿態下的曝險換股是否放寬；你認為尚未轉向就填 no_turn_evidence 或 turn_unconfirmed，不要為了配合它而升級，"
    "規則見 cash-stance.md「turn_state」節。"
)


EXPOSURE_VIEW_PROMPT = (
    "摘要含 exposure_facts（程式算的組合 beta、低 beta 占比、現金權重、持股產業權重與最近一個交易池上漲日的報酬，是背景事實、"
    "沒有 evidence_id、不可引用，理由也不要寫出 exposure_facts 這個鍵名）：另輸出 exposure_view（keep_defensive／start_rebuild／rebuild），"
    "只是紀錄你認為組合該不該開始回補市場參與度，不改現金姿態、現金比例、持股廣度或換股；不是 keep_defensive 時 evidence_ids "
    "至少引用一個市場層級證據。反彈可能失敗，證據不足就填 keep_defensive。規則見 cash-stance.md「exposure_facts」節。"
)


OFFENSE_STANCE_ATTEMPTS = 3  # offense 啟用時現金姿態的驗證嘗試上限（第一次加兩次帶回饋的重試）
# 改等級時殘留欄位是最常見的退回原因；只在 offense 啟用時附在簡報提示與驗證回饋（中性提醒，不引導方向）。
OFFENSE_FIELD_REMINDER = (
    "改等級時請清空不適用的欄位：keep_defensive 不得填 defensive_reconciliation、offense_basis、failure_condition；"
    "start_rebuild／rebuild 不得填 offense_basis、failure_condition（只有 probe 填這兩欄）。"
)

# 轉向後進攻（只有 position_sizing.offense 啟用才使用，取代 EXPOSURE_VIEW_PROMPT*；沒有 offense 的 Policy 簡報逐位元不變）。
OFFENSE_VIEW_PROMPT = (
    "摘要含 exposure_facts（程式算的組合 beta、低 beta 占比、現金權重、持股產業權重與最近一個交易池上漲日的報酬）與 offense_facts"
    "（程式算的賣壓竭盡、廣度回升、量能、情緒極端、指數站回五類事實與一組對照樣本；全部是背景事實、沒有 evidence_id、不可引用，"
    "理由也不要寫出 exposure_facts 或 offense_facts 這兩個鍵名；缺資料的類別標 unavailable，不補值），另有 offense levels（各等級對映的人填上限）。"
    "另輸出 exposure_view（keep_defensive／probe／start_rebuild／rebuild），回答「組合要不要開始回補市場參與度、先試還是等確認」，它會實際決定配置："
    "keep_defensive 時曝險配對一對都不成交、現金目標照格點；probe 是預測型，轉向還看不出來或尚未確認時小量先試（turn_assessment 可配 no_turn_evidence 或 turn_unconfirmed）；"
    "start_rebuild 要 turn_assessment 至少 turn_unconfirmed；rebuild 要 turn_confirmed。進攻等級會依提名先後取用至多該級的對數與份量格、組合 beta 預估達停止線即停止，"
    "防守姿態下現金目標另放寬到該級的人填值（只放寬）；缺資料、回撤下限為 defensive 或回撤過深時，程式會把有效等級壓回 keep_defensive，不會升級。"
    "非 keep_defensive 時 evidence_ids 至少引用兩個市場層級證據、其中至少一個不是新聞；probe 必須填 offense_basis（至少兩個不同的事實類別，"
    "各類事實須 available）與 failure_condition（之後看得到、會讓這次試單算失敗的事，含時間條件）；姿態為 defensive 而 exposure_view 不是 keep_defensive 時，"
    "必須在 defensive_reconciliation 說明防守的現金判斷與進攻為什麼不矛盾；不適用的欄位填空陣列或空字串。你不得輸出任何百分比或數字。"
    "兩種錯誤都有成本：太早進攻，可能追高、遇到反彈失敗被套；太晚進攻，組合 beta 留在低檔而錯過反彈。請對照事實與市場證據權衡，"
    "不要把 keep_defensive 當成沒有成本的預設；證據不足而選它時，理由要說明為什麼現在還不是進攻時機。規則見 cash-stance.md「offense_facts」節。"
    "前次現金目標若是放寬值（前次進攻等級的 defensive_cash_buffer），今日回到 keep_defensive 時現金目標會回到防守格點，"
    "程式會依交易 Agent 的釋出順序賣股補回現金，換手與費稅都有成本；這與前述兩種錯誤成本並列，請一併權衡，不要因此預設任何一個方向。"
    + OFFENSE_FIELD_REMINDER
)


# 簡報說真話（只有 position_sizing.exposure_view 為 v2 才使用；v1 與沒有閘門的 Policy 沿用上面各段舊文字，簡報逐位元不變）。
# 取代 TURN_FACTS_PROMPT／TURN_STATE_PROMPT*／EXPOSURE_VIEW_PROMPT 中與現行行為不一致的句子。
TURN_FACTS_PROMPT_AGENT_LED = (
    "摘要含 turn_facts（程式依行情算的交易池轉向事實，是事實不是訊號）：另輸出 turn_assessment"
    "（no_turn_evidence／turn_unconfirmed／turn_confirmed），回答「市場有沒有轉向」，不要求與現金姿態一致，不改現金格點與回撤下限；"
    "它在本 Policy 下的實際作用見下一段。規則見 cash-stance.md「turn_facts」節。"
)
TURN_STATE_NOTE_AGENT_LED = (
    "程式狀態機的結果，只是背景（沒有證據編號、不可引用）：它只在資料不可用或回撤下限為 defensive 時把你的 turn_assessment 壓回不放寬；"
    "turn_assessment 的實際作用見提示文字（現金目標、換手上限與 quality_dip 門檻），不再啟動曝險換股。"
)
TURN_ASSESSMENT_PROMPT_AGENT_LED = (
    "turn_assessment 在本 Policy 下的實際作用（換股由交易 Agent 提名，程式不再依它啟動曝險換股）：一、防守姿態下，"
    "Policy 有為 turn_unconfirmed（候選）或 turn_confirmed（確認）填 defensive_cash_buffer 時，現金目標會放寬到該值（只放寬、不收緊），"
    "turn_confirmed 另可放寬換手上限（Policy 有填才有）；二、它是優質股換股（quality_dip）的最低門檻，低於 Policy 的 min_turn_assessment "
    "時 quality_dip 配對全部取消；三、Policy 有 quality_dip v4 的 ladder 時，no_turn_evidence 也可抄底但須交易池回檔達人設深度門檻，有轉向跡象（turn_unconfirmed 以上）則不受該門檻限制；四、它不決定曝險配對（那是 exposure_view 的事）。程式只在 turn_state 的 data_status 為 unavailable "
    "或回撤下限為 defensive 時壓回不放寬，regime 不是 bear 不擋。"
    "兩種錯誤都有成本：太早升級，可能在反彈失敗時放寬現金、買在高點而被套；太晚升級，會在反彈已經發生後仍停在低參與度、錯過上漲。"
    "請依 turn_facts 與 turn_state 的事實權衡，兩邊都不是預設安全的答案，理由要能說明為什麼現在是或不是轉向。規則見 cash-stance.md「turn_state」節。"
)
# offense 啟用時的 turn_assessment 說明（取代 TURN_ASSESSMENT_PROMPT_AGENT_LED／TURN_STATE_NOTE_AGENT_LED；兩者仍供沒有 offense 的 agent_led Policy 使用）。
# offense 與 rebound_participation.by_state.*.defensive_cash_buffer 互斥，防守姿態的現金放寬只看 exposure_view 的等級，不看 turn_assessment。
TURN_STATE_NOTE_OFFENSE = (
    "程式狀態機的結果，只是背景（沒有證據編號、不可引用）：它只在資料不可用或回撤下限為 defensive 時把你的 turn_assessment 壓回不放寬；"
    "turn_assessment 的實際作用見提示文字，它不決定現金目標或換手上限，也不啟動曝險換股。"
)
TURN_ASSESSMENT_PROMPT_OFFENSE = (
    "turn_assessment 在本 Policy 下的實際作用（換股由交易 Agent 提名，程式不依它啟動曝險換股，也不依它放寬現金或換手）：一、它必須與 exposure_view 一致"
    "（probe 配 no_turn_evidence 或 turn_unconfirmed、start_rebuild 至少 turn_unconfirmed、rebuild 須 turn_confirmed），不一致會被退回，程式不會替你調整；"
    "二、它是優質股換股（quality_dip）的最低門檻，低於 Policy 的 min_turn_assessment 時 quality_dip 配對全部取消；"
    "三、Policy 有 quality_dip v4 的 ladder 時，no_turn_evidence 也可抄底但須交易池回檔達人設深度門檻，有轉向跡象（turn_unconfirmed 以上）則不受該門檻限制；"
    "四、防守姿態下的現金放寬只由 exposure_view 的等級決定（offense 各級的 defensive_cash_buffer），與 turn_assessment 無關。"
    "程式只在 turn_state 的 data_status 為 unavailable 或回撤下限為 defensive 時壓回不放寬，regime 不是 bear 不擋。"
    "兩種錯誤都有成本：太早升級，可能在反彈失敗時買在高點而被套；太晚升級，會在反彈已經發生後仍停在低參與度、錯過上漲。"
    "請依 turn_facts 與 turn_state 的事實權衡，兩邊都不是預設安全的答案，理由要能說明為什麼現在是或不是轉向。規則見 cash-stance.md「turn_state」節。"
)
EXPOSURE_VIEW_PROMPT_AGENT_LED = (
    "摘要含 exposure_facts（程式算的組合 beta、低 beta 占比、現金權重、持股產業權重與最近一個交易池上漲日的報酬，是背景事實、"
    "沒有 evidence_id、不可引用，理由也不要寫出 exposure_facts 這個鍵名）：另輸出 exposure_view（keep_defensive／start_rebuild／rebuild），"
    "回答「組合要不要開始回補市場參與度」，它會實際決定配置：keep_defensive 時交易 Agent 提名的 purpose=exposure 配對一對都不成交、換出者不賣；"
    "start_rebuild／rebuild 時依提名清單的先後取用，至多取 Policy 對映該等級的對數（人設的硬上限），仍受換手、現金、單股、產業與回撤下限限制；"
    "它不改現金姿態、現金比例或持股廣度。不是 keep_defensive 時 evidence_ids 至少引用一個市場層級證據。"
    "兩種錯誤都有成本：太早回補，可能追高、遇到反彈失敗被套；太晚回補，組合 beta 留在低檔而錯過反彈。"
    "請對照組合曝險事實與市場證據權衡，不要把 keep_defensive 當成沒有成本的預設；證據不足而選它時，理由要說明為什麼現在還不是回補時機。"
    "規則見 cash-stance.md「exposure_facts」節。"
)


CASH_GRADE_PROMPT = (
    "Policy 啟用 cash_ratio_range：摘要含 cash_grade_grid 與 cash_grade_allowed_by_stance，除了姿態另給 cash_grade"
    "（lower／middle／upper），只能選該姿態今日可選的子等級，你不得輸出百分比，規則見 cash-stance.md「cash_grade_allowed_by_stance」節。"
)


def _increase_cash_buffer_cap(policy: Mapping[str, object]) -> Decimal:
    """increase_cash_buffer 的 value 上限：與 risk.py 驗證一致（現金上限減邊際；cash_ratio_range 開啟時再取今日最高格點）。"""
    cap = Decimal(str(policy["cash_weight_ceiling"])) - CASH_BUFFER_CEILING_MARGIN
    sizing = policy.get("position_sizing") or {}
    gate = cash_ratio_gate(sizing)  # type: ignore[arg-type]
    if gate is not None:
        allowed = allowed_cash_grades(gate).get(str(sizing.get("cash_stance")), [])  # type: ignore[union-attr]
        top = cash_grade_grid(gate)[str(sizing.get("cash_stance"))][allowed[-1]] if allowed else Decimal("0")  # type: ignore[union-attr]
        cap = min(cap, Decimal(str(top)))
    return cap


def _grade_brief(gate: Mapping[str, object]) -> Dict[str, object]:
    """cash_ratio_range 啟用時給風險 Agent 的格點、今日可選子等級與前次比例（不含前次 run 的識別碼）。"""
    try:
        cash_grade_grid(gate)
        allowed = allowed_cash_grades(gate)
    except DecisionToolError as error:
        raise DailyPipelineError("position_sizing.cash_ratio_range 無法計算今日可選子等級：%s" % error)
    anchor = gate["anchor"]
    source = anchor.get("source") if isinstance(anchor, Mapping) else None
    return {
        "cash_grade_grid": {level: dict(gate["grid"][level]) for level in CASH_STANCES},  # type: ignore[index]
        "cash_grade_allowed_by_stance": {
            level: {grade: gate["grid"][level][grade] for grade in allowed[level]}  # type: ignore[index]
            for level in CASH_STANCES
        },
        "cash_ratio_anchor": {
            "status": anchor["status"], "ratio": anchor["ratio"],  # type: ignore[index]
            "as_of": source.get("target_session_start") if isinstance(source, Mapping) else None,
            "citable": False,
        },
        "max_daily_raise": gate["max_daily_raise"],
    }


def _cash_ratio(account: Mapping[str, object]) -> Optional[str]:
    """帳戶現金占 NAV 的比例（字串）；缺值或無法解析時回 None，不猜。"""
    try:
        cash, nav = Decimal(str(account["cash"])), Decimal(str(account["nav"]))
    except (KeyError, InvalidOperation):
        return None
    return str((cash / nav).quantize(Decimal("0.0001"))) if nav > 0 else None


class RiskStagesMixin:
    """由 ``DailyDecisionPipeline`` 組合；依賴 ``PipelineBase`` 的 save_*、agent_step 與 ``root``。"""

    def run_cash_stance(
        self,
        bundle: Mapping[str, object],
        momentum: Mapping[str, object],
        analyst_reports: Mapping[str, Mapping[str, object]],
        drawdown_control: Optional[Mapping[str, object]] = None,
        breadth_targets: Optional[Mapping[str, object]] = None,
        cash_buffers: Optional[Mapping[str, object]] = None,
        high_conviction_cash_floor: Optional[Mapping[str, object]] = None,
        cash_ratio_range: Optional[Mapping[str, object]] = None,
        prior_context: Optional[Mapping[str, object]] = None,
        cash_stance_recency: Optional[Mapping[str, object]] = None,
        cash_stance_turn_facts: Optional[Mapping[str, object]] = None,
        turn_state: Optional[Mapping[str, object]] = None,
        market_index_facts_gate: Optional[Mapping[str, object]] = None,
        exposure_view_gate: Optional[Mapping[str, object]] = None,
        exposure_facts: Optional[Mapping[str, object]] = None,
        agent_led: bool = False,
        offense_gate: Optional[Mapping[str, object]] = None,
        offense_facts: Optional[Mapping[str, object]] = None,
        cash_stance_brief_gate: Optional[Mapping[str, object]] = None,
        benchmark_facts: Optional[Mapping[str, object]] = None,
    ) -> Dict[str, object]:
        """風險 Agent 依市場層級摘要給現金姿態與持股廣度；分布與計數由程式統計，回撤下限由程式強制。

        brief 不再附交易 Agent 的意圖與 buy／add 信心統計（避免群體思維）：現金姿態只看市場層級背景、
        分析師看法分布、交易狀態、公司事件風險、帳戶與回撤。"""

        def counts(values: Sequence[object]) -> Dict[str, int]:
            result: Dict[str, int] = {}
            for value in values:
                result[str(value)] = result.get(str(value), 0) + 1
            return dict(sorted(result.items()))

        price_evidence = sorted(
            {str(row["source_evidence_id"]) for row in bundle["snapshot"]["latest_prices"] if row.get("source_evidence_id")}
        )
        # 隔夜市場背景只在決策輸入有附時才進 brief 與提示詞，沒有附的舊流程維持原樣。
        overnight = overnight_market_channel(bundle)
        context = DecisionContext(bundle)
        typed = context.evidence_policy is not None
        regime = momentum.get("regime_assessment")
        if typed and isinstance(regime, Mapping):
            # typed_evidence_v1：市場狀態有市場層級證據編號，現金姿態可引用它當市場層級依據。
            regime = {**regime, "evidence_id": MARKET_REGIME_EVIDENCE_ID}
        soft_floors = _soft_floors(cash_buffers, high_conviction_cash_floor)
        # cash_ratio_range（版本閘門，已綁定 anchor）：簡報改附格點與今日可選子等級，不再附三段階梯，避免當成錨點。
        graded = cash_ratio_range is not None
        if graded and "anchor" not in cash_ratio_range:  # type: ignore[operator]
            raise DailyPipelineError("position_sizing.cash_ratio_range 尚未綁定 anchor（前次現金比例）")
        grade_brief = _grade_brief(cash_ratio_range) if graded else {}  # type: ignore[arg-type]
        # cash_stance_recency（版本閘門）：簡報第一個鍵附程式算的證據時間表；沒有閘門時簡報與舊版逐鍵相同。
        recency_brief: Dict[str, object] = {}
        if cash_stance_recency is not None:
            if not typed:
                raise DailyPipelineError("position_sizing.cash_stance_recency 只能用於 evidence_policy=typed_evidence_v1")
            recency_brief = {"evidence_recency": evidence_recency(bundle, context.market_evidence_ids)}
        # cash_stance_turn_facts（版本閘門）：regime_assessment 旁附程式算的轉向事實；沒有閘門時簡報與舊版逐鍵相同。
        turn_brief: Dict[str, object] = {}
        if cash_stance_turn_facts is not None:
            if not typed:
                raise DailyPipelineError("position_sizing.cash_stance_turn_facts 只能用於 evidence_policy=typed_evidence_v1")
            turn_brief = {"turn_facts": turn_facts(bundle)}
            if turn_state is not None:
                # rebound_participation（版本閘門）：程式狀態機的結果只當背景（沒有 evidence_id，不可引用）。
                turn_brief["turn_state"] = {"citable": False, **dict(turn_state)}
                if agent_led:
                    # 只換簡報副本的 note（team_inputs 內封存的原文會進雜湊、不動）：舊文字寫 turn_confirmed 放寬曝險換股，與現行行為不符。
                    turn_brief["turn_state"]["note"] = TURN_STATE_NOTE_OFFENSE if offense_gate is not None else TURN_STATE_NOTE_AGENT_LED
        # market_index_facts（版本閘門）：附程式算的加權指數事實（背景、不可引用）；沒有閘門時簡報與舊版逐鍵相同。
        index_brief: Dict[str, object] = (
            {"market_index_facts": market_index_facts(bundle)} if market_index_facts_gate is not None else {}
        )
        # benchmark_facts（版本閘門）：附程式算的 0050 基準事實（背景、不可引用）；沒有閘門時簡報與舊版逐鍵相同。
        benchmark_brief: Dict[str, object] = {}
        if benchmark_facts is not None:
            if not typed:
                raise DailyPipelineError("position_sizing.benchmark_facts 只能用於 evidence_policy=typed_evidence_v1")
            benchmark_brief = {"benchmark_facts": risk_benchmark_brief(benchmark_facts)}
        # exposure_view（版本閘門）：附程式算的組合曝險與產業權重事實（背景、不可引用）；沒有閘門時簡報與舊版逐鍵相同。
        exposure_brief: Dict[str, object] = {}
        if exposure_view_gate is not None:
            if not typed:
                raise DailyPipelineError("position_sizing.exposure_view 只能用於 evidence_policy=typed_evidence_v1")
            if not isinstance(exposure_facts, Mapping):
                raise DailyPipelineError("position_sizing.exposure_view 啟用時必須先建立 exposure_facts")
            exposure_brief = {"exposure_facts": risk_exposure_brief(exposure_facts, agent_led=agent_led, offense=offense_gate is not None)}
        # offense（轉向後進攻，版本閘門）：附程式算的進攻事實與各等級對映的人填上限（背景、不可引用）；仍不附交易 Agent 的意圖與信心統計。
        # 沒有閘門時簡報與舊版逐鍵相同。
        offense_block: Dict[str, object] = {}
        if offense_gate is not None:
            if exposure_view_gate is None or not isinstance(offense_facts, Mapping):
                raise DailyPipelineError("position_sizing.offense 啟用時必須有 exposure_view 與 offense_facts")
            offense_block = {"offense_facts": (
                offense_brief(offense_facts, offense_gate, include_reference=False) if offense_reference_omitted(cash_stance_brief_gate)
                else offense_brief(offense_facts, offense_gate))}
            glide = cash_glide_brief(offense_gate, cash_ratio_range)
            if glide is not None:
                offense_block["offense_cash_glide"] = glide  # offense.cash_glide：只在前次放寬過現金時附（背景、不可引用）
        if offense_reference_omitted(cash_stance_brief_gate) and offense_gate is None:
            raise DailyPipelineError("position_sizing.cash_stance_brief.offense_reference 需要 position_sizing.offense")
        brief_body = {
                **recency_brief,
                "regime_assessment": regime,
                **turn_brief,
                **index_brief,
                **benchmark_brief,
                **exposure_brief,
                **offense_block,
                "analyst_outlooks": {
                    name: counts([item.get("outlook") for item in report.get("items", [])])
                    for name, report in analyst_reports.items()
                },
                "tradability": counts(
                    [item.get("state") for item in (bundle.get("tradability_assessment") or {}).get("symbols", [])]
                ),
                "company_events": [
                    {"symbol": item["symbol"], "event_outlook": item.get("event_outlook"),
                     "events": item.get("events", []), "findings": item.get("findings", []),
                     "data_gaps": item.get("data_gaps", [])}
                    for item in analyst_reports["event"].get("items", [])
                ],
                "account": {key: bundle["account_snapshot"].get(key) for key in ("cash", "nav", "positions")},
                "rules": {key: bundle["rules"].get(key) for key in ("cash_weight_must_be_below", "min_positions", "max_positions")},
                # 策略文件：風險 Agent 看整體市場情緒；與事件分析師讀同一份由程式重建的全市場通道。
                "market_sentiment": market_sentiment_channel(bundle),
                "drawdown_control": dict(drawdown_control) if drawdown_control else None,
                # 持股廣度對應的目標檔數由 Policy 決定；風險 Agent 只選等級，程式依信心順序挑到該檔數。
                "positions_by_breadth": dict(breadth_targets) if breadth_targets else None,
                # 姿態的代價：各姿態對應的現金緩衝與目前現金比例，讓風險 Agent 知道選哪個姿態會不會讓買單被緩衝擋掉。
                **({"cash_buffer_by_stance": dict(cash_buffers) if cash_buffers else None} if not graded else grade_brief),
                "cash_ratio_now": _cash_ratio(bundle["account_snapshot"]),
                # high_conviction_cash_floor（版本閘門）啟用時才附：各姿態下高信心買單可動用到、程式減碼也只補到的現金下限。
                **({"high_conviction_cash_floor_by_stance": soft_floors} if soft_floors else {}),
                "citable_evidence_ids": price_evidence,
                **({"overnight_market": overnight, "citable_evidence_ids": sorted(
                    set(price_evidence) | {row["evidence_id"] for row in overnight["components"] if row["evidence_id"]})}
                   if overnight is not None else {}),
                # typed_evidence_v1：市場層級證據另列一份，至少要引用其中一個；個股行情只能作補充。
                **({"market_level_evidence_ids": sorted(context.market_evidence_ids),
                    "citable_evidence_ids": sorted(set(price_evidence) | context.market_evidence_ids)} if typed else {}),
            }
        # cash_stance_brief（版本閘門）：公司事件改摘要、必讀欄位重排；沒有閘門時簡報與舊版逐鍵相同。
        if cash_stance_brief_gate is not None:
            brief_body = reorder_brief(brief_body, company_events_summary(analyst_reports["event"].get("items", [])))
        brief_path = self.save_brief("brief_cash_stance", brief_body)
        floor = (drawdown_control or {}).get("stance_floor")

        def validate(stance: Mapping[str, object]) -> List[str]:
            errors: List[str] = []
            validate_cash_stance(
                context, stance, "cash_stance", errors, cash_ratio_range=cash_ratio_range, prior_context=prior_context,
                recency_gate=cash_stance_recency, turn_facts_gate=cash_stance_turn_facts,
                exposure_view_gate=exposure_view_gate, exposure_facts=exposure_facts,
                **({"offense_gate": offense_gate, "offense_facts": offense_facts} if offense_gate is not None else {}),
            )
            if breadth_targets and isinstance(stance, Mapping) and "breadth" not in stance:
                errors.append("cash_stance.breadth 必須是 %s" % "／".join(POSITION_BREADTHS))
            if isinstance(stance, Mapping) and not stance_meets_floor(stance.get("level"), floor):
                errors.append("帳戶回撤 %s 已達門檻，現金姿態不得低於 %s" % (drawdown_control.get("drawdown"), floor))
            return errors

        schema = (
            cash_stance_turn_facts_schema(graded, cash_stance_recency is not None) if cash_stance_turn_facts is not None
            else cash_stance_recency_schema(graded) if cash_stance_recency is not None
            else (CASH_GRADE_STANCE_SCHEMA if graded else CASH_STANCE_SCHEMA)
        )
        if exposure_view_gate is not None:
            schema = exposure_view_schema(schema, probe=offense_gate is not None)
        if offense_gate is not None:
            schema = offense_stance_schema(schema)
        task = AgentTask(
            "cash_stance",
            "你是 $portfolio-risk-review 風險 Agent 的市場風險評估。先讀 %s（「配置前現金姿態」一節）及其連結的 %s，"
            "再讀市場層級摘要 %s。給整體現金姿態 aggressive／neutral／defensive 與持股廣度 breadth"
            "（concentrated／balanced／diversified），rationale 同時說明兩者的理由；"
            "摘要刻意不附交易 Agent 的意圖與信心統計，請獨立判斷市場風險。競賽規定現金必須低於 NAV 25%%，%s"
            "%s%s%s判斷依據、證據選法與各欄位的用法依 cash-stance.md。%s"
            % (self.root / "tradeagent/risk/portfolio-risk-review/SKILL.md",
               self.root / "tradeagent/risk/portfolio-risk-review/references/cash-stance.md", brief_path,
               CASH_GRADE_PROMPT if graded else "姿態對應的現金比例由 Policy 決定，你不得輸出百分比。",
               OVERNIGHT_PROMPT if overnight is not None else "",
               SOFT_FLOOR_PROMPT if soft_floors else "",
               TYPED_STANCE_PROMPT if typed else "", _SHARED_RULES)
            + (RECENCY_PROMPT if cash_stance_recency is not None else "")
            + (
                (TURN_FACTS_PROMPT_AGENT_LED if agent_led else TURN_FACTS_PROMPT)
                if cash_stance_turn_facts is not None else ""
            )
            + (
                (TURN_ASSESSMENT_PROMPT_OFFENSE if offense_gate is not None else TURN_ASSESSMENT_PROMPT_AGENT_LED if agent_led else
                 (TURN_STATE_PROMPT_V2 if turn_state.get("version") == "v2" else TURN_STATE_PROMPT))
                if turn_state is not None else ""
            )
            + (MARKET_INDEX_PROMPT if market_index_facts_gate is not None else "")
            + (BENCHMARK_PROMPT if benchmark_facts is not None else "")
            + (
                (OFFENSE_VIEW_PROMPT.replace(REFERENCE_PHRASE, REFERENCE_PHRASE_OMITTED)
                 if offense_reference_omitted(cash_stance_brief_gate) else OFFENSE_VIEW_PROMPT) if offense_gate is not None else
                (EXPOSURE_VIEW_PROMPT_AGENT_LED if agent_led else EXPOSURE_VIEW_PROMPT)
                if exposure_view_gate is not None else ""
            )
            + (BRIEF_PROMPT if cash_stance_brief_gate is not None else ""),
            schema,
            deny_reads=self._independent_deny_reads(),
        )
        # offense 的 Schema 要求不適用欄位以空陣列／空字串送出，封存時還原成缺席；沒有閘門時與舊版相同（原樣複製）。
        # offense 的跨欄位規則多，第一次加兩次帶回饋的重試共三次嘗試（與降級版步驟的上限相同）；沒有 offense 時沿用 ``self.max_attempts``，行為不變。
        stance = self.agent_step(
            task, (lambda output: normalize_offense_stance(output)) if offense_gate is not None else (lambda output: dict(output)), validate,
            **({"max_attempts": max(self.max_attempts, OFFENSE_STANCE_ATTEMPTS)} if offense_gate is not None else {}))
        self.save_artifact("cash_stance", stance)
        return stance

    def _independent_deny_reads(self) -> Sequence[str]:
        """獨立判斷任務（現金姿態、優質股裁決）的拒讀樣式：只在主執行器能強制拒讀時才帶。

        Codex 無法強制拒讀，帶 deny_reads 會在 ``_call_agent`` 被拒絕啟動（會讓 Codex 路徑整個跑不起來），所以不帶、維持原行為。"""
        return RISK_INDEPENDENT_DENY_GLOBS if getattr(self.runner, "enforces_deny_reads", False) else ()

    # ------------------------------------------------------------------ quality_dip 裁決
    def run_quality_dip_ruling(
        self,
        bundle: Mapping[str, object],
        policy: Mapping[str, object],
        trade_decision: Mapping[str, object],
        debate: Mapping[str, object],
        exposure_review: Mapping[str, object],
        nomination: Mapping[str, object],
        stance: Mapping[str, object],
        prior_context: Optional[Mapping[str, object]],
        exposure_facts: Mapping[str, object],
        dip_facts: Optional[Mapping[str, object]],
        turn_state: Optional[Mapping[str, object]],
        run_id: str,
        decline_depth_facts: Optional[Mapping[str, object]] = None,
    ) -> Dict[str, object]:
        """風險 Agent 對交易 Agent 提名的 purpose=quality_dip 配對做最後裁決（agent_swap.quality_dip v3，排在現金姿態之後、配置之前）。

        ``policy`` 是已綁定本次現金姿態（turn_assessment）與回撤下限的 Policy。提名沒有 quality_dip 配對時封存 ``not_required``、前置條件不成立時封存
        ``skipped_by_program``（都不呼叫 Agent）；其餘呼叫 Agent，驗證最多三次嘗試，用盡封存 ``exhausted``、暫時性失敗重試後仍失敗封存
        ``unavailable``（空清單，全部 quality_dip 配對取消、其餘決策照常）；安全拒絕仍整個停止（比照換股提名）。
        這是現金姿態之外的另一次獨立呼叫：現金姿態的簡報完全不變。"""
        envelope = ruling_envelope(bundle, nomination, stance, "quality-dip-ruling:%s" % run_id)
        validator = QualityDipRulingValidator(
            bundle, policy, nomination, stance, prior_context=prior_context, exposure_facts=exposure_facts, dip_facts=dip_facts,
            decline_depth_facts=decline_depth_facts)
        try:
            status, reason = expected_program_status(policy, nomination)
        except DecisionToolError as error:
            raise DailyPipelineError("優質股裁決無法重算前置檢查：%s" % error) from error
        if status == STATUS_NOT_REQUIRED:
            ruling = program_ruling(envelope, STATUS_NOT_REQUIRED)
        elif status == STATUS_SKIPPED:
            ruling = program_ruling(envelope, STATUS_SKIPPED, [str(reason)])
        else:
            try:
                brief = quality_dip_ruling_brief(
                    bundle, trade_decision, debate, exposure_review, nomination, stance, exposure_facts, dip_facts,
                    turn_facts(bundle), market_index_facts(bundle), turn_state,
                    agent_led=exposure_view_agent_led(policy.get("position_sizing")), decline_depth_facts=decline_depth_facts)
            except (DecisionToolError, KeyError, TypeError, ValueError) as error:
                raise DailyPipelineError("優質股裁決簡報建立失敗：%s" % error) from error
            brief_path = self.save_brief("brief_quality_dip_ruling", brief)
            task = AgentTask(
                "quality_dip_ruler",
                "你是 $portfolio-risk-review 風險 Agent 的優質股換股裁決。先讀 %s（「優質股換股裁決」一節）及其連結的 %s，再讀簡報 %s。"
                "交易 Agent 在換股提名提出了 purpose=quality_dip 的配對與份量格；你對每一組依序給 approve、downsize 或 reject"
                "（只能縮不能放，不得新增或更換配對），downsize／reject 要附 reason_kind 與理由，evidence_ids 至少引用一個市場層級證據。"
                "本次現金姿態為防守看法（defensive 或 keep_defensive）而仍放行任何一組時，必須在 defensive_reconciliation 說明為什麼；"
                "否則該欄位填空字串。你不得輸出任何百分比或數字。%s"
                % (self.root / "tradeagent/risk/portfolio-risk-review/SKILL.md",
                   self.root / "tradeagent/risk/portfolio-risk-review/references/quality-dip-ruling.md", brief_path, _SHARED_RULES),
                QUALITY_DIP_RULING_SCHEMA,
                deny_reads=self._independent_deny_reads(),
            )
            fingerprint = canonical_sha256({"envelope": envelope, "brief": brief})
            outcome = self.agent_step_degradable(
                task, lambda output: seal_quality_dip_ruling(envelope, output), validator.validate, fingerprint)
            if outcome.artifact is not None:
                ruling = outcome.artifact
            elif outcome.status in DEGRADED_STATUSES:
                ruling = program_ruling(envelope, outcome.status, outcome.errors)
            else:
                raise DailyPipelineError("優質股裁決未預期的步驟狀態：%s" % outcome.status)
        errors = validator.validate(ruling)
        if errors:
            raise DailyPipelineError("QualityDipRuling 驗證失敗：" + "；".join(errors[:10]))
        self.save_artifact("quality_dip_ruling", ruling)
        return ruling

    # ------------------------------------------------------------------ review
    def _review(
        self, bundle: Mapping[str, object], policy: Mapping[str, object],
        proposal: Mapping[str, object], scenario: Mapping[str, object], guard: Mapping[str, object], run_id: str,
    ) -> Dict[str, object]:
        validator = RiskReviewValidator(bundle, policy)
        revision = int(proposal["revision_count"])

        max_revisions = int(policy["max_revisions"])

        def build(output: Mapping[str, object]) -> Dict[str, object]:
            decision = output["decision"]
            rationale = output["rationale"]
            risk_flags = list(output["risk_flags"])
            if decision == "revise" and revision >= max_revisions:
                # 已用完修正次數：確定性降級為 reject，不靠下一次被 Validator 拒絕後整條鏈失敗。
                decision = "reject"
                rationale = "已達修正上限（%d 次）仍要求修正，依契約降級為拒絕：%s" % (max_revisions, rationale)
                risk_flags.append("REVISION_LIMIT_REACHED")
            review: Dict[str, object] = {
                "schema_version": "1.0",
                "review_id": "risk-review:%s:r%d" % (run_id, revision),
                "bundle_id": bundle["bundle_id"],
                "snapshot_id": bundle["snapshot_id"],
                "decision_cutoff": bundle["decision_cutoff"],
                "bundle_hash": bundle["bundle_sha256"],
                "proposal_id": proposal["proposal_id"],
                "scenario_id": scenario["scenario_id"],
                "guard_id": guard["guard_id"],
                "revision_index": revision + 1 if decision == "revise" else revision,
                "decision": decision,
                "rationale": rationale,
                "evidence_ids": output["evidence_ids"],
                "risk_flags": risk_flags,
                "revision_actions": output["revision_actions"] if decision == "revise" else [],
                "unresolved_questions": output["unresolved_questions"],
                "status": "completed",
                "errors": [],
            }
            review["content_sha256"] = artifact_content_sha256(review)
            return review

        def validate(review: Mapping[str, object]) -> List[str]:
            return validator.validate(proposal, scenario, guard, review)

        if guard.get("passed") is not True:
            # 契約規定硬性規則失敗時只能 reject，確定性產生，不交給 Agent 判斷。
            failed = [str(item.get("rule_id")) for item in guard.get("checks", []) if not item.get("passed")]
            review = build(
                {
                    "decision": "reject",
                    "rationale": "CompetitionGuard 硬性規則未通過（%s），依契約自動拒絕。" % "、".join(failed),
                    "evidence_ids": [str(bundle["account_snapshot"]["source_evidence_id"])],
                    "risk_flags": ["GUARD_FAILED:%s" % rule for rule in failed],
                    "unresolved_questions": [],
                    "revision_actions": [],
                }
            )
            errors = validate(review)
            if errors:
                raise DailyPipelineError("Guard 失敗的拒絕審查驗證失敗：" + "；".join(errors))
            return review
        exposure = sector_exposure(proposal, policy)
        liquidity = liquidity_exposure(proposal, MomentumEngine(bundle).run())
        # 審查 Agent 只讀提案／情境／Guard，沒有共同輸入；不提供可引用清單時它會編造 artifact ID
        # 而被 Validator 拒絕，正式流程曾要靠第二次重試才通過。
        held = {str(item["symbol"]).upper() for item in proposal["allocation_proposal"]["positions"]}
        citable = sorted(
            str(row["source_evidence_id"]) for row in bundle["snapshot"]["latest_prices"]
            if row.get("source_evidence_id") and str(row["symbol"]).upper() in held
        )
        citable_path = self.save_artifact("review_citable_evidence_r%d" % revision, {"citable_evidence_ids": citable})
        names = {
            name: self.save_artifact("%s_review_input_r%d" % (name, revision), payload)
            for name, payload in (("proposal", proposal), ("scenario", scenario), ("guard", guard),
                                  ("sector_exposure", exposure), ("liquidity_exposure", liquidity))
        }
        task = AgentTask(
            "review_r%d" % revision,
            "你是 $portfolio-risk-review 風險審查子 Agent。先讀 %s 及其連結的 %s，再讀提案 %s、情境 %s、Guard %s、"
            "程式計算的產業實際權重 %s 與每筆委託佔 20 日均量的比例 %s（flagged 表示超過警示比例或缺均量，可考慮 remove_candidate 或降低周轉）；"
            "可引用的 evidence ID 清單在 %s（evidence_ids 只能從它選取）。輸出 approve、revise 或 reject；revision_actions 的欄位格式與限制依 risk-review-output.md，"
            "本次 increase_cash_buffer 的 value 不得高於 %s（競賽現金上限 %s 減邊際 %s；cash_ratio_range 開啟時另取今日可選最高格點，且為兩位小數）；已是第 %d 次修正、上限 %d 次"
            "（已達上限時不得再 revise，只能 approve 或 reject，要求修正會由程式降級為 reject）。%s%s"
            % (
                self.root / "tradeagent/risk/portfolio-risk-review/SKILL.md",
                self.root / "tradeagent/risk/portfolio-risk-review/references/risk-review-output.md",
                names["proposal"], names["scenario"], names["guard"], names["sector_exposure"], names["liquidity_exposure"], citable_path,
                _increase_cash_buffer_cap(policy),
                policy["cash_weight_ceiling"], CASH_BUFFER_CEILING_MARGIN, revision, int(policy["max_revisions"]),
                _revision_bounds_text(policy), _SHARED_RULES,
            ),
            REVIEW_SCHEMA,
        )
        return self.agent_step(task, build, validate)
