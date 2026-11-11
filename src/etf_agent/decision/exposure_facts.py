"""組合曝險事實與風險 Agent 的回補意見（``position_sizing.exposure_view`` v1 版本閘門，只由人設定、策略反思不得改）。

設計見 ``docs/agent_led_exposure_plan.md`` 第 3.5、4.1、6 節；本模組只做**階段 1：事實與紀錄**：

- 程式依同一份 bundle 與 Policy 確定性重算組合曝險（直接重用 ``exposure.build_exposure_review``，與 ``exposure_review`` 同口徑）、
  依 Policy 綁定的 ``sector_by_symbol`` 算持股的產業權重、最近一個交易池上漲日的報酬，以及非持股的 beta（``exposure.BetaModel``，
  同一個 ``beta_window_days``）。整份另存為 ``team_inputs.exposure_facts``，Finalizer 以同一份輸入整份重算比對。
- 風險 Agent 簡報附組合曝險與產業權重事實（背景、``citable=false``、沒有 evidence_id），輸出多一個必填列舉 ``exposure_view``
  （``keep_defensive``／``start_rebuild``／``rebuild``），**純紀錄，不接進任何配置**；換股提名簡報（agent_swap 也開啟時）
  另附轉向、加權指數、組合曝險、上漲日報酬與新股 beta 事實，同樣是背景、不改提名的任何驗證規則。

口徑（版本 v1 寫死，不是旋鈕）：

- 上漲日＝交易池等權日報酬（與 ``turn_facts`` 同一個 ``_Pool.pool_return``）大於 0 的交易日，從最近交易日往回最多找
  ``UP_DAY_LOOKBACK`` 個交易日，找不到即 ``unavailable``。
- 持股當日貢獻＝Σ（本次決策的起始權重 × 該股當日收盤報酬）；起始權重是 ``holding_weights``（與配置引擎相同的 NAV 口徑），
  上漲日不是最近交易日時權重仍是本次決策的權重。任一持股缺當日或前一日收盤時，該股報酬為 null、貢獻整體為 null（不當成 0）。
- 產業代碼只讀 Policy 的 ``sector_by_symbol``（policy_builder 綁定、有版本）；缺代碼的持股歸 ``unknown``。
  金融保險為 ``industry:17``（證交所／櫃買中心產業別代碼）。

任何 bar 晚於 ``snapshot.latest_trade_date``、bar 可得時間缺時區或晚於 ``decision_cutoff`` 一律拒絕；缺資料只降級為 null，不補值。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.core import canonical_sha256, parse_aware_time

from .exposure import LOW_BETA_THRESHOLD, beta_model, build_exposure_review, exposure_rotation_gate, holding_weights
from .fields import DecisionToolError, reject_unknown_fields

EXPOSURE_VIEW_GATE = "v1"
EXPOSURE_VIEW_GATE_FIELDS = {"version"}
EXPOSURE_VIEWS = ("keep_defensive", "start_rebuild", "rebuild")
EXPOSURE_FACTS_KEY = "exposure_facts"  # team_inputs 的選配鍵與簡報區塊鍵：與 Policy 閘門雙向同時存在
EXPOSURE_FACTS_SCHEMA_VERSION = "1.0"
UP_DAY_LOOKBACK = 20
FINANCIAL_INSURANCE_SECTOR = "industry:17"
UNKNOWN_SECTOR = "unknown"
QUANTUM = Decimal("0.000001")
FACTS_FIELDS = {
    "schema_version", "version", "facts_id", "as_of", "note", "portfolio", "sectors", "up_day", "holdings", "non_held",
    "data_gaps",
}
FACTS_NOTE = (
    "這些是程式算的組合曝險、產業權重、上漲日報酬與 beta 事實，不是訊號，也不決定現金、權重、股數或換股；"
    "沒有 evidence_id，不可引用。beta 與捕捉率都是對交易池等權的歷史事實（缺資料為 null，不補值）。"
    "持股當日貢獻＝Σ（本次決策的起始權重 × 該股當日收盤報酬），上漲日不是最近交易日時權重仍是本次決策的權重，只是估計。"
    "低 beta 占比是 beta 低於門檻的持股權重合計，不代表這些持股該賣。"
)
RISK_NOTE = (
    "組合曝險與產業權重是程式算的背景事實（citable=false、沒有 evidence_id、不可引用），用來判斷 exposure_view："
    "組合有多防守、反彈日跟上了多少。exposure_view 只是紀錄你認為組合該不該開始回補市場參與度，"
    "不改現金姿態、現金比例、持股廣度、換股或任何配置；反彈可能失敗，證據不足就填 keep_defensive。"
)
NOMINATION_NOTE = (
    "以下是程式算的轉向、加權指數、組合曝險、產業權重、上漲日報酬與新股 beta 事實，全部是背景（citable=false、沒有 evidence_id），"
    "不可放進 buy_basis_ids／sell_basis_ids，也不改變提名規則：每一對仍需換入股已採納的多頭論點與換出股已採納的空頭論點或"
    "持股檢視事實，市場參與度低本身不是賣出理由，也不得純粹為了買別檔而賣。"
)
# agent_swap.exposure 啟用時的背景說明：組合層級事實仍不可引用；市場參與度低只在 purpose=exposure 的配對可以作為讓位理由。
NOMINATION_NOTE_EXPOSURE = (
    "以下是程式算的轉向、加權指數、組合曝險、產業權重、上漲日報酬與新股 beta 事實，全部是背景（citable=false、沒有 evidence_id），"
    "不可放進 buy_basis_ids／sell_basis_ids。purpose=thesis 的配對規則不變：需要換出股已採納的空頭論點或持股檢視事實，"
    "不得純粹為了買別檔而賣；purpose=exposure 的配對才可以以市場參與度過低為由讓位，換出依據用持股的曝險事實編號"
    "（exposure_basis，程式算的該股事實），並寫明為什麼是這檔讓位、放掉它的代價。"
)


def exposure_view_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """exposure_view 啟用時回傳閘門物件；沒有閘門回 None（簡報、輸出白名單與 team_inputs 與舊版逐鍵相同）。"""
    gate = sizing.get("exposure_view") if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def validate_exposure_view_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """閘門形狀：{version: v1}；前提（exposure_rotation、cash_stance_turn_facts、market_index_facts）由 GATE_PREREQUISITES 檢查，
    typed_evidence_v1 在驗證現金姿態時檢查。"""
    gate = sizing["exposure_view"]
    prefix = "position_sizing.exposure_view"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, EXPOSURE_VIEW_GATE_FIELDS, prefix, errors)
    if gate.get("version") != EXPOSURE_VIEW_GATE:
        errors.append("%s.version 只允許 %s" % (prefix, EXPOSURE_VIEW_GATE))


def _q(value: Decimal) -> str:
    return str(value.quantize(QUANTUM, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- 事實

def _check_bars(bundle: Mapping[str, object], trade_date: str, cutoff) -> None:
    """BetaModel 本身不檢查時間：先拒絕晚於最近交易日、可得時間缺時區或晚於 cutoff 的 bar（做法同 dip_facts）。"""
    rows = bundle.get("price_series")
    if not isinstance(rows, list):
        raise DecisionToolError("exposure_facts 需要 price_series")
    for row in rows:
        symbol = str(row["symbol"]).upper()
        for bar in row["bars"]:
            day = str(bar["trade_date"])
            if day > trade_date:
                raise DecisionToolError("exposure_facts：%s 的行情 %s 晚於 snapshot.latest_trade_date %s" % (symbol, day, trade_date))
            if bar.get("available_at") is not None:
                moment = parse_aware_time(bar["available_at"], "%s.bars.available_at" % symbol, error=DecisionToolError)
                if moment > cutoff:
                    raise DecisionToolError("exposure_facts：%s 的行情 %s 可得時間晚於 decision_cutoff" % (symbol, day))


def _sectors(weights: Mapping[str, Decimal], policy: Mapping[str, object]) -> Dict[str, object]:
    raw = policy.get("sector_by_symbol")
    if not isinstance(raw, Mapping):
        raise DecisionToolError("exposure_facts 需要 Policy 綁定的 sector_by_symbol")
    sector_by_symbol = {str(symbol).upper(): str(sector) for symbol, sector in raw.items()}
    groups: Dict[str, List[str]] = {}
    for symbol in sorted(weights):
        groups.setdefault(sector_by_symbol.get(symbol, UNKNOWN_SECTOR), []).append(symbol)

    def row(sector: str, symbols: Sequence[str]) -> Dict[str, object]:
        return {
            "sector": sector, "count": len(symbols), "symbols": list(symbols),
            "weight": _q(sum((weights[s] for s in symbols), Decimal(0))),
        }

    items = sorted((row(sector, symbols) for sector, symbols in groups.items()),
                   key=lambda item: (-Decimal(str(item["weight"])), str(item["sector"])))
    return {
        "items": items,
        "financial_insurance": row(FINANCIAL_INSURANCE_SECTOR, groups.get(FINANCIAL_INSURANCE_SECTOR, [])),
        "unknown_symbols": list(groups.get(UNKNOWN_SECTOR, [])),
    }


def _up_day(bundle: Mapping[str, object], trade_date: str, weights: Mapping[str, Decimal], gaps: List[str]):
    """(上漲日事實, 逐檔持股當日報酬)。池報酬沿用 turn_facts 的 ``_Pool.pool_return``（與 pool_return_latest 同一個數字）。"""
    from .turn_facts import _Pool

    pool = _Pool(bundle, trade_date)
    result: Dict[str, object] = {
        "status": "unavailable", "lookback_days": UP_DAY_LOOKBACK, "date": None, "previous_date": None,
        "is_latest_trade_date": None, "trading_days_ago": None, "pool_return": None, "holdings_contribution": None,
        "weight_basis": "decision_start_weights", "missing_symbols": [],
    }
    returns: Dict[str, Optional[str]] = {symbol: None for symbol in weights}
    if not pool.days:
        gaps.append("price_series 沒有任何行情，上漲日無法計算")
        return result, returns
    if pool.days[-1] != trade_date:
        gaps.append("行情最後一個交易日 %s 不是 snapshot.latest_trade_date %s" % (pool.days[-1], trade_date))
    last = len(pool.days) - 1
    found: Optional[int] = None
    for index in range(last, max(last - UP_DAY_LOOKBACK, 0), -1):
        value = pool.pool_return(index)
        if value is not None and value > 0:
            found = index
            break
    if found is None:
        gaps.append("最近 %d 個交易日內交易池沒有上漲日，上漲日報酬不可用" % UP_DAY_LOOKBACK)
        return result, returns
    day, before = pool.days[found], pool.days[found - 1]
    contribution = Decimal(0)
    missing: List[str] = []
    for symbol in sorted(weights):
        dates = pool.dates_by_symbol.get(symbol, [])
        closes = pool.closes_by_symbol.get(symbol, [])
        series = dict(zip(dates, closes))
        start, end = series.get(before), series.get(day)
        if start is None or end is None or start <= 0:
            missing.append(symbol)
            continue
        change = end / start - Decimal(1)
        returns[symbol] = _q(change)
        contribution += weights[symbol] * change
    if missing:
        gaps.append("持股 %s 缺上漲日或前一日收盤，持股當日貢獻為 null（不當成 0）" % "、".join(missing))
    result.update(
        status="available", date=day, previous_date=before, is_latest_trade_date=day == trade_date,
        trading_days_ago=last - found, pool_return=_q(pool.pool_return(found)),  # type: ignore[arg-type]
        holdings_contribution=None if missing else _q(contribution), missing_symbols=missing,
    )
    return result, returns


def build_exposure_facts(bundle: Mapping[str, object], policy: Mapping[str, object]) -> Dict[str, object]:
    """完整事實（team_inputs.exposure_facts）：只依 bundle 與 Policy 中 base／final 相同的欄位
    （exposure_rotation.beta_window_days、sector_by_symbol），不讀任何 Agent 輸出。"""
    sizing = policy.get("position_sizing")
    gate = exposure_view_gate(sizing)
    if gate is None:
        raise DecisionToolError("DecisionPolicy 沒有 position_sizing.exposure_view，不應建立組合曝險事實")
    errors: List[str] = []
    validate_exposure_view_gate(sizing, errors)  # type: ignore[arg-type]
    if errors:
        raise DecisionToolError("；".join(errors))
    exposure_gate = exposure_rotation_gate(sizing)
    if exposure_gate is None:
        raise DecisionToolError("position_sizing.exposure_view 需要 exposure_rotation（同一個 beta 口徑與 exposure_review）")
    snapshot = bundle.get("snapshot")
    trade_date = snapshot.get("latest_trade_date") if isinstance(snapshot, Mapping) else None
    if not isinstance(trade_date, str):
        raise DecisionToolError("exposure_facts 需要 snapshot.latest_trade_date")
    cutoff = parse_aware_time(bundle.get("decision_cutoff"), "decision_cutoff", error=DecisionToolError)
    _check_bars(bundle, trade_date, cutoff)
    review = build_exposure_review(bundle, policy)
    weights, _, _ = holding_weights(bundle)
    gaps: List[str] = []
    items = {str(item["symbol"]): item for item in review["items"]}  # type: ignore[union-attr]
    low = [s for s, item in items.items() if item["beta"] is not None and Decimal(str(item["beta"])) < LOW_BETA_THRESHOLD]
    portfolio = {
        **dict(review["portfolio"]),  # type: ignore[arg-type]
        "held_count": len(items),
        "low_beta_count": len(low),
        "beta_window_days": review["policy"]["beta_window_days"],  # type: ignore[index]
        "beta_first_date": review["pool"]["first_date"],  # type: ignore[index]
        "beta_last_date": review["pool"]["last_date"],  # type: ignore[index]
    }
    if portfolio["uncovered_symbols"]:
        gaps.append("持股 %s 的 beta 不可用，組合 beta 與低 beta 占比為 null" % "、".join(portfolio["uncovered_symbols"]))  # type: ignore[arg-type]
    sectors = _sectors(weights, policy)
    up_day, day_returns = _up_day(bundle, trade_date, weights, gaps)
    sector_of = {symbol: row["sector"] for row in sectors["items"] for symbol in row["symbols"]}  # type: ignore[union-attr]
    holdings = [
        {**{key: items[symbol][key] for key in ("symbol", "weight", "beta", "up_capture", "down_capture")},
         "sector": sector_of[symbol], "up_day_return": day_returns[symbol]}
        for symbol in sorted(items)
    ]
    model = beta_model(bundle, exposure_gate)
    pool_symbols = sorted({str(row["symbol"]).upper() for row in bundle["price_series"]})  # type: ignore[union-attr]
    raw_sectors = policy["sector_by_symbol"]  # type: ignore[index]
    sector_by_symbol = {str(s).upper(): str(v) for s, v in raw_sectors.items()}  # type: ignore[union-attr]
    non_held = []
    for symbol in pool_symbols:
        if symbol in items:
            continue
        beta, up, down = model.stats(symbol)
        non_held.append({
            "symbol": symbol, "beta": None if beta is None else _q(beta), "up_capture": None if up is None else _q(up),
            "down_capture": None if down is None else _q(down), "sector": sector_by_symbol.get(symbol, UNKNOWN_SECTOR),
        })
    body: Dict[str, object] = {
        "schema_version": EXPOSURE_FACTS_SCHEMA_VERSION,
        "version": str(gate["version"]),
        "as_of": trade_date,
        "note": FACTS_NOTE,
        "portfolio": portfolio,
        "sectors": sectors,
        "up_day": up_day,
        "holdings": holdings,
        "non_held": non_held,
        "data_gaps": gaps,
    }
    body["facts_id"] = "exposure-facts:" + canonical_sha256(body)[:20]
    return body


def validate_exposure_facts(
    bundle: Mapping[str, object], policy: Mapping[str, object], team: Mapping[str, object], errors: List[str],
) -> None:
    """以 bundle 與 Policy 整份重算 ``team_inputs.exposure_facts``；缺少、多餘欄位或不一致即拒絕。"""
    prefix = "team_inputs.exposure_facts"
    facts = team.get(EXPOSURE_FACTS_KEY)
    if not isinstance(facts, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    if exposure_view_gate(policy.get("position_sizing")) is None:
        errors.append("%s 只能用於啟用 position_sizing.exposure_view 的 Policy" % prefix)
        return
    reject_unknown_fields(facts, FACTS_FIELDS, prefix, errors)
    try:
        expected = build_exposure_facts(bundle, policy)
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        errors.append("%s 無法以共同輸入重算：%s" % (prefix, error))
        return
    if dict(facts) != expected:
        errors.append("%s 與重算結果不同，拒絕改寫" % prefix)


# ---------------------------------------------------------------- 簡報

def risk_exposure_brief(facts: Mapping[str, object]) -> Dict[str, object]:
    """風險 Agent 的背景：組合曝險、產業權重與上漲日摘要；沒有 facts_id、沒有逐檔的非持股 beta，``citable=false``。"""
    return {
        "citable": False,
        "note": RISK_NOTE,
        "as_of": facts["as_of"],
        "portfolio": facts["portfolio"],
        "sectors": facts["sectors"],
        "up_day": facts["up_day"],
        "data_gaps": facts["data_gaps"],
    }


def nomination_exposure_brief(
    facts: Mapping[str, object],
    candidate_symbols: Sequence[str],
    turn: Mapping[str, object],
    index: Mapping[str, object],
    turn_state: Optional[Mapping[str, object]] = None,
    exposure_swap: bool = False,
) -> Dict[str, object]:
    """換股提名簡報的背景區塊：轉向、加權指數、組合曝險、產業權重、上漲日報酬（含逐檔持股）與新股 beta（只列提名候選）。

    ``exposure_swap``（position_sizing.agent_swap.exposure 啟用）時改用 ``NOMINATION_NOTE_EXPOSURE``：purpose=exposure 的配對
    以曝險事實編號為依據；沒有閘門時與階段 1 逐位元相同。"""
    wanted = {str(symbol).upper() for symbol in candidate_symbols}
    brief: Dict[str, object] = {
        "citable": False,
        "note": NOMINATION_NOTE_EXPOSURE if exposure_swap else NOMINATION_NOTE,
        "as_of": facts["as_of"],
        "turn_facts": dict(turn),
        "market_index_facts": dict(index),
        "portfolio": facts["portfolio"],
        "sectors": facts["sectors"],
        "up_day": facts["up_day"],
        "holdings": {
            str(row["symbol"]): {"sector": row["sector"], "up_day_return": row["up_day_return"]}
            for row in facts["holdings"]  # type: ignore[union-attr]
        },
        "candidate_betas": {
            str(row["symbol"]): {key: row[key] for key in ("beta", "up_capture", "down_capture", "sector")}
            for row in facts["non_held"] if str(row["symbol"]) in wanted  # type: ignore[union-attr]
        },
        "data_gaps": facts["data_gaps"],
    }
    if turn_state is not None:
        brief["turn_state"] = {"citable": False, **dict(turn_state)}
    return brief


# ---------------------------------------------------------------- 風險 Agent 的列舉

def exposure_reference_tokens(facts: Optional[Mapping[str, object]]) -> List[str]:
    """風險 Agent 文字欄位不得出現的識別碼（背景不是證據）：簡報區塊鍵名，以及有事實時的 facts_id。"""
    tokens = [EXPOSURE_FACTS_KEY]
    if isinstance(facts, Mapping) and isinstance(facts.get("facts_id"), str) and facts["facts_id"].strip():
        tokens.append(str(facts["facts_id"]))
    return tokens


def validate_exposure_view(
    stance: Mapping[str, object], evidence: Sequence[str], market_evidence_ids: object,
    facts: Optional[Mapping[str, object]], prefix: str, errors: List[str],
) -> None:
    """列舉必填且在值域內；非 keep_defensive 時必須引用至少一個市場層級證據；理由不得寫出背景識別碼。
    不驗列舉與現金姿態、turn_assessment 的對應（純紀錄，不接進配置）。"""
    value = stance.get("exposure_view")
    if not isinstance(value, str) or value not in EXPOSURE_VIEWS:
        errors.append("%s.exposure_view 必須是 %s" % (prefix, "／".join(EXPOSURE_VIEWS)))
    elif value != "keep_defensive" and not set(evidence) & set(market_evidence_ids or ()):  # type: ignore[arg-type]
        errors.append(
            "%s.exposure_view=%s（不是 keep_defensive）時 evidence_ids 至少要引用一個市場層級證據；"
            "組合曝險事實是背景，不能單獨支持回補" % (prefix, value)
        )
    rationale = stance.get("rationale")
    cited = sorted(token for token in exposure_reference_tokens(facts) if isinstance(rationale, str) and token in rationale)
    if cited:
        errors.append("%s.rationale 不得寫出組合曝險背景的識別碼（%s）；它不是證據" % (prefix, ", ".join(cited)))


__all__ = [
    "EXPOSURE_FACTS_KEY", "EXPOSURE_VIEWS", "EXPOSURE_VIEW_GATE", "build_exposure_facts", "exposure_reference_tokens",
    "exposure_view_gate", "nomination_exposure_brief", "risk_exposure_brief", "validate_exposure_facts",
    "validate_exposure_view", "validate_exposure_view_gate",
]
