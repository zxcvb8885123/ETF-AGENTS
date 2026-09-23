"""Stable JSON contracts shared by Data Agent tools and snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


SOURCE_PROBE_STATUSES = {"passed", "failed"}
REPORT_STATUSES = {"passed", "degraded", "failed"}
INSTRUMENT_STATUSES = {"tradable", "not_tradable", "universe_mismatch"}
VALIDATION_STATUSES = {"completed", "degraded", "failed"}
SOURCE_AUTHORITIES = {
    "twse",
    "tpex",
    "taifex",
    "mops",
    "fininst",
    "media",
    "vendor",
    "other",
}


@dataclass(frozen=True)
class SourceEvidence:
    """Auditable source metadata consumed by downstream report builders.

    ``evidence_id`` is an internal stable reference.  A D-Plan builder assigns
    the submission-only ``S1``/``S2`` identifiers after selecting the sources
    actually used by that report.
    """

    evidence_id: str
    source: str
    authority: str
    data_type: str
    url: str
    content_as_of: str
    fetched_at: str
    content_sha256: str
    raw_payload_id: int
    published_at: Optional[str] = None
    archive_url: Optional[str] = None

    def __post_init__(self) -> None:
        if self.authority not in SOURCE_AUTHORITIES:
            raise ValueError("無效的來源權威分類：%s" % self.authority)
        if self.raw_payload_id <= 0:
            raise ValueError("raw_payload_id 必須為正整數")
        if not self.evidence_id or not self.url or not self.content_as_of:
            raise ValueError("來源證據缺少必要欄位")

    def as_dict(self) -> Dict[str, object]:
        result: Dict[str, object] = {
            "evidence_id": self.evidence_id,
            "source": self.source,
            "authority": self.authority,
            "data_type": self.data_type,
            "url": self.url,
            "content_as_of": self.content_as_of,
            "fetched_at": self.fetched_at,
            "content_sha256": self.content_sha256,
            "raw_payload_id": self.raw_payload_id,
        }
        if self.published_at is not None:
            result["published_at"] = self.published_at
        if self.archive_url is not None:
            result["archive_url"] = self.archive_url
        return result


@dataclass(frozen=True)
class SourceProbeResult:
    source_id: str
    market: str
    data_type: str
    url: str
    status: str
    fetched_at: str
    duration_ms: int
    http_status: Optional[int]
    format: str
    row_count: int
    data_date: Optional[str]
    schema_fields: List[str] = field(default_factory=list)
    observed_codes: List[str] = field(default_factory=list)
    tradable_codes: List[str] = field(default_factory=list)
    universe_total: int = 0
    universe_covered: int = 0
    missing_symbols: List[str] = field(default_factory=list)
    content_sha256: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in SOURCE_PROBE_STATUSES:
            raise ValueError("無效的來源探測狀態：%s" % self.status)
        if self.duration_ms < 0 or self.row_count < 0:
            raise ValueError("來源探測計數不得為負數")
        if self.universe_covered > self.universe_total:
            raise ValueError("來源覆蓋數不得大於交易池數")

    def as_dict(self) -> Dict[str, object]:
        return {
            "source_id": self.source_id,
            "market": self.market,
            "data_type": self.data_type,
            "url": self.url,
            "status": self.status,
            "fetched_at": self.fetched_at,
            "duration_ms": self.duration_ms,
            "http_status": self.http_status,
            "format": self.format,
            "row_count": self.row_count,
            "data_date": self.data_date,
            "schema_fields": list(self.schema_fields),
            "observed_codes": list(self.observed_codes),
            "tradable_codes": list(self.tradable_codes),
            "universe_total": self.universe_total,
            "universe_covered": self.universe_covered,
            "missing_symbols": list(self.missing_symbols),
            "content_sha256": self.content_sha256,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class SourceFeasibilityReport:
    schema_version: str
    report_id: str
    generated_at: str
    status: str
    probes: List[SourceProbeResult]
    errors: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in REPORT_STATUSES:
            raise ValueError("無效的來源報告狀態：%s" % self.status)

    def as_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "status": self.status,
            "probes": [probe.as_dict() for probe in self.probes],
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class UniverseInstrumentStatus:
    symbol: str
    code: str
    name: str
    market: str
    status: str
    source_id: str
    data_date: Optional[str]
    reason_code: str
    evidence: str
    successor_candidate: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in INSTRUMENT_STATUSES:
            raise ValueError("無效的交易池狀態：%s" % self.status)

    def as_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "code": self.code,
            "name": self.name,
            "market": self.market,
            "status": self.status,
            "source_id": self.source_id,
            "data_date": self.data_date,
            "reason_code": self.reason_code,
            "evidence": self.evidence,
            "successor_candidate": self.successor_candidate,
        }


@dataclass(frozen=True)
class UniverseValidationResult:
    schema_version: str
    validation_id: str
    source_report_id: str
    generated_at: str
    universe_version: str
    status: str
    usable: bool
    total_count: int
    tradable_count: int
    not_tradable_count: int
    mismatch_count: int
    instruments: List[UniverseInstrumentStatus]
    errors: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in VALIDATION_STATUSES:
            raise ValueError("無效的交易池驗證狀態：%s" % self.status)
        counted = self.tradable_count + self.not_tradable_count + self.mismatch_count
        if counted != self.total_count or len(self.instruments) != self.total_count:
            raise ValueError("交易池驗證統計與明細數量不一致")
        if self.usable and (
            self.status != "completed"
            or self.not_tradable_count
            or self.mismatch_count
            or self.errors
        ):
            raise ValueError("只有全部可交易且無錯誤的驗證結果可以標記 usable")

    def as_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "validation_id": self.validation_id,
            "source_report_id": self.source_report_id,
            "generated_at": self.generated_at,
            "universe_version": self.universe_version,
            "status": self.status,
            "usable": self.usable,
            "total_count": self.total_count,
            "tradable_count": self.tradable_count,
            "not_tradable_count": self.not_tradable_count,
            "mismatch_count": self.mismatch_count,
            "instruments": [item.as_dict() for item in self.instruments],
            "errors": list(self.errors),
        }
