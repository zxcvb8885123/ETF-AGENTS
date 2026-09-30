"""版本化擷取 FinMind 財報與個股新聞線索。"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable

from etf_agent.core import canonical_json, canonical_sha256, decimal_string, parse_aware_time, parse_decimal

from .database import MarketDataDatabase


FINMIND_ENDPOINT = "https://api.finmindtrade.com/api/v4/data"
FINMIND_DATASETS = frozenset({
    "TaiwanStockFinancialStatements",
    "TaiwanStockBalanceSheet",
    "TaiwanStockCashFlowsStatement",
})
FINMIND_NEWS_DATASET = "TaiwanStockNews"
FINMIND_LICENSE_URL = "https://finmind.github.io/en/Disclaimer/"

SUPPLEMENTAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS supplemental_facts (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    dataset TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    value TEXT NOT NULL,
    origin_name TEXT NOT NULL,
    published_at TEXT,
    available_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payloads(id)
);
CREATE INDEX IF NOT EXISTS idx_supplemental_facts_lookup
ON supplemental_facts(stock_id, dataset, period_end, available_at);
CREATE TABLE IF NOT EXISTS news_candidates (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT NOT NULL,
    summary TEXT NOT NULL,
    published_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    license_status TEXT NOT NULL CHECK (license_status = 'approved'),
    license_url TEXT NOT NULL,
    symbol TEXT,
    canonical_content_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payloads(id)
);
CREATE INDEX IF NOT EXISTS idx_news_candidates_cutoff
ON news_candidates(available_at, canonical_content_id);
CREATE TABLE IF NOT EXISTS finmind_news_candidates (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK (source = 'FINMIND_NEWS'),
    stock_id TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT NOT NULL,
    publisher TEXT NOT NULL,
    source_time_raw TEXT NOT NULL,
    published_at TEXT,
    available_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    license_status TEXT NOT NULL CHECK (license_status = 'unverified'),
    license_url TEXT NOT NULL,
    canonical_content_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payloads(id)
);
CREATE INDEX IF NOT EXISTS idx_finmind_news_candidates_cutoff
ON finmind_news_candidates(stock_id, available_at, canonical_content_id);
INSERT OR IGNORE INTO schema_versions(version) VALUES (9);
"""


@dataclass(frozen=True)
class SupplementalResult:
    run_id: str
    source: str
    fetched_rows: int
    stored_rows: int


class SupplementalRepository:
    """只新增版本；同內容重抓去重，更正內容另留版本。"""

    def __init__(self, database: MarketDataDatabase):
        self.database = database

    def save(self, source: str, endpoint: str, raw: str, fetched_at: str,
             table: str, rows: list[dict]) -> SupplementalResult:
        if table not in {"supplemental_facts", "finmind_news_candidates"}:
            raise ValueError("補充資料表未列入白名單")
        fetched_at = parse_aware_time(fetched_at, "fetched_at").isoformat()
        self.database.initialize()
        run_id = str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.executescript(SUPPLEMENTAL_SCHEMA)
            connection.execute(
                "INSERT INTO collection_runs(run_id, source, started_at, finished_at, status, fetched_rows) "
                "VALUES (?, ?, ?, ?, 'success', ?)",
                (run_id, source, fetched_at, fetched_at, len(rows)),
            )
            cursor = connection.execute(
                "INSERT INTO raw_payloads(run_id, source, endpoint, fetched_at, sha256, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, source, endpoint, fetched_at, canonical_sha256(raw),
                 canonical_json({"raw": raw})),
            )
            raw_id = cursor.lastrowid
            stored = 0
            for row in rows:
                fields = {**row, "raw_payload_id": raw_id}
                columns = ", ".join(fields)
                placeholders = ", ".join("?" for _ in fields)
                cursor = connection.execute(
                    f"INSERT OR IGNORE INTO {table}({columns}) VALUES ({placeholders})",
                    tuple(fields.values()),
                )
                stored += cursor.rowcount
            connection.execute(
                "UPDATE collection_runs SET stored_rows = ? WHERE run_id = ?",
                (stored, run_id),
            )
        return SupplementalResult(run_id, source, len(rows), stored)


class FinMindApiProvider:
    allowed_datasets = frozenset()
    dataset_error = "FinMind 資料集未列入白名單"

    def __init__(self, token: str | None = None,
                 opener: Callable = urllib.request.urlopen):
        self.token = token if token is not None else os.environ.get("FINMIND_TOKEN", "")
        self.opener = opener

    def fetch(self, dataset: str, stock_id: str, start: date, end: date) -> str:
        if not self.token:
            raise ValueError("缺少 FINMIND_TOKEN 環境變數")
        if dataset not in self.allowed_datasets:
            raise ValueError(self.dataset_error)
        if not re.fullmatch(r"[0-9A-Z]{4,6}", stock_id):
            raise ValueError("股票代號格式不合法")
        if start > end:
            raise ValueError("起日不得晚於迄日")
        query = urllib.parse.urlencode({
            "dataset": dataset, "data_id": stock_id,
            "start_date": start.isoformat(), "end_date": end.isoformat(),
        })
        request = urllib.request.Request(
            FINMIND_ENDPOINT + "?" + query,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise RuntimeError("FinMind HTTP %d；請檢查權限或額度" % error.code) from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError("FinMind 連線失敗") from None


class FinMindFundamentalProvider(FinMindApiProvider):
    allowed_datasets = FINMIND_DATASETS
    dataset_error = "FinMind 資料集未列入財報白名單"


class FinMindNewsProvider(FinMindApiProvider):
    allowed_datasets = frozenset({FINMIND_NEWS_DATASET})


class FinMindFundamentalCollector:
    def __init__(self, repository: SupplementalRepository,
                 provider: FinMindFundamentalProvider):
        self.repository = repository
        self.provider = provider

    def collect(self, dataset: str, stock_id: str, start: date, end: date,
                fetched_at: str | None = None) -> SupplementalResult:
        raw = self.provider.fetch(dataset, stock_id, start, end)
        fetched = parse_aware_time(
            fetched_at or datetime.now(timezone.utc).isoformat(), "fetched_at"
        ).isoformat()
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as error:
            raise ValueError("FinMind 回應不是 JSON") from error
        if not isinstance(payload, dict) or payload.get("status") != 200 or not isinstance(payload.get("data"), list):
            raise ValueError("FinMind 回應狀態或資料格式不正確")
        rows = []
        for item in payload["data"]:
            if not isinstance(item, dict) or item.get("stock_id") != stock_id:
                raise ValueError("FinMind 財報股票代號不符")
            try:
                period = date.fromisoformat(item["date"])
                fact_type = item["type"]
                origin_name = item["origin_name"]
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("FinMind 財報欄位不完整") from error
            if not start <= period <= end or not isinstance(fact_type, str) or not fact_type.strip() or not isinstance(origin_name, str):
                raise ValueError("FinMind 財報期間或欄位不正確")
            value = decimal_string(parse_decimal(item.get("value"), "value", reject_bool=True))
            fact = {"source": "FINMIND", "dataset": dataset, "stock_id": stock_id,
                    "period_end": period.isoformat(), "fact_type": fact_type,
                    "value": value, "origin_name": origin_name,
                    "published_at": None, "available_at": fetched, "fetched_at": fetched}
            rows.append({**fact, "content_sha256": canonical_sha256(
                {key: value for key, value in fact.items() if key not in {"available_at", "fetched_at"}}
            )})
        return self.repository.save("FINMIND", FINMIND_ENDPOINT, raw, fetched,
                                    "supplemental_facts", rows)


class FinMindNewsCollector:
    """逐股保存新聞線索；來源時間無時區與媒體授權未核實，禁止正式計算。"""

    def __init__(self, repository: SupplementalRepository,
                 provider: FinMindNewsProvider):
        self.repository = repository
        self.provider = provider

    def collect(self, stock_id: str, day: date,
                fetched_at: str | None = None) -> SupplementalResult:
        raw = self.provider.fetch(FINMIND_NEWS_DATASET, stock_id, day, day)
        fetched = parse_aware_time(
            fetched_at or datetime.now(timezone.utc).isoformat(), "fetched_at"
        ).isoformat()
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as error:
            raise ValueError("FinMind 新聞回應不是 JSON") from error
        if not isinstance(payload, dict) or payload.get("status") != 200 or not isinstance(payload.get("data"), list):
            raise ValueError("FinMind 新聞回應狀態或資料格式不正確")
        rows = []
        for item in payload["data"]:
            if not isinstance(item, dict) or item.get("stock_id") != stock_id:
                raise ValueError("FinMind 新聞股票代號不符")
            title = item.get("title")
            link = item.get("link")
            publisher = item.get("source")
            source_time = item.get("date")
            if not all(isinstance(value, str) and value.strip()
                       for value in (title, link, publisher, source_time)):
                raise ValueError("FinMind 新聞欄位不完整")
            title, link, publisher, source_time = (
                value.strip() for value in (title, link, publisher, source_time)
            )
            try:
                content_time = datetime.fromisoformat(source_time)
            except ValueError as error:
                raise ValueError("FinMind 新聞時間格式不正確") from error
            if content_time.date() != day:
                raise ValueError("FinMind 新聞日期不符")
            parsed_link = urllib.parse.urlparse(link)
            if parsed_link.scheme not in {"http", "https"} or not parsed_link.hostname:
                raise ValueError("FinMind 新聞連結格式不正確")
            canonical_link = urllib.parse.urlunparse(parsed_link._replace(fragment=""))
            content = {"source": "FINMIND_NEWS", "stock_id": stock_id,
                       "title": title, "link": link, "publisher": publisher,
                       "source_time_raw": source_time, "published_at": None,
                       "license_status": "unverified", "license_url": FINMIND_LICENSE_URL,
                       "canonical_content_id": canonical_sha256({"link": canonical_link})}
            rows.append({**content, "available_at": fetched, "fetched_at": fetched,
                         "content_sha256": canonical_sha256(content)})
        endpoint = FINMIND_ENDPOINT + "?" + urllib.parse.urlencode({
            "dataset": FINMIND_NEWS_DATASET, "data_id": stock_id,
            "start_date": day.isoformat(), "end_date": day.isoformat(),
        })
        return self.repository.save("FINMIND_NEWS", endpoint, raw, fetched,
                                    "finmind_news_candidates", rows)
