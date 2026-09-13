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
                price_change, volume_shares, trade_value, transactions, source,
                fetched_at, run_id, raw_payload_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, trade_date, source) DO UPDATE SET
                open_price = excluded.open_price,
                high_price = excluded.high_price,
                low_price = excluded.low_price,
                close_price = excluded.close_price,
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
