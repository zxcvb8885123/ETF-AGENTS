"""Deterministic source-health probes and competition-universe validation."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.contracts import (
    SourceFeasibilityReport,
    SourceProbeResult,
    UniverseInstrumentStatus,
    UniverseValidationResult,
)

from .twse import parse_roc_date
from .universe import Instrument


SCHEMA_VERSION = "1.0"


def _market_key(value: str) -> str:
    normalized = value.strip().upper()
    if normalized in {"TPEX", "OTC", "上櫃"}:
        return "TPEX"
    if normalized in {"TWSE", "上市"}:
        return "TWSE"
    return normalized


def compute_universe_version(universe: Sequence[Instrument]) -> str:
    content = "\n".join(
        "%s|%s" % (item.symbol, item.source_date)
        for item in sorted(universe, key=lambda value: value.symbol)
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SourceProbeDefinition:
    source_id: str
    market: str
    data_type: str
    url: str
    format: str
    code_field: str
    date_field: str
    tradable_field: str
    required_fields: Tuple[str, ...]
    timeout_seconds: int = 30
    user_agent: str = "ETF-Agent-AICUP-2026/0.1"

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "SourceProbeDefinition":
        required = (
            "source_id",
            "market",
            "data_type",
            "url",
            "format",
            "code_field",
            "date_field",
            "tradable_field",
            "required_fields",
        )
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError("來源探測設定缺少欄位：%s" % ", ".join(missing))
        fields = value["required_fields"]
        if not isinstance(fields, list) or not fields:
            raise ValueError("required_fields 必須是非空陣列")
        timeout = int(value.get("timeout_seconds", 30))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout_seconds 必須介於 1 與 120")
        return cls(
            source_id=str(value["source_id"]),
            market=_market_key(str(value["market"])),
            data_type=str(value["data_type"]),
            url=str(value["url"]),
            format=str(value["format"]).lower(),
            code_field=str(value["code_field"]),
            date_field=str(value["date_field"]),
            tradable_field=str(value["tradable_field"]),
            required_fields=tuple(str(item) for item in fields),
            timeout_seconds=timeout,
            user_agent=str(value.get("user_agent", "ETF-Agent-AICUP-2026/0.1")),
        )


class SourceHealthProbe:
    def __init__(
        self,
        opener: Optional[Callable[..., object]] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.opener = opener or urllib.request.urlopen
        self.clock = clock or time.monotonic

    def run(
        self,
        definitions: Sequence[SourceProbeDefinition],
        universe: Sequence[Instrument],
    ) -> SourceFeasibilityReport:
        generated_at = datetime.now(timezone.utc).isoformat()
        probes = [self.probe(definition, universe) for definition in definitions]
        errors: List[str] = []
        if not definitions:
            errors.append("NO_PROBE_SOURCES_CONFIGURED")
        failed_count = sum(probe.status == "failed" for probe in probes)
        incomplete_count = sum(
            probe.universe_covered < probe.universe_total for probe in probes
        )
        if not probes or failed_count == len(probes):
            status = "failed"
        elif failed_count or incomplete_count:
            status = "degraded"
        else:
            status = "passed"
        return SourceFeasibilityReport(
            schema_version=SCHEMA_VERSION,
            report_id=str(uuid.uuid4()),
            generated_at=generated_at,
            status=status,
            probes=probes,
            errors=errors,
        )

    def probe(
        self,
        definition: SourceProbeDefinition,
        universe: Sequence[Instrument],
    ) -> SourceProbeResult:
        started = self.clock()
        fetched_at = datetime.now(timezone.utc).isoformat()
        http_status: Optional[int] = None
        payload: Optional[str] = None
        request = urllib.request.Request(
            definition.url,
            headers={
                "Accept": "application/json",
                "User-Agent": definition.user_agent,
            },
        )
        try:
            with self.opener(request, timeout=definition.timeout_seconds) as response:
                payload = response.read().decode("utf-8-sig")
                http_status = int(
                    getattr(response, "status", None) or response.getcode()
                )
            return self.parse_payload(
                definition,
                payload,
                universe,
                fetched_at=fetched_at,
                duration_ms=max(0, int((self.clock() - started) * 1000)),
                http_status=http_status,
            )
        except Exception as error:
            market_symbols = [
                item.symbol
                for item in universe
                if _market_key(item.market) == definition.market
            ]
            return SourceProbeResult(
                source_id=definition.source_id,
                market=definition.market,
                data_type=definition.data_type,
                url=definition.url,
                status="failed",
                fetched_at=fetched_at,
                duration_ms=max(0, int((self.clock() - started) * 1000)),
                http_status=http_status,
                format=definition.format,
                row_count=0,
                data_date=None,
                universe_total=len(market_symbols),
                universe_covered=0,
                missing_symbols=sorted(market_symbols),
                content_sha256=(
                    hashlib.sha256(payload.encode("utf-8")).hexdigest()
                    if payload is not None
                    else None
                ),
                errors=["%s: %s" % (type(error).__name__, error)],
            )

    @staticmethod
    def parse_payload(
        definition: SourceProbeDefinition,
        payload: str,
        universe: Sequence[Instrument],
        fetched_at: str,
        duration_ms: int = 0,
        http_status: int = 200,
    ) -> SourceProbeResult:
        if definition.format != "json":
            raise ValueError("M0 只支援 JSON 來源探測")
        try:
            rows = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ValueError("來源回應不是有效 JSON") from error
        if not isinstance(rows, list) or not rows:
            raise ValueError("來源回應必須是非空 JSON 陣列")
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError("來源回應包含非物件資料列")

        schema_fields = sorted(str(key) for key in rows[0].keys())
        missing_fields = sorted(set(definition.required_fields) - set(schema_fields))
        if missing_fields:
            raise ValueError("來源 schema 缺少欄位：%s" % ", ".join(missing_fields))

        observed_codes = set()
        tradable_codes = set()
        dates = set()
        for index, row in enumerate(rows, start=1):
            code = str(row.get(definition.code_field) or "").strip().upper()
            raw_date = str(row.get(definition.date_field) or "").strip()
            if not code or not raw_date:
                raise ValueError("來源第 %d 列缺少代號或日期" % index)
            observed_codes.add(code)
            dates.add(parse_roc_date(raw_date))
            value = str(row.get(definition.tradable_field) or "").strip()
            if value not in {"", "-", "--", "---"}:
                tradable_codes.add(code)

        market_universe = [
            item for item in universe if _market_key(item.market) == definition.market
        ]
        covered = [item for item in market_universe if item.code in observed_codes]
        missing = [item.symbol for item in market_universe if item.code not in observed_codes]
        return SourceProbeResult(
            source_id=definition.source_id,
            market=definition.market,
            data_type=definition.data_type,
            url=definition.url,
            status="passed",
            fetched_at=fetched_at,
            duration_ms=duration_ms,
            http_status=http_status,
            format=definition.format,
            row_count=len(rows),
            data_date=max(dates),
            schema_fields=schema_fields,
            observed_codes=sorted(observed_codes),
            tradable_codes=sorted(tradable_codes),
            universe_total=len(market_universe),
            universe_covered=len(covered),
            missing_symbols=sorted(missing),
            content_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            errors=[],
        )


class UniverseValidator:
    def validate(
        self,
        universe: Sequence[Instrument],
        report: SourceFeasibilityReport,
        successor_candidates: Optional[Mapping[str, str]] = None,
    ) -> UniverseValidationResult:
        generated_at = datetime.now(timezone.utc).isoformat()
        successors = {
            str(key).upper(): str(value).upper()
            for key, value in (successor_candidates or {}).items()
        }
        probes_by_market = {
            probe.market: probe
            for probe in report.probes
            if probe.data_type == "latest_prices"
        }
        statuses: List[UniverseInstrumentStatus] = []
        errors: List[str] = []
        if not universe:
            errors.append("EMPTY_COMPETITION_UNIVERSE")

        for instrument in sorted(universe, key=lambda value: value.symbol):
            market = _market_key(instrument.market)
            probe = probes_by_market.get(market)
            if probe is None:
                statuses.append(
                    self._mismatch(instrument, "", None, "SOURCE_NOT_CONFIGURED", None)
                )
                continue
            if probe.status != "passed":
                statuses.append(
                    self._mismatch(
                        instrument,
                        probe.source_id,
                        probe.data_date,
                        "SOURCE_UNAVAILABLE",
                        None,
                    )
                )
                continue
            if instrument.code not in probe.observed_codes:
                candidate = successors.get(instrument.symbol.upper()) or successors.get(
                    instrument.code.upper()
                )
                candidate_symbol = None
                if candidate:
                    candidate_code = candidate.split(".", 1)[0]
                    if candidate_code in probe.observed_codes:
                        suffix = ".TWO" if market == "TPEX" else ".TW"
                        candidate_symbol = (
                            candidate if "." in candidate else candidate_code + suffix
                        )
                statuses.append(
                    self._mismatch(
                        instrument,
                        probe.source_id,
                        probe.data_date,
                        "SYMBOL_NOT_FOUND",
                        candidate_symbol,
                    )
                )
                continue
            if instrument.code not in probe.tradable_codes:
                statuses.append(
                    UniverseInstrumentStatus(
                        symbol=instrument.symbol,
                        code=instrument.code,
                        name=instrument.name,
                        market=market,
                        status="not_tradable",
                        source_id=probe.source_id,
                        data_date=probe.data_date,
                        reason_code="NO_TRADABLE_PRICE",
                        evidence="官方最新行情存在代號，但沒有可用成交價",
                    )
                )
                continue
            statuses.append(
                UniverseInstrumentStatus(
                    symbol=instrument.symbol,
                    code=instrument.code,
                    name=instrument.name,
                    market=market,
                    status="tradable",
                    source_id=probe.source_id,
                    data_date=probe.data_date,
                    reason_code="LATEST_PRICE_PRESENT",
                    evidence="官方最新行情包含代號與可用成交價",
                )
            )

        tradable_count = sum(item.status == "tradable" for item in statuses)
        not_tradable_count = sum(item.status == "not_tradable" for item in statuses)
        mismatch_count = sum(item.status == "universe_mismatch" for item in statuses)
        if errors or report.status == "failed":
            status = "failed"
        elif not_tradable_count or mismatch_count or report.status == "degraded":
            status = "degraded"
        else:
            status = "completed"
        usable = status == "completed" and tradable_count == len(statuses)
        return UniverseValidationResult(
            schema_version=SCHEMA_VERSION,
            validation_id=str(uuid.uuid4()),
            source_report_id=report.report_id,
            generated_at=generated_at,
            universe_version=compute_universe_version(universe),
            status=status,
            usable=usable,
            total_count=len(statuses),
            tradable_count=tradable_count,
            not_tradable_count=not_tradable_count,
            mismatch_count=mismatch_count,
            instruments=statuses,
            errors=errors,
        )

    @staticmethod
    def _mismatch(
        instrument: Instrument,
        source_id: str,
        data_date: Optional[str],
        reason_code: str,
        successor_candidate: Optional[str],
    ) -> UniverseInstrumentStatus:
        evidence = "官方最新行情找不到交易池代號"
        if reason_code == "SOURCE_UNAVAILABLE":
            evidence = "官方來源探測失敗，無法確認交易池代號"
        elif reason_code == "SOURCE_NOT_CONFIGURED":
            evidence = "沒有設定此市場的官方最新行情探測來源"
        elif successor_candidate:
            evidence += "；來源中出現候選承接代號 %s，但不自動替換" % successor_candidate
        return UniverseInstrumentStatus(
            symbol=instrument.symbol,
            code=instrument.code,
            name=instrument.name,
            market=_market_key(instrument.market),
            status="universe_mismatch",
            source_id=source_id,
            data_date=data_date,
            reason_code=reason_code,
            evidence=evidence,
            successor_candidate=successor_candidate,
        )


def load_probe_definitions(config: Mapping[str, object]) -> List[SourceProbeDefinition]:
    values = config.get("source_probes", [])
    if not isinstance(values, list):
        raise ValueError("source_probes 必須是陣列")
    return [SourceProbeDefinition.from_dict(item) for item in values]


def write_json_artifact(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
