"""風險 Agent 現金姿態的「0050 基準事實」（position_sizing.benchmark_facts v1 版本閘門，已實作、尚未開啟）。

紀錄工具，不是規則：程式依決策輸入的選配鍵 ``benchmark_input``（0050 官方收盤序列與人工抄錄的前幾大成分權重，
只在閘門開啟時建立、進 bundle 雜湊）與同 Snapshot 的帳戶、行情，重算四個區塊：

- ``benchmark_return``：0050 最近 1 日、5 日報酬；
- ``holdings_gap_1d``：持股依本次起始權重估的最近一日貢獻，減 0050 當日報酬；
- ``active_weights``：0050 前幾大成分的權重、組合起始權重、差額與該股規則上限，另附持股檔資料日與距今交易日數；
- ``beta_to_benchmark``：組合（含現金，現金 beta 為 0）對 0050 的 beta，視窗沿用 ``exposure_rotation.beta_window_days``。

全部是 Decimal 字串，``citable=false``、沒有 evidence_id，Agent 不算數字；缺資料時該區塊 ``status=unavailable`` 並寫原因，
不補假值。事實由決策輸入重算、不新增 ``team_inputs`` 鍵、不升版（比照 ``market_index_facts``）。沒有閘門時決策輸入沒有
``benchmark_input``、簡報與提示完全不變。時間一致性：價格 ``fetched_at``、持股檔 ``available_at`` 不得晚於
``decision_cutoff``、須含時區；來源未核准、權重或代號不合法即拒絕。
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.core import canonical_sha256, parse_aware_time

from .exposure import exposure_rotation_gate, holding_weights
from .fields import DecisionToolError, decimal_value, reject_unknown_fields

BENCHMARK_FACTS = "v1"
BENCHMARK_FACTS_FIELDS = {"version", "max_holdings_age_days"}
BENCHMARK_INPUT_KEY = "benchmark_input"
BENCHMARK_FACTS_KEY = "benchmark_facts"
BENCHMARK_INPUT_SCHEMA = "benchmark-input-1"
BENCHMARK_ID = "0050_buy_and_hold"
PRICE_SOURCE = "TWSE_STOCK_DAY"
HOLDINGS_ETF_CODE = "0050"
HOLDINGS_FIELDS = ["etf_code", "rank", "stock_name", "symbol", "weight", "source_date", "available_at", "source"]
MAX_HOLDINGS_ROWS = 10
MAX_AGE_DAYS_LIMIT = 60
PRICE_LOOKBACK_CALENDAR_DAYS = 130
QUANTUM = Decimal("0.000001")
MIN_VARIANCE = Decimal("0.000000000001")  # 視窗內 0050 日報酬離差平方和低於此值視為沒有變動
BENCHMARK_NOTE = (
    "這是 0050 基準的事實描述，不是目標、不是訊號，也不決定現金姿態、現金下限、權重或換股，沒有 evidence_id、不可引用。"
    "規則上單股上限 10%（2330 為 25%），帳戶無法複製 0050 的集中度，與 0050 的差距不代表該補足。"
    "落後基準可能是錯過反彈，也可能只是不追高：追高被套與錯過上漲都是成本，請連同市場層級證據一起判斷，"
    "不要只憑與基準的差距改變姿態。成分權重是人工抄錄、會隨股價漂移，只涵蓋前幾大；beta 的視窗與交易池 beta 相同，視窗不足時不提供。"
    "僅依本次截止前可得的資料判斷，不要補入未提供的歷史案例或後續報酬。"
)


def benchmark_facts_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """benchmark_facts 啟用時回傳閘門物件；沒有閘門回 None（決策輸入、簡報與舊版逐位元相同）。"""
    gate = sizing.get(BENCHMARK_FACTS_KEY) if isinstance(sizing, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def validate_benchmark_facts_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """形狀：{version: v1, max_holdings_age_days: 正整數（由人填，程式不給預設）}。"""
    gate = sizing[BENCHMARK_FACTS_KEY]
    prefix = "position_sizing.%s" % BENCHMARK_FACTS_KEY
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, BENCHMARK_FACTS_FIELDS, prefix, errors)
    if gate.get("version") != BENCHMARK_FACTS:
        errors.append("%s.version 只允許 %s" % (prefix, BENCHMARK_FACTS))
    age = gate.get("max_holdings_age_days")
    if isinstance(age, bool) or not isinstance(age, int) or not 1 <= age <= MAX_AGE_DAYS_LIMIT:
        errors.append("%s.max_holdings_age_days 必須是 1 到 %d 的整數（由人填）" % (prefix, MAX_AGE_DAYS_LIMIT))


def _q(value: Decimal) -> str:
    return str(value.quantize(QUANTUM, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- 決策輸入（benchmark_input）

def _approved_holdings_sources(approvals_path: Path, cutoff: datetime) -> Dict[str, Mapping[str, object]]:
    try:
        config = json.loads(Path(approvals_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DecisionToolError("無法讀取 0050 成分權重來源核准檔：%s" % approvals_path) from error
    approved: Dict[str, Mapping[str, object]] = {}
    for item in config.get("sources", []) if isinstance(config, Mapping) else []:
        if not isinstance(item, Mapping) or item.get("license_status") != "approved":
            continue
        reviewed = parse_aware_time(item.get("reviewed_at"), "sources.reviewed_at", error=DecisionToolError)
        if reviewed > cutoff:
            continue
        approved[str(item.get("source"))] = item
    return approved


def load_benchmark_holdings(holdings_path: Path, approvals_path: Path, cutoff_text: str) -> Dict[str, object]:
    """讀 ``data/benchmark_holdings.csv``：只有表頭回 ``status=unavailable``；有資料列則來源須核准、可得時間不晚於截止，否則拒絕。"""
    cutoff = parse_aware_time(cutoff_text, "decision_cutoff", error=DecisionToolError)
    try:
        raw = Path(holdings_path).read_bytes()
    except OSError as error:
        raise DecisionToolError("無法讀取 0050 成分權重檔：%s" % holdings_path) from error
    lines = raw.decode("utf-8").splitlines()
    rows = list(csv.DictReader(lines))
    if not lines or [name.strip() for name in next(csv.reader(lines[:1]))] != HOLDINGS_FIELDS:
        raise DecisionToolError("0050 成分權重檔欄位必須為：%s" % ",".join(HOLDINGS_FIELDS))
    digest = hashlib.sha256(raw).hexdigest()
    if not rows:
        return {"status": "unavailable", "reason": "0050 成分權重檔沒有資料列", "file_sha256": digest}
    approved = _approved_holdings_sources(approvals_path, cutoff)
    pack = _holdings_pack(rows, approved, cutoff)
    pack["file_sha256"] = digest
    return pack


def _holdings_pack(rows: Sequence[Mapping[str, str]], approved: Mapping[str, Mapping[str, object]], cutoff: datetime) -> Dict[str, object]:
    sources = {row["source"].strip() for row in rows}
    dates = {row["source_date"].strip() for row in rows}
    stamps = {row["available_at"].strip() for row in rows}
    if len(sources) != 1 or len(dates) != 1 or len(stamps) != 1:
        raise DecisionToolError("0050 成分權重檔的來源、資料日與可得時間必須一致")
    source, source_date, available_text = next(iter(sources)), next(iter(dates)), next(iter(stamps))
    if source not in approved:
        raise DecisionToolError("0050 成分權重來源 %s 未經核准" % source)
    pack: Dict[str, object] = {
        "status": "available", "etf_code": HOLDINGS_ETF_CODE, "source": source, "license_status": "approved",
        "source_date": source_date, "available_at": available_text,
        "rows": [
            {"rank": int(row["rank"]), "symbol": row["symbol"].strip().upper(), "stock_name": row["stock_name"].strip(),
             "weight": str(decimal_value(row["weight"], "0050.weight"))}
            for row in rows
        ],
    }
    errors: List[str] = []
    _check_holdings(pack, cutoff, errors)
    if errors:
        raise DecisionToolError("；".join(errors))
    return pack


def _check_holdings(pack: Mapping[str, object], cutoff: datetime, errors: List[str]) -> None:
    """已有資料列的持股包：時間、來源核准標記、排名、代號、權重。"""
    if pack.get("license_status") != "approved":
        errors.append("0050 成分權重來源未核准")
    try:
        available = parse_aware_time(pack.get("available_at"), "benchmark_input.holdings.available_at", error=DecisionToolError)
        if available > cutoff:
            errors.append("0050 成分權重晚於 decision_cutoff 才可得")
        datetime.fromisoformat("%sT00:00:00" % pack.get("source_date"))
    except (DecisionToolError, ValueError, TypeError) as error:
        errors.append("0050 成分權重時間無效：%s" % error)
    rows = pack.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_HOLDINGS_ROWS:
        errors.append("0050 成分權重必須是 1 到 %d 列" % MAX_HOLDINGS_ROWS)
        return
    seen = set()
    total = Decimal(0)
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping) or row.get("rank") != index:
            errors.append("0050 成分權重排名必須自 1 起連續")
            return
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
            errors.append("0050 成分權重第 %d 列代號無效" % index)
            continue
        if symbol in seen:
            errors.append("0050 成分權重代號重複：%s" % symbol)
        seen.add(symbol)
        try:
            weight = decimal_value(row.get("weight"), "0050.%s.weight" % symbol)
        except DecisionToolError as error:
            errors.append(str(error))
            continue
        if weight <= 0 or weight > 1:
            errors.append("0050 成分權重 %s 必須大於 0 且不大於 1" % symbol)
        total += weight
    if total > 1:
        errors.append("0050 成分權重合計不得超過 1")


def build_benchmark_input(
    database_path: Path,
    holdings_path: Path,
    holdings_approvals_path: Path,
    benchmarks_config_path: Path,
    snapshot: Mapping[str, object],
) -> Dict[str, object]:
    """決策輸入的 ``benchmark_input``：0050 官方收盤（抓取時間與收盤都不晚於截止）加成分權重；來源未核准即拒絕。"""
    from datetime import timedelta

    from etf_agent.benchmark.contracts import BenchmarkError
    from etf_agent.benchmark.repository import BenchmarkPriceRepository
    from etf_agent.data.benchmarks import BenchmarkConfigError, load_benchmarks

    snapshot_id, cutoff_text, latest = snapshot.get("snapshot_id"), snapshot.get("decision_cutoff"), snapshot.get("latest_trade_date")
    if not isinstance(snapshot_id, str) or not isinstance(cutoff_text, str) or not isinstance(latest, str):
        raise DecisionToolError("benchmark_input 需要 Snapshot 的 snapshot_id、decision_cutoff 與 latest_trade_date")
    cutoff = parse_aware_time(cutoff_text, "decision_cutoff", error=DecisionToolError)
    try:
        definitions = load_benchmarks(benchmarks_config_path)
    except BenchmarkConfigError as error:
        raise DecisionToolError("無法讀取基準設定：%s" % error) from error
    definition = next((item for item in definitions if item.benchmark_id == BENCHMARK_ID), None)
    if definition is None or not definition.approved or definition.instrument is None or definition.source != PRICE_SOURCE:
        raise DecisionToolError("基準 %s 未核准或來源不是 %s，不建立 benchmark_input" % (BENCHMARK_ID, PRICE_SOURCE))
    start = (datetime.fromisoformat("%sT00:00:00" % latest) - timedelta(days=PRICE_LOOKBACK_CALENDAR_DAYS)).date().isoformat()
    try:
        table = BenchmarkPriceRepository(database_path).official_prices([definition.instrument.symbol], start, latest, cutoff)
    except BenchmarkError as error:
        raise DecisionToolError("無法讀取 0050 行情：%s" % error) from error
    prices = []
    for trade_date in sorted(table):
        row = table[trade_date].get(definition.instrument.symbol)
        if row is not None and row.source == PRICE_SOURCE:
            prices.append({"trade_date": trade_date, "close": str(row.close_price), "source": row.source,
                           "fetched_at": row.fetched_at.isoformat()})
    pack: Dict[str, object] = {
        "schema_version": BENCHMARK_INPUT_SCHEMA, "snapshot_id": snapshot_id, "decision_cutoff": cutoff_text,
        "benchmark": {"benchmark_id": BENCHMARK_ID, "symbol": definition.instrument.symbol, "source": PRICE_SOURCE,
                      "license_status": definition.license_status, "reviewed_at": definition.reviewed_at},
        "prices": prices,
        "holdings": load_benchmark_holdings(holdings_path, holdings_approvals_path, cutoff_text),
    }
    pack["content_sha256"] = benchmark_input_sha256(pack)
    errors = validate_benchmark_input(pack, snapshot_id, cutoff_text)
    if errors:
        raise DecisionToolError("；".join(errors))
    return pack


def benchmark_input_sha256(pack: Mapping[str, object]) -> str:
    payload = dict(pack)
    payload.pop("content_sha256", None)
    return canonical_sha256(payload)


def validate_benchmark_input(pack: object, snapshot_id: object, cutoff_text: object, latest_trade_date: object = None) -> List[str]:
    """回傳錯誤清單：欄位白名單、內容雜湊、snapshot／cutoff 一致、來源核准、價格與持股的時間與數值。"""
    errors: List[str] = []
    if not isinstance(pack, Mapping):
        return ["benchmark_input 必須是物件"]
    reject_unknown_fields(
        pack, {"schema_version", "snapshot_id", "decision_cutoff", "benchmark", "prices", "holdings", "content_sha256"},
        "benchmark_input", errors)
    if pack.get("schema_version") != BENCHMARK_INPUT_SCHEMA:
        errors.append("benchmark_input.schema_version 必須是 %s" % BENCHMARK_INPUT_SCHEMA)
    if pack.get("content_sha256") != benchmark_input_sha256(pack):
        errors.append("benchmark_input.content_sha256 與內容不一致")
    if pack.get("snapshot_id") != snapshot_id:
        errors.append("benchmark_input.snapshot_id 與決策輸入不一致")
    if pack.get("decision_cutoff") != cutoff_text:
        errors.append("benchmark_input.decision_cutoff 與決策輸入不一致")
    try:
        cutoff = parse_aware_time(cutoff_text, "decision_cutoff", error=DecisionToolError)
    except DecisionToolError as error:
        return errors + [str(error)]
    meta = pack.get("benchmark")
    if (not isinstance(meta, Mapping) or meta.get("license_status") != "approved"
            or meta.get("source") != PRICE_SOURCE or meta.get("benchmark_id") != BENCHMARK_ID):
        errors.append("benchmark_input.benchmark 必須是已核准的 %s（來源 %s）" % (BENCHMARK_ID, PRICE_SOURCE))
    prices = pack.get("prices")
    if not isinstance(prices, list):
        errors.append("benchmark_input.prices 必須是陣列")
        prices = []
    previous = ""
    for index, row in enumerate(prices):
        label = "benchmark_input.prices[%d]" % index
        if not isinstance(row, Mapping) or not isinstance(row.get("trade_date"), str):
            errors.append("%s 格式無效" % label)
            continue
        day = str(row["trade_date"])
        if day <= previous:
            errors.append("%s 的交易日必須嚴格遞增" % label)
        previous = day
        if latest_trade_date is not None and day > str(latest_trade_date):
            errors.append("%s 的交易日 %s 晚於 snapshot.latest_trade_date" % (label, day))
        if row.get("source") != PRICE_SOURCE:
            errors.append("%s 的來源必須是 %s" % (label, PRICE_SOURCE))
        try:
            if decimal_value(row.get("close"), "%s.close" % label) <= 0:
                errors.append("%s.close 必須大於 0" % label)
            if parse_aware_time(row.get("fetched_at"), "%s.fetched_at" % label, error=DecisionToolError) > cutoff:
                errors.append("%s 抓取時間晚於 decision_cutoff" % label)
        except DecisionToolError as error:
            errors.append(str(error))
    holdings = pack.get("holdings")
    if not isinstance(holdings, Mapping):
        errors.append("benchmark_input.holdings 必須是物件")
    elif holdings.get("status") == "available":
        _check_holdings(holdings, cutoff, errors)
    elif holdings.get("status") != "unavailable":
        errors.append("benchmark_input.holdings.status 必須是 available 或 unavailable")
    return errors


# ---------------------------------------------------------------- 事實

def _unavailable(reason: str) -> Dict[str, object]:
    return {"status": "unavailable", "reason": reason}


def _pool_series(bundle: Mapping[str, object], trade_date: str) -> Dict[str, Dict[str, Decimal]]:
    series: Dict[str, Dict[str, Decimal]] = {}
    for row in bundle.get("price_series", []):  # type: ignore[union-attr]
        symbol = str(row["symbol"]).upper()
        bars: Dict[str, Decimal] = {}
        for bar in row["bars"]:
            day = str(bar["trade_date"])
            if day > trade_date:
                raise DecisionToolError("benchmark_facts：%s 的行情 %s 晚於 snapshot.latest_trade_date %s" % (symbol, day, trade_date))
            bars[day] = decimal_value(bar["close"], "%s.close" % symbol)
        series[symbol] = bars
    return series


def _holding_return(series: Mapping[str, Mapping[str, Decimal]], symbol: str, before: str, day: str) -> Optional[Decimal]:
    bars = series.get(symbol, {})
    start, end = bars.get(before), bars.get(day)
    if start is None or end is None or start <= 0:
        return None
    return end / start - Decimal(1)


def build_benchmark_facts(bundle: Mapping[str, object], policy: Mapping[str, object]) -> Dict[str, object]:
    """依 bundle 與 Policy 重算四個區塊；沒有 ``benchmark_input`` 或內容不合法即拒絕，資料不足的區塊標 unavailable。"""
    sizing = policy.get("position_sizing")
    gate = benchmark_facts_gate(sizing)
    if gate is None:
        raise DecisionToolError("DecisionPolicy 沒有 position_sizing.%s，不應建立基準事實" % BENCHMARK_FACTS_KEY)
    pack = bundle.get(BENCHMARK_INPUT_KEY)
    if not isinstance(pack, Mapping):
        raise DecisionToolError("position_sizing.%s 啟用時決策輸入必須有 %s" % (BENCHMARK_FACTS_KEY, BENCHMARK_INPUT_KEY))
    snapshot = bundle.get("snapshot")
    trade_date = snapshot.get("latest_trade_date") if isinstance(snapshot, Mapping) else None
    if not isinstance(trade_date, str):
        raise DecisionToolError("benchmark_facts 需要 snapshot.latest_trade_date")
    errors = validate_benchmark_input(pack, bundle.get("snapshot_id"), bundle.get("decision_cutoff"), trade_date)
    if errors:
        raise DecisionToolError("benchmark_facts：" + "；".join(errors))
    closes = {str(row["trade_date"]): Decimal(str(row["close"])) for row in pack["prices"]}  # type: ignore[index,union-attr]
    pool = _pool_series(bundle, trade_date)
    days = sorted({day for bars in pool.values() for day in bars})
    ret = _benchmark_return(closes, trade_date)
    weights: Optional[Dict[str, Decimal]] = None
    weights_error = ""
    if isinstance(bundle.get("account_snapshot"), Mapping):
        weights = holding_weights(bundle)[0]
    else:
        weights_error = "決策輸入沒有帳戶資料，無法取得持股起始權重"
    return {
        "version": BENCHMARK_FACTS, "citable": False, "source": PRICE_SOURCE, "as_of": trade_date, "note": BENCHMARK_NOTE,
        "benchmark_return": ret,
        "holdings_gap_1d": _holdings_gap(ret, closes, pool, weights, weights_error, trade_date),
        "active_weights": _active_weights(pack["holdings"], policy, weights, weights_error, days, trade_date,  # type: ignore[arg-type,index]
                                          int(gate["max_holdings_age_days"])),  # type: ignore[arg-type]
        "beta_to_benchmark": _beta(sizing, closes, pool, weights, weights_error, days, trade_date),
    }


def _benchmark_return(closes: Mapping[str, Decimal], trade_date: str) -> Dict[str, object]:
    days = sorted(closes)
    if not days:
        return _unavailable("沒有可用的 0050 官方收盤")
    if days[-1] != trade_date:
        return _unavailable("0050 最後一個交易日 %s 不是 snapshot.latest_trade_date %s" % (days[-1], trade_date))
    if len(days) < 2:
        return _unavailable("0050 歷史不足，沒有前一個交易日")
    last = closes[days[-1]]
    result: Dict[str, object] = {
        "status": "available", "date": days[-1], "previous_date": days[-2], "close": _q(last),
        "return_1d": _q(last / closes[days[-2]] - Decimal(1)),
        "return_5d": _q(last / closes[days[-6]] - Decimal(1)) if len(days) >= 6 else None,
    }
    if len(days) < 6:
        result["return_5d_reason"] = "0050 歷史不足 6 日，近 5 日報酬為 null"
    return result


def _holdings_gap(ret: Mapping[str, object], closes: Mapping[str, Decimal], pool: Mapping[str, Mapping[str, Decimal]],
                  weights: Optional[Mapping[str, Decimal]], weights_error: str, trade_date: str) -> Dict[str, object]:
    if ret.get("status") != "available":
        return _unavailable("0050 報酬不可用：%s" % ret.get("reason"))
    if weights is None:
        return _unavailable(weights_error)
    day, before = str(ret["date"]), str(ret["previous_date"])
    contribution = Decimal(0)
    missing: List[str] = []
    for symbol in sorted(weights):
        change = _holding_return(pool, symbol, before, day)
        if change is None:
            missing.append(symbol)
        else:
            contribution += weights[symbol] * change
    if missing:
        return _unavailable("持股 %s 缺 %s 或前一日收盤，不當成 0" % ("、".join(missing), day))
    bench = Decimal(str(ret["return_1d"]))
    return {"status": "available", "date": day, "previous_date": before, "weight_basis": "decision_start_weights",
            "holdings_contribution": _q(contribution), "benchmark_return_1d": _q(bench), "gap": _q(contribution - bench)}


def _active_weights(holdings: Mapping[str, object], policy: Mapping[str, object], weights: Optional[Mapping[str, Decimal]],
                    weights_error: str, days: Sequence[str], trade_date: str, max_age: int) -> Dict[str, object]:
    if holdings.get("status") != "available":
        return _unavailable("0050 成分權重不可用：%s" % holdings.get("reason", "沒有資料"))
    source_date = str(holdings["source_date"])
    age = sum(1 for day in days if source_date < day <= trade_date)
    meta = {"source_date": source_date, "available_at": holdings["available_at"], "age_trading_days": age,
            "max_holdings_age_days": max_age}
    if age > max_age:
        return {**_unavailable("成分權重檔資料日 %s 距今 %d 個交易日，超過上限 %d" % (source_date, age, max_age)), **meta}
    if weights is None:
        return {**_unavailable(weights_error), **meta}
    default_limit = decimal_value(policy["max_stock_weight"], "DecisionPolicy.max_stock_weight")
    special = policy.get("special_weight_limits") or {}
    rows: List[Dict[str, object]] = []
    bench_total = held_total = Decimal(0)
    for row in holdings["rows"]:  # type: ignore[union-attr]
        symbol = str(row["symbol"])
        bench = Decimal(str(row["weight"]))
        held = weights.get(symbol, Decimal(0))
        limit = decimal_value(special[symbol], "special_weight_limits.%s" % symbol) if symbol in special else default_limit
        bench_total += bench
        held_total += held
        rows.append({"symbol": symbol, "benchmark_weight": _q(bench), "portfolio_weight": _q(held),
                     "difference": _q(held - bench), "rule_weight_limit": _q(limit)})
    return {"status": "available", "weight_basis": "decision_start_weights", **meta, "top_n": len(rows),
            "benchmark_top_n_total": _q(bench_total), "portfolio_top_n_total": _q(held_total), "rows": rows}


def _beta(sizing: object, closes: Mapping[str, Decimal], pool: Mapping[str, Mapping[str, Decimal]],
          weights: Optional[Mapping[str, Decimal]], weights_error: str, days: Sequence[str], trade_date: str) -> Dict[str, object]:
    gate = exposure_rotation_gate(sizing)
    if gate is None:
        return _unavailable("沒有 exposure_rotation，無法取得 beta 視窗")
    window = int(gate["beta_window_days"])
    base: Dict[str, object] = {"beta_window_days": window}
    if weights is None:
        return {**_unavailable(weights_error), **base}
    if not weights:
        return {**_unavailable("沒有持股，組合 beta 不適用"), **base}
    if len(days) < window + 1:
        return {**_unavailable("行情歷史不足 %d 個交易日" % (window + 1)), **base}
    span = list(days[-(window + 1):])
    if span[-1] != trade_date:
        return {**_unavailable("行情最後一個交易日不是 snapshot.latest_trade_date"), **base}
    have = [day for day in span if day in closes]
    if len(have) < len(span):
        return {**_unavailable("0050 歷史只涵蓋視窗 %d 個交易日中的 %d 個，不補值" % (len(span), len(have))), **base}
    portfolio: List[Decimal] = []
    bench: List[Decimal] = []
    for before, day in zip(span, span[1:]):
        total = Decimal(0)
        for symbol, weight in weights.items():
            change = _holding_return(pool, symbol, before, day)
            if change is None:
                return {**_unavailable("持股 %s 在視窗內缺收盤，不補值" % symbol), **base}
            total += weight * change
        portfolio.append(total)
        bench.append(closes[day] / closes[before] - Decimal(1))
    mean_b = sum(bench, Decimal(0)) / Decimal(window)
    mean_p = sum(portfolio, Decimal(0)) / Decimal(window)
    variance = sum(((value - mean_b) ** 2 for value in bench), Decimal(0))
    if variance <= MIN_VARIANCE:
        return {**_unavailable("0050 在視窗內沒有變動，beta 無法計算"), **base}
    covariance = sum(((p - mean_p) * (b - mean_b) for p, b in zip(portfolio, bench)), Decimal(0))
    return {"status": "available", **base, "first_date": span[0], "last_date": span[-1], "beta": _q(covariance / variance),
            "weight_basis": "decision_start_weights"}


# ---------------------------------------------------------------- 風險 Agent

def benchmark_reference_tokens() -> List[str]:
    """風險 Agent 理由不得寫出的鍵名（背景不是證據）：區塊鍵名與輸入鍵名。"""
    return [BENCHMARK_FACTS_KEY, BENCHMARK_INPUT_KEY, "benchmark_return", "holdings_gap_1d", "active_weights", "beta_to_benchmark"]


def risk_benchmark_brief(facts: Mapping[str, object]) -> Dict[str, object]:
    """風險 Agent 簡報的區塊：整份事實（本來就沒有 evidence_id、``citable=false``）。"""
    return dict(facts)
