import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from etf_agent.automation import local_notify

ROOT = Path(__file__).resolve().parents[2]
TAIPEI = timezone(timedelta(hours=8))
SESSION = {"start": "2026-10-23T09:00:00+08:00", "end": "2026-10-23T13:30:00+08:00"}


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "run_daily_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FinalStatusTests(unittest.TestCase):
    """run_daily_pipeline.py 結束時的單一最終狀態、通知文字與狀態列。"""

    @classmethod
    def setUpClass(cls):
        cls.module = load_script("run_daily_pipeline_status")
        cls.args = SimpleNamespace(agent_cli="claude", research_agent_cli="claude", account_id="acct")

    def test_final_status_mapping(self):
        final = self.module.final_run_status
        self.assertEqual(final({"status": "completed", "decision_status": "approved", "dplan": {"status": "ready"}}), "approved")
        self.assertEqual(final({"status": "completed", "decision_status": "no_trade", "dplan": {"status": "ready"}}), "no_trade")
        self.assertEqual(final({"status": "completed", "decision_status": "rejected", "dplan": {"status": "blocked"}}), "rejected")
        self.assertEqual(final({"status": "completed", "decision_status": "approved", "dplan": {"status": "blocked"}}), "blocked")
        self.assertEqual(final({"status": "completed", "decision_status": "approved"}), "blocked")
        self.assertEqual(final({"status": "failed", "error": "x"}), "failed")
        for base in ({"status": "failed"}, {"status": "completed", "decision_status": "rejected"}):
            self.assertEqual(final(dict(base, dplan={"status": "fallback", "output": "/x"})), "fallback")

    def test_notification_mentions_fallback_and_not_generated(self):
        summary = {"status": "completed", "decision_status": "rejected", "order_count": 0, "cash_stance": "neutral",
                   "dplan": {"status": "fallback", "output": "/tmp/D-Plan.json", "main_replaced": True}, "delivery": {"report_status": "ready"}}
        message = self.module.finish_notification(summary, self.args)
        self.assertIn("最終狀態 fallback", message)
        self.assertIn("備援 D-Plan", message)
        self.assertIn("/tmp/D-Plan.json", message)
        self.assertIn("請人工決定是否上傳", message)
        summary["dplan"]["main_replaced"] = False
        self.assertIn("主檔仍是正式決策", self.module.finish_notification(summary, self.args))
        failed = {"status": "failed", "error": "決策鏈失敗：逾時",
                  "dplan": {"status": "blocked", "error": "x", "fallback": {"status": "not_generated", "error": "持股 19 檔"}}}
        message = self.module.finish_notification(failed, self.args)
        self.assertIn("最終狀態 failed", message)
        self.assertIn("備援 D-Plan 也未產生：持股 19 檔", message)
        self.assertIn("持股 19 檔", self.module.status_detail(failed))
        self.assertEqual(self.module.status_detail({"dplan": {"status": "ready", "output": "/p"}}), "/p")

    def test_record_run_status_appends_one_line_and_validates_status(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory) / "logs"
            now = datetime(2026, 10, 23, 7, 40, tzinfo=TAIPEI)
            with redirect_stdout(io.StringIO()):
                path = self.module.record_run_status("fallback", trade_date="2026-10-23", run_id="daily-20261023-070000",
                                                     detail="/tmp/x.json", now=now, logs_root=logs)
                self.module.record_run_status("skipped", detail="休市", now=now.replace(hour=21), logs_root=logs)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            first = local_notify.parse_status_line(lines[0])
            self.assertEqual((first["run_type"], first["status"], first["trade_date"], first["run_id"], first["detail"]),
                             ("morning", "fallback", "2026-10-23", "daily-20261023-070000", "/tmp/x.json"))
            self.assertEqual(local_notify.parse_status_line(lines[1])["run_type"], "evening")
            with self.assertRaises(ValueError):
                self.module.record_run_status("done", logs_root=logs)

    def test_notify_wrapper_never_raises(self):
        with patch.object(local_notify.subprocess, "run", side_effect=OSError("boom")), \
                patch.object(self.module.sys, "platform", "darwin"), redirect_stdout(io.StringIO()):
            self.assertFalse(self.module.notify("t", "m"))


class AttemptFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_script("run_daily_pipeline_fallback")
        cls.args = SimpleNamespace(agent_cli="claude", research_agent_cli="claude", account_id="acct")
        cls.now = datetime(2026, 10, 23, 7, 50, tzinfo=TAIPEI)

    def attempt(self, summary, session=SESSION, now=None, result=None):
        notices = []
        calls = []

        class FakeService:
            def __init__(self, root):
                calls.append(root)

            def generate(self, **kwargs):
                calls.append(kwargs)
                return result or {"status": "not_generated", "error": "前提不滿足：持股 19 檔", "failures": ["持股 19 檔"]}

        with patch("etf_agent.competition.FallbackDPlanService", FakeService), \
                patch.object(self.module, "notify", lambda title, message: notices.append(message)), \
                redirect_stdout(io.StringIO()):
            self.module.attempt_fallback_dplan(summary, self.args, session, Path("/a"), Path("/s"), now or self.now)
        return notices, calls

    def test_ready_or_fallback_plan_is_left_alone(self):
        for status in ("ready", "fallback"):
            summary = {"status": "completed", "dplan": {"status": status, "output": "/p"}}
            notices, calls = self.attempt(summary)
            self.assertEqual((notices, calls), ([], []))
            self.assertEqual(summary["dplan"]["status"], status)

    def test_rolled_back_run_keeps_original_plan(self):
        summary = {"status": "failed", "error": "決策鏈失敗", "rolled_back": {"decision_run_id": "decision-old"}, "datapack": {"status": "matched"}}
        notices, calls = self.attempt(summary)
        self.assertEqual(calls, [])
        self.assertEqual(summary["dplan"]["status"], "blocked")
        self.assertEqual(summary["dplan"]["fallback"]["status"], "not_generated")
        self.assertIn("原 D-Plan 主檔仍可上傳", summary["dplan"]["fallback"]["error"])

    def test_failure_before_datapack_or_session_notifies_without_generating(self):
        summary = {"status": "failed", "error": "./start.sh daily 失敗"}
        notices, calls = self.attempt(summary, session=None)
        self.assertEqual(calls, [])
        self.assertEqual(summary["dplan"]["fallback"]["status"], "not_generated")
        self.assertEqual(len(notices), 1)
        self.assertIn("今日沒有可交的 D-Plan", notices[0])

    def test_past_deadline_does_not_generate(self):
        summary = {"status": "completed", "decision_status": "rejected", "datapack": {"status": "matched"}, "dplan": {"status": "blocked", "error": "決策狀態為 rejected"}}
        notices, calls = self.attempt(summary, now=datetime(2026, 10, 23, 9, 0, tzinfo=TAIPEI))
        self.assertEqual(calls, [])
        self.assertIn("交件期限", summary["dplan"]["fallback"]["error"])

    def test_reason_is_derived_from_summary_and_result_is_recorded(self):
        generated = {"status": "fallback", "fallback": True, "output": "/plan.json", "run_id": "fallback-1", "main_replaced": True}
        cases = (
            ({"status": "completed", "decision_status": "rejected", "datapack": {"status": "matched"}, "dplan": {"status": "blocked", "error": "決策狀態為 rejected"}}, "decision_rejected"),
            ({"status": "completed", "decision_status": "approved", "datapack": {"status": "matched"}, "dplan": {"status": "blocked", "error": "缺官方價"}}, "dplan_blocked"),
            ({"status": "failed", "error": "決策鏈失敗：逾時", "datapack": {"status": "matched"}}, "pipeline_failed"),
        )
        for summary, reason in cases:
            notices, calls = self.attempt(summary, result=dict(generated))
            kwargs = calls[1]
            self.assertEqual(kwargs["reason"], reason)
            self.assertEqual(kwargs["account_id"], "acct")
            self.assertEqual(kwargs["daily_run"]["target_session"], SESSION)
            self.assertEqual(summary["dplan"]["status"], "fallback")
            self.assertEqual(summary["dplan"]["output"], "/plan.json")
            self.assertIn("original", summary["dplan"])
            self.assertEqual(notices, [])
            self.assertEqual(self.module.final_run_status(summary), "fallback")

    def test_not_generated_result_is_recorded_and_notified(self):
        summary = {"status": "completed", "decision_status": "rejected", "datapack": {"status": "matched"}, "dplan": {"status": "blocked", "error": "決策狀態為 rejected"}}
        notices, calls = self.attempt(summary)
        self.assertEqual(summary["dplan"]["status"], "blocked")
        self.assertEqual(summary["dplan"]["fallback"]["status"], "not_generated")
        self.assertEqual(len(notices), 1)
        self.assertIn("持股 19 檔", notices[0])
        self.assertEqual(self.module.final_run_status(summary), "rejected")


if __name__ == "__main__":
    unittest.main()
