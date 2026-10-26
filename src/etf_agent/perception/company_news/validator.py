"""以封存內容重建驗證個股新聞資料包；任何改寫、未核准或越界都拒絕。"""
from etf_agent.core import canonical_sha256, parse_aware_time

from .contracts import (
    CANDIDATE_FIELDS, ITEM_FIELDS, PACK_FIELDS, REVIEW_FIELDS, SCHEMA_VERSION, SOURCE, SOURCES,
    WINDOW_DAYS, PUBLISHER_HOSTS, CompanyNewsError, canonical_content_id, check_fields,
    evidence_id, link_host, publisher_policy,
)
from .tools import seal, symbol_codes, window_start


def validate_pack(pack, snapshot_id, decision_cutoff, symbols):
    check_fields(pack, PACK_FIELDS, "個股新聞資料包")
    if pack["schema_version"] != SCHEMA_VERSION or pack["source"] != SOURCE:
        raise CompanyNewsError("個股新聞版本或來源不符")
    cutoff = parse_aware_time(decision_cutoff, "decision_cutoff", error=CompanyNewsError)
    if pack["snapshot_id"] != snapshot_id or parse_aware_time(pack["decision_cutoff"], "decision_cutoff", error=CompanyNewsError) != cutoff:
        raise CompanyNewsError("個股新聞 Snapshot 或截止時間不一致")
    latest = pack["latest_fetch_at"]
    if latest is not None and parse_aware_time(latest, "latest_fetch_at", error=CompanyNewsError) > cutoff:
        raise CompanyNewsError("個股新聞最後抓取時間晚於截止時間")
    if pack["window_days"] != WINDOW_DAYS or pack["window_start"] != window_start(pack["decision_cutoff"], latest):
        raise CompanyNewsError("個股新聞查詢視窗不符")
    if pack["publisher_policy"] != publisher_policy():
        raise CompanyNewsError("個股新聞媒體白名單版本不符")
    review = pack["source_review"]
    check_fields(review, REVIEW_FIELDS, "個股新聞來源核對")
    if (review["source"] != SOURCE or review["terms_url"] != SOURCES[SOURCE]["terms_url"]
            or review["license_status"] != "approved" or review["usage_scope"] != "competition_research"
            or not isinstance(review["evidence"], str) or not review["evidence"].strip()):
        raise CompanyNewsError("個股新聞來源未在截止時間前核准")
    if parse_aware_time(review["reviewed_at"], "reviewed_at", error=CompanyNewsError) > cutoff:
        raise CompanyNewsError("個股新聞來源核准晚於截止時間")
    if not isinstance(pack["items"], list):
        raise CompanyNewsError("個股新聞 items 必須是陣列")
    start = parse_aware_time(pack["window_start"], "window_start", error=CompanyNewsError)
    codes = symbol_codes(symbols)
    seen_ids, seen_keys, order = set(), set(), []
    for index, item in enumerate(pack["items"]):
        name = "個股新聞 items[%d]" % index
        check_fields(item, ITEM_FIELDS, name)
        candidate = item["candidate"]
        check_fields(candidate, set(CANDIDATE_FIELDS), name + ".candidate")
        if canonical_sha256(candidate) != item["content_sha256"]:
            raise CompanyNewsError(name + " 內容遭修改")
        if item["evidence_id"] != evidence_id(item["content_sha256"]):
            raise CompanyNewsError(name + " evidence_id 不符")
        if candidate["source"] != SOURCE or codes.get(str(candidate["stock_id"])) != item["symbol"]:
            raise CompanyNewsError(name + " 股票或來源不符")
        if link_host(candidate["link"]) not in PUBLISHER_HOSTS:
            raise CompanyNewsError(name + " 媒體不在白名單")
        if candidate["canonical_content_id"] != canonical_content_id(candidate["link"]):
            raise CompanyNewsError(name + " canonical_content_id 不符")
        if candidate["published_at"] is not None:
            raise CompanyNewsError(name + " 來源時間沒有時區，published_at 必須留空")
        available = parse_aware_time(item["available_at"], "available_at", error=CompanyNewsError)
        if not start < available <= cutoff or available > parse_aware_time(latest, "latest_fetch_at", error=CompanyNewsError):
            raise CompanyNewsError(name + " 可得時間不在查詢視窗內")
        key = (item["symbol"], candidate["canonical_content_id"])
        if item["evidence_id"] in seen_ids or key in seen_keys:
            raise CompanyNewsError(name + " 重複")
        seen_ids.add(item["evidence_id"])
        seen_keys.add(key)
        order.append((item["symbol"], item["available_at"], item["evidence_id"]))
    if order != sorted(order):
        raise CompanyNewsError("個股新聞排序不符")
    if pack["content_sha256"] != seal(pack)["content_sha256"]:
        raise CompanyNewsError("個股新聞資料包雜湊不符")
    return pack
