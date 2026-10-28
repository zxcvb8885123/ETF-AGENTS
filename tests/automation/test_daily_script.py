import importlib.util
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


class NamedResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("run_daily_pipeline", ROOT / "scripts" / "run_daily_pipeline.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def make_run(self, root, name, status, raw=True, decision_status=None, agent_cli=None):
        run_dir = root / ("%s-%s-%s" % (name[6:10], name[10:12], name[12:14])) / name
        run_dir.mkdir(parents=True)
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

    def test_rolled_back_run_cannot_be_resumed(self):
        # 還原後 Agent 輸出綁定的帳本狀態已移走，續跑會配錯帳戶快照。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = self.make_run(root, "daily-20261023-070000", "failed")
            summary = json.loads((run_dir / "pipeline.json").read_text(encoding="utf-8"))
            summary["rolled_back"] = {"decision_run_id": "decision-1"}
            (run_dir / "pipeline.json").write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "--force"):
                self.module.named_resumable_run(root, "daily-20261023-070000")
            self.assertIsNone(self.module.latest_resumable_run(root, "daily-20261023"))

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
            run_dir = root / "artifacts" / "daily" / "2026-09-29" / "daily-20260929"
            run_dir.mkdir(parents=True)
            (run_dir / "pipeline.json").write_text(json.dumps({"run_id": "daily-20260929", "status": "failed", "started_at": "2026-09-29T18:00:00+08:00"}), encoding="utf-8")
            (run_dir / "bull_b0_raw_1.json").write_text("{}", encoding="utf-8")
            code, seen = self.run_main("--resume", "daily-20260929", "--force", root=root)
        self.assertEqual(code, 1)
        self.assertEqual(seen, [True])

    def test_resume_without_resumable_run_still_fetches_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts" / "daily").mkdir(parents=True)
            code, seen = self.run_main("--resume", "--force", root=root)
        self.assertEqual(code, 1)
        self.assertEqual(seen, [False])

    def test_resume_with_unknown_run_id_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts" / "daily").mkdir(parents=True)
            code, seen = self.run_main("--resume", "daily-20200101", "--force", root=root)
        self.assertEqual(code, 1)
        self.assertEqual(seen, [])


class FakeCalendar:
    """週一到週五開市，另可指定額外休市日。"""

    def __init__(self, holidays=()):
        self.holidays = set(holidays)

    def is_trading_day(self, day):
        return day.weekday() < 5 and day not in self.holidays


class DataStageHistoryEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("run_daily_pipeline_end", ROOT / "scripts" / "run_daily_pipeline.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def run_stage(self, history_end):
        completed = type("Completed", (), {"returncode": 0})()
        with patch.object(self.module.subprocess, "run", return_value=completed) as run:
            self.module.run_data_stage("acct", "prepare-1", history_end)
        return run.call_args.kwargs["env"]

    def test_history_end_is_passed_to_start_script(self):
        self.assertEqual(self.run_stage(date(2026, 10, 21))["HISTORY_END"], "2026-10-21")

    def test_unknown_history_end_is_not_guessed(self):
        with patch.dict(self.module.os.environ, {}, clear=False):
            self.module.os.environ.pop("HISTORY_END", None)
            self.assertNotIn("HISTORY_END", self.run_stage(None))

    def test_failed_data_stage_raises(self):
        completed = type("Completed", (), {"returncode": 2})()
        with patch.object(self.module.subprocess, "run", return_value=completed):
            with self.assertRaises(RuntimeError):
                self.module.run_data_stage("acct", "prepare-1", date(2026, 10, 21))


class CalendarCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("run_daily_pipeline", ROOT / "scripts" / "run_daily_pipeline.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_missing_calendar_is_fetched_first_and_existing_one_is_not(self):
        now = datetime(2026, 10, 23, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(self.module, "capture_trading_sources") as capture:
                self.module.ensure_calendar_capture(root, now)
                capture.assert_called_once_with(root)
            with patch.object(self.module, "latest_calendar_capture_before", return_value=root), \
                    patch.object(self.module, "capture_trading_sources") as capture:
                self.module.ensure_calendar_capture(root, now)
                capture.assert_not_called()

    def test_fetch_failure_does_not_hide_the_original_calendar_error(self):
        now = datetime(2026, 10, 23, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(self.module, "capture_trading_sources", side_effect=OSError("離線")):
                self.module.ensure_calendar_capture(Path(directory), now)
                with self.assertRaises(self.module.TradingCalendarError):
                    self.module.trading_day_check(Path(directory), now)


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
        snapshot = {
            "usable": True, "latest_trade_date": "2026-10-21", "universe_size": 150, "latest_price_symbols": 150,
            "created_at": "2026-10-21T16:00:00+08:00",
        }
        snapshot.update(overrides)
        account_dir = root / "artifacts" / "ledger" / "ai-cup-2026"
        account_dir.mkdir(parents=True, exist_ok=True)
        (root / "artifacts" / "data").mkdir(parents=True, exist_ok=True)
        (root / "artifacts" / "data" / "research_snapshot_latest.json").write_text(json.dumps(snapshot), encoding="utf-8")
        account = account_dir / "account_snapshot_latest.json"
        account.write_text("{}", encoding="utf-8")
        return root / "artifacts" / "data" / "research_snapshot_latest.json", account

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

    def test_snapshot_made_before_data_was_final_is_refetched(self):
        # Yahoo 盤中就回傳當日那根日線但尚未定案；15:00 前建立的快照不能當作當日資料。
        self.assertFalse(self.fetched(created_at="2026-10-21T13:04:00+08:00"))
        self.assertFalse(self.fetched(created_at="2026-10-21T14:59:59+08:00"))
        self.assertTrue(self.fetched(created_at="2026-10-21T15:00:00+08:00"))
        self.assertTrue(self.fetched(created_at="2026-10-21T08:30:00+00:00"))
        self.assertTrue(self.fetched(created_at="2026-10-22T07:00:00+08:00"))

    def test_missing_or_naive_created_at_is_refetched(self):
        for value in (None, "", "昨天", "2026-10-21T16:00:00"):
            self.assertFalse(self.fetched(created_at=value), value)

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

    def refresh(self, snapshot_cutoff, now, calendar=True):
        from etf_agent.data import TradingCalendar

        real = TradingCalendar.from_ogd_facts(
            [{"kind": "calendar_event", "event_date": "2026-10-10", "name": "國慶日"}], "fixture-sha")
        with tempfile.TemporaryDirectory() as directory:
            snapshot, _ = self.write_data(Path(directory), decision_cutoff=snapshot_cutoff)
            with patch.object(self.module, "latest_calendar_capture_before", lambda *_a: object() if calendar else None), \
                    patch.object(self.module, "calendar_from_capture", lambda _capture: real):
                return self.module.refresh_before_deadline(Path(directory), snapshot,
                                                           datetime.fromisoformat(now))

    def test_refreshes_snapshot_until_submission_deadline(self):
        # 10/21 晚上建立的 Snapshot 排到 10/22 時段；10/22 08:55 前重跑仍是同一時段，應重抓事件資料。
        evening = "2026-10-21T18:00:00+08:00"
        self.assertTrue(self.refresh(evening, "2026-10-21T20:00:00+08:00"))
        self.assertTrue(self.refresh(evening, "2026-10-22T07:30:00+08:00"))
        self.assertTrue(self.refresh(evening, "2026-10-22T08:54:59+08:00"))

    def test_keeps_snapshot_after_deadline_or_without_calendar(self):
        evening = "2026-10-21T18:00:00+08:00"
        self.assertFalse(self.refresh(evening, "2026-10-22T08:55:00+08:00"))
        self.assertFalse(self.refresh(evening, "2026-10-22T10:00:00+08:00"))
        self.assertFalse(self.refresh(evening, "2026-10-22T07:30:00+08:00", calendar=False))
        self.assertFalse(self.refresh(None, "2026-10-22T07:30:00+08:00"))

    def write_run(self, runs_root, name, status, session_start=None, finished_at=None):
        run_dir = runs_root / "2026-10-22" / name
        run_dir.mkdir(parents=True)
        summary = {"run_id": name, "status": status}
        if session_start:
            summary["target_session"] = {"start": session_start}
        if finished_at:
            summary["finished_at"] = finished_at
        (run_dir / "pipeline.json").write_text(json.dumps(summary), encoding="utf-8")

    def test_same_day_run_for_another_session_is_not_a_duplicate(self):
        # 早上的 run 為 10/22 時段；晚上的 run 為 10/23 時段，不能被當成「當日已完成」。
        morning, tomorrow = "2026-10-22T09:00:00+08:00", "2026-10-23T09:00:00+08:00"
        now = datetime.fromisoformat("2026-10-22T18:30:00+08:00")
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            self.write_run(runs, "daily-20261022", "completed", morning, "2026-10-22T18:00:00+08:00")
            find = self.module.completed_run_for_session
            self.assertIsNone(find(runs, "daily-20261022", tomorrow, now))
            self.assertEqual(find(runs, "daily-20261022", morning, now)[0], "daily-20261022")
            # 晚上的 run 完成後，再次觸發同一時段就略過。
            self.write_run(runs, "daily-20261022-180000", "completed", tomorrow, "2026-10-22T18:20:00+08:00")
            self.assertEqual(find(runs, "daily-20261022", tomorrow, now)[0], "daily-20261022-180000")

    def test_only_recently_finished_run_skips_schedule(self):
        # 半夜 04:36 手動跑完，07:00 排程照常重做；06:30 跑完則 07:00 略過。
        session = "2026-10-23T09:00:00+08:00"
        seven = datetime.fromisoformat("2026-10-23T07:00:00+08:00")
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            find = self.module.completed_run_for_session
            self.write_run(runs, "daily-20261023", "completed", session, "2026-10-23T04:36:00+08:00")
            self.assertIsNone(find(runs, "daily-20261023", session, seven))
            self.write_run(runs, "daily-20261023-060000", "completed", session, "2026-10-23T06:30:00+08:00")
            self.assertEqual(find(runs, "daily-20261023", session, seven)[0], "daily-20261023-060000")
            # 剛好一小時仍算剛完成。
            self.assertIsNotNone(find(runs, "daily-20261023", session, datetime.fromisoformat("2026-10-23T07:30:00+08:00")))
            self.assertIsNone(find(runs, "daily-20261023", session, datetime.fromisoformat("2026-10-23T07:31:00+08:00")))

    def test_unknown_session_or_legacy_run_keeps_skip(self):
        now = datetime.fromisoformat("2026-10-22T18:30:00+08:00")
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            # 舊 run 沒有時段也沒有完成時間：保守視為剛完成，維持略過。
            self.write_run(runs, "daily-20261022", "completed")
            find = self.module.completed_run_for_session
            self.assertIsNotNone(find(runs, "daily-20261022", "2026-10-23T09:00:00+08:00", now))
            self.assertIsNotNone(find(runs, "daily-20261022", None, now))
            self.assertIsNone(find(runs, "daily-20261021", None, now))

    def test_failed_run_is_not_counted_as_completed(self):
        now = datetime.fromisoformat("2026-10-22T18:30:00+08:00")
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            self.write_run(runs, "daily-20261022", "failed", "2026-10-23T09:00:00+08:00", "2026-10-22T18:20:00+08:00")
            self.assertIsNone(self.module.completed_run_for_session(runs, "daily-20261022", None, now))

    def test_run_rebuilds_snapshot_before_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_data(root)
            with patch.object(self.module, "refresh_before_deadline", lambda *_a: True):
                self.assertEqual(self.run_main(root, "--force"), [False])
            with patch.object(self.module, "refresh_before_deadline", lambda *_a: False):
                self.assertEqual(self.run_main(root, "--force"), [True])

    def test_session_past_submission_deadline_is_refused(self):
        # 10/23 13:59 沿用凌晨的 Snapshot，目標仍是 10/23 時段：不得再產生決策取代已上傳那份。
        passed = self.module.session_deadline_passed
        session = {"start": "2026-10-23T09:00:00+08:00", "end": "2026-10-23T13:30:00+08:00"}
        self.assertTrue(passed(session, datetime(2026, 10, 23, 13, 59, tzinfo=self.TZ)))
        self.assertTrue(passed(session, datetime(2026, 10, 23, 8, 55, tzinfo=self.TZ)))
        self.assertFalse(passed(session, datetime(2026, 10, 23, 8, 54, 59, tzinfo=self.TZ)))
        self.assertFalse(passed(session, datetime(2026, 10, 22, 18, 0, tzinfo=self.TZ)))
        for broken in (None, {}, {"start": "明天"}, {"start": "2026-10-23T09:00:00"}):
            self.assertTrue(passed(broken, datetime(2026, 10, 22, 18, 0, tzinfo=self.TZ)), broken)

    def test_decision_finished_after_deadline_is_not_exported(self):
        # 早上重做拖過 08:55 才跑完：不換 D-Plan 主檔；沒有備案時把新決策移到封存區，帳本視為沒交件。
        session = {"start": "2026-10-23T09:00:00+08:00", "end": "2026-10-23T13:30:00+08:00"}
        refuse = self.module.refuse_late_decision
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fresh = root / "artifacts" / "decisions" / "2026-10-23" / "decision-20261023T000000Z"
            fresh.mkdir(parents=True)
            with patch.object(self.module, "ROOT", root):
                summary = {}
                refuse(summary, session, fresh.name, datetime(2026, 10, 23, 8, 54, tzinfo=self.TZ))
                self.assertEqual(summary, {})
                self.assertTrue(fresh.exists())
                # 有被取代的備案：交給結束時的還原處理，這裡不搬新決策。
                summary = {"superseded_pending": {"decision_run_id": "decision-old"}}
                with self.assertRaisesRegex(RuntimeError, "交件期限"):
                    refuse(summary, session, fresh.name, datetime(2026, 10, 23, 9, 5, tzinfo=self.TZ))
                self.assertTrue(fresh.exists())
                summary = {}
                with self.assertRaisesRegex(RuntimeError, "交件期限"):
                    refuse(summary, session, fresh.name, datetime(2026, 10, 23, 9, 5, tzinfo=self.TZ))
            self.assertFalse(fresh.exists())
            archived = root / "artifacts" / "archive" / "superseded_decisions" / "2026-10-23" / fresh.name
            self.assertEqual(summary["late_decision_archived_to"], str(archived))
            self.assertTrue(archived.is_dir())

    def test_voided_closed_session_decision_is_not_treated_as_superseded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending = {"decision_run_id": "decision-typhoon", "account_run_id": "prepare-old"}
            (root / "artifacts" / "decisions").mkdir(parents=True)
            with patch.object(self.module, "ROOT", root):
                self.assertIsNone(self.module.voided_pending(pending))
                superseded = root / "artifacts" / "archive" / "superseded_decisions" / "2026-10-23" / "decision-typhoon"
                superseded.mkdir(parents=True)
                self.assertTrue(self.module.superseded_pending("acct", pending))
                superseded.rmdir()
                voided = root / "artifacts" / "archive" / "voided_closed_sessions" / "2026-10-23" / "decision-typhoon"
                voided.mkdir(parents=True)
                self.assertFalse(self.module.superseded_pending("acct", pending))
                self.assertEqual(self.module.voided_pending(pending), voided)
                self.assertFalse(self.module.superseded_pending("acct", None))
                self.assertIsNone(self.module.voided_pending(None))

    def test_trading_day_check_runs_holiday_evening_before_trading_day(self):
        class FakeCalendar:
            closed = {date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 26)}

            def is_trading_day(self, day):
                return day not in self.closed

        def check(at):
            with patch.object(self.module, "latest_calendar_capture_before", lambda *_a: Path("capture")), \
                    patch.object(self.module, "calendar_from_capture", lambda *_a: FakeCalendar()):
                return self.module.trading_day_check(Path("captures"), datetime.fromisoformat(at))

        self.assertTrue(check("2026-10-23T07:00:00+08:00"))
        self.assertFalse(check("2026-10-26T07:00:00+08:00"))
        self.assertTrue(check("2026-10-26T21:00:00+08:00"))
        self.assertFalse(check("2026-10-26T14:59:00+08:00"))
        self.assertFalse(check("2026-10-25T21:00:00+08:00"))
        self.assertTrue(check("2026-10-27T21:00:00+08:00"))

    def test_auto_resume_blocker_skips_quota_security_and_late_runs(self):
        deadline = datetime.fromisoformat("2026-10-27T08:55:00+08:00")
        early = datetime.fromisoformat("2026-10-27T07:20:00+08:00")
        blocker = self.module.auto_resume_blocker
        self.assertIsNone(blocker(["event_b3 Agent 輸出驗證失敗：items[14] 未分級"], deadline, early))
        self.assertIn("額度", blocker(["event_b3 Agent 未產生結構化輸出：You've hit your session limit"], deadline, early))
        self.assertIn("安全", blocker(["bull_b0 Agent 輸出包含本機秘密設定的值，已拒絕"], deadline, early))
        self.assertIn("不足", blocker(["timeout"], deadline, datetime.fromisoformat("2026-10-27T08:31:00+08:00")))
        self.assertIsNotNone(blocker(["timeout"], None, early))

    def test_run_decision_chain_resumes_once_with_validated_outputs(self):
        from types import SimpleNamespace

        def outcome(status, calls, errors=()):
            return SimpleNamespace(status=status, agent_calls=calls, errors=list(errors))

        deadline = datetime.fromisoformat("2026-10-27T08:55:00+08:00")
        now = lambda: datetime.fromisoformat("2026-10-27T07:20:00+08:00")
        built, logs = [], []
        results = iter([outcome("failed", [{"name": "event_b3"}], ["event_b3 Agent 輸出驗證失敗"]),
                        outcome("completed", [{"name": "trader_b0"}])])
        result, calls, record = self.module.run_decision_chain(
            lambda resume: built.append(resume) or resume, lambda _: next(results), deadline, now, logs.append)
        self.assertEqual(built, [False, True])
        self.assertEqual(result.status, "completed")
        self.assertEqual([call["name"] for call in calls], ["event_b3", "trader_b0"])
        self.assertEqual((record["attempted"], record["status"]), (True, "completed"))
        # 額度用完時不續跑，只呼叫一次並記下原因。
        built.clear()
        result, calls, record = self.module.run_decision_chain(
            lambda resume: built.append(resume), lambda _: outcome("failed", [], ["hit your session limit"]), deadline, now, logs.append)
        self.assertEqual(built, [False])
        self.assertFalse(record["attempted"])
        # 關閉自動續跑或第一次就成功時不記錄。
        built.clear()
        _, _, record = self.module.run_decision_chain(
            lambda resume: built.append(resume), lambda _: outcome("failed", [], ["timeout"]), deadline, now, logs.append, auto_resume=False)
        self.assertEqual((built, record), ([False], None))

    def test_trading_day_check_runs_sunday_evening_before_monday(self):
        def check(at):
            with patch.object(self.module, "latest_calendar_capture_before", lambda *_a: Path("capture")), \
                    patch.object(self.module, "calendar_from_capture", lambda *_a: FakeCalendar()):
                return self.module.trading_day_check(Path("captures"), datetime.fromisoformat(at))

        self.assertTrue(check("2026-11-01T21:00:00+08:00"))
        self.assertFalse(check("2026-10-31T21:00:00+08:00"))
        self.assertFalse(check("2026-11-01T07:00:00+08:00"))

    def scheduled_main(self, root, at, session_start="2026-10-23T09:00:00+08:00"):
        """不帶 --force 在指定台北時間觸發 main，回傳 (exit code, 是否走到預檢)。"""
        fixed = datetime.fromisoformat(at)
        real_datetime = datetime

        class FixedDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        seen = []

        def fake_preflight(_root, _account_id, skip_data, **_kwargs):
            seen.append(skip_data)
            return [self.module.PreflightCheck("fake", False, "測試用預檢失敗")]

        with patch.object(self.module, "datetime", FixedDatetime), \
                patch.object(self.module, "run_preflight", fake_preflight), \
                patch.object(self.module, "notify", lambda *_a, **_k: None), \
                patch.object(self.module, "ensure_calendar_capture", lambda *_a: None), \
                patch.object(self.module, "refresh_inferred_closures", lambda *_a: None), \
                patch.object(self.module, "trading_day_check", lambda *_a: True), \
                patch.object(self.module, "upcoming_session_start", lambda *_a: session_start), \
                patch.object(self.module, "expected_latest_trade_date", lambda *_a: date(2026, 10, 22)), \
                patch.object(self.module, "refresh_before_deadline", lambda *_a: False), \
                patch.object(self.module, "ROOT", root), \
                patch("sys.argv", ["run_daily_pipeline.py"]):
            return self.module.main(), bool(seen)

    def test_schedule_skips_only_after_recent_run_and_stamps_every_folder(self):
        session = "2026-10-23T09:00:00+08:00"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_data(root)
            runs = root / "artifacts" / "daily"
            # 半夜 04:36 手動跑完：07:00 照跑，另開帶時間的新目錄，舊目錄保留。
            self.write_run(runs, "daily-20261023", "completed", session, "2026-10-23T04:36:00+08:00")
            self.assertEqual(self.scheduled_main(root, "2026-10-23T07:00:00+08:00"), (1, True))
            day = runs / "2026-10-23"
            self.assertEqual(sorted(path.name for path in day.iterdir()), ["daily-20261023-070000"])
            self.assertTrue((runs / "2026-10-22" / "daily-20261023" / "pipeline.json").exists())
            # 06:30 跑完：07:00 略過，不建立新目錄。
            self.write_run(runs, "daily-20261023-060000", "completed", session, "2026-10-23T06:30:00+08:00")
            self.assertEqual(self.scheduled_main(root, "2026-10-23T07:00:00+08:00"), (0, False))
            self.assertEqual(sorted(path.name for path in day.iterdir()), ["daily-20261023-070000"])

    def test_evening_run_folder_uses_target_trading_day_not_execution_day(self):
        # 10/23 晚上跑，算的是 10/24 的時段：資料夾要開在 10/24，不是跑的那天 10/23。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_data(root)
            runs = root / "artifacts" / "daily"
            self.assertEqual(
                self.scheduled_main(root, "2026-10-23T21:00:00+08:00", session_start="2026-10-24T09:00:00+08:00"),
                (1, True),
            )
            self.assertFalse((runs / "2026-10-23").exists())
            self.assertEqual(
                sorted(path.name for path in (runs / "2026-10-24").iterdir()), ["daily-20261023-210000"],
            )

    def test_daily_run_path_prefers_day_hint_for_new_run_but_not_resume(self):
        find = self.module.daily_run_path
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            # 新 run：day_hint 存在就優先用它，不管 run_id 本身的執行日期。
            fresh = find(runs, "daily-20261023-210000", "2026-10-24")
            self.assertEqual(fresh, runs / "2026-10-24" / "daily-20261023-210000")
            # 沒給 day_hint 時照舊退回 run_id 內的執行日期。
            self.assertEqual(find(runs, "daily-20261023-210000"), runs / "2026-10-23" / "daily-20261023-210000")
            # 已存在的 run 一律用它原本的位置，day_hint 不會把它搬家。
            (runs / "2026-10-22" / "daily-20261023-210000").mkdir(parents=True)
            self.assertEqual(
                find(runs, "daily-20261023-210000", "2026-10-24"), runs / "2026-10-22" / "daily-20261023-210000",
            )


if __name__ == "__main__":
    unittest.main()


class DataPackGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("run_daily_pipeline_datapack", ROOT / "scripts" / "run_daily_pipeline.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "config").mkdir()
        (self.root / "data").mkdir()
        (self.root / "run").mkdir()
        (self.root / "var/datapack").mkdir(parents=True)
        (self.root / "config/competition_profile.json").write_text(json.dumps({"team_id": "TEAM_11067"}), encoding="utf-8")
        (self.root / "data/official_universe.csv").write_text("symbol,name,market,source_date\n2330,台積電,TWSE,2026-09-14\n", encoding="utf-8")
        self.account = self.root / "account.json"
        self.account.write_text(json.dumps({"cash": "1000000000", "positions": []}), encoding="utf-8")
        self.session = {"start": "2026-10-26T09:00:00+08:00"}
        self.pack = {
            "schema_version": "3.0", "doc_type": "DataPack", "team_id": "TEAM_11067", "trade_date": "2026-10-26",
            "as_of_date": None, "generated_at": "2026-10-20T05:12:08+08:00", "missing": [],
            "prev_day_orders": {"outcome": "none", "void_reasons": [], "excluded_codes": []},
            "account": {"cash": 1000000000.0, "cash_ratio_tradable": 1.0, "nav_tradable": 1000000000.0,
                        "aum": 1000000000.0, "receivable_cash": 0.0, "holdings_count": 0},
            "holdings": [], "compliance": {"violations": 0, "notices": 0, "incidents": []},
        }

    def gate(self, previous=None, **overrides):
        from decimal import Decimal
        from types import SimpleNamespace
        args = SimpleNamespace(skip_datapack=False, datapack=None, datapack_dir=self.root / "var/datapack", account_id="acct",
                               datapack_cash_tolerance=Decimal("0.0005"))
        for key, value in overrides.items():
            setattr(args, key, value)
        with patch.object(self.module, "ROOT", self.root), patch.object(self.module, "log", lambda *_a: None):
            return self.module.datapack_gate(args, self.root / "run", self.account, self.session, previous)

    def write_pack(self, name="pack.json"):
        (self.root / "var/datapack" / name).write_text(json.dumps(self.pack), encoding="utf-8")

    def test_matching_datapack_is_recorded_and_copied(self):
        self.write_pack()
        result = self.gate()
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["trade_date"], "2026-10-26")
        self.assertTrue((self.root / "run/datapack.json").is_file())

    def test_missing_mismatched_or_changed_datapack_stops_before_agents(self):
        from etf_agent.competition import DataPackError
        with self.assertRaisesRegex(DataPackError, "找不到"):
            self.gate()
        self.write_pack()
        self.account.write_text(json.dumps({"cash": "1000000000", "positions": [{"symbol": "2330.TW", "shares": 1000}]}), encoding="utf-8")
        with self.assertRaisesRegex(DataPackError, "不一致"):
            self.gate()
        with self.assertRaisesRegex(DataPackError, "續跑"):
            self.gate(previous={"datapack_sha256": "0" * 64})

    def test_explicit_skip_is_recorded(self):
        self.assertEqual(self.gate(skip_datapack=True), {"status": "skipped"})

    def test_rebase_flag_rebuilds_then_rechecks(self):
        self.write_pack()
        self.account.write_text(json.dumps({"cash": "1000000000", "positions": [{"symbol": "2330.TW", "shares": 1000}]}), encoding="utf-8")
        rebuilt = {"rebase_run_id": "rebase-x", "prepare_run_id": "prepare-x-datapack",
                   "account_snapshot": {"cash": "1000000000", "positions": []}}
        with patch.object(self.module, "rebase_account_to_datapack", return_value=rebuilt) as rebase:
            result = self.gate(rebase_to_datapack=True)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["rebased"]["prepare_run_id"], "prepare-x-datapack")
        rebase.assert_called_once()
        with patch.object(self.module, "rebase_account_to_datapack", return_value=dict(rebuilt, account_snapshot={"cash": "1", "positions": []})):
            with self.assertRaisesRegex(Exception, "不一致"):
                self.gate(rebase_to_datapack=True)


class MarketNewsSwitchTests(unittest.TestCase):
    def test_no_market_news_flag_is_parsed_and_rejects_nothing_else(self):
        spec = importlib.util.spec_from_file_location("run_daily_pipeline_news", ROOT / "scripts" / "run_daily_pipeline.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = (ROOT / "scripts" / "run_daily_pipeline.py").read_text(encoding="utf-8")
        self.assertIn('market_news_root=None if args.no_market_news else', source)
        self.assertIn('"market_news": "disabled" if args.no_market_news else "enabled"', source)
        with patch("sys.argv", ["run_daily_pipeline.py", "--no-market-news", "--help"]):
            with self.assertRaises(SystemExit) as caught:
                module.main()
        self.assertEqual(caught.exception.code, 0)
