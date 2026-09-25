"""Canonical JSON encoding and content hashes shared by all artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Mapping


def canonical_json(payload: object) -> str:
    """Encode JSON with sorted keys, compact separators and raw Unicode."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(payload: object) -> str:
    """Hash the canonical JSON encoding of a payload."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def content_sha256(payload: Mapping[str, object], field: str = "content_sha256") -> str:
    """Hash an artifact without its self-referential hash field."""
    body = dict(payload)
    body.pop(field, None)
    return canonical_sha256(body)
