"""個股新聞的來源核對與決策輸入建立；不呼叫模型、不建立交易決策。"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from etf_agent.perception.market_news.contracts import MarketNewsError
from etf_agent.perception.market_news.repository import MarketNewsRepository

from .contracts import CANDIDATE_FIELDS, REVIEW_TABLE, SOURCE, SOURCES, CompanyNewsError
from .tools import build_pack, symbol_codes


def record_source_review(database_path, license_status, evidence, provider=None):
    """抓取 FinMind 規範原文後保存一次核對；只對保存之後的決策截止時間生效，不回溯。"""
    from etf_agent.perception.market_news.provider import MarketNewsRSSProvider
    provider = provider or MarketNewsRSSProvider()
    terms_url = SOURCES[SOURCE]["terms_url"]
    try:
        terms = [provider.capture_terms(terms_url, registry=SOURCES)]
        reviewed_at = datetime.now(timezone.utc).isoformat()
        review = {"source": SOURCE, "license_status": license_status, "terms_url": terms_url,
                  "usage_scope": "competition_research", "reviewed_at": reviewed_at, "evidence": evidence}
        MarketNewsRepository(database_path).save_source_reviews(
            [review], terms, reviewed_at, registry=SOURCES, table=REVIEW_TABLE)
    except MarketNewsError as error:
        raise CompanyNewsError(str(error)) from error
    return {"reviewed_at": reviewed_at, "source": SOURCE, "license_status": license_status,
            "terms": [{"terms_url": t["terms_url"], "raw_sha256": t["raw_sha256"]} for t in terms]}


def approved_review(database_path, decision_cutoff):
    """截止時間前最新的一筆核對；不是 approved 時回傳 None。"""
    try:
        reviews = MarketNewsRepository(database_path).load_source_reviews(decision_cutoff, table=REVIEW_TABLE)
    except MarketNewsError as error:
        raise CompanyNewsError(str(error)) from error
    review = next((row for row in reviews if row["source"] == SOURCE), None)
    return review if review is not None and review["license_status"] == "approved" else None


def build_company_news_input(database_path, snapshot):
    """FinMind 未在截止時間前核准時回傳 None（決策輸入不加這個鍵）；核准但沒有新聞時 items 為空。"""
    cutoff = snapshot["decision_cutoff"]
    review = approved_review(database_path, cutoff)
    if review is None:
        return None
    symbols = [str(row["symbol"]).upper() for row in snapshot.get("latest_prices", [])]
    codes = sorted(symbol_codes(symbols))
    path = Path(database_path).resolve()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='finmind_news_candidates'").fetchone()
        # 時間欄位可能帶不同時區，不在 SQL 用字串比較；視窗由 build_pack 解析後判斷。
        rows = [] if not exists or not codes else [dict(row) for row in connection.execute(
            "SELECT %s, available_at, content_sha256 FROM finmind_news_candidates WHERE stock_id IN (%s)"
            % (", ".join(CANDIDATE_FIELDS), ",".join("?" * len(codes))), codes)]
    return build_pack(rows, review, symbols, snapshot["snapshot_id"], cutoff)
