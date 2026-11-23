import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from etf_agent.data.source_capture import capture_with_retry

ROOT = Path(__file__).resolve().parents[2]
TAIPEI = timezone(timedelta(hours=8))
SESSION = {"start": "2026-11-24T09:00:00+08:00", "end": "2026-11-24T13:30:00+08:00"}
BAD = {"status": "partial_failure", "sources": [{"source_id": "X", "status": "failed"}]}


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "run_daily_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CaptureRetryStopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_script("run_daily_pipeline_deadline")

    def test_morning_run_stops_at_todays_0755_not_tomorrow(self):
        for hour, minute in ((7, 0), (7, 54), (8, 30), (12, 0)):
            now = datetime(2026, 11, 24, hour, minute, tzinfo=TAIPEI)
            self.assertEqual(self.module.capture_retry_stop_at(now), datetime(2026, 11, 24, 7, 55, tzinfo=TAIPEI))

    def test_evening_run_targets_next_morning(self):
        now = datetime(2026, 11, 23, 21, 0, tzinfo=TAIPEI)
        self.assertEqual(self.module.capture_retry_stop_at(now), datetime(2026, 11, 24, 7, 55, tzinfo=TAIPEI))

    def test_late_run_tries_only_twice_without_sleeping(self):
        clock = [datetime(2026, 11, 24, 8, 30, tzinfo=TAIPEI)]
        sleeps, calls = [], []

        def capture():
            calls.append(1)
            return BAD

        result = capture_with_retry(
            capture, now=lambda: clock[0], sleep=sleeps.append,
            stop_at=self.module.capture_retry_stop_at(clock[0]))
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(sleeps, [])  # 不會睡到隔天、也不會每 30 分鐘重試
        self.assertEqual(len(calls), 2)


class CatchupDatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_script("run_daily_pipeline_news_dates")

    def test_midweek_covers_yesterday_and_today(self):
        self.assertEqual(self.module.company_news_catchup_dates(date(2026, 11, 24)), [date(2026, 11, 23), date(2026, 11, 24)])

    def test_monday_covers_friday_sunday_and_today(self):
        self.assertEqual(self.module.company_news_catchup_dates(date(2026, 11, 23)),
                         [date(2026, 11, 20), date(2026, 11, 22), date(2026, 11, 23)])

    def test_catchup_fetches_each_date_oldest_first(self):
        fetched = []
        repository = SimpleNamespace(count_collected_days=lambda *args: 0)
        with patch.object(self.module, "UniverseLoader", lambda: SimpleNamespace(load=lambda path: [1, 2])), \
                patch.object(self.module, "SupplementalRepository", lambda database: repository), \
                patch.object(self.module, "MarketDataDatabase", lambda path: None), \
                patch.object(self.module.subprocess, "run",
                             lambda command, **kwargs: fetched.append(command[-1]) or SimpleNamespace(returncode=0)), \
                redirect_stdout(io.StringIO()):
            result = self.module.ensure_company_news(datetime(2026, 11, 24, 7, 0, tzinfo=TAIPEI))
        self.assertEqual(fetched, ["2026-11-23", "2026-11-24"])
        self.assertEqual(result["status"], "caught_up")


class PrecommitFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_script("run_daily_pipeline_precommit")
        cls.args = SimpleNamespace(account_id="acct")
        cls.now = datetime(2026, 11, 24, 7, 20, tzinfo=TAIPEI)

    def run_precommit(self, summary, session=SESSION, now=None, result=None):
        calls = []

        class FakeService:
            def __init__(self, root):
                pass

            def generate(self, **kwargs):
                calls.append(kwargs)
                return result or {"status": "fallback", "output": "/main.json", "main_replaced": True}

        with patch("etf_agent.competition.FallbackDPlanService", FakeService), redirect_stdout(io.StringIO()):
            self.module.precommit_fallback_dplan(summary, self.args, session, Path("/a"), Path("/s"), now or self.now)
        return calls

    def test_generates_fallback_after_matched_datapack_without_touching_dplan(self):
        summary = {"datapack": {"status": "matched"}}
        calls = self.run_precommit(summary)
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary["early_fallback"]["status"], "fallback")
        self.assertNotIn("dplan", summary)  # 正式 D-Plan 狀態不受影響
        self.assertEqual(calls[0]["reason"], "pipeline_failed")

    def test_skips_when_datapack_not_matched_or_deadline_passed(self):
        self.assertEqual(self.run_precommit({"datapack": {"status": "skipped"}}), [])
        self.assertEqual(self.run_precommit({}), [])
        late = datetime(2026, 11, 24, 8, 56, tzinfo=TAIPEI)
        self.assertEqual(self.run_precommit({"datapack": {"status": "matched"}}, now=late), [])

    def test_service_exception_is_swallowed(self):
        class Boom:
            def __init__(self, root):
                pass

            def generate(self, **kwargs):
                raise RuntimeError("boom")

        summary = {"datapack": {"status": "matched"}}
        with patch("etf_agent.competition.FallbackDPlanService", Boom), redirect_stdout(io.StringIO()):
            self.module.precommit_fallback_dplan(summary, self.args, SESSION, Path("/a"), Path("/s"), self.now)
        self.assertEqual(summary["early_fallback"]["status"], "not_generated")


if __name__ == "__main__":
    unittest.main()
