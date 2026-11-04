"""回測格子（run × symbol 兩種超額報酬）、重算驗證與一頁 HTML 報告。零 token，只讀已封存 Decision run 與官方均價。"""

import copy
import json
import re
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from etf_agent.calibration import CalibrationRepository, evaluate as calibration_evaluate, record_day
from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.evaluation import (
    GridError, build_grid, read_grid, render_eval_html, render_grid_html, render_replay_html, save_grid, verify_grid,
)
from etf_agent.evaluation.grid import grid_body_sha256
from etf_agent.evaluation.html_report import bar_chart_svg, line_chart_svg
from test_calibration import CONVICTION, DAILY_GROWTH, INTENT, SYMBOLS, add_price, make_database, weekdays

ROOT = Path(__file__).resolve().parents[2]
AS_OF = "2026-12-31T00:00:00+00:00"
BENCHMARK = "0050.TW"
FORBIDDEN = ("<script", "http://", "https://", "url(", "<link", "<iframe")


class GridWorld(unittest.TestCase):
    """多日多檔的封存決策與官方均價：五檔每日成長率固定，0050 每日漲 0.5%。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.decisions = self.tmp / "artifacts" / "decisions"
        self.db = self.tmp / "prices.db"
        make_database(self.db)

    def build_world(self, decision_days, total_days=45, benchmark_fetched="2026-12-31T00:00:00+00:00", with_benchmark=True):
        days = weekdays(date(2026, 6, 1), total_days)
        for index, day in enumerate(days):
            for symbol in SYMBOLS:
                add_price(self.db, symbol, day.isoformat(), DAILY_GROWTH[symbol] ** index * 100)
            if with_benchmark:
                add_price(self.db, BENCHMARK, day.isoformat(), Decimal("1.005") ** index * 200, fetched=benchmark_fetched)
        for index in range(decision_days):
            self.save_run(days[index], "decision-%sT100000Z" % days[index].strftime("%Y%m%d"), "%sT10:00:00+00:00" % days[index])
        return days

    def save_run(self, day, run_id, cutoff):
        return ImmutableRunStore(self.decisions / day.isoformat(), schema_version="1.0").save(run_id, {
            "intent": {
                "decision_id": "d" + run_id, "snapshot_id": "s", "status": "completed", "decision_cutoff": cutoff,
                "items": [{"symbol": symbol, "intent": INTENT[symbol], "conviction": CONVICTION[symbol]} for symbol in SYMBOLS],
            },
            "debate": {"packets": [
                {"role": "bull", "items": [{"symbol": symbol, "strength": "moderate"} for symbol in SYMBOLS]},
                {"role": "bear", "items": [{"symbol": symbol, "strength": "weak"} for symbol in SYMBOLS]},
            ]},
        })

    def grid(self, **options):
        options.setdefault("as_of", AS_OF)
        return build_grid(self.tmp, decision_root=self.decisions, database=self.db, **options)


class BuildGridTests(GridWorld):
    def test_one_row_per_run_and_symbol_with_both_alphas(self):
        self.build_world(10)
        body = self.grid()
        self.assertEqual((len(body["days"]), len(body["rows"])), (10, 50))
        self.assertEqual(body["provenance"]["provenance_class"], "archived")
        self.assertEqual(body["inputs"]["benchmark"], BENCHMARK)
        self.assertEqual(len(body["inputs"]["run_ids"]), 10)
        for horizon in ("1", "5", "20"):
            entry = body["horizons"][horizon]
            self.assertEqual((entry["status"], entry["complete_days"], entry["n_obs"]), ("ok", 10, 50), horizon)
            self.assertEqual(entry["benchmark_days"], {"available": 10, "unavailable": 0, "ex_rights": 0})
            for key in ("by_conviction", "by_intent", "by_bull_strength", "by_bear_strength", "by_net_strength"):
                self.assertIn(key, entry)
        five = body["horizons"]["5"]["by_conviction"]
        self.assertEqual(sorted(five), ["high", "low", "medium"])
        self.assertGreater(Decimal(five["high"]["mean_excess_vs_pool"]), Decimal(five["low"]["mean_excess_vs_pool"]))
        # 1101 每日 +2%、0050 每日 +0.5%：高信心贏過基準；1104 每日 −1%：低信心輸給基準。兩種 alpha 並列，沒有合成分數。
        self.assertGreater(Decimal(five["high"]["mean_excess_vs_benchmark"]), 0)
        self.assertLess(Decimal(five["low"]["mean_excess_vs_benchmark"]), 0)
        self.assertEqual(five["high"]["beat_pool"], {"n": 10, "wins": 10, "ties": 0, "rate": "1.000000"})
        self.assertEqual(five["high"]["beat_benchmark"]["rate"], "1.000000")
        self.assertNotIn("score", five["high"])
        # by_intent 含全部股票（含 no_trade）；by_net_strength 的鍵依整數排序。
        self.assertEqual(body["horizons"]["1"]["by_intent"]["no_trade"]["n_obs"], 10)
        self.assertEqual(list(body["horizons"]["1"]["by_net_strength"]), ["1"])
        row = body["rows"][0]
        self.assertEqual((row["symbol"], row["intent"], row["conviction"], row["net_strength"]), ("1101.TW", "buy", "high", 1))
        cell = row["horizons"]["1"]
        self.assertEqual(cell["status"], "complete")
        self.assertEqual(cell["benchmark_status"], "available")
        # 基準欄＝個股報酬 − 基準同窗口報酬（官方均價四捨五入到 0.01，所以不是剛好 0.5%）。
        benchmark = Decimal(body["days"][0]["horizons"]["1"]["benchmark_return"])
        self.assertAlmostEqual(Decimal(cell["excess_vs_benchmark"]), Decimal(cell["return"]) - benchmark, places=5)
        self.assertNotEqual(cell["excess_vs_benchmark"], cell["excess_vs_pool"])
        # 同樣輸入重算得到同樣的雜湊：格子是確定性的。
        self.assertEqual(self.grid()["content_sha256"], body["content_sha256"])

    def test_pool_alpha_matches_calibration_by_conviction(self):
        self.build_world(10)
        repository = CalibrationRepository(self.tmp / "calibration")
        for run_dir in sorted(self.decisions.glob("*/decision-*")):
            record_day(run_dir, repository)
        calibration = calibration_evaluate(repository, self.db, AS_OF)
        body = self.grid()
        for horizon in ("1", "5", "20"):
            expected = calibration["horizons"][horizon]["by_conviction"]
            actual = body["horizons"][horizon]["by_conviction"]
            self.assertEqual(sorted(expected), sorted(actual))
            for name, row in expected.items():
                self.assertEqual(actual[name]["n_obs"], row["n_obs"], (horizon, name))
                self.assertEqual(actual[name]["mean_return"], row["mean_return"], (horizon, name))
                self.assertEqual(actual[name]["mean_excess_vs_pool"], row["mean_excess_return"], (horizon, name))
            self.assertEqual(body["horizons"][horizon]["by_intent"]["buy"]["mean_excess_vs_pool"],
                             calibration["horizons"][horizon]["by_intent"]["buy"]["mean_excess_return"])

    def test_benchmark_missing_or_fetched_after_as_of_is_unavailable_not_filled(self):
        # 0050 的列都在 as_of 之後才抓到：基準 unavailable，池 alpha 照算，基準欄留空。
        self.build_world(10, benchmark_fetched="2027-01-01T00:00:00+00:00")
        body = self.grid()
        entry = body["horizons"]["1"]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["benchmark_days"], {"available": 0, "unavailable": 10, "ex_rights": 0})
        high = entry["by_conviction"]["high"]
        self.assertIsNotNone(high["mean_excess_vs_pool"])
        self.assertEqual((high["n_obs"], high["n_obs_benchmark"], high["mean_excess_vs_benchmark"]), (10, 0, None))
        self.assertEqual(high["beat_benchmark"], {"n": 0, "wins": 0, "ties": 0, "rate": None})
        cell = body["rows"][0]["horizons"]["1"]
        self.assertEqual((cell["benchmark_status"], cell["excess_vs_benchmark"]), ("unavailable", None))
        self.assertIsNotNone(cell["excess_vs_pool"])
        self.assertEqual(body["days"][0]["horizons"]["1"]["benchmark_return"], None)
        # 資料庫完全沒有 0050：同樣 unavailable。
        other = GridWorld("setUp")
        other.setUp()
        other.build_world(6, with_benchmark=False)
        self.assertEqual(other.grid()["horizons"]["1"]["benchmark_days"]["unavailable"], 6)

    def test_benchmark_ex_rights_in_window_is_flagged(self):
        days = self.build_world(6)
        connection = sqlite3.connect(self.db)
        connection.execute("UPDATE daily_prices SET ex_rights_marker = 1 WHERE symbol = ? AND trade_date = ?", (BENCHMARK, days[3].isoformat()))
        connection.commit()
        connection.close()
        body = self.grid()
        # 決策日 days[2]（成交日 days[3]）的 1 日窗口 (days[3], days[4]] 不含除權息日；days[1] 決策（成交日 days[2]）的窗口 (days[2], days[3]] 含。
        by_day = {day["entry_day"]: day["horizons"]["1"] for day in body["days"]}
        self.assertEqual(by_day[days[2].isoformat()]["benchmark_status"], "ex_rights")
        self.assertEqual(by_day[days[3].isoformat()]["benchmark_status"], "available")
        self.assertEqual(body["horizons"]["1"]["benchmark_days"]["ex_rights"], 1)
        # 5 日窗口 (T, T+5] 含 days[3] 的只有成交日 days[1]、days[2] 兩天。
        self.assertEqual(body["horizons"]["5"]["benchmark_days"]["ex_rights"], 2)

    def test_insufficient_sample_gives_counts_only(self):
        self.build_world(4)
        body = self.grid()
        for horizon in ("1", "5", "20"):
            entry = body["horizons"][horizon]
            self.assertEqual((entry["status"], entry["complete_days"]), ("insufficient_sample", 4))
            self.assertEqual(entry["n_obs"], 20)
            for key in ("by_conviction", "by_intent", "by_net_strength"):
                self.assertNotIn(key, entry)
        # 每列仍有逐格報酬，只是不做分組統計。
        self.assertEqual(body["rows"][0]["horizons"]["1"]["status"], "complete")

    def test_pending_prices_and_range_filter(self):
        days = self.build_world(8, total_days=12)
        body = self.grid()
        twenty = body["horizons"]["20"]
        self.assertEqual(twenty["complete_days"], 0)
        self.assertEqual(set(twenty["pending_days"].values()), {"exit_price_pending"})
        self.assertEqual(body["rows"][0]["horizons"]["20"], {"status": "exit_price_pending"})
        # 依成交日篩選：只留三個決策日；範圍外的記在挑選紀錄；pinned run 只含納入者。
        narrowed = self.grid(start=days[2].isoformat(), end=days[4].isoformat())
        self.assertEqual([day["entry_day"] for day in narrowed["days"]], [d.isoformat() for d in days[2:5]])
        self.assertEqual(len(narrowed["inputs"]["run_ids"]), 3)
        self.assertEqual(len(narrowed["provenance"]["selection"]["out_of_range_entry_days"]), 5)
        with self.assertRaisesRegex(GridError, "沒有可納入"):
            self.grid(start="2030-01-01", end="2030-01-02")
        with self.assertRaisesRegex(GridError, "YYYY-MM-DD"):
            self.grid(start="2026/06/01")
        with self.assertRaisesRegex(GridError, "start 不得晚於 end"):
            self.grid(start="2026-06-10", end="2026-06-01")

    def test_same_entry_day_keeps_latest_cutoff_and_pins_superseded(self):
        days = self.build_world(3)
        later = "decision-%sT110000Z" % days[0].strftime("%Y%m%d")
        self.save_run(days[0], later, "%sT11:00:00+00:00" % days[0])
        body = self.grid()
        self.assertEqual(len(body["days"]), 3)
        first = body["provenance"]["runs"][0]
        self.assertEqual((first["run_id"], first["superseded_run_ids"]), (later, ["decision-%sT100000Z" % days[0].strftime("%Y%m%d")]))
        self.assertEqual(len(body["inputs"]["run_ids"]), 4)
        self.assertEqual([row["run_id"] for row in body["rows"][:5]], [later] * 5)

    def test_naive_as_of_and_bad_inputs_are_rejected(self):
        self.build_world(2)
        with self.assertRaises(GridError):
            self.grid(as_of="2026-12-31T00:00:00")
        with self.assertRaisesRegex(GridError, "benchmark"):
            self.grid(benchmark="  ")
        with self.assertRaisesRegex(GridError, "找不到"):
            self.grid(run_ids=["decision-missing"])
        with self.assertRaisesRegex(GridError, "沒有可納入"):
            build_grid(self.tmp, decision_root=self.tmp / "empty", database=self.db, as_of=AS_OF)

    def test_tampered_or_incomplete_archive_is_skipped_or_rejected(self):
        days = self.build_world(6)
        target = next(self.decisions.glob("*/decision-%sT100000Z" % days[0].strftime("%Y%m%d")))
        intent = json.loads((target / "intent.json").read_text(encoding="utf-8"))
        intent["items"][0]["conviction"] = "low"
        (target / "intent.json").write_text(json.dumps(intent), encoding="utf-8")
        body = self.grid()
        self.assertEqual(len(body["days"]), 5)
        skipped = body["provenance"]["selection"]["skipped"]
        self.assertEqual([item["run_id"] for item in skipped], [target.name])
        with self.assertRaisesRegex(GridError, "無法納入"):
            self.grid(run_ids=[target.name])


class SaveAndVerifyTests(GridWorld):
    def test_save_read_verify_and_html_derivative(self):
        self.build_world(6)
        body = self.grid()
        grid_id, run_dir = save_grid(self.tmp, body, "grid-test")
        grid, _ = read_grid(self.tmp, grid_id)
        self.assertEqual(grid["content_sha256"], body["content_sha256"])
        self.assertEqual(grid["grid_id"], "grid-test")
        self.assertEqual(grid["evidence_grade"], "replay_reference_only")  # 對舊日期的重算，不是前瞻驗證
        html_path = run_dir.parent / "grid-test.html"
        self.assertTrue(html_path.exists())
        self.assertIn("回測報告：", html_path.read_text(encoding="utf-8"))
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["artifacts"]), ["grid"])
        result = verify_grid(self.tmp, grid_id)
        self.assertTrue(result["valid"], result["errors"])
        with self.assertRaisesRegex(GridError, "不覆寫"):
            save_grid(self.tmp, body, "grid-test")
        with self.assertRaisesRegex(GridError, "找不到"):
            read_grid(self.tmp, "grid-none")
        # 決策庫之後新增 run 不影響既有格子的重算驗證（inputs.run_ids 已釘住）。
        days = weekdays(date(2026, 6, 1), 10)
        self.save_run(days[7], "decision-%sT100000Z" % days[7].strftime("%Y%m%d"), "%sT10:00:00+00:00" % days[7])
        self.assertTrue(verify_grid(self.tmp, grid_id)["valid"])

    def test_tampered_grid_is_rejected(self):
        self.build_world(10)
        body = self.grid()
        grid_id, run_dir = save_grid(self.tmp, body, "grid-tamper")
        path = run_dir / "grid.json"
        grid = json.loads(path.read_text(encoding="utf-8"))
        grid["horizons"]["1"]["by_conviction"]["high"]["mean_excess_vs_pool"] = "0.500000"
        path.write_text(json.dumps(grid), encoding="utf-8")
        result = verify_grid(self.tmp, grid_id)
        self.assertFalse(result["valid"])
        self.assertIn("封存驗證失敗", result["errors"][0])
        # 連雜湊一起改寫、重新封存：重算比對仍拒絕。
        forged = copy.deepcopy(body)
        forged["horizons"]["1"]["by_conviction"]["high"]["mean_excess_vs_pool"] = "0.500000"
        forged["content_sha256"] = grid_body_sha256(forged)
        forged_id, _ = save_grid(self.tmp, forged, "grid-forged")
        result = verify_grid(self.tmp, forged_id)
        self.assertFalse(result["valid"])
        self.assertIn("格子的 horizons 與重算不一致", result["errors"])
        # 封存的 Decision run 被改動：重算在讀取時就停止。
        target = next(self.decisions.glob("*/decision-*"))
        intent = json.loads((target / "intent.json").read_text(encoding="utf-8"))
        intent["items"][0]["conviction"] = "low"
        (target / "intent.json").write_text(json.dumps(intent), encoding="utf-8")
        result = verify_grid(self.tmp, "grid-forged")
        self.assertFalse(result["valid"])
        self.assertTrue(any("重算失敗" in error for error in result["errors"]), result["errors"])


class HtmlReportTests(GridWorld):
    def assert_self_contained(self, page):
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        self.assertRegex(page, r'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'')
        for forbidden in FORBIDDEN:
            self.assertNotIn(forbidden, page)

    def test_grid_html_escapes_untrusted_text_and_has_no_external_resources(self):
        self.build_world(10)
        body = self.grid()
        evil = "<script>alert(1)</script>\" onload=\"x"
        body["provenance"]["runs"][0]["run_id"] = evil
        body["days"][0]["run_id"] = evil
        body["rows"][0]["run_id"] = evil
        body["rows"][0]["symbol"] = "<img src=x onerror=alert(1)>"
        body["provenance"]["selection"]["skipped"] = [{"run_id": evil, "reason": "<b>bad</b>"}]
        body["grid_id"] = "grid-x"
        page = render_grid_html(body)
        self.assert_self_contained(page)
        self.assertNotIn("<img", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;&quot; onload=&quot;x", page)
        self.assertNotIn("grid-verify", page)
        self.assertNotIn(body["content_sha256"], page)
        self.assertIn("<svg", page)
        self.assertIn("信心等級", page)

    def test_grid_html_without_sample_says_so(self):
        self.build_world(2)
        page = render_grid_html(dict(self.grid(), grid_id="grid-small"))
        self.assert_self_contained(page)
        self.assertIn("先不下結論", page)
        self.assertNotIn("<svg", page)

    def test_replay_html_handles_empty_nav_and_escapes(self):
        report = {
            "replay_id": "replay-<x>", "status": "stopped_pending_execution", "content_sha256": "abc",
            "inputs": {"run_ids": ["<run>"], "as_of": AS_OF}, "provenance": {"policy_template": {"policy_id": "p<1>", "content_sha256": "def"},
                                                                          "runs": [{"run_id": "<run>", "decision_cutoff": AS_OF, "session_day": "2026-09-21",
                                                                                    "session_day_basis": "target_session", "manifest_sha256": "m", "superseded_run_ids": []}]},
            "metrics": {"start_nav": "1", "end_nav": "1", "trading_days": 0, "total_return": None, "annualized_return": {"status": "unavailable", "reason": "沒有成交日"},
                        "max_drawdown": None, "sharpe": {"status": "unavailable", "reason": "樣本不足"}, "turnover_rate": None, "total_fees_and_taxes": "0", "fill_count": 0},
            "nav_series": [{"trade_date": "2026-09-19", "nav": "1"}],
            "days": [{"index": 1, "run_id": "<run>", "trade_date": None, "status": "pending_execution", "decision_status": "approved",
                      "policy": {"cash_stance": "neutral"}, "orders": [], "execution": None, "close": None, "pending_reason": "待 <價格>"}],
            "not_replayed_run_ids": [], "limitations": ["<b>限制</b>"],
        }
        page = render_replay_html(report)
        self.assert_self_contained(page)
        self.assertIn("無圖", page)
        self.assertIn("&lt;價格&gt;", page)
        self.assertNotIn("<價格>", page)
        self.assertNotIn("verify --replay-id", page)

    def test_eval_html_renders_without_answers(self):
        report = {"eval_id": "eval-<1>", "suite": "s", "repeats": 1, "stop_after": "full", "generated_at": "now",
                  "overall": {"versus_reference": {"technical": {"pairs": 1, "mean_agreement": 1.0, "min_agreement": 1.0, "opposite": 0}},
                              "stability": {}, "runs": 1, "failed_runs": 0, "estimated_usage_usd": 0.01},
                  "cases": [{"case_id": "<case>", "policy": {"available_at_overridden": True}, "failed_runs": [],
                             "process": [{"repeat": 1, "calls": 7, "retried_steps": [], "validation_failures": [], "estimated_usage_usd": 0.01, "agent_seconds": 0.0}]}],
                  "answers": {"required_decision_days": 5, "overall": {}, "cases": [{"case_id": "<case>", "entry_day": None, "horizons": {"1": {"status": "entry_price_pending"}}}]}}
        page = render_eval_html(report)
        self.assert_self_contained(page)
        self.assertIn("&lt;case&gt;", page)
        self.assertIn("還沒有任何考卷的股價到齊", page)
        self.assertNotIn("report --eval-id", page)

    def test_charts_fail_closed_without_points(self):
        self.assertIn("無圖", line_chart_svg([], "NAV"))
        self.assertIn("無圖", line_chart_svg([("a", Decimal(1))], "NAV"))
        self.assertIn("<polyline", line_chart_svg([("a", Decimal(1)), ("b", Decimal(1))], "NAV"))
        self.assertIn("無圖", bar_chart_svg([("high", None, None)], "x"))
        chart = bar_chart_svg([("<h>", Decimal("0.01"), None)], "x<y>")
        self.assertIn("&lt;h&gt;", chart)
        self.assertNotIn("<h>", chart)
        for forbidden in FORBIDDEN:
            self.assertNotIn(forbidden, chart)


class IsolationTests(unittest.TestCase):
    def test_decision_chain_and_reflection_never_import_grid_or_html(self):
        pattern = re.compile(r"evaluation\.(grid|html_report)|from etf_agent\.evaluation import .*\b(build_grid|save_grid|render_\w+_html)\b")
        roots = [ROOT / "src" / "etf_agent" / name for name in ("decision", "automation", "strategy_reflection")]
        paths = [path for root in roots for path in root.rglob("*.py")] + [ROOT / "scripts" / "run_daily_pipeline.py"]
        self.assertGreater(len(paths), 5)
        for path in paths:
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), str(path))


if __name__ == "__main__":
    unittest.main()
