"""不可變保存：每次抓取一個 run（``valuation-<UTC 時間>``），依資料抓取日分資料夾；更正另存新 run，不覆蓋。"""

from __future__ import annotations

from pathlib import Path

from etf_agent.core.artifact_store import ImmutableRunStore

from .contracts import SCHEMA_VERSION, ValuationError


def valuation_store(root: Path) -> ImmutableRunStore:
    return ImmutableRunStore(root, schema_version=SCHEMA_VERSION, error=ValuationError, dated=True)
