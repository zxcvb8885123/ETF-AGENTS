"""把已封存的政府開放 CSV 轉成 TradingStatusBundle 的 records／coverage 與交易日曆。

語意不確定的地方一律保守：沒有明確終止日的限制視為持續有效；處置提到順延時不設
終止日；缺少對應來源的市場／類別不產生 coverage（Bundle 會判 unknown）。來源是否
核准由 ``config/trading_status_approvals.json`` 決定，程式不自行核准；只有已核准且
抓取時間在允許時效內的來源，coverage 才標為 complete。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from etf_agent.core import canonical_sha256, parse_aware_time
from .evidence import TAIPEI_TIMEZONE
from .ogd_candidate_facts import extract_ogd_candidate_facts
from .trading_status import DEFAULT_REQUIRED_CATEGORIES, TradingStatusError, TradingStatusRequest


OGD_MAPPING_VERSION = "ogd-trading-status-mapping-1"
# 市場 × 類別 → 可提供該類別狀態的政府開放來源；未列出者（例如 TWSE 管理股票）沒有來源。
CATEGORY_SOURCES: Dict[Tuple[str, str], Tuple[str, ...]] = {
    ("TPEX", "trading_halt"): ("TPEX_SPECIAL_OGD", "TPEX_HALTS_HISTORY_OGD"),
    ("TPEX", "special_trading"): ("TPEX_SPECIAL_OGD",),
    ("TPEX", "split_trading"): ("TPEX_SPECIAL_OGD",),
    ("TPEX", "management"): ("TPEX_SPECIAL_OGD",),
    ("TPEX", "disposition"): ("TPEX_DISPOSAL_OGD",),
    ("TPEX", "attention"): ("TPEX_ATTENTION_OGD",),
    ("TWSE", "trading_halt"): ("TWSE_HALTS_OGD",),
    ("TWSE", "special_trading"): ("TWSE_SPECIAL_OGD",),
    ("TWSE", "split_trading"): ("TWSE_SPECIAL_OGD",),
    ("TWSE", "disposition"): ("TWSE_DISPOSAL_OGD",),
}
SOURCE_SEMANTICS = {
    "TPEX_SPECIAL_OGD": "dated_daily_roster",
    "TPEX_HALTS_HISTORY_OGD": "event_history",
    "TPEX_DISPOSAL_OGD": "announcement_with_interval",
    "TPEX_ATTENTION_OGD": "announcement",
    "TWSE_HALTS_OGD": "event_history",
    "TWSE_SPECIAL_OGD": "undated_current_roster",
    "TWSE_DISPOSAL_OGD": "announcement_with_interval",
}
_TPEX_FLAG_CODES = {
    "trading_halt": "halted",
    "special_trading": "special_trading",
    "split_trading": "split_trading",
    "management": "management",
}


def _local_start(day: str) -> str:
    return datetime.combine(date.fromisoformat(day), time(0, 0), TAIPEI_TIMEZONE).isoformat()


def _local_datetime(day: str, clock: str) -> str:
    return datetime.combine(date.fromisoformat(day), time.fromisoformat(clock), TAIPEI_TIMEZONE).isoformat()


# ---------------------------------------------------------------- calendar
def closed_dates_from_calendar(facts: Iterable[Mapping[str, object]]) -> Set[str]:
    """開休市日期表中列出的日期，除明示為「開始交易」「最後交易」者外都視為休市。"""
    closed: Set[str] = set()
    for fact in facts:
        if fact.get("kind") != "calendar_event":
            continue
        name = str(fact.get("name", ""))
        if "開始交易" in name or "最後交易" in name:
            continue
        closed.add(str(fact["event_date"]))
    return closed


def next_trading_session(decision_cutoff: str, closed_dates: Set[str]) -> Dict[str, str]:
    """cutoff 後第一個非週末、非休市日的 09:00–13:30（台北）。"""
    cutoff = parse_aware_time(decision_cutoff, "decision_cutoff", error=TradingStatusError).astimezone(TAIPEI_TIMEZONE)
    day = cutoff.date() + timedelta(days=1)
    for _ in range(40):
        if day.weekday() < 5 and day.isoformat() not in closed_dates:
            break
        day += timedelta(days=1)
    else:
        raise TradingStatusError("40 天內找不到交易日，行事曆資料可能錯誤")
    return {
        "start": datetime.combine(day, time(9, 0), TAIPEI_TIMEZONE).isoformat(),
        "end": datetime.combine(day, time(13, 30), TAIPEI_TIMEZONE).isoformat(),
    }


# ---------------------------------------------------------------- approvals
def load_approvals(path: Path) -> Dict[str, Mapping[str, object]]:
    """讀取人工核准清單；每個核准必須寫明核准者、時間、證據與允許時效。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("approved_sources")
    if not isinstance(entries, list):
        raise TradingStatusError("approved_sources 必須是陣列")
    approvals: Dict[str, Mapping[str, object]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise TradingStatusError("approved_sources[%d] 必須是物件" % index)
        source_id = str(entry.get("source_id", ""))
        if source_id not in SOURCE_SEMANTICS:
            raise TradingStatusError("approved_sources[%d] 不是可核准的交易狀態來源：%s" % (index, source_id))
        for field in ("approved_by", "approved_at", "evidence"):
            if not isinstance(entry.get(field), str) or not str(entry[field]).strip():
                raise TradingStatusError("approved_sources[%d].%s 必須是非空字串" % (index, field))
        parse_aware_time(entry["approved_at"], "approved_at", error=TradingStatusError)
        max_age = entry.get("max_age_hours")
        if not isinstance(max_age, (int, float)) or isinstance(max_age, bool) or max_age <= 0:
            raise TradingStatusError("approved_sources[%d].max_age_hours 必須是正數" % index)
        if source_id in approvals:
            raise TradingStatusError("來源重複核准：%s" % source_id)
        approvals[source_id] = dict(entry)
    return approvals


def load_not_applicable(path: Path) -> List[Dict[str, object]]:
    """讀取人工核准的「不適用類別」政策；每筆需有官方依據、證據與核准者。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("not_applicable", [])
    if not isinstance(entries, list):
        raise TradingStatusError("not_applicable 必須是陣列")
    result: List[Dict[str, object]] = []
    seen: Set[Tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise TradingStatusError("not_applicable[%d] 必須是物件" % index)
        key = (str(entry.get("market")), str(entry.get("category")))
        if key[0] not in {"TWSE", "TPEX"} or key[1] not in DEFAULT_REQUIRED_CATEGORIES:
            raise TradingStatusError("not_applicable[%d] 市場或類別無效：%s" % (index, "／".join(key)))
        if key in CATEGORY_SOURCES:
            raise TradingStatusError("not_applicable[%d] 已有對應來源，不得同時列為不適用：%s" % (index, "／".join(key)))
        for field in ("basis", "evidence", "approved_by", "approved_at"):
            if not isinstance(entry.get(field), str) or not str(entry[field]).strip():
                raise TradingStatusError("not_applicable[%d].%s 必須是非空字串" % (index, field))
        parse_aware_time(entry["approved_at"], "approved_at", error=TradingStatusError)
        if key in seen:
            raise TradingStatusError("not_applicable 重複：%s" % "／".join(key))
        seen.add(key)
        result.append(dict(entry))
    return result


def not_applicable_coverage(entries: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
    """已核准的不適用政策以 coverage 表示：完整且沒有紀錄，理由與證據保存在 reason 與雜湊。"""
    return [
        {
            "source_id": "POLICY_NOT_APPLICABLE:%s:%s" % (entry["market"], entry["category"]),
            "market": entry["market"],
            "category": entry["category"],
            "approval_status": "approved",
            "coverage_status": "complete",
            "semantics": "not_applicable_policy",
            "as_of": entry["approved_at"],
            "query_start": entry["approved_at"],
            "query_end": entry["approved_at"],
            "row_count": 0,
            "page_count": 1,
            "expected_page_count": 1,
            "raw_payload_id": None,
            "raw_payload_sha256": canonical_sha256(dict(entry)),
            "reason": "%s（證據：%s）" % (entry["basis"], entry["evidence"]),
        }
        for entry in entries
    ]


# ---------------------------------------------------------------- records
class OgdTradingStatusMapper:
    """由一次封存（manifest＋原始 CSV）建立固定交易池的交易狀態 records 與 coverage。"""

    def __init__(self, manifest: Mapping[str, object], capture_dir: Path):
        entries = manifest.get("sources")
        if not isinstance(entries, list):
            raise TradingStatusError("封存 manifest 缺少 sources")
        self.entries: Dict[str, Mapping[str, object]] = {}
        self.facts: Dict[str, List[Dict[str, object]]] = {}
        for entry in entries:
            source_id = str(entry.get("source_id"))
            if entry.get("status") != "captured_candidate_only":
                raise TradingStatusError("來源封存失敗：%s" % source_id)
            raw = (capture_dir / ("%s.csv" % source_id)).read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
                raise TradingStatusError("封存原始檔雜湊不一致：%s" % source_id)
            facts, _ = extract_ogd_candidate_facts(source_id, raw, fetched_at=str(entry["fetch_finished_at"]))
            self.entries[source_id] = entry
            self.facts[source_id] = facts

    def closed_dates(self) -> Set[str]:
        return closed_dates_from_calendar(self.facts.get("TWSE_CALENDAR_OGD", []))

    def build(
        self, request: TradingStatusRequest, approvals: Mapping[str, Mapping[str, object]]
    ) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
        universe = set(request.universe_symbols)
        records: List[Dict[str, object]] = []
        for source_id, facts in sorted(self.facts.items()):
            if source_id not in SOURCE_SEMANTICS:
                continue
            for fact in facts:
                if fact.get("symbol") in universe:
                    records.extend(self._records(source_id, fact))
            records.extend(self._halt_records(source_id, [fact for fact in facts if fact.get("symbol") in universe]))
        return records, self._coverage(request, approvals)

    def _record(
        self, source_id: str, fact: Mapping[str, object], category: str, status_code: str,
        effective_from: str, effective_to: Optional[str], suffix: str = "",
    ) -> Dict[str, object]:
        entry = self.entries[source_id]
        record_id = "%s:%s:%s%s" % (source_id, str(fact["source_row_sha256"])[:16], category, suffix)
        record: Dict[str, object] = {
            "record_id": record_id,
            "symbol": fact["symbol"],
            "market": fact["market"],
            "category": category,
            "status_code": status_code,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "published_at": None,
            "available_at": entry["fetch_finished_at"],
            "fetched_at": entry["fetch_finished_at"],
            "source_id": source_id,
            "source_url": entry["url"],
            "raw_payload_id": None,
            "raw_payload_sha256": entry["sha256"],
            "evidence_id": "trading-status:%s" % record_id,
            "version": "1",
            "parser_version": OGD_MAPPING_VERSION,
        }
        record["content_sha256"] = canonical_sha256(record)
        return record

    def _records(self, source_id: str, fact: Mapping[str, object]) -> List[Dict[str, object]]:
        kind = fact.get("kind")
        if kind == "dated_status_flags":
            # 每日名單沒有終止日：保守視為持續到下一份名單。
            return [
                self._record(source_id, fact, category, _TPEX_FLAG_CODES[category], _local_start(str(fact["content_date"])), None)
                for category, flagged in sorted(dict(fact["flags"]).items())
                if flagged
            ]
        if kind == "undated_special_roster":
            start = str(self.entries[source_id]["fetch_finished_at"])
            result = [self._record(source_id, fact, "special_trading", "special_trading", start, None)]
            if fact.get("split_trading"):
                result.append(self._record(source_id, fact, "split_trading", "split_trading", start, None))
            return result
        if kind == "disposition_announcement":
            interval = dict(fact["tentative_interval"])
            end = None if fact.get("extension_mentioned") else _local_start(
                (date.fromisoformat(interval["end_date"]) + timedelta(days=1)).isoformat()
            )
            return [self._record(source_id, fact, "disposition", "disposition", _local_start(interval["start_date"]), end)]
        if kind == "attention_notice":
            day = str(fact["announced_on"])
            end = _local_start((date.fromisoformat(day) + timedelta(days=2)).isoformat())
            return [self._record(source_id, fact, "attention", "attention", _local_start(day), end)]
        return []

    def _halt_records(self, source_id: str, facts: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
        """停復牌事件依股票與時間配對：暫停到下一個恢復為止，沒有恢復就持續有效。"""
        events: Dict[str, List[Mapping[str, object]]] = {}
        for fact in facts:
            if fact.get("kind") in {"halt_event", "resume_event"}:
                events.setdefault(str(fact["symbol"]), []).append(fact)
        records: List[Dict[str, object]] = []
        for symbol in sorted(events):
            ordered = sorted(events[symbol], key=lambda item: (item["event_date"], item["event_time"], item["kind"] != "halt_event"))
            for index, event in enumerate(ordered):
                if event["kind"] != "halt_event":
                    continue
                resume = next((later for later in ordered[index + 1:] if later["kind"] == "resume_event"), None)
                records.append(
                    self._record(
                        source_id, event, "trading_halt", "halted",
                        _local_datetime(str(event["event_date"]), str(event["event_time"])),
                        _local_datetime(str(resume["event_date"]), str(resume["event_time"])) if resume else None,
                        suffix=":%s" % event["event_date"],
                    )
                )
        return records

    def _coverage(
        self, request: TradingStatusRequest, approvals: Mapping[str, Mapping[str, object]]
    ) -> List[Dict[str, object]]:
        session_start = parse_aware_time(request.target_session_start, "target_session.start", error=TradingStatusError)
        coverage: List[Dict[str, object]] = []
        for (market, category), sources in sorted(CATEGORY_SOURCES.items()):
            for source_id in sources:
                entry = self.entries.get(source_id)
                if entry is None:
                    continue
                fetched = parse_aware_time(entry["fetch_finished_at"], "fetch_finished_at", error=TradingStatusError)
                approval = approvals.get(source_id)
                fresh = approval is not None and (session_start - fetched) <= timedelta(hours=float(approval["max_age_hours"]))
                reason = None
                if approval is None:
                    reason = "SOURCE_NOT_APPROVED"
                elif not fresh:
                    reason = "STALE_CAPTURE"
                coverage.append(
                    {
                        "source_id": source_id,
                        "market": market,
                        "category": category,
                        "approval_status": "approved" if approval is not None else "candidate",
                        "coverage_status": "complete" if approval is not None and fresh else "partial",
                        "semantics": SOURCE_SEMANTICS[source_id],
                        "as_of": entry["fetch_finished_at"],
                        "query_start": entry["fetch_started_at"],
                        "query_end": entry["fetch_finished_at"],
                        "row_count": int(entry.get("row_count", 0)),
                        "page_count": 1,
                        "expected_page_count": 1,
                        "raw_payload_id": None,
                        "raw_payload_sha256": entry["sha256"],
                        "reason": reason,
                    }
                )
        return coverage


def latest_capture_before(captures_root: Path, cutoff: str) -> Optional[Path]:
    """找出 fetch 完成時間不晚於 cutoff 的最新成功封存目錄。"""
    limit = parse_aware_time(cutoff, "decision_cutoff", error=TradingStatusError)
    best: Optional[Tuple[datetime, Path]] = None
    for manifest_path in sorted(captures_root.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sources = manifest.get("sources", [])
        if manifest.get("status") != "captured_candidate_only" or not sources:
            continue
        finished = max(parse_aware_time(item["fetch_finished_at"], "fetch_finished_at", error=TradingStatusError) for item in sources)
        if finished <= limit and (best is None or finished > best[0]):
            best = (finished, manifest_path.parent)
    return best[1] if best else None


def build_trading_status_from_capture(
    snapshot: Mapping[str, object],
    capture_dir: Optional[Path],
    approvals: Mapping[str, Mapping[str, object]],
    not_applicable: Sequence[Mapping[str, object]] = (),
) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, str]]:
    """以封存的官方 CSV 建立狀態包；沒有可用封存時不提供 records／coverage，逐檔 unknown。"""
    from .trading_status import TradingStatusBundleBuilder

    mapper = None
    if capture_dir is not None:
        mapper = OgdTradingStatusMapper(json.loads((capture_dir / "manifest.json").read_text(encoding="utf-8")), capture_dir)
    closed = mapper.closed_dates() if mapper is not None else set()
    session = next_trading_session(str(snapshot["decision_cutoff"]), closed)
    request = TradingStatusRequest.from_snapshot(
        snapshot, session["start"], session["end"],
        source_config_version="%s+approvals:%s" % (
            OGD_MAPPING_VERSION, canonical_sha256({"sources": dict(approvals), "not_applicable": list(not_applicable)})[:12]
        ),
    )
    records, coverage = mapper.build(request, approvals) if mapper is not None else ([], [])
    coverage = coverage + not_applicable_coverage(not_applicable)
    bundle, assessment = TradingStatusBundleBuilder(request, records, coverage).build()
    return bundle, assessment, session
