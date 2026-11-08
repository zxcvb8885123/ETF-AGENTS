import json
import subprocess
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path

from etf_agent.automation.preflight import (
    PreflightError, agent_cli_versions, effective_research_agent_cli, required_files, require_preflight, run_preflight,
)
from etf_agent.virtual_account import VirtualAccountRepository, VirtualAccountService


Usage = namedtuple("Usage", "total used free")
GIB = 1024 ** 3
LOGGED_IN = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "someone@example.com"})


class FakeRunner:
    def __init__(self, docker_exit=0, auth_stdout=LOGGED_IN, raise_for=None, exec_help="  --ephemeral  不保存 session\n",
                 version_stdout="9.9.9 (Fake CLI)\nsecond line\n", version_exit=0):
        self.exec_help = exec_help
        self.docker_exit = docker_exit
        self.auth_stdout = auth_stdout
        self.raise_for = raise_for
        self.version_stdout = version_stdout
        self.version_exit = version_exit
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        self.assert_timeout(kwargs)
        if self.raise_for and command[0] == self.raise_for:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if command[0] == "docker":
            return subprocess.CompletedProcess(command, self.docker_exit, "29.7.2\n", "")
        if command[:3] == ["codex", "exec", "--help"]:
            return subprocess.CompletedProcess(command, 0, self.exec_help, "")
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, self.version_exit, self.version_stdout, "")
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
        repository = VirtualAccountRepository(self.root / "artifacts" / "ledger", "cup-test")
        VirtualAccountService(repository).initialize(rules, "cup-test", "2026-09-18T17:00:00+08:00")

    def tearDown(self):
        self.temp.cleanup()

    def run_checks(self, runner=None, which=which_all, free=50 * GIB, skip_data=False, account_id="cup-test", agent_cli="claude", research_agent_cli=None):
        checks = run_preflight(
            self.root, account_id, skip_data, runner=runner or FakeRunner(), which=which,
            disk_usage=lambda _: Usage(0, 0, free), min_free_bytes=2 * GIB, agent_cli=agent_cli,
            research_agent_cli=research_agent_cli,
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
        (self.root / "artifacts" / "data" / "research_snapshot_latest.json").unlink()
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

    def test_codex_main_defaults_researchers_to_codex(self):
        runner = FakeRunner()
        checks = self.run_checks(runner=runner, agent_cli="codex")
        self.assertEqual(list(checks), ["docker", "codex", "research_agent_cli", "account", "files", "disk"])
        self.assertTrue(all(check.ok for check in checks.values()))
        self.assertIn(["codex", "login", "status"], runner.commands)
        require_preflight(list(checks.values()))

    def test_codex_main_with_explicit_claude_researchers_checks_both_logins(self):
        runner = FakeRunner()
        checks = self.run_checks(runner=runner, agent_cli="codex", research_agent_cli="claude")
        self.assertEqual(list(checks), ["docker", "codex", "claude", "account", "files", "disk"])
        self.assertTrue(all(check.ok for check in checks.values()))

    def test_codex_main_with_opencode_researchers_checks_opencode_login(self):
        runner = FakeRunner(auth_stdout="1 credentials")
        checks = self.run_checks(runner=runner, agent_cli="codex", research_agent_cli="opencode")
        self.assertEqual(list(checks), ["docker", "codex", "opencode", "account", "files", "disk"])
        self.assertTrue(all(check.ok for check in checks.values()))

    def test_codex_main_fails_closed_when_researcher_login_is_missing(self):
        unauthenticated = FakeRunner(auth_stdout=json.dumps({"loggedIn": False}))
        checks = self.run_checks(runner=unauthenticated, agent_cli="codex", research_agent_cli="claude")
        self.assertFalse(checks["claude"].ok)
        with self.assertRaises(PreflightError):
            require_preflight(list(checks.values()))
        checks = self.run_checks(agent_cli="codex", research_agent_cli="claude", which=lambda name: None if name == "codex" else which_all(name))
        self.assertFalse(checks["codex"].ok)
        self.assertTrue(checks["claude"].ok)

    def test_codex_researchers_need_ephemeral_and_state_staged_isolation(self):
        for agent_cli, expected in (("claude", ["docker", "claude", "codex", "research_agent_cli"]),
                                    ("opencode", ["docker", "opencode", "codex", "research_agent_cli"])):
            runner = FakeRunner(auth_stdout="1 credentials")
            checks = self.run_checks(runner=runner, agent_cli=agent_cli, research_agent_cli="codex")
            self.assertEqual(list(checks)[:4], expected)
            self.assertTrue(checks["research_agent_cli"].ok)
            self.assertIn("輸出暫存", checks["research_agent_cli"].detail)
            self.assertIn("舊 run", checks["research_agent_cli"].detail)
            self.assertIn(["codex", "login", "status"], runner.commands)
            self.assertIn(["codex", "exec", "--help"], runner.commands)

    def test_codex_main_and_codex_researchers_check_codex_login_once(self):
        runner = FakeRunner()
        checks = self.run_checks(runner=runner, agent_cli="codex", research_agent_cli="codex")
        self.assertEqual(list(checks), ["docker", "codex", "research_agent_cli", "account", "files", "disk"])
        self.assertEqual(sum(1 for command in runner.commands if command[:2] == ["codex", "login"]), 1)
        self.assertTrue(all(check.ok for check in checks.values()))

    def test_codex_researchers_fail_closed_without_ephemeral_or_login(self):
        old_version = self.run_checks(runner=FakeRunner(exec_help="  --sandbox  沙箱\n"), agent_cli="codex", research_agent_cli="codex")
        self.assertFalse(old_version["research_agent_cli"].ok)
        self.assertIn("--ephemeral", old_version["research_agent_cli"].detail)
        with self.assertRaises(PreflightError):
            require_preflight(list(old_version.values()))
        timeout = self.run_checks(runner=FakeRunner(raise_for="codex"), agent_cli="codex", research_agent_cli="codex")
        self.assertFalse(timeout["codex"].ok)
        self.assertFalse(timeout["research_agent_cli"].ok)
        missing = self.run_checks(agent_cli="codex", research_agent_cli="codex",
                                  which=lambda name: None if name == "codex" else which_all(name))
        self.assertFalse(missing["codex"].ok)
        self.assertFalse(missing["research_agent_cli"].ok)

    def test_non_codex_researchers_never_run_codex_commands(self):
        runner = FakeRunner()
        self.run_checks(runner=runner, agent_cli="claude", research_agent_cli="claude")
        self.assertFalse([command for command in runner.commands if command[0] == "codex"])

    def test_same_researcher_cli_is_checked_once(self):
        runner = FakeRunner()
        checks = self.run_checks(runner=runner, agent_cli="claude", research_agent_cli="claude")
        self.assertEqual(list(checks), ["docker", "claude", "account", "files", "disk"])
        self.assertEqual(sum(1 for command in runner.commands if command[0] == "claude"), 1)

    def test_different_researcher_cli_is_checked_in_addition(self):
        runner = FakeRunner(auth_stdout=json.dumps({"loggedIn": True, "authMethod": "x"}))
        checks = self.run_checks(runner=runner, agent_cli="opencode", research_agent_cli="claude")
        self.assertIn("claude", checks)
        self.assertIn("opencode", checks)

    def test_effective_research_agent_cli_defaults(self):
        self.assertEqual(effective_research_agent_cli("claude"), "claude")
        self.assertEqual(effective_research_agent_cli("opencode"), "opencode")
        self.assertEqual(effective_research_agent_cli("codex"), "codex")
        self.assertEqual(effective_research_agent_cli("codex", "claude"), "claude")
        self.assertEqual(effective_research_agent_cli("codex", "opencode"), "opencode")
        self.assertEqual(effective_research_agent_cli("claude", "opencode"), "opencode")

    def test_unknown_researcher_cli_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_checks(research_agent_cli="gemini")

    def test_opencode_selected_and_credential_failure_is_closed(self):
        runner = FakeRunner(auth_stdout="1 credentials")
        checks = self.run_checks(runner=runner, agent_cli="opencode")
        self.assertTrue(checks["opencode"].ok)
        self.assertIn(["opencode", "auth", "list", "--pure"], runner.commands)
        for fallback in (FakeRunner(auth_stdout="0 credentials"), FakeRunner(raise_for="opencode")):
            self.assertFalse(self.run_checks(runner=fallback, agent_cli="opencode")["opencode"].ok)
        self.assertFalse(self.run_checks(agent_cli="opencode", which=lambda name: None if name == "opencode" else which_all(name))["opencode"].ok)

    def test_opencode_credentials_with_ansi_colour_codes_are_recognised(self):
        runner = FakeRunner(auth_stdout="\x1b[0m\x1b[32m1 credentials\x1b[0m")
        self.assertTrue(self.run_checks(runner=runner, agent_cli="opencode")["opencode"].ok)

    def test_uninitialized_account_fails_without_creating_directory(self):
        checks = self.run_checks(account_id="missing")
        self.assertIn("尚未開帳", checks["account"].detail)
        self.assertFalse((self.root / "artifacts" / "ledger" / "missing").exists())

    def test_tampered_account_state_fails(self):
        index = self.root / "artifacts" / "ledger" / "cup-test" / "latest.json"
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


class AgentCliVersionTests(unittest.TestCase):
    def test_records_first_line_of_main_and_researcher_cli_versions(self):
        runner = FakeRunner()
        self.assertEqual(agent_cli_versions("claude", None, runner=runner, which=which_all), {"claude": "9.9.9 (Fake CLI)"})
        self.assertEqual([command for command in runner.commands if command[1:] == ["--version"]], [["claude", "--version"]])
        runner = FakeRunner(version_stdout="\x1b[32mcodex-cli 0.1.0\x1b[0m\n")
        versions = agent_cli_versions("codex", "opencode", runner=runner, which=which_all)
        self.assertEqual(versions, {"codex": "codex-cli 0.1.0", "opencode": "codex-cli 0.1.0"})
        self.assertEqual(list(versions), ["codex", "opencode"])

    def test_same_cli_is_queried_once(self):
        runner = FakeRunner()
        self.assertEqual(agent_cli_versions("codex", "codex", runner=runner, which=which_all), {"codex": "9.9.9 (Fake CLI)"})
        self.assertEqual(sum(1 for command in runner.commands if command[1:] == ["--version"]), 1)

    def test_failures_are_recorded_as_none_and_never_raise(self):
        self.assertEqual(agent_cli_versions("claude", runner=FakeRunner(), which=lambda name: None), {"claude": None})
        self.assertEqual(agent_cli_versions("claude", runner=FakeRunner(raise_for="claude"), which=which_all), {"claude": None})
        self.assertEqual(agent_cli_versions("claude", runner=FakeRunner(version_exit=1), which=which_all), {"claude": None})
        self.assertEqual(agent_cli_versions("claude", runner=FakeRunner(version_stdout="  \n"), which=which_all), {"claude": None})

    def test_long_output_is_truncated(self):
        runner = FakeRunner(version_stdout="v" * 500)
        self.assertEqual(len(agent_cli_versions("opencode", runner=runner, which=which_all)["opencode"]), 80)


if __name__ == "__main__":
    unittest.main()
