import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAIPEI = timezone(timedelta(hours=8))


class DailyLogTests(unittest.TestCase):
    """run_daily_pipeline.py 的一天一檔日誌：同時寫終端與檔案、只清掉符合命名格式的舊檔。"""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("run_daily_pipeline_log", ROOT / "scripts" / "run_daily_pipeline.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def tearDown(self):
        self.module.close_daily_log()

    def test_log_writes_to_terminal_and_taipei_dated_file(self):
        with tempfile.TemporaryDirectory() as directory:
            logs_root = Path(directory) / "var" / "logs"
            # UTC 2026-10-22 23:30 是台北 2026-10-23 07:30，檔名要用台北日期。
            path = self.module.open_daily_log(logs_root, datetime(2026, 10, 22, 23, 30, tzinfo=timezone.utc))
            self.assertEqual(path, logs_root / "daily-2026-10-23.log")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.module.log("第一行")
                self.module.log("第二行")
            self.module.close_daily_log()
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(lines[0].endswith("] 第一行"))
            self.assertEqual(stdout.getvalue().splitlines(), lines)
            # 再次開啟同一天是附加，不覆寫。
            self.module.open_daily_log(logs_root, datetime(2026, 10, 23, 8, 0, tzinfo=TAIPEI))
            with redirect_stdout(io.StringIO()):
                self.module.log("第三行")
            self.module.close_daily_log()
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)

    def test_log_without_open_file_only_prints(self):
        self.module.close_daily_log()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.module.log("只到終端")
        self.assertIn("只到終端", stdout.getvalue())

    def test_unwritable_log_root_does_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "file"
            blocker.write_text("x", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertIsNone(self.module.open_daily_log(blocker / "logs", datetime(2026, 10, 23, tzinfo=TAIPEI)))
                self.module.log("仍可記錄")

    def test_prune_removes_only_old_files_with_exact_name_format(self):
        now = datetime(2026, 10, 23, 7, 0, tzinfo=TAIPEI)
        with tempfile.TemporaryDirectory() as directory:
            logs_root = Path(directory)
            names = [
                "daily-2026-09-22.log",      # 31 天前：刪
                "daily-2026-09-23.log",      # 剛好 30 天：保留
                "daily-2026-10-22.log",      # 昨天：保留
                "daily-2026-13-01.log",      # 日期無效：不動
                "daily-20260801.log",        # 格式不同：不動
                "daily-2026-08-01.log.bak",  # 格式不同：不動
                "launchd.log",               # launchd 輸出：不動
                "other-2026-08-01.log",      # 其他程式：不動
            ]
            for name in names:
                (logs_root / name).write_text("x", encoding="utf-8")
            (logs_root / "daily-2026-01-01.log.d").mkdir()
            removed = self.module.prune_daily_logs(logs_root, now, keep_days=30)
            self.assertEqual(removed, ["daily-2026-09-22.log"])
            self.assertEqual(sorted(path.name for path in logs_root.iterdir()), sorted(set(names) - {"daily-2026-09-22.log"} | {"daily-2026-01-01.log.d"}))
            self.assertEqual(self.module.prune_daily_logs(logs_root / "missing", now), [])
            with self.assertRaises(ValueError):
                self.module.prune_daily_logs(logs_root, now, keep_days=-1)


class SessionDeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("run_daily_pipeline_deadline", ROOT / "scripts" / "run_daily_pipeline.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_deadline_is_0855_taipei_of_session_day(self):
        deadline = self.module.session_deadline({"start": "2026-10-23T09:00:00+08:00"})
        self.assertEqual(deadline, datetime(2026, 10, 23, 8, 55, tzinfo=TAIPEI))
        self.assertIsNotNone(deadline.tzinfo)
        self.assertFalse(self.module.session_deadline_passed({"start": "2026-10-23T09:00:00+08:00"}, datetime(2026, 10, 23, 8, 54, tzinfo=TAIPEI)))
        self.assertTrue(self.module.session_deadline_passed({"start": "2026-10-23T09:00:00+08:00"}, datetime(2026, 10, 23, 8, 55, tzinfo=TAIPEI)))

    def test_missing_or_naive_session_has_no_deadline_and_counts_as_passed(self):
        for session in (None, {}, {"start": "not-a-time"}, {"start": "2026-10-23T09:00:00"}):
            with self.subTest(session=session):
                self.assertIsNone(self.module.session_deadline(session))
                self.assertTrue(self.module.session_deadline_passed(session, datetime(2026, 10, 23, 7, 0, tzinfo=TAIPEI)))


if __name__ == "__main__":
    unittest.main()
