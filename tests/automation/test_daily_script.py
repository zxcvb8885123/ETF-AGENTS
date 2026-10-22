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

    def write_run(self, runs_root, name, status, session_start=None):
        run_dir = runs_root / "2026-10-22" / name
        run_dir.mkdir(parents=True)
        summary = {"run_id": name, "status": status}
        if session_start:
            summary["target_session"] = {"start": session_start}
        (run_dir / "pipeline.json").write_text(json.dumps(summary), encoding="utf-8")

    def test_same_day_run_for_another_session_is_not_a_duplicate(self):
        # 早上的 run 為 10/22 時段；晚上的 run 為 10/23 時段，不能被當成「當日已完成」。
        morning, tomorrow = "2026-10-22T09:00:00+08:00", "2026-10-23T09:00:00+08:00"
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            self.write_run(runs, "daily-20261022", "completed", morning)
            find = self.module.completed_run_for_session
            self.assertIsNone(find(runs, "daily-20261022", tomorrow))
            self.assertEqual(find(runs, "daily-20261022", morning)[0], "daily-20261022")
            # 晚上的 run 完成後，再次觸發同一時段就略過。
            self.write_run(runs, "daily-20261022-180000", "completed", tomorrow)
            self.assertEqual(find(runs, "daily-20261022", tomorrow)[0], "daily-20261022-180000")

    def test_unknown_session_or_legacy_run_keeps_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            self.write_run(runs, "daily-20261022", "completed")
            find = self.module.completed_run_for_session
            self.assertIsNotNone(find(runs, "daily-20261022", "2026-10-23T09:00:00+08:00"))
            self.assertIsNotNone(find(runs, "daily-20261022", None))
            self.assertIsNone(find(runs, "daily-20261021", None))

    def test_failed_run_is_not_counted_as_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            self.write_run(runs, "daily-20261022", "failed", "2026-10-23T09:00:00+08:00")
            self.assertIsNone(self.module.completed_run_for_session(runs, "daily-20261022", None))

    def test_run_rebuilds_snapshot_before_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_data(root)
            with patch.object(self.module, "refresh_before_deadline", lambda *_a: True):
                self.assertEqual(self.run_main(root, "--force"), [False])
            with patch.object(self.module, "refresh_before_deadline", lambda *_a: False):
                self.assertEqual(self.run_main(root, "--force"), [True])


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
