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
    APPROVAL_FIELDS, CAPTURE_FIELDS, MAIN_SYMBOLS, MIXED_MIN_ABS, NEGATIVE_AT_OR_BELOW, OUTLOOK_LABELS,
    PACK_FIELDS, PACK_VERSION, PERCENTILE_WINDOW, POSITIVE_AT_OR_ABOVE, PROXY_SYMBOLS, RESULT_VERSION, SIGNAL_VERSION,
    SOURCE_ID, SOURCES, SYMBOLS, OvernightMarketError, check_fields, seal,
)
from .provider import chart_url

NEW_YORK = ZoneInfo("America/New_York")
TAIPEI = ZoneInfo("Asia/Taipei")
LIMITATIONS = [
    "美股指數與美股掛牌的台股代理（EWT、TSM）只是相關背景，不是台股漲跌的因果，也不是個股訊號。",
    "只代表台股上一個收盤到本次截止之間已完成的美股時段，不含台指期夜盤與盤前資訊。",
    "本背景描述隔夜市場，不是買賣訊號，也不預測今日台股指數方向。",
]
NIGHT_SESSION_GAP = "台指期夜盤尚未接入"
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


def _approvals(rows, cutoff):
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
        if row["license_status"] == "approved" and reviewed_at > cutoff:
            raise OvernightMarketError("來源核准晚於截止時間")
        if row["license_status"] == "approved" and (
                row["usage_scope"] != "competition_research"
                or not all(isinstance(row[k], str) and row[k].strip() for k in ("evidence", "approved_by"))):
            raise OvernightMarketError("核准來源缺少比賽研究用途、核准人或依據")
        approvals[source] = row["license_status"] == "approved"
    return approvals


def _pct(ratio):
    return (ratio * HUNDRED).quantize(Decimal("0.01"))


def _component(symbol, bars, fetched_at, start, cutoff):
    """回傳 (欄位, 缺口原因)；視窗內沒有完成時段或缺基準收盤即不可用。"""
    done = sorted(day for day in bars if session_end(day) <= fetched_at)
    sessions = [day for day in done if start < session_end(day) <= cutoff]
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
    return {"as_of": last, "sessions": sessions, "session_end": session_end(last).isoformat(),
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


def evaluate(pack):
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
