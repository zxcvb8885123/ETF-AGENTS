"""Immutable on-disk storage for reproducible backtest runs."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Mapping

from etf_agent.core import canonical_sha256

from .contracts import BACKTEST_SCHEMA_VERSION, BacktestToolError


class BacktestRepository:
    def __init__(self, root: Path):
        self.root = root

    def save(self, run_id: str, artifacts: Mapping[str, Mapping[str, object]]) -> Path:
        self._validate_name(run_id, "run_id", r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
        for name in artifacts:
            self._validate_name(str(name), "artifact", r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        directory = self.root / run_id
        if directory.is_symlink():
            raise BacktestToolError("backtest run 目錄不得是符號連結")
        manifest = {
            "schema_version": BACKTEST_SCHEMA_VERSION,
            "run_id": run_id,
            "artifacts": {
                name: {"filename": name + ".json", "sha256": canonical_sha256(payload)}
                for name, payload in sorted(artifacts.items())
            },
        }
        manifest["manifest_sha256"] = canonical_sha256(manifest)
        if directory.exists():
            self.verify(run_id)
            current = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            if current != manifest:
                raise BacktestToolError("相同 backtest run_id 已存在不同內容")
            return directory
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".%s-" % run_id, dir=str(self.root)))
        try:
            for name, payload in artifacts.items():
                (temporary / (name + ".json")).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(str(temporary), str(directory))
        except Exception:
            for child in temporary.iterdir():
                child.unlink()
            temporary.rmdir()
            raise
        return directory

    def verify(self, run_id: str) -> Path:
        self._validate_name(run_id, "run_id", r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
        directory = self.root / run_id
        if directory.is_symlink():
            raise BacktestToolError("backtest run 目錄不得是符號連結")
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BacktestToolError("backtest manifest 無法讀取") from error
        body = dict(manifest)
        checksum = body.pop("manifest_sha256", None)
        if checksum != canonical_sha256(body) or manifest.get("run_id") != run_id:
            raise BacktestToolError("backtest manifest 不一致")
        expected = {"manifest.json"}
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise BacktestToolError("backtest manifest 缺少 artifacts")
        for name, meta in artifacts.items():
            self._validate_name(str(name), "artifact", r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
            if not isinstance(meta, Mapping) or meta.get("filename") != str(name) + ".json":
                raise BacktestToolError("backtest manifest artifact 檔名不一致")
            path = directory / str(meta["filename"])
            if path.is_symlink():
                raise BacktestToolError("backtest artifact 不得是符號連結")
            expected.add(path.name)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise BacktestToolError("backtest artifact 無法讀取：%s" % name) from error
            if not isinstance(payload, Mapping) or canonical_sha256(payload) != meta.get("sha256"):
                raise BacktestToolError("backtest artifact 與 manifest 不一致：%s" % name)
        actual = {item.name for item in directory.iterdir() if item.is_file()}
        if actual != expected:
            raise BacktestToolError("backtest run 檔案集合與 manifest 不一致")
        return directory

    @staticmethod
    def _validate_name(value: str, label: str, pattern: str) -> None:
        if not re.fullmatch(pattern, value):
            raise BacktestToolError("%s 不符合安全白名單" % label)
