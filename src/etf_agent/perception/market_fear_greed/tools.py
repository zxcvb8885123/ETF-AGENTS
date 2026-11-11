"""由原始回應確定性重建恐懼貪婪分數；不呼叫模型、不補值。"""
import csv
import io
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from etf_agent.core import canonical_sha256, decimal_string, parse_aware_time
from .contracts import (
    APPROVAL_FIELDS, BAND_LABELS, BANDS, CAPTURE_FIELDS, LEVEL_INPUTS, LEVEL_NAME, LEVEL_SOURCE, LEVEL_WINDOW,
    MARGIN_CHANGE_DAYS, MAX_STALE_DAYS, MIN_COMPONENTS, PACK_FIELDS, PACK_VERSION_V11, PACK_VERSIONS, RESULT_VERSION,
    RESULT_VERSION_V11, RESULT_VERSION_V12, PACK_VERSION_V12, SCORE_VERSION, SCORE_VERSION_V2, SCORE_VERSION_V3, SOURCES, WINDOW, FearGreedError, check_fields, seal, sources_for,
)

PCR_HEADER = ["日期", "賣權成交量", "買權成交量", "買賣權成交量比率%", "賣權未平倉量", "買權未平倉量", "買賣權未平倉量比率%"]
LIMITATIONS_V2_EXTRA = [
    "融資水位用「融資金額 ÷ 加權指數」近 %d 個交易日百分位；加權指數只是市值的代理，不含新上市與增資，一年窗口內偏差小。" % LEVEL_WINDOW,
]
LIMITATIONS = [
    "本指標為專案自建，不是 CNN Fear & Greed Index；只反映選擇權與融資部位的相對變化。",
    "每項以最近 %d 個交易日的百分位計分，只代表近期相對位置，不代表長期高低。" % WINDOW,
    "分數描述市場情緒，不是買賣訊號，也不預測指數方向。",
]


def _decimal(text, name):
    try:
        value = Decimal(str(text).replace(",", "").strip())
    except InvalidOperation as exc:
        raise FearGreedError(name + " 不是數字") from exc
    if not value.is_finite():
        raise FearGreedError(name + " 不是有限數字")
    return value


def _iso(compact):
    if not re.fullmatch(r"\d{8}", compact):
        raise FearGreedError("日期格式錯誤：" + compact)
    return date(int(compact[:4]), int(compact[4:6]), int(compact[6:])).isoformat()


def parse_vix(raw):
    """期交所 VIX 月檔：日期、時間、收盤指數、收盤前一分鐘均值；以 tab 分隔。"""
    rows = {}
    for line in raw.splitlines():
        parts = line.split()
        if not parts or not re.fullmatch(r"\d{8}", parts[0]):
            continue
        if len(parts) != 4:
            raise FearGreedError("VIX 欄位數變更，需核對")
        rows[_iso(parts[0])] = _decimal(parts[2], "VIX")
    return rows


def parse_pcr(raw):
    reader = list(csv.reader(io.StringIO(raw.lstrip("﻿"))))
    if not reader or [cell.strip().lstrip("﻿") for cell in reader[0][:7]] != PCR_HEADER:
        raise FearGreedError("Put/Call 欄位變更，需核對")
    rows = {}
    for row in reader[1:]:
        if not row or not row[0].strip():
            continue
        rows[_iso(row[0].strip().replace("/", ""))] = _decimal(row[3], "買賣權成交量比率")
    return rows


def parse_margin(raw):
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("status") != 200 or not isinstance(payload.get("data"), list):
        raise FearGreedError("FinMind 融資回應狀態異常")
    rows = {}
    for row in payload["data"]:
        if row.get("name") == "MarginPurchaseMoney":
            rows[date.fromisoformat(row["date"]).isoformat()] = _decimal(row["TodayBalance"], "融資金額")
    return rows


def parse_twse_index(raw):
    """證交所市場成交資訊月檔：日期為民國年（115/11/03），欄位固定，收盤為第 5 欄。"""
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("stat") != "OK" or not isinstance(payload.get("data"), list):
        raise FearGreedError("證交所加權指數回應狀態異常")
    if payload.get("fields", [])[:5] != ["日期", "成交股數", "成交金額", "成交筆數", "發行量加權股價指數"]:
        raise FearGreedError("證交所加權指數欄位變更，需核對")
    rows = {}
    for row in payload["data"]:
        year, month, day = (int(part) for part in row[0].split("/"))
        rows[date(year + 1911, month, day).isoformat()] = _decimal(row[4], "加權指數")
    return rows


PARSERS = {"TAIFEX_VIX": parse_vix, "TAIFEX_PCR": parse_pcr, "FINMIND_MARGIN": parse_margin,
           "TWSE_TAIEX": parse_twse_index}


def _check_url(source, url, cutoff_day, sources=SOURCES):
    spec = sources[source]
    if urlsplit(url).hostname not in spec["hosts"]:
        raise FearGreedError("來源主機不符")
    if "url" in spec:
        if url != spec["url"]:
            raise FearGreedError("來源網址不符")
        return
    match = re.fullmatch(spec["url_pattern"], url)
    if not match:
        raise FearGreedError("來源網址不符")
    if source == "TAIFEX_VIX" and match.group(1) > cutoff_day[:7].replace("-", ""):
        raise FearGreedError("VIX 月檔晚於截止月份")
    if source == "TWSE_TAIEX" and (not match.group(1).endswith("01") or match.group(1)[:6] > cutoff_day[:7].replace("-", "")):
        raise FearGreedError("加權指數月檔晚於截止月份")
    if source == "FINMIND_MARGIN" and (match.group(1) > match.group(2) or match.group(2) > cutoff_day):
        raise FearGreedError("融資查詢區間晚於截止日")


def percentile(values, latest):
    """latest 在 values（含 latest）中的百分位，同值各算一半。"""
    less = sum(1 for v in values if v < latest)
    equal = sum(1 for v in values if v == latest)
    return (Decimal(less) + Decimal(equal) / 2) / Decimal(len(values)) * 100


def _score(source, series, cutoff_day):
    days = sorted(day for day in series if day <= cutoff_day)
    if source == "FINMIND_MARGIN":
        need = WINDOW + MARGIN_CHANGE_DAYS
        if len(days) < need:
            return None, "融資資料只有 %d 個交易日，需要 %d 個" % (len(days), need)
        days = days[-need:]
        if any(series[day] <= 0 for day in days):
            raise FearGreedError("融資金額必須為正數")
        values = [series[days[i]] / series[days[i - MARGIN_CHANGE_DAYS]] - 1 for i in range(MARGIN_CHANGE_DAYS, need)]
        latest_value = values[-1] * 100
    else:
        if len(days) < WINDOW:
            return None, "%s 只有 %d 個交易日，需要 %d 個" % (SOURCES[source]["name"], len(days), WINDOW)
        days = days[-WINDOW:]
        values = [series[day] for day in days]
        latest_value = values[-1]
    if (date.fromisoformat(cutoff_day) - date.fromisoformat(days[-1])).days > MAX_STALE_DAYS:
        return None, "%s 最新資料 %s 距截止日超過 %d 天" % (SOURCES[source]["name"], days[-1], MAX_STALE_DAYS)
    rank = percentile(values, values[-1])
    score = rank if SOURCES[source]["direction"] == "greed" else 100 - rank
    return {"as_of": days[-1], "latest_value": decimal_string(latest_value.quantize(Decimal("0.01"))),
            "score": decimal_string(score.quantize(Decimal("0.1"))), "sample_size": len(values)}, None


def _level_score(margin, index, cutoff_day):
    """融資金額 ÷ 加權指數（百萬元／點）在最近 LEVEL_WINDOW 個共同交易日的百分位。"""
    days = sorted(day for day in margin if day in index and day <= cutoff_day)
    if len(days) < LEVEL_WINDOW:
        return None, "%s 只有 %d 個共同交易日，需要 %d 個" % (LEVEL_NAME, len(days), LEVEL_WINDOW)
    days = days[-LEVEL_WINDOW:]
    if any(index[day] <= 0 or margin[day] <= 0 for day in days):
        raise FearGreedError("融資金額與加權指數必須為正數")
    values = [margin[day] / index[day] / 1_000_000 for day in days]
    if (date.fromisoformat(cutoff_day) - date.fromisoformat(days[-1])).days > MAX_STALE_DAYS:
        return None, "%s 最新資料 %s 距截止日超過 %d 天" % (LEVEL_NAME, days[-1], MAX_STALE_DAYS)
    score = percentile(values, values[-1])
    return {"as_of": days[-1], "latest_value": decimal_string(values[-1].quantize(Decimal("0.01"))),
            "score": decimal_string(score.quantize(Decimal("0.1"))), "sample_size": len(values)}, None


def _band(score):
    for upper, band, outlook in BANDS:
        if score <= upper:
            return band, outlook
    raise FearGreedError("分數超出 0～100")


def _approvals(rows, cutoff, sources=SOURCES):
    if not isinstance(rows, list):
        raise FearGreedError("來源核准必須是陣列")
    approvals = {}
    for row in rows:
        check_fields(row, APPROVAL_FIELDS, "來源核准")
        source = row["source"]
        if source not in sources or source in approvals or row["terms_url"] != sources[source]["terms_url"]:
            raise FearGreedError("來源核准身分無效")
        if row["license_status"] not in {"approved", "unverified", "restricted"}:
            raise FearGreedError("來源使用狀態無效")
        if parse_aware_time(row["reviewed_at"], "reviewed_at", error=FearGreedError) > cutoff:
            raise FearGreedError("來源核准晚於截止時間")
        if row["license_status"] == "approved" and (
                row["usage_scope"] != "competition_research"
                or not all(isinstance(row[k], str) and row[k].strip() for k in ("evidence", "approved_by"))):
            raise FearGreedError("核准來源缺少比賽研究用途、核准人或依據")
        approvals[source] = row["license_status"] == "approved"
    return approvals


def _load_series(pack, cutoff, cutoff_day, sources):
    """逐筆驗證原始回應並解析成（來源 → 日期 → 數值）；``evaluate`` 與 ``score_as_of`` 共用，行為與原內嵌迴圈相同。"""
    series = {source: {} for source in sources}
    raws = {source: [] for source in sources}
    gaps, seen = [], set()
    for capture in pack["captures"]:
        check_fields(capture, CAPTURE_FIELDS, "原始回應")
        source = capture["source"]
        if source not in sources:
            raise FearGreedError("不支援的來源")
        _check_url(source, capture["url"], cutoff_day, sources)
        if (source, capture["url"]) in seen:
            raise FearGreedError("原始回應重複")
        seen.add((source, capture["url"]))
        if parse_aware_time(capture["available_at"], "available_at", error=FearGreedError) > cutoff:
            raise FearGreedError("原始回應取得時間晚於截止時間")
        if not isinstance(capture["raw_text"], str) or capture["raw_sha256"] != canonical_sha256(capture["raw_text"]):
            raise FearGreedError("原始回應遭修改")
        if capture["status"] == "failed":
            gaps.append("%s 擷取失敗：%s" % (sources[source]["name"], capture["error"] or "未知原因"))
            continue
        if capture["status"] != "fetched":
            raise FearGreedError("原始回應狀態無效")
        try:
            rows = PARSERS[source](capture["raw_text"])
        except (KeyError, TypeError, ValueError) as exc:
            gaps.append("%s 格式無法解析：%s" % (sources[source]["name"], exc))
            continue
        for day, value in rows.items():
            if series[source].get(day, value) != value:
                raise FearGreedError("%s 同日數值不一致：%s" % (sources[source]["name"], day))
            series[source][day] = value
        raws[source].append(capture["raw_sha256"])
    return series, raws, gaps


def score_as_of(pack, day):
    """用同一份資料包的原始回應，重算內容日 <= ``day`` 的正式分數（已核准且可用的成分平均，至少 MIN_COMPONENTS 項）。

    只用內容日不晚於 ``day`` 的列；原始回應的取得時間仍須不晚於資料包截止時間。成分不足、資料過舊或來源未核准一律回 None，不補值。
    只能用來算「截止時間之前」的歷史分數，``day`` 晚於資料包截止日即拒絕。
    """
    check_fields(pack, PACK_FIELDS, "恐懼貪婪資料包")
    if pack["schema_version"] not in PACK_VERSIONS or seal(pack) != pack:
        raise FearGreedError("恐懼貪婪資料包身分或雜湊無效")
    cutoff = parse_aware_time(pack["decision_cutoff"], "decision_cutoff", error=FearGreedError)
    cutoff_day = cutoff.astimezone(ZoneInfo("Asia/Taipei")).date().isoformat()
    if day > cutoff_day:
        raise FearGreedError("歷史分數日期晚於資料包截止日")
    v2 = pack["schema_version"] in (PACK_VERSION_V11, PACK_VERSION_V12)
    sources = sources_for(pack["schema_version"])
    approvals = _approvals(pack["source_approvals"], cutoff, sources)
    if not isinstance(pack["captures"], list):
        raise FearGreedError("原始回應必須是陣列")
    series, raws, _gaps = _load_series(pack, cutoff, cutoff_day, sources)
    scores = []
    for source in [s for s in sources if s in SOURCES] + ([LEVEL_SOURCE] if v2 else []):
        if source == LEVEL_SOURCE:
            if not (all(approvals.get(item, False) for item in LEVEL_INPUTS) and all(raws[item] for item in LEVEL_INPUTS)):
                continue
            computed, _reason = _level_score(series["FINMIND_MARGIN"], series["TWSE_TAIEX"], day)
        else:
            if not (approvals.get(source, False) and raws[source]):
                continue
            computed, _reason = _score(source, series[source], day)
        if computed is not None:
            scores.append(Decimal(computed["score"]))
    if len(scores) < MIN_COMPONENTS:
        return None
    return decimal_string((sum(scores) / len(scores)).quantize(Decimal("0.1")))


def evaluate(pack):
    check_fields(pack, PACK_FIELDS, "恐懼貪婪資料包")
    if pack["schema_version"] not in PACK_VERSIONS or not isinstance(pack["snapshot_id"], str) or not pack["snapshot_id"]:
        raise FearGreedError("恐懼貪婪資料包身分無效")
    if seal(pack) != pack:
        raise FearGreedError("恐懼貪婪資料包雜湊不符")
    cutoff = parse_aware_time(pack["decision_cutoff"], "decision_cutoff", error=FearGreedError)
    cutoff_day = cutoff.astimezone(ZoneInfo("Asia/Taipei")).date().isoformat()
    v3 = pack["schema_version"] == PACK_VERSION_V12
    v2 = v3 or pack["schema_version"] == PACK_VERSION_V11
    score_version = SCORE_VERSION_V3 if v3 else SCORE_VERSION_V2 if v2 else SCORE_VERSION
    sources = sources_for(pack["schema_version"])
    approvals = _approvals(pack["source_approvals"], cutoff, sources)
    if not isinstance(pack["captures"], list):
        raise FearGreedError("原始回應必須是陣列")
    series, raws, gaps = _load_series(pack, cutoff, cutoff_day, sources)
    components = []
    scored = [(source, SOURCES[source]["name"], approvals.get(source, False)) for source in sources if source in SOURCES]
    if v2:
        scored.append((LEVEL_SOURCE, LEVEL_NAME, all(approvals.get(source, False) for source in LEVEL_INPUTS)))
    for source, name, approved in scored:
        if source == LEVEL_SOURCE:
            have = all(raws[item] for item in LEVEL_INPUTS)
            computed, reason = _level_score(series["FINMIND_MARGIN"], series["TWSE_TAIEX"], cutoff_day) if have else (
                None, "%s 缺少融資金額或加權指數的原始回應" % name)
            source_raws = sorted(raws["FINMIND_MARGIN"] + raws["TWSE_TAIEX"])
        else:
            computed, reason = _score(source, series[source], cutoff_day) if raws[source] else (None, "%s 沒有可用原始回應" % name)
            source_raws = sorted(raws[source])
        row = {"source": source, "name": name, "approved": approved}
        if computed is None:
            gaps.append(reason)
            row.update(status="unavailable", as_of=None, latest_value=None, score=None, sample_size=0, evidence_id=None)
        else:
            evidence = "fear-greed:%s:%s" % (source, canonical_sha256({"raws": source_raws, "as_of": computed["as_of"]})[:20])
            row.update(status="available", evidence_id=evidence, **computed)
        if not row["approved"]:
            gaps.append("%s 尚未核准使用，只作診斷，不計入正式分數。" % name)
        components.append(row)
    available = [row for row in components if row["status"] == "available"]
    formal = [row for row in available if row["approved"]]

    def mean(rows):
        return (sum(Decimal(row["score"]) for row in rows) / len(rows)).quantize(Decimal("0.1")) if rows else None

    score = mean(formal) if len(formal) >= MIN_COMPONENTS else None
    if score is None:
        gaps.append("已核准且可用的指標只有 %d 項，至少需要 %d 項。" % (len(formal), MIN_COMPONENTS))
    band, outlook = _band(score) if score is not None else (None, "unknown")
    findings = []
    if score is not None:
        parts = ["%s %s（%s，%s 分）" % (row["name"], row["latest_value"], row["as_of"], row["score"]) for row in formal]
        findings.append({"text": "台股恐懼貪婪指標（%s）為 %s 分，屬「%s」；依據：%s。0 分最恐懼、100 分最貪婪。" % (
            score_version, decimal_string(score), BAND_LABELS[band], "、".join(parts)),
            "evidence_ids": [row["evidence_id"] for row in formal]})
    diagnostic = mean(available)
    return seal({
        "schema_version": RESULT_VERSION_V12 if v3 else RESULT_VERSION_V11 if v2 else RESULT_VERSION,
        "score_version": score_version,
        "snapshot_id": pack["snapshot_id"], "decision_cutoff": pack["decision_cutoff"],
        "input_sha256": pack["content_sha256"], "status": "available" if score is not None else "unavailable",
        "score": decimal_string(score) if score is not None else None, "band": band, "outlook": outlook,
        "findings": findings, "components": components,
        "diagnostic_score": decimal_string(diagnostic) if diagnostic is not None else None,
        "data_gaps": gaps, "limitations": list(LIMITATIONS) + (LIMITATIONS_V2_EXTRA if v2 else []),
    })
