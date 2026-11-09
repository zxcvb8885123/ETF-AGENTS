import unittest
from datetime import datetime, timedelta, timezone

from etf_agent.data.source_capture import capture_with_retry

TZ = timezone(timedelta(hours=8))
OK = {"status": "captured_candidate_only", "sources": [], "run_id": "r"}
BAD = {"status": "partial_failure", "sources": [{"source_id": "X", "status": "failed"}], "run_id": "b"}


class CaptureRetryTest(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2026, 11, 8, 21, 0, tzinfo=TZ)
        self.sleeps = []

    def _run(self, results, stop_hours=10):
        calls = iter(results)

        def capture():
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.clock += timedelta(seconds=seconds)

        return capture_with_retry(capture, now=lambda: self.clock, sleep=sleep,
                                  stop_at=self.clock + timedelta(hours=stop_hours))

    def test_first_success_no_sleep(self):
        self.assertEqual(self._run([OK])["run_id"], "r")
        self.assertEqual(self.sleeps, [])

    def test_second_attempt_is_immediate(self):
        self.assertEqual(self._run([BAD, OK])["run_id"], "r")
        self.assertEqual(self.sleeps, [])

    def test_then_waits_half_hour(self):
        self.assertEqual(self._run([BAD, BAD, OK])["run_id"], "r")
        self.assertEqual(self.sleeps, [1800])

    def test_exception_counts_as_failure(self):
        self.assertEqual(self._run([OSError("down"), OK])["run_id"], "r")

    def test_gives_up_at_stop_time(self):
        result = self._run([BAD] * 20, stop_hours=1)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(self.sleeps, [1800, 1800])


if __name__ == "__main__":
    unittest.main()
