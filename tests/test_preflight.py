import json
import subprocess
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path

from etf_agent.automation.preflight import PreflightError, required_files, require_preflight, run_preflight
from etf_agent.virtual_account import VirtualAccountRepository, VirtualAccountService


Usage = namedtuple("Usage", "total used free")
GIB = 1024 ** 3
LOGGED_IN = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "someone@example.com"})


class FakeRunner:
    def __init__(self, docker_exit=0, auth_stdout=LOGGED_IN, raise_for=None):
        self.docker_exit = docker_exit
        self.auth_stdout = auth_stdout
        self.raise_for = raise_for
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        self.assert_timeout(kwargs)
        if self.raise_for and command[0] == self.raise_for:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if command[0] == "docker":
            return subprocess.CompletedProcess(command, self.docker_exit, "29.7.2\n", "")
        return subprocess.CompletedProcess(command, 0, self.auth_stdout, "")

    @staticmethod
    def assert_timeout(kwargs):
        if not kwargs.get("timeout"):
            raise AssertionError("外部指令必須設定逾時")


def which_all(name):
    return "/usr/local/bin/%s" % name


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for path in required_files("cup-test", skip_data=True):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        rules = self.root / "rules.json"
        rules.write_text(json.dumps({"initial_capital_twd": 1_000_000_000, "version": "fixture-v1"}), encoding="utf-8")
        repository = VirtualAccountRepository(self.root / "artifacts" / "virtual_accounts", "cup-test")
        VirtualAccountService(repository).initialize(rules, "cup-test", "2026-09-18T17:00:00+08:00")

    def tearDown(self):
        self.temp.cleanup()

    def run_checks(self, runner=None, which=which_all, free=50 * GIB, skip_data=False, account_id="cup-test"):
        checks = run_preflight(
            self.root, account_id, skip_data, runner=runner or FakeRunner(), which=which,
            disk_usage=lambda _: Usage(0, 0, free), min_free_bytes=2 * GIB,
        )
        return {check.name: check for check in checks}

    def test_all_checks_pass(self):
        checks = self.run_checks()
        self.assertEqual(list(checks), ["docker", "claude", "account", "files", "disk"])
        self.assertTrue(all(check.ok for check in checks.values()))
        require_preflight(list(checks.values()))

    def test_login_identity_is_not_recorded(self):
        checks = self.run_checks()
        self.assertNotIn("someone@example.com", checks["claude"].detail)

    def test_skip_data_does_not_need_docker_but_needs_existing_snapshot(self):
        runner = FakeRunner(docker_exit=1)
        checks = self.run_checks(runner=runner, skip_data=True)
        self.assertNotIn("docker", checks)
        self.assertTrue(all(check.ok for check in checks.values()))
        self.assertFalse(any(command[0] == "docker" for command in runner.commands))
        (self.root / "artifacts" / "research_snapshot_latest.json").unlink()
        checks = self.run_checks(skip_data=True)
        self.assertFalse(checks["files"].ok)
        self.assertIn("research_snapshot_latest.json", checks["files"].detail)

    def test_docker_missing_or_daemon_down(self):
        checks = self.run_checks(which=lambda name: None if name == "docker" else which_all(name))
        self.assertIn("找不到 docker", checks["docker"].detail)
        checks = self.run_checks(runner=FakeRunner(docker_exit=1))
        self.assertIn("Docker Desktop", checks["docker"].detail)
        checks = self.run_checks(runner=FakeRunner(raise_for="docker"))
        self.assertFalse(checks["docker"].ok)

    def test_claude_not_logged_in_bad_output_or_timeout(self):
        for runner in (
            FakeRunner(auth_stdout=json.dumps({"loggedIn": False})),
            FakeRunner(auth_stdout="not json"),
            FakeRunner(raise_for="claude"),
        ):
            self.assertFalse(self.run_checks(runner=runner)["claude"].ok)
        checks = self.run_checks(which=lambda name: None if name == "claude" else which_all(name))
        self.assertIn("PATH", checks["claude"].detail)

    def test_uninitialized_account_fails_without_creating_directory(self):
        checks = self.run_checks(account_id="missing")
        self.assertIn("尚未開帳", checks["account"].detail)
        self.assertFalse((self.root / "artifacts" / "virtual_accounts" / "missing").exists())

    def test_tampered_account_state_fails(self):
        index = self.root / "artifacts" / "virtual_accounts" / "cup-test" / "latest.json"
        payload = json.loads(index.read_text(encoding="utf-8"))
        payload["state_sha256"] = "0" * 64
        index.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIn("驗證失敗", self.run_checks()["account"].detail)

    def test_missing_database_and_low_disk_are_all_reported(self):
        (self.root / "var" / "etf_agent.db").unlink()
        checks = self.run_checks(free=GIB)
        self.assertIn("var/etf_agent.db", checks["files"].detail)
        self.assertFalse(checks["disk"].ok)
        with self.assertRaises(PreflightError) as caught:
            require_preflight(list(checks.values()))
        self.assertIn("files", str(caught.exception))
        self.assertIn("disk", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
