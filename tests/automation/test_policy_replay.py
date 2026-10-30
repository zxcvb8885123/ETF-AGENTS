"""便宜版回測：多日滾動回放的結轉、換 Policy、前視價格、缺日與報告竄改。"""

import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

from etf_agent.decision import DecisionRepository, canonical_sha256, decision_bundle_sha256
from etf_agent.evaluation import (
    ReplayError, build_replay, compare_replays, nav_metrics, read_replay, render_comparison, render_replay_report,
    save_replay, select_runs, verify_replay,
)
from etf_agent.evaluation.replay import DECISION_DECIMAL_PRECISION, _trade_date_for, replay_body_sha256
from etf_agent.evaluation.replay_market import ReplayMarket
from test_team_chain import finalize, team_world, tradable_bundle
from test_virtual_account_daily import create_prices


ROOT = Path(__file__).resolve().parents[2]
AS_OF = "2026-09-30T00:00:00+00:00"
# (run_id, cutoff, Snapshot 最後交易日, 2330 決策用價, 2317 決策用價)
DAYS = (
    ("decision-20260920T005500Z", "2026-09-20T00:55:00+00:00", "2026-09-19", "164", "112"),
    ("decision-20260922T005500Z", "2026-09-22T00:55:00+00:00", "2026-09-21", "165", "113"),
    ("decision-20260923T005500Z", "2026-09-23T00:55:00+00:00", "2026-09-22", "166", "110"),
)
# 官方成交：(symbol, 日期, 收盤, 成交股數, 來源, 抓取時間)；成交金額省略時均價等於收盤價。
PRICES = [
    ("2330.TW", "2026-09-21", "165", 1000, "TWSE_STOCK_DAY", "2026-09-21T10:00:00+00:00"),
    ("2317.TW", "2026-09-21", "113", 1000, "TWSE_STOCK_DAY", "2026-09-21T10:00:00+00:00"),
    ("2330.TW", "2026-09-22", "166", 1000, "TWSE_STOCK_DAY", "2026-09-22T10:00:00+00:00"),
    ("2317.TW", "2026-09-22", "110", 1000, "TWSE_STOCK_DAY", "2026-09-22T10:00:00+00:00"),
    ("2330.TW", "2026-09-23", "160", 1000, "TWSE_STOCK_DAY", "2026-09-23T10:00:00+00:00"),
    ("2317.TW", "2026-09-23", "115", 1000, "TWSE_STOCK_DAY", "2026-09-23T10:00:00+00:00"),
]


def day_bundle(cutoff, trade_date, price_2330, price_2317):
    """把 fixture bundle 移到指定日期：cutoff、最後交易日、決策用價與價格序列都延伸到該日。"""
    bundle = tradable_bundle()
    snapshot = bundle["snapshot"]
    snapshot["decision_cutoff"] = bundle["decision_cutoff"] = cutoff
    snapshot["latest_trade_date"] = trade_date
    prices = {"2330.TW": price_2330, "2317.TW": price_2317}
    for row in snapshot["latest_prices"]:
        price = prices[row["symbol"]]
        row.update(trade_date=trade_date, close_price=price, analysis_close_price=price, execution_close_price=price)
    target = date.fromisoformat(trade_date)
    for series in bundle["price_series"]:
        bars = series["bars"]
        price = prices[series["symbol"]]
        last = date.fromisoformat(bars[-1]["trade_date"])
        while last < target:
            last += timedelta(days=1)
            close = Decimal(price)
            bars.append({"trade_date": last.isoformat(), "available_at": last.isoformat() + "T16:00:00+00:00",
                         "open": str(close - Decimal("0.5")), "high": str(close + 1), "low": str(close - 1),
                         "close": str(close), "volume": 1200000})
        bars[-1]["close"] = price
        series["series_sha256"] = canonical_sha256(bars)
    account = bundle["account_snapshot"]
    account["available_at"] = account["valuation_at"] = cutoff
    account["nav"] = str(Decimal(account["cash"]) + 1000 * Decimal(price_2330))
    bundle["snapshot_sha256"] = canonical_sha256(snapshot)
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def archive_day(decisions, run_id, cutoff, trade_date, price_2330, price_2317):
    """用分析團隊 fixture 產生完整封存 Decision run（Agent 輸出固定：2317 buy／medium、2330 hold、現金 neutral）。

    每日決策鏈以 Decimal 預設精度封存；測試行程因匯入 evaluation（→ calibration）全域精度被改成 40，這裡鎖回 28 才與真實封存一致。
    """
    with localcontext() as context:
        context.prec = DECISION_DECIMAL_PRECISION
        bundle, momentum, debate, trade, policy, team, _, _ = team_world(day_bundle(cutoff, trade_date, price_2330, price_2317))
        artifacts, result = finalize(bundle, momentum, debate, trade, policy, team)
    assert result["status"] == "approved", result["errors"]
    DecisionRepository(decisions).save(run_id, {
        "decision_input": bundle, "momentum": momentum, "debate": debate, "intent": trade, "policy": policy,
        "decision": result, "team_inputs": team, **artifacts,
    })
    return run_id


class PolicyReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "tradeagent").mkdir()
        (self.root / "tradeagent" / "SKILL.md").write_text("fixture", encoding="utf-8")
        self.decisions = self.root / "artifacts" / "decisions"
        self.database = self.root / "market.db"
        create_prices(self.database, PRICES)
        template = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        template.update(policy_id="replay-A", available_at="2026-09-19T00:00:00+00:00")
        self.policy_a = self.root / "policy_a.json"
        self.policy_a.write_text(json.dumps(template), encoding="utf-8")
        other = copy.deepcopy(template)
        other.update(policy_id="replay-B")
        other["position_sizing"]["cash_buffer_by_stance"] = {"aggressive": "0.3", "neutral": "0.5", "defensive": "0.6"}
        self.policy_b = self.root / "policy_b.json"
        self.policy_b.write_text(json.dumps(other), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, days=DAYS):
        return [archive_day(self.decisions, *day) for day in days]

    def replay(self, policy, **options):
        options.setdefault("as_of", AS_OF)
        return build_replay(self.root, policy, decision_root=self.decisions, database=self.database, **options)

    def test_multi_day_replay_carries_simulated_positions_and_cash(self):
        self.archive()
        body, artifacts = self.replay(self.policy_a, start="2026-09-21", end="2026-09-23")
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["inputs"]["run_ids"], [day[0] for day in DAYS])
        first = body["days"][0]
        # 第一天：起點是封存帳戶（現金 500000、2330 一張），2317 依信心等級與 ATR 配到 2 張，以 9/21 官方均價 113 成交。
        self.assertEqual(first["account_before"]["symbols"], ["2330.TW"])
        self.assertEqual([(o["symbol"], o["side"], o["shares"]) for o in first["orders"]], [("2317.TW", "buy", 2000)])
        self.assertEqual(first["execution"]["fills"][0]["execution_price"], "113.00")
        self.assertEqual(first["close"]["nav"], "664677")
        # 第二天讀的是回放自己的帳本（已持有 2317），不是封存 run 當時的帳戶。
        second = body["days"][1]
        self.assertEqual(second["account_before"]["symbols"], ["2317.TW", "2330.TW"])
        self.assertEqual(second["status"], "executed")
        self.assertEqual([row["trade_date"] for row in body["nav_series"]], ["2026-09-19", "2026-09-21", "2026-09-22", "2026-09-23"])
        metrics = body["metrics"]
        self.assertEqual(metrics["trading_days"], 3)
        self.assertEqual(metrics["sharpe"]["status"], "unavailable")
        self.assertFalse(metrics["annualized_return"]["reliable"])
        self.assertGreater(Decimal(metrics["total_fees_and_taxes"]), 0)
        self.assertGreaterEqual(metrics["fill_count"], 1)
        self.assertEqual(body["provenance"]["policy_template"]["policy_id"], "replay-A")
        self.assertEqual(body["provenance"]["tradeagent"]["files"], 1)
        self.assertEqual(len(body["provenance"]["runs"]), 3)
        # 封存、重讀、Markdown 與重算驗證。
        replay_id, run_dir = save_replay(self.root, body, artifacts, "replay-test")
        report, _ = read_replay(self.root, replay_id)
        self.assertEqual(report["content_sha256"], body["content_sha256"])
        self.assertIn("## 績效", render_replay_report(report))
        self.assertTrue((run_dir.parent / "replay-test.md").exists())
        self.assertTrue((run_dir / "d01_proposal.json").exists())
        result = verify_replay(self.root, replay_id)
        self.assertTrue(result["valid"], result["errors"])
        with self.assertRaisesRegex(ReplayError, "不覆寫"):
            save_replay(self.root, body, artifacts, "replay-test")

    def test_same_session_day_keeps_latest_cutoff_and_records_superseded(self):
        self.archive(DAYS[:1])
        archive_day(self.decisions, "decision-20260919T200000Z", "2026-09-19T20:00:00+00:00", "2026-09-19", "164", "112")
        market = ReplayMarket(self.database, AS_OF)
        chosen = select_runs(self.decisions, market, "2026-09-21", "2026-09-21")
        self.assertEqual([item["run_id"] for item in chosen], ["decision-20260920T005500Z"])
        self.assertEqual(chosen[0]["superseded_run_ids"], ["decision-20260919T200000Z"])
        self.assertEqual(chosen[0]["session_day_basis"], "first_official_trading_day")
        with self.assertRaisesRegex(ReplayError, "找不到"):
            select_runs(self.decisions, market, run_ids=["decision-missing"])
        with self.assertRaisesRegex(ReplayError, "沒有可回放"):
            select_runs(self.decisions, market, "2030-01-01", "2030-01-02")

    def test_changing_policy_changes_result_but_both_rebuild(self):
        self.archive()
        body_a, artifacts_a = self.replay(self.policy_a, start="2026-09-21", end="2026-09-23")
        body_b, artifacts_b = self.replay(self.policy_b, start="2026-09-21", end="2026-09-23")
        self.assertNotEqual(body_a["metrics"]["end_nav"], body_b["metrics"]["end_nav"])
        self.assertEqual(body_b["days"][0]["orders"][0]["shares"], 1000)
        self.assertNotEqual(body_a["provenance"]["policy_template"]["content_sha256"], body_b["provenance"]["policy_template"]["content_sha256"])
        id_a, _ = save_replay(self.root, body_a, artifacts_a, "replay-a")
        id_b, _ = save_replay(self.root, body_b, artifacts_b, "replay-b")
        self.assertTrue(verify_replay(self.root, id_a)["valid"])
        self.assertTrue(verify_replay(self.root, id_b)["valid"])
        comparison = compare_replays(read_replay(self.root, id_a)[0], read_replay(self.root, id_b)[0])
        self.assertEqual(comparison["order_differences"][0]["changed"], ["2317.TW buy 2000→1000"])
        self.assertFalse(comparison["statistically_meaningful"])
        self.assertEqual(len(comparison["nav"]), 4)
        self.assertIn("張數不同", render_comparison(comparison))
        # 重跑同樣輸入得到同樣的雜湊：回放是確定性的。
        again, _ = self.replay(self.policy_a, start="2026-09-21", end="2026-09-23")
        self.assertEqual(again["content_sha256"], body_a["content_sha256"])

    def test_lookahead_price_is_rejected_and_unavailable_price_is_pending(self):
        self.archive(DAYS[:1])
        # as_of 早於 9/21 官方價的抓取時間：不能成交，標為待官方價格，不補假值。
        body, _ = self.replay(self.policy_a, run_ids=[DAYS[0][0]], as_of="2026-09-21T09:00:00+00:00")
        self.assertEqual(body["status"], "stopped_pending_execution")
        self.assertEqual(body["days"][0]["status"], "pending_execution")
        self.assertEqual(body["days"][0]["execution"], None)
        self.assertEqual(body["metrics"]["trading_days"], 0)
        self.assertEqual(len(body["nav_series"]), 1)
        # 成交日的官方列在該日收盤前就「抓到」：前視價格，直接拒絕。
        early = self.root / "early.db"
        create_prices(early, [(symbol, day, close, volume, source, "2026-09-21T02:00:00+00:00")
                              for symbol, day, close, volume, source, _ in PRICES if day == "2026-09-21"])
        with self.assertRaisesRegex(ReplayError, "前視價格"):
            build_replay(self.root, self.policy_a, decision_root=self.decisions, database=early, as_of=AS_OF, run_ids=[DAYS[0][0]])

    def test_missing_decision_day_stops_unless_gaps_are_allowed(self):
        self.archive((DAYS[0], DAYS[2]))
        with self.assertRaisesRegex(ReplayError, "缺日：2026-09-22"):
            self.replay(self.policy_a, start="2026-09-21", end="2026-09-23")
        body, _ = self.replay(self.policy_a, start="2026-09-21", end="2026-09-23", allow_gaps=True)
        self.assertEqual([day["status"] for day in body["days"]], ["executed", "gap", "executed"])
        self.assertEqual(body["days"][1]["trade_date"], "2026-09-22")
        self.assertEqual([row["basis"] for row in body["nav_series"]][2], "gap_official_close")
        # 有目標時段的 run：成交日沒有官方列但之後的日子有，是缺價，不順延到下一個有資料的日子。
        missing = self.root / "missing.db"
        create_prices(missing, [row for row in PRICES if row[1] != "2026-09-21"])
        session_bundle = {"decision_cutoff": DAYS[0][1], "trading_status_bundle": {"target_session": {"start": "2026-09-21T09:00:00+08:00"}}}
        with self.assertRaisesRegex(ReplayError, "缺價或缺日"):
            _trade_date_for(ReplayMarket(missing, AS_OF), session_bundle, DAYS[0][0])
        self.assertIsNone(_trade_date_for(ReplayMarket(missing, "2026-09-21T00:00:00+00:00"), session_bundle, DAYS[0][0]))
        # 沒有目標時段的 fixture run 只能取資料庫 cutoff 後第一個官方交易日，報告記錄 basis。
        body, _ = build_replay(self.root, self.policy_a, decision_root=self.decisions, database=missing, as_of=AS_OF, run_ids=[DAYS[0][0]])
        self.assertEqual((body["days"][0]["trade_date"], body["provenance"]["runs"][0]["session_day_basis"]),
                         ("2026-09-22", "first_official_trading_day"))

    def test_tampered_report_or_archive_is_rejected(self):
        self.archive(DAYS[:1])
        body, artifacts = self.replay(self.policy_a, run_ids=[DAYS[0][0]])
        replay_id, run_dir = save_replay(self.root, body, artifacts, "replay-tamper")
        report_path = run_dir / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["metrics"]["total_return"] = "0.50000000"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        result = verify_replay(self.root, replay_id)
        self.assertFalse(result["valid"])
        self.assertIn("封存驗證失敗", result["errors"][0])
        # 連雜湊一起改寫、重新封存：重算比對仍拒絕。
        forged = copy.deepcopy(body)
        forged["metrics"]["total_return"] = "0.50000000"
        forged["content_sha256"] = replay_body_sha256(forged)
        forged_id, _ = save_replay(self.root, forged, artifacts, "replay-forged")
        result = verify_replay(self.root, forged_id)
        self.assertFalse(result["valid"])
        self.assertIn("回放報告的 metrics 與重算不一致", result["errors"])
        # 封存 Decision run 被改動：回放在讀取時就停止。
        intent_path = next(self.decisions.glob("*/%s/intent.json" % DAYS[0][0]))
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        intent["items"][0]["conviction"] = "high"
        intent_path.write_text(json.dumps(intent), encoding="utf-8")
        with self.assertRaisesRegex(ReplayError, "封存驗證失敗"):
            self.replay(self.policy_a, run_ids=[DAYS[0][0]])

    def test_initial_cash_start_and_newer_template_is_moved_back(self):
        self.archive(DAYS[:1])
        template = json.loads(self.policy_a.read_text(encoding="utf-8"))
        template["available_at"] = "2030-01-01T00:00:00+00:00"
        newer = self.root / "newer.json"
        newer.write_text(json.dumps(template), encoding="utf-8")
        body, _ = self.replay(newer, run_ids=[DAYS[0][0]], initial_cash="1000000")
        self.assertEqual(body["start_account"], {"basis": "initial_cash", "cash": "1000000", "positions": []})
        self.assertEqual(body["days"][0]["account_before"]["symbols"], [])
        self.assertTrue(body["days"][0]["policy"]["available_at_overridden"])
        self.assertEqual(body["nav_series"][0]["nav"], "1000000")
        with self.assertRaisesRegex(ReplayError, "initial_cash"):
            self.replay(self.policy_a, run_ids=[DAYS[0][0]], initial_cash="0")


class ReplayRankingBindingTests(unittest.TestCase):
    """樣板啟用 selection_ranking 時，回放要用封存 run 的入選名次，否則候選策略與實際配置的入選順序不同。"""

    def setUp(self):
        from etf_agent.decision import program_ranking, ranking_envelope
        from etf_agent.evaluation.replay import bind_replay_policy

        cutoff, trade_date, p2330, p2317 = DAYS[0][1:]
        with localcontext() as context:
            context.prec = DECISION_DECIMAL_PRECISION
            self.bundle, _, self.debate, self.trade, self.policy, self.team, _, _ = team_world(
                day_bundle(cutoff, trade_date, p2330, p2317))
        self.ranking = program_ranking(ranking_envelope(self.bundle, self.trade, "r-1"), self.trade)
        self.template = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        self.template.update(policy_id="replay-A", available_at="2026-09-19T00:00:00+00:00")
        self.bind = bind_replay_policy

    def bound(self, template, ranking):
        with localcontext() as context:
            context.prec = DECISION_DECIMAL_PRECISION
            return self.bind(template, self.bundle, self.policy, self.trade, self.debate,
                             self.team["cash_stance"], [], ranking)

    def test_ranking_is_bound_when_template_enables_it(self):
        policy, notes = self.bound(self.template, self.ranking)
        self.assertTrue(notes["selection_ranking_applied"])
        self.assertEqual(policy["position_sizing"]["selection_ranking_id"], "r-1")
        self.assertIn("rank_by_symbol", policy["position_sizing"])

    def test_old_archived_run_without_ranking_keeps_old_ordering(self):
        policy, notes = self.bound(self.template, None)
        self.assertFalse(notes["selection_ranking_applied"])
        self.assertNotIn("rank_by_symbol", policy["position_sizing"])

    def test_candidate_template_without_selection_ranking_ignores_it(self):
        template = copy.deepcopy(self.template)
        template["position_sizing"].pop("selection_ranking")
        policy, notes = self.bound(template, self.ranking)
        self.assertFalse(notes["selection_ranking_applied"])
        self.assertNotIn("rank_by_symbol", policy["position_sizing"])


class NavMetricsTests(unittest.TestCase):
    def test_metrics_from_nav_series(self):
        navs = [Decimal(1000)]
        for index in range(1, 25):
            navs.append(navs[-1] * (Decimal("1.01") if index % 3 else Decimal("0.98")))
        series = [{"nav": str(nav)} for nav in navs]
        metrics = nav_metrics(series, Decimal(5000), Decimal(70), Decimal(30), 7)
        self.assertEqual(metrics["trading_days"], 24)
        self.assertEqual(metrics["sharpe"]["status"], "available")
        self.assertEqual(metrics["annualized_return"]["status"], "available")
        self.assertFalse(metrics["annualized_return"]["reliable"])
        peak, worst = navs[0], Decimal(0)
        for nav in navs:
            peak, worst = max(peak, nav), max(worst, (peak - nav) / peak)
        self.assertEqual(metrics["max_drawdown"], str(worst.quantize(Decimal("0.00000001"))))
        self.assertGreater(Decimal(metrics["max_drawdown"]), Decimal("0.02"))
        self.assertEqual(metrics["total_fees_and_taxes"], "100")
        self.assertEqual(metrics["fill_count"], 7)
        self.assertEqual(Decimal(metrics["total_return"]), (navs[-1] / navs[0] - 1).quantize(Decimal("0.00000001")))

    def test_single_point_and_flat_series(self):
        single = nav_metrics([{"nav": "1000"}], Decimal(0), Decimal(0), Decimal(0), 0)
        self.assertEqual((single["trading_days"], single["total_return"], single["annualized_return"]["status"]), (0, None, "unavailable"))
        flat = nav_metrics([{"nav": "1000"}] * 25, Decimal(0), Decimal(0), Decimal(0), 0)
        self.assertEqual(flat["sharpe"]["status"], "unavailable")
        self.assertEqual(flat["total_return"], "0E-8")
        with self.assertRaisesRegex(ReplayError, "大於 0"):
            nav_metrics([{"nav": "0"}, {"nav": "1"}], Decimal(0), Decimal(0), Decimal(0), 0)


if __name__ == "__main__":
    unittest.main()
