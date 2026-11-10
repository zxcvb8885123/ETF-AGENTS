"""優質股被大盤拖累時的事實（``position_sizing.quality_dip_facts`` v1 版本閘門，只由人設定、策略反思不得改）。

第一階段只算事實與標示（設計見 ``docs/quality_dip_plan.md``）：程式對全交易池重算短期價格事實與三個財報清單，
合成互斥的 ``price_pattern`` 與 ``candidate``，以背景放進簡報；**不影響名額、權重、股數、現金與 Guard**，
Agent 也不輸出任何新欄位。整份事實另存為 ``team_inputs.dip_facts``，Finalizer 以同一份輸入整份重算比對。

口徑（版本 v1 寫死，不是旋鈕）：

- 日期格是交易池全部行情交易日的聯集。評估視窗＝最後 21 個日期（基準日 D[-21] 收盤到最近交易日 D[-1] 收盤，20 個日報酬）；
  視窗前 beta 用基準日以前的 61 個日期（60 個日報酬，D[-81]～D[-21]），兩個視窗在基準日相接、不重疊。
  beta 直接用 ``exposure.BetaModel``（交易池等權日報酬、窗內缺一天即 null、池報酬變異為 0 時不可用），
  只是把行情截到基準日再建模型；``BetaModel`` 本身與 ``exposure_review`` 的路徑完全沒有改。
- 殘差 = 個股 20 日報酬 − 視窗前 beta × **交易池等權日報酬連乘**的 20 日報酬（與 beta 估計所用的日報酬序列同一口徑）。
  另附「相對交易池」= 個股 20 日報酬 − 各股 20 日報酬的簡單平均（與持股檢視同一公式）；兩個池報酬欄位名稱不同、不混用。
- 有殘差的股票少於交易池的 ``MIN_REGIME_COVERAGE``（80%，沿用市場狀態的覆蓋門檻）時整份 ``data_status: unavailable``。
- 程式不設殘差、跌幅或名次門檻：``price_pattern`` 只看符號（殘差為 0 歸 ``market_explained``），「明不明顯」由 Agent 判斷。
- 「視窗內新可得」＝ ``available_at`` 的台北日期晚於基準日、且不晚於 ``decision_cutoff``；晚於 cutoff 的文件不計入。
- 「淨利由盈轉虧」的前一期可比口徑是基本面指標既有的去年同季（``net_income_yoy_pct`` 的 prior_year 依賴）；沒有可比前一期即列「不知道」。
- 「high 事件且事件面負面」讀事件分析師報告：分析師階段事件報告尚未完成，所以基本面分析師看到的清單不含這一項
  （``event_check: not_applied``），交易 Agent 的背景與 ``team_inputs`` 才含（``event_check: applied``）。

任何 bar 晚於 ``snapshot.latest_trade_date``、時間缺時區或 bar 可得時間晚於 cutoff 一律拒絕；缺資料只降級為 null／不知道，不補值。
"""

from __future__ import annotations

from datetime import date, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core import canonical_sha256, parse_aware_time

from .analysts import growth_signal_quality
from .cash_flow_context import cash_flow_context
from .exposure import BetaModel
from .fields import DecisionToolError, decimal_value, reject_unknown_fields
from .momentum import MIN_REGIME_COVERAGE

QUALITY_DIP_FACTS = "v1"
QUALITY_DIP_ASSESSMENT = "v1"
QUALITY_DIP_ASSESSMENT_FIELDS = {"version"}
# 事件分析師對每則事件的方向列舉（position_sizing.event_direction 啟用才有）：只描述該則事件對公司的方向，不是整檔 event_outlook。
QUALITY_DIP_EVENT_DIRECTION = "v1"
QUALITY_DIP_EVENT_DIRECTION_FIELDS = {"version"}
EVENT_DIRECTIONS = ("positive", "negative", "neutral", "unknown")
# 交易 Agent 對 buy／add 的列舉（紀錄用，與意圖、信心不設對應）：價格事實不可用時只能 undetermined。
DIP_ASSESSMENTS = ("not_a_dip", "market_driven", "oversold_cause_unclear", "stock_specific", "undetermined")
QUALITY_DIP_FACTS_FIELDS = {"version"}
DIP_FACTS_SCHEMA_VERSION = "1.0"
DIP_FACTS_KEY = "dip_facts"  # team_inputs 的選配鍵：與 Policy 閘門雙向同時存在
WINDOW_DAYS = 20
SHORT_WINDOW_DAYS = 5
BETA_WINDOW_DAYS = 60
HIGH_WINDOWS = (60, 120)
NEW_LOW_WINDOW = 20
MIN_COVERAGE = Decimal(str(MIN_REGIME_COVERAGE))
PRICE_PATTERNS = ("unavailable", "no_decline", "market_explained", "beyond_market")
CANDIDATES = ("market_dragged", "oversold", "none")
DATA_STATUSES = ("available", "unavailable")
EVENT_CHECK_APPLIED = "applied"
EVENT_CHECK_NOT_APPLIED = "not_applied"
# 指標鍵 → 應有的單位；單位不符視為單位衝突，列「不知道」，不拿來比大小。
CONCERN_METRICS = (
    ("revenue_yoy_pct", "percent"),
    ("net_income_yoy_pct", "percent"),
    ("operating_margin_pp_yoy", "percentage_points"),
)
TAIPEI = timezone(timedelta(hours=8))
QUANTUM = Decimal("0.000001")
_TWO = Decimal("0.01")
FACTS_FIELDS = {
    "schema_version", "version", "facts_id", "as_of", "data_status", "window", "pool", "data_gaps", "note", "items",
}
ITEM_FIELDS = {
    "symbol", "evidence_ids", "market_attribution", "price_position", "fundamental_flags", "price_pattern", "candidate",
    "cause",
}
DIP_NOTE = (
    "這些是程式算的價格與財報事實，不是買賣訊號，也不決定名額、權重或現金。殘差＝個股 20 日報酬減「視窗前 60 日估的 beta × "
    "交易池等權日報酬連乘的 20 日報酬」；殘差接近零時 market_explained 與 beyond_market 沒有實質差別，請看數值與名次。"
    "beyond_market（超跌）只表示比 beta × 大盤多跌、原因不明（cause: unknown），不等於低於本來價值，不得寫便宜或低估。"
    "疑慮是過去某一期的事實，不知道是缺資料：兩者都不擋進場也不算通過；查無新壞事不代表營運沒變。"
)
ANALYST_NOTE = (
    "這些是程式算的事實，不是訊號，也沒有自己的 evidence_id：引用時請引用各列附的該股既有證據編號（行情或財報文件）。"
    "殘差接近零時「可由大盤解釋」與「超出大盤」沒有實質差別；不得寫便宜或低估。疑慮是某一期的事實，不知道是缺資料，"
    "都不是利空或利多的結論。事件面負面一項要等事件分析師完成，這一階段不含（event_check: not_applied）。"
)


def quality_dip_facts_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """quality_dip_facts 啟用時回傳閘門物件；沒有閘門回 None（簡報與 team_inputs 與舊版逐鍵相同）。"""
    gate = sizing.get("quality_dip_facts") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def validate_quality_dip_facts_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """閘門形狀：{version: v1}；視窗長度是版本內的程式常數，不是旋鈕。"""
    gate = sizing["quality_dip_facts"]
    prefix = "position_sizing.quality_dip_facts"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, QUALITY_DIP_FACTS_FIELDS, prefix, errors)
    if gate.get("version") != QUALITY_DIP_FACTS:
        errors.append("%s.version 只允許 %s" % (prefix, QUALITY_DIP_FACTS))


def quality_dip_assessment_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """quality_dip_assessment 啟用時回傳閘門物件；沒有閘門回 None（交易意圖欄位與舊版完全相同，出現 dip_assessment 即拒絕）。"""
    gate = sizing.get("quality_dip_assessment") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def validate_quality_dip_assessment_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """閘門形狀：{version: v1}。前提 quality_dip_facts 由 GATE_PREREQUISITES 檢查，typed_evidence_v1 在驗證交易意圖時檢查。"""
    gate = sizing["quality_dip_assessment"]
    prefix = "position_sizing.quality_dip_assessment"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, QUALITY_DIP_ASSESSMENT_FIELDS, prefix, errors)
    if gate.get("version") != QUALITY_DIP_ASSESSMENT:
        errors.append("%s.version 只允許 %s" % (prefix, QUALITY_DIP_ASSESSMENT))


def event_direction_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """event_direction 啟用時回傳閘門物件；沒有閘門回 None（事件報告與新壞事的事件面判準與舊版相同）。"""
    gate = sizing.get("event_direction") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def validate_event_direction_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """閘門形狀：{version: v1}。前提 quality_dip_facts 由 GATE_PREREQUISITES 檢查。"""
    gate = sizing["event_direction"]
    prefix = "position_sizing.event_direction"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, QUALITY_DIP_EVENT_DIRECTION_FIELDS, prefix, errors)
    if gate.get("version") != QUALITY_DIP_EVENT_DIRECTION:
        errors.append("%s.version 只允許 %s" % (prefix, QUALITY_DIP_EVENT_DIRECTION))


def validate_event_directions(event_report: Mapping[str, object], errors: List[str]) -> None:
    """event_direction 啟用時，事件報告的每則事件都必須有列舉內的 direction（Finalizer 重建驗證用）。"""
    for item in event_report.get("items", []):
        for event in item.get("events", []) if isinstance(item, Mapping) else []:
            if not isinstance(event, Mapping) or event.get("direction") not in EVENT_DIRECTIONS:
                errors.append(
                    "team_inputs.analyst_reports.event：%s 的事件 %s 必須有 direction（%s）；已啟用 position_sizing.event_direction"
                    % (item.get("symbol"), event.get("evidence_id") if isinstance(event, Mapping) else "?", "／".join(EVENT_DIRECTIONS))
                )


def validate_dip_assessment(
    item: Mapping[str, object], symbol: str, prefix: str, facts: Optional[Mapping[str, object]], errors: List[str],
) -> None:
    """buy／add 的 ``dip_assessment``：必填、只驗列舉值域；該股價格事實不可用（含整份 unavailable、找不到該股）時只能 undetermined。
    不驗與意圖、信心或 candidate 的對應（純紀錄，不影響配置）。"""
    value = item.get("dip_assessment")
    if value not in DIP_ASSESSMENTS:
        errors.append("%s.dip_assessment 必須是 %s" % (prefix, "／".join(DIP_ASSESSMENTS)))
        return
    if not isinstance(facts, Mapping):
        errors.append("%s：quality_dip_assessment 需要 team_inputs.dip_facts（無法確認該股價格事實是否可用）" % prefix)
        return
    row = next((entry for entry in facts.get("items", []) if str(entry.get("symbol", "")).upper() == symbol), None)  # type: ignore[union-attr]
    unavailable = facts.get("data_status") != "available" or row is None or row.get("price_pattern") == "unavailable"
    if unavailable and value != "undetermined":
        errors.append(
            "%s.dip_assessment：該股價格事實為 unavailable（資料不足，無法判斷是否被大盤拖累），只能填 undetermined" % prefix
        )


def _q(value: Decimal) -> str:
    return str(value.quantize(QUANTUM, rounding=ROUND_HALF_UP))


def _q2(value: Decimal) -> str:
    return str(value.quantize(_TWO, rounding=ROUND_HALF_UP))


def _taipei_date(moment) -> date:
    return moment.astimezone(TAIPEI).date()


# ---------------------------------------------------------------- 價格事實

class _Prices:
    """交易池逐檔收盤（日期 → 收盤）。bar 晚於最近交易日、可得時間缺時區或晚於 cutoff 一律拒絕。"""

    def __init__(self, bundle: Mapping[str, object], trade_date: str, cutoff):
        self.closes: Dict[str, Dict[str, Decimal]] = {}
        self.evidence: Dict[str, str] = {}
        rows = bundle.get("price_series")
        if not isinstance(rows, list):
            raise DecisionToolError("dip_facts 需要 price_series")
        for row in rows:
            symbol = str(row["symbol"]).upper()
            series: Dict[str, Decimal] = {}
            for bar in row["bars"]:
                day = str(bar["trade_date"])
                if day > trade_date:
                    raise DecisionToolError("dip_facts：%s 的行情 %s 晚於 snapshot.latest_trade_date %s" % (symbol, day, trade_date))
                if bar.get("available_at") is not None:
                    moment = parse_aware_time(bar["available_at"], "%s.bars.available_at" % symbol, error=DecisionToolError)
                    if moment > cutoff:
                        raise DecisionToolError("dip_facts：%s 的行情 %s 可得時間晚於 decision_cutoff" % (symbol, day))
                series[day] = decimal_value(bar["close"], "%s.close" % symbol)
            self.closes[symbol] = series
            self.evidence[symbol] = str(row.get("evidence_id"))
        self.days = sorted({day for series in self.closes.values() for day in series})

    def window_return(self, symbol: str, days: Sequence[str]) -> Optional[Decimal]:
        """``days`` 每一天都有收盤才算（窗內缺一天即 null，不補值）。"""
        series = self.closes[symbol]
        if any(day not in series for day in days) or series[days[0]] <= 0:
            return None
        return series[days[-1]] / series[days[0]] - Decimal(1)

    def ordered(self, symbol: str) -> List[Decimal]:
        series = self.closes[symbol]
        return [series[day] for day in sorted(series)]


def _price_position(prices: _Prices, symbol: str, trade_date: str) -> Dict[str, object]:
    """價格位置（不是估值）：距近 60／120 日收盤高點的回檔、收盤相對 MA20／MA60、是否創 20 日收盤新低。"""
    series = prices.closes[symbol]
    closes = prices.ordered(symbol)
    position: Dict[str, object] = {
        "close_vs_high_60d": None, "close_vs_high_120d": None, "close_vs_ma20": None, "close_vs_ma60": None,
        "new_low_20d": None,
    }
    if not closes or max(series) != trade_date:
        return position  # 最後一根不是最近交易日：位置會過期，全部 null
    last = closes[-1]
    for window in HIGH_WINDOWS:
        if len(closes) >= window:
            high = max(closes[-window:])
            if high > 0:
                position["close_vs_high_%dd" % window] = _q(last / high - Decimal(1))
    for window in (20, 60):
        if len(closes) >= window:
            average = sum(closes[-window:], Decimal(0)) / Decimal(window)
            if average > 0:
                position["close_vs_ma%d" % window] = _q(last / average - Decimal(1))
    if len(closes) >= NEW_LOW_WINDOW:
        position["new_low_20d"] = last <= min(closes[-NEW_LOW_WINDOW:])
    return position


def _index_return(bundle: Mapping[str, object], base: str, last: str, gaps: List[str]) -> Optional[Decimal]:
    """加權指數同期報酬；來源未核准、基準日或最近交易日沒有收盤時為 null 並記缺口（不影響交易池口徑）。"""
    from .market_index import _series

    series = _series(bundle, gaps)
    if not series:
        return None
    later = sorted(day for day in series if day > last)
    if later:
        raise DecisionToolError("dip_facts：加權指數 %s 晚於 snapshot.latest_trade_date %s" % (later[0], last))
    if base not in series or last not in series or series[base] <= 0:
        gaps.append("加權指數在基準日 %s 或最近交易日 %s 沒有收盤，相對加權指數一欄為 null" % (base, last))
        return None
    return series[last] / series[base] - Decimal(1)


def _price_facts(bundle: Mapping[str, object], trade_date: str, cutoff) -> Tuple[Dict[str, object], Dict[str, Dict[str, object]], List[str], Optional[str]]:
    """回傳 (視窗與池事實, 逐檔價格事實, 資料缺口, 基準日)。"""
    prices = _Prices(bundle, trade_date, cutoff)
    gaps: List[str] = []
    days = prices.days
    size = len(prices.closes)
    window: Dict[str, object] = {
        "return_days": WINDOW_DAYS, "base_date": None, "first_date": None, "last_date": None,
        "beta_window_days": BETA_WINDOW_DAYS, "beta_first_date": None, "beta_last_date": None,
        "new_data_rule": "available_at 的台北日期晚於基準日且不晚於 decision_cutoff",
    }
    pool: Dict[str, object] = {
        "size": size, "return_20d": None, "return_5d": None, "compound_return_20d": None,
        "index_return_20d": None, "residual_sample": 0, "min_coverage": str(MIN_COVERAGE),
    }
    per_symbol: Dict[str, Dict[str, object]] = {}
    available = True
    base: Optional[str] = None
    if not days or days[-1] != trade_date:
        gaps.append("行情最後一個交易日不是 snapshot.latest_trade_date %s" % trade_date)
        available = False
    if len(days) < WINDOW_DAYS + 1:
        gaps.append("交易日只有 %d 天，不足評估視窗 %d 日" % (len(days), WINDOW_DAYS + 1))
        available = False
    else:
        base = days[-(WINDOW_DAYS + 1)]
        window.update(base_date=base, first_date=days[-WINDOW_DAYS], last_date=days[-1])
    if len(days) < WINDOW_DAYS + BETA_WINDOW_DAYS + 1:
        gaps.append("交易日只有 %d 天，不足視窗前 beta 所需的 %d 天" % (len(days), WINDOW_DAYS + BETA_WINDOW_DAYS + 1))
        available = False
    else:
        window.update(beta_first_date=days[-(WINDOW_DAYS + BETA_WINDOW_DAYS + 1)], beta_last_date=base)

    window_days = days[-(WINDOW_DAYS + 1):] if base is not None else []
    short_days = days[-(SHORT_WINDOW_DAYS + 1):] if len(days) >= SHORT_WINDOW_DAYS + 1 else []
    own20 = {s: prices.window_return(s, window_days) if window_days else None for s in prices.closes}
    own5 = {s: prices.window_return(s, short_days) if short_days else None for s in prices.closes}
    values20 = [v for v in own20.values() if v is not None]
    values5 = [v for v in own5.values() if v is not None]
    pool20 = sum(values20, Decimal(0)) / Decimal(len(values20)) if values20 else None
    pool5 = sum(values5, Decimal(0)) / Decimal(len(values5)) if values5 else None
    pool["return_20d"] = None if pool20 is None else _q(pool20)
    pool["return_5d"] = None if pool5 is None else _q(pool5)

    compound: Optional[Decimal] = None
    pre_model: Optional[BetaModel] = None
    if available:
        # 評估視窗內的池等權日報酬：直接用 BetaModel（最後 20 個日報酬），與 beta 同一個日報酬定義。
        window_model = BetaModel(bundle, WINDOW_DAYS)
        if len(window_model.pool_returns) == WINDOW_DAYS:
            growth = Decimal(1)
            for value in window_model.pool_returns:
                growth *= Decimal(1) + value
            compound = growth - Decimal(1)
        else:
            gaps.append("評估視窗內有交易日沒有任何股票的日報酬，池報酬無法連乘")
            available = False
        # 視窗前 beta：只把行情截到基準日再建 BetaModel；模型本身與 exposure_review 用的是同一份程式。
        truncated = {"price_series": [
            {"symbol": row["symbol"], "bars": [bar for bar in row["bars"] if str(bar["trade_date"]) <= str(base)]}
            for row in bundle["price_series"]
        ]}
        pre_model = BetaModel(truncated, BETA_WINDOW_DAYS)
        if not pre_model.available:
            gaps.append("視窗前 %d 日的交易池日報酬不可用（缺日或變異為 0），beta 與殘差無法計算" % BETA_WINDOW_DAYS)
            available = False
    pool["compound_return_20d"] = None if compound is None else _q(compound)
    index20 = _index_return(bundle, base, days[-1], gaps) if base is not None and days else None
    pool["index_return_20d"] = None if index20 is None else _q(index20)

    residuals: Dict[str, Decimal] = {}
    betas: Dict[str, Optional[Decimal]] = {}
    for symbol in sorted(prices.closes):
        beta = pre_model.beta(symbol) if available and pre_model is not None else None
        betas[symbol] = beta
        if beta is not None and compound is not None and own20[symbol] is not None:
            residuals[symbol] = Decimal(_q(own20[symbol] - beta * compound))
    pool["residual_sample"] = len(residuals)
    if available and (not size or Decimal(len(residuals)) < MIN_COVERAGE * Decimal(size)):
        gaps.append("有殘差的股票 %d 檔，少於交易池 %d 檔的 %s" % (len(residuals), size, MIN_COVERAGE))
        available = False
    if not available:
        residuals = {}
    ranks = {symbol: index + 1 for index, symbol in enumerate(sorted(residuals, key=lambda s: (residuals[s], s)))}
    for symbol in sorted(prices.closes):
        own = own20[symbol]
        attribution = {
            "return_20d": None if own is None else _q(own),
            "relative_to_pool_20d": None if own is None or pool20 is None else _q(own - pool20),
            "return_5d": None if own5[symbol] is None else _q(own5[symbol]),
            "relative_to_pool_5d": None if own5[symbol] is None or pool5 is None else _q(own5[symbol] - pool5),
            "relative_to_index_20d": None if own is None or index20 is None else _q(own - index20),
            "beta_pre_window": None if betas[symbol] is None or not available else _q(betas[symbol]),
            "residual_20d": _q(residuals[symbol]) if symbol in residuals else None,
            "residual_rank": ranks.get(symbol),
            "residual_sample": len(residuals) if symbol in residuals else None,
        }
        if not residuals or symbol not in residuals:
            pattern = "unavailable"
        elif own is not None and compound is not None and (own >= 0 or compound >= 0):
            pattern = "no_decline"
        elif residuals[symbol] >= 0:
            pattern = "market_explained"
        else:
            pattern = "beyond_market"
        per_symbol[symbol] = {
            "evidence_ids": [prices.evidence[symbol]],
            "market_attribution": attribution,
            "price_position": _price_position(prices, symbol, trade_date),
            "price_pattern": pattern,
        }
    return {"window": window, "pool": pool, "data_status": "available" if available else "unavailable"}, per_symbol, gaps, base


# ---------------------------------------------------------------- 財報三個清單

class _Documents:
    """cutoff 前可得的文件（晚於 cutoff 不計入；可得時間缺時區即拒絕）。"""

    def __init__(self, bundle: Mapping[str, object], cutoff):
        self.by_type: Dict[str, Dict[str, List[Tuple[object, Mapping[str, object]]]]] = {}
        self.available_by_evidence: Dict[str, object] = {}
        for document in bundle["snapshot"].get("documents", []):
            if not isinstance(document, Mapping) or not document.get("symbol"):
                continue
            if document.get("document_type") not in ("monthly_revenue", "financial_statement", "material_event"):
                continue
            moment = parse_aware_time(document.get("available_at"), "文件可得時間", error=DecisionToolError)
            if moment > cutoff:
                continue
            symbol = str(document["symbol"]).upper()
            self.by_type.setdefault(str(document["document_type"]), {}).setdefault(symbol, []).append((moment, document))
            if document.get("source_evidence_id"):
                self.available_by_evidence[str(document["source_evidence_id"])] = moment
        news = bundle.get("company_news_input")
        if news is not None:
            from etf_agent.perception.company_news import news_by_symbol
            for rows in news_by_symbol(news).values():
                for row in rows:
                    moment = parse_aware_time(row.get("available_at"), "個股新聞可得時間", error=DecisionToolError)
                    if moment <= cutoff:
                        self.available_by_evidence[str(row["evidence_id"])] = moment

    def of(self, document_type: str, symbol: str) -> List[Tuple[object, Mapping[str, object]]]:
        return self.by_type.get(document_type, {}).get(symbol, [])


def _decimal_or_none(value: object) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _is_new(moment, base: Optional[str]) -> Optional[bool]:
    if base is None:
        return None
    return _taipei_date(moment) > date.fromisoformat(base)


FINANCIAL_INDUSTRIES = frozenset({"fh", "basi", "bd", "ins"})  # 與 corporate_parsing._FINANCIAL_INDUSTRIES 相同


def _financial_industry(documents: "_Documents", symbol: str) -> bool:
    return any(
        isinstance(document.get("financial_statement"), Mapping)
        and document["financial_statement"].get("industry") in FINANCIAL_INDUSTRIES
        for _, document in documents.of("financial_statement", symbol)
    )


def _revenue_flags(documents: _Documents, symbol: str, base: Optional[str], growth_level: str,
                   adverse: List[Dict[str, object]], concerns: List[Dict[str, object]], unknown: List[Dict[str, object]]) -> None:
    rows = documents.of("monthly_revenue", symbol)
    latest_by_period: Dict[str, Tuple[object, Mapping[str, object]]] = {}
    for moment, document in rows:
        body = document.get("monthly_revenue")
        if not isinstance(body, Mapping) or not body.get("revenue_period"):
            continue
        period = str(body["revenue_period"])
        if period not in latest_by_period or moment > latest_by_period[period][0]:
            latest_by_period[period] = (moment, document)
    if not latest_by_period:
        unknown.append({"item": "monthly_revenue", "reason_code": "NO_MONTHLY_REVENUE_DOCUMENT"})
        return
    periods = sorted(latest_by_period)
    moment, latest = latest_by_period[periods[-1]]
    new = _is_new(moment, base)
    if new is None:
        unknown.append({"item": "monthly_revenue_yoy_turn", "reason_code": "EVALUATION_WINDOW_UNAVAILABLE"})
        return
    if not new:
        return
    current = _decimal_or_none(latest["monthly_revenue"].get("yoy_pct"))
    if current is None:
        unknown.append({"item": "monthly_revenue_yoy_turn", "reason_code": "YOY_NOT_REPORTED",
                        "period": periods[-1], "evidence_ids": [latest.get("source_evidence_id")]})
        return
    if current >= 0:
        return
    previous = latest_by_period[periods[-2]][1] if len(periods) >= 2 else None
    before = _decimal_or_none(previous["monthly_revenue"].get("yoy_pct")) if previous is not None else None
    if before is None:
        unknown.append({"item": "monthly_revenue_yoy_turn", "reason_code": "NO_PREVIOUS_MONTH_YOY",
                        "period": periods[-1], "evidence_ids": [latest.get("source_evidence_id")]})
        return
    if before <= 0:
        return
    fact = {
        "item": "monthly_revenue_yoy_turned_negative", "period": periods[-1], "previous_period": periods[-2],
        "yoy_pct": _q2(current), "previous_yoy_pct": _q2(before),
        "evidence_ids": [latest.get("source_evidence_id"), previous.get("source_evidence_id")],
    }
    if growth_level == "industry_formula_unsupported" or _financial_industry(documents, symbol):
        # 金融業等月營收內容不可比的產業：不算突變，改列疑慮。金融股的淨利年增可算，growth_signal_quality 會是 comparable，
        # 所以另以財報的產業別（與 analysts／corporate_parsing 同一組金融業代碼）判定。
        concerns.append(dict(fact, note="產業報表格式使月營收與一般業不可比，轉負列為疑慮，不算新壞事"))
    else:
        adverse.append(fact)


def _metric_evidence(metric: Mapping[str, object]) -> List[str]:
    return sorted({str(dep.get("source_evidence_id")) for dep in metric.get("dependencies", []) if dep.get("source_evidence_id")})


def _metric_flags(metrics: Optional[Mapping[str, object]], documents: _Documents, base: Optional[str],
                  adverse: List[Dict[str, object]], concerns: List[Dict[str, object]], unknown: List[Dict[str, object]]) -> None:
    if not isinstance(metrics, Mapping):
        unknown.append({"item": "financial_metrics", "reason_code": "NO_FINANCIAL_METRICS"})
        return
    by_key = {str(metric.get("metric_key")): metric for metric in metrics.get("metrics", []) if isinstance(metric, Mapping)}
    for key, unit in CONCERN_METRICS:
        metric = by_key.get(key)
        if metric is None:
            unknown.append({"item": key, "reason_code": "METRIC_NOT_COMPUTED"})
            continue
        if metric.get("status") != "available":
            unknown.append({"item": key, "reason_code": metric.get("reason_code"), "period": metric.get("period")})
            continue
        currencies = {dep.get("currency") for dep in metric.get("dependencies", []) if isinstance(dep, Mapping)}
        value = _decimal_or_none(metric.get("value"))
        if metric.get("unit") != unit or len(currencies) > 1 or value is None:
            unknown.append({"item": key, "reason_code": "UNIT_CONFLICT", "period": metric.get("period")})
            continue
        if key == "net_income_yoy_pct":
            current = next((dep for dep in metric.get("dependencies", []) if dep.get("scope") == "current"), None)
            prior = next((dep for dep in metric.get("dependencies", []) if dep.get("scope") == "prior_year"), None)
            now = _decimal_or_none(current.get("value")) if current else None
            then = _decimal_or_none(prior.get("value")) if prior else None
            if now is not None and then is not None and now < 0 < then:
                moment = documents.available_by_evidence.get(str(current.get("source_evidence_id")))
                new = _is_new(moment, base) if moment is not None else None
                if new is None:
                    unknown.append({"item": "net_income_turned_loss", "reason_code": "AVAILABILITY_UNKNOWN",
                                    "period": metric.get("period"), "evidence_ids": _metric_evidence(metric)})
                elif new:
                    adverse.append({"item": "net_income_turned_loss", "period": metric.get("period"),
                                    "comparison_period": metric.get("comparison_period"), "evidence_ids": _metric_evidence(metric)})
                    continue  # 同一事實不重複列為疑慮
        if value < 0:
            concerns.append({
                "item": key, "value": _q2(value), "unit": unit, "period": metric.get("period"),
                "comparison_period": metric.get("comparison_period"), "evidence_ids": _metric_evidence(metric),
            })


def _cash_flow_flags(cash: Optional[Mapping[str, object]], concerns: List[Dict[str, object]], unknown: List[Dict[str, object]]) -> None:
    if not isinstance(cash, Mapping):
        unknown.append({"item": "cash_flow", "reason_code": "NO_CASH_FLOW_DOCUMENT"})
        return
    if cash.get("status") != "available":
        unknown.append({"item": "cash_flow", "reason_code": "CASH_FLOW_UNVERIFIED", "period": cash.get("period"),
                        "evidence_ids": list(cash.get("evidence_ids", []))})
        return
    if cash.get("currency") != "TWD":
        unknown.append({"item": "cash_flow", "reason_code": "UNIT_CONFLICT", "period": cash.get("period")})
        return
    for key in ("operating_cash_flow", "free_cash_flow"):
        value = _decimal_or_none(cash.get(key))
        if value is None:
            unknown.append({"item": key, "reason_code": "NOT_REPORTED", "period": cash.get("period")})
        elif value < 0:
            concerns.append({"item": key, "value": str(cash.get(key)), "currency": "TWD", "period": cash.get("period"),
                             "period_kind": cash.get("period_kind"), "evidence_ids": list(cash.get("evidence_ids", []))})


def _event_flags(event_item: Optional[Mapping[str, object]], documents: _Documents, base: Optional[str],
                 adverse: List[Dict[str, object]], unknown: List[Dict[str, object]], by_direction: bool = False) -> None:
    """事件面。舊判準：整檔 event_outlook 為 negative，且視窗內新可得的事件有 materiality: high。
    ``by_direction``（position_sizing.event_direction）：改看每則事件——視窗內新可得、materiality high 且 direction 為 negative；
    high 事件缺 direction（舊報告或 Agent 漏填）列「不知道」，不當成通過也不當成利空。"""
    if by_direction:
        _event_direction_flags(event_item, documents, base, adverse, unknown)
        return
    if not isinstance(event_item, Mapping):
        unknown.append({"item": "high_event_negative_outlook", "reason_code": "NO_EVENT_REPORT_ITEM"})
        return
    if event_item.get("event_outlook") != "negative":
        return
    high: List[str] = []
    for event in event_item.get("events", []):
        if not isinstance(event, Mapping) or event.get("materiality") != "high":
            continue
        moment = documents.available_by_evidence.get(str(event.get("evidence_id")))
        if moment is None:
            continue
        if _is_new(moment, base):
            high.append(str(event["evidence_id"]))
    if high:
        adverse.append({"item": "high_event_negative_outlook", "evidence_ids": sorted(high)})


def _event_direction_flags(event_item: Optional[Mapping[str, object]], documents: _Documents, base: Optional[str],
                           adverse: List[Dict[str, object]], unknown: List[Dict[str, object]]) -> None:
    if not isinstance(event_item, Mapping):
        unknown.append({"item": "high_event_negative_direction", "reason_code": "NO_EVENT_REPORT_ITEM"})
        return
    negative: List[str] = []
    undirected: List[str] = []
    for event in event_item.get("events", []):
        if not isinstance(event, Mapping) or event.get("materiality") != "high":
            continue
        moment = documents.available_by_evidence.get(str(event.get("evidence_id")))
        if moment is None or not _is_new(moment, base):
            continue
        direction = event.get("direction")
        if direction == "negative":
            negative.append(str(event["evidence_id"]))
        elif direction not in EVENT_DIRECTIONS:
            undirected.append(str(event["evidence_id"]))
    if negative:
        adverse.append({"item": "high_event_negative_direction", "evidence_ids": sorted(negative)})
    if undirected:
        unknown.append({"item": "high_event_negative_direction", "reason_code": "EVENT_DIRECTION_MISSING",
                        "evidence_ids": sorted(undirected)})


def _sorted(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    return sorted(rows, key=lambda row: canonical_sha256(row))


def _latest_iso(rows: Sequence[Tuple[object, Mapping[str, object]]]) -> Optional[str]:
    return max(moment for moment, _ in rows).astimezone(timezone.utc).isoformat() if rows else None  # type: ignore[union-attr]


def _fundamental_flags(
    bundle: Mapping[str, object], fundamental_metrics: Optional[Mapping[str, object]], documents: _Documents,
    cash: Mapping[str, Mapping[str, object]], base: Optional[str], symbol: str,
    event_report: Optional[Mapping[str, object]], event_direction: bool = False,
) -> Dict[str, object]:
    metrics_by_symbol = {
        str(item.get("symbol", "")).upper(): item
        for item in (fundamental_metrics or {}).get("items", []) if isinstance(item, Mapping)
    }
    metrics = metrics_by_symbol.get(symbol)
    adverse: List[Dict[str, object]] = []
    concerns: List[Dict[str, object]] = []
    unknown: List[Dict[str, object]] = []
    growth = growth_signal_quality(metrics or {})
    _revenue_flags(documents, symbol, base, str(growth["level"]), adverse, concerns, unknown)
    _metric_flags(metrics, documents, base, adverse, concerns, unknown)
    _cash_flow_flags(cash.get(symbol), concerns, unknown)
    if event_report is not None:
        items = {str(item.get("symbol", "")).upper(): item for item in event_report.get("items", []) if isinstance(item, Mapping)}
        _event_flags(items.get(symbol), documents, base, adverse, unknown, event_direction)
    revenue_rows = documents.of("monthly_revenue", symbol)
    financial_rows = documents.of("financial_statement", symbol)
    newest = [moment for moment, _ in (*revenue_rows, *financial_rows)]
    return {
        "new_adverse_facts": _sorted(adverse),
        "concerns": _sorted(concerns),
        "unknown": _sorted(unknown),
        "event_check": EVENT_CHECK_APPLIED if event_report is not None else EVENT_CHECK_NOT_APPLIED,
        "latest_revenue_available_at": _latest_iso(revenue_rows),
        "latest_financial_available_at": _latest_iso(financial_rows),
        "no_new_fundamental_data_in_window": None if base is None else not any(_is_new(m, base) for m in newest),
    }


# ---------------------------------------------------------------- 組裝、重算與簡報

def _require_inputs(bundle: Mapping[str, object]):
    if bundle.get("fundamental_period_policy") is None:
        raise DecisionToolError("position_sizing.quality_dip_facts 需要 fundamental_period_policy（team_inputs 2.6 以上）")
    snapshot = bundle.get("snapshot")
    trade_date = snapshot.get("latest_trade_date") if isinstance(snapshot, Mapping) else None
    if not isinstance(trade_date, str):
        raise DecisionToolError("dip_facts 需要 snapshot.latest_trade_date")
    cutoff = parse_aware_time(bundle.get("decision_cutoff"), "decision_cutoff", error=DecisionToolError)
    return trade_date, cutoff


def _compute(
    bundle: Mapping[str, object], gate: Mapping[str, object], fundamental_metrics: Optional[Mapping[str, object]],
    event_report: Optional[Mapping[str, object]], event_direction: bool = False,
) -> Tuple[Dict[str, object], Dict[str, Dict[str, object]], List[str]]:
    errors: List[str] = []
    validate_quality_dip_facts_gate({"quality_dip_facts": gate}, errors)
    if errors:
        raise DecisionToolError("；".join(errors))
    trade_date, cutoff = _require_inputs(bundle)
    meta, per_symbol, gaps, base = _price_facts(bundle, trade_date, cutoff)
    documents = _Documents(bundle, cutoff)
    cash = cash_flow_context(bundle)
    for symbol, entry in per_symbol.items():
        entry["fundamental_flags"] = _fundamental_flags(
            bundle, fundamental_metrics, documents, cash, base, symbol, event_report, event_direction,
        )
    return meta, per_symbol, gaps


def _candidate(pattern: str, flags: Mapping[str, object]) -> Tuple[str, Optional[str]]:
    """(candidate, cause)：互斥；有新壞事、沒跌或資料不可用時為 none。超跌固定帶 cause: unknown（原因程式分不出來）。"""
    if flags["new_adverse_facts"] or pattern not in ("market_explained", "beyond_market"):
        return "none", None
    if pattern == "market_explained":
        return "market_dragged", None
    return "oversold", "unknown"


def build_dip_facts(
    bundle: Mapping[str, object], gate: Mapping[str, object], fundamental_metrics: Optional[Mapping[str, object]],
    event_report: Mapping[str, object], event_direction: bool = False,
) -> Dict[str, object]:
    """完整事實（交易 Agent 背景與 team_inputs.dip_facts）：含事件面一項、price_pattern 與 candidate。
    ``event_direction`` 為 True（position_sizing.event_direction）時事件面改看每則事件的 direction；預設 False，與舊版逐位元相同。"""
    if not isinstance(event_report, Mapping):
        raise DecisionToolError("dip_facts 需要事件分析師報告（事件面負面一項）")
    meta, per_symbol, gaps = _compute(bundle, gate, fundamental_metrics, event_report, event_direction)
    items = []
    for symbol in sorted(per_symbol):
        entry = per_symbol[symbol]
        candidate, cause = _candidate(str(entry["price_pattern"]), entry["fundamental_flags"])  # type: ignore[arg-type]
        items.append({
            "symbol": symbol,
            "evidence_ids": entry["evidence_ids"],
            "market_attribution": entry["market_attribution"],
            "price_position": entry["price_position"],
            "fundamental_flags": entry["fundamental_flags"],
            "price_pattern": entry["price_pattern"],
            "candidate": candidate,
            "cause": cause,
        })
    body: Dict[str, object] = {
        "schema_version": DIP_FACTS_SCHEMA_VERSION,
        "version": str(gate["version"]),
        "as_of": str(bundle["snapshot"]["latest_trade_date"]),  # type: ignore[index]
        "data_status": meta["data_status"],
        "window": meta["window"],
        "pool": meta["pool"],
        "data_gaps": gaps,
        "note": DIP_NOTE,
        "items": items,
    }
    body["facts_id"] = "dip-facts:" + canonical_sha256(body)[:20]
    return body


def analyst_dip_view(
    bundle: Mapping[str, object], gate: Mapping[str, object], fundamental_metrics: Optional[Mapping[str, object]],
) -> Dict[str, object]:
    """分析師階段的事實（事件報告尚未完成）：技術分析師拿價格事實、基本面分析師拿三個清單；都不含 price_pattern／candidate。"""
    meta, per_symbol, gaps = _compute(bundle, gate, fundamental_metrics, None)
    background = {
        "citable": False, "version": str(gate["version"]), "data_status": meta["data_status"], "window": meta["window"],
        "pool": meta["pool"], "data_gaps": gaps, "note": ANALYST_NOTE,
    }
    return {
        "background": background,
        "technical": {
            symbol: {
                "market_attribution": dict(entry["market_attribution"], evidence_ids=entry["evidence_ids"]),
                "price_position": dict(entry["price_position"], evidence_ids=entry["evidence_ids"]),
            }
            for symbol, entry in per_symbol.items()
        },
        "fundamental": {symbol: {"fundamental_flags": entry["fundamental_flags"]} for symbol, entry in per_symbol.items()},
    }


def validate_dip_facts(
    bundle: Mapping[str, object], policy: Mapping[str, object], team: Mapping[str, object], errors: List[str],
) -> None:
    """以 bundle、team_inputs 的基本面指標與事件報告整份重算 ``team_inputs.dip_facts``；缺少、多餘欄位或不一致即拒絕。"""
    prefix = "team_inputs.dip_facts"
    facts = team.get(DIP_FACTS_KEY)
    if not isinstance(facts, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    gate = quality_dip_facts_gate(policy.get("position_sizing"))
    if gate is None:
        errors.append("%s 只能用於啟用 position_sizing.quality_dip_facts 的 Policy" % prefix)
        return
    reject_unknown_fields(facts, FACTS_FIELDS, prefix, errors)
    reports = team.get("analyst_reports")
    event = reports.get("event") if isinstance(reports, Mapping) else None
    metrics = team.get("fundamental_metrics")
    by_direction = event_direction_gate(policy.get("position_sizing")) is not None
    if by_direction and isinstance(event, Mapping):
        validate_event_directions(event, errors)
    try:
        expected = build_dip_facts(
            bundle, gate, metrics if isinstance(metrics, Mapping) else None, event, by_direction,  # type: ignore[arg-type]
        )
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        errors.append("%s 無法以共同輸入重算：%s" % (prefix, error))
        return
    if dict(facts) != expected:
        errors.append("%s 與重算結果不同，拒絕改寫" % prefix)


def _strip_evidence(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _strip_evidence(item) for key, item in value.items() if key != "evidence_ids"}
    if isinstance(value, list):
        return [_strip_evidence(item) for item in value]
    return value


def dip_background_brief(facts: Mapping[str, object]) -> Dict[str, object]:
    """給交易 Agent 的背景：沒有 evidence_id、沒有 facts_id，``citable=false``，不可引用。"""
    return {
        "background": {
            "citable": False, "version": facts["version"], "data_status": facts["data_status"], "window": facts["window"],
            "pool": facts["pool"], "data_gaps": facts["data_gaps"], "note": facts["note"],
        },
        "by_symbol": {
            str(item["symbol"]): _strip_evidence({key: item[key] for key in (
                "market_attribution", "price_position", "fundamental_flags", "price_pattern", "candidate", "cause",
            )})
            for item in facts["items"]  # type: ignore[union-attr]
        },
    }


def dip_reference_tokens(facts: Optional[Mapping[str, object]]) -> List[str]:
    """交易 Agent 文字欄位不得出現的識別碼（背景不是證據）；沒有事實（舊 run）回空清單，驗證不變。"""
    if not isinstance(facts, Mapping):
        return []
    tokens = ["dip_background", DIP_FACTS_KEY, facts.get("facts_id")]
    return [token for token in tokens if isinstance(token, str) and token.strip()]


__all__ = [
    "CANDIDATES", "DIP_ASSESSMENTS", "DIP_FACTS_KEY", "QUALITY_DIP_ASSESSMENT", "quality_dip_assessment_gate",
    "EVENT_DIRECTIONS", "event_direction_gate", "validate_event_direction_gate", "validate_event_directions",
    "validate_dip_assessment", "validate_quality_dip_assessment_gate", "PRICE_PATTERNS", "QUALITY_DIP_FACTS", "analyst_dip_view", "build_dip_facts",
    "dip_background_brief", "dip_reference_tokens", "quality_dip_facts_gate", "validate_dip_facts",
    "validate_quality_dip_facts_gate",
]
