"""PerceptionDataBundle 的 Provider 邊界；目前只有本機 JSON 實作。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Protocol

from .contracts import PerceptionToolError


class PerceptionDataProvider(Protocol):
    """Provider boundary for licensed, versioned perception data."""

    def load_bundle(self) -> Mapping[str, object]:
        ...


class JsonPerceptionDataProvider:
    """Load a saved provider bundle without performing network access."""

    def __init__(self, path: Path):
        self.path = path

    def load_bundle(self) -> Mapping[str, object]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PerceptionToolError("無法讀取 PerceptionDataBundle：%s" % error) from error
        if not isinstance(payload, Mapping):
            raise PerceptionToolError("PerceptionDataBundle 必須是 JSON 物件")
        return payload
