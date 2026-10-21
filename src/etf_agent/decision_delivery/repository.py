"""不可變交付封存及有序、原子 latest 索引。"""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from etf_agent.core import parse_aware_time, canonical_sha256
from etf_agent.core.artifact_store import ImmutableRunStore
from .contracts import DeliveryError, VERSION


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".publish-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def publication_lock(root, filename=".publish.lock"):
    root.mkdir(parents=True, exist_ok=True)
    with (root / filename).open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise DeliveryError("另一交付正在發布，請重試") from error
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class DeliveryRepository(ImmutableRunStore):
    def __init__(self, root):
        self.delivery_root = Path(root)
        super().__init__(self.delivery_root / "runs", schema_version=VERSION, error=DeliveryError)

    def save_delivery(self, run_id, artifacts):
        path = self.save(run_id, artifacts)
        # Markdown 精確文字封存在 rendering.json；閱讀副本不混入 core 的 JSON-only run。
        view = self.delivery_root / "views" / run_id / "report.md"
        expected = artifacts["rendering"]["markdown"]
        with publication_lock(self.delivery_root):
            if view.exists() and view.read_text(encoding="utf-8") != expected:
                raise DeliveryError("既有閱讀副本遭修改，拒絕覆寫")
            if not view.exists():
                atomic_text(view, expected)
        return path, view

    def verify_delivery(self, run_id):
        path = self.verify(run_id)
        artifacts = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in path.glob("*.json") if p.name != "manifest.json"}
        if run_id != "delivery-" + canonical_sha256(artifacts)[:32]:
            raise DeliveryError("交付識別與內容不一致")
        view = self.delivery_root / "views" / run_id / "report.md"
        if view.is_symlink() or view.parent.is_symlink():
            raise DeliveryError("交付閱讀副本不得是符號連結")
        if view.read_text(encoding="utf-8") != artifacts["rendering"]["markdown"]:
            raise DeliveryError("閱讀副本與封存 Markdown 不一致")
        return artifacts

    def publish(self, run_id, reports_root, started_at):
        artifacts = self.verify_delivery(run_id)
        started = parse_aware_time(started_at, "started_at", error=DeliveryError)
        root = Path(reports_root)
        with publication_lock(root):
            latest = root / "latest.json"
            if latest.exists():
                old = json.loads(latest.read_text(encoding="utf-8"))
                # 舊版報告索引只有 generated_at；允許依其發布時間遷移，但不接受未知格式。
                old_time = old.get("started_at")
                if old_time is None and old.get("schema_version") == "1.0" and old.get("workflow_run_id"):
                    old_time = old.get("generated_at")
                previous = parse_aware_time(old_time, "舊報告時間", error=DeliveryError)
                if previous > started:
                    return False
                if previous == started and old.get("delivery_id") != run_id:
                    if "delivery_id" not in old:
                        raise DeliveryError("舊報告與本次發布同一時間，拒絕覆寫")
                    old_ready = old["status"]["report_status"] == "ready"
                    new_ready = artifacts["delivery_status"]["report_status"] == "ready"
                    invalidated_source = (
                        artifacts["delivery_status"].get("report_status") == "blocked"
                        and artifacts["delivery_status"].get("run_id") is not None
                        and old.get("request", {}).get("decision_run_id") == artifacts["delivery_status"].get("run_id")
                    )
                    if old_ready and not new_ready and not invalidated_source:
                        return False
                    if old_ready == new_ready:
                        raise DeliveryError("相同發布順序對應不同交付，拒絕覆寫")
            payload = {"delivery_id": run_id, "started_at": started_at,
                "request": artifacts["request"],
                "repository": str(self.delivery_root.resolve()), "status": artifacts["delivery_status"],
                "report": str((self.delivery_root / "views" / run_id / "report.md").resolve())}
            atomic_text(latest, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            # latest.json 是權威；捷徑含 ID，消費端可偵測跨檔中斷。
            atomic_text(root / "latest.md", "<!-- delivery_id: %s -->\n%s" % (run_id, artifacts["rendering"]["markdown"]))
        return True
