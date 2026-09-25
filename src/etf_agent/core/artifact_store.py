"""Immutable, hash-sealed run directories shared by Agent repositories.

A run is a directory of ``<artifact>.json`` files plus ``manifest.json`` that
records each artifact's canonical SHA-256 and a ``manifest_sha256`` seal. Runs
are written atomically, never overwritten with different content, and every
read goes through a full verification of the manifest and file set.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, Mapping, Type

from .canonical import canonical_sha256


RUN_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
ARTIFACT_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"


def _dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


class ImmutableRunStore:
    """Atomically save one immutable run and reject conflicting reuse.

    Subclasses bind ``schema_version`` and the Agent's own ``error`` type.
    """

    def __init__(self, root: Path, *, schema_version: str, error: Type[Exception] = ValueError):
        self.root = root
        self.schema_version = schema_version
        self.error = error

    def build_manifest(self, run_id: str, artifacts: Mapping[str, Mapping[str, object]]) -> Dict[str, object]:
        manifest: Dict[str, object] = {
            "schema_version": self.schema_version,
            "run_id": run_id,
            "artifacts": {
                name: {"filename": "%s.json" % name, "sha256": canonical_sha256(payload)}
                for name, payload in sorted(artifacts.items())
            },
        }
        manifest["manifest_sha256"] = canonical_sha256(manifest)
        return manifest

    def save(self, run_id: str, artifacts: Mapping[str, Mapping[str, object]]) -> Path:
        run_dir = self._run_dir(run_id)
        for name in artifacts:
            if not re.fullmatch(ARTIFACT_NAME_PATTERN, str(name)):
                raise self.error("artifact 名稱不符合安全白名單：%s" % name)
        manifest = self.build_manifest(run_id, artifacts)
        if run_dir.exists():
            existing = run_dir / "manifest.json"
            if not existing.exists():
                raise self.error("既有 run 缺少 manifest，拒絕覆蓋")
            try:
                current = json.loads(existing.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise self.error("既有 manifest 無法讀取") from error
            self.verify(run_id)
            if current != manifest:
                raise self.error("相同 run_id 已存在不同內容，拒絕覆蓋")
            return run_dir
        self.root.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=".%s-" % run_id, dir=str(self.root)))
        try:
            for name, payload in sorted(artifacts.items()):
                (temp_dir / ("%s.json" % name)).write_text(_dump(payload), encoding="utf-8")
            (temp_dir / "manifest.json").write_text(_dump(manifest), encoding="utf-8")
            os.replace(str(temp_dir), str(run_dir))
        except Exception:
            for child in temp_dir.iterdir():
                child.unlink()
            temp_dir.rmdir()
            raise
        return run_dir

    def verify(self, run_id: str) -> Path:
        run_dir = self._run_dir(run_id)
        try:
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise self.error("run manifest 無法讀取") from error
        if not isinstance(manifest, Mapping):
            raise self.error("run manifest 必須是物件")
        body = dict(manifest)
        recorded = body.pop("manifest_sha256", None)
        if recorded != canonical_sha256(body):
            raise self.error("manifest_sha256 與內容不一致")
        if manifest.get("run_id") != run_id or manifest.get("schema_version") != self.schema_version:
            raise self.error("manifest 身分欄位不一致")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise self.error("manifest.artifacts 必須是物件")
        expected_files = {"manifest.json"}
        for name, metadata in artifacts.items():
            if not re.fullmatch(ARTIFACT_NAME_PATTERN, str(name)):
                raise self.error("manifest 含不安全 artifact 名稱")
            if not isinstance(metadata, Mapping) or metadata.get("filename") != "%s.json" % name:
                raise self.error("manifest artifact 檔名不一致：%s" % name)
            path = run_dir / str(metadata["filename"])
            if path.is_symlink():
                raise self.error("artifact 不得是符號連結：%s" % name)
            expected_files.add(path.name)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise self.error("artifact 無法讀取：%s" % name) from error
            if not isinstance(payload, Mapping) or canonical_sha256(payload) != metadata.get("sha256"):
                raise self.error("artifact 內容與 manifest 不一致：%s" % name)
        actual_files = {path.name for path in run_dir.iterdir() if path.is_file()}
        if actual_files != expected_files:
            raise self.error("run 目錄檔案集合與 manifest 不一致")
        return run_dir

    def _run_dir(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not re.fullmatch(RUN_ID_PATTERN, run_id):
            raise self.error("run_id 只能包含英數、點、底線與連字號")
        run_dir = self.root / run_id
        if run_dir.is_symlink():
            raise self.error("run 目錄不得是符號連結")
        return run_dir
