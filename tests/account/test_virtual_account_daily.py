import importlib.util
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from etf_agent.core import content_sha256
from etf_agent.dashboard import build_performance_summary
from etf_agent.data import TradingCalendar, TradingCalendarError
from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository, VirtualAccountService
from etf_agent.ledger import ExecutionSimulator, LedgerError
from etf_agent.virtual_account.daily import (
    DailyAccountRunner, OfficialCloseMarket, add_business_days, average_price, pending_decision,
    restore_superseded_decision, supersede_decision_runs,
)
from test_virtual_account import prepare_and_decide


def create_prices(path, rows):
    """rows 為 (symbol, date, close, volume, source, fetched[, trade_value])；省略成交金額時均價等於收盤價。"""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS raw_payloads (id INTEGER PRIMARY KEY, source TEXT, fetched_at TEXT, sha256 TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_prices (
            symbol TEXT, trade_date TEXT, close_price TEXT, volume_shares INTEGER,
            source TEXT, fetched_at TEXT, raw_payload_id INTEGER, trade_value INTEGER
        );
        INSERT OR IGNORE INTO raw_payloads (id, sha256) VALUES (1, 'raw-sha');
        """
    )
    connection.executemany(
        "INSERT INTO daily_prices VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        [tuple(row[:6]) + ((row[6] if len(row) > 6 else int(Decimal(row[2]) * row[3])),) for row in rows],
    )
    connection.commit()
    connection.close()


class SupersedeDecisionRunsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.decisions = self.root / "decisions"

    def tearDown(self):
        self.temp.cleanup()

    def make_run(self, name, snapshot):
        run = self.decisions / "2026-10-21" / name
        run.mkdir(parents=True)
        (run / "decision_input.json").write_text(json.dumps({"account_snapshot": snapshot}), encoding="utf-8")

    def test_older_run_with_same_account_snapshot_is_archived(self):
        self.make_run("decision-old", {"id": 1})
        self.make_run("decision-new", {"id": 1})
        self.make_run("decision-other", {"id": 2})
        archive = self.root / "archive"
        self.assertEqual(supersede_decision_runs(self.decisions, "decision-new", archive), ["decision-old"])
        self.assertEqual(sorted(path.name for path in self.decisions.glob("*/*")), ["decision-new", "decision-other"])
        self.assertTrue((archive / "2026-10-21" / "decision-old" / "decision_input.json").exists())

    def test_without_duplicates_nothing_moves(self):
        self.make_run("decision-new", {"id": 1})
        self.assertEqual(supersede_decision_runs(self.decisions, "decision-new", self.root / "archive"), [])
        self.assertFalse((self.root / "archive").exists())


class DailyAccountRunnerTests(unittest.TestCase):
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

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, decisions=None):
        return DailyAccountRunner(self.repository, self.database, decisions or self.root / "decisions")

    def test_business_day_settlement_skips_weekend(self):
        self.assertEqual(add_business_days(date(2026, 9, 24), 2), date(2026, 9, 28))

    def test_decision_waits_for_official_close_and_blocks_new_prepare(self):
        snapshot, decisions = prepare_and_decide(self.service, self.root)
        # 只有決策 cutoff 前的收盤價，或只有 Yahoo 資料時都不能結算。
        create_prices(self.database, [
            ("2317.TW", "2026-09-18", "100", 5_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-18T14:00:00+00:00"),
            ("2317.TW", "2026-09-21", "101", 5_000_000, "YAHOO_FINANCE", "2026-09-21T10:00:00+00:00"),
        ])
        runner = self.runner(decisions)
        self.assertEqual(runner.settle()["status"], "waiting_for_close_data")
        later = dict(snapshot, decision_cutoff="2026-09-21T12:00:00+00:00")
        later_path = self.root / "later.json"
        later_path.write_text(json.dumps(later), encoding="utf-8")
        result = runner.run(later_path, "prepare-next", self.root / "account.json")
        self.assertEqual(result["prepare"]["status"], "blocked_by_pending_decision")
        self.assertEqual(self.repository.latest()["run_id"], "prepare-with-orders")

    def test_settles_at_next_trading_day_official_close(self):
        snapshot, decisions = prepare_and_decide(self.service, self.root)
        decision = json.loads(next(decisions.glob("*/decision-1/decision.json")).read_text(encoding="utf-8"))
        order = decision["orders"][0]
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
            ("2317.TW", "2026-09-21", "999", 50_000_000, "YAHOO_FINANCE", "2026-09-21T12:00:00+00:00"),
            ("2330.TW", "2026-09-21", "500", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        result = self.runner(decisions).settle()
        self.assertEqual(result["status"], "settled")
        self.assertEqual(result["trade_date"], "2026-09-21")
        self.assertEqual(result["settlement_date"], "2026-09-23")
        state = self.repository.latest()["state"]
        self.assertEqual(state["provenance"]["type"], "close")
        self.assertEqual(state["provenance"]["trade_date"], "2026-09-21")
        position = next(row for row in state["positions"] if row["symbol"] == "2317.TW")
        self.assertEqual(position["shares"], order["shares"])
        self.assertEqual(position["close_price"], "120")
        # 以收盤價 120 成交：成本＝成交金額＋手續費（0.1425%，無條件進位到元）。
        gross = Decimal(120) * order["shares"]
        commission = (gross * Decimal("0.001425")).to_integral_value(rounding="ROUND_CEILING")
        self.assertEqual(Decimal(state["settled_cash"]), Decimal(1_000_000_000) - gross - commission)
        summary = build_performance_summary(self.repository)
        self.assertEqual(summary["today"]["trade_date"], "2026-09-21")
        self.assertEqual(summary["fees"]["commission"], "%s.00" % commission)
        self.assertEqual(self.runner(decisions).settle()["status"], "nothing_pending")

    def settle_with_calendar(self, rows):
        snapshot, decisions = prepare_and_decide(self.service, self.root)
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
            ("2330.TW", "2026-09-21", "500", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        calendar = TradingCalendar.from_ogd_facts(
            [{"kind": "calendar_event", "event_date": day, "name": name} for day, name in rows], "fixture-sha"
        )
        runner = DailyAccountRunner(self.repository, self.database, decisions, calendar=calendar)
        return runner.settle()

    def test_official_calendar_settlement_skips_holiday_and_counts_settlement_only_day(self):
        result = self.settle_with_calendar([("2026-09-22", "國定假日"), ("2026-09-23", "市場無交易，僅辦理結算交割作業")])
        self.assertEqual(result["status"], "settled")
        self.assertEqual(result["settlement_date"], "2026-09-24")
        self.assertEqual(result["settlement_calendar_basis"], "twse_ogd_calendar")
        self.assertEqual(result["settlement_calendar_source_sha256"], "fixture-sha")

    def test_official_calendar_rejects_trade_on_closed_day(self):
        with self.assertRaisesRegex(VirtualAccountError, "不是交易日"):
            self.settle_with_calendar([("2026-09-21", "國定假日")])
        self.assertEqual(self.repository.latest()["run_id"], "prepare-with-orders")
        self.assertFalse((self.repository.root / "inputs" / "close-2026-09-21").exists())

    def test_official_calendar_rejects_uncovered_year(self):
        with self.assertRaisesRegex(TradingCalendarError, "未涵蓋 2026"):
            self.settle_with_calendar([("2027-01-01", "開國紀念日")])

    def test_fills_in_full_at_official_average_price_and_values_at_close(self):
        snapshot, decisions = prepare_and_decide(self.service, self.root)
        order = json.loads(next(decisions.glob("*/decision-1/decision.json")).read_text(encoding="utf-8"))["orders"][0]
        # 當日只成交 1 張，仍全部成交；成交均價 605,000 ÷ 5,000 = 121，收盤 120。
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 5_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00", 605_000),
            ("2330.TW", "2026-09-21", "500", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        self.assertLess(5_000, order["shares"])
        result = self.runner(decisions).settle()
        self.assertEqual((result["fills"], result["unfilled"]), (1, 0))
        market = json.loads((self.repository.root / "inputs" / "close-2026-09-21" / "execution_market.json").read_text(encoding="utf-8"))
        self.assertEqual(market["price_rule"], "official_average_price")
        self.assertEqual(market["liquidity_cap"], "none")
        self.assertEqual(market["quotes"]["2317.TW"]["execution_price"], "121.00")
        self.assertIsNone(market["quotes"]["2317.TW"]["available_lots"])
        state = self.repository.latest()["state"]
        position = next(row for row in state["positions"] if row["symbol"] == "2317.TW")
        self.assertEqual(position["shares"], order["shares"])
        self.assertEqual(position["close_price"], "120")
        gross = Decimal(121) * order["shares"]
        commission = (gross * Decimal("0.001425")).to_integral_value(rounding="ROUND_CEILING")
        self.assertEqual(Decimal(state["settled_cash"]), Decimal(1_000_000_000) - gross - commission)

    def test_symbol_without_trades_has_no_average_price_and_is_not_filled(self):
        snapshot, decisions = prepare_and_decide(self.service, self.root)
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 0, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00", 0),
            ("2330.TW", "2026-09-21", "500", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        result = self.runner(decisions).settle()
        self.assertEqual((result["fills"], result["unfilled"]), (0, 1))
        transition = json.loads((self.repository.root / "runs" / result["run_id"] / "transition.json").read_text(encoding="utf-8"))
        self.assertEqual(transition["execution"]["unfilled_orders"][0]["reason"], "NOT_TRADABLE")

    def test_average_price_rounds_half_up_to_cent(self):
        self.assertEqual(average_price(1_000_001, 3_000), Decimal("333.33"))
        self.assertEqual(average_price(1_000_015, 1_000), Decimal("1000.02"))
        self.assertIsNone(average_price(0, 1_000))
        self.assertIsNone(average_price(1_000, 0))

    def test_simulator_liquidity_cap_is_explicit(self):
        simulator = ExecutionSimulator({"lot_size": 1000, "commission_rate": "0.001425", "minimum_commission": "0",
                                        "sell_tax_rate": "0.003", "reuse_sell_proceeds": True})
        decision = {"status": "approved", "orders": [{"symbol": "2330.TW", "side": "buy", "shares": 3000}]}
        decision["content_sha256"] = content_sha256(decision)
        def market(lots):
            return {"price_basis": "unadjusted", "available_at": "2026-09-21T13:30:00+08:00",
                    "quotes": {"2330.TW": {"tradable": True, "available_lots": lots, "execution_price": "100"}}}
        uncapped = simulator.run(decision, market(None), Decimal(10_000_000), "2026-09-21T13:30:00+08:00")
        self.assertEqual(uncapped["fills"][0]["shares"], 3000)
        capped = simulator.run(decision, market(1), Decimal(10_000_000), "2026-09-21T13:30:00+08:00")
        self.assertEqual(capped["unfilled_orders"][0]["unfilled_shares"], 2000)
        # 無量能上限時仍受買力限制。
        cash_limited = simulator.run(decision, market(None), Decimal(250_000), "2026-09-21T13:30:00+08:00")
        self.assertEqual(cash_limited["fills"][0]["shares"], 2000)
        for invalid in (-1, "3", True):
            with self.assertRaises(LedgerError):
                simulator.run(decision, market(invalid), Decimal(10_000_000), "2026-09-21T13:30:00+08:00")

    def test_prepare_without_decision_carries_forward(self):
        snapshot = {
            "snapshot_id": "snapshot-1", "decision_cutoff": "2026-09-21T12:00:00+00:00",
            "usable": True, "latest_trade_date": "2026-09-21",
            "latest_prices": [{"symbol": "2330.TW", "analysis_close_price": "100", "close_price": "100"}],
        }
        path = self.root / "snapshot.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        first = self.runner().run(path, "prepare-1", self.root / "account.json")
        self.assertEqual(first["settle"]["status"], "nothing_pending")
        self.assertEqual(first["prepare"]["status"], "prepared")
        snapshot.update(snapshot_id="snapshot-2", decision_cutoff="2026-09-22T12:00:00+00:00", latest_trade_date="2026-09-22")
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        second = self.runner().run(path, "prepare-2", self.root / "account.json")
        self.assertEqual(second["settle"]["status"], "no_decision")
        self.assertEqual(self.repository.latest()["run_id"], "prepare-2")
        history = build_performance_summary(self.repository)["history"]
        self.assertEqual([row["trade_date"] for row in history], ["2026-09-18", "2026-09-21", "2026-09-22"])

    def test_same_snapshot_reuses_state_and_restores_output(self):
        snapshot = {"snapshot_id": "same", "decision_cutoff": "2026-09-21T00:55:00+00:00",
                    "usable": True, "latest_trade_date": "2026-09-18",
                    "latest_prices": [{"symbol": "2330.TW", "close_price": "100"}]}
        path, output = self.root / "snapshot.json", self.root / "account.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        self.runner().run(path, "prepare-first", output)
        expected = output.read_bytes()
        output.unlink()
        repeated = self.runner().run(path, "prepare-second", output)
        self.assertEqual(repeated["prepare"]["status"], "reused")
        self.assertEqual(output.read_bytes(), expected)
        self.assertEqual(self.repository.latest()["run_id"], "prepare-first")

    def test_prices_captured_after_observation_are_not_used(self):
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 1000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        market = OfficialCloseMarket(self.database)
        self.assertIsNone(market.build("2026-09-21", ["2317.TW"], "2026-09-21T08:00:00+00:00"))
        self.assertIsNotNone(market.build("2026-09-21", ["2317.TW"], "2026-09-21T12:00:00+00:00"))

    def test_latest_available_price_version_is_independent_of_insert_order(self):
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "121", 1000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
            ("2317.TW", "2026-09-21", "120", 1000, "TWSE_STOCK_DAY_ALL", "2026-09-21T11:00:00+00:00"),
        ])
        market = OfficialCloseMarket(self.database)
        self.assertEqual(market.quotes("2026-09-21")["2317.TW"]["close_price"], "121")
        self.assertEqual(market.quotes("2026-09-21", "2026-09-21T11:30:00+00:00")["2317.TW"]["close_price"], "120")

    def test_invalid_next_snapshot_does_not_settle_pending_decision(self):
        snapshot, decisions = prepare_and_decide(self.service, self.root)
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 1000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        snapshot.update(decision_cutoff="2026-09-22T00:55:00+00:00", latest_prices=[])
        path = self.root / "invalid.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.runner(decisions).run(path, "bad-prepare", self.root / "output.json")
        self.assertEqual(self.repository.latest()["run_id"], "prepare-with-orders")

    def test_close_market_waits_for_all_required_symbols(self):
        market = OfficialCloseMarket(self.database)
        self.assertIsNone(market.build("2026-09-21", ["2330.TW"]))
        create_prices(self.database, [
            ("2317.TW", "2026-09-21", "120", 1000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])
        self.assertIsNone(market.build("2026-09-21", ["2330.TW"]))
        execution, close = market.build("2026-09-21", ["2317.TW"])
        self.assertEqual(execution["execution_at"], "2026-09-21T13:30:00+08:00")


class PendingDecisionFixture:
    """帳本已有一份目標時段為 9/21 的待成交決策。"""

    SESSION_START = "2026-09-21T09:00:00+08:00"

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
        # settle 與取代判斷讀驗證後路徑內的 decision_input；以複本加上目標時段。
        self.original = next(self.decisions.glob("*/decision-1"))
        self.patched = self.root / "patched-decision"
        shutil.copytree(self.original, self.patched)
        bundle = json.loads((self.patched / "decision_input.json").read_text(encoding="utf-8"))
        bundle["trading_status_bundle"] = {"target_session": {
            "start": self.SESSION_START, "end": "2026-09-21T13:30:00+08:00"}}
        (self.patched / "decision_input.json").write_text(json.dumps(bundle), encoding="utf-8")
        self.archive = self.root / "archive"

    def tearDown(self):
        self.temp.cleanup()

    def rerun(self, cutoff):
        path = self.root / "next.json"
        path.write_text(json.dumps(dict(self.snapshot, decision_cutoff=cutoff)), encoding="utf-8")
        runner = DailyAccountRunner(self.repository, self.database, self.decisions, archive_root=self.archive)
        with mock.patch("etf_agent.virtual_account.daily.DecisionRepository.verify", return_value=self.patched):
            return runner.run(path, "prepare-morning", self.root / "account.json")


class SupersedeBeforeSessionTests(PendingDecisionFixture, unittest.TestCase):
    """晚上已有待成交決策，隔天交件期限（08:55）前用新資料重跑可取代；過期限、已開盤或收盤價晚到仍阻擋。"""

    def test_cutoff_before_session_start_archives_decision_and_prepares(self):
        result = self.rerun("2026-09-21T07:30:00+08:00")
        self.assertEqual(result["settle"]["status"], "superseded_before_session")
        self.assertEqual(result["settle"]["decision_run_id"], "decision-1")
        self.assertEqual(result["prepare"]["status"], "prepared")
        self.assertEqual(self.repository.latest()["run_id"], "prepare-morning")
        self.assertFalse(self.original.exists())
        archived = self.archive / self.original.parent.name / "decision-1"
        self.assertEqual(Path(result["settle"]["archived_to"]), archived)
        self.assertTrue((archived / "decision.json").exists())

    def test_cutoff_after_session_start_stays_blocked(self):
        result = self.rerun("2026-09-21T10:00:00+08:00")
        self.assertEqual(result["prepare"]["status"], "blocked_by_pending_decision")
        self.assertEqual(self.repository.latest()["run_id"], self.before)
        self.assertTrue(self.original.exists())
        self.assertFalse(self.archive.exists())

    def test_cutoff_after_submission_deadline_stays_blocked(self):
        # 08:55 起的新 cutoff 會排到下一個時段，取代會讓原時段沒有決策。
        result = self.rerun("2026-09-21T08:57:00+08:00")
        self.assertEqual(result["prepare"]["status"], "blocked_by_pending_decision")
        self.assertEqual(self.repository.latest()["run_id"], self.before)
        self.assertTrue(self.original.exists())

    def test_late_close_data_next_morning_stays_blocked(self):
        # 成交日收盤後官方價量尚未入庫，隔天早上重跑：時段已過，不得取代。
        result = self.rerun("2026-09-22T07:30:00+08:00")
        self.assertEqual(result["settle"]["status"], "waiting_for_close_data")
        self.assertEqual(result["prepare"]["status"], "blocked_by_pending_decision")
        self.assertEqual(self.repository.latest()["run_id"], self.before)
        self.assertTrue(self.original.exists())

    def test_archive_name_collision_fails_closed(self):
        (self.archive / self.original.parent.name / "decision-1").mkdir(parents=True)
        with self.assertRaisesRegex(VirtualAccountError, "封存區已有同名"):
            self.rerun("2026-09-21T07:30:00+08:00")
        self.assertEqual(self.repository.latest()["run_id"], self.before)
        self.assertTrue(self.original.exists())


class RestoreSupersededDecisionTests(PendingDecisionFixture, unittest.TestCase):
    """早上重跑取代了晚上的決策卻沒產生新 D-Plan：帳本與決策還原成重跑前，全部只搬移不刪除。"""

    def setUp(self):
        super().setUp()
        self.pending = pending_decision(self.repository, self.decisions)
        self.account_output = self.root / "account.json"

    def restore(self, pending=None, discard=None):
        return restore_superseded_decision(
            self.repository, self.decisions, self.archive, pending or self.pending, self.account_output,
            discard_decision_run_id=discard,
        )

    def test_pending_decision_is_found_before_rerun(self):
        self.assertEqual(self.pending, {"account_run_id": self.before, "decision_run_id": "decision-1"})

    def test_restore_puts_ledger_and_decision_back(self):
        before_snapshot = self.repository.latest()["state"]["provenance"]["account_snapshot"]
        self.rerun("2026-09-21T07:30:00+08:00")
        # 本次重跑已封存的新決策也一併移走，不留在決策庫。
        fresh = self.original.parent / "decision-2"
        fresh.mkdir()
        result = self.restore(discard="decision-2")
        self.assertEqual(self.repository.latest()["run_id"], self.before)
        self.assertTrue((self.original / "decision.json").exists())
        self.assertFalse((self.archive / self.original.parent.name / "decision-1").exists())
        self.assertFalse(fresh.exists())
        self.assertTrue((self.archive / self.original.parent.name / "decision-2").is_dir())
        rolled_back = self.repository.root / "rolled_back" / "prepare-morning"
        self.assertEqual(Path(result["rolled_back_account_run"]), rolled_back)
        self.assertTrue((rolled_back / "state.json").exists())
        self.assertFalse((self.repository.runs / "prepare-morning").exists())
        self.assertEqual(json.loads(self.account_output.read_text(encoding="utf-8")), before_snapshot)
        self.assertEqual(pending_decision(self.repository, self.decisions), self.pending)

    def test_restore_refused_when_nothing_was_superseded(self):
        with self.assertRaisesRegex(VirtualAccountError, "仍在決策庫"):
            self.restore()
        self.assertEqual(self.repository.latest()["run_id"], self.before)

    def test_restore_refused_when_archived_decision_is_missing(self):
        self.rerun("2026-09-21T07:30:00+08:00")
        shutil.rmtree(self.archive / self.original.parent.name / "decision-1")
        with self.assertRaisesRegex(VirtualAccountError, "封存區找不到"):
            self.restore()
        self.assertEqual(self.repository.latest()["run_id"], "prepare-morning")

    def test_restore_refused_when_ledger_is_not_the_rerun(self):
        self.rerun("2026-09-21T07:30:00+08:00")
        with self.assertRaisesRegex(VirtualAccountError, "拒絕還原"):
            self.restore(dict(self.pending, account_run_id="prepare-other"))
        self.assertEqual(self.repository.latest()["run_id"], "prepare-morning")
        self.assertTrue((self.archive / self.original.parent.name / "decision-1").exists())

    def run_script(self, supersede=True, fail_prepare=False):
        """以 run_daily_pipeline.main 重現 10/23 02:06：資料階段取代決策後，DataPack 階段失敗。"""
        spec = importlib.util.spec_from_file_location(
            "run_daily_pipeline_rollback", Path(__file__).resolve().parents[2] / "scripts" / "run_daily_pipeline.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = self.root / "app"
        artifacts = app / "artifacts"
        (artifacts / "data").mkdir(parents=True)
        (artifacts / "archive").mkdir()
        shutil.move(str(self.root / "accounts"), str(artifacts / "ledger"))
        shutil.move(str(self.decisions), str(artifacts / "decisions"))
        self.original = next((artifacts / "decisions").glob("*/decision-1"))
        evening = {"usable": True, "decision_cutoff": "2026-09-20T18:00:00+08:00"}
        latest_snapshot = artifacts / "data" / "research_snapshot_latest.json"
        latest_snapshot.write_text(json.dumps(evening), encoding="utf-8")
        repository = VirtualAccountRepository(artifacts / "ledger", "cup-test")

        def fake_data_stage(_account_id, prepare_run_id, _history_end=None):
            if not supersede:
                return
            path = self.root / "next.json"
            path.write_text(json.dumps(dict(self.snapshot, decision_cutoff="2026-09-21T07:30:00+08:00")), encoding="utf-8")
            runner = DailyAccountRunner(repository, self.database, artifacts / "decisions",
                                        archive_root=artifacts / "archive" / "superseded_decisions")
            with mock.patch("etf_agent.virtual_account.daily.DecisionRepository.verify", return_value=self.patched):
                if fail_prepare:
                    # 決策已封存後，建立新 prepare 才失敗（例如持股缺官方價）。
                    with mock.patch.object(runner.service, "prepare_day", side_effect=VirtualAccountError("缺少持股官方價（測試）")):
                        runner.run(path, prepare_run_id, artifacts / "ledger" / "cup-test" / "account_snapshot_latest.json")
                runner.run(path, prepare_run_id, artifacts / "ledger" / "cup-test" / "account_snapshot_latest.json")
            latest_snapshot.write_text(json.dumps(dict(evening, decision_cutoff="2026-09-21T07:30:00+08:00")), encoding="utf-8")

        def missing_datapack(*_args):
            raise RuntimeError("找不到 DataPack（測試）")

        with mock.patch.object(module, "ROOT", app), \
                mock.patch.object(module, "run_preflight", lambda *_a, **_k: []), \
                mock.patch.object(module, "run_data_stage", fake_data_stage), \
                mock.patch.object(module, "capture_trading_sources", lambda *_a: {}), \
                mock.patch.object(module, "refresh_inferred_closures", lambda *_a: None), \
                mock.patch.object(module, "expected_latest_trade_date", lambda *_a: date(2026, 9, 18)), \
                mock.patch.object(module, "data_already_fetched", lambda *_a: False), \
                mock.patch.object(module, "latest_capture_before", missing_datapack), \
                mock.patch.object(module, "save_failure", lambda *_a, **_k: {"report_status": "failed", "report_path": "x"}), \
                mock.patch.object(module, "record_calibration", lambda: {}), \
                mock.patch.object(module, "notify", lambda *_a, **_k: None), \
                mock.patch("sys.argv", ["run_daily_pipeline.py", "--force", "--account-id", "cup-test"]):
            code = module.main()
        summary = json.loads(next((artifacts / "daily").glob("*/daily-*/pipeline.json")).read_text(encoding="utf-8"))
        return code, summary, repository, latest_snapshot, evening

    def test_failed_rerun_through_script_restores_evening_backup(self):
        before_index = (self.repository.root / "latest.json").read_text(encoding="utf-8")
        code, summary, repository, latest_snapshot, evening = self.run_script()
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["superseded_pending"], self.pending)
        self.assertEqual(summary["rolled_back"]["decision_run_id"], "decision-1")
        self.assertNotIn("rollback_error", summary)
        self.assertEqual((repository.root / "latest.json").read_text(encoding="utf-8"), before_index)
        self.assertTrue((self.original / "decision.json").exists())
        self.assertEqual(json.loads(latest_snapshot.read_text(encoding="utf-8")), evening)
        self.assertEqual(pending_decision(repository, self.original.parents[1]), self.pending)

    def test_decision_restored_when_ledger_step_fails_after_archiving(self):
        before_index = (self.repository.root / "latest.json").read_text(encoding="utf-8")
        code, summary, repository, _, _ = self.run_script(fail_prepare=True)
        self.assertEqual(code, 1)
        self.assertIn("缺少持股官方價", summary["error"])
        self.assertEqual(summary["rolled_back"]["decision_run_id"], "decision-1")
        self.assertIsNone(summary["rolled_back"]["rolled_back_account_run"])
        self.assertEqual((repository.root / "latest.json").read_text(encoding="utf-8"), before_index)
        self.assertTrue((self.original / "decision.json").exists())
        self.assertFalse((repository.root / "rolled_back").exists())

    def test_failed_run_without_supersede_does_not_roll_back(self):
        code, summary, repository, _, _ = self.run_script(supersede=False)
        self.assertEqual(code, 1)
        self.assertNotIn("superseded_pending", summary)
        self.assertNotIn("rolled_back", summary)
        self.assertEqual(repository.latest()["run_id"], self.before)
        self.assertTrue(self.original.exists())

    def test_rollback_latest_refuses_when_latest_moved(self):
        self.rerun("2026-09-21T07:30:00+08:00")
        with self.assertRaisesRegex(VirtualAccountError, "已不是"):
            self.repository.rollback_latest("prepare-other", self.before)
        with self.assertRaisesRegex(VirtualAccountError, "直接父狀態"):
            self.repository.rollback_latest("prepare-morning", "genesis")
        self.assertEqual(self.repository.latest()["run_id"], "prepare-morning")
        self.assertFalse((self.repository.root / "rolled_back").exists())


if __name__ == "__main__":
    unittest.main()
