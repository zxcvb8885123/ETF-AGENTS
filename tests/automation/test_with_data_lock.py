import fcntl
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load_module():
    spec = importlib.util.spec_from_file_location("with_data_lock", ROOT / "scripts" / "with_data_lock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DataLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def hold(self, path):
        handle = path.open("a+b")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle

    def test_free_lock_runs_command_and_returns_its_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = self.module.main(["--wait-seconds", "0", "--", sys.executable, "-c", "raise SystemExit(3)"], root=root)
            self.assertEqual(code, 3)
            # 指令結束後鎖已釋放：能再取一次。
            handle = self.module.acquire(root / self.module.LOCK_RELATIVE_PATH, 0)
            self.assertIsNotNone(handle)
            handle.close()

    def test_busy_lock_times_out_without_running_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "ran"
            path = root / self.module.LOCK_RELATIVE_PATH
            path.parent.mkdir(parents=True)
            holder = self.hold(path)
            notices = []
            stderr = io.StringIO()
            try:
                with patch.object(self.module, "notify", notices.append), redirect_stderr(stderr):
                    code = self.module.main(
                        ["--wait-seconds", "0", "--", sys.executable, "-c", "open(%r, 'w').close()" % str(marker)], root=root)
            finally:
                holder.close()
            self.assertEqual(code, self.module.TIMEOUT_EXIT)
            self.assertFalse(marker.exists())
            self.assertEqual(len(notices), 1)
            self.assertIn("等待", stderr.getvalue())

    def test_waits_until_holder_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily" / ".data.lock"
            path.parent.mkdir()
            holder = self.hold(path)
            clock = {"now": 0.0}
            sleeps = []

            def sleep(seconds):
                sleeps.append(seconds)
                clock["now"] += seconds
                if len(sleeps) == 2:
                    holder.close()

            handle = self.module.acquire(path, 60, poll_seconds=5, sleep=sleep, monotonic=lambda: clock["now"])
            self.assertIsNotNone(handle)
            self.assertEqual(sleeps, [5, 5])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pid"], __import__("os").getpid())
            handle.close()

    def test_non_busy_os_error_is_not_swallowed(self):
        with tempfile.TemporaryDirectory() as directory:
            notices = []
            with patch.object(self.module.fcntl, "flock", side_effect=PermissionError("拒絕存取")), \
                    patch.object(self.module, "notify", notices.append), redirect_stderr(io.StringIO()):
                code = self.module.main(["--wait-seconds", "0", "--", sys.executable, "-c", "pass"], root=Path(directory))
            self.assertEqual(code, 1)
            self.assertEqual(len(notices), 1)

    def test_requires_a_command(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                self.module.main(["--wait-seconds", "0"], root=Path(directory))
        self.assertEqual(caught.exception.code, 2)


class ScheduleWiringTests(unittest.TestCase):
    """資料與新聞排程入口必須經過資料抓取鎖（含 21:00 決策自己呼叫的 start.sh daily）。"""

    def test_start_script_and_news_script_use_the_lock(self):
        start = (ROOT / "start.sh").read_text(encoding="utf-8")
        news = (ROOT / "scripts" / "run_finmind_news.sh").read_text(encoding="utf-8")
        for text in (start, news):
            self.assertIn("with_data_lock.py", text)
            self.assertIn("ETF_DATA_LOCK_HELD", text)
        for mode in ('"data"', '"auto"', '"daily"'):
            self.assertIn('[ "$MODE" = %s ]' % mode, start)


if __name__ == "__main__":
    unittest.main()
