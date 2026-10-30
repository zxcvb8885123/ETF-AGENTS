"""由原始日線確定性重建隔夜市場背景；不呼叫模型、不補值。"""
import json
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from etf_agent.core import canonical_sha256, decimal_string, parse_aware_time, parse_decimal
from etf_agent.perception.market_fear_greed.tools import percentile
from .contracts import (
    AGREEMENT_MIN_TOTAL, AGREEMENT_RATIO, APPROVAL_FIELDS, CAPTURE_FIELDS, DIRECTION_DEADBAND, MAIN_SYMBOLS,
    MIXED_MIN_ABS, NEGATIVE_AT_OR_BELOW, NIGHT_NAME, NIGHT_SYMBOL, OUTLOOK_LABELS, PACK_FIELDS, PACK_VERSION,
    PACK_VERSION_V11, PERCENTILE_WINDOW, POSITIVE_AT_OR_ABOVE, PROXY_SYMBOLS, RESULT_VERSION, RESULT_VERSION_V11,
    SIGNAL_VERSION, SIGNAL_VERSION_V11, SOURCE_ID, SOURCES, SYMBOLS, SYMBOLS_V11, TAIFEX_QUOTE_URL, TAIFEX_SOURCE_ID,
    OvernightMarketError, check_fields, seal,
)
from .provider import chart_url

NEW_YORK = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")
SEOUL = ZoneInfo("Asia/Seoul")
TAIPEI = ZoneInfo("Asia/Taipei")
LIMITATIONS = [
    "美股指數與美股掛牌的台股代理（EWT、TSM）只是相關背景，不是台股漲跌的因果，也不是個股訊號。",
    "只代表台股上一個收盤到本次截止之間已完成的美股時段，不含台指期夜盤與盤前資訊。",
    "本背景描述隔夜市場，不是買賣訊號，也不預測今日台股指數方向。",
]
NIGHT_SESSION_GAP = "台指期夜盤尚未接入"
LIMITATIONS_V11 = [
    LIMITATIONS[0],
    "只代表台股上一個收盤到本次截止之間已完成的時段；台指期夜盤要到台北 05:00 結束後才有，平日 21:00 的決策沒有當夜夜盤。",
    LIMITATIONS[2],
]
HUNDRED = Decimal(100)


def session_end(day):
    """美股該日 16:00 America/New_York（夏令／冬令自動換算）轉 UTC；提早收盤日取較晚值，偏保守。"""
    return datetime.combine(date.fromisoformat(day), time(16, 0), tzinfo=NEW_YORK).astimezone(timezone.utc)


def window_start(latest_trade_date):
    """台股最近交易日 13:30（台北）收盤，之後完成的美股時段才算隔夜。"""
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", latest_trade_date):
            raise ValueError(latest_trade_date)
        day = date.fromisoformat(latest_trade_date)
    except (TypeError, ValueError) as exc:
        raise OvernightMarketError("latest_trade_date 必須是 YYYY-MM-DD") from exc
    return datetime.combine(day, time(13, 30), tzinfo=TAIPEI).astimezone(timezone.utc)


def parse_bars(raw, symbol):
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) != {"symbol", "bars"} or payload["symbol"] != symbol \
            or not isinstance(payload["bars"], list) or not payload["bars"]:
        raise OvernightMarketError("日線原始回應格式不符")
    bars = {}
    for bar in payload["bars"]:
        if not isinstance(bar, dict) or set(bar) != {"date", "close"}:
            raise OvernightMarketError("日線欄位不符")
        try:
            day = date.fromisoformat(bar["date"]).isoformat()
        except (TypeError, ValueError) as exc:
            raise OvernightMarketError("日線日期無效") from exc
        close = parse_decimal(bar["close"], "收盤價", error=OvernightMarketError)
        if close <= 0 or (day in bars and bars[day] != close):
            raise OvernightMarketError("日線收盤價無效或同日不一致")
        bars[day] = close
    return bars


def _check_url(symbol, url):
    if urlsplit(url).hostname not in SOURCES[SOURCE_ID]["hosts"]:
        raise OvernightMarketError("來源主機不符")
    if url != chart_url(symbol):
        raise OvernightMarketError("來源網址不符")


def _approvals(rows, cutoff, degrade_late=False):
    """``degrade_late``（1.1）：核准時間晚於截止時間的來源視同未核准，不中止整次決策；1.0 維持原本直接拒絕。"""
    if not isinstance(rows, list):
        raise OvernightMarketError("來源核准必須是陣列")
    approvals = {}
    for row in rows:
        check_fields(row, APPROVAL_FIELDS, "來源核准")
        source = row["source"]
        if source not in SOURCES or source in approvals or row["terms_url"] != SOURCES[source]["terms_url"]:
            raise OvernightMarketError("來源核准身分無效")
        if row["license_status"] not in {"approved", "unverified", "restricted"}:
            raise OvernightMarketError("來源使用狀態無效")
        reviewed_at = parse_aware_time(row["reviewed_at"], "reviewed_at", error=OvernightMarketError)
        # 只有「已核准」才需要核准時間不晚於截止；未核准的紀錄不影響結果，不因時間而中斷整次決策。
        late = row["license_status"] == "approved" and reviewed_at > cutoff
        if late and not degrade_late:
            raise OvernightMarketError("來源核准晚於截止時間")
        if row["license_status"] == "approved" and (
                row["usage_scope"] != "competition_research"
                or not all(isinstance(row[k], str) and row[k].strip() for k in ("evidence", "approved_by"))):
            raise OvernightMarketError("核准來源缺少比賽研究用途、核准人或依據")
        approvals[source] = row["license_status"] == "approved" and not late
    return approvals


def _pct(ratio):
    return (ratio * HUNDRED).quantize(Decimal("0.01"))


def _component(symbol, bars, fetched_at, start, cutoff, end=session_end):
    """回傳 (欄位, 缺口原因)；視窗內沒有完成時段或缺基準收盤即不可用。"""
    done = sorted(day for day in bars if end(day) <= fetched_at)
    sessions = [day for day in done if start < end(day) <= cutoff]
    if not sessions:
        return None, "%s 視窗內沒有已完成的美股交易時段" % symbol
    first = done.index(sessions[0])
    if first == 0:
        return None, "%s 缺少視窗前一個時段的收盤價作基準" % symbol
    returns = {done[i]: bars[done[i]] / bars[done[i - 1]] - 1 for i in range(1, len(done))}
    cumulative = Decimal(1)
    for day in sessions:
        cumulative *= 1 + returns[day]
    last = sessions[-1]
    history = [returns[day] for day in done if day in returns and day <= last][-PERCENTILE_WINDOW:]
    rank = percentile(history, returns[last]).quantize(Decimal("0.1")) if len(history) == PERCENTILE_WINDOW else None
    return {"as_of": last, "sessions": sessions, "session_end": end(last).isoformat(),
            "change_pct": decimal_string(_pct(returns[last])),
            "cumulative_change_pct": decimal_string(_pct(cumulative - 1)),
            "percentile_20d": decimal_string(rank) if rank is not None else None}, None


def _outlook(main, proxies):
    """overnight-v1：主訊號分三段；與台股代理方向相反且兩者幅度都 > 0.75% 時為 mixed。"""
    signal = (sum(main) / len(main)).quantize(Decimal("0.01"))
    outlook = "negative" if signal <= Decimal(str(NEGATIVE_AT_OR_BELOW)) else (
        "positive" if signal >= Decimal(str(POSITIVE_AT_OR_ABOVE)) else "neutral")
    proxy = (sum(proxies) / len(proxies)).quantize(Decimal("0.01")) if proxies else None
    floor = Decimal(str(MIXED_MIN_ABS))
    if proxy is not None and abs(signal) > floor and abs(proxy) > floor and (signal > 0) != (proxy > 0):
        outlook = "mixed"
    return signal, proxy, outlook


def _evaluate_v10(pack):
    check_fields(pack, PACK_FIELDS, "隔夜市場資料包")
    if pack["schema_version"] != PACK_VERSION or not isinstance(pack["snapshot_id"], str) or not pack["snapshot_id"]:
        raise OvernightMarketError("隔夜市場資料包身分無效")
    if seal(pack) != pack:
        raise OvernightMarketError("隔夜市場資料包雜湊不符")
    cutoff = parse_aware_time(pack["decision_cutoff"], "decision_cutoff", error=OvernightMarketError)
    start = window_start(pack["latest_trade_date"])
    if start > cutoff:
        raise OvernightMarketError("台股最近交易日收盤晚於截止時間")
    approved = _approvals(pack["source_approvals"], cutoff).get(SOURCE_ID, False)
    if not isinstance(pack["captures"], list):
        raise OvernightMarketError("原始回應必須是陣列")
    computed, gaps, seen = {}, [NIGHT_SESSION_GAP], set()
    for capture in pack["captures"]:
        check_fields(capture, CAPTURE_FIELDS, "原始回應")
        symbol = capture["symbol"]
        if capture["source"] != SOURCE_ID or symbol not in SYMBOLS:
            raise OvernightMarketError("不支援的來源或標的")
        _check_url(symbol, capture["url"])
        if symbol in seen:
            raise OvernightMarketError("原始回應重複")
        seen.add(symbol)
        fetched_at = parse_aware_time(capture["available_at"], "available_at", error=OvernightMarketError)
        if fetched_at > cutoff:
            raise OvernightMarketError("原始回應取得時間晚於截止時間")
        if not isinstance(capture["raw_text"], str) or capture["raw_sha256"] != canonical_sha256(capture["raw_text"]):
            raise OvernightMarketError("原始回應遭修改")
        if capture["status"] == "failed":
            gaps.append("%s 擷取失敗：%s" % (symbol, capture["error"] or "未知原因"))
            continue
        if capture["status"] != "fetched":
            raise OvernightMarketError("原始回應狀態無效")
        try:
            bars = parse_bars(capture["raw_text"], symbol)
        except (KeyError, TypeError, ValueError) as exc:
            gaps.append("%s 格式無法解析：%s" % (symbol, exc))
            continue
        row, reason = _component(symbol, bars, fetched_at, start, cutoff)
        if row is None:
            gaps.append(reason)
        else:
            row["raw_sha256"], row["available_at"] = capture["raw_sha256"], fetched_at.isoformat()
            computed[symbol] = row
    components = []
    for symbol, spec in SYMBOLS.items():
        row = {"source": SOURCE_ID, "symbol": symbol, "name": spec["name"], "role": spec["role"], "approved": approved}
        if symbol in computed:
            data = computed[symbol]
            evidence = None
            if approved:
                evidence = "overnight:%s:%s:%s" % (SOURCE_ID, symbol, canonical_sha256(
                    {"raw": data["raw_sha256"], "sessions": data["sessions"]})[:20])
            row.update(status="available", evidence_id=evidence, **data)
        else:
            row.update(status="unavailable", evidence_id=None, as_of=None, sessions=[], session_end=None,
                       change_pct=None, cumulative_change_pct=None, percentile_20d=None, raw_sha256=None,
                       available_at=None)
            if symbol not in seen:
                gaps.append("%s 沒有原始回應" % symbol)
        components.append(row)
    if not approved:
        gaps.append("%s 尚未核准使用，只作診斷，不進正式結果。" % SOURCES[SOURCE_ID]["name"])
    by_symbol = {row["symbol"]: row for row in components}
    main_missing = [s for s in MAIN_SYMBOLS if by_symbol[s]["status"] != "available"]
    if main_missing:
        gaps.append("主訊號標的不可用：%s；隔夜市場背景整體不可用。" % "、".join(main_missing))
    status = "available" if approved and not main_missing else "unavailable"
    signal = proxy = None
    outlook, findings = "unknown", []
    if status == "available":
        proxies = [Decimal(by_symbol[s]["cumulative_change_pct"]) for s in PROXY_SYMBOLS if by_symbol[s]["status"] == "available"]
        signal, proxy, outlook = _outlook([Decimal(by_symbol[s]["cumulative_change_pct"]) for s in MAIN_SYMBOLS], proxies)
        shown = [row for row in components if row["status"] == "available"]
        parts = ["%s 累計 %s%%（%s，%d 個時段）" % (row["symbol"], row["cumulative_change_pct"], row["as_of"], len(row["sessions"]))
                 for row in shown]
        findings.append({
            "text": "隔夜市場背景（%s）：主訊號（^SOX 與 ^GSPC 累計漲跌幅平均）%s%%，判讀「%s」；%s。僅為隔夜背景，不含台指期夜盤。" % (
                SIGNAL_VERSION, decimal_string(signal), OUTLOOK_LABELS[outlook], "、".join(parts)),
            "evidence_ids": [row["evidence_id"] for row in shown]})
    return seal({
        "schema_version": RESULT_VERSION, "signal_version": SIGNAL_VERSION,
        "snapshot_id": pack["snapshot_id"], "decision_cutoff": pack["decision_cutoff"],
        "input_sha256": pack["content_sha256"], "status": status, "outlook": outlook,
        "signal_pct": decimal_string(signal) if signal is not None else None,
        "proxy_signal_pct": decimal_string(proxy) if proxy is not None else None,
        "window": {"start": start.isoformat(), "end": cutoff.isoformat(), "latest_trade_date": pack["latest_trade_date"]},
        "findings": findings, "components": components, "night_session": None,
        "data_gaps": gaps, "limitations": list(LIMITATIONS),
    })


def evaluate(pack):
    """依資料包版本重建結果：1.0 維持原行為（舊封存逐位元相同），1.1 加入日韓、VIX 與台指期夜盤。"""
    check_fields(pack, PACK_FIELDS, "隔夜市場資料包")
    if pack["schema_version"] == PACK_VERSION_V11:
        return _evaluate_v11(pack)
    return _evaluate_v10(pack)


# ---------------------------------------------------------------- overnight-market-1.1
def _yahoo_end(symbol):
    """各指數的日線完成時間：日韓 15:30 當地、VIX 16:15 ET、美股與 ETF 16:00 ET。"""
    zone, at = {"^N225": (TOKYO, time(15, 30)), "^KS11": (SEOUL, time(15, 30)), "^VIX": (NEW_YORK, time(16, 15))}.get(
        symbol, (NEW_YORK, time(16, 0)))
    return lambda day: datetime.combine(date.fromisoformat(day), at, tzinfo=zone).astimezone(timezone.utc)


NIGHT_ROW_FIELDS = {"SymbolID", "CDate", "CTime", "CLastPrice", "CRefPrice", "CTotalVolume"}
_NIGHT_CONTRACT = re.compile(r"TXF[A-L]\d-M")
NIGHT_END = time(5, 0)
NIGHT_LAST_QUOTE = time(5, 5)


def parse_night(raw):
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) != {"rows"} or not isinstance(payload["rows"], list):
        raise OvernightMarketError("夜盤原始回應格式不符")
    for row in payload["rows"]:
        if not isinstance(row, dict) or set(row) != NIGHT_ROW_FIELDS:
            raise OvernightMarketError("夜盤報價欄位不符")
    return payload["rows"]


def _night_component(raw, fetched_at, start, cutoff):
    """台指期近月（月合約中成交量最大者）盤後最後價相對前一日日盤結算價；夜盤尚未結束或報價不在夜盤時段就不可用。

    夜盤台北 05:00 結束、08:45 日盤才開：抓取時間在夜盤結束之後，且報價時間不晚於 05:05（仍是夜盤的最後報價）才算完成。"""
    candidates = []
    for row in parse_night(raw):
        if not _NIGHT_CONTRACT.fullmatch(row["SymbolID"]):
            continue
        try:
            last = parse_decimal(row["CLastPrice"], "最後價", error=OvernightMarketError)
            ref = parse_decimal(row["CRefPrice"], "前一日結算價", error=OvernightMarketError)
            volume = int(row["CTotalVolume"])
        except (OvernightMarketError, ValueError):
            continue
        if last > 0 and ref > 0 and volume > 0:
            candidates.append((-volume, row["SymbolID"], row, last, ref))
    if not candidates:
        return None, None, "台指期夜盤沒有有效報價（尚未開盤、無成交或格式不符）"
    _, _, row, last, ref = sorted(candidates, key=lambda item: item[:2])[0]
    try:
        quote_day = datetime.strptime(row["CDate"], "%Y%m%d").date()
        quote_time = datetime.strptime(row["CTime"], "%H%M%S").time()
    except ValueError:
        return None, None, "台指期夜盤報價時間無法解析"
    quote_at = datetime.combine(quote_day, quote_time, tzinfo=TAIPEI)
    ended = datetime.combine(quote_day, NIGHT_END, tzinfo=TAIPEI)
    if quote_time > NIGHT_LAST_QUOTE or quote_at > fetched_at or ended > fetched_at:
        return None, None, "台指期夜盤尚未結束或最新報價不在夜盤時段（夜盤台北 05:00 結束）"
    if not start < ended.astimezone(timezone.utc) <= cutoff:
        return None, None, "台指期夜盤結束時間不在本次視窗內"
    change = ((last / ref - 1) * HUNDRED).quantize(Decimal("0.01"))
    detail = {"contract": row["SymbolID"], "last": decimal_string(last), "reference_settlement": decimal_string(ref),
              "change_pct": decimal_string(change), "quote_time": quote_at.astimezone(timezone.utc).isoformat(),
              "volume": int(row["CTotalVolume"])}
    component = {"as_of": quote_day.isoformat(), "sessions": [quote_day.isoformat()],
                 "session_end": ended.astimezone(timezone.utc).isoformat(), "change_pct": decimal_string(change),
                 "cumulative_change_pct": decimal_string(change), "percentile_20d": None}
    return component, detail, None


def _direction(value):
    band = Decimal(str(DIRECTION_DEADBAND))
    return "up" if value >= band else ("down" if value <= -band else "flat")


def _agreement(rows):
    """各方向訊號（美股指數、台股代理、日韓指數、夜盤；VIX 不算方向）的同向程度。"""
    counts = {"up": 0, "down": 0, "flat": 0}
    for row in rows:
        counts[_direction(Decimal(row["cumulative_change_pct"]))] += 1
    total = sum(counts.values())
    ratio = Decimal(str(AGREEMENT_RATIO))
    if total < AGREEMENT_MIN_TOTAL:
        label = "insufficient"
    elif counts["down"] == 0 and Decimal(counts["up"]) / total >= ratio:
        label = "aligned_up"
    elif counts["up"] == 0 and Decimal(counts["down"]) / total >= ratio:
        label = "aligned_down"
    else:
        label = "mixed"
    return dict(counts, total=total, label=label)


def _check_url_v11(source, symbol, url):
    if source == SOURCE_ID:
        if symbol not in SYMBOLS_V11:
            raise OvernightMarketError("不支援的來源或標的")
        _check_url(symbol, url)
    elif source == TAIFEX_SOURCE_ID:
        if symbol != NIGHT_SYMBOL or url != TAIFEX_QUOTE_URL or urlsplit(url).hostname not in SOURCES[source]["hosts"]:
            raise OvernightMarketError("夜盤來源網址或標的不符")
    else:
        raise OvernightMarketError("不支援的來源或標的")


def _evaluate_v11(pack):
    if not isinstance(pack["snapshot_id"], str) or not pack["snapshot_id"]:
        raise OvernightMarketError("隔夜市場資料包身分無效")
    if seal(pack) != pack:
        raise OvernightMarketError("隔夜市場資料包雜湊不符")
    cutoff = parse_aware_time(pack["decision_cutoff"], "decision_cutoff", error=OvernightMarketError)
    start = window_start(pack["latest_trade_date"])
    if start > cutoff:
        raise OvernightMarketError("台股最近交易日收盤晚於截止時間")
    approvals = _approvals(pack["source_approvals"], cutoff, degrade_late=True)
    approved, night_approved = approvals.get(SOURCE_ID, False), approvals.get(TAIFEX_SOURCE_ID, False)
    if not isinstance(pack["captures"], list):
        raise OvernightMarketError("原始回應必須是陣列")
    computed, gaps, seen, night, night_detail = {}, [], set(), None, None
    gaps.extend("%s 的核准時間晚於截止時間，視同未核准。" % row["source"] for row in pack["source_approvals"]
                if row.get("license_status") == "approved"
                and parse_aware_time(row["reviewed_at"], "reviewed_at", error=OvernightMarketError) > cutoff)
    for capture in pack["captures"]:
        check_fields(capture, CAPTURE_FIELDS, "原始回應")
        source, symbol = capture["source"], capture["symbol"]
        _check_url_v11(source, symbol, capture["url"])
        if symbol in seen:
            raise OvernightMarketError("原始回應重複")
        seen.add(symbol)
        fetched_at = parse_aware_time(capture["available_at"], "available_at", error=OvernightMarketError)
        if fetched_at > cutoff:
            raise OvernightMarketError("原始回應取得時間晚於截止時間")
        if not isinstance(capture["raw_text"], str) or capture["raw_sha256"] != canonical_sha256(capture["raw_text"]):
            raise OvernightMarketError("原始回應遭修改")
        if capture["status"] == "failed":
            gaps.append("%s 擷取失敗：%s" % (symbol, capture["error"] or "未知原因"))
            continue
        if capture["status"] != "fetched":
            raise OvernightMarketError("原始回應狀態無效")
        try:
            if source == TAIFEX_SOURCE_ID:
                if not night_approved:
                    gaps.append("%s 尚未核准使用，夜盤不進正式結果。" % SOURCES[TAIFEX_SOURCE_ID]["name"])
                    continue
                row, night_detail, reason = _night_component(capture["raw_text"], fetched_at, start, cutoff)
            else:
                row, reason = _component(symbol, parse_bars(capture["raw_text"], symbol), fetched_at, start, cutoff,
                                         _yahoo_end(symbol))
        except (KeyError, TypeError, ValueError) as exc:
            gaps.append("%s 格式無法解析：%s" % (symbol, exc))
            continue
        if row is None:
            gaps.append(reason)
        else:
            row["raw_sha256"], row["available_at"] = capture["raw_sha256"], fetched_at.isoformat()
            computed[symbol] = row
    specs = [(symbol, spec["name"], spec["role"], SOURCE_ID, approved) for symbol, spec in SYMBOLS_V11.items()]
    specs.append((NIGHT_SYMBOL, NIGHT_NAME, "taifex_night", TAIFEX_SOURCE_ID, night_approved))
    components = []
    for symbol, name, role, source, is_approved in specs:
        row = {"source": source, "symbol": symbol, "name": name, "role": role, "approved": is_approved}
        if symbol in computed:
            data = computed[symbol]
            evidence = None
            if is_approved:
                evidence = "overnight:%s:%s:%s" % (source, symbol, canonical_sha256(
                    {"raw": data["raw_sha256"], "sessions": data["sessions"]})[:20])
            row.update(status="available", evidence_id=evidence, **data)
        else:
            row.update(status="unavailable", evidence_id=None, as_of=None, sessions=[], session_end=None,
                       change_pct=None, cumulative_change_pct=None, percentile_20d=None, raw_sha256=None,
                       available_at=None)
            if symbol not in seen:
                gaps.append("%s 沒有原始回應" % symbol)
        components.append(row)
    if not approved:
        gaps.append("%s 尚未核准使用，只作診斷，不進正式結果。" % SOURCES[SOURCE_ID]["name"])
    by_symbol = {row["symbol"]: row for row in components}
    main_missing = [s for s in MAIN_SYMBOLS if by_symbol[s]["status"] != "available"]
    if main_missing:
        gaps.append("主訊號標的不可用：%s；隔夜市場背景整體不可用。" % "、".join(main_missing))
    status = "available" if approved and not main_missing else "unavailable"
    signal = proxy = None
    outlook, findings, agreement = "unknown", [], None
    if status == "available":
        proxies = [Decimal(by_symbol[s]["cumulative_change_pct"]) for s in PROXY_SYMBOLS if by_symbol[s]["status"] == "available"]
        signal, proxy, outlook = _outlook([Decimal(by_symbol[s]["cumulative_change_pct"]) for s in MAIN_SYMBOLS], proxies)
        shown = [row for row in components if row["status"] == "available"]
        directional = [row for row in shown if row["role"] != "volatility"]
        agreement = _agreement(directional)
        parts = ["%s 累計 %s%%（%s，%d 個時段）" % (row["symbol"], row["cumulative_change_pct"], row["as_of"], len(row["sessions"]))
                 for row in shown]
        findings.append({
            "text": "隔夜市場背景（%s）：主訊號（^SOX 與 ^GSPC 累計漲跌幅平均）%s%%，判讀「%s」；方向一致性 %s（上 %d、下 %d、持平 %d）；%s。僅為隔夜背景。" % (
                SIGNAL_VERSION_V11, decimal_string(signal), OUTLOOK_LABELS[outlook], agreement["label"],
                agreement["up"], agreement["down"], agreement["flat"], "、".join(parts)),
            "evidence_ids": [row["evidence_id"] for row in shown]})
    return seal({
        "schema_version": RESULT_VERSION_V11, "signal_version": SIGNAL_VERSION_V11,
        "snapshot_id": pack["snapshot_id"], "decision_cutoff": pack["decision_cutoff"],
        "input_sha256": pack["content_sha256"], "status": status, "outlook": outlook,
        "signal_pct": decimal_string(signal) if signal is not None else None,
        "proxy_signal_pct": decimal_string(proxy) if proxy is not None else None,
        "agreement": agreement,
        "window": {"start": start.isoformat(), "end": cutoff.isoformat(), "latest_trade_date": pack["latest_trade_date"]},
        "findings": findings, "components": components,
        "night_session": night_detail if status == "available" and NIGHT_SYMBOL in computed else None,
        "data_gaps": gaps, "limitations": list(LIMITATIONS_V11),
    })


def historical_signals(bars_by_symbol, night_change_pct, latest_trade_date, decision_at):
    """離線回測用：以與正式流程相同的視窗與公式，算出 ``decision_at``（台北 07:00 類的時點）當下的隔夜訊號。

    ``bars_by_symbol`` 是 {代號: {日期: 收盤 Decimal}}；``night_change_pct`` 為當夜台指期漲跌幅（Decimal）或 None。
    只回傳數字，不產生 evidence、不看核准；缺主訊號時 outlook 為 None。"""
    start = window_start(latest_trade_date)
    components = {}
    for symbol, bars in bars_by_symbol.items():
        if symbol not in SYMBOLS_V11:
            continue
        row, _ = _component(symbol, bars, decision_at, start, decision_at, _yahoo_end(symbol))
        if row is not None:
            components[symbol] = row
    if night_change_pct is not None:
        components[NIGHT_SYMBOL] = {"cumulative_change_pct": decimal_string(night_change_pct)}
    result = {"components": {s: Decimal(r["cumulative_change_pct"]) for s, r in components.items()},
              "signal": None, "proxy": None, "outlook": None, "agreement": None}
    if all(s in components for s in MAIN_SYMBOLS):
        proxies = [Decimal(components[s]["cumulative_change_pct"]) for s in PROXY_SYMBOLS if s in components]
        result["signal"], result["proxy"], result["outlook"] = _outlook(
            [Decimal(components[s]["cumulative_change_pct"]) for s in MAIN_SYMBOLS], proxies)
    directional = [{"cumulative_change_pct": r["cumulative_change_pct"]} for s, r in components.items()
                   if s != "^VIX"]
    result["agreement"] = _agreement(directional) if directional else None
    return result
