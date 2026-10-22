import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class StartEntrypointTests(unittest.TestCase):
    def invoke(self, args, *, python=True, exit_code=0):
        with tempfile.TemporaryDirectory(prefix="etf entrypoints ") as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "start.sh", root / "start.sh")
            bin_dir = root / ".venv" / "bin"
            bin_dir.mkdir(parents=True)
            if python:
                executable = bin_dir / "python"
                executable.write_text(
                    '#!/bin/sh\n'
                    'printf "%s\\n" "$PWD" "$@" > "$CALL_LOG"\n'
                    'exit "$PYTHON_EXIT"\n', encoding="utf-8"
                )
                executable.chmod(0o755)
            docker = bin_dir / "docker"
            docker.write_text(
                '#!/bin/sh\n'
                'echo unexpected > "$DOCKER_LOG"\nexit 99\n', encoding="utf-8"
            )
            docker.chmod(0o755)
            log = root / "python.txt"
            docker_log = root / "docker.txt"
            env = dict(os.environ, CALL_LOG=str(log), DOCKER_LOG=str(docker_log),
                       PYTHON_EXIT=str(exit_code), PATH=f"{bin_dir}:{os.environ['PATH']}")
            result = subprocess.run(
                ["sh", str(root / "start.sh"), *args], cwd=root.parent,
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertFalse(docker_log.exists(), result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            return result, root, calls

    def test_local_entrypoints_forward_arguments_and_use_project_directory(self):
        for mode, script, args in (
            ("run", "run_daily_pipeline.py", ["--agent-cli", "codex", "--model", "model with spaces"]),
            ("status", "data_status.py", ["--database", "folder with spaces/data.db"]),
        ):
            with self.subTest(mode=mode):
                result, root, calls = self.invoke([mode, *args], exit_code=7)
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertEqual(calls, [str(root), str(root / "scripts" / script), *args])

    def test_subcommand_help_reaches_python_without_docker(self):
        for mode in ("run", "status"):
            with self.subTest(mode=mode):
                result, _, calls = self.invoke([mode, "--help"])
                self.assertEqual(result.returncode, 0)
                self.assertEqual(calls[-1], "--help")

    def test_missing_local_python_stops_before_docker(self):
        for mode in ("run", "status"):
            with self.subTest(mode=mode):
                result, _, calls = self.invoke([mode], python=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn("python3 -m venv .venv", result.stderr)
                self.assertFalse(calls)

    def test_help_needs_neither_local_python_nor_docker(self):
        for option in ("--help", "-h", "help"):
            result, _, calls = self.invoke([option], python=False)
            self.assertEqual(result.returncode, 0)
            self.assertIn("run", result.stdout)
            self.assertIn("status", result.stdout)
            self.assertFalse(calls)

    def test_unknown_mode_and_extra_docker_mode_arguments_are_rejected(self):
        for args in (["unknown"], ["auto", "--agent-cli", "codex"], ["daily", "--force"]):
            with self.subTest(args=args):
                result, _, calls = self.invoke(args)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(calls)


if __name__ == "__main__":
    unittest.main()
