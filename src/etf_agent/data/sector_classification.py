"""官方公司基本資料的產業代碼分類，供 DecisionPolicy 的產業曝險檢查使用。

只保存官方回應中的產業代碼，不自行翻譯產業名稱；TWSE 與 TPEx 的公司基本資料
使用同一套公開資訊觀測站產業代碼，標籤統一為 ``industry:<代碼>``。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Sequence
from urllib.request import Request, urlopen

from etf_agent.core import canonical_sha256
from .universe import Instrument, normalize_symbol


SECTOR_CLASSIFICATION_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class SectorSource:
    source_id: str
    market: str
    url: str
    code_field: str
    industry_field: str
    date_field: str


SECTOR_SOURCES = (
    SectorSource(
        "TWSE_COMPANY_PROFILE", "TWSE",
        "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
        "公司代號", "產業別", "出表日期",
    ),
    SectorSource(
        "TPEX_COMPANY_PROFILE", "TPEX",
        "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
        "SecuritiesCompanyCode", "SecuritiesIndustryCode", "Date",
    ),
)


class SectorClassificationError(ValueError):
    """產業分類來源或內容不可安全使用。"""


def _roc_date(value: str) -> str:
    text = value.strip()
    if len(text) != 7 or not text.isdigit():
        raise SectorClassificationError("出表日期不是 ROC yyyMMdd：%s" % value)
    return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:])).isoformat()


class SectorClassificationBuilder:
    """由已保存的原始回應與固定交易池確定性建立分類；缺漏股票明列，不補值。"""

    def __init__(self, universe: Sequence[Instrument], captures: Mapping[str, Mapping[str, object]]):
        self.universe = list(universe)
        self.captures = dict(captures)

    def build(self) -> Dict[str, object]:
        codes: Dict[str, Dict[str, str]] = {}
        sources: List[Dict[str, object]] = []
        for source in SECTOR_SOURCES:
            capture = self.captures.get(source.source_id)
            if capture is None:
                raise SectorClassificationError("缺少來源回應：%s" % source.source_id)
            raw = capture["raw"]
            if not isinstance(raw, bytes):
                raise SectorClassificationError("%s 原始回應必須是 bytes" % source.source_id)
            try:
                rows = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise SectorClassificationError("%s 不是有效 JSON：%s" % (source.source_id, error)) from error
            if not isinstance(rows, list) or not rows:
                raise SectorClassificationError("%s 回應必須是非空陣列" % source.source_id)
            market_codes: Dict[str, str] = {}
            content_dates = set()
            for index, row in enumerate(rows):
                if not isinstance(row, Mapping):
                    raise SectorClassificationError("%s 第 %d 列不是物件" % (source.source_id, index))
                company = str(row.get(source.code_field, "")).strip()
                industry = str(row.get(source.industry_field, "")).strip()
                if not company or not industry:
                    continue
                if company in market_codes and market_codes[company] != industry:
                    raise SectorClassificationError("%s 公司 %s 產業代碼衝突" % (source.source_id, company))
                market_codes[company] = industry
                content_dates.add(_roc_date(str(row.get(source.date_field, ""))))
            if len(content_dates) != 1:
                raise SectorClassificationError("%s 出表日期必須唯一：%s" % (source.source_id, sorted(content_dates)))
            codes[source.market] = market_codes
            sources.append(
                {
                    "source_id": source.source_id,
                    "market": source.market,
                    "url": source.url,
                    "fetched_at": capture["fetched_at"],
                    "content_date": next(iter(content_dates)),
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "row_count": len(rows),
                }
            )

        sector_by_symbol: Dict[str, str] = {}
        missing: List[str] = []
        for instrument in self.universe:
            market = "TPEX" if instrument.market.strip().upper() in {"TPEX", "OTC", "上櫃"} else "TWSE"
            symbol = normalize_symbol(instrument.symbol, instrument.market)
            industry = codes[market].get(instrument.code)
            if industry:
                sector_by_symbol[symbol] = "industry:%s" % industry
            else:
                missing.append(symbol)
        body: Dict[str, object] = {
            "schema_version": SECTOR_CLASSIFICATION_SCHEMA_VERSION,
            "available_at": max(str(item["fetched_at"]) for item in sources),
            "sources": sources,
            "sector_by_symbol": dict(sorted(sector_by_symbol.items())),
            "missing_symbols": sorted(missing),
            "status": "completed" if not missing else "degraded",
        }
        body["version"] = "sector:" + canonical_sha256(
            {"sources": sources, "sector_by_symbol": body["sector_by_symbol"]}
        )[:20]
        return body


def capture_sector_sources(
    output_dir: Path,
    *,
    fetch: Callable[..., object] = urlopen,
    now: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(),
) -> Dict[str, Dict[str, object]]:
    """抓取兩份官方公司基本資料並先保存原始位元組，再交給 Builder 解析。"""
    output_dir.mkdir(parents=True, exist_ok=False)
    captures: Dict[str, Dict[str, object]] = {}
    for source in SECTOR_SOURCES:
        request = Request(source.url, headers={"User-Agent": "ETF-Agent-AICUP-2026/0.1"})
        with fetch(request, timeout=30) as response:
            raw = response.read()
            status = response.status
        fetched_at = now()
        (output_dir / ("%s.json" % source.source_id)).write_bytes(raw)
        if status != 200:
            raise SectorClassificationError("%s HTTP 狀態不是 200：%s" % (source.source_id, status))
        captures[source.source_id] = {"raw": raw, "fetched_at": fetched_at}
    return captures
