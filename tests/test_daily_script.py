import importlib.util
import json
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
