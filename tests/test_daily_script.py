import importlib.util
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class NamedResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("run_daily_pipeline", ROOT / "scripts" / "run_daily_pipeline.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def make_run(self, root, name, status, raw=True, decision_status=None, agent_cli=None):
        run_dir = root / name
        run_dir.mkdir()
        summary = {"run_id": name, "status": status}
        if decision_status:
            summary["decision_status"] = decision_status
        if agent_cli:
            summary["agent_cli"] = agent_cli
        (run_dir / "pipeline.json").write_text(json.dumps(summary), encoding="utf-8")
        if raw:
            (run_dir / "bull_b0_raw_1.json").write_text("{}", encoding="utf-8")
        return run_dir

    def test_resumes_previous_day_failed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = self.make_run(root, "daily-20260929", "failed")
            self.assertEqual(self.module.named_resumable_run(root, "daily-20260929"), run_dir)
            rejected = self.make_run(root, "daily-20260928", "completed", decision_status="rejected")
            self.assertEqual(self.module.named_resumable_run(root, "daily-20260928"), rejected)

    def test_rejects_missing_completed_empty_and_path_like_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_run(root, "daily-20260927", "completed", decision_status="approved")
            self.make_run(root, "daily-20260926", "failed", raw=False)
            for name, message in (
                ("daily-20260901", "找不到"),
                ("daily-20260927", "已完成"),
                ("daily-20260926", "沒有已保存"),
                ("../daily-20260927", "找不到"),
                ("daily-20260927/../x", "找不到"),
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    self.module.named_resumable_run(root, name)

    def test_resumable_cli_comes_from_selected_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = self.make_run(root, "daily-20260928", "failed")
            codex = self.make_run(root, "daily-20260929", "failed", agent_cli="codex")
            opencode = self.make_run(root, "daily-20260930", "failed", agent_cli="opencode")
            self.assertEqual(self.module.resumable_agent_cli(legacy), "claude")
            self.assertEqual(self.module.resumable_agent_cli(codex), "codex")
            self.assertEqual(self.module.resumable_agent_cli(opencode), "opencode")


class ArgumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("run_daily_pipeline_args", ROOT / "scripts" / "run_daily_pipeline.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def run_main(self, *argv, root=None):
        """以假預檢攔截 main：回傳（結束碼, 預檢收到的 skip_data）；預檢一律失敗，不會呼叫任何 Agent。"""
        seen = []

        def fake_preflight(_root, _account_id, skip_data, **_kwargs):
            seen.append(skip_data)
            return [self.module.PreflightCheck("fake", False, "測試用預檢失敗")]

        with patch.object(self.module, "run_preflight", fake_preflight), \
                patch.object(self.module, "notify", lambda *_args, **_kwargs: None), \
                patch.object(self.module, "ROOT", root or ROOT), \
                patch("sys.argv", ["run_daily_pipeline.py", *argv]):
            code = self.module.main()
        return code, seen

    def test_perception_requires_bundle_and_result_together(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_main("--perception", "bundle.json")
        self.assertEqual(caught.exception.code, 2)

    def test_old_split_arguments_are_gone(self):
        for argv in (["--perception-bundle", "bundle.json"], ["--resume-run", "daily-20260929"]):
            with self.assertRaises(SystemExit) as caught:
                self.run_main(*argv)
            self.assertEqual(caught.exception.code, 2)

    def test_resume_with_run_id_implies_skip_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "artifacts" / "daily_runs" / "daily-20260929"
            run_dir.mkdir(parents=True)
            (run_dir / "pipeline.json").write_text(json.dumps({"run_id": "daily-20260929", "status": "failed", "started_at": "2026-09-29T18:00:00+08:00"}), encoding="utf-8")
            (run_dir / "bull_b0_raw_1.json").write_text("{}", encoding="utf-8")
            code, seen = self.run_main("--resume", "daily-20260929", "--force", root=root)
        self.assertEqual(code, 1)
        self.assertEqual(seen, [True])

    def test_resume_without_resumable_run_still_fetches_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts" / "daily_runs").mkdir(parents=True)
            code, seen = self.run_main("--resume", "--force", root=root)
        self.assertEqual(code, 1)
        self.assertEqual(seen, [False])

    def test_resume_with_unknown_run_id_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts" / "daily_runs").mkdir(parents=True)
            code, seen = self.run_main("--resume", "daily-20200101", "--force", root=root)
        self.assertEqual(code, 1)
        self.assertEqual(seen, [])


class FakeCalendar:
    """週一到週五開市，另可指定額外休市日。"""

    def __init__(self, holidays=()):
        self.holidays = set(holidays)

    def is_trading_day(self, day):
        return day.weekday() < 5 and day not in self.holidays


class DataReuseTests(unittest.TestCase):
    TZ = timezone(timedelta(hours=8))

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("run_daily_pipeline_reuse", ROOT / "scripts" / "run_daily_pipeline.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def expected(self, now, calendar=None, capture=object()):
        with patch.object(self.module, "latest_calendar_capture_before", lambda *_a: capture), \
                patch.object(self.module, "calendar_from_capture", lambda _c: calendar or FakeCalendar()):
            return self.module.expected_latest_trade_date(Path("unused"), now)

    def test_trade_day_data_counts_only_after_three_pm(self):
        wednesday = date(2026, 10, 21)
        self.assertEqual(self.expected(datetime(2026, 10, 21, 14, 59, tzinfo=self.TZ)), date(2026, 10, 20))
        self.assertEqual(self.expected(datetime(2026, 10, 21, 15, 0, tzinfo=self.TZ)), wednesday)
        self.assertEqual(self.expected(datetime(2026, 10, 21, 23, 0, tzinfo=self.TZ)), wednesday)
        self.assertEqual(self.expected(datetime(2026, 10, 22, 8, 0, tzinfo=self.TZ)), wednesday)

    def test_weekend_and_holiday_use_previous_trading_day(self):
        friday = date(2026, 10, 23)
        self.assertEqual(self.expected(datetime(2026, 10, 24, 20, 0, tzinfo=self.TZ)), friday)
        self.assertEqual(self.expected(datetime(2026, 10, 26, 10, 0, tzinfo=self.TZ)), friday)
        holiday = FakeCalendar(holidays=[date(2026, 10, 21)])
        self.assertEqual(self.expected(datetime(2026, 10, 21, 20, 0, tzinfo=self.TZ), holiday), date(2026, 10, 20))

    def test_missing_or_broken_calendar_returns_none(self):
        now = datetime(2026, 10, 21, 20, 0, tzinfo=self.TZ)
        self.assertIsNone(self.expected(now, capture=None))
        self.assertIsNone(self.expected(now, FakeCalendar(holidays=[now.date() - timedelta(days=n) for n in range(30)])))

        class Broken:
            def is_trading_day(self, _day):
                raise self.module.TradingCalendarError("超出日曆範圍")

        Broken.module = self.module
        self.assertIsNone(self.expected(now, Broken()))

    def write_data(self, root, **overrides):
        snapshot = {"usable": True, "latest_trade_date": "2026-10-21", "universe_size": 150, "latest_price_symbols": 150}
        snapshot.update(overrides)
        account_dir = root / "artifacts" / "virtual_accounts" / "ai-cup-2026"
        account_dir.mkdir(parents=True, exist_ok=True)
        (root / "artifacts" / "research_snapshot_latest.json").write_text(json.dumps(snapshot), encoding="utf-8")
        account = account_dir / "account_snapshot_latest.json"
        account.write_text("{}", encoding="utf-8")
        return root / "artifacts" / "research_snapshot_latest.json", account

    def fetched(self, expected=date(2026, 10, 21), drop_account=False, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            snapshot, account = self.write_data(Path(directory), **overrides)
            if drop_account:
                account.unlink()
            return self.module.data_already_fetched(snapshot, account, expected)

    def test_snapshot_with_expected_trade_date_counts_as_fetched(self):
        self.assertTrue(self.fetched())

    def test_missing_expected_date_or_old_trade_date_is_refetched(self):
        self.assertFalse(self.fetched(expected=None))
        self.assertFalse(self.fetched(latest_trade_date="2026-10-20"))
        self.assertFalse(self.fetched(expected=date(2026, 10, 22)))

    def test_incomplete_unusable_or_missing_data_is_refetched(self):
        self.assertFalse(self.fetched(usable=False))
        self.assertFalse(self.fetched(latest_price_symbols=149))
        self.assertFalse(self.fetched(universe_size="150"))
        self.assertFalse(self.fetched(drop_account=True))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(self.module.data_already_fetched(root / "none.json", root / "none2.json", date(2026, 10, 21)))

    def run_main(self, root, *argv, expected=date(2026, 10, 21)):
        seen = []

        def fake_preflight(_root, _account_id, skip_data, **_kwargs):
            seen.append(skip_data)
            return [self.module.PreflightCheck("fake", False, "測試用預檢失敗")]

        with patch.object(self.module, "run_preflight", fake_preflight), \
                patch.object(self.module, "notify", lambda *_a, **_k: None), \
                patch.object(self.module, "expected_latest_trade_date", lambda *_a: expected), \
                patch.object(self.module, "ROOT", root), \
                patch("sys.argv", ["run_daily_pipeline.py", *argv]):
            self.assertEqual(self.module.main(), 1)
        return seen

    def test_run_skips_data_stage_only_when_data_already_fetched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_data(root)
            self.assertEqual(self.run_main(root, "--force"), [True])
            self.assertEqual(self.run_main(root, "--force", expected=date(2026, 10, 22)), [False])
            self.assertEqual(self.run_main(root, "--force", expected=None), [False])


if __name__ == "__main__":
    unittest.main()
