"""Shared primitives for whole-lot execution and daily account ledgers."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from etf_agent.core import parse_aware_time, parse_decimal


MONEY = Decimal("1")


class LedgerError(ValueError):
    """Raised when a fill or ledger transition would break cash or share accounting."""


def money_string(value: Decimal) -> str:
    """Round to whole TWD for ledger output."""
    return str(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def decimal_value(value: object, field: str) -> Decimal:
    return parse_decimal(value, field, error=LedgerError, parse_message="%(field)s 必須是有限數值")


def parse_time(value: object, field: str) -> datetime:
    return parse_aware_time(value, field, error=LedgerError)
