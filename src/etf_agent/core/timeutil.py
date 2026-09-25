"""Timezone-aware time parsing; naive timestamps always fail closed."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Type


def parse_aware_time(
    value: object,
    field: str,
    *,
    error: Type[Exception] = ValueError,
    to_utc: bool = True,
) -> datetime:
    """Parse an ISO-8601 string that must carry a timezone offset.

    ``error`` lets each Agent keep its own exception type so callers catch
    the failure at the correct boundary.
    """
    if not isinstance(value, str) or not value.strip():
        raise error("%s 必須是包含時區的時間字串" % field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise error("%s 無法解析：%s" % (field, value)) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise error("%s 必須包含時區" % field)
    return parsed.astimezone(timezone.utc) if to_utc else parsed
