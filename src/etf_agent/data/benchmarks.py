"""基準標的（benchmark）設定與官方歷史行情收集。

基準標的（例如 0050）只用來和虛擬帳戶績效比較，不是交易標的：收集時以
``in_competition_universe=False`` 保存，`instruments.in_competition_universe` 維持 0，
Snapshot、交易池驗證與 `analysis_daily_prices` 都不會多出一檔。只有
``license_status=approved`` 的基準才會收集與進入正式計算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence, Tuple

from .database import MarketDataDatabase
from .historical import HistoricalPriceProvider
from .historical_collector import HistoricalCollectionResult, HistoricalPriceCollector
from .universe import Instrument, normalize_symbol

BENCHMARK_KIND_SINGLE = "single_instrument"
BENCHMARK_KIND_EQUAL_WEIGHT = "universe_equal_weight"
BENCHMARK_KINDS = (BENCHMARK_KIND_SINGLE, BENCHMARK_KIND_EQUAL_WEIGHT)
APPROVED = "approved"


class BenchmarkConfigError(ValueError):
    """基準設定缺欄位或格式錯誤。"""


@dataclass(frozen=True)
class BenchmarkDefinition:
    benchmark_id: str
    label: str
    kind: str
    license_status: str
    instrument: Optional[Instrument] = None
    source: Optional[str] = None
    universe_path: Optional[str] = None
    terms_url: Optional[str] = None
    basis: Optional[str] = None
    reviewed_at: Optional[str] = None
    evidence: Optional[str] = None

    @property
    def approved(self) -> bool:
        return self.license_status == APPROVED

    def as_dict(self) -> dict:
        return {
            "benchmark_id": self.benchmark_id,
            "label": self.label,
            "kind": self.kind,
            "license_status": self.license_status,
            "symbol": self.instrument.symbol if self.instrument else None,
            "source": self.source,
            "universe": self.universe_path,
            "terms_url": self.terms_url,
            "basis": self.basis,
            "reviewed_at": self.reviewed_at,
            "evidence": self.evidence,
        }


def _text(row: Mapping[str, object], field: str, benchmark_id: str, required: bool = True) -> Optional[str]:
    value = row.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise BenchmarkConfigError("基準 %s 缺少欄位：%s" % (benchmark_id, field))
        return None
    if not isinstance(value, str):
        raise BenchmarkConfigError("基準 %s 的 %s 必須是字串" % (benchmark_id, field))
    return value.strip()


def parse_benchmarks(config: Mapping[str, object]) -> List[BenchmarkDefinition]:
    rows = config.get("benchmarks")
    if not isinstance(rows, list) or not rows:
        raise BenchmarkConfigError("benchmarks 必須是非空陣列")
    result: List[BenchmarkDefinition] = []
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise BenchmarkConfigError("benchmarks 的每一項必須是物件")
        benchmark_id = _text(row, "benchmark_id", str(row.get("benchmark_id", "?")))
        if benchmark_id in seen:
            raise BenchmarkConfigError("benchmark_id 重複：%s" % benchmark_id)
        seen.add(benchmark_id)
        kind = _text(row, "kind", benchmark_id)
        if kind not in BENCHMARK_KINDS:
            raise BenchmarkConfigError("基準 %s 的 kind 不支援：%s" % (benchmark_id, kind))
        license_status = _text(row, "license_status", benchmark_id)
        instrument = None
        source = None
        universe_path = None
        if kind == BENCHMARK_KIND_SINGLE:
            market = _text(row, "market", benchmark_id).upper()
            symbol = normalize_symbol(_text(row, "symbol", benchmark_id), market)
            instrument = Instrument(
                symbol=symbol,
                code=_text(row, "code", benchmark_id, required=False) or symbol.split(".", 1)[0],
                name=_text(row, "name", benchmark_id),
                market=market,
                source_date=_text(row, "reviewed_at", benchmark_id, required=False) or "",
            )
            source = _text(row, "source", benchmark_id)
        else:
            universe_path = _text(row, "universe", benchmark_id)
        result.append(
            BenchmarkDefinition(
                benchmark_id=benchmark_id,
                label=_text(row, "label", benchmark_id),
                kind=kind,
                license_status=license_status,
                instrument=instrument,
                source=source,
                universe_path=universe_path,
                terms_url=_text(row, "terms_url", benchmark_id, required=False),
                basis=_text(row, "basis", benchmark_id, required=False),
                reviewed_at=_text(row, "reviewed_at", benchmark_id, required=False),
                evidence=_text(row, "evidence", benchmark_id, required=False),
            )
        )
    return result


def load_benchmarks(path: Path) -> List[BenchmarkDefinition]:
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkConfigError("無法讀取基準設定：%s" % path) from error
    if not isinstance(config, Mapping):
        raise BenchmarkConfigError("基準設定頂層必須是物件")
    return parse_benchmarks(config)


def benchmark_instruments(benchmarks: Sequence[BenchmarkDefinition]) -> Tuple[List[Instrument], List[str]]:
    """回傳 (已核准單一標的清單, 被略過的基準說明)。未核准或非單一標的的基準不收集。"""
    instruments: List[Instrument] = []
    skipped: List[str] = []
    for item in benchmarks:
        if item.kind != BENCHMARK_KIND_SINGLE:
            continue
        if not item.approved:
            skipped.append("%s：license_status=%s，未核准不收集" % (item.benchmark_id, item.license_status))
            continue
        assert item.instrument is not None
        instruments.append(item.instrument)
    return instruments, skipped


def collect_benchmark_history(
    database: MarketDataDatabase,
    provider: HistoricalPriceProvider,
    benchmarks: Sequence[BenchmarkDefinition],
    start: date,
    end: date,
    max_workers: int = 1,
    retries: int = 3,
    progress: Optional[Callable[[str], None]] = None,
) -> Tuple[Optional[HistoricalCollectionResult], List[str]]:
    """以官方個股月行情收集已核准的基準標的；標的不進交易池。

    回傳 (收集結果, 被略過的基準說明)；沒有可收集的基準時結果為 None。
    """
    instruments, skipped = benchmark_instruments(benchmarks)
    if not instruments:
        return None, skipped
    for instrument in instruments:
        expected = next(item.source for item in benchmarks if item.instrument == instrument)
        actual = provider.source_for(instrument)
        if actual != expected:
            raise BenchmarkConfigError(
                "基準 %s 設定來源 %s，但 Provider 會寫入 %s" % (instrument.symbol, expected, actual)
            )
    collector = HistoricalPriceCollector(
        database,
        provider,
        max_workers=max_workers,
        retries=retries,
        progress=progress,
        in_competition_universe=False,
    )
    return collector.collect(instruments, start, end), skipped
