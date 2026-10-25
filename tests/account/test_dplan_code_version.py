"""D-Plan code_version：只看影響決策的路徑，未提交修改附內容雜湊並另存可還原的快照。"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from etf_agent.competition.service import DPlanExportService

SCHEMA_PATTERN = re.compile(r"^[A-Za-z0-9_.:/\-]{1,64}$")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


class CodeVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "test@example.com")
        _git(self.root, "config", "user.name", "test")
        (self.root / ".gitignore").write_text("artifacts/*\n", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "rule.py").write_text("LIMIT = 1\n", encoding="utf-8")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "init")
        self.revision = _git(self.root, "rev-parse", "--short=12", "HEAD")
        self.service = DPlanExportService(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_clean_tree_is_plain_revision(self):
        self.assertEqual(self.service.code_version(), "git:%s" % self.revision)

    def test_untracked_files_outside_code_paths_do_not_mark_dirty(self):
        (self.root / "game_docs").mkdir()
        (self.root / "game_docs" / "rules.pdf").write_bytes(b"%PDF")
        (self.root / ".archify").mkdir()
        (self.root / ".archify" / "note.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.service.code_version(), "git:%s" % self.revision)

    def test_dirty_version_hashes_content_and_saves_snapshot(self):
        (self.root / "src" / "rule.py").write_text("LIMIT = 2\n", encoding="utf-8")
        (self.root / "src" / "new_prompt.md").write_text("prompt\n", encoding="utf-8")
        version = self.service.code_version()
        self.assertRegex(version, r"^git:%s-dirty-[0-9a-f]{12}$" % self.revision)
        self.assertRegex(version, SCHEMA_PATTERN)
        self.assertEqual(self.service.code_version(), version)
        snapshot = self.root / "artifacts" / "code_versions" / version.replace(":", "_")
        self.assertIn("LIMIT = 2", (snapshot / "changes.patch").read_text(encoding="utf-8"))
        self.assertEqual((snapshot / "untracked" / "src" / "new_prompt.md").read_text(encoding="utf-8"), "prompt\n")
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["code_version"], version)
        self.assertEqual(manifest["base_revision"], self.revision)

    def test_different_uncommitted_content_changes_version(self):
        (self.root / "src" / "rule.py").write_text("LIMIT = 2\n", encoding="utf-8")
        first = self.service.code_version()
        (self.root / "src" / "rule.py").write_text("LIMIT = 3\n", encoding="utf-8")
        self.assertNotEqual(self.service.code_version(), first)


if __name__ == "__main__":
    unittest.main()
