import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, Iterator, Optional, Sequence, Tuple

from etf_agent.contracts import SourceFeasibilityReport, UniverseValidationResult

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
    fetched_at,
    run_id,
    raw_payload_id
FROM (
    SELECT
        daily_prices.*,
        ROW_NUMBER() OVER (
            PARTITION BY symbol, trade_date
            ORDER BY CASE source
                WHEN 'TPEX_TRADING_STOCK' THEN 1
                WHEN 'TWSE_STOCK_DAY' THEN 1
                WHEN 'TWSE_STOCK_DAY_ALL' THEN 2
                WHEN 'TPEX_MAINBOARD_QUOTES' THEN 2
                WHEN 'YAHOO_FINANCE' THEN 3
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

CREATE TABLE IF NOT EXISTS financial_statements (
    document_id INTEGER PRIMARY KEY REFERENCES source_documents(id),
    symbol TEXT NOT NULL REFERENCES instruments(symbol),
    statement_type TEXT NOT NULL CHECK (
        statement_type IN ('income_statement', 'balance_sheet')
    ),
    industry TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_quarter INTEGER NOT NULL CHECK (fiscal_quarter BETWEEN 1 AND 4),
    period_start TEXT,
    period_end TEXT NOT NULL,
    period_kind TEXT NOT NULL CHECK (
        period_kind IN ('cumulative_to_quarter', 'point_in_time')
    ),
    reporting_scope TEXT NOT NULL CHECK (
        reporting_scope IN ('consolidated', 'individual', 'unknown')
    ),
    currency TEXT NOT NULL,
    unit_multiplier INTEGER NOT NULL CHECK (unit_multiplier > 0),
    reported_at TEXT NOT NULL,
    source_published_at TEXT,
    mapping_version TEXT NOT NULL,
    facts_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_financial_statements_symbol_period
ON financial_statements(symbol, statement_type, fiscal_year, fiscal_quarter);

CREATE TABLE IF NOT EXISTS financial_statement_facts (
    document_id INTEGER NOT NULL REFERENCES financial_statements(document_id),
    metric_key TEXT NOT NULL,
    source_field TEXT,
    value TEXT,
    value_status TEXT NOT NULL CHECK (
        value_status IN ('provided', 'not_reported')
    ),
    currency TEXT NOT NULL,
    unit_multiplier INTEGER NOT NULL CHECK (unit_multiplier > 0),
    PRIMARY KEY(document_id, metric_key),
    CHECK (
        (value_status = 'provided' AND value IS NOT NULL AND source_field IS NOT NULL)
        OR (value_status = 'not_reported' AND value IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_financial_statement_facts_metric
ON financial_statement_facts(metric_key);

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

CREATE TABLE IF NOT EXISTS snapshot_prices (
    snapshot_id TEXT NOT NULL REFERENCES research_snapshots(id),
    symbol TEXT NOT NULL REFERENCES instruments(symbol),
    trade_date TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payloads(id),
    PRIMARY KEY(snapshot_id, symbol)
);
"""

SOURCE_HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_feasibility_reports (
    report_id TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'degraded', 'failed')),
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS universe_validation_runs (
    validation_id TEXT PRIMARY KEY,
    source_report_id TEXT NOT NULL REFERENCES source_feasibility_reports(report_id),
    generated_at TEXT NOT NULL,
    universe_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'degraded', 'failed')),
    usable INTEGER NOT NULL CHECK (usable IN (0, 1)),
    total_count INTEGER NOT NULL,
    tradable_count INTEGER NOT NULL,
    not_tradable_count INTEGER NOT NULL,
    mismatch_count INTEGER NOT NULL,
    result_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_universe_validation_generated_at
ON universe_validation_runs(generated_at);
"""

TRADING_STATUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS trading_status_bundles (
    bundle_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    decision_cutoff TEXT NOT NULL,
    target_session_start TEXT NOT NULL,
    target_session_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'degraded')),
    content_sha256 TEXT NOT NULL,
    bundle_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trading_status_snapshot_cutoff
ON trading_status_bundles(snapshot_id, decision_cutoff);
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
            connection.executescript(SOURCE_HEALTH_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_versions(version) VALUES (4)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_versions(version) VALUES (5)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_versions(version) VALUES (6)"
            )
            connection.executescript(TRADING_STATUS_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_versions(version) VALUES (7)"
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
                    WHEN instruments.in_competition_universe = 1
                         AND excluded.in_competition_universe = 0
                    THEN instruments.source_date
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

    @classmethod
    def replace_competition_universe(
        cls,
        connection: sqlite3.Connection,
        instruments: Iterable[Instrument],
    ) -> None:
        rows = list(instruments)
        if not rows:
            raise ValueError("官方交易池是空的")
        connection.execute(
            "UPDATE instruments SET in_competition_universe = 0, updated_at = CURRENT_TIMESTAMP"
        )
        cls.upsert_instruments(connection, rows, True)

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

    def latest_complete_trade_date(self, sources: Sequence[str]) -> Optional[str]:
        required = tuple(dict.fromkeys(sources))
        if not required:
            raise ValueError("至少需要一個行情來源")
        placeholders = ", ".join("?" for _ in required)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT source, MAX(trade_date) AS last_date
                FROM daily_prices
                WHERE source IN (%s)
                GROUP BY source
                """
                % placeholders,
                required,
            ).fetchall()
        if len(rows) != len(required) or any(not row["last_date"] for row in rows):
            return None
        return min(str(row["last_date"]) for row in rows)

    def latest_trade_dates_by_symbol(self, sources: Sequence[str]) -> Dict[str, str]:
        """傳回指定來源各標的最後一筆已保存日線，供增量更新使用。"""

        required = tuple(dict.fromkeys(sources))
        if not required:
            raise ValueError("至少需要一個行情來源")
        placeholders = ", ".join("?" for _ in required)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, MAX(trade_date) AS last_date
                FROM daily_prices
                WHERE source IN (%s)
                GROUP BY symbol
                """
                % placeholders,
                required,
            ).fetchall()
        return {
            str(row["symbol"]): str(row["last_date"])
            for row in rows
            if row["last_date"]
        }

    def history_price_ranges(
        self, sources: Sequence[str]
    ) -> Dict[Tuple[str, str], Tuple[str, str, int]]:
        """傳回官方歷史行情的可稽核區間，不以 Yahoo 補足覆蓋。"""

        required = tuple(dict.fromkeys(sources))
        if not required:
            raise ValueError("至少需要一個行情來源")
        placeholders = ", ".join("?" for _ in required)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, source, MIN(trade_date) AS first_date,
                       MAX(trade_date) AS last_date, COUNT(*) AS row_count
                FROM daily_prices
                WHERE source IN (%s)
                GROUP BY symbol, source
                """
                % placeholders,
                required,
            ).fetchall()
        return {
            (str(row["symbol"]), str(row["source"])): (
                str(row["first_date"]),
                str(row["last_date"]),
                int(row["row_count"]),
            )
            for row in rows
            if row["first_date"] and row["last_date"]
        }

    def save_source_feasibility_report(
        self, report: SourceFeasibilityReport
    ) -> None:
        self.initialize()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_feasibility_reports(
                    report_id, generated_at, status, report_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.generated_at,
                    report.status,
                    json.dumps(report.as_dict(), ensure_ascii=False),
                ),
            )

    def save_universe_validation(
        self, validation: UniverseValidationResult
    ) -> None:
        self.initialize()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO universe_validation_runs(
                    validation_id, source_report_id, generated_at, universe_version,
                    status, usable, total_count, tradable_count,
                    not_tradable_count, mismatch_count, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validation.validation_id,
                    validation.source_report_id,
                    validation.generated_at,
                    validation.universe_version,
                    validation.status,
                    int(validation.usable),
                    validation.total_count,
                    validation.tradable_count,
                    validation.not_tradable_count,
                    validation.mismatch_count,
                    json.dumps(validation.as_dict(), ensure_ascii=False),
                ),
            )
