import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class StartDataModeTests(unittest.TestCase):
    def run_mode(self, mode, *, empty=False, history_exit=0, token=True, prices_current=False, force_fetch=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "start.sh", root / "start.sh")
            # 抓資料模式會先經 scripts/with_data_lock.py 取得資料抓取鎖。
            (root / "scripts").mkdir()
            shutil.copyfile(ROOT / "scripts" / "with_data_lock.py", root / "scripts" / "with_data_lock.py")
            for name in ("Dockerfile", "requirements.txt"):
                (root / name).touch()
            (root / "data").mkdir()
            (root / "data" / "official_universe.csv").write_text(
                "symbol\n" if empty else "symbol\n2330.TW\n", encoding="utf-8"
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            docker = bin_dir / "docker"
            docker.write_text(
                '#!/bin/sh\n'
                'echo "$*" >> "$CALL_LOG"\n'
                'case "$*" in\n'
                '  *scripts/prices_current.py*) exit "$PRICES_CURRENT" ;;\n'
                '  *scripts/collect_history.py*) exit "$HISTORY_EXIT" ;;\n'
                'esac\n'
                'exit 0\n', encoding="utf-8"
            )
            docker.chmod(0o755)
            security = bin_dir / "security"
            security.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            security.chmod(0o755)
            log = root / "calls.txt"
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                       CALL_LOG=str(log), HISTORY_EXIT=str(history_exit),
                       PRICES_CURRENT="0" if prices_current else "1", FORCE_FETCH="1" if force_fetch else "0",
                       FINMIND_TOKEN="test-token" if token else "")
            result = subprocess.run(
                ["sh", str(root / "start.sh"), mode], env=env,
                capture_output=True, text=True, timeout=30,
            )
            return result, log.read_text(encoding="utf-8")

    def test_busy_data_lock_stops_collection_without_touching_docker(self):
        import fcntl

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "start.sh", root / "start.sh")
            (root / "scripts").mkdir()
            shutil.copyfile(ROOT / "scripts" / "with_data_lock.py", root / "scripts" / "with_data_lock.py")
            (root / "artifacts" / "daily").mkdir(parents=True)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            docker = bin_dir / "docker"
            docker.write_text('#!/bin/sh\necho "$*" >> "$CALL_LOG"\nexit 0\n', encoding="utf-8")
            docker.chmod(0o755)
            log = root / "calls.txt"
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", CALL_LOG=str(log), ETF_DATA_LOCK_WAIT_SECONDS="0")
            with (root / "artifacts" / "daily" / ".data.lock").open("a+b") as holder:
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for mode in ("data", "auto", "daily"):
                    with self.subTest(mode=mode):
                        result = subprocess.run(["sh", str(root / "start.sh"), mode], env=env,
                                                capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 75, result.stderr)
                        self.assertIn("等待", result.stderr)
                        self.assertFalse(log.exists())

    def test_auto_and_data_run_same_complete_collection_without_account(self):
        calls = []
        for mode in ("auto", "data"):
            with self.subTest(mode=mode):
                result, log = self.run_mode(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                for command in (
                    "scripts/collect_latest_prices.py", "scripts/collect_history.py",
                    "cli/data_agent.py collect", "scripts/collect_financial_statements.py",
                    "scripts/collect_sector_classification.py",
                    "TaiwanStockFinancialStatements", "TaiwanStockBalanceSheet",
                    "TaiwanStockCashFlowsStatement", "scripts/data_status.py",
                ):
                    self.assertIn(command, log)
                for forbidden in (
                    "cli/data_agent.py snapshot", "cli/virtual_account.py",
                    "scripts/collect_official_history.py", "scripts/probe_data_sources.py",
                ):
                    self.assertNotIn(forbidden, log)
                calls.append(log)
        self.assertEqual(*calls)

    def test_history_failure_still_collects_company_data_and_reports_failure(self):
        for mode in ("auto", "data"):
            with self.subTest(mode=mode):
                result, log = self.run_mode(mode, history_exit=7)
                self.assertEqual(result.returncode, 7)
                self.assertIn("cli/data_agent.py collect", log)
                self.assertIn("scripts/data_status.py", log)
                self.assertNotIn("cli/virtual_account.py", log)

    def test_empty_universe_only_collects_development_prices_and_reports_partial(self):
        for mode in ("auto", "data"):
            with self.subTest(mode=mode):
                result, log = self.run_mode(mode, empty=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn("scripts/collect_twse.py --all-listed", log)
                self.assertNotIn("cli/data_agent.py", log)
                self.assertNotIn("scripts/collect_history.py", log)
                self.assertNotIn("cli/virtual_account.py", log)

    def test_current_prices_skip_price_steps_but_still_collect_irregular_data(self):
        for mode in ("auto", "data"):
            with self.subTest(mode=mode):
                result, log = self.run_mode(mode, prices_current=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                for skipped in ("scripts/collect_latest_prices.py", "scripts/collect_history.py"):
                    self.assertNotIn(skipped, log)
                for command in (
                    "cli/data_agent.py collect", "scripts/collect_financial_statements.py",
                    "scripts/collect_sector_classification.py", "TaiwanStockFinancialStatements",
                ):
                    self.assertIn(command, log)

    def test_force_fetch_collects_prices_even_when_database_is_current(self):
        result, log = self.run_mode("auto", prices_current=True, force_fetch=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scripts/collect_latest_prices.py", log)
        self.assertIn("scripts/collect_history.py", log)

    def test_missing_optional_token_reports_partial_after_collection(self):
        result, log = self.run_mode("auto", token=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("scripts/collect_latest_prices.py", log)
        self.assertIn("cli/data_agent.py collect", log)
        self.assertNotIn("scripts/collect_supplemental_sources.py", log)


if __name__ == "__main__":
    unittest.main()
