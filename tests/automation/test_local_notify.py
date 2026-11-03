import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from etf_agent.automation import local_notify

TAIPEI = timezone(timedelta(hours=8))


class NotifyTests(unittest.TestCase):
    """本機通知任何失敗都不得拋出，也不得阻塞。"""

    def test_uses_display_notification_and_reports_success(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(local_notify.subprocess, "run", fake_run):
            self.assertTrue(local_notify.notify("標題", "內容 \"引號\"", platform="darwin"))
        argv, kwargs = calls[0]
        self.assertEqual(argv[:2], ["osascript", "-e"])
        self.assertIn("display notification", argv[2])
        self.assertNotIn("display alert", argv[2])
        self.assertNotIn("display dialog", argv[2])
        self.assertEqual(kwargs["timeout"], local_notify.NOTIFY_TIMEOUT_SECONDS)

    def test_failures_return_false_and_are_logged(self):
        logged = []
        for error in (FileNotFoundError("no osascript"), subprocess.TimeoutExpired("osascript", 15)):
            with patch.object(local_notify.subprocess, "run", side_effect=error):
                self.assertFalse(local_notify.notify("t", "m", log=logged.append, platform="darwin"))
        with patch.object(local_notify.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "execution error: 不允許")):
            self.assertFalse(local_notify.notify("t", "m", log=logged.append, platform="darwin"))
        self.assertEqual(len(logged), 3)
        self.assertTrue(all("不影響流程" in line for line in logged))
        self.assertIn("不允許", logged[2])

    def test_non_darwin_is_a_noop(self):
        with patch.object(local_notify.subprocess, "run", side_effect=AssertionError("不該呼叫")):
            self.assertFalse(local_notify.notify("t", "m", platform="linux"))


class StatusLineTests(unittest.TestCase):
    def test_format_and_parse_round_trip_single_line(self):
        now = datetime(2026, 10, 22, 23, 31, 2, tzinfo=timezone.utc)  # 台北 10-23 07:31:02
        line = local_notify.format_status_line(now, "morning", "fallback", "2026-10-23", "daily-20261023-070000",
                                               "備援 D-Plan\n已存為 /tmp/x.json  （多個空白）")
        self.assertNotIn("\n", line)
        record = local_notify.parse_status_line(line)
        self.assertEqual(record["at"], "2026-10-23T07:31:02+08:00")
        self.assertEqual(record["run_type"], "morning")
        self.assertEqual(record["status"], "fallback")
        self.assertEqual(record["trade_date"], "2026-10-23")
        self.assertEqual(record["run_id"], "daily-20261023-070000")
        self.assertEqual(record["detail"], "備援 D-Plan 已存為 /tmp/x.json （多個空白）")
        empty = local_notify.parse_status_line(local_notify.format_status_line(now, "evening", "skipped", None, None, ""))
        self.assertEqual((empty["trade_date"], empty["run_id"], empty["detail"]), ("-", "-", "-"))
        self.assertEqual(local_notify.parse_status_line("garbage"), {})

    def test_naive_time_is_rejected(self):
        with self.assertRaises(ValueError):
            local_notify.format_status_line(datetime(2026, 10, 23, 7, 0), "morning", "failed", None, None, "")
        with self.assertRaises(ValueError):
            local_notify.classify_run_type(datetime(2026, 10, 23, 7, 0))

    def test_run_type_by_taipei_clock(self):
        self.assertEqual(local_notify.classify_run_type(datetime(2026, 10, 23, 7, 5, tzinfo=TAIPEI)), "morning")
        self.assertEqual(local_notify.classify_run_type(datetime(2026, 10, 23, 21, 0, tzinfo=TAIPEI)), "evening")
        # UTC 13:00 是台北 21:00。
        self.assertEqual(local_notify.classify_run_type(datetime(2026, 10, 23, 13, 0, tzinfo=timezone.utc)), "evening")

    def test_append_creates_folder_and_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory) / "var" / "logs"
            path = local_notify.append_status_line(logs, "第一行")
            local_notify.append_status_line(logs, "第二行")
            self.assertEqual(path, logs / local_notify.STATUS_LOG_NAME)
            self.assertEqual(path.read_text(encoding="utf-8").splitlines(), ["第一行", "第二行"])

    def test_unwritable_root_returns_none_without_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "file"
            blocker.write_text("x", encoding="utf-8")
            logged = []
            self.assertIsNone(local_notify.append_status_line(blocker / "logs", "line", log=logged.append))
            self.assertEqual(len(logged), 1)


if __name__ == "__main__":
    unittest.main()
