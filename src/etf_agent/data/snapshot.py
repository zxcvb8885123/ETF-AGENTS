"""Data Agent 的物件化研究快照建構流程。"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from etf_agent.contracts import SourceEvidence

from .database import MarketDataDatabase
from .evidence import TAIPEI_TIMEZONE, SourceEvidenceBuilder
from .snapshot_repository import SnapshotRepository


@dataclass(frozen=True)
class SnapshotPrice:
    symbol: str
    code: str
    trade_date: str
    open_price: Optional[str]
    high_price: Optional[str]
    low_price: Optional[str]
    close_price: str
    analysis_close_price: str
    adjusted_close_price: Optional[str]
    price_change: Optional[str]
    volume_shares: int
    trade_value: int
    transactions: int
    source: str
    source_evidence_id: str

    @classmethod
    def from_row(cls, row, evidence_builder: SourceEvidenceBuilder):
        return cls(
            symbol=str(row["symbol"]),
            code=str(row["code"]),
            trade_date=str(row["trade_date"]),
            open_price=row["open_price"],
            high_price=row["high_price"],
            low_price=row["low_price"],
            close_price=str(row["close_price"]),
            analysis_close_price=str(
                row["adjusted_close_price"] or row["close_price"]
            ),
            adjusted_close_price=row["adjusted_close_price"],
            price_change=row["price_change"],
            volume_shares=int(row["volume_shares"]),
            trade_value=int(row["trade_value"]),
            transactions=int(row["transactions"]),
            source=str(row["source"]),
            source_evidence_id=evidence_builder.price_evidence_id(row),
        )

    def as_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "code": self.code,
            "trade_date": self.trade_date,
            "open_price": self.open_price,
            "high_price": self.high_price,
            "low_price": self.low_price,
            "close_price": self.close_price,
            "analysis_close_price": self.analysis_close_price,
            "adjusted_close_price": self.adjusted_close_price,
            "price_change": self.price_change,
            "volume_shares": self.volume_shares,
            "trade_value": self.trade_value,
            "transactions": self.transactions,
            "source": self.source,
            "source_evidence_id": self.source_evidence_id,
        }


@dataclass(frozen=True)
class SnapshotMonthlyRevenue:
    revenue_period: str
    currency: str
    unit_multiplier: int
    current_revenue: Optional[str]
    previous_month_revenue: Optional[str]
    previous_year_revenue: Optional[str]
    mom_pct: Optional[str]
    yoy_pct: Optional[str]
    cumulative_revenue: Optional[str]
    previous_year_cumulative_revenue: Optional[str]
    cumulative_yoy_pct: Optional[str]
    note: str

    @classmethod
    def from_row(cls, row):
        if row["revenue_period"] is None:
            return None
        return cls(
            revenue_period=str(row["revenue_period"]),
            currency=str(row["currency"]),
            unit_multiplier=int(row["unit_multiplier"]),
            current_revenue=row["current_revenue"],
            previous_month_revenue=row["previous_month_revenue"],
            previous_year_revenue=row["previous_year_revenue"],
            mom_pct=row["mom_pct"],
            yoy_pct=row["yoy_pct"],
            cumulative_revenue=row["cumulative_revenue"],
            previous_year_cumulative_revenue=row[
                "previous_year_cumulative_revenue"
            ],
            cumulative_yoy_pct=row["cumulative_yoy_pct"],
            note=str(row["note"]),
        )

    def as_dict(self) -> Dict[str, object]:
        return {
            "revenue_period": self.revenue_period,
            "currency": self.currency,
            "unit_multiplier": self.unit_multiplier,
            "current_revenue": self.current_revenue,
            "previous_month_revenue": self.previous_month_revenue,
            "previous_year_revenue": self.previous_year_revenue,
            "mom_pct": self.mom_pct,
            "yoy_pct": self.yoy_pct,
            "cumulative_revenue": self.cumulative_revenue,
            "previous_year_cumulative_revenue": self.previous_year_cumulative_revenue,
            "cumulative_yoy_pct": self.cumulative_yoy_pct,
            "note": self.note,
        }


@dataclass(frozen=True)
class SnapshotDocument:
    document_id: int
    source: str
    external_id: str
    version: int
    document_type: str
    symbol: str
    title: str
    body: str
    source_url: str
    published_at: str
    available_at: str
    content_sha256: str
    source_evidence_id: str
    monthly_revenue: Optional[SnapshotMonthlyRevenue] = None

    @classmethod
    def from_row(cls, row, evidence_builder: SourceEvidenceBuilder):
        return cls(
            document_id=int(row["id"]),
            source=str(row["source"]),
            external_id=str(row["external_id"]),
            version=int(row["version"]),
            document_type=str(row["document_type"]),
            symbol=str(row["symbol"]),
            title=str(row["title"]),
            body=str(row["body"]),
            source_url=str(row["source_url"]),
            published_at=str(row["published_at"]),
            available_at=str(row["available_at"]),
            content_sha256=str(row["content_sha256"]),
            source_evidence_id=evidence_builder.document_evidence_id(row),
            monthly_revenue=SnapshotMonthlyRevenue.from_row(row),
        )

    def as_dict(self) -> Dict[str, object]:
        result: Dict[str, object] = {
            "document_id": self.document_id,
            "source": self.source,
            "external_id": self.external_id,
            "version": self.version,
            "document_type": self.document_type,
            "symbol": self.symbol,
            "title": self.title,
            "body": self.body,
            "source_url": self.source_url,
            "published_at": self.published_at,
            "available_at": self.available_at,
            "content_sha256": self.content_sha256,
            "source_evidence_id": self.source_evidence_id,
        }
        if self.monthly_revenue is not None:
            result["monthly_revenue"] = self.monthly_revenue.as_dict()
        return result


@dataclass(frozen=True)
class UniverseValidationState:
    validation_id: str
    universe_version: str
    status: str
    usable: bool
    tradable_count: int
    not_tradable_count: int
    mismatch_count: int

    @classmethod
    def from_row(cls, row):
        if row is None:
            return None
        return cls(
            validation_id=str(row["validation_id"]),
            universe_version=str(row["universe_version"]),
            status=str(row["status"]),
            usable=bool(row["usable"]),
            tradable_count=int(row["tradable_count"]),
            not_tradable_count=int(row["not_tradable_count"]),
            mismatch_count=int(row["mismatch_count"]),
        )


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
    universe_validation_id: Optional[str]
    universe_validation_status: Optional[str]
    tradable_symbols: int
    not_tradable_symbols: int
    universe_mismatches: int
    latest_prices: List[SnapshotPrice]
    source_evidence: List[SourceEvidence]
    documents: List[SnapshotDocument]

    def as_dict(self) -> Dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "decision_cutoff": self.decision_cutoff,
            "universe_version": self.universe_version,
            "created_at": self.created_at,
            "usable": self.usable,
            "quality_flags": list(self.quality_flags),
            "latest_trade_date": self.latest_trade_date,
            "universe_size": self.universe_size,
            "latest_price_symbols": self.latest_price_symbols,
            "universe_validation_id": self.universe_validation_id,
            "universe_validation_status": self.universe_validation_status,
            "tradable_symbols": self.tradable_symbols,
            "not_tradable_symbols": self.not_tradable_symbols,
            "universe_mismatches": self.universe_mismatches,
            "latest_prices": [item.as_dict() for item in self.latest_prices],
            "source_evidence": [item.as_dict() for item in self.source_evidence],
            "documents": [item.as_dict() for item in self.documents],
        }


class SnapshotQualityPolicy:
    """集中計算正式快照的 fail-closed 品質旗標。"""

    def evaluate(
        self,
        *,
        universe_size: int,
        universe_version: str,
        validation: Optional[UniverseValidationState],
        latest_trade_date: Optional[str],
        latest_price_symbols: int,
        require_prices: bool,
        require_universe_validation: bool,
    ) -> List[str]:
        flags: List[str] = []
        if universe_size == 0:
            flags.append("EMPTY_COMPETITION_UNIVERSE")
        if require_universe_validation and validation is None:
            flags.append("MISSING_UNIVERSE_VALIDATION")
        elif (
            require_universe_validation
            and validation is not None
            and validation.universe_version != universe_version
        ):
            flags.append("STALE_UNIVERSE_VALIDATION")
        elif (
            require_universe_validation
            and validation is not None
            and not validation.usable
        ):
            flags.append("UNUSABLE_UNIVERSE_VALIDATION_%s" % validation.validation_id)
        if require_prices and not latest_trade_date:
            flags.append("MISSING_PRICE_DATA")
        elif require_prices and latest_price_symbols < universe_size:
            flags.append(
                "INCOMPLETE_PRICE_COVERAGE_%d_OF_%d"
                % (latest_price_symbols, universe_size)
            )
        return flags


class SnapshotBuilder:
    """協調 Repository、Evidence Builder 與品質政策來建立快照。"""

    def __init__(
        self,
        repository: SnapshotRepository,
        evidence_builder: Optional[SourceEvidenceBuilder] = None,
        quality_policy: Optional[SnapshotQualityPolicy] = None,
    ):
        self.repository = repository
        self.evidence_builder = evidence_builder or SourceEvidenceBuilder()
        self.quality_policy = quality_policy or SnapshotQualityPolicy()

    def build(
        self,
        decision_cutoff: str,
        run_id: Optional[str] = None,
        require_prices: bool = True,
        require_universe_validation: bool = True,
    ) -> ResearchSnapshot:
        cutoff = _normalize_cutoff(decision_cutoff)
        snapshot_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        self.repository.initialize()

        with self.repository.connect() as connection:
            instruments = self.repository.load_universe(connection)
            universe_version = _universe_version(instruments)
            validation = UniverseValidationState.from_row(
                self.repository.load_validation(connection, cutoff)
            )
            latest_trade_date = self.repository.find_latest_trade_date(
                connection,
                _price_date_limit(cutoff),
                cutoff,
            )
            price_rows = (
                self.repository.load_prices(connection, latest_trade_date, cutoff)
                if latest_trade_date
                else []
            )
            document_rows = self.repository.load_documents(connection, cutoff)
            prices = [
                SnapshotPrice.from_row(row, self.evidence_builder)
                for row in price_rows
            ]
            documents = [
                SnapshotDocument.from_row(row, self.evidence_builder)
                for row in document_rows
            ]
            source_evidence = self.evidence_builder.build_for_prices(price_rows)
            source_evidence.extend(
                self.evidence_builder.build_for_document(row)
                for row in document_rows
            )
            quality_flags = self.quality_policy.evaluate(
                universe_size=len(instruments),
                universe_version=universe_version,
                validation=validation,
                latest_trade_date=latest_trade_date,
                latest_price_symbols=len(prices),
                require_prices=require_prices,
                require_universe_validation=require_universe_validation,
            )
            snapshot = ResearchSnapshot(
                snapshot_id=snapshot_id,
                decision_cutoff=cutoff,
                universe_version=universe_version,
                created_at=created_at,
                usable=not quality_flags,
                quality_flags=quality_flags,
                latest_trade_date=latest_trade_date,
                universe_size=len(instruments),
                latest_price_symbols=len(prices),
                universe_validation_id=(
                    validation.validation_id if validation is not None else None
                ),
                universe_validation_status=(
                    validation.status if validation is not None else None
                ),
                tradable_symbols=(
                    validation.tradable_count if validation is not None else 0
                ),
                not_tradable_symbols=(
                    validation.not_tradable_count if validation is not None else 0
                ),
                universe_mismatches=(
                    validation.mismatch_count if validation is not None else 0
                ),
                latest_prices=prices,
                source_evidence=source_evidence,
                documents=documents,
            )
            self.repository.save_snapshot(
                connection,
                snapshot_id=snapshot.snapshot_id,
                run_id=run_id,
                decision_cutoff=snapshot.decision_cutoff,
                universe_version=snapshot.universe_version,
                created_at=snapshot.created_at,
                usable=snapshot.usable,
                quality_flags=snapshot.quality_flags,
                price_rows=price_rows,
                document_rows=document_rows,
            )
        return snapshot


class DataAgentService:
    """供 CLI 與其他 Agent 呼叫的穩定 Data Agent 外觀物件。"""

    def __init__(
        self,
        database: MarketDataDatabase,
        snapshot_builder: Optional[SnapshotBuilder] = None,
    ):
        self.database = database
        self.snapshot_builder = snapshot_builder or SnapshotBuilder(
            SnapshotRepository(database)
        )

    def build_snapshot(
        self,
        decision_cutoff: str,
        run_id: Optional[str] = None,
        require_prices: bool = True,
        require_universe_validation: bool = True,
    ) -> ResearchSnapshot:
        return self.snapshot_builder.build(
            decision_cutoff,
            run_id=run_id,
            require_prices=require_prices,
            require_universe_validation=require_universe_validation,
        )


def _normalize_cutoff(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("decision_cutoff 必須包含時區")
    return parsed.astimezone(timezone.utc).isoformat()


def _price_date_limit(cutoff: str) -> str:
    local_cutoff = datetime.fromisoformat(cutoff).astimezone(TAIPEI_TIMEZONE)
    price_date = local_cutoff.date()
    if local_cutoff.time() < time(hour=13, minute=30):
        price_date -= timedelta(days=1)
    return price_date.isoformat()


def _universe_version(rows: Sequence[object]) -> str:
    content = "\n".join(
        "%s|%s" % (str(row["symbol"]), str(row["source_date"])) for row in rows
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
