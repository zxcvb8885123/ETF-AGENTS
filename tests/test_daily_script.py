import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NamedResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("run_daily_pipeline", ROOT / "scripts" / "run_daily_pipeline.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def make_run(self, root, name, status, raw=True, decision_status=None):
        run_dir = root / name
        run_dir.mkdir()
        summary = {"run_id": name, "status": status}
        if decision_status:
            summary["decision_status"] = decision_status
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


if __name__ == "__main__":
    unittest.main()
