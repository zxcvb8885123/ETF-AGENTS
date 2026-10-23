import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, Optional, Sequence, Tuple

from etf_agent.core import parse_aware_time

from .contracts import SourceFeasibilityReport, UniverseValidationResult
from .price_finality import (
    TAIPEI_TIMEZONE,
    price_finalized_at,
    requires_finalized_fetch,
)
from .twse import DailyPrice
from .universe import Instrument


# daily_prices 是 append-only 的版本表：主鍵含 fetched_at，同一（symbol, trade_date, source）
# 每次抓到不同內容就新增一個版本，讀取端取「fetched_at ≤ cutoff 的最新版本」才能依原時間點重放。
DAILY_PRICES_COLUMNS_SQL = """
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
    ex_rights_marker INTEGER,
    PRIMARY KEY (symbol, trade_date, source, fetched_at)
"""

DAILY_PRICES_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_daily_prices_trade_date
ON daily_prices(trade_date);

CREATE INDEX IF NOT EXISTS idx_daily_prices_source_date
ON daily_prices(source, trade_date, symbol);
"""

# 版本內容欄位：這些欄位全部相同就視為同一版本，不另新增列。
PRICE_CONTENT_FIELDS = (
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "adjusted_close_price",
    "price_change",
    "volume_shares",
    "trade_value",
    "transactions",
    "ex_rights_marker",
)

# 抓取時間允許的時鐘誤差；超過即視為未來時間拒絕。
FETCHED_AT_FUTURE_TOLERANCE = timedelta(minutes=5)

DAILY_PRICE_VERSIONS_SCHEMA_VERSION = 10


def _same_value(stored: object, incoming: object) -> bool:
    if stored is None or incoming is None:
        return stored is None and incoming is None
    return str(stored) == str(incoming)


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

CREATE TABLE IF NOT EXISTS daily_prices (%s);
%s
INSERT OR IGNORE INTO schema_versions(version) VALUES (1);
""" % (DAILY_PRICES_COLUMNS_SQL, DAILY_PRICES_INDEXES_SQL)

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
                WHEN 'YAHOO_FINANCE' THEN 1
                WHEN 'TPEX_TRADING_STOCK' THEN 2
                WHEN 'TWSE_STOCK_DAY' THEN 2
                WHEN 'TWSE_STOCK_DAY_ALL' THEN 3
                WHEN 'TPEX_MAINBOARD_QUOTES' THEN 3
                ELSE 9
            END,
            daily_prices.fetched_at DESC
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
            if "ex_rights_marker" not in columns:
                # NULL 表示來源未提供除權息標記，不得視為「非除權息日」。
                connection.execute(
                    "ALTER TABLE daily_prices ADD COLUMN ex_rights_marker INTEGER"
                )
            self.migrate_daily_price_versions(connection)
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
    def daily_prices_versioned(connection: sqlite3.Connection) -> bool:
        """daily_prices 主鍵是否已含 fetched_at（append-only 版本表）。"""
        info = connection.execute("PRAGMA table_info(daily_prices)").fetchall()
        return any(row["name"] == "fetched_at" and int(row["pk"]) > 0 for row in info)

    @classmethod
    def migrate_daily_price_versions(cls, connection: sqlite3.Connection) -> int:
        """把舊的「每檔每日每來源一列、原地覆寫」結構改為 append-only 版本表。

        既有每一列成為該（symbol, trade_date, source）的第一個版本，fetched_at 正規化為
        UTC ISO 字串；缺時區的 fetched_at 直接失敗並回滾，不猜測。整個重建在同一交易內
        完成，可重複執行：已遷移時只補記 schema 版本號並回傳 0。回傳搬移的列數。
        """
        if cls.daily_prices_versioned(connection):
            connection.execute(
                "INSERT OR IGNORE INTO schema_versions(version) VALUES (?)",
                (DAILY_PRICE_VERSIONS_SCHEMA_VERSION,),
            )
            return 0
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP VIEW IF EXISTS analysis_daily_prices")
        connection.execute("DROP TABLE IF EXISTS daily_prices_versioned")
        connection.execute(
            "CREATE TABLE daily_prices_versioned (%s)" % DAILY_PRICES_COLUMNS_SQL
        )
        legacy_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(daily_prices)").fetchall()
        }
        rows = connection.execute(
            "SELECT * FROM daily_prices ORDER BY symbol, trade_date, source"
        ).fetchall()
        moved = []
        for row in rows:
            fetched_at = parse_aware_time(row["fetched_at"], "daily_prices.fetched_at").isoformat()
            moved.append(
                (
                    row["symbol"],
                    row["trade_date"],
                    row["open_price"],
                    row["high_price"],
                    row["low_price"],
                    row["close_price"],
                    row["adjusted_close_price"] if "adjusted_close_price" in legacy_columns else None,
                    row["price_change"],
                    row["volume_shares"],
                    row["trade_value"],
                    row["transactions"],
                    row["source"],
                    fetched_at,
                    row["run_id"],
                    row["raw_payload_id"],
                    row["ex_rights_marker"] if "ex_rights_marker" in legacy_columns else None,
                )
            )
        connection.executemany(
            """
            INSERT INTO daily_prices_versioned(
                symbol, trade_date, open_price, high_price, low_price, close_price,
                adjusted_close_price, price_change, volume_shares, trade_value, transactions,
                source, fetched_at, run_id, raw_payload_id, ex_rights_marker
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            moved,
        )
        copied = int(
            connection.execute("SELECT COUNT(*) FROM daily_prices_versioned").fetchone()[0]
        )
        if copied != len(rows):
            raise ValueError("daily_prices 遷移列數不符：%d → %d" % (len(rows), copied))
        connection.execute("DROP TABLE daily_prices")
        connection.execute("ALTER TABLE daily_prices_versioned RENAME TO daily_prices")
        for statement in DAILY_PRICES_INDEXES_SQL.strip().split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            "INSERT OR IGNORE INTO schema_versions(version) VALUES (?)",
            (DAILY_PRICE_VERSIONS_SCHEMA_VERSION,),
        )
        return copied

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
    def price_content(price: DailyPrice) -> Dict[str, object]:
        """版本比對用的內容欄位，與資料表保存格式一致。"""
        return {
            "open_price": str(price.open_price) if price.open_price is not None else None,
            "high_price": str(price.high_price) if price.high_price is not None else None,
            "low_price": str(price.low_price) if price.low_price is not None else None,
            "close_price": str(price.close_price),
            "adjusted_close_price": (
                str(price.adjusted_close) if price.adjusted_close is not None else None
            ),
            "price_change": str(price.change) if price.change is not None else None,
            "volume_shares": int(price.volume_shares),
            "trade_value": int(price.trade_value),
            "transactions": int(price.transactions),
            "ex_rights_marker": None if price.ex_rights is None else int(price.ex_rights),
        }

    @classmethod
    def store_prices(
        cls,
        connection: sqlite3.Connection,
        prices: Iterable[DailyPrice],
        source: str,
        fetched_at: str,
        run_id: str,
        raw_payload_id: int,
    ) -> int:
        """append-only 寫入日線版本；回傳本次處理（新增或確認未變）的列數。

        - 同一（symbol, trade_date, source）內容與最新版本相同時不新增版本；但非官方來源
          的最新版本若在該日定案時間前抓取、本次已在定案後抓取，仍新增版本以記錄定案。
        - 內容不同時新增版本，且 fetched_at 必須晚於最新版本（版本依抓取時間遞增）。
        - fetched_at 缺時區、是未來時間或早於 trade_date 當天一律拒絕，不寫入任何列。
        """
        fetched = parse_aware_time(fetched_at, "fetched_at")
        now = datetime.now(timezone.utc)
        if fetched > now + FETCHED_AT_FUTURE_TOLERANCE:
            raise ValueError("fetched_at %s 晚於現在時間，拒絕寫入日線" % fetched_at)
        fetched_iso = fetched.isoformat()
        rows = list(prices)
        for price in rows:
            content = cls.price_content(price)
            try:
                trade_day = date.fromisoformat(str(price.trade_date))
            except ValueError as error:
                raise ValueError("trade_date 無法解析：%s" % price.trade_date) from error
            day_start = datetime.combine(trade_day, datetime.min.time(), tzinfo=TAIPEI_TIMEZONE)
            if fetched < day_start:
                raise ValueError(
                    "%s %s 的 fetched_at %s 早於交易日，拒絕寫入"
                    % (price.symbol, price.trade_date, fetched_at)
                )
            latest = connection.execute(
                """
                SELECT %s, fetched_at FROM daily_prices
                WHERE symbol = ? AND trade_date = ? AND source = ?
                ORDER BY fetched_at DESC LIMIT 1
                """
                % ", ".join(PRICE_CONTENT_FIELDS),
                (price.symbol, price.trade_date, source),
            ).fetchone()
            if latest is not None:
                latest_fetched = parse_aware_time(latest["fetched_at"], "daily_prices.fetched_at")
                unchanged = all(
                    _same_value(latest[field], content[field]) for field in PRICE_CONTENT_FIELDS
                )
                if unchanged:
                    finalized_at = price_finalized_at(price.trade_date)
                    confirms_finality = (
                        requires_finalized_fetch(source)
                        and latest_fetched < finalized_at <= fetched
                    )
                    if not confirms_finality:
                        continue
                if fetched <= latest_fetched:
                    raise ValueError(
                        "%s %s %s 已有 %s 抓取的版本，內容不同的 %s 不得寫入（版本必須依抓取時間遞增）"
                        % (price.symbol, price.trade_date, source, latest["fetched_at"], fetched_iso)
                    )
            connection.execute(
                """
                INSERT INTO daily_prices(
                    symbol, trade_date, open_price, high_price, low_price, close_price,
                    adjusted_close_price, price_change, volume_shares, trade_value, transactions,
                    source, fetched_at, run_id, raw_payload_id, ex_rights_marker
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    price.symbol,
                    price.trade_date,
                    content["open_price"],
                    content["high_price"],
                    content["low_price"],
                    content["close_price"],
                    content["adjusted_close_price"],
                    content["price_change"],
                    content["volume_shares"],
                    content["trade_value"],
                    content["transactions"],
                    source,
                    fetched_iso,
                    run_id,
                    raw_payload_id,
                    content["ex_rights_marker"],
                ),
            )
        return len(rows)

    # 舊名稱；語意已是 append-only 版本寫入。
    upsert_prices = store_prices

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

    def latest_reported_trade_date(self, sources: Sequence[str]) -> Optional[str]:
        """指定來源中任一來源已有的最新交易日；全部來源都沒有資料時回傳 None。

        與 ``latest_complete_trade_date`` 不同，不要求所有來源都到齊：證交所全市場當日
        行情端點常晚一天更新，但只要另一市場的當日行情已公布，該日必為已收盤交易日。
        """
        required = tuple(dict.fromkeys(sources))
        if not required:
            raise ValueError("至少需要一個行情來源")
        placeholders = ", ".join("?" for _ in required)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(trade_date) AS last_date FROM daily_prices WHERE source IN (%s)" % placeholders,
                required,
            ).fetchone()
        return str(row["last_date"]) if row and row["last_date"] else None

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
                       MAX(trade_date) AS last_date,
                       COUNT(DISTINCT trade_date) AS row_count
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
