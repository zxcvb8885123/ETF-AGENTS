"""ResearchSnapshot 的 SQLite 存取物件。"""

import bisect
import json
import sqlite3
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from .database import MarketDataDatabase
from .price_finality import finalized_rows_sql
from .yfinance_history import BASIS_TOLERANCE


# 研究日線同日優先 Yahoo；只有該股該日缺 Yahoo 時才改用官方價，不因其他日期有 Yahoo 而丟棄。
# 官方原價仍獨立保留給帳本結算與交叉核對。
_SOURCE_PRIORITY_SQL = """CASE daily_prices.source
                               WHEN 'YAHOO_FINANCE' THEN 1
                               WHEN 'TPEX_TRADING_STOCK' THEN 2
                               WHEN 'TWSE_STOCK_DAY' THEN 2
                               WHEN 'TWSE_STOCK_DAY_ALL' THEN 3
                               WHEN 'TPEX_MAINBOARD_QUOTES' THEN 3
                               ELSE 9
                           END"""

# daily_prices 是 append-only 版本表：同一（symbol, trade_date, source）可能有多個版本，
# 所有查詢都只看 fetched_at ≤ cutoff 的列，並在來源優先序之後以 fetched_at DESC 取最新版本。
_VERSION_ORDER_SQL = "%s, daily_prices.fetched_at DESC" % _SOURCE_PRIORITY_SQL

# 非官方來源（Yahoo）盤中就有當日日線，只有在該日定案時間後抓到的版本才能進 Snapshot。
_FINALIZED_SQL = finalized_rows_sql("daily_prices")

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
                  AND %s
                GROUP BY daily_prices.trade_date
            )
            SELECT MAX(trade_date) AS value
            FROM covered_dates
            WHERE symbol_count = (SELECT size FROM universe)
            """ % _FINALIZED_SQL,
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
              AND %s
            """ % _FINALIZED_SQL,
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
                           ORDER BY %s
                       ) AS source_rank
                FROM daily_prices
                JOIN instruments USING(symbol)
                WHERE instruments.in_competition_universe = 1
                  AND daily_prices.trade_date = ?
                  AND daily_prices.fetched_at <= ?
                  AND %s
            ), official AS (
                -- 決策與帳本用價：同日官方原價（不含 Yahoo，其 Close 可能被事後調整）。
                SELECT daily_prices.symbol, daily_prices.close_price,
                       daily_prices.source, daily_prices.raw_payload_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY daily_prices.symbol ORDER BY %s
                       ) AS official_rank
                FROM daily_prices
                WHERE daily_prices.trade_date = ?
                  AND daily_prices.fetched_at <= ?
                  AND daily_prices.source != 'YAHOO_FINANCE'
            )
            SELECT ranked.*, raw_payloads.endpoint,
                   raw_payloads.sha256 AS raw_sha256,
                   raw_payloads.fetched_at AS payload_fetched_at,
                   official.close_price AS execution_close_price,
                   official.source AS execution_source,
                   official.raw_payload_id AS execution_raw_payload_id,
                   execution_payloads.endpoint AS execution_endpoint,
                   execution_payloads.sha256 AS execution_raw_sha256,
                   execution_payloads.fetched_at AS execution_payload_fetched_at
            FROM ranked
            JOIN raw_payloads ON raw_payloads.id = ranked.raw_payload_id
            LEFT JOIN official ON official.symbol = ranked.symbol AND official.official_rank = 1
            LEFT JOIN raw_payloads AS execution_payloads
                   ON execution_payloads.id = official.raw_payload_id
            WHERE source_rank = 1
            ORDER BY ranked.symbol
            """ % (_VERSION_ORDER_SQL, _FINALIZED_SQL, _VERSION_ORDER_SQL),
            (trade_date, cutoff, trade_date, cutoff),
        ).fetchall()

    @staticmethod
    def load_price_history(
        connection: sqlite3.Connection,
        before_date: str,
        cutoff: str,
        bars_per_symbol: int,
    ) -> List[Dict[str, object]]:
        """每檔取 before_date 前最近 N 個交易日，來源優先序與 load_prices 相同。

        Yahoo 缺某日而改用官方原價時，依相鄰 Yahoo 列的還原係數換算到同一基準；
        無法確定係數的列排除，不以原價混入還原序列。
        """
        rows = connection.execute(
            """
            WITH ranked AS (
                SELECT daily_prices.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY daily_prices.symbol, daily_prices.trade_date
                           ORDER BY %s
                       ) AS source_rank
                FROM daily_prices
                JOIN instruments USING(symbol)
                WHERE instruments.in_competition_universe = 1
                  AND daily_prices.trade_date < ?
                  AND daily_prices.fetched_at <= ?
                  AND %s
            ), recent AS (
                SELECT ranked.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY symbol ORDER BY trade_date DESC
                       ) AS recency
                FROM ranked
                WHERE source_rank = 1
            )
            SELECT symbol, trade_date, open_price, high_price, low_price,
                   close_price, adjusted_close_price, volume_shares, source,
                   fetched_at
            FROM recent
            WHERE recency <= ?
            ORDER BY symbol, trade_date
            """ % (_VERSION_ORDER_SQL, _FINALIZED_SQL),
            (before_date, cutoff, bars_per_symbol),
        ).fetchall()
        return _rebase_official_gaps(connection, [dict(row) for row in rows], before_date, cutoff)

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


_RATIO_QUANTUM = Decimal("0.0001")


def _basis_factor(
    trade_date: str,
    dates: Sequence[str],
    ratios: Sequence[Decimal],
    markers: Dict[str, Optional[int]],
    latest_date: str,
) -> Optional[Decimal]:
    """以缺口前後最近的 Yahoo 還原係數（Yahoo adj ÷ 同日官方原價）推得該日係數；無法確定時回傳 None。

    係數只在除權息日改變。前後係數相同代表其間沒有除權息，直接沿用；缺口之後
    沒有 Yahoo 列時，以最新一日（係數 1）作為後端。前後係數不同時，以官方漲跌
    欄的除權息標記定位除權息日 E：E 不晚於缺口日則用後端係數，否則用前端係數。
    區間內官方列缺標記、找不到或有多個除權息日時無法確定，回傳 None。
    """
    index = bisect.bisect_left(dates, trade_date)
    if index < len(dates):
        following_date, following = dates[index], ratios[index]
    else:
        following_date, following = latest_date, Decimal(1)
    if index == 0:
        return following
    previous_date, previous = dates[index - 1], ratios[index - 1]
    if abs(previous - following) <= BASIS_TOLERANCE * max(abs(previous), abs(following)):
        return following
    window = {day: flag for day, flag in markers.items() if previous_date < day <= following_date}
    if following_date not in window or any(flag is None for flag in window.values()):
        return None
    ex_dates = [day for day, flag in window.items() if flag]
    if len(ex_dates) != 1:
        return None
    return following if ex_dates[0] <= trade_date else previous


def _rebase_official_gaps(
    connection: sqlite3.Connection,
    rows: List[Dict[str, object]],
    before_date: str,
    cutoff: str,
) -> List[Dict[str, object]]:
    gap_symbols = sorted({
        str(row["symbol"]) for row in rows if row["source"] != "YAHOO_FINANCE"
    })
    if not gap_symbols:
        return rows
    placeholders = ",".join("?" for _ in gap_symbols)
    # Yahoo Close 遇配股、分割、減資會被事後改寫，不是交易所原價；還原係數必須以
    # 同日官方原價為分母（Yahoo adj ÷ 官方 close）。同日沒有官方原價的 Yahoo 列
    # 無法確認基準，不作為參考。同日多個官方來源時以來源優先序最高者為準。
    # 版本表：每個（symbol, trade_date, source）只取 cutoff 前最新版本；來源優先序低者先寫、
    # 高者後寫覆蓋，同來源舊版本先寫、新版本後寫覆蓋。
    official_closes: Dict[Tuple[str, str], Decimal] = {}
    for row in connection.execute(
        """
        SELECT symbol, trade_date, close_price
        FROM daily_prices
        WHERE source != 'YAHOO_FINANCE' AND trade_date <= ? AND fetched_at <= ?
          AND symbol IN (%s)
        ORDER BY %s DESC, daily_prices.fetched_at ASC
        """ % (placeholders, _SOURCE_PRIORITY_SQL),
        (before_date, cutoff, *gap_symbols),
    ).fetchall():
        official_closes[(str(row["symbol"]), str(row["trade_date"]))] = Decimal(str(row["close_price"]))
    references: Dict[str, Tuple[List[str], List[Decimal]]] = {}
    for row in connection.execute(
        """
        WITH versions AS (
            SELECT symbol, trade_date, adjusted_close_price,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, trade_date ORDER BY fetched_at DESC
                   ) AS version_rank
            FROM daily_prices
            WHERE source = 'YAHOO_FINANCE'
              AND trade_date <= ? AND fetched_at <= ? AND %s
              AND symbol IN (%s)
        )
        SELECT symbol, trade_date, adjusted_close_price
        FROM versions
        WHERE version_rank = 1 AND adjusted_close_price IS NOT NULL
        ORDER BY symbol, trade_date
        """ % (_FINALIZED_SQL, placeholders),
        (before_date, cutoff, *gap_symbols),
    ).fetchall():
        dates, ratios = references.setdefault(str(row["symbol"]), ([], []))
        official = official_closes.get((str(row["symbol"]), str(row["trade_date"])))
        if official is None or official <= 0:
            continue
        dates.append(str(row["trade_date"]))
        ratios.append(Decimal(str(row["adjusted_close_price"])) / official)
    # 同日多個官方來源時，任一標記除權息即為除權息；全為 NULL 表示未知。
    markers: Dict[str, Dict[str, Optional[int]]] = {}
    for row in connection.execute(
        """
        WITH versions AS (
            SELECT symbol, trade_date, ex_rights_marker,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, trade_date, source ORDER BY fetched_at DESC
                   ) AS version_rank
            FROM daily_prices
            WHERE source != 'YAHOO_FINANCE' AND trade_date <= ? AND fetched_at <= ?
              AND symbol IN (%s)
        )
        SELECT symbol, trade_date, MAX(ex_rights_marker) AS marker
        FROM versions
        WHERE version_rank = 1
        GROUP BY symbol, trade_date
        """ % placeholders,
        (before_date, cutoff, *gap_symbols),
    ).fetchall():
        markers.setdefault(str(row["symbol"]), {})[str(row["trade_date"])] = row["marker"]
    result: List[Dict[str, object]] = []
    for row in rows:
        symbol = str(row["symbol"])
        reference = references.get(symbol)
        if row["source"] == "YAHOO_FINANCE" or reference is None:
            # 從未有 Yahoo 的股票整段都是官方原價，本身口徑一致。
            result.append(row)
            continue
        if not reference[0]:
            # 有 Yahoo 列但沒有任何可核對官方原價的參考：無法確定基準，排除。
            continue
        factor = _basis_factor(
            str(row["trade_date"]), *reference, markers.get(symbol, {}), before_date
        )
        if factor is None:
            continue
        adjusted = Decimal(str(row["close_price"])) * factor
        result.append({
            **row,
            "adjusted_close_price": str(adjusted.quantize(_RATIO_QUANTUM, ROUND_HALF_EVEN)),
        })
    return result
