"""臨時休市（例如颱風假）：官方日期表尚未列入時，帳本與下一交易時段的行為。

情境：決策時預定的成交日（2026-09-21）臨時休市，官方不會有該日行情。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from etf_agent.data import TradingCalendar
from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository, VirtualAccountService
from etf_agent.virtual_account.daily import DailyAccountRunner
from test_virtual_account import prepare_and_decide
from test_virtual_account_daily import create_prices

TYPHOON_DAY = "2026-09-21"
HOLIDAY_ROWS = [{"kind": "calendar_event", "event_date": "2026-10-10", "name": "國慶日"}]


def calendar(*closed_days):
    facts = HOLIDAY_ROWS + [{"kind": "calendar_event", "event_date": day, "name": "颱風停止上班"} for day in closed_days]
    return TradingCalendar.from_ogd_facts(facts, "fixture-sha")


class TyphoonCalendarTests(unittest.TestCase):
    def test_closure_missing_from_official_table_is_still_scheduled(self):
        session = calendar().next_trading_session("2026-09-18T20:00:00+08:00")
        self.assertTrue(session["start"].startswith("2026-09-21T09:00"))

    def test_closure_listed_in_official_table_moves_session_to_next_trading_day(self):
        session = calendar(TYPHOON_DAY).next_trading_session("2026-09-18T20:00:00+08:00")
        self.assertTrue(session["start"].startswith("2026-09-22T09:00"))


class TyphoonLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        rules = self.root / "rules.json"
        rules.write_text(json.dumps({"initial_capital_twd": 1_000_000_000, "version": "fixture-v1"}), encoding="utf-8")
        self.repository = VirtualAccountRepository(self.root / "accounts", "cup-test")
        self.service = VirtualAccountService(self.repository)
        self.service.initialize(rules, "cup-test", "2026-09-18T17:00:00+08:00")
        self.database = self.root / "prices.db"
        create_prices(self.database, [])
        self.snapshot, self.decisions = prepare_and_decide(self.service, self.root)
        self.before = self.repository.latest()["run_id"]
        # 決策的目標時段是 TYPHOON_DAY；改寫複本內的 decision_input，讓 settle 走「驗證過的目標時段」路徑。
        original = next(self.decisions.glob("*/decision-1"))
        self.patched = self.root / "patched-decision"
        shutil.copytree(original, self.patched)
        bundle = json.loads((self.patched / "decision_input.json").read_text(encoding="utf-8"))
        bundle["trading_status_bundle"] = {"target_session": {"end": "%sT13:30:00+08:00" % TYPHOON_DAY}}
        (self.patched / "decision_input.json").write_text(json.dumps(bundle), encoding="utf-8")
        # 颱風假隔天（09-22）有資料，颱風假當天沒有。
        create_prices(self.database, [
            ("2317.TW", "2026-09-22", "120", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-22T12:00:00+00:00"),
            ("2330.TW", "2026-09-22", "500", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-22T12:00:00+00:00"),
        ])

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, closed_calendar=None):
        runner = DailyAccountRunner(self.repository, self.database, self.decisions, calendar=closed_calendar)
        return runner

    def settle(self, runner):
        with mock.patch("etf_agent.virtual_account.daily.DecisionRepository.verify", return_value=self.patched):
            return runner.settle()

    def test_pending_decision_waits_and_does_not_slide_to_next_day(self):
        result = self.settle(self.runner())
        self.assertEqual(result["status"], "waiting_for_close_data")
        self.assertEqual(result["trade_date"], TYPHOON_DAY)
        self.assertEqual(self.repository.latest()["run_id"], self.before)

    def test_new_prepare_stays_blocked_while_decision_is_pending(self):
        later = dict(self.snapshot, decision_cutoff="2026-09-22T12:00:00+00:00")
        later_path = self.root / "later.json"
        later_path.write_text(json.dumps(later), encoding="utf-8")
        runner = self.runner()
        with mock.patch("etf_agent.virtual_account.daily.DecisionRepository.verify", return_value=self.patched):
            result = runner.run(later_path, "prepare-next", self.root / "account.json")
        self.assertEqual(result["prepare"]["status"], "blocked_by_pending_decision")
        self.assertEqual(self.repository.latest()["run_id"], self.before)

    def test_closure_added_to_calendar_later_reports_closed_session_and_keeps_ledger(self):
        result = self.settle(self.runner(calendar(TYPHOON_DAY)))
        self.assertEqual(result["status"], "session_closed")
        self.assertEqual(result["trade_date"], TYPHOON_DAY)
        self.assertEqual(self.repository.latest()["run_id"], self.before)

    def test_settlement_date_still_rejects_closed_day(self):
        with self.assertRaisesRegex(VirtualAccountError, "不是交易日"):
            self.runner(calendar(TYPHOON_DAY)).settlement_date(TYPHOON_DAY)

    def test_closed_session_decision_is_voided_and_next_prepare_proceeds(self):
        later = dict(self.snapshot, decision_cutoff="2026-09-22T12:00:00+00:00")
        later_path = self.root / "later.json"
        later_path.write_text(json.dumps(later), encoding="utf-8")
        before_state = self.repository.latest()["state"]
        runner = self.runner(calendar(TYPHOON_DAY))
        decision_run = next(self.decisions.glob("*/decision-1"))
        with mock.patch("etf_agent.virtual_account.daily.DecisionRepository.verify", return_value=self.patched):
            result = runner.run(later_path, "prepare-next", self.root / "account.json")
        self.assertEqual(result["settle"]["status"], "voided_session_closed")
        self.assertEqual(result["prepare"]["status"], "prepared")
        self.assertFalse(decision_run.exists())
        archived = Path(result["settle"]["archived_to"])
        self.assertTrue(archived.is_dir())
        self.assertIn("voided_closed_sessions", archived.parts)
        record = json.loads((archived.parent / (archived.name + ".void.json")).read_text(encoding="utf-8"))
        self.assertEqual(record["reason"], "target_session_closed")
        self.assertEqual(record["trade_date"], TYPHOON_DAY)
        after_state = self.repository.latest()["state"]
        self.assertEqual(after_state["settled_cash"], before_state["settled_cash"])
        self.assertEqual(after_state["unsettled_cash"], before_state["unsettled_cash"])
        self.assertEqual(after_state["positions"], before_state["positions"])

    def test_without_calendar_closed_session_still_waits(self):
        later = dict(self.snapshot, decision_cutoff="2026-09-22T12:00:00+00:00")
        later_path = self.root / "later.json"
        later_path.write_text(json.dumps(later), encoding="utf-8")
        with mock.patch("etf_agent.virtual_account.daily.DecisionRepository.verify", return_value=self.patched):
            result = self.runner().run(later_path, "prepare-next", self.root / "account.json")
        self.assertEqual(result["prepare"]["status"], "blocked_by_pending_decision")
        self.assertTrue(next(self.decisions.glob("*/decision-1")).exists())


if __name__ == "__main__":
    unittest.main()
