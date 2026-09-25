"""重算政府開放 CSV 與官方 JSON 的候選來源對照；不核准交易來源。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Mapping


_SOURCE_ID = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ROC_DATE = re.compile(r"^(\d{3})(?:/)?(\d{2})(?:/)?(\d{2})$")
_ROC_INTERVAL = re.compile(r"^\s*([^~～]+)\s*[~～]\s*([^~～]+)\s*$")


def roc_date_to_iso(value: str) -> str:
    """只轉換明確的民國日期，不推測發布時間或時區。"""

    if not isinstance(value, str):
        raise ValueError("民國日期必須是字串")
    match = _ROC_DATE.fullmatch(value.strip())
    if not match:
        raise ValueError("民國日期格式不明：%s" % value)
    year, month, day = (int(part) for part in match.groups())
    if year <= 0:
        raise ValueError("民國年份必須大於 0")
    try:
        return date(year + 1911, month, day).isoformat()
    except ValueError as error:
        raise ValueError("民國日期無效：%s" % value) from error


def roc_interval_to_iso(value: str) -> Dict[str, str]:
    """轉換處置欄位的明確日期範圍；順延條件仍須另行判讀。"""

    if not isinstance(value, str):
        raise ValueError("處置起訖時間必須是字串")
    match = _ROC_INTERVAL.fullmatch(value)
    if not match:
        raise ValueError("處置起訖時間格式不明：%s" % value)
    start, end = (roc_date_to_iso(part) for part in match.groups())
    if start > end:
        raise ValueError("處置起日不得晚於迄日")
    return {"start_date": start, "end_date": end}


def _date_diagnostics(source_id: str, rows: List[Mapping[str, str]]) -> Dict[str, object]:
    """分析內容日期，不把日期當成 available_at 或交易許可。"""

    if source_id.endswith("DISPOSAL_OGD"):
        published_field = "公布日期"
        interval_field = "處置起訖時間" if source_id.startswith("TPEX_") else "處置起迄時間"
        text_field = "處置內容"
        future_starts = 0
        extension_mentions = 0
        for row in rows:
            published = roc_date_to_iso(row[published_field])
            interval = roc_interval_to_iso(row[interval_field])
            if interval["start_date"] > published:
                future_starts += 1
            if "順延" in row[text_field]:
                extension_mentions += 1
        return {
            "date_semantics": "announcement_and_tentative_effective_interval",
            "future_effective_starts": future_starts,
            "extension_mentions": extension_mentions,
            "available_at_verified": False,
        }
    if source_id == "TPEX_HALTS_HISTORY_OGD":
        halt_events = 0
        resume_events = 0
        for row in rows:
            halt = row["暫停交易日期"].strip()
            resume = row["恢復交易日期"].strip()
            if bool(halt) == bool(resume):
                raise ValueError("櫃買歷史停復牌每列必須只有一種事件日期")
            roc_date_to_iso(halt or resume)
            halt_events += bool(halt)
            resume_events += bool(resume)
        return {
            "date_semantics": "separate_halt_or_resume_events",
            "halt_events": halt_events,
            "resume_events": resume_events,
            "current_status_inferred": False,
            "available_at_verified": False,
        }
    if source_id == "TWSE_HALTS_OGD":
        open_intervals = 0
        for row in rows:
            start = roc_date_to_iso(row["暫停交易日期"])
            resume = row["恢復交易日期"].strip()
            if resume:
                if roc_date_to_iso(resume) < start:
                    raise ValueError("恢復交易日期早於暫停交易日期")
            else:
                open_intervals += 1
        return {
            "date_semantics": "historical_halt_and_resume",
            "open_halt_intervals": open_intervals,
            "available_at_verified": False,
        }
    field = next((key for key in ("公告日期", "資料日期", "日期") if rows and key in rows[0]), None)
    if field:
        dates = set()
        for row in rows:
            value = row[field].strip()
            if not value:
                raise ValueError("內容日期空白")
            dates.add(roc_date_to_iso(value))
        return {"date_semantics": "content_date_only", "content_dates": sorted(dates), "available_at_verified": False}
    return {"date_semantics": "undetermined", "available_at_verified": False}


def verify_ogd_crosscheck(report: Mapping[str, Any], audit_dir: Path) -> Dict[str, object]:
    """逐來源重算雜湊、日期範圍及對應欄位；只證明封存檔內容一致。"""

    sources = report.get("sources")
    if not isinstance(sources, list) or not sources:
        return {"valid": False, "errors": ["sources 必須是非空陣列"], "sources": []}
    errors: List[str] = []
    results: List[Dict[str, object]] = []
    seen = set()
    for index, source in enumerate(sources):
        prefix = "sources[%d]" % index
        if not isinstance(source, Mapping):
            errors.append("%s 必須是物件" % prefix)
            continue
        source_id = source.get("source_id")
        json_source_id = source.get("json_source_id")
        if (
            not isinstance(source_id, str)
            or not _SOURCE_ID.fullmatch(source_id)
            or not source_id.endswith("_OGD")
            or not isinstance(json_source_id, str)
            or not _SOURCE_ID.fullmatch(json_source_id)
            or source_id != json_source_id + "_OGD"
        ):
            errors.append("%s 來源 ID 不合法" % prefix)
            continue
        if source_id in seen:
            errors.append("%s 來源 ID 重複" % prefix)
            continue
        seen.add(source_id)
        mappings = source.get("mapped_fields")
        if not isinstance(mappings, Mapping) or not mappings or any(
            not isinstance(left, str) or not left or not isinstance(right, str) or not right
            for left, right in mappings.items()
        ) or len(set(mappings.values())) != len(mappings):
            errors.append("%s mapped_fields 不合法" % prefix)
            continue
        normalization = source.get("normalization", "strip")
        if normalization not in {"strip", "remove_unicode_whitespace"}:
            errors.append("%s normalization 不合法" % prefix)
            continue
        csv_path = audit_dir / (source_id + ".csv")
        json_path = audit_dir / (json_source_id + ".json")
        try:
            raw = csv_path.read_bytes()
            json_rows = json.loads(json_path.read_text(encoding="utf-8"))
            if not isinstance(json_rows, list) or any(not isinstance(row, dict) for row in json_rows):
                raise ValueError("JSON 必須是物件陣列")
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError("CSV 欄位缺失或重複")
            csv_rows = list(reader)
            if any(None in row for row in csv_rows):
                raise ValueError("CSV 有多餘欄位")
            if any(value is None for row in csv_rows for value in row.values()):
                raise ValueError("CSV 有缺漏欄位")
            if any(left not in reader.fieldnames for left in mappings):
                raise ValueError("CSV 缺少對應欄位")
            if any(right not in row for row in json_rows for right in mappings.values()):
                raise ValueError("JSON 缺少對應欄位")
            digest = hashlib.sha256(raw).hexdigest()
            if digest != source.get("sha256") or len(raw) != source.get("size"):
                raise ValueError("CSV 原始檔雜湊或大小不一致")
            if len(csv_rows) != source.get("row_count") or len(json_rows) != source.get("json_row_count"):
                raise ValueError("CSV／JSON 原始列數不一致")
            date_counts = source.get("csv_date_counts")
            if date_counts is not None:
                if not isinstance(date_counts, Mapping):
                    raise ValueError("csv_date_counts 必須是物件")
                csv_date_field, json_date_field = next(iter(mappings.items()))
                actual_csv_dates = Counter(row[csv_date_field] for row in csv_rows)
                actual_json_dates = Counter(str(row[json_date_field]) for row in json_rows)
                if dict(actual_csv_dates) != dict(date_counts) or dict(actual_json_dates) != source.get("json_date_counts"):
                    raise ValueError("公告日期分布不一致")
                json_rows = [row for row in json_rows if str(row[json_date_field]) in actual_csv_dates]
                if len(json_rows) != source.get("json_rows_after_date_filter"):
                    raise ValueError("依日期篩選後列數不一致")
            normalize = (
                (lambda value: re.sub(r"\s+", "", value))
                if normalization == "remove_unicode_whitespace"
                else (lambda value: value.strip())
            )
            csv_values = Counter(
                tuple(normalize(row[left]) for left in mappings) for row in csv_rows
            )
            json_values = Counter(
                tuple(normalize(str(row[right])) for right in mappings.values())
                for row in json_rows
            )
            if csv_values != json_values:
                raise ValueError("對應欄位內容或列重數不一致")
            date_diagnostics = _date_diagnostics(source_id, csv_rows)
            symbol_field = next((key for key in ("證券代號", "代號") if key in reader.fieldnames), None)
            empty_symbol_rows = sum(not row[symbol_field].strip() for row in csv_rows) if symbol_field else 0
            results.append({
                "source_id": source_id,
                "csv_rows": len(csv_rows),
                "json_rows_compared": len(json_rows),
                "status": "matched_candidate_only",
                "empty_symbol_rows": empty_symbol_rows,
                **date_diagnostics,
            })
        except (OSError, UnicodeError, json.JSONDecodeError, csv.Error, ValueError, TypeError, KeyError) as error:
            errors.append("%s %s：%s" % (prefix, source_id, error))
    return {"valid": not errors, "errors": errors, "sources": results}
