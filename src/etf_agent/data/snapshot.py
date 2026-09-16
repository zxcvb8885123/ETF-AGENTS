import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .database import MarketDataDatabase


@dataclass(frozen=True)
class ResearchSnapshot:
    snapshot_id: str
    decision_cutoff: str
    universe_version: str
    created_at: str
    usable: bool
    quality_flags: List[str]
    latest_trade_date: Optional[str]
    universe_size: int
    latest_price_symbols: int
    documents: List[Dict[str, object]]

    def as_dict(self) -> Dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "decision_cutoff": self.decision_cutoff,
            "universe_version": self.universe_version,
            "created_at": self.created_at,
            "usable": self.usable,
            "quality_flags": self.quality_flags,
            "latest_trade_date": self.latest_trade_date,
            "universe_size": self.universe_size,
            "latest_price_symbols": self.latest_price_symbols,
            "documents": self.documents,
        }


class DataAgentService:
    def __init__(self, database: MarketDataDatabase):
        self.database = database

    def build_snapshot(
        self,
        decision_cutoff: str,
        run_id: Optional[str] = None,
        require_prices: bool = True,
    ) -> ResearchSnapshot:
        cutoff = _normalize_cutoff(decision_cutoff)
        self.database.initialize()
        snapshot_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        with self.database.connect() as connection:
            instruments = connection.execute(
                """
                SELECT symbol, source_date FROM instruments
                WHERE in_competition_universe = 1 ORDER BY symbol
                """
            ).fetchall()
            universe_version = _universe_version(instruments)
            latest_trade_row = connection.execute(
                "SELECT MAX(trade_date) AS value FROM analysis_daily_prices "
                "WHERE trade_date <= ?",
                (cutoff[:10],),
            ).fetchone()
            latest_trade_date = (
                str(latest_trade_row["value"])
                if latest_trade_row and latest_trade_row["value"]
                else None
            )
            latest_price_symbols = 0
            if latest_trade_date:
                latest_price_symbols = int(
                    connection.execute(
                        """
                        SELECT COUNT(DISTINCT symbol) AS value
                        FROM analysis_daily_prices WHERE trade_date = ?
                        """,
                        (latest_trade_date,),
                    ).fetchone()["value"]
                )
            rows = connection.execute(
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
                       monthly_revenues.note
                FROM eligible
                JOIN document_instruments
                  ON document_instruments.document_id = eligible.id
                LEFT JOIN monthly_revenues
                  ON monthly_revenues.document_id = eligible.id
                WHERE version_rank = 1
                ORDER BY published_at, eligible.id
                """,
                (cutoff, cutoff),
            ).fetchall()

            quality_flags: List[str] = []
            if not instruments:
                quality_flags.append("EMPTY_COMPETITION_UNIVERSE")
            if require_prices and not latest_trade_date:
                quality_flags.append("MISSING_PRICE_DATA")
            elif require_prices and latest_price_symbols < len(instruments):
                quality_flags.append(
                    "INCOMPLETE_PRICE_COVERAGE_%d_OF_%d"
                    % (latest_price_symbols, len(instruments))
                )
            usable = not quality_flags
            documents = [_row_to_document(row) for row in rows]
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
                    cutoff,
                    universe_version,
                    created_at,
                    int(usable),
                    json.dumps(quality_flags, ensure_ascii=False),
                ),
            )
            connection.executemany(
                "INSERT INTO snapshot_documents(snapshot_id, document_id) VALUES (?, ?)",
                [(snapshot_id, int(row["id"])) for row in rows],
            )

        return ResearchSnapshot(
            snapshot_id=snapshot_id,
            decision_cutoff=cutoff,
            universe_version=universe_version,
            created_at=created_at,
            usable=usable,
            quality_flags=quality_flags,
            latest_trade_date=latest_trade_date,
            universe_size=len(instruments),
            latest_price_symbols=latest_price_symbols,
            documents=documents,
        )


def _normalize_cutoff(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("decision_cutoff 必須包含時區")
    return parsed.astimezone(timezone.utc).isoformat()


def _universe_version(rows) -> str:
    content = "\n".join(
        "%s|%s" % (str(row["symbol"]), str(row["source_date"])) for row in rows
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _row_to_document(row) -> Dict[str, object]:
    document: Dict[str, object] = {
        "document_id": int(row["id"]),
        "source": str(row["source"]),
        "external_id": str(row["external_id"]),
        "version": int(row["version"]),
        "document_type": str(row["document_type"]),
        "symbol": str(row["symbol"]),
        "title": str(row["title"]),
        "body": str(row["body"]),
        "source_url": str(row["source_url"]),
        "published_at": str(row["published_at"]),
        "available_at": str(row["available_at"]),
        "content_sha256": str(row["content_sha256"]),
    }
    if row["revenue_period"] is not None:
        document["monthly_revenue"] = {
            "revenue_period": row["revenue_period"],
            "currency": row["currency"],
            "unit_multiplier": row["unit_multiplier"],
            "current_revenue": row["current_revenue"],
            "previous_month_revenue": row["previous_month_revenue"],
            "previous_year_revenue": row["previous_year_revenue"],
            "mom_pct": row["mom_pct"],
            "yoy_pct": row["yoy_pct"],
            "cumulative_revenue": row["cumulative_revenue"],
            "previous_year_cumulative_revenue": row[
                "previous_year_cumulative_revenue"
            ],
            "cumulative_yoy_pct": row["cumulative_yoy_pct"],
            "note": row["note"],
        }
    return document
