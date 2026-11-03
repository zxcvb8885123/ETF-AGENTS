import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from etf_agent.competition import (
    DPlanError, DPlanValidator, FallbackDPlanRepository, FallbackDPlanService, build_fallback_plan,
    fallback_preconditions, load_fallback_run,
)
from etf_agent.competition.fallback import DEFAULT_FALLBACK_DIRECTORY, FALLBACK_MODEL_PROVIDER, FALLBACK_MODEL_VERSION
from etf_agent.competition.service import DPlanExportService
from etf_agent.core import canonical_sha256

TAIPEI = timezone(timedelta(hours=8))
TEAM, TRADE_DATE = "TEAM_042", "2026-10-23"
NOW = datetime(2026, 10, 23, 8, 30, tzinfo=TAIPEI)


def tickers():
    """150 檔測試交易池：含 2330，其餘為 1000 起連號。"""
    return ["2330"] + ["%04d" % (1000 + index) for index in range(149)]


def world(positions=28, cash_ratio=Decimal("0.10"), weights=None):
    """帳戶、Snapshot 與已對帳 DataPack 三者一致的 fixture；nav_tradable 固定 10 億，權重是其比例。

    ``weights`` 指定個別代號的目標權重（預設 2330 為 20%），其餘持股平分剩下的股票權重；股數向下取整張，
    所以實際現金比略高於 ``cash_ratio``。"""
    universe = tickers()
    held = universe[:positions]
    price = {ticker: Decimal("1000") if ticker == "2330" else Decimal("100") for ticker in universe}
    nav_tradable = Decimal("1000000000")
    fixed = {"2330": Decimal("0.20")} if weights is None else {k: Decimal(str(v)) for k, v in weights.items()}
    remaining = [ticker for ticker in held if ticker not in fixed]
    spread = (1 - cash_ratio - sum(fixed.get(t, Decimal("0")) for t in held)) / len(remaining)
    shares = {}
    for ticker in held:
        weight = fixed.get(ticker, spread)
        shares[ticker] = int((weight * nav_tradable / price[ticker] / 1000).to_integral_value(rounding="ROUND_FLOOR")) * 1000
    value = sum(Decimal(shares[t]) * price[t] for t in held)
    cash = nav_tradable - value
    account = {
        "account_id": "acct", "cash": str(cash), "nav": str(nav_tradable),
        "positions": [{"symbol": t + ".TW", "shares": shares[t], "average_cost": str(price[t])} for t in held],
    }
    latest_prices, evidence = [], []
    for ticker in universe:
        symbol = ticker + ".TW"
        latest_prices.append({
            "symbol": symbol, "code": ticker, "trade_date": "2026-10-22",
            "execution_close_price": str(price[ticker]), "analysis_close_price": str(price[ticker]),
            "source_evidence_id": "price:v:%s" % ticker, "execution_source_evidence_id": "price:o:%s" % ticker,
            "execution_price_source": "TWSE_STOCK_DAY",
        })
        evidence.append({"evidence_id": "price:v:%s" % ticker, "source": "YAHOO_FINANCE", "authority": "vendor",
                         "url": "https://finance.example.test/%s" % ticker, "content_as_of": "2026-10-22T13:30:00+08:00",
                         "fetched_at": "2026-10-22T16:00:00+08:00"})
        evidence.append({"evidence_id": "price:o:%s" % ticker, "source": "TWSE_STOCK_DAY", "authority": "twse",
                         "url": "https://www.twse.example.test/%s" % ticker, "content_as_of": "2026-10-22T13:30:00+08:00"})
    snapshot = {
        "snapshot_id": "snap-1", "decision_cutoff": "2026-10-22T23:30:00+00:00", "usable": True,
        "universe_size": 150, "latest_prices": latest_prices, "source_evidence": evidence,
    }
    datapack = {
        "status": "matched", "team_id": TEAM, "trade_date": TRADE_DATE, "datapack_sha256": "a" * 64,
        "as_of_date": "2026-10-22", "generated_at": "2026-10-22T19:05:00+08:00",
        "nav_tradable": str(nav_tradable), "cash": {"ledger": str(cash), "datapack": str(cash)},
        "close_prices": {t + ".TW": str(price[t]) for t in held},
        "sellable_shares": {t: shares[t] for t in held},
    }
    return account, snapshot, datapack


def profile():
    return {
        "schema_version": "1.0", "team_id": TEAM, "strategy_status": "approved",
        "strategy": {"etf_name": "主動測試ETF", "investment_theme": "測試", "investment_philosophy": "測試用投資理念。" * 12,
                     "approved_at": "2026-09-19T20:00:00+08:00"},
        "agent": {"model_provider": "anthropic", "model_version": "claude-opus"},
    }


def daily_run(datapack, **extra):
    run = {"run_id": "daily-20261023-070000", "started_at": "2026-10-23T07:00:00+08:00", "status": "completed",
           "decision_status": "rejected", "planned_decision_run_id": "decision-20261022T230000Z",
           "target_session": {"start": "2026-10-23T09:00:00+08:00", "end": "2026-10-23T13:30:00+08:00"}, "datapack": datapack}
    run.update(extra)
    return run


class FallbackPreconditionTests(unittest.TestCase):
    def setUp(self):
        self.account, self.snapshot, self.datapack = world()

    def check(self, account=None, snapshot=None, datapack=None, now=NOW):
        return fallback_preconditions(
            self.account if account is None else account, self.snapshot if snapshot is None else snapshot,
            self.datapack if datapack is None else datapack, team_id=TEAM, trade_date=TRADE_DATE, now=now)

    def test_consistent_world_passes(self):
        result = self.check()
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(result["metrics"]["position_count"], 28)
        self.assertAlmostEqual(float(result["metrics"]["cash_ratio"]), 0.10, delta=0.001)
        self.assertEqual(result["warnings"], [])

    def test_position_count_outside_20_30_fails(self):
        for count in (19, 31):
            account, snapshot, datapack = world(positions=count)
            result = self.check(account, snapshot, datapack)
            self.assertFalse(result["ok"])
            self.assertTrue(any("檔" in item and "20–30" in item for item in result["failures"]), result["failures"])

    def test_cash_ratio_at_or_above_25_percent_fails(self):
        account, snapshot, datapack = world(cash_ratio=Decimal("0.25"))
        result = self.check(account, snapshot, datapack)
        self.assertFalse(result["ok"])
        self.assertTrue(any("現金比" in item for item in result["failures"]))

    def test_single_stock_cap_uses_25_for_2330_and_10_for_others(self):
        ok_account, ok_snapshot, ok_datapack = world(weights={"2330": "0.249", "1000": "0.099"})
        result = self.check(ok_account, ok_snapshot, ok_datapack)
        self.assertTrue(result["ok"], result["failures"])
        account, snapshot, datapack = world(weights={"2330": "0.251"})
        result = self.check(account, snapshot, datapack)
        self.assertTrue(any("2330" in item and "25%" in item for item in result["failures"]), result["failures"])
        account, snapshot, datapack = world(weights={"2330": "0.05", "1000": "0.101"})
        result = self.check(account, snapshot, datapack)
        self.assertTrue(any("1000" in item and "10%" in item for item in result["failures"]), result["failures"])

    def test_requires_matched_datapack_with_same_team_and_date(self):
        for datapack in ({"status": "skipped"}, {"status": "mismatch"}, {}):
            result = self.check(datapack=datapack)
            self.assertFalse(result["ok"])
            self.assertTrue(any("matched" in item for item in result["failures"]))
        result = self.check(datapack=dict(self.datapack, trade_date="2026-10-24"))
        self.assertTrue(any("交易日" in item for item in result["failures"]))

    def test_ledger_positions_must_equal_datapack_holdings(self):
        datapack = json.loads(json.dumps(self.datapack))
        datapack["sellable_shares"]["1000"] += 1000
        result = self.check(datapack=datapack)
        self.assertTrue(any("結算持股不同" in item for item in result["failures"]))

    def test_holding_outside_universe_or_missing_official_close_fails(self):
        account = json.loads(json.dumps(self.account))
        account["positions"][-1]["symbol"] = "9999.TW"
        datapack = json.loads(json.dumps(self.datapack))
        datapack["close_prices"]["9999.TW"] = datapack["close_prices"].pop("1026.TW")
        datapack["sellable_shares"]["9999"] = datapack["sellable_shares"].pop("1026")
        result = self.check(account=account, datapack=datapack)
        self.assertTrue(any("150 檔" in item for item in result["failures"]), result["failures"])
        datapack = json.loads(json.dumps(self.datapack))
        del datapack["close_prices"]["1000.TW"]
        result = self.check(datapack=datapack)
        self.assertTrue(any("1000 缺 DataPack 官方收盤價" in item for item in result["failures"]))

    def test_deadline_and_cutoff_are_enforced(self):
        late = datetime(2026, 10, 23, 8, 55, tzinfo=TAIPEI)
        result = self.check(now=late)
        self.assertTrue(any("交件期限" in item for item in result["failures"]))
        snapshot = dict(self.snapshot, decision_cutoff="2026-10-23T01:00:00+00:00")  # 台北 09:00，晚於 08:55
        result = self.check(snapshot=snapshot)
        self.assertTrue(any("交件期限" in item for item in result["failures"]))
        with self.assertRaisesRegex(DPlanError, "時區"):
            self.check(now=NOW.replace(tzinfo=None))

    def test_price_difference_is_a_warning_not_a_failure(self):
        snapshot = json.loads(json.dumps(self.snapshot))
        snapshot["latest_prices"][1]["execution_close_price"] = "101"
        snapshot["latest_prices"][2]["execution_close_price"] = None
        result = self.check(snapshot=snapshot)
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(len(result["warnings"]), 2)
        self.assertIn("1000", result["warnings"][0])


class FallbackPlanBuilderTests(unittest.TestCase):
    def setUp(self):
        self.account, self.snapshot, self.datapack = world()

    def build(self, **overrides):
        kwargs = dict(team_id=TEAM, trade_date=TRADE_DATE, account=self.account, snapshot=self.snapshot, datapack=self.datapack,
                      reason="decision_rejected", detail="風控拒絕：\n測試", started_at="2026-10-23T07:00:00+08:00",
                      completed_at=NOW.isoformat(), code_version="git:abc123")
        kwargs.update(overrides)
        return build_fallback_plan(**kwargs)

    def test_plan_holds_everything_without_orders_and_passes_validator(self):
        plan = self.build()
        self.assertEqual(DPlanValidator().validate(plan), [])
        self.assertEqual(plan["decisions"], [])
        self.assertEqual(plan["orders"], [])
        self.assertEqual([row["ticker"] for row in plan["no_trade_decisions"]], sorted(tickers()[:28]))
        self.assertEqual(len(plan["inferences"]), 29)
        self.assertEqual(len(plan["observations"]), 29)
        for row in plan["no_trade_decisions"]:
            self.assertEqual(row["reason_refs"][0], "I1")
        self.assertEqual(plan["market_view"]["posture"]["net_exposure_intent"], "hold")
        low, high = plan["market_view"]["posture"]["target_cash_pct_range"]
        self.assertLess(low, 0.10)
        self.assertGreater(high, 0.10)
        self.assertLessEqual(high, 0.25)
        # 官方價來源（twse）優先於日線來源被引用。
        self.assertEqual(sorted(source["authority"] for source in plan["sources"]), ["twse"])
        self.assertEqual(plan["agent_metadata"]["model_provider"], FALLBACK_MODEL_PROVIDER)
        self.assertEqual(plan["agent_metadata"]["model_version"], FALLBACK_MODEL_VERSION)
        # 錯誤訊息進文字欄位前去掉換行。
        self.assertNotIn("\n", plan["inferences"][0]["logic"])
        self.assertIn("風控拒絕： 測試", plan["inferences"][0]["logic"])

    def test_invalid_reason_or_time_order_is_rejected(self):
        with self.assertRaisesRegex(DPlanError, "原因代碼"):
            self.build(reason="agent_tired")
        with self.assertRaisesRegex(DPlanError, "run_completed_at"):
            self.build(completed_at="2026-10-23T06:00:00+08:00")
        with self.assertRaisesRegex(DPlanError, "code_version"):
            self.build(code_version="bad version!")

    def test_holding_without_price_evidence_is_rejected(self):
        snapshot = json.loads(json.dumps(self.snapshot))
        snapshot["source_evidence"] = [row for row in snapshot["source_evidence"] if "1000" not in row["evidence_id"]]
        with self.assertRaisesRegex(DPlanError, "1000.TW 缺少可引用的封存價格來源"):
            self.build(snapshot=snapshot)

    def test_plan_is_deterministic(self):
        self.assertEqual(canonical_sha256(self.build()), canonical_sha256(self.build()))


class FallbackServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.account, self.snapshot, self.datapack = world()
        (self.root / "config").mkdir(parents=True)
        self.profile_path = self.root / "config" / "competition_profile.json"
        self.profile_path.write_text(json.dumps(profile()), encoding="utf-8")
        self.account_path = self.root / "account.json"
        self.snapshot_path = self.root / "snapshot.json"
        self.account_path.write_text(json.dumps(self.account), encoding="utf-8")
        self.snapshot_path.write_text(json.dumps(self.snapshot), encoding="utf-8")
        self.main = self.root / "artifacts" / "competition_d_plan" / ("D-Plan_%s_%s.json" % (TEAM, TRADE_DATE))

    def tearDown(self):
        self.directory.cleanup()

    def generate(self, run=None, reason="decision_rejected", now=NOW, **kwargs):
        with patch.object(DPlanExportService, "code_version", return_value="git:test"):
            return FallbackDPlanService(self.root).generate(
                daily_run=run or daily_run(self.datapack), account_path=self.account_path, snapshot_path=self.snapshot_path,
                profile_path=self.profile_path, account_id="acct", reason=reason, detail="測試原因", now=now, **kwargs)

    def test_generates_sealed_run_version_and_main_file(self):
        result = self.generate()
        self.assertEqual(result["status"], "fallback")
        self.assertTrue(result["fallback"])
        self.assertFalse(result["uploaded"])
        self.assertTrue(result["main_replaced"])
        self.assertEqual(Path(result["output"]), self.main)
        plan = json.loads(self.main.read_text(encoding="utf-8"))
        self.assertEqual(DPlanValidator().validate(plan), [])
        self.assertEqual(plan["orders"], [])
        version = Path(result["version_output"])
        self.assertTrue(version.name.endswith("_fallback.json"))
        self.assertEqual(version.parent, self.main.parent / "versions" / TRADE_DATE)
        # 走不可變封存：manifest 驗證通過，decision 明標 fallback 且不是 Agent 產製。
        run_dir = Path(result["run_dir"])
        self.assertEqual(run_dir.parent, self.root / DEFAULT_FALLBACK_DIRECTORY / TRADE_DATE)
        FallbackDPlanRepository(self.root / DEFAULT_FALLBACK_DIRECTORY).verify(result["run_id"])
        run = load_fallback_run(self.root, result["run_id"])
        self.assertEqual(sorted(run), ["account_snapshot", "decision", "dplan", "preconditions"])
        self.assertTrue(run["decision"]["fallback"])
        self.assertFalse(run["decision"]["agent_generated"])
        self.assertEqual(run["decision"]["status"], "no_trade")
        self.assertEqual(run["decision"]["reason"], "decision_rejected")
        self.assertEqual(run["decision"]["dplan_sha256"], canonical_sha256(plan))
        self.assertEqual(run["decision"]["account_snapshot_sha256"], canonical_sha256(self.account))
        self.assertEqual(run["dplan"], plan)
        # 不會放進 Agent 決策庫。
        self.assertFalse((self.root / "artifacts" / "decisions").exists())
        # 竄改封存內容會被 verify 拒絕。
        (run_dir / "dplan.json").write_text(json.dumps(dict(plan, orders=[{"x": 1}])), encoding="utf-8")
        with self.assertRaises(DPlanError):
            load_fallback_run(self.root, result["run_id"])

    def test_existing_real_plan_is_kept_and_fallback_only_versioned(self):
        self.main.parent.mkdir(parents=True)
        real = {"schema_version": "4.3", "doc_type": "D-Plan", "team_id": TEAM, "trade_date": TRADE_DATE, "orders": [{"real": True}]}
        self.main.write_text(json.dumps(real), encoding="utf-8")
        result = self.generate(reason="pipeline_failed")
        self.assertEqual(result["status"], "fallback")
        self.assertFalse(result["main_replaced"])
        self.assertIn("正式決策", result["main_kept_reason"])
        self.assertEqual(Path(result["output"]), Path(result["version_output"]))
        self.assertEqual(json.loads(self.main.read_text(encoding="utf-8")), real)
        # 主檔本身是備援時，新的備援可以取代它。
        self.main.write_text(Path(result["version_output"]).read_text(encoding="utf-8"), encoding="utf-8")
        later = self.generate(reason="dplan_blocked", now=NOW + timedelta(minutes=1))
        self.assertTrue(later["main_replaced"])
        self.assertEqual(json.loads(self.main.read_text(encoding="utf-8")), json.loads(Path(later["version_output"]).read_text(encoding="utf-8")))

    def test_not_generated_when_preconditions_fail_or_rolled_back(self):
        result = self.generate(run=daily_run({"status": "skipped"}))
        self.assertEqual(result["status"], "not_generated")
        self.assertIn("備援前提不滿足", result["error"])
        self.assertTrue(result["failures"])
        self.assertFalse(self.main.exists())
        self.assertFalse((self.root / DEFAULT_FALLBACK_DIRECTORY).exists())
        result = self.generate(run=daily_run(self.datapack, rolled_back={"decision_run_id": "decision-old"}))
        self.assertEqual(result["status"], "not_generated")
        self.assertIn("還原", result["error"])
        result = self.generate(now=datetime(2026, 10, 23, 9, 0, tzinfo=TAIPEI))
        self.assertEqual(result["status"], "not_generated")
        self.assertIn("交件期限", result["error"])
        result = self.generate(reason="unknown")
        self.assertEqual(result["status"], "not_generated")
        self.assertIn("原因代碼", result["error"])
        self.assertFalse(self.main.exists())

    def test_unapproved_profile_or_wrong_account_is_refused(self):
        self.profile_path.write_text(json.dumps(dict(profile(), strategy_status="pending")), encoding="utf-8")
        result = self.generate()
        self.assertEqual(result["status"], "not_generated")
        self.assertIn("策略尚未確認", result["error"])
        self.profile_path.write_text(json.dumps(profile()), encoding="utf-8")
        self.account_path.write_text(json.dumps(dict(self.account, account_id="other")), encoding="utf-8")
        result = self.generate()
        self.assertEqual(result["status"], "not_generated")
        self.assertIn("account_id", result["error"])

    def test_missing_target_session_is_not_generated(self):
        run = daily_run(self.datapack)
        del run["target_session"]
        result = self.generate(run=run)
        self.assertEqual(result["status"], "not_generated")
        self.assertIn("target_session", result["error"])


if __name__ == "__main__":
    unittest.main()
