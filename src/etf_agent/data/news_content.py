"""新聞原頁核對版本保存；候選不直接升級為正式情緒證據。"""

from etf_agent.core import canonical_json, canonical_sha256, parse_aware_time
from .database import MarketDataDatabase


def save_news_captures(database: MarketDataDatabase, captures: list) -> int:
    database.initialize()
    saved = 0
    with database.connect() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS news_page_captures (
                id INTEGER PRIMARY KEY, url TEXT NOT NULL, observed_at TEXT NOT NULL,
                status TEXT NOT NULL, content_sha256 TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                license_status TEXT NOT NULL CHECK(license_status='unverified')
            );
            CREATE INDEX IF NOT EXISTS idx_news_page_captures ON news_page_captures(url,observed_at);
        """)
        for capture in captures:
            parse_aware_time(capture.get("observed_at"), "observed_at")
            if capture.get("status") == "fetched" and capture.get("html_sha256") != canonical_sha256(capture.get("html")):
                raise ValueError("新聞原頁內容雜湊不符，拒絕保存")
            digest = canonical_sha256(capture)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO news_page_captures(url,observed_at,status,content_sha256,payload_json,license_status) VALUES (?,?,?,?,?,'unverified')",
                (capture["url"],capture["observed_at"],capture["status"],digest,canonical_json(capture)))
            saved += cursor.rowcount
    return saved
