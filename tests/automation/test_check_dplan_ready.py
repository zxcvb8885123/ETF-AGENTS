import fcntl
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from etf_agent.automation import local_notify
from etf_agent.core import canonical_sha256

ROOT = Path(__file__).resolve().parents[2]
TAIPEI = timezone(timedelta(hours=8))
TEAM, TRADE_DATE = "TEAM_042", "2026-10-23"


def valid_plan(orders=()):
    return {
        "schema_version": "4.3", "doc_type": "D-Plan", "team_id": TEAM, "trade_date": TRADE_DATE,
        "sources": [{"source_id": "S1", "authority": "twse", "url": "https://www.twse.example.test/x", "content_as_of": "2026-10-22T13:30:00+08:00"}],
        "observations": [{"obs_id": "O1", "source_ref": ["S1"], "statement": "測試用事實資料，不代表真實市場行情。", "values": {"v": 1.0}}],
        "market_view": {"basis_refs": ["O1"], "logic": "此為測試內容，不構成任何真實市場方向判斷。", "regime": "neutral", "stance": "neutral",
                        "posture": {"net_exposure_intent": "hold", "target_cash_pct_range": [0.0, 0.25]}, "counter_evidence": None},
        "inferences": [{"inf_id": "I1", "premise_refs": ["O1"], "logic": "此為測試用推論內容，僅用於驗證引用鏈和欄位契約。", "counter_evidence": None}],
        "decisions": [], "no_trade_decisions": [{"ticker": "2330", "reason_refs": ["I1"]}], "orders": list(orders),
        "agent_metadata": {"model_provider": "other", "model_version": "rule-fallback-test", "run_started_at": "2026-10-23T07:00:00+08:00",
                           "run_completed_at": "2026-10-23T07:30:00+08:00", "code_version": "git:test"},
    }


class FakeCalendar:
    closed = {date(2026, 10, 24), date(2026, 10, 25)}

    def is_trading_day(self, day):
        return day not in self.closed


class WatchdogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("check_dplan_ready", ROOT / "scripts" / "check_dplan_ready.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "competition_profile.json").write_text(json.dumps({"team_id": TEAM}), encoding="utf-8")
        self.main = self.root / "artifacts" / "competition_d_plan" / ("D-Plan_%s_%s.json" % (TEAM, TRADE_DATE))

    def tearDown(self):
        self.directory.cleanup()

    def write_run(self, name, dplan, status="completed", session_start="2026-10-23T09:00:00+08:00", finished="2026-10-23T07:30:00+08:00"):
        run_dir = self.root / "artifacts" / "daily" / TRADE_DATE / name
        run_dir.mkdir(parents=True, exist_ok=True)
        summary = {"run_id": name, "status": status, "finished_at": finished, "target_session": {"start": session_start}}
        if dplan is not None:
            summary["dplan"] = dplan
        (run_dir / "pipeline.json").write_text(json.dumps(summary), encoding="utf-8")

    def run_main(self, at="2026-10-23T08:20:00+08:00", notify=True):
        notices = []
        with patch.object(self.module, "latest_calendar_capture_before", lambda *_a: Path("capture")), \
                patch.object(self.module, "calendar_from_capture", lambda *_a: FakeCalendar()), \
                patch.object(self.module.local_notify, "notify", lambda title, message, **_k: notices.append(message) or True), \
                redirect_stdout(io.StringIO()):
            argv = ["--root", str(self.root), "--at", at] + ([] if notify else ["--no-notify"])
            code = self.module.main(argv)
        lines = (self.root / "var" / "logs" / local_notify.STATUS_LOG_NAME).read_text(encoding="utf-8").splitlines()
        return code, notices, local_notify.parse_status_line(lines[-1])

    def test_missing_main_file_notifies_and_fails(self):
        self.write_run("daily-20261023-070000", {"status": "blocked", "error": "決策狀態為 rejected"})
        code, notices, record = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "dplan_missing")
        self.assertEqual(record["run_type"], "watchdog")
        self.assertEqual(record["trade_date"], TRADE_DATE)
        self.assertIn("決策狀態為 rejected", record["detail"])
        self.assertEqual(len(notices), 1)
        self.assertIn("dplan_missing", notices[0])

    def test_valid_main_file_is_ready_without_notification(self):
        self.main.parent.mkdir(parents=True)
        self.main.write_text(json.dumps(valid_plan()), encoding="utf-8")
        self.write_run("daily-20261023-070000", {"status": "ready", "output": str(self.main)})
        code, notices, record = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(record["status"], "dplan_ready")
        self.assertEqual(notices, [])
        # 最新 run 沒產生新 D-Plan（例如已還原）但主檔仍在：仍是 ready，但提醒確認。
        self.write_run("daily-20261023-073000", {"status": "blocked", "error": "x"}, finished="2026-10-23T08:00:00+08:00")
        code, notices, record = self.run_main()
        self.assertEqual((code, record["status"]), (0, "dplan_ready"))
        self.assertIn("主檔是較早那份", record["detail"])

    def test_invalid_or_mismatched_main_file_fails(self):
        self.main.parent.mkdir(parents=True)
        self.main.write_text(json.dumps(dict(valid_plan(), trade_date="2026-10-22")), encoding="utf-8")
        code, notices, record = self.run_main()
        self.assertEqual((code, record["status"]), (1, "dplan_invalid"))
        self.assertIn("不一致", record["detail"])
        self.main.write_text("{not json", encoding="utf-8")
        code, notices, record = self.run_main(notify=False)
        self.assertEqual((code, record["status"]), (1, "dplan_invalid"))
        self.assertEqual(notices, [])

    def test_fallback_main_file_is_flagged_for_human_decision(self):
        plan = valid_plan()
        self.main.parent.mkdir(parents=True)
        self.main.write_text(json.dumps(plan), encoding="utf-8")
        run_dir = self.root / "artifacts" / "fallback_decisions" / TRADE_DATE / "fallback-20261022T233000Z"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(json.dumps({"artifacts": {"dplan": {"sha256": canonical_sha256(plan)}}}), encoding="utf-8")
        self.write_run("daily-20261023-070000", {"status": "fallback", "output": str(self.main)})
        code, notices, record = self.run_main()
        self.assertEqual((code, record["status"]), (0, "dplan_fallback"))
        self.assertIn("備援", record["detail"])
        self.assertEqual(len(notices), 1)
        self.assertIn("dplan_fallback", notices[0])

    def test_holiday_or_past_deadline_is_skipped(self):
        code, notices, record = self.run_main(at="2026-10-24T08:20:00+08:00")
        self.assertEqual((code, record["status"]), (2, "skipped"))
        self.assertIn("休市", record["detail"])
        self.assertEqual(len(notices), 1)
        code, notices, record = self.run_main(at="2026-10-23T09:10:00+08:00")
        self.assertEqual((code, record["status"]), (2, "skipped"))
        self.assertIn("交件期限", record["detail"])
        self.assertEqual(len(notices), 1)

    def test_missing_calendar_is_not_guessed(self):
        with patch.object(self.module, "latest_calendar_capture_before", lambda *_a: None), redirect_stdout(io.StringIO()):
            result = self.module.check(self.root, datetime(2026, 10, 23, 8, 20, tzinfo=TAIPEI))
        self.assertEqual(result["status"], "skipped")
        self.assertIn("開休市日期表", result["detail"])

    @staticmethod
    def finished_pid() -> int:
        """一個已結束程序的 pid，模擬流程跑完後留在鎖檔裡的持有者。"""
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        return child.pid

    def write_lock(self, pid, started_at="2026-10-23T07:00:05+08:00"):
        path = self.root / "artifacts" / "daily" / ".lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": pid, "started_at": started_at}), encoding="utf-8")
        return path

    def test_running_pipeline_reports_pending_instead_of_last_night_main_file(self):
        # 昨晚的主檔仍在、昨晚的 run 也是 ready；07:00 重做正在跑（鎖被持有）時不能報成可交。
        self.main.parent.mkdir(parents=True)
        self.main.write_text(json.dumps(valid_plan()), encoding="utf-8")
        self.write_run("daily-20261022-210000", {"status": "ready", "output": str(self.main)}, finished="2026-10-22T21:25:00+08:00")
        lock_path = self.write_lock(os.getpid())
        holder = lock_path.open("a+b")
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, notices, record = self.run_main(at="2026-10-23T08:30:00+08:00")
            self.assertEqual((code, record["status"]), (2, "dplan_pending"))
            self.assertEqual(record["trade_date"], TRADE_DATE)
            self.assertIn("仍在執行", record["detail"])
            self.assertIn("07:00", record["detail"])
            self.assertIn("可能是較早那份", record["detail"])
            self.assertEqual(len(notices), 1)
            self.assertIn("dplan_pending", notices[0])
            # 探測不得把鎖搶走：持有者仍握著鎖，別的程序仍取不到。
            with lock_path.open("a+b") as probe:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # 沒有主檔時也是 pending（不是 missing），因為流程結束後可能就有了。
            self.main.unlink()
            code, notices, record = self.run_main(at="2026-10-23T08:45:00+08:00")
            self.assertEqual((code, record["status"]), (2, "dplan_pending"))
            self.assertIn("目前沒有主檔", record["detail"])
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        # 鎖釋放後鎖檔仍在（流程跑完不刪檔），不能再當成在跑：回到正常判定。
        self.assertTrue(lock_path.is_file())
        code, notices, record = self.run_main(at="2026-10-23T08:45:00+08:00")
        self.assertEqual((code, record["status"]), (1, "dplan_missing"))
        self.main.write_text(json.dumps(valid_plan()), encoding="utf-8")
        code, notices, record = self.run_main(at="2026-10-23T08:45:00+08:00")
        self.assertEqual((code, record["status"]), (0, "dplan_ready"))
        self.assertEqual(notices, [])

    def test_leftover_lock_file_without_holder_is_not_pending(self):
        # 流程結束後鎖檔留著（內容是已結束的 pid）：沒人持有就照常檢查主檔。
        finished = self.finished_pid()
        self.write_lock(finished)
        self.main.parent.mkdir(parents=True)
        self.main.write_text(json.dumps(valid_plan()), encoding="utf-8")
        self.write_run("daily-20261023-070000", {"status": "ready", "output": str(self.main)})
        code, notices, record = self.run_main()
        self.assertEqual((code, record["status"]), (0, "dplan_ready"))
        self.assertEqual(notices, [])
        # 鎖檔內容壞掉也一樣，不因此誤報。
        (self.root / "artifacts" / "daily" / ".lock").write_text("{broken", encoding="utf-8")
        code, notices, record = self.run_main()
        self.assertEqual((code, record["status"]), (0, "dplan_ready"))

    def test_pid_fallback_when_lock_probe_unavailable(self):
        # 鎖探測不可用（例如檔案系統不支援）時改看持有者 pid 是否存活；pid 已結束或讀不到則不當在跑。
        self.main.parent.mkdir(parents=True)
        self.main.write_text(json.dumps(valid_plan()), encoding="utf-8")
        self.write_lock(os.getpid())
        with patch.object(self.module, "lock_is_held", lambda _path: None):
            code, notices, record = self.run_main()
            self.assertEqual((code, record["status"]), (2, "dplan_pending"))
            finished = self.finished_pid()
            self.write_lock(finished)
            code, notices, record = self.run_main()
            self.assertEqual((code, record["status"]), (0, "dplan_ready"))
            self.write_lock("not-a-pid")
            code, notices, record = self.run_main()
            self.assertEqual((code, record["status"]), (0, "dplan_ready"))

    def test_lock_holder_description_handles_missing_or_naive_started_at(self):
        now = datetime(2026, 10, 23, 8, 30, tzinfo=TAIPEI)
        self.write_lock(os.getpid(), started_at="2026-10-23T07:00:00")
        with patch.object(self.module, "lock_is_held", lambda _path: True):
            text = self.module.pipeline_lock_holder(self.root, now)
            self.assertIn("仍被持有", text)
            self.assertIn("pid %d" % os.getpid(), text)
            self.assertNotIn("分鐘", text)
            self.write_lock(os.getpid(), started_at="2026-10-23T07:00:00+08:00")
            text = self.module.pipeline_lock_holder(self.root, now)
            self.assertIn("約 90 分鐘", text)
        # 沒有鎖檔：不在跑。
        (self.root / "artifacts" / "daily" / ".lock").unlink()
        self.assertIsNone(self.module.pipeline_lock_holder(self.root, now))


if __name__ == "__main__":
    unittest.main()
