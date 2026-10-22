"""校準觀測的不可變保存。獨立根目錄，不寫進 Decision run。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Mapping

from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.core.run_layout import list_run_dirs

from .contracts import SCHEMA_VERSION, CalibrationError


class CalibrationRepository(ImmutableRunStore):
    def __init__(self, root: Path):
        super().__init__(Path(root), schema_version=SCHEMA_VERSION, error=CalibrationError, dated=True)

    def observations(self) -> List[Mapping[str, object]]:
        """所有已驗證的觀測；任何一份遭竄改即拒絕整批。"""
        result = []
        for run_dir in list_run_dirs(self.root):
            self.verify(run_dir.name)
            payload = json.loads((run_dir / "observation.json").read_text(encoding="utf-8"))
            result.append(payload)
        return result
