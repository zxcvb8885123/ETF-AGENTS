"""反思封存與套用歷史。放在 var/（不在 artifacts/），清空 artifacts 或重開帳戶不會遺失。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Mapping

from etf_agent.core.artifact_store import ImmutableRunStore

from .contracts import POLICY_ROOT, SCHEMA_VERSION, ReflectionError


def atomic_write_json(path: Path, payload: object) -> None:
    """同目錄暫存檔再 rename；中途失敗不會留下半份檔案。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


class ReflectionRepository(ImmutableRunStore):
    def __init__(self, root: Path):
        self.base = Path(root) / POLICY_ROOT
        super().__init__(self.base / "runs", schema_version=SCHEMA_VERSION, error=ReflectionError)

    @property
    def history_path(self) -> Path:
        return self.base / "history.json"

    @property
    def policy_backups(self) -> Path:
        return self.base / "policies"

    def history(self) -> List[Dict[str, object]]:
        if not self.history_path.exists():
            return []
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ReflectionError("套用歷史無法讀取：%s" % error) from error
        if not isinstance(data, list):
            raise ReflectionError("套用歷史格式錯誤")
        return data

    def append_history(self, entry: Mapping[str, object]) -> None:
        atomic_write_json(self.history_path, self.history() + [dict(entry)])
