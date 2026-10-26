"""確定性整理個股新聞：視窗、白名單、去重與封存雜湊。"""
from datetime import timedelta

from etf_agent.core import canonical_sha256, parse_aware_time

from .contracts import (
    BATCH_HOURS, CANDIDATE_FIELDS, SCHEMA_VERSION, SOURCE, WINDOW_DAYS, CompanyNewsError,
    PUBLISHER_HOSTS, evidence_id, link_host, outlet, publisher_policy,
)


def window_start(decision_cutoff, latest_fetch_at):
    """最後一批抓取往前 BATCH_HOURS，且不早於截止前 WINDOW_DAYS；沒有任何抓取時視窗為空（等於截止時間）。"""
    cutoff = parse_aware_time(decision_cutoff, "decision_cutoff", error=CompanyNewsError)
    if latest_fetch_at is None:
        return cutoff.isoformat()
    latest = parse_aware_time(latest_fetch_at, "latest_fetch_at", error=CompanyNewsError)
    return max(latest - timedelta(hours=BATCH_HOURS), cutoff - timedelta(days=WINDOW_DAYS)).isoformat()


def latest_fetch(fetch_times, decision_cutoff):
    """截止前最後一次抓取的時間（交易池內全部抓取，不分媒體、含空回應與重複內容）。"""
    cutoff = parse_aware_time(decision_cutoff, "decision_cutoff", error=CompanyNewsError)
    times = [(parse_aware_time(value, "fetched_at", error=CompanyNewsError), value) for value in fetch_times]
    eligible = [entry for entry in times if entry[0] <= cutoff]
    return max(eligible)[1] if eligible else None


def symbol_codes(symbols):
    """交易池代號（2330.TW）對應 FinMind 股票代號（2330）。"""
    return {str(symbol).upper().split(".")[0]: str(symbol).upper() for symbol in symbols}


def build_pack(rows, review, symbols, snapshot_id, decision_cutoff, fetch_times=None):
    """只收視窗內、白名單媒體、交易池內的候選；同股同連結取最早可得的版本。

    ``fetch_times`` 是交易池內每次實際抓取的時間（raw_payloads，含空回應與全部重複的回應）；
    候選依內容去重，重抓舊聞不會新增候選，只看候選時間會把前一批誤認為最新批次。
    省略時只用候選的可得時間（僅供沒有抓取紀錄的測試資料）。
    """
    cutoff = parse_aware_time(decision_cutoff, "decision_cutoff", error=CompanyNewsError)
    codes = symbol_codes(symbols)
    if fetch_times is None:
        fetch_times = [row["available_at"] for row in rows if str(row["stock_id"]) in codes]
    latest = latest_fetch(fetch_times, decision_cutoff)
    start_at = window_start(decision_cutoff, latest)
    start = parse_aware_time(start_at, "window_start", error=CompanyNewsError)
    chosen = {}
    for row in rows:
        symbol = codes.get(str(row["stock_id"]))
        if symbol is None or link_host(row["link"]) not in PUBLISHER_HOSTS:
            continue
        available = parse_aware_time(row["available_at"], "available_at", error=CompanyNewsError)
        if not start < available <= cutoff:
            continue
        candidate = {field: row[field] for field in CANDIDATE_FIELDS}
        if canonical_sha256(candidate) != row["content_sha256"]:
            raise CompanyNewsError("資料庫個股新聞內容雜湊不符：%s" % row["content_sha256"])
        item = {"evidence_id": evidence_id(row["content_sha256"]), "symbol": symbol,
                "available_at": row["available_at"], "content_sha256": row["content_sha256"],
                "candidate": candidate}
        key = (symbol, candidate["canonical_content_id"])
        order = (available, row["content_sha256"])
        if key not in chosen or order < chosen[key][0]:
            chosen[key] = (order, item)
    items = sorted((entry[1] for entry in chosen.values()),
                   key=lambda item: (item["symbol"], item["available_at"], item["evidence_id"]))
    return seal({
        "schema_version": SCHEMA_VERSION, "source": SOURCE, "snapshot_id": snapshot_id,
        "decision_cutoff": decision_cutoff, "latest_fetch_at": latest, "window_start": start_at,
        "window_days": WINDOW_DAYS, "publisher_policy": publisher_policy(),
        "source_review": dict(review), "items": items,
    })


def seal(pack):
    body = {key: value for key, value in pack.items() if key != "content_sha256"}
    return dict(body, content_sha256=canonical_sha256(body))


def news_outlets(pack):
    """每檔股票每則新聞的媒體品牌；Validator 用來確認 high 不是只憑單一媒體的標題。"""
    result = {}
    for item in (pack or {}).get("items", []):
        result.setdefault(item["symbol"], {})[item["evidence_id"]] = outlet(item["candidate"]["link"])
    return result


def news_by_symbol(pack):
    """依股票整理給事件分析師的新聞；只有標題，沒有內文。"""
    result = {}
    for item in (pack or {}).get("items", []):
        result.setdefault(item["symbol"], []).append({
            "evidence_id": item["evidence_id"], "title": item["candidate"]["title"],
            "publisher": item["candidate"]["publisher"], "available_at": item["available_at"],
        })
    return result
