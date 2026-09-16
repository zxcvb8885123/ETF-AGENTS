import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .twse import DailyPrice
from .universe import Instrument


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    fetched_rows INTEGER NOT NULL DEFAULT 0,
    stored_rows INTEGER NOT NULL DEFAULT 0,
    warning_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS raw_payloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    market TEXT NOT NULL,
    source_date TEXT NOT NULL,
    in_competition_universe INTEGER NOT NULL CHECK (in_competition_universe IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_prices (
    symbol TEXT NOT NULL REFERENCES instruments(symbol),
    trade_date TEXT NOT NULL,
    open_price TEXT,
    high_price TEXT,
    low_price TEXT,
    close_price TEXT NOT NULL,
    adjusted_close_price TEXT,
    price_change TEXT,
    volume_shares INTEGER NOT NULL,
    trade_value INTEGER NOT NULL,
    transactions INTEGER NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payloads(id),
    PRIMARY KEY (symbol, trade_date, source)
);

CREATE INDEX IF NOT EXISTS idx_daily_prices_trade_date
ON daily_prices(trade_date);

INSERT OR IGNORE INTO schema_versions(version) VALUES (1);
"""

ANALYSIS_VIEW = """
DROP VIEW IF EXISTS analysis_daily_prices;
CREATE VIEW analysis_daily_prices AS
SELECT
    symbol,
    trade_date,
    open_price,
    high_price,
    low_price,
    close_price,
    COALESCE(adjusted_close_price, close_price) AS analysis_close_price,
    adjusted_close_price,
    price_change,
    volume_shares,
    trade_value,
    transactions,
    source,
    fetched_at
FROM (
    SELECT
        daily_prices.*,
        ROW_NUMBER() OVER (
            PARTITION BY symbol, trade_date
            ORDER BY CASE source
                WHEN 'YAHOO_FINANCE' THEN 1
                WHEN 'TPEX_TRADING_STOCK' THEN 2
                WHEN 'TWSE_STOCK_DAY' THEN 2
                WHEN 'TWSE_STOCK_DAY_ALL' THEN 3
                ELSE 9
            END
        ) AS source_rank
    FROM daily_prices
    JOIN instruments USING(symbol)
    WHERE instruments.in_competition_universe = 1
)
WHERE source_rank = 1;
"""

CORPORATE_DATA_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    document_type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source_url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payloads(id),
    supersedes_document_id INTEGER REFERENCES source_documents(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, external_id, content_sha256),
    UNIQUE(source, external_id, version)
);

CREATE INDEX IF NOT EXISTS idx_source_documents_cutoff
ON source_documents(published_at, available_at);

CREATE TABLE IF NOT EXISTS document_instruments (
    document_id INTEGER NOT NULL REFERENCES source_documents(id),
    symbol TEXT NOT NULL REFERENCES instruments(symbol),
    evidence TEXT NOT NULL,
    PRIMARY KEY(document_id, symbol)
);

CREATE TABLE IF NOT EXISTS monthly_revenues (
    document_id INTEGER PRIMARY KEY REFERENCES source_documents(id),
    symbol TEXT NOT NULL REFERENCES instruments(symbol),
    revenue_period TEXT NOT NULL,
    currency TEXT NOT NULL,
    unit_multiplier INTEGER NOT NULL,
    current_revenue TEXT,
    previous_month_revenue TEXT,
    previous_year_revenue TEXT,
    mom_pct TEXT,
    yoy_pct TEXT,
    cumulative_revenue TEXT,
    previous_year_cumulative_revenue TEXT,
    cumulative_yoy_pct TEXT,
    note TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_monthly_revenues_symbol_period
ON monthly_revenues(symbol, revenue_period);

CREATE TABLE IF NOT EXISTS quality_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT REFERENCES collection_runs(run_id),
    source TEXT NOT NULL,
    external_id TEXT,
    symbol TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('warning', 'error')),
    issue_code TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS research_snapshots (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    decision_cutoff TEXT NOT NULL,
    universe_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    usable INTEGER NOT NULL CHECK (usable IN (0, 1)),
    quality_flags_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_documents (
    snapshot_id TEXT NOT NULL REFERENCES research_snapshots(id),
    document_id INTEGER NOT NULL REFERENCES source_documents(id),
    PRIMARY KEY(snapshot_id, document_id)
);
"""


class MarketDataDatabase:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(daily_prices)").fetchall()
            }
            if "adjusted_close_price" not in columns:
                connection.execute(
                    "ALTER TABLE daily_prices ADD COLUMN adjusted_close_price TEXT"
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_versions(version) VALUES (2)"
            )
            connection.executescript(CORPORATE_DATA_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_versions(version) VALUES (3)"
            )
            connection.executescript(ANALYSIS_VIEW)

    @staticmethod
    def upsert_instruments(
        connection: sqlite3.Connection,
        instruments: Iterable[Instrument],
        in_competition_universe: bool,
    ) -> None:
        connection.executemany(
            """
            INSERT INTO instruments(
                symbol, code, name, market, source_date, in_competition_universe
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                code = excluded.code,
                name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE instruments.name END,
                market = excluded.market,
                source_date = CASE
                    WHEN excluded.source_date <> '' THEN excluded.source_date
                    ELSE instruments.source_date
                END,
                in_competition_universe = MAX(
                    instruments.in_competition_universe,
                    excluded.in_competition_universe
                ),
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    item.symbol,
                    item.code,
                    item.name,
                    item.market,
                    item.source_date,
                    int(in_competition_universe),
                )
                for item in instruments
            ],
        )

    @staticmethod
    def insert_raw_payload(
        connection: sqlite3.Connection,
        run_id: str,
        source: str,
        endpoint: str,
        fetched_at: str,
        payload: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO raw_payloads(
                run_id, source, endpoint, fetched_at, sha256, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                source,
                endpoint,
                fetched_at,
                hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                payload,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def upsert_prices(
        connection: sqlite3.Connection,
        prices: Iterable[DailyPrice],
        source: str,
        fetched_at: str,
        run_id: str,
        raw_payload_id: int,
    ) -> int:
        rows = list(prices)
        connection.executemany(
            """
            INSERT INTO daily_prices(
                symbol, trade_date, open_price, high_price, low_price, close_price,
                adjusted_close_price, price_change, volume_shares, trade_value, transactions, source,
                fetched_at, run_id, raw_payload_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, trade_date, source) DO UPDATE SET
                open_price = excluded.open_price,
                high_price = excluded.high_price,
                low_price = excluded.low_price,
                close_price = excluded.close_price,
                adjusted_close_price = excluded.adjusted_close_price,
                price_change = excluded.price_change,
                volume_shares = excluded.volume_shares,
                trade_value = excluded.trade_value,
                transactions = excluded.transactions,
                fetched_at = excluded.fetched_at,
                run_id = excluded.run_id,
                raw_payload_id = excluded.raw_payload_id
            """,
            [
                (
                    price.symbol,
                    price.trade_date,
                    str(price.open_price) if price.open_price is not None else None,
                    str(price.high_price) if price.high_price is not None else None,
                    str(price.low_price) if price.low_price is not None else None,
                    str(price.close_price),
                    str(price.adjusted_close) if price.adjusted_close is not None else None,
                    str(price.change) if price.change is not None else None,
                    price.volume_shares,
                    price.trade_value,
                    price.transactions,
                    source,
                    fetched_at,
                    run_id,
                    raw_payload_id,
                )
                for price in rows
            ],
        )
        return len(rows)

    def latest_trade_date(self) -> Optional[str]:
        with self.connect() as connection:
            row = connection.execute("SELECT MAX(trade_date) AS value FROM daily_prices").fetchone()
            return str(row["value"]) if row and row["value"] else None
