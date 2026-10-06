"""追加保存全市場 RSS 版本；不覆寫個股新聞及原始時間。"""
import json
import sqlite3
from etf_agent.core import canonical_sha256, canonical_json, parse_aware_time
from .contracts import NEWS_SOURCES, MarketNewsError, check_fields

class MarketNewsRepository:
    def __init__(self, database_path):
        self.database_path = str(database_path)

    def save_captures(self, captures):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS market_news_captures (
                capture_id TEXT PRIMARY KEY, source TEXT NOT NULL, scope TEXT NOT NULL,
                available_at TEXT NOT NULL, raw_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL)''')
            for row in captures:
                connection.execute('INSERT OR IGNORE INTO market_news_captures VALUES (?,?,?,?,?,?)',
                    (canonical_sha256(row),row['source'],'TW_STOCK_MARKET',row['available_at'],row['raw_sha256'],
                     json.dumps(row,ensure_ascii=False,sort_keys=True)))

    def save_source_reviews(self, reviews, terms_captures, available_at):
        """追加真實核對紀錄與原始依據；不推定或提升使用狀態。"""
        available = parse_aware_time(available_at, 'available_at', error=MarketNewsError)
        entries = []
        for review in reviews:
            check_fields(review, {'source','license_status','terms_url','usage_scope','reviewed_at','evidence'}, '來源使用核對')
            source = review['source']
            if source not in NEWS_SOURCES or review['terms_url'] != NEWS_SOURCES[source]['terms_url']:
                raise MarketNewsError('來源使用核對身分無效')
            if review['license_status'] not in {'approved','unverified','restricted'}:
                raise MarketNewsError('來源使用狀態無效')
            if review['usage_scope'] != 'competition_research' or not isinstance(review['evidence'], str) or not review['evidence'].strip():
                raise MarketNewsError('來源使用核對缺少範圍或依據')
            reviewed = parse_aware_time(review['reviewed_at'], 'reviewed_at', error=MarketNewsError)
            if reviewed > available:
                raise MarketNewsError('來源使用核對時間晚於保存時間')
            captures = [dict(c) for c in terms_captures if c.get('terms_url') == review['terms_url']]
            if not captures:
                raise MarketNewsError('來源使用核對缺少原始規範依據')
            for capture in captures:
                if not isinstance(capture.get('raw_text'), str) or not capture['raw_text'].strip() or capture.get('raw_sha256') != canonical_sha256(capture['raw_text']):
                    raise MarketNewsError('來源規範內容遭修改或為空')
                if parse_aware_time(capture['available_at'], '規範取得時間', error=MarketNewsError) > reviewed:
                    raise MarketNewsError('來源規範取得時間晚於核對時間')
            payload = {'review':dict(review),'terms_captures':captures,'available_at':available.isoformat()}
            entries.append((canonical_sha256(payload),source,available.isoformat(),canonical_json(payload)))
        with sqlite3.connect(self.database_path) as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS market_news_source_reviews (
                review_id TEXT PRIMARY KEY, source TEXT NOT NULL,
                available_at TEXT NOT NULL, payload_json TEXT NOT NULL)''')
            connection.executemany('INSERT OR IGNORE INTO market_news_source_reviews VALUES (?,?,?,?)',entries)

    def load_source_reviews(self, decision_cutoff):
        """只讀截止時間前已保存的版本，較晚的限制可取代較早核准。"""
        cutoff = parse_aware_time(decision_cutoff, 'decision_cutoff', error=MarketNewsError)
        with sqlite3.connect(self.database_path) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_news_source_reviews'").fetchone():
                return []
            rows = connection.execute('SELECT review_id,source,available_at,payload_json FROM market_news_source_reviews').fetchall()
        selected = {}
        for identity, source, saved_at, raw in rows:
            saved = parse_aware_time(saved_at, '保存時間', error=MarketNewsError)
            if saved > cutoff:
                continue
            payload = json.loads(raw)
            if canonical_sha256(payload) != identity or payload['review']['source'] != source or payload['available_at'] != saved_at:
                raise MarketNewsError('資料庫來源使用核對遭修改')
            reviewed = parse_aware_time(payload['review']['reviewed_at'], 'reviewed_at', error=MarketNewsError)
            if reviewed > saved:
                raise MarketNewsError('來源使用核對時間晚於保存時間')
            order = (reviewed, saved, identity)
            if source not in selected or order > selected[source][0]:
                selected[source] = (order,payload['review'])
        return [selected[s][1] for s in sorted(selected)]
