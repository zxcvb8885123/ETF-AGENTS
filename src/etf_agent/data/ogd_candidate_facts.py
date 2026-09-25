"""政府開放 CSV 的日期精度候選事實；不產生交易許可或正式 coverage。"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping, Tuple

from etf_agent.core import canonical_sha256
from .source_audit import roc_date_to_iso, roc_interval_to_iso


_SOURCE_MARKET = {
    "TPEX_SPECIAL_OGD": "TPEX",
    "TPEX_ATTENTION_OGD": "TPEX",
    "TPEX_DISPOSAL_OGD": "TPEX",
    "TPEX_HALTS_HISTORY_OGD": "TPEX",
    "TWSE_SPECIAL_OGD": "TWSE",
    "TWSE_DISPOSAL_OGD": "TWSE",
    "TWSE_HALTS_OGD": "TWSE",
    "TWSE_CALENDAR_OGD": "TWSE",
}
_SYMBOL = re.compile(r"^[0-9]{4}$")
_TIME = re.compile(r"^[0-9]{6}$")


def _row_hash(row: Mapping[str, str]) -> str:
    return canonical_sha256(row)


def _roc_time(value: str) -> str:
    if not _TIME.fullmatch(value.strip()):
        raise ValueError("事件時間格式不明")
    raw = value.strip()
    try:
        return datetime.strptime(raw, "%H%M%S").time().isoformat()
    except ValueError as error:
        raise ValueError("事件時間無效") from error


def _event(row: Mapping[str, str], date_field: str, time_field: str, kind: str) -> Dict[str, object]:
    return {
        "kind": kind,
        "event_date": roc_date_to_iso(row[date_field]),
        "event_time": _roc_time(row[time_field]),
    }


def extract_ogd_candidate_facts(
    source_id: str, raw: bytes, *, fetched_at: str
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    """依官方欄位轉換候選事實；無時段／不完整歷史一律不推導 allowed。"""

    market = _SOURCE_MARKET.get(source_id)
    if market is None:
        raise ValueError("未支援的政府開放來源")
    try:
        fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("fetched_at 無法解析") from error
    if fetched.tzinfo is None:
        raise ValueError("fetched_at 必須含時區")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError("CSV 欄位缺失或重複")
    rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("CSV 列欄位數不一致")
    required = {
        "TPEX_SPECIAL_OGD": {"資料日期", "證券代號", "變更交易", "分盤交易", "管理股票", "停止交易"},
        "TPEX_ATTENTION_OGD": {"公告日期", "證券代號", "注意交易資訊"},
        "TPEX_DISPOSAL_OGD": {"公布日期", "證券代號", "處置起訖時間", "處置內容"},
        "TPEX_HALTS_HISTORY_OGD": {"資料日期", "證券代號", "暫停交易日期", "暫停交易時間", "恢復交易日期", "恢復交易時間"},
        "TWSE_SPECIAL_OGD": {"證券代號", "分盤集合競價(以**表示)"},
        "TWSE_DISPOSAL_OGD": {"公布日期", "證券代號", "處置起迄時間", "處置內容"},
        "TWSE_HALTS_OGD": {"證券代號", "暫停交易日期", "暫停交易時間", "恢復交易日期", "恢復交易時間"},
        "TWSE_CALENDAR_OGD": {"日期", "名稱", "說明"},
    }[source_id]
    if required - set(reader.fieldnames):
        raise ValueError("CSV 缺少必要欄位：%s" % ", ".join(sorted(required - set(reader.fieldnames))))
    facts: List[Dict[str, object]] = []
    excluded_nonstock = 0
    for index, row in enumerate(rows):
        try:
            if source_id == "TWSE_CALENDAR_OGD":
                facts.append({
                    "source_id": source_id,
                    "market": market,
                    "kind": "calendar_event",
                    "event_date": roc_date_to_iso(row["日期"]),
                    "name": row["名稱"].strip(),
                    "description": row["說明"].strip(),
                    "source_row_sha256": _row_hash(row),
                    "available_at": fetched_at,
                    "candidate_only": True,
                })
                continue
            code = row["證券代號"].strip()
            if not _SYMBOL.fullmatch(code):
                excluded_nonstock += 1
                continue
            base = {
                "source_id": source_id,
                "market": market,
                "symbol": code + (".TWO" if market == "TPEX" else ".TW"),
                "source_row_sha256": _row_hash(row),
                "available_at": fetched_at,
                "candidate_only": True,
            }
            if source_id == "TPEX_SPECIAL_OGD":
                flags = {}
                for column, category in (
                    ("變更交易", "special_trading"),
                    ("分盤交易", "split_trading"),
                    ("管理股票", "management"),
                    ("停止交易", "trading_halt"),
                ):
                    value = row[column].strip()
                    if value not in {"", "Y", "Ｙ"}:
                        raise ValueError("狀態旗標不明：%s" % column)
                    flags[category] = bool(value)
                facts.append({**base, "kind": "dated_status_flags", "content_date": roc_date_to_iso(row["資料日期"]), "flags": flags})
            elif source_id == "TWSE_SPECIAL_OGD":
                split = row["分盤集合競價(以**表示)"].strip()
                if split not in {"", "**"}:
                    raise ValueError("分盤集合競價旗標不明")
                facts.append({**base, "kind": "undated_special_roster", "split_trading": split == "**", "content_date": None})
            elif source_id == "TPEX_ATTENTION_OGD":
                facts.append({**base, "kind": "attention_notice", "announced_on": roc_date_to_iso(row["公告日期"]), "detail": row["注意交易資訊"].strip()})
            elif source_id.endswith("DISPOSAL_OGD"):
                announced = roc_date_to_iso(row["公布日期"])
                interval = roc_interval_to_iso(row["處置起訖時間"] if market == "TPEX" else row["處置起迄時間"])
                facts.append({**base, "kind": "disposition_announcement", "announced_on": announced, "tentative_interval": interval, "extension_mentioned": "順延" in row["處置內容"]})
            elif source_id == "TPEX_HALTS_HISTORY_OGD":
                halt = row["暫停交易日期"].strip()
                resume = row["恢復交易日期"].strip()
                if bool(halt) == bool(resume):
                    raise ValueError("歷史停復牌列必須只有一種事件")
                event = _event(row, "暫停交易日期", "暫停交易時間", "halt_event") if halt else _event(row, "恢復交易日期", "恢復交易時間", "resume_event")
                if str(int(row["資料日期"]) + 1911) != event["event_date"][:4]:
                    raise ValueError("歷史停復牌資料年份不一致")
                facts.append({**base, **event})
            elif source_id == "TWSE_HALTS_OGD":
                halt_event = _event(row, "暫停交易日期", "暫停交易時間", "halt_event")
                facts.append({**base, **halt_event})
                if row["恢復交易日期"].strip():
                    resume_event = _event(row, "恢復交易日期", "恢復交易時間", "resume_event")
                    if (resume_event["event_date"], resume_event["event_time"]) < (halt_event["event_date"], halt_event["event_time"]):
                        raise ValueError("恢復交易早於暫停交易")
                    facts.append({**base, **resume_event})
        except (ValueError, KeyError) as error:
            raise ValueError("%s 第 %d 列：%s" % (source_id, index + 1, error)) from error
    summary = {
        "source_id": source_id,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "row_count": len(rows),
        "candidate_fact_count": len(facts),
        "excluded_nonstock_rows": excluded_nonstock,
        "approved": False,
        "complete_coverage_verified": False,
    }
    return facts, summary


def build_candidate_fact_report(
    manifest: Mapping[str, object], capture_dir: Path, universe_symbols: set[str]
) -> Dict[str, object]:
    """以封存檔重建固定交易池命中；不把候選命中當作可交易覆蓋。"""

    entries = manifest.get("sources")
    if not isinstance(entries, list) or len(entries) != len(_SOURCE_MARKET):
        raise ValueError("封存清單必須包含八份來源")
    if {entry.get("source_id") for entry in entries if isinstance(entry, dict)} != set(_SOURCE_MARKET):
        raise ValueError("封存來源 ID 不完整或重複")
    if len(universe_symbols) != 150:
        raise ValueError("固定交易池必須恰有 150 檔")
    summaries = []
    hits = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "captured_candidate_only":
            raise ValueError("來源封存失敗，不得建立候選事實報告")
        source_id = entry["source_id"]
        raw = (capture_dir / (source_id + ".csv")).read_bytes()
        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha != entry.get("sha256"):
            raise ValueError("封存原始檔雜湊不一致：%s" % source_id)
        facts, summary = extract_ogd_candidate_facts(
            source_id, raw, fetched_at=str(entry.get("fetch_finished_at", ""))
        )
        if summary["row_count"] != entry.get("row_count"):
            raise ValueError("封存列數不一致：%s" % source_id)
        summary["universe_fact_count"] = sum(
            fact.get("symbol") in universe_symbols for fact in facts
        )
        summaries.append(summary)
        hits.extend(fact for fact in facts if fact.get("symbol") in universe_symbols)
    return {
        "run_id": manifest.get("run_id"),
        "status": "candidate_facts_only",
        "approved_sources": 0,
        "formal_tradability_assessments": 0,
        "universe_size": len(universe_symbols),
        "candidate_hit_symbols": sorted({str(fact["symbol"]) for fact in hits}),
        "source_summaries": summaries,
        "facts_in_universe": hits,
    }
