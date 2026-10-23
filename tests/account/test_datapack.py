import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from etf_agent.competition import (
    DataPackError, check_dplan_against_datapack, find_datapack, load_datapack, load_universe,
    open_competition_account, reconcile,
)
from etf_agent.competition.datapack import parse_datapack
from etf_agent.virtual_account import VirtualAccountRepository, active_account_id


READ_AT = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
UNIVERSE = {"2330": "2330.TW", "2317": "2317.TW", "3293": "3293.TWO", "6669": "6669.TW"}


def settled():
    """規格 §3.1 縮小版：已結算、有部位。"""
    return {
        "schema_version": "3.0", "doc_type": "DataPack", "team_id": "TEAM_11067",
        "trade_date": "2026-10-28", "as_of_date": "2026-10-27", "generated_at": "2026-10-27T18:58:18+08:00",
        "missing": [], "prev_day_orders": {"outcome": "executed", "void_reasons": [], "excluded_codes": []},
        "account": {"cash": 100000000.0, "cash_ratio_tradable": 0.1, "nav_tradable": 1000000000.0,
                    "aum": 1000000000.0, "receivable_cash": 0.0, "holdings_count": 2},
        "holdings": [
            {"code": "2330", "shares": 73000, "receivable_shares": 0, "close_price": 2585.0, "tradable_weight": 0.188744},
            {"code": "3293", "shares": 43000, "receivable_shares": 0, "close_price": 787.0, "tradable_weight": 0.0338},
        ],
        "compliance": {"violations": 0, "notices": 0, "incidents": []},
    }


def first_day():
    """規格 §3.2(a)：第一個交易日，全現金、0 檔。"""
    return {
        "schema_version": "3.0", "doc_type": "DataPack", "team_id": "TEAM_11067",
        "trade_date": "2026-10-27", "as_of_date": None, "generated_at": "2026-10-27T05:12:08+08:00",
        "missing": [], "prev_day_orders": {"outcome": "none", "void_reasons": [], "excluded_codes": []},
        "account": {"cash": 1000000000.0, "cash_ratio_tradable": 1.0, "nav_tradable": 1000000000.0,
                    "aum": 1000000000.0, "receivable_cash": 0.0, "holdings_count": 0},
        "holdings": [], "compliance": {"violations": 0, "notices": 0, "incidents": []},
    }


def ledger(cash="100000000", **shares):
    positions = [{"symbol": symbol, "shares": count} for symbol, count in (shares or {"2330.TW": 73000, "3293.TWO": 43000}).items()]
    return {"cash": cash, "positions": positions}


def loaded(pack):
    return {"pack": parse_datapack(pack, READ_AT), "raw_sha256": "sha", "path": "datapack.json", "read_at": READ_AT.isoformat()}


class DataPackParseTests(unittest.TestCase):
    def setUp(self):
        self.pack = settled()

    def test_spec_examples_are_accepted(self):
        self.assertEqual(parse_datapack(self.pack, READ_AT)["trade_date"], "2026-10-28")
        self.assertEqual(parse_datapack(first_day(), READ_AT)["holdings"], [])
        pending = dict(first_day(), trade_date="2026-10-29", missing=["prev_settlement"], account=None, holdings=None,
                       prev_day_orders={"outcome": "unknown", "void_reasons": [], "excluded_codes": []})
        self.assertIsNone(parse_datapack(pending, READ_AT)["holdings"])

    def test_rejects_unknown_fields_bad_types_and_bad_semantics(self):
        cases = []
        extra = copy.deepcopy(self.pack); extra["note"] = "x"; cases.append(extra)
        flag = copy.deepcopy(self.pack); flag["holdings"][0]["shares"] = True; cases.append(flag)
        frac = copy.deepcopy(self.pack); frac["holdings"][0]["shares"] = 73000.5; cases.append(frac)
        count = copy.deepcopy(self.pack); count["account"]["holdings_count"] = 3; cases.append(count)
        dup = copy.deepcopy(self.pack); dup["holdings"].append(dict(dup["holdings"][0])); cases.append(dup)
        naive = copy.deepcopy(self.pack); naive["generated_at"] = "2026-10-27T18:58:18"; cases.append(naive)
        future = copy.deepcopy(self.pack); future["generated_at"] = "2026-10-28T18:58:18+08:00"; cases.append(future)
        order = copy.deepcopy(self.pack); order["as_of_date"] = "2026-10-28"; cases.append(order)
        void = copy.deepcopy(self.pack); void["prev_day_orders"]["outcome"] = "voided"; cases.append(void)
        null_as_empty = dict(first_day(), missing=["prev_settlement"]); cases.append(null_as_empty)
        ratio = copy.deepcopy(self.pack); ratio["missing"] = ["prev_close"]; cases.append(ratio)
        for case in cases:
            with self.assertRaises(DataPackError):
                parse_datapack(case, READ_AT)

    def test_receivable_only_row_is_not_a_position(self):
        self.pack["holdings"].append({"code": "6669", "shares": 0, "receivable_shares": 9913, "close_price": 2610.0, "tradable_weight": None})
        result = reconcile(loaded(self.pack), ledger(), team_id="TEAM_11067", trade_date="2026-10-28", universe=UNIVERSE)
        self.assertEqual(result["status"], "matched")
        self.assertNotIn("6669", result["sellable_shares"])
        self.assertTrue(any("應收配股" in w for w in result["warnings"]))

    def test_load_and_find_datapack_by_team_and_trade_date(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            older = settled()
            newer = dict(settled(), generated_at="2026-10-27T19:30:00+08:00")
            (folder / "a.json").write_text(json.dumps(older), encoding="utf-8")
            (folder / "b.json").write_text(json.dumps(newer), encoding="utf-8")
            (folder / "broken.json").write_text("{", encoding="utf-8")
            found = find_datapack(folder, "TEAM_11067", "2026-10-28", READ_AT)
            self.assertEqual(found["pack"]["generated_at"], "2026-10-27T19:30:00+08:00")
            self.assertEqual(len(found["raw_sha256"]), 64)
            with self.assertRaisesRegex(DataPackError, "找不到"):
                find_datapack(folder, "TEAM_11067", "2026-10-29", READ_AT)
            with self.assertRaises(DataPackError):
                load_datapack(folder / "broken.json", READ_AT)

    def test_universe_maps_listed_and_otc_suffixes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "u.csv"
            path.write_text("symbol,name,market,source_date\n2330,台積電,TWSE,2026-09-14\n3293,鈊象,TPEX,2026-09-14\n", encoding="utf-8")
            self.assertEqual(load_universe(path), {"2330": "2330.TW", "3293": "3293.TWO"})
            path.write_text("symbol,name,market,source_date\n2330,台積電,XX,2026-09-14\n", encoding="utf-8")
            with self.assertRaises(DataPackError):
                load_universe(path)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.pack = settled()

    def run_reconcile(self, account, pack=None, **kwargs):
        options = dict(team_id="TEAM_11067", trade_date="2026-10-28", universe=UNIVERSE)
        options.update(kwargs)
        return reconcile(loaded(pack or self.pack), account, **options)

    def test_matching_ledger_passes(self):
        result = self.run_reconcile(ledger())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["sellable_shares"], {"2330": 73000, "3293": 43000})
        self.assertEqual(Decimal(result["cash"]["difference"]), 0)
        # D-Plan 換算股數沿用主辦方的分母與前日收盤價。
        self.assertEqual(Decimal(result["nav_tradable"]), Decimal("1000000000"))
        self.assertEqual({k: Decimal(v) for k, v in result["close_prices"].items()},
                         {"2330.TW": Decimal("2585"), "3293.TWO": Decimal("787")})

    def test_share_and_cash_differences_stop(self):
        result = self.run_reconcile(ledger(**{"2330.TW": 74000, "3293.TWO": 43000, "2317.TW": 1000}))
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual([d["symbol"] for d in result["share_differences"]], ["2317.TW", "2330.TW"])
        tolerance = Decimal("1000000000") * Decimal("0.0005")
        self.assertEqual(self.run_reconcile(ledger(cash=str(Decimal("100000000") + tolerance)))["status"], "matched")
        self.assertEqual(self.run_reconcile(ledger(cash=str(Decimal("100000000") + tolerance + 1)))["status"], "mismatch")

    def test_wrong_team_date_or_pending_settlement_is_not_ready(self):
        self.assertEqual(self.run_reconcile(ledger(), team_id="TEAM_X")["status"], "not_ready")
        self.assertEqual(self.run_reconcile(ledger(), trade_date="2026-10-29")["status"], "not_ready")
        pending = dict(first_day(), trade_date="2026-10-28", missing=["prev_settlement"], account=None, holdings=None,
                       prev_day_orders={"outcome": "unknown", "void_reasons": [], "excluded_codes": []})
        result = self.run_reconcile(ledger(), pack=pending)
        self.assertEqual(result["status"], "not_ready")
        self.assertIn("prev_settlement", result["problems"][0])

    def test_unmapped_code_stops_and_voided_day_explains_cause(self):
        self.pack["holdings"].append({"code": "9999", "shares": 1000, "receivable_shares": 0, "close_price": 10.0, "tradable_weight": 0.00001})
        self.pack["account"]["holdings_count"] = 3
        self.pack["prev_day_orders"] = {"outcome": "voided", "void_reasons": ["oversell"], "excluded_codes": []}
        self.pack["compliance"] = {"violations": 1, "notices": 0, "incidents": [
            {"date": "2026-10-27", "check_code": "oversell", "status": "fail", "message": "賣出 5,000 股超過庫存 3,000 股"}]}
        result = self.run_reconcile(ledger())
        self.assertEqual(result["status"], "mismatch")
        self.assertTrue(any("9999" in p for p in result["problems"]))
        self.assertTrue(any("賣超持股" in w for w in result["warnings"]))
        self.assertTrue(any("違規 1 天" in w for w in result["warnings"]))

    def test_violations_warn_but_do_not_block(self):
        self.pack["compliance"] = {"violations": 1, "notices": 0, "incidents": [
            {"date": "2026-10-27", "check_code": "cash_ratio", "status": "fail", "message": "現金比超過上限"}]}
        result = self.run_reconcile(ledger())
        self.assertEqual(result["status"], "matched")
        self.assertTrue(result["warnings"])

    def test_first_day_all_cash(self):
        pack = first_day()
        result = self.run_reconcile({"cash": "1000000000", "positions": []}, pack=pack, trade_date="2026-10-27")
        self.assertEqual(result["status"], "matched")
        result = self.run_reconcile(ledger(), pack=pack, trade_date="2026-10-27")
        self.assertEqual(result["status"], "mismatch")  # 測試帳戶帶持股進比賽會被擋下。


class DPlanAgainstDataPackTests(unittest.TestCase):
    def setUp(self):
        self.pack = parse_datapack(settled(), READ_AT)
        self.plan = {"team_id": "TEAM_11067", "trade_date": "2026-10-28",
                     "decisions": [{"ticker": "2330", "action": "TRIM"}], "no_trade_decisions": [{"ticker": "3293"}],
                     "orders": [{"ticker": "2330", "side": "SELL", "shares": 3000}]}

    def test_valid_plan_passes(self):
        self.assertEqual(check_dplan_against_datapack(self.plan, self.pack), [])

    def test_oversell_missing_coverage_and_identity(self):
        plan = copy.deepcopy(self.plan)
        plan["orders"].append({"ticker": "2330", "side": "SELL", "shares": 71000})
        plan["orders"].append({"ticker": "2317", "side": "SELL", "shares": 1000})
        plan["no_trade_decisions"] = []
        plan["trade_date"] = "2026-10-29"
        errors = check_dplan_against_datapack(plan, self.pack)
        self.assertTrue(any("2330 賣出 74000" in e for e in errors))
        self.assertTrue(any("2317 賣出" in e for e in errors))
        self.assertTrue(any("3293" in e for e in errors))
        self.assertTrue(any("trade_date" in e for e in errors))


class OpenCompetitionAccountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "config").mkdir()
        (self.root / "config/competition_rules.json").write_text(json.dumps({"initial_capital_twd": 1000000000}), encoding="utf-8")
        (self.root / "config/competition_profile.json").write_text(json.dumps({"team_id": "TEAM_11067"}), encoding="utf-8")
        self.datapack = self.root / "first.json"
        self.datapack.write_text(json.dumps(first_day()), encoding="utf-8")
        self.now = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)

    def test_opens_new_account_and_switches_active_without_touching_old(self):
        old = self.root / "artifacts/ledger/ai-cup-2026"
        old.mkdir(parents=True)
        (old / "keep.txt").write_text("test", encoding="utf-8")
        result = open_competition_account(self.root, "ai-cup-2026-final", datapack_path=self.datapack, now=self.now)
        self.assertTrue(result["datapack_checked"])
        self.assertEqual(active_account_id(self.root), "ai-cup-2026-final")
        self.assertEqual((old / "keep.txt").read_text(encoding="utf-8"), "test")
        state = VirtualAccountRepository(self.root / "artifacts/ledger", "ai-cup-2026-final").latest()["state"]
        self.assertEqual(state["settled_cash"], "1000000000")
        self.assertEqual(state["positions"], [])

    def test_opening_without_datapack_uses_rules_capital(self):
        result = open_competition_account(self.root, "final", now=self.now)
        self.assertFalse(result["datapack_checked"])
        self.assertEqual(result["initial_capital"], "1000000000")

    def test_rejects_existing_or_same_account_and_non_first_day_pack(self):
        with self.assertRaisesRegex(DataPackError, "不得與目前使用中"):
            open_competition_account(self.root, "ai-cup-2026", now=self.now)
        (self.root / "artifacts/ledger/used").mkdir(parents=True)
        with self.assertRaisesRegex(DataPackError, "已存在"):
            open_competition_account(self.root, "used", now=self.now)
        for change in ({"holdings_count": 1}, {"cash": 999999999.0}):
            pack = first_day()
            pack["account"].update(change)
            self.datapack.write_text(json.dumps(pack), encoding="utf-8")
            with self.assertRaises(DataPackError):
                open_competition_account(self.root, "another", datapack_path=self.datapack, now=self.now)
            self.assertFalse((self.root / "artifacts/ledger/another").exists())
        pack = first_day()
        pack["team_id"] = "TEAM_X"
        self.datapack.write_text(json.dumps(pack), encoding="utf-8")
        with self.assertRaisesRegex(DataPackError, "隊伍"):
            open_competition_account(self.root, "another", datapack_path=self.datapack, now=self.now)


class ReconcileAccountCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "config").mkdir()
        (self.root / "data").mkdir()
        (self.root / "artifacts/ledger/acct").mkdir(parents=True)
        (self.root / "config/competition_profile.json").write_text(json.dumps({"team_id": "TEAM_11067"}), encoding="utf-8")
        (self.root / "data/official_universe.csv").write_text(
            "symbol,name,market,source_date\n2330,台積電,TWSE,2026-09-14\n3293,鈊象,TPEX,2026-09-14\n", encoding="utf-8")
        pack = dict(settled(), generated_at="2026-10-01T18:58:18+08:00")
        self.datapack = self.root / "pack.json"
        self.datapack.write_text(json.dumps(pack), encoding="utf-8")

    def write_account(self, account):
        (self.root / "artifacts/ledger/acct/account_snapshot_latest.json").write_text(json.dumps(account), encoding="utf-8")

    def test_uses_datapack_trade_date_and_reports_mismatch(self):
        from etf_agent.competition import reconcile_account
        self.write_account(ledger())
        result = reconcile_account(self.root, "acct", self.datapack)
        self.assertEqual((result["status"], result["trade_date"], result["account_id"]), ("matched", "2026-10-28", "acct"))
        self.write_account(ledger(**{"2330.TW": 72000, "3293.TWO": 43000}))
        self.assertEqual(reconcile_account(self.root, "acct", self.datapack)["status"], "mismatch")
        with self.assertRaises(DataPackError):
            reconcile_account(self.root, "missing", self.datapack)


class RebaseAccountTests(unittest.TestCase):
    """對帳不一致時依 DataPack 重建帳本，再用同一份 Snapshot 重建決策前快照。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "config").mkdir()
        rules = self.root / "config/competition_rules.json"
        rules.write_text(json.dumps({"initial_capital_twd": 1000000000}), encoding="utf-8")
        self.repository = VirtualAccountRepository(self.root / "artifacts/ledger", "acct")
        from etf_agent.virtual_account import VirtualAccountService
        self.service = VirtualAccountService(self.repository)
        self.service.initialize(rules, "acct", "2026-10-26T00:00:00+00:00")
        self.snapshot = {
            "snapshot_id": "snapshot-20261027", "decision_cutoff": "2026-10-27T12:00:00+00:00",
            "usable": True, "latest_trade_date": "2026-10-27",
            "latest_prices": [{"symbol": "2330.TW", "close_price": "2585"}, {"symbol": "3293.TWO", "close_price": "787"}],
        }
        self.snapshot_path = self.root / "snapshot.json"
        self.snapshot_path.write_text(json.dumps(self.snapshot), encoding="utf-8")
        self.service.prepare_day(self.snapshot_path, "prepare-1")
        self.account_output = self.root / "artifacts/ledger/acct/account_snapshot_latest.json"
        self.pack = dict(settled(), generated_at="2026-10-27T18:58:18+08:00")  # 10:58Z，早於 cutoff 12:00Z

    def rebase(self, pack=None):
        from etf_agent.competition import rebase_account_to_datapack
        return rebase_account_to_datapack(self.root, "acct", loaded(pack or self.pack), self.snapshot_path,
                                          "prepare-1-datapack", self.account_output, UNIVERSE)

    def test_rebase_matches_datapack_and_keeps_old_state(self):
        result = self.rebase()
        account = json.loads(self.account_output.read_text(encoding="utf-8"))
        self.assertEqual({p["symbol"]: p["shares"] for p in account["positions"]}, {"2330.TW": 73000, "3293.TWO": 43000})
        self.assertEqual(Decimal(account["cash"]), Decimal("100000000"))
        check = reconcile(loaded(self.pack), account, team_id="TEAM_11067", trade_date="2026-10-28", universe=UNIVERSE)
        self.assertEqual(check["status"], "matched")
        latest = self.repository.latest()
        self.assertEqual(latest["run_id"], "prepare-1-datapack")
        rebase_state = json.loads((self.repository.runs / result["rebase_run_id"] / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(rebase_state["provenance"]["type"], "datapack_rebase")
        self.assertEqual(rebase_state["provenance"]["parent_run_id"], "prepare-1")
        self.assertTrue(self.repository.verify("prepare-1").exists())  # 舊狀態保留。

    def test_rebase_rejects_late_datapack_wrong_day_or_missing_price(self):
        late = dict(self.pack, generated_at="2026-10-27T20:00:00+08:00")  # 等於 cutoff 也不行
        with self.assertRaisesRegex(DataPackError, "不早於"):
            self.rebase(late)
        wrong_day = dict(self.pack, as_of_date="2026-10-24")
        with self.assertRaisesRegex(DataPackError, "結算日"):
            self.rebase(wrong_day)
        no_price = copy.deepcopy(self.pack)
        no_price["holdings"][0]["close_price"] = None
        with self.assertRaises(Exception):
            self.rebase(no_price)
        self.assertEqual(self.repository.latest()["run_id"], "prepare-1")  # 失敗時帳本不動。

    def test_rebase_requires_fresh_prepare_without_pending_decision(self):
        from unittest.mock import patch
        from etf_agent.virtual_account import VirtualAccountError, VirtualAccountService
        with patch("etf_agent.virtual_account.daily.find_decision_run", return_value="decision-1"):
            with self.assertRaisesRegex(DataPackError, "待成交"):
                self.rebase()
        genesis_only = VirtualAccountService(VirtualAccountRepository(self.root / "artifacts/ledger", "fresh"))
        genesis_only.initialize(self.root / "config/competition_rules.json", "fresh", "2026-10-26T00:00:00+00:00")
        with self.assertRaisesRegex(VirtualAccountError, "prepare"):
            genesis_only.rebase({}, "1000000000", "2026-10-27T10:00:00+00:00", {}, "rebase-1")
