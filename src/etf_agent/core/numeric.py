"""Finite Decimal parsing and stable Decimal text."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional, Type


def parse_decimal(
    value: object,
    field: str,
    *,
    error: Type[Exception] = ValueError,
    reject_bool: bool = False,
    parse_message: Optional[str] = None,
) -> Decimal:
    """Parse a finite Decimal; NaN and Infinity always fail closed.

    ``parse_message`` overrides the unparsable-value message; it may use
    ``%(field)s`` and ``%(value)s``.
    """
    template = parse_message or "%(field)s 無法解析數值：%(value)s"
    if reject_bool and (isinstance(value, bool) or value is None):
        raise error(template % {"field": field, "value": value})
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise error(template % {"field": field, "value": value}) from exc
    if not result.is_finite():
        raise error("%s 必須是有限數值" % field)
    return result


def decimal_string(value: Decimal) -> str:
    """Render a Decimal without exponent or trailing zeros; negative zero is ``0``."""
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text
