"""產業比較 C1：收集全市場各產業 EPS 統計（t187ap14）、建立代碼↔名稱表、逐檔核對。

只收集與稽核，不進決策：本模組的輸出不進 bundle、team_inputs、fundamental_metrics、
簡報或任何 Agent 輸入。資料時間規則：``available_at`` 是實際抓取時間且不回填；
``出表日期`` 是內容時間（民國日期轉西元、Asia/Taipei 零時）。數值一律用有限 Decimal，
缺值不補，核對只接受完全相符。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core import canonical_sha256, decimal_string, parse_aware_time, parse_decimal
from .corporate_contracts import TAIPEI_TIMEZONE
from .database import MarketDataDatabase
from .universe import Instrument, normalize_symbol


INDUSTRY_PEER_SCHEMA_VERSION = "1.0"
INDUSTRY_NAME_MAP_SCHEMA_VERSION = "1.0"
# t187ap14 與官方損益表 t187ap06 同為千元；金額文字內容以千元為單位。
PEER_UNIT_MULTIPLIER = 1000
# 官方以 "--" 表示該項不適用或未申報（例如金融保險業的營業利益）；視為缺值，不補。
MISSING_MARKERS = {"", "--"}
# 報表格式與一般業不同、營業收入／營業利益口徑不可比的業別名稱（只收集，不核對、不計中位數）。
NOT_COMPARABLE_INDUSTRY_NAMES = frozenset({"金融保險業", "金融業"})
# 官方損益表的業別代號：只有 ci（一般業）的營業收入與營業利益可與 t187ap14 同口徑核對。
COMPARABLE_STATEMENT_INDUSTRY = "ci"
VERIFIED_METRICS = (
    ("revenue", "營業收入"),
    ("operating_profit", "營業利益"),
    ("net_income", "稅後淨利"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS industry_peer_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    market TEXT NOT NULL,
    company_code TEXT NOT NULL,
    company_name TEXT NOT NULL,
    industry_name TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_quarter INTEGER NOT NULL CHECK (fiscal_quarter BETWEEN 1 AND 4),
    version INTEGER NOT NULL,
    revenue TEXT,
    operating_profit TEXT,
    non_operating_income TEXT,
    net_income TEXT,
    basic_eps TEXT,
    unit_multiplier INTEGER NOT NULL CHECK (unit_multiplier > 0),
    content_date TEXT NOT NULL,
    published_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    raw_payload_id INTEGER NOT NULL REFERENCES raw_payloads(id),
    mapping_version TEXT NOT NULL,
    UNIQUE (source, company_code, fiscal_year, fiscal_quarter, version)
);
CREATE INDEX IF NOT EXISTS idx_industry_peer_stats_company
ON industry_peer_stats(source, company_code, fiscal_year, fiscal_quarter);
"""
MAPPING_VERSION = "twse-tpex-t187ap14-2026-11-v1"


class IndustryPeerError(ValueError):
    """產業比較來源、內容或核對輸入不可安全使用。"""


@dataclass(frozen=True)
class PeerSource:
    source_id: str
    market: str
    url: str
    license_status: str
    terms_url: str
    evidence: str


@dataclass(frozen=True)
class PeerRow:
    market: str
    company_code: str
    company_name: str
    industry_name: str
    fiscal_year: int
    fiscal_quarter: int
    revenue: Optional[Decimal]
    operating_profit: Optional[Decimal]
    non_operating_income: Optional[Decimal]
    net_income: Optional[Decimal]
    basic_eps: Optional[Decimal]
    content_date: str
    published_at: str

    def content_sha256(self) -> str:
        """版本比較用雜湊：不含出表日期（每日重新產製），數值以固定字串表示。"""
        return canonical_sha256(
            {
                "market": self.market,
                "company_code": self.company_code,
                "company_name": self.company_name,
                "industry_name": self.industry_name,
                "fiscal_year": self.fiscal_year,
                "fiscal_quarter": self.fiscal_quarter,
                "unit_multiplier": PEER_UNIT_MULTIPLIER,
                "revenue": _dec_text(self.revenue),
                "operating_profit": _dec_text(self.operating_profit),
                "non_operating_income": _dec_text(self.non_operating_income),
                "net_income": _dec_text(self.net_income),
                "basic_eps": _dec_text(self.basic_eps),
            }
        )


# 上市與櫃買同一份資料集的欄位名稱不同（櫃買日期欄為 Date、年度為 Year、代號為 SecuritiesCompanyCode）。
_FIELDS = {
    "TWSE": {
        "date": "出表日期", "year": "年度", "quarter": "季別", "code": "公司代號",
        "name": "公司名稱", "industry": "產業別", "eps": "基本每股盈餘(元)",
    },
    "TPEX": {
        "date": "Date", "year": "Year", "quarter": "季別", "code": "SecuritiesCompanyCode",
        "name": "CompanyName", "industry": "產業別", "eps": "基本每股盈餘",
    },
}
_AMOUNT_FIELDS = {
    "revenue": "營業收入",
    "operating_profit": "營業利益",
    "non_operating_income": "營業外收入及支出",
    "net_income": "稅後淨利",
}


def _dec_text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else decimal_string(value)


def load_peer_sources(path: Path) -> List[PeerSource]:
    """讀取來源登記；只有 license_status=approved 的來源可收集，其餘明列跳過原因由呼叫端處理。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        raise IndustryPeerError("來源登記必須含非空 sources 陣列")
    sources: List[PeerSource] = []
    seen = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise IndustryPeerError("sources[%d] 必須是物件" % index)
        values = {}
        for key in ("source", "market", "url", "license_status", "terms_url", "evidence"):
            text = entry.get(key)
            if not isinstance(text, str) or not text.strip():
                raise IndustryPeerError("sources[%d].%s 必須是非空字串" % (index, key))
            values[key] = text.strip()
        if values["market"] not in _FIELDS:
            raise IndustryPeerError("sources[%d] 不支援的市場：%s" % (index, values["market"]))
        if values["source"] in seen:
            raise IndustryPeerError("來源代碼重複：%s" % values["source"])
        seen.add(values["source"])
        sources.append(
            PeerSource(values["source"], values["market"], values["url"],
                       values["license_status"], values["terms_url"], values["evidence"])
        )
    return sources


def _roc_date(value: str) -> date:
    text = value.strip()
    if len(text) != 7 or not text.isdigit():
        raise IndustryPeerError("出表日期不是民國 yyyMMdd：%s" % value)
    try:
        return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:]))
    except ValueError as error:
        raise IndustryPeerError("出表日期不是有效日期：%s" % value) from error


def _text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise IndustryPeerError("缺少或空白欄位：%s" % key)
    return value.strip()


def _amount(row: Mapping[str, object], key: str) -> Optional[Decimal]:
    if key not in row:
        raise IndustryPeerError("缺少欄位：%s" % key)
    value = row[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise IndustryPeerError("%s 必須是字串數值：%r" % (key, value))
    text = value.strip()
    if text in MISSING_MARKERS:
        return None
    return parse_decimal(text, key, error=IndustryPeerError)


def parse_peer_payload(
    payload: str,
    source: PeerSource,
    fetched_at: str,
    decision_cutoff: Optional[str] = None,
) -> Tuple[List[PeerRow], List[str]]:
    """解析一份 t187ap14 回應。

    回應層級問題（不是陣列、空陣列、重複公司代號、抓取時間缺時區或晚於截止）整份拒絕；
    單列問題（欄位缺漏、型別錯誤、非有限數值、日期錯誤）略過該列並記入警告，不補值。
    """
    fetched = parse_aware_time(fetched_at, "fetched_at", error=IndustryPeerError)
    if decision_cutoff is not None:
        cutoff = parse_aware_time(decision_cutoff, "decision_cutoff", error=IndustryPeerError)
        if fetched > cutoff:
            raise IndustryPeerError("抓取時間 %s 晚於決策截止時間 %s，拒絕使用" % (fetched_at, decision_cutoff))
    try:
        rows = json.loads(payload)
    except json.JSONDecodeError as error:
        raise IndustryPeerError("%s 不是有效 JSON：%s" % (source.source_id, error)) from error
    if not isinstance(rows, list) or not rows:
        raise IndustryPeerError("%s 回應必須是非空 JSON 陣列" % source.source_id)
    fields = _FIELDS[source.market]
    parsed: List[PeerRow] = []
    warnings: List[str] = []
    # 同一回應內公司代號重複無法判斷哪列為準，整份拒絕（不擇一）。
    seen_codes: Dict[str, int] = {}
    for index, row in enumerate(rows):
        code_value = row.get(fields["code"]) if isinstance(row, dict) else None
        if isinstance(code_value, str) and code_value.strip():
            code_text = code_value.strip()
            if code_text in seen_codes:
                raise IndustryPeerError(
                    "%s 公司代號 %s 重複（第 %d 與第 %d 列）" % (source.source_id, code_text, seen_codes[code_text] + 1, index + 1)
                )
            seen_codes[code_text] = index
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            warnings.append("第 %d 列不是物件，已略過" % (index + 1))
            continue
        try:
            code = _text(row, fields["code"])
            content_day = _roc_date(_text(row, fields["date"]))
            published = datetime.combine(content_day, datetime.min.time(), tzinfo=TAIPEI_TIMEZONE)
            if published > fetched:
                raise IndustryPeerError("出表日期 %s 晚於抓取時間，來源內容時間不可信" % content_day.isoformat())
            year_text = _text(row, fields["year"])
            quarter_text = _text(row, fields["quarter"])
            if not year_text.isdigit() or len(year_text) != 3:
                raise IndustryPeerError("年度不是民國三位數：%s" % year_text)
            if not quarter_text.isdigit() or not 1 <= int(quarter_text) <= 4:
                raise IndustryPeerError("季別必須介於 1 與 4：%s" % quarter_text)
            eps = _amount(row, fields["eps"])
            amounts = {name: _amount(row, label) for name, label in _AMOUNT_FIELDS.items()}
            parsed.append(
                PeerRow(
                    market=source.market,
                    company_code=code,
                    company_name=_text(row, fields["name"]),
                    industry_name=_text(row, fields["industry"]),
                    fiscal_year=int(year_text) + 1911,
                    fiscal_quarter=int(quarter_text),
                    revenue=amounts["revenue"],
                    operating_profit=amounts["operating_profit"],
                    non_operating_income=amounts["non_operating_income"],
                    net_income=amounts["net_income"],
                    basic_eps=eps,
                    content_date=content_day.isoformat(),
                    published_at=published.astimezone(timezone.utc).isoformat(),
                )
            )
        except IndustryPeerError as error:
            warnings.append("第 %d 列無法解析：%s" % (index + 1, error))
    if not parsed:
        raise IndustryPeerError("%s 沒有任何可解析的資料列：%s" % (source.source_id, "；".join(warnings[:3])))
    return parsed, warnings


def fetch_peer_payload(
    source: PeerSource, *, timeout_seconds: int = 30, retries: int = 3,
    user_agent: str = "ETF-Agent-AICUP-2026/0.1",
) -> Tuple[str, str]:
    request = urllib.request.Request(
        source.url, headers={"Accept": "application/json", "User-Agent": user_agent}
    )
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read().decode("utf-8-sig")
            return payload, datetime.now(timezone.utc).isoformat()
        except Exception as error:  # noqa: BLE001 - 重試後由呼叫端降級
            last_error = error
            if attempt + 1 < retries:
                time.sleep(0.25 * (2 ** attempt))
    assert last_error is not None
    raise last_error


@dataclass(frozen=True)
class SourceOutcome:
    source_id: str
    market: str
    status: str  # stored | failed | skipped
    fetched_rows: int = 0
    stored_rows: int = 0
    duplicate_rows: int = 0
    warnings: Tuple[str, ...] = ()
    reason: Optional[str] = None
    fetched_at: Optional[str] = None
    content_dates: Tuple[str, ...] = ()
    periods: Tuple[str, ...] = ()
    raw_sha256: Optional[str] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "source_id": self.source_id, "market": self.market, "status": self.status,
            "fetched_rows": self.fetched_rows, "stored_rows": self.stored_rows,
            "duplicate_rows": self.duplicate_rows, "warning_count": len(self.warnings),
            "warnings": list(self.warnings[:20]), "reason": self.reason,
            "fetched_at": self.fetched_at, "content_dates": list(self.content_dates),
            "periods": list(self.periods), "raw_sha256": self.raw_sha256,
        }


class IndustryPeerRepository:
    """產業比較表的保存與讀取；與既有文件／事實表共用同一個資料庫與原始回應表。"""

    def __init__(self, database: MarketDataDatabase):
        self.database = database

    def initialize(self) -> None:
        self.database.initialize()
        with self.database.connect() as connection:
            connection.executescript(SCHEMA)

    def store(
        self, rows: Sequence[PeerRow], source: PeerSource, payload: str,
        fetched_at: str, run_id: str,
    ) -> Tuple[int, int]:
        """回傳（新增列數, 重複列數）。同內容不新增版本，內容變動才 +1；較舊的抓取不蓋過新版。"""
        fetched_utc = parse_aware_time(fetched_at, "fetched_at", error=IndustryPeerError).isoformat()
        stored = duplicates = 0
        with self.database.connect() as connection:
            raw_id = self._raw_payload_id(connection, run_id, source, fetched_utc, payload)
            for row in rows:
                digest = row.content_sha256()
                previous = connection.execute(
                    """
                    SELECT version, content_sha256, fetched_at FROM industry_peer_stats
                    WHERE source = ? AND company_code = ? AND fiscal_year = ? AND fiscal_quarter = ?
                    ORDER BY version DESC LIMIT 1
                    """,
                    (source.source_id, row.company_code, row.fiscal_year, row.fiscal_quarter),
                ).fetchone()
                if previous is not None:
                    if previous["content_sha256"] == digest or _earlier(fetched_utc, str(previous["fetched_at"])):
                        duplicates += 1
                        continue
                version = int(previous["version"]) + 1 if previous is not None else 1
                connection.execute(
                    """
                    INSERT INTO industry_peer_stats(
                        source, market, company_code, company_name, industry_name,
                        fiscal_year, fiscal_quarter, version, revenue, operating_profit,
                        non_operating_income, net_income, basic_eps, unit_multiplier,
                        content_date, published_at, available_at, fetched_at,
                        content_sha256, raw_payload_id, mapping_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source.source_id, row.market, row.company_code, row.company_name,
                        row.industry_name, row.fiscal_year, row.fiscal_quarter, version,
                        _dec_text(row.revenue), _dec_text(row.operating_profit),
                        _dec_text(row.non_operating_income), _dec_text(row.net_income),
                        _dec_text(row.basic_eps), PEER_UNIT_MULTIPLIER, row.content_date,
                        row.published_at, fetched_utc, fetched_utc, digest, raw_id, MAPPING_VERSION,
                    ),
                )
                stored += 1
        return stored, duplicates

    @staticmethod
    def _raw_payload_id(
        connection: sqlite3.Connection, run_id: str, source: PeerSource, fetched_at: str, payload: str
    ) -> int:
        sha = _sha256_text(payload)
        existing = connection.execute(
            "SELECT id FROM raw_payloads WHERE source = ? AND sha256 = ? ORDER BY id LIMIT 1",
            (source.source_id, sha),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        return MarketDataDatabase.insert_raw_payload(connection, run_id, source.source_id, source.url, fetched_at, payload)

    def latest_rows(
        self, market: Optional[str] = None, as_of: Optional[str] = None
    ) -> List[Dict[str, object]]:
        """每家公司最新期別的最新版本（available_at 不晚於 as_of）。"""
        clause = ""
        params: List[object] = []
        if as_of is not None:
            clause += " AND available_at <= ?"
            params.append(parse_aware_time(as_of, "as_of", error=IndustryPeerError).isoformat())
        if market is not None:
            clause += " AND market = ?"
            params.append(market)
        with self.database.connect() as connection:
            records = connection.execute(
                "SELECT * FROM industry_peer_stats WHERE 1 = 1" + clause
                + " ORDER BY source, company_code, fiscal_year DESC, fiscal_quarter DESC, version DESC",
                params,
            ).fetchall()
        latest: Dict[Tuple[str, str], Dict[str, object]] = {}
        for record in records:
            key = (str(record["source"]), str(record["company_code"]))
            if key not in latest:
                latest[key] = dict(record)
        return list(latest.values())


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _earlier(candidate: str, existing: str) -> bool:
    try:
        return parse_aware_time(candidate, "fetched_at") < parse_aware_time(existing, "fetched_at")
    except ValueError:
        return False


class IndustryPeerCollector:
    """逐來源抓取與保存；單一來源失敗只記原因，不影響其他來源。"""

    def __init__(
        self,
        repository: IndustryPeerRepository,
        sources: Sequence[PeerSource],
        fetch: Callable[[PeerSource], Tuple[str, str]] = fetch_peer_payload,
    ):
        self.repository = repository
        self.sources = list(sources)
        self.fetch = fetch

    def collect(self, decision_cutoff: Optional[str] = None) -> List[SourceOutcome]:
        self.repository.initialize()
        run_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()
        database = self.repository.database
        with database.connect() as connection:
            connection.execute(
                "INSERT INTO collection_runs(run_id, source, started_at, status) VALUES (?, 'INDUSTRY_PEER', ?, 'running')",
                (run_id, started_at),
            )
        outcomes: List[SourceOutcome] = []
        for source in self.sources:
            if source.license_status != "approved":
                outcomes.append(SourceOutcome(
                    source.source_id, source.market, "skipped",
                    reason="license_status=%s，未核准不收集" % source.license_status))
                continue
            try:
                payload, fetched_at = self.fetch(source)
            except Exception as error:  # noqa: BLE001 - 端點失敗降級
                outcomes.append(SourceOutcome(source.source_id, source.market, "failed", reason="取得失敗：%s" % error))
                continue
            try:
                rows, warnings = parse_peer_payload(payload, source, fetched_at, decision_cutoff)
                stored, duplicates = self.repository.store(rows, source, payload, fetched_at, run_id)
            except (IndustryPeerError, sqlite3.Error) as error:
                outcomes.append(SourceOutcome(source.source_id, source.market, "failed", reason="解析或保存失敗：%s" % error,
                                              fetched_at=fetched_at))
                continue
            outcomes.append(SourceOutcome(
                source.source_id, source.market, "stored", fetched_rows=len(rows) + len(warnings),
                stored_rows=stored, duplicate_rows=duplicates, warnings=tuple(warnings),
                fetched_at=fetched_at, raw_sha256=_sha256_text(payload),
                content_dates=tuple(sorted({row.content_date for row in rows})),
                periods=tuple(sorted({"%d-Q%d" % (row.fiscal_year, row.fiscal_quarter) for row in rows})),
            ))
        failed = [item for item in outcomes if item.status == "failed"]
        with database.connect() as connection:
            connection.execute(
                """
                UPDATE collection_runs SET finished_at = ?, status = ?, fetched_rows = ?, stored_rows = ?,
                    warning_count = ?, error_message = ? WHERE run_id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    "failed" if outcomes and len(failed) == len(outcomes) else "success",
                    sum(item.fetched_rows for item in outcomes),
                    sum(item.stored_rows for item in outcomes),
                    sum(len(item.warnings) for item in outcomes),
                    "；".join("%s：%s" % (item.source_id, item.reason) for item in failed) or None,
                    run_id,
                ),
            )
        return outcomes


# ---------------------------------------------------------------------------
# 代碼 ↔ 名稱表
# ---------------------------------------------------------------------------


def _market_of(instrument: Instrument) -> str:
    return "TPEX" if instrument.market.strip().upper() in {"TPEX", "OTC", "上櫃"} else "TWSE"


def _code_of(label: str) -> str:
    return label.split(":", 1)[1] if label.startswith("industry:") else label


def build_industry_name_map(
    universe: Sequence[Instrument],
    sector_by_symbol: Mapping[str, str],
    peer_rows: Sequence[Mapping[str, object]],
    sector_version: str,
    sources: Sequence[Mapping[str, object]] = (),
) -> Dict[str, object]:
    """以交易池公司對照 t187ap14 產業名稱與既有產業代碼，建立一對一的代碼↔名稱表。

    以市場為範圍檢查：同一代碼對多個名稱、或同一名稱對多個代碼，整組拒絕並列入 conflicts，
    不猜、不擇一。上市與櫃買的名稱不同時另列 cross_market_name_differences，不合併。
    """
    peer_by_key = {(str(row["market"]), str(row["company_code"])): row for row in peer_rows}
    pairs: Dict[str, Dict[Tuple[str, str], List[str]]] = {}
    unmapped: List[Dict[str, str]] = []
    for instrument in universe:
        market = _market_of(instrument)
        symbol = normalize_symbol(instrument.symbol, instrument.market)
        label = sector_by_symbol.get(symbol)
        peer = peer_by_key.get((market, instrument.code))
        if not label or peer is None:
            unmapped.append({"symbol": symbol, "reason": "no_sector_code" if not label else "no_peer_row"})
            continue
        pairs.setdefault(market, {}).setdefault((_code_of(label), str(peer["industry_name"])), []).append(symbol)

    conflicts: List[Dict[str, object]] = []
    mapping: Dict[str, Dict[str, Dict[str, object]]] = {}
    for market in sorted(pairs):
        by_code: Dict[str, Dict[str, List[str]]] = {}
        by_name: Dict[str, Dict[str, List[str]]] = {}
        for (code, name), symbols in pairs[market].items():
            by_code.setdefault(code, {})[name] = symbols
            by_name.setdefault(name, {})[code] = symbols
        bad_codes = set()
        bad_names = set()
        for code, names in sorted(by_code.items()):
            if len(names) > 1:
                bad_codes.add(code)
                conflicts.append({"market": market, "kind": "code_multiple_names", "code": code,
                                  "names": {n: sorted(s) for n, s in sorted(names.items())}})
        for name, codes in sorted(by_name.items()):
            if len(codes) > 1:
                bad_names.add(name)
                conflicts.append({"market": market, "kind": "name_multiple_codes", "name": name,
                                  "codes": {c: sorted(s) for c, s in sorted(codes.items())}})
        mapping[market] = {}
        for code, names in sorted(by_code.items()):
            if code in bad_codes:
                continue
            name, symbols = next(iter(names.items()))
            if name in bad_names:
                continue
            mapping[market][code] = {"name": name, "pool_symbols": sorted(symbols)}
    differences = []
    if "TWSE" in mapping and "TPEX" in mapping:
        for code in sorted(set(mapping["TWSE"]) & set(mapping["TPEX"])):
            if mapping["TWSE"][code]["name"] != mapping["TPEX"][code]["name"]:
                differences.append({"code": code, "TWSE": mapping["TWSE"][code]["name"],
                                    "TPEX": mapping["TPEX"][code]["name"]})
    body: Dict[str, object] = {
        "schema_version": INDUSTRY_NAME_MAP_SCHEMA_VERSION,
        "mapping_version": MAPPING_VERSION,
        "sector_classification_version": sector_version,
        "mapping": mapping,
        "conflicts": conflicts,
        "cross_market_name_differences": differences,
        "unmapped_pool_symbols": sorted(unmapped, key=lambda item: item["symbol"]),
        "status": "completed" if not conflicts and not unmapped else "degraded",
    }
    # 版本只由對照內容決定（不含每日變動的抓取時間與出表日期），內容不變則版本不變。
    body["version"] = "industry_name_map:" + canonical_sha256(
        {key: body[key] for key in ("mapping", "conflicts", "cross_market_name_differences",
                                    "unmapped_pool_symbols", "sector_classification_version", "mapping_version")}
    )[:20]
    body["sources"] = [dict(item) for item in sources]
    body["content_sha256"] = canonical_sha256({key: value for key, value in body.items() if key != "content_sha256"})
    return body


def verify_industry_name_map(artifact: Mapping[str, object]) -> None:
    """重算版本與內容雜湊；不符即拒絕（靜態產物被手改或損毀）。"""
    for key in ("mapping", "conflicts", "cross_market_name_differences", "unmapped_pool_symbols",
                "sector_classification_version", "mapping_version", "version", "content_sha256"):
        if key not in artifact:
            raise IndustryPeerError("代碼名稱表缺少欄位：%s" % key)
    version = "industry_name_map:" + canonical_sha256(
        {key: artifact[key] for key in ("mapping", "conflicts", "cross_market_name_differences",
                                        "unmapped_pool_symbols", "sector_classification_version", "mapping_version")}
    )[:20]
    if version != artifact["version"]:
        raise IndustryPeerError("代碼名稱表版本與內容不符")
    body = {key: value for key, value in artifact.items() if key != "content_sha256"}
    if canonical_sha256(body) != artifact["content_sha256"]:
        raise IndustryPeerError("代碼名稱表 content_sha256 與內容不符")


# ---------------------------------------------------------------------------
# 逐檔核對
# ---------------------------------------------------------------------------


def _official_statement(
    connection: sqlite3.Connection, symbol: str, fiscal_year: int, fiscal_quarter: int, as_of: str
) -> Tuple[Optional[Dict[str, object]], List[str]]:
    """同公司同期別累計的官方損益表最新版本；多個業別並存視為不明確。"""
    rows = connection.execute(
        """
        SELECT sd.id AS document_id, sd.external_id, sd.version, sd.source, fs.industry
        FROM financial_statements fs JOIN source_documents sd ON sd.id = fs.document_id
        WHERE fs.symbol = ? AND fs.statement_type = 'income_statement'
          AND fs.fiscal_year = ? AND fs.fiscal_quarter = ?
          AND (sd.source LIKE 'TWSE_MOPS_INCOME%' OR sd.source LIKE 'TPEX_MOPS_INCOME%')
          AND sd.fetched_at <= ?
        ORDER BY sd.external_id, sd.version DESC
        """,
        (symbol, fiscal_year, fiscal_quarter, as_of),
    ).fetchall()
    latest: Dict[str, sqlite3.Row] = {}
    for row in rows:
        latest.setdefault(str(row["external_id"]), row)
    if not latest:
        return None, []
    if len(latest) > 1:
        return None, sorted(latest)
    row = next(iter(latest.values()))
    facts = {
        fact["metric_key"]: dict(fact)
        for fact in connection.execute(
            "SELECT metric_key, value, value_status, unit_multiplier FROM financial_statement_facts WHERE document_id = ?",
            (row["document_id"],),
        )
    }
    return {"document_id": int(row["document_id"]), "external_id": str(row["external_id"]),
            "industry": str(row["industry"]), "facts": facts}, []


def verify_pool_against_official(
    database: MarketDataDatabase,
    universe: Sequence[Instrument],
    peer_rows: Sequence[Mapping[str, object]],
    as_of: str,
) -> Dict[str, object]:
    """交易池公司的 t187ap14 三個損益數字與官方損益表逐檔完全相符才算核對通過。

    verified｜unavailable（原因逐檔記錄，不填假值）｜not_comparable（金融保險業等報表格式不同，
    只收集、不當核對失敗）。
    """
    as_of_utc = parse_aware_time(as_of, "as_of", error=IndustryPeerError).isoformat()
    peer_by_key = {(str(row["market"]), str(row["company_code"])): row for row in peer_rows}
    per_symbol: Dict[str, Dict[str, object]] = {}
    with database.connect() as connection:
        for instrument in universe:
            symbol = normalize_symbol(instrument.symbol, instrument.market)
            market = _market_of(instrument)
            peer = peer_by_key.get((market, instrument.code))
            result = _verify_one(connection, symbol, peer, as_of_utc)
            result["market"] = market
            per_symbol[symbol] = result
    counts = {"verified": 0, "unavailable": 0, "not_comparable": 0}
    reasons: Dict[str, int] = {}
    for result in per_symbol.values():
        counts[str(result["status"])] += 1
        if result["status"] != "verified":
            reasons[str(result["reason"])] = reasons.get(str(result["reason"]), 0) + 1
    return {
        "as_of": as_of_utc,
        "total": len(per_symbol),
        "verified": counts["verified"],
        "unavailable": counts["unavailable"],
        "not_comparable": counts["not_comparable"],
        "value_mismatch": reasons.get("value_mismatch", 0),
        "reason_counts": dict(sorted(reasons.items())),
        "by_symbol": dict(sorted(per_symbol.items())),
    }


def _verify_one(
    connection: sqlite3.Connection, symbol: str, peer: Optional[Mapping[str, object]], as_of: str
) -> Dict[str, object]:
    if peer is None:
        # 沒有 t187ap14 列時，若官方損益表屬金融類報表格式，仍標不可比（金融股本來就沒有可比營收）。
        row = connection.execute(
            """
            SELECT fs.industry FROM financial_statements fs JOIN source_documents sd ON sd.id = fs.document_id
            WHERE fs.symbol = ? AND fs.statement_type = 'income_statement'
              AND (sd.source LIKE 'TWSE_MOPS_INCOME%' OR sd.source LIKE 'TPEX_MOPS_INCOME%')
            ORDER BY fs.fiscal_year DESC, fs.fiscal_quarter DESC, sd.version DESC LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if row is not None and str(row["industry"]) != COMPARABLE_STATEMENT_INDUSTRY:
            return {"status": "not_comparable", "reason": "statement_industry_%s" % row["industry"]}
        return {"status": "unavailable", "reason": "no_peer_row"}
    period = {"fiscal_year": int(peer["fiscal_year"]), "fiscal_quarter": int(peer["fiscal_quarter"]),
              "industry_name": str(peer["industry_name"]), "peer_content_sha256": str(peer["content_sha256"]),
              "peer_source": str(peer["source"]), "peer_version": int(peer["version"])}
    official, ambiguous = _official_statement(connection, symbol, period["fiscal_year"], period["fiscal_quarter"], as_of)
    if ambiguous:
        return {"status": "unavailable", "reason": "official_ambiguous", "external_ids": ambiguous, **period}
    if official is None:
        other = connection.execute(
            """
            SELECT fs.fiscal_year, fs.fiscal_quarter, fs.industry FROM financial_statements fs
            JOIN source_documents sd ON sd.id = fs.document_id
            WHERE fs.symbol = ? AND fs.statement_type = 'income_statement'
              AND (sd.source LIKE 'TWSE_MOPS_INCOME%' OR sd.source LIKE 'TPEX_MOPS_INCOME%')
            ORDER BY fs.fiscal_year DESC, fs.fiscal_quarter DESC LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if period["industry_name"] in NOT_COMPARABLE_INDUSTRY_NAMES or (
            other is not None and str(other["industry"]) != COMPARABLE_STATEMENT_INDUSTRY
        ):
            return {"status": "not_comparable", "reason": "financial_industry", **period}
        if other is None:
            return {"status": "unavailable", "reason": "no_official_statement", **period}
        return {"status": "unavailable", "reason": "period_mismatch",
                "official_latest_period": "%d-Q%d" % (other["fiscal_year"], other["fiscal_quarter"]), **period}
    if period["industry_name"] in NOT_COMPARABLE_INDUSTRY_NAMES or official["industry"] != COMPARABLE_STATEMENT_INDUSTRY:
        return {"status": "not_comparable", "reason": "financial_industry",
                "statement_industry": official["industry"], **period}
    metrics: Dict[str, Dict[str, object]] = {}
    problems: List[str] = []
    for key, label in VERIFIED_METRICS:
        peer_value = peer[key]
        fact = official["facts"].get(key)
        if peer_value is None:
            metrics[key] = {"status": "peer_value_missing"}
            problems.append("peer_value_missing")
            continue
        if fact is None or fact["value_status"] != "provided" or fact["value"] is None:
            metrics[key] = {"status": "official_not_reported", "peer": str(peer_value)}
            problems.append("official_not_reported")
            continue
        peer_amount = Decimal(str(peer_value)) * int(peer["unit_multiplier"])
        official_amount = Decimal(str(fact["value"])) * int(fact["unit_multiplier"])
        if peer_amount == official_amount:
            metrics[key] = {"status": "match"}
        else:
            metrics[key] = {"status": "mismatch", "peer_amount": decimal_string(peer_amount),
                            "official_amount": decimal_string(official_amount)}
            problems.append("value_mismatch")
    if problems:
        # 失敗原因優先順序：數值不符 > 官方未申報 > t187ap14 缺值。
        for reason in ("value_mismatch", "official_not_reported", "peer_value_missing"):
            if reason in problems:
                return {"status": "unavailable", "reason": reason, "metrics": metrics,
                        "official_external_id": official["external_id"], **period}
    return {"status": "verified", "reason": None, "metrics": metrics,
            "official_external_id": official["external_id"], **period}
