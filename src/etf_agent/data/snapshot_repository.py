"""ResearchSnapshot 的 SQLite 存取物件。"""

import json
import sqlite3
from typing import Sequence

from .database import MarketDataDatabase


class SnapshotRepository:
    """集中管理快照查詢與不可變版本關聯。"""

    def __init__(self, database: MarketDataDatabase):
        self.database = database

    def initialize(self) -> None:
        self.database.initialize()

    def connect(self):
        return self.database.connect()

    @staticmethod
    def load_universe(connection: sqlite3.Connection):
        return connection.execute(
            """
            SELECT symbol, source_date FROM instruments
            WHERE in_competition_universe = 1 ORDER BY symbol
            """
        ).fetchall()

    @staticmethod
    def load_validation(connection: sqlite3.Connection, cutoff: str):
        return connection.execute(
            """
            SELECT validation_id, universe_version, status, usable,
                   tradable_count, not_tradable_count, mismatch_count
            FROM universe_validation_runs
            WHERE generated_at <= ?
            ORDER BY generated_at DESC LIMIT 1
            """,
            (cutoff,),
        ).fetchone()

    @staticmethod
    def find_latest_trade_date(
        connection: sqlite3.Connection,
        price_date_limit: str,
        cutoff: str,
    ):
        row = connection.execute(
            """
            WITH universe AS (
                SELECT COUNT(*) AS size
                FROM instruments
                WHERE in_competition_universe = 1
            ), covered_dates AS (
                SELECT daily_prices.trade_date,
                       COUNT(DISTINCT daily_prices.symbol) AS symbol_count
                FROM daily_prices
                JOIN instruments USING(symbol)
                WHERE instruments.in_competition_universe = 1
                  AND daily_prices.trade_date <= ?
                  AND daily_prices.fetched_at <= ?
                GROUP BY daily_prices.trade_date
            )
            SELECT MAX(trade_date) AS value
            FROM covered_dates
            WHERE symbol_count = (SELECT size FROM universe)
            """,
            (price_date_limit, cutoff),
        ).fetchone()
        if row and row["value"]:
            return str(row["value"])

        row = connection.execute(
            """
            SELECT MAX(daily_prices.trade_date) AS value
            FROM daily_prices
            JOIN instruments USING(symbol)
            WHERE instruments.in_competition_universe = 1
              AND daily_prices.trade_date <= ?
              AND daily_prices.fetched_at <= ?
            """,
            (price_date_limit, cutoff),
        ).fetchone()
        return str(row["value"]) if row and row["value"] else None

    @staticmethod
    def load_prices(
        connection: sqlite3.Connection,
        trade_date: str,
        cutoff: str,
    ):
        return connection.execute(
            """
            WITH ranked AS (
                SELECT daily_prices.*, instruments.code,
                       ROW_NUMBER() OVER (
                           PARTITION BY daily_prices.symbol, daily_prices.trade_date
                           ORDER BY CASE daily_prices.source
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
                  AND daily_prices.trade_date = ?
                  AND daily_prices.fetched_at <= ?
            )
            SELECT ranked.*, raw_payloads.endpoint,
                   raw_payloads.sha256 AS raw_sha256,
                   raw_payloads.fetched_at AS payload_fetched_at
            FROM ranked
            JOIN raw_payloads ON raw_payloads.id = ranked.raw_payload_id
            WHERE source_rank = 1
            ORDER BY ranked.symbol
            """,
            (trade_date, cutoff),
        ).fetchall()

    @staticmethod
    def load_documents(connection: sqlite3.Connection, cutoff: str):
        return connection.execute(
            """
            WITH eligible AS (
                SELECT source_documents.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY source, external_id
                           ORDER BY version DESC
                       ) AS version_rank
                FROM source_documents
                WHERE published_at <= ? AND available_at <= ?
            )
            SELECT eligible.*, document_instruments.symbol,
                   raw_payloads.endpoint AS raw_endpoint,
                   raw_payloads.sha256 AS raw_sha256,
                   raw_payloads.fetched_at AS payload_fetched_at,
                   monthly_revenues.revenue_period,
                   monthly_revenues.currency,
                   monthly_revenues.unit_multiplier,
                   monthly_revenues.current_revenue,
                   monthly_revenues.previous_month_revenue,
                   monthly_revenues.previous_year_revenue,
                   monthly_revenues.mom_pct,
                   monthly_revenues.yoy_pct,
                   monthly_revenues.cumulative_revenue,
                   monthly_revenues.previous_year_cumulative_revenue,
                   monthly_revenues.cumulative_yoy_pct,
                   monthly_revenues.note,
                   financial_statements.statement_type,
                   financial_statements.industry AS financial_industry,
                   financial_statements.fiscal_year,
                   financial_statements.fiscal_quarter,
                   financial_statements.period_start AS financial_period_start,
                   financial_statements.period_end AS financial_period_end,
                   financial_statements.period_kind AS financial_period_kind,
                   financial_statements.reporting_scope,
                   financial_statements.currency AS financial_currency,
                   financial_statements.unit_multiplier AS financial_unit_multiplier,
                   financial_statements.reported_at,
                   financial_statements.source_published_at,
                   financial_statements.mapping_version,
                   financial_statements.facts_json
            FROM eligible
            JOIN document_instruments
              ON document_instruments.document_id = eligible.id
            JOIN raw_payloads
              ON raw_payloads.id = eligible.raw_payload_id
            LEFT JOIN monthly_revenues
              ON monthly_revenues.document_id = eligible.id
            LEFT JOIN financial_statements
              ON financial_statements.document_id = eligible.id
            WHERE version_rank = 1
            ORDER BY published_at, eligible.id
            """,
            (cutoff, cutoff),
        ).fetchall()

    @staticmethod
    def save_snapshot(
        connection: sqlite3.Connection,
        *,
        snapshot_id: str,
        run_id,
        decision_cutoff: str,
        universe_version: str,
        created_at: str,
        usable: bool,
        quality_flags: Sequence[str],
        price_rows,
        document_rows,
    ) -> None:
        connection.execute(
            """
            INSERT INTO research_snapshots(
                id, run_id, decision_cutoff, universe_version,
                created_at, usable, quality_flags_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                run_id,
                decision_cutoff,
                universe_version,
                created_at,
                int(usable),
                json.dumps(list(quality_flags), ensure_ascii=False),
            ),
        )
        connection.executemany(
            "INSERT INTO snapshot_documents(snapshot_id, document_id) VALUES (?, ?)",
            [(snapshot_id, int(row["id"])) for row in document_rows],
        )
        connection.executemany(
            """
            INSERT INTO snapshot_prices(
                snapshot_id, symbol, trade_date, source, raw_payload_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot_id,
                    str(row["symbol"]),
                    str(row["trade_date"]),
                    str(row["source"]),
                    int(row["raw_payload_id"]),
                )
                for row in price_rows
            ],
        )
