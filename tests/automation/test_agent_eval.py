import copy
import json
import shutil
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.data.corporate import CorporateRecord, SourceDocument
from etf_agent.data.universe import Instrument
from etf_agent.evaluation.answers import answer_key, pool_scores, score_day
from etf_agent.evaluation import (
    EvaluationError, build_suite, compare_maps, document_recall, evaluate, price_coverage, run_suite, universe_coverage,
)
from test_daily_pipeline import TECHNICAL, TRADE, FakeRunner, PipelineWorld, team_outputs


class AgentEvaluationTests(unittest.TestCase):
    """以 fixture 每日 run 當考卷，用假 Agent 重跑，不產生用量。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "app"
        world = PipelineWorld(Path(self.temp.name), approved_status=True)
        result = world.run(FakeRunner(team_outputs()))
        self.assertEqual(result.status, "completed", result.errors)
        reference = self.root / "artifacts" / "daily" / "2026-09-20" / "daily-20260920"
        shutil.copytree(world.run_dir, reference)
        (reference / "pipeline.json").write_text(json.dumps(
            {"status": "completed", "decision_run_id": "decision-fixture", "agent_estimated_usage_usd": 1.5}), encoding="utf-8")
        for target, source in (("config/decision_policy.json", "template"), ("data/sector_classification.json", "sectors")):
            (self.root / target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(world.paths[source], self.root / target)
        (self.root / "var").mkdir()
        shutil.copy(world.paths["database"], self.root / "var" / "etf_agent.db")
        self.reference = reference

    def tearDown(self):
        self.temp.cleanup()

    def pipelines(self, outputs):
        """每次重跑各給一個假 Agent；outputs 依重跑次序取用。"""
        queue = list(outputs)

        def make(run_dir, repository):
            return DailyDecisionPipeline(self.root, run_dir, FakeRunner(queue.pop(0)), repository, log=lambda _: None)
        return make

    def test_identical_reruns_agree_with_reference_and_each_other(self):
        suite = build_suite(self.root, "fixture")
        self.assertEqual([case["case_id"] for case in suite["cases"]], ["daily-20260920"])
        manifest = run_suite(self.root, suite, self.pipelines([team_outputs(), team_outputs()]), repeats=2, log=lambda _: None)
        report = evaluate(self.root, manifest["eval_id"])
        self.assertEqual(report["overall"]["failed_runs"], 0)
        for name in ("technical", "intent", "cash_stance", "orders"):
            self.assertEqual(report["overall"]["versus_reference"][name]["mean_agreement"], 1.0, name)
            self.assertEqual(report["overall"]["stability"][name]["mean_agreement"], 1.0, name)
        # 重跑寫在評測目錄自己的決策庫，不碰正式決策庫。
        self.assertFalse((self.root / "artifacts" / "decisions").exists())
        self.assertTrue((self.root / "artifacts" / "evaluation" / "runs" / manifest["eval_id"] / "report.md").exists())
        # 沒有重大訊息時事件分析師不呼叫 Agent，其餘七個步驟各一次。
        self.assertEqual(report["cases"][0]["process"][0]["calls"], 7)

    def test_unstable_rerun_is_measured_with_opposite_views(self):
        flipped_tech = copy.deepcopy(TECHNICAL)
        flipped_tech["items"][0]["outlook"] = "negative"
        changed_trade = copy.deepcopy(TRADE)
        changed_trade["items"][0]["conviction"] = "high"
        suite = build_suite(self.root, "fixture")
        manifest = run_suite(self.root, suite, self.pipelines(
            [team_outputs(), team_outputs(technical_b0=[flipped_tech], trader_b0=[changed_trade])]), repeats=2, log=lambda _: None)
        report = evaluate(self.root, manifest["eval_id"])
        technical = report["overall"]["stability"]["technical"]
        self.assertEqual((technical["mean_agreement"], technical["opposite"]), (0.5, 1))
        self.assertEqual(report["overall"]["stability"]["intent"]["mean_agreement"], 0.5)
        self.assertEqual(report["overall"]["versus_reference"]["technical"]["min_agreement"], 0.5)

    def test_analysts_only_run_skips_trading_comparison(self):
        suite = build_suite(self.root, "fixture")
        outputs = {key: value for key, value in team_outputs().items() if key.split("_")[0] in {"technical", "fundamental", "event"}}
        manifest = run_suite(self.root, suite, self.pipelines([outputs]), repeats=1, stop_after="analysts", log=lambda _: None)
        report = evaluate(self.root, manifest["eval_id"])
        self.assertEqual(report["overall"]["versus_reference"]["technical"]["mean_agreement"], 1.0)
        self.assertIsNone(report["overall"]["versus_reference"]["intent"])

    def test_failed_rerun_is_reported_not_hidden(self):
        suite = build_suite(self.root, "fixture")
        broken = team_outputs(technical_b0=[{"items": []}, {"items": []}])
        manifest = run_suite(self.root, suite, self.pipelines([broken]), repeats=1, log=lambda _: None)
        report = evaluate(self.root, manifest["eval_id"])
        self.assertEqual(report["overall"]["failed_runs"], 1)
        process = report["cases"][0]["process"][0]
        self.assertEqual(process["retried_steps"], ["technical_b0"])
        self.assertTrue(process["validation_failures"][0]["first_validation_errors"])

    def test_changed_exam_input_or_bad_options_stop(self):
        suite = build_suite(self.root, "fixture")
        with self.assertRaises(EvaluationError):
            run_suite(self.root, suite, self.pipelines([]), repeats=0)
        with self.assertRaises(EvaluationError):
            run_suite(self.root, suite, self.pipelines([]), stop_after="trader")
        bundle = self.reference / "decision_input.json"
        bundle.write_text(bundle.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(EvaluationError, "已改變"):
            run_suite(self.root, suite, self.pipelines([team_outputs()]), repeats=1, log=lambda _: None)
        with self.assertRaisesRegex(EvaluationError, "找不到"):
            build_suite(self.root, "missing", ["daily-20990101"])

    def test_policy_newer_than_exam_cutoff_is_moved_back_and_recorded(self):
        policy_path = self.root / "config" / "decision_policy.json"
        template = json.loads(policy_path.read_text(encoding="utf-8"))
        template["available_at"] = "2030-01-01T00:00:00+08:00"
        policy_path.write_text(json.dumps(template), encoding="utf-8")
        suite = build_suite(self.root, "fixture")
        manifest = run_suite(self.root, suite, self.pipelines([team_outputs()]), repeats=1, log=lambda _: None)
        self.assertTrue(manifest["cases"][0]["policy"]["available_at_overridden"])
        self.assertEqual(manifest["cases"][0]["runs"][0]["status"], "completed")


class CompareTests(unittest.TestCase):
    def test_orders_compare_union_and_missing_side_is_skipped(self):
        result = compare_maps({"2330.TW": "buy"}, {"2317.TW": "buy"}, union=True)
        self.assertEqual((result["compared"], result["agreement"]), (2, 0.0))
        self.assertIsNone(compare_maps(None, {"2330.TW": "buy"}))


class DataCoverageTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE raw_payloads(id INTEGER, source TEXT, fetched_at TEXT, payload_json TEXT);
            CREATE TABLE source_documents(source TEXT, external_id TEXT, version INTEGER, body TEXT);
            CREATE TABLE daily_prices(source TEXT, symbol TEXT, trade_date TEXT);
        """)
        self.universe = [Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-01"),
                         Instrument("8069.TWO", "8069", "元太", "TPEX", "2026-09-01")]

    def test_missing_and_stale_documents_are_counted(self):
        self.connection.execute("INSERT INTO raw_payloads VALUES(1,'TWSE_TEST','2026-10-23T00:00:00+00:00','[]')")
        self.connection.execute("INSERT INTO source_documents VALUES('TWSE_TEST','a',1,'old')")

        class Provider:
            source, document_type = "TWSE_TEST", "monthly_revenue"

            def parse(self, payload, fetched_at):
                docs = [SourceDocument("TWSE_TEST", eid, "monthly_revenue", "t", "new", "u", fetched_at, fetched_at,
                                       fetched_at, symbol, "e") for eid, symbol in (("a", "2330.TW"), ("b", "8069.TWO"), ("c", "9999.TW"))]
                return [CorporateRecord(doc) for doc in docs], [], 3

        with patch("etf_agent.evaluation.coverage.OfficialCorporateProviderFactory.create", return_value=[Provider()]):
            rows = document_recall(self.connection, {}, ["2330.TW", "8069.TWO"])
        self.assertEqual((rows[0]["expected"], rows[0]["found"], rows[0]["current"]), (2, 1, 0))
        self.assertEqual((rows[0]["missing"], rows[0]["stale"]), (["b"], ["a"]))
        coverage = universe_coverage(rows, ["2330.TW", "8069.TWO", "1101.TW"])
        self.assertEqual(coverage[0]["missing"], ["1101.TW"])

    def test_price_coverage_by_market(self):
        self.connection.executemany("INSERT INTO daily_prices VALUES(?,?,?)", [
            ("TWSE_STOCK_DAY_ALL", "2330.TW", "2026-10-22"), ("TPEX_MAINBOARD_QUOTES", "8069.TWO", "2026-10-21"),
            ("YAHOO_FINANCE", "2330.TW", "2026-10-22")])
        rows = {row["source"]: row for row in price_coverage(self.connection, self.universe)}
        self.assertEqual(rows["TWSE_STOCK_DAY_ALL"]["recall"], 1.0)
        self.assertEqual(rows["YAHOO_FINANCE"]["missing"], ["8069.TWO"])
        self.assertEqual(rows["TPEX_MAINBOARD_QUOTES"]["latest_trade_date"], "2026-10-21")



class FakeBook:
    """兩個交易日的官方均價：2330 漲 10%、2317 跌 10%、1101 持平。"""

    def __init__(self, days=("2026-10-23", "2026-10-26"), ex_rights=()):
        self.days = list(days)
        self.ex_rights = set(ex_rights)

    def execution_date(self, cutoff):
        return self.days[0] if self.days else None

    def trading_days(self, start):
        return self.days

    def vwap(self, day):
        if day == self.days[0]:
            return {"2330.TW": Decimal("100"), "2317.TW": Decimal("100"), "1101.TW": Decimal("100")}
        return {"2330.TW": Decimal("110"), "2317.TW": Decimal("90"), "1101.TW": Decimal("100")}

    def ex_rights_symbols(self, after, through):
        return self.ex_rights


class AnswerKeyTests(unittest.TestCase):
    SYMBOLS = ["1101.TW", "2317.TW", "2330.TW"]

    def test_excess_return_against_pool_and_pending_horizons(self):
        key = answer_key(FakeBook(), "2026-10-22T20:00:00+00:00", self.SYMBOLS)
        one_day = key["horizons"]["1"]
        self.assertEqual(one_day["status"], "complete")
        self.assertEqual(one_day["excess"]["2330.TW"], Decimal("0.1"))
        self.assertEqual(one_day["excess"]["2317.TW"], Decimal("-0.1"))
        # 5 日、20 日的價格還沒出來：標 pending，不補值。
        self.assertEqual(key["horizons"]["5"]["status"], "exit_price_pending")
        self.assertEqual(answer_key(FakeBook(days=()), "2026-10-22T20:00:00+00:00", self.SYMBOLS)["entry_day"], None)
        # 除權息的股票不計，覆蓋不到八成就不給答案。
        low = answer_key(FakeBook(ex_rights={"2330.TW"}), "2026-10-22T20:00:00+00:00", self.SYMBOLS)
        self.assertEqual(low["horizons"]["1"]["status"], "coverage_too_low")

    def test_hits_count_bullish_and_bearish_calls(self):
        excess = answer_key(FakeBook(), "2026-10-22T20:00:00+00:00", self.SYMBOLS)["horizons"]["1"]["excess"]
        outputs = {"technical": {"2330.TW": "positive", "2317.TW": "positive", "1101.TW": "negative"},
                   "intent": {"2330.TW": "buy/high", "2317.TW": "exit/None", "1101.TW": "hold/None"},
                   "orders": {"2330.TW": "buy"}}
        scores = score_day(outputs, excess)
        # 1101 超額為 0，不算命中也不算錯。
        self.assertEqual((scores["technical"]["n"], scores["technical"]["hits"]), (2, 1))
        self.assertEqual((scores["intent"]["n"], scores["intent"]["hits"]), (2, 2))
        self.assertEqual((scores["orders"]["n"], scores["orders"]["hits"]), (1, 1))

    def test_hit_rate_needs_ten_distinct_days(self):
        day = {"technical": {"n": 2, "hits": 1, "up_excess": ["0.1"], "down_excess": ["-0.1"]}}
        # 同一天重跑 25 次仍只算一天。
        self.assertEqual(pool_scores([("daily-1", day)] * 25)["technical"]["status"], "insufficient_sample")
        enough = pool_scores([("daily-%d" % index, day) for index in range(10)])["technical"]
        self.assertEqual((enough["status"], enough["hit_rate"], enough["complete_days"]), ("ok", 0.5, 10))


if __name__ == "__main__":
    unittest.main()
