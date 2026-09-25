"""Provider-neutral primitives shared by every Agent: hashing, time and numbers.

These helpers are the single definition of canonical JSON hashing, timezone-aware
time parsing and finite Decimal parsing. Validators across Agents recompute hashes
and cutoffs with the same functions, so they must stay dependency-free.
"""

from .canonical import canonical_json, canonical_sha256, content_sha256
from .numeric import decimal_string, parse_decimal
from .timeutil import parse_aware_time

__all__ = [
    "canonical_json",
    "canonical_sha256",
    "content_sha256",
    "decimal_string",
    "parse_aware_time",
    "parse_decimal",
]
