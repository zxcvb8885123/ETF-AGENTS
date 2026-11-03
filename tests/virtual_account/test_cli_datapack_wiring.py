"""`cli/virtual_account.py daily／settle` 接上主辦方 DataPack：讀 competition_profile 的 team_id 與 --datapack-dir，
voided 的日子結算為無成交（HOST_VOIDED）；讀不到 team_id 時維持純模擬成交並印明確訊息。"""

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from etf_agent.ledger import HOST_VOIDED
from etf_agent.virtual_account import VirtualAccountRepository, VirtualAccountService

from test_host_outcome_settlement import TEAM, TRADE_DATE, datapack, prepare_and_decide
from test_virtual_account_daily import create_prices

ROOT = Path(__file__).resolve().parents[2]


def load_cli():
    spec = importlib.util.spec_from_file_location("virtual_account_cli_under_test", ROOT / "cli" / "virtual_account.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CliDataPackWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = load_cli()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        rules = self.root / "rules.json"
        rules.write_text(json.dumps({"initial_capital_twd": 1_000_000_000, "version": "fixture-v1"}), encoding="utf-8")
        self.accounts = self.root / "accounts"
        self.repository = VirtualAccountRepository(self.accounts, "cup-test")
        VirtualAccountService(self.repository).initialize(rules, "cup-test", "2026-09-18T17:00:00+08:00")
        self.database = self.root / "prices.db"
        create_prices(self.database, [
            ("2317.TW", TRADE_DATE, "120", 1_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00", 120 * 1_000),
            ("2330.TW", TRADE_DATE, "500", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        self.datapacks = self.root / "datapack"
        self.datapacks.mkdir()
        (self.datapacks / "pack.json").write_text(json.dumps(datapack()), encoding="utf-8")
        self.profile = self.root / "competition_profile.json"
        self.profile.write_text(json.dumps({"schema_version": "1.0", "team_id": TEAM}), encoding="utf-8")
        self.decision, self.decisions = prepare_and_decide(VirtualAccountService(self.repository), self.root)

    def settle(self, profile=None):
        argv = ["--repository", str(self.accounts), "--account-id", "cup-test", "settle",
                "--database", str(self.database), "--decision-repository", str(self.decisions),
                "--captures-root", str(self.root / "captures"), "--datapack-dir", str(self.datapacks),
                "--competition-profile", str(profile or self.profile)]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(self.cli, "load_calendar", return_value=None), redirect_stdout(out), redirect_stderr(err):
            code = self.cli.main(argv)
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_voided_datapack_settles_as_host_voided_through_the_cli(self):
        code, result, err = self.settle()
        self.assertEqual(code, 0, err)
        self.assertEqual((result["status"], result["fills"], result["unfilled"], result["host_outcome"]), ("settled", 0, 1, "voided"))
        transition = json.loads((self.repository.root / "runs" / result["run_id"] / "transition.json").read_text(encoding="utf-8"))
        self.assertEqual(transition["execution"]["unfilled_orders"][0]["reason"], HOST_VOIDED)
        state = self.repository.latest()["state"]
        self.assertEqual(state["positions"], [])
        self.assertEqual(state["provenance"]["host_outcome"]["outcome"], "voided")

    def test_missing_team_id_keeps_pure_simulation_and_says_so(self):
        for profile_body in ({"schema_version": "1.0"}, {"team_id": ""}):
            with self.subTest(profile=profile_body):
                self.profile.write_text(json.dumps(profile_body), encoding="utf-8")
                self.repository = VirtualAccountRepository(self.accounts, "cup-test")
                code, result, err = self.settle()
                self.assertEqual(code, 0, err)
                self.assertIn("team_id", err)
                self.assertIn("純模擬成交", err)
                self.assertEqual((result["fills"], result["unfilled"]), (1, 0))
                self.assertNotIn("host_outcome", result)
                break  # 一次結算後 prepare 狀態已消耗；第二種設定由 load_team_id 單元測試覆蓋

    def test_load_team_id_fails_soft_for_missing_or_empty(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(self.cli.load_team_id(self.root / "nope.json"))
            self.profile.write_text(json.dumps({"team_id": "   "}), encoding="utf-8")
            self.assertIsNone(self.cli.load_team_id(self.profile))
            self.profile.write_text(json.dumps({"team_id": " TEAM_X "}), encoding="utf-8")
            self.assertEqual(self.cli.load_team_id(self.profile), "TEAM_X")
        self.assertEqual(err.getvalue().count("純模擬成交"), 2)

    def test_defaults_point_at_var_datapack_and_competition_profile(self):
        captured = {}

        class Runner:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)

            def settle(self):
                return {"status": "nothing_pending"}

        out = io.StringIO()
        with patch.object(self.cli, "load_calendar", return_value=None), patch.object(self.cli, "DailyAccountRunner", Runner), \
                patch.object(self.cli, "load_team_id", side_effect=lambda path: ("TEAM_D", captured.setdefault("profile", path))[0]), \
                redirect_stdout(out):
            code = self.cli.main(["--repository", str(self.accounts), "--account-id", "cup-test", "settle",
                                  "--database", str(self.database), "--decision-repository", str(self.decisions)])
        self.assertEqual(code, 0)
        self.assertEqual(captured["datapack_dir"], ROOT / "var" / "datapack")
        self.assertEqual(captured["team_id"], "TEAM_D")
        self.assertEqual(captured["profile"], ROOT / "config" / "competition_profile.json")

    def test_start_script_passes_datapack_dir_to_daily(self):
        script = (ROOT / "start.sh").read_text(encoding="utf-8")
        self.assertIn("--datapack-dir var/datapack", script)
        self.assertIn("--competition-profile config/competition_profile.json", script)


if __name__ == "__main__":
    unittest.main()
