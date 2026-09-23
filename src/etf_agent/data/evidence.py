"""Deterministic source-evidence helpers for ResearchSnapshot."""

from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote

from etf_agent.contracts import SourceEvidence


TAIPEI_TIMEZONE = timezone(timedelta(hours=8))


def source_authority(source: str) -> str:
    normalized = source.upper()
    if "MOPS" in normalized:
        return "mops"
    if normalized.startswith("TWSE"):
        return "twse"
    if normalized.startswith("TPEX"):
        return "tpex"
    if normalized.startswith("TAIFEX"):
        return "taifex"
    if normalized.startswith("YAHOO"):
        return "vendor"
    return "other"


def taipei_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("來源時間必須包含時區")
    return parsed.astimezone(TAIPEI_TIMEZONE).isoformat()


def daily_content_as_of(trade_date: str) -> str:
    parsed = date.fromisoformat(trade_date)
    return datetime.combine(parsed, time(hour=13, minute=30), TAIPEI_TIMEZONE).isoformat()


def public_price_url(source: str, endpoint: str, symbol: str) -> str:
    if source.upper() == "YAHOO_FINANCE":
        return "https://finance.yahoo.com/quote/%s/history" % quote(symbol, safe=".")
    return endpoint


class SourceEvidenceBuilder:
    """把已選定的資料庫紀錄轉成可稽核的來源證據物件。"""

    def build_for_prices(self, rows):
        evidence = []
        seen = set()
        for row in rows:
            evidence_id = self.price_evidence_id(row)
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            evidence.append(
                SourceEvidence(
                    evidence_id=evidence_id,
                    source=str(row["source"]),
                    authority=source_authority(str(row["source"])),
                    data_type="daily_price",
                    url=public_price_url(
                        str(row["source"]),
                        str(row["endpoint"]),
                        str(row["symbol"]),
                    ),
                    content_as_of=daily_content_as_of(str(row["trade_date"])),
                    fetched_at=taipei_timestamp(str(row["payload_fetched_at"])),
                    content_sha256=str(row["raw_sha256"]),
                    raw_payload_id=int(row["raw_payload_id"]),
                )
            )
        return evidence

    def build_for_document(self, row) -> SourceEvidence:
        content_as_of = taipei_timestamp(str(row["published_at"]))
        published_at = content_as_of
        if str(row["document_type"]) == "financial_statement":
            # 財報彙總端點只提供「出表日期」，不能把它宣稱為公司發布時間。
            # source_published_at 目前應保留 null；仍以 available_at/fetched_at
            # 防止把今日回補內容放進過去的快照。
            if row["source_published_at"] is not None:
                published_at = taipei_timestamp(str(row["source_published_at"]))
            else:
                published_at = None
        return SourceEvidence(
            evidence_id=self.document_evidence_id(row),
            source=str(row["source"]),
            authority=source_authority(str(row["source"])),
            data_type=str(row["document_type"]),
            url=str(row["source_url"]),
            content_as_of=content_as_of,
            published_at=published_at,
            fetched_at=taipei_timestamp(str(row["fetched_at"])),
            content_sha256=str(row["content_sha256"]),
            raw_payload_id=int(row["raw_payload_id"]),
        )

    @staticmethod
    def price_evidence_id(row) -> str:
        return "price:%d:%s" % (
            int(row["raw_payload_id"]),
            str(row["trade_date"]),
        )

    @staticmethod
    def document_evidence_id(row) -> str:
        return "document:%d" % int(row["id"])
