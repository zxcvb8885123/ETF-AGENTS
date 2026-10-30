"""每週反思：診斷包、週報、六天保護與週六套用的確定性與 fail-closed 測試。"""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from etf_agent.strategy_reflection import (
    ReflectionError, apply_pending, build_brief, build_diagnostics, observe_only, render_weekly_report, run_auto,
    save_reflection, service,
)
from test_policy_replay import AS_OF, DAYS, PRICES, archive_day
from test_strategy_reflection import Fixture, NOW, report, replay_body
from test_virtual_account_daily import create_prices

FRIDAY = NOW  # 2026-11-20 12:30 UTC＝週五 20:30 台北
SATURDAY = datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc)  # 週六 10:00 台北
SATURDAY_LATE = datetime(2026, 11, 21, 13, 0, tzinfo=timezone.utc)  # 週六 21:00 台北：在 20:50～09:00 封鎖窗內


def passing_replayer(path, replay_id):
    return replay_body("101") if replay_id.endswith("candidate") else replay_body()


from etf_agent.strategy_reflection import diagnostics as diagnostics_module


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.decisions = self.root / "decisions"
        self.database = self.root / "market.db"
        create_prices(self.database, PRICES)
        with sqlite3.connect(self.database) as connection:  # 診斷讀除權息標記；共用的測試價格表沒有這一欄
            connection.execute("ALTER TABLE daily_prices ADD COLUMN ex_rights_marker INTEGER")
        for day in DAYS:
            archive_day(self.decisions, *day)
        self.diagnostics = build_diagnostics(self.decisions, self.database, AS_OF)

    def test_small_samples_are_flagged_and_never_given_averages(self):
        for horizons in self.diagnostics["analyst"].values():
            for entry in horizons.values():
                self.assertEqual(entry["status"], "insufficient_sample", entry)
                self.assertNotIn("hit_rate", entry)
        for entry in self.diagnostics["sell"].values():
            self.assertEqual(entry["status"], "insufficient_sample")
            self.assertNotIn("regret_rate", entry)

    def test_turnover_is_a_plain_fact_without_threshold(self):
        turnover = self.diagnostics["turnover"]
        self.assertEqual(turnover["status"], "ok")
        self.assertGreater(turnover["days"], 0)

    def test_rejected_decision_without_portfolio_does_not_break_turnover_or_thesis(self):
        from etf_agent.strategy_reflection.diagnostics import _turnover
        from etf_agent.strategy_reflection.thesis import build_thesis_tracking
        rejected = {"decision": {"status": "rejected", "portfolio": None, "orders": []}, "intent": {"items": []}, "team_inputs": {}}
        original = diagnostics_module._latest_per_entry_day
        diagnostics_module._latest_per_entry_day = lambda runs, book: ({"2026-11-02": rejected}, [])
        try:
            self.assertEqual(_turnover([rejected], None)["status"], "ok")
        finally:
            diagnostics_module._latest_per_entry_day = original
        self.assertEqual(build_thesis_tracking({"2026-11-02": rejected}, None), [])

    def test_thesis_numbers_are_in_diagnostics_but_the_reasons_are_not(self):
        thesis = self.diagnostics["thesis"]
        self.assertEqual(thesis["status"], "ok")
        self.assertGreater(thesis["holdings"], 0)
        self.assertNotIn("rationale", json.dumps(thesis, ensure_ascii=False))
        self.assertEqual(self.diagnostics["week"]["status"], "ok")

    def test_review_is_unavailable_before_team_inputs_2_4(self):
        self.assertEqual(self.diagnostics["review"]["status"], "unavailable")

    def test_only_numbers_and_enums_no_free_text(self):
        """診斷包不收自由文字：葉節點只能是數值、列舉字串或固定說明，且沒有 findings／claims／rationale 等欄位。"""
        banned = {"findings", "claims", "rationale", "text", "summary", "title"}

        def walk(value):
            if isinstance(value, dict):
                self.assertFalse(banned & set(value), value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            else:
                self.assertIsInstance(value, (int, float, str, type(None)))

        walk(self.diagnostics)

    def test_decisions_after_as_of_are_excluded(self):
        before = build_diagnostics(self.decisions, self.database, "2026-01-01T00:00:00+08:00")
        self.assertEqual(before["run_count"], 0)
        self.assertEqual(before["turnover"]["status"], "unavailable")

    def test_no_decision_runs_gives_unavailable_not_an_error(self):
        empty = build_diagnostics(self.root / "none", self.database, AS_OF)
        self.assertEqual(empty["turnover"]["status"], "unavailable")
        self.assertEqual(empty["analyst"], {})


class BriefDiagnosticsTests(Fixture):
    def test_only_ok_metrics_become_evidence(self):
        diagnostics = {
            "decision_days": 3,
            "analyst": {"technical": {"h5": {"status": "insufficient_sample", "complete_days": 3, "n": 90, "required_days": 10}}},
            "turnover": {"status": "ok", "days": 3, "mean_turnover_rate": "0.1"},
        }
        brief = build_brief(report(), self.template, NOW.isoformat(), diagnostics)
        self.assertIn("diag.turnover.mean_turnover_rate", brief["evidence"])
        self.assertEqual([key for key in brief["evidence"] if key.startswith("diag.analyst")], [])
        self.assertEqual(brief["diagnostics"], diagnostics)

    def test_brief_without_diagnostics_is_unchanged(self):
        self.assertNotIn("diagnostics", self.brief)


class WeeklyRunTests(Fixture):
    class Runner:
        def __init__(self, output=None):
            self.calls, self.output = 0, output

        def run(self, task, run_dir):
            from etf_agent.automation.agent_runner import AgentCall

            self.calls += 1
            return AgentCall(task.name, 0, self.output, 0.5)

    def closed(self):
        return build_brief(report(days=2, status="insufficient"), self.template, NOW.isoformat(),
                           {"decision_days": 2, "turnover": {"status": "ok", "days": 2, "mean_turnover_rate": "0.4",
                                                             "mean_fee_drag": "0.001", "sell_orders": 0}})

    def run_week(self, now, force=False, brief=None, runner=None, **options):
        with mock.patch.object(service, "make_brief", return_value=brief or self.closed()):
            return run_auto(self.tmp, self.tmp / "db", self.tmp / "decisions", runner or self.Runner(), now, force=force, **options)

    def test_closed_gate_still_writes_a_weekly_report_without_calling_the_agent(self):
        runner = self.Runner()
        result = self.run_week(FRIDAY, runner=runner)
        self.assertEqual((result["status"], runner.calls), ("observe_only", 0))
        text = Path(result["report"]).read_text(encoding="utf-8")
        self.assertIn("策略反思週報", text)
        self.assertIn("只觀察、不提案", text)
        self.assertEqual(Path(result["report"]).name, "weekly-2026-11-20.md")

    def test_second_run_within_six_days_is_skipped_unless_forced(self):
        self.run_week(FRIDAY)
        again = self.run_week(FRIDAY + timedelta(days=2))
        self.assertEqual(again["status"], "skipped")
        self.assertEqual(self.run_week(FRIDAY + timedelta(days=2), force=True)["status"], "observe_only")
        # 上一份是「強制補跑」的那一份（週日），所以隔滿 6 天後才會再跑。
        self.assertEqual(self.run_week(FRIDAY + timedelta(days=5))["status"], "skipped")
        self.assertEqual(self.run_week(FRIDAY + timedelta(days=9))["status"], "observe_only")

    def test_open_gate_no_apply_seals_and_reports_without_touching_config(self):
        runner = self.Runner(self.output())
        before = self.live()
        result = self.run_week(FRIDAY, brief=self.brief, runner=runner, apply=False)
        self.assertEqual(result["status"], "proposed")
        self.assertNotIn("apply", result)
        self.assertEqual(self.live(), before)
        self.assertIn("本次只封存", Path(result["report"]).read_text(encoding="utf-8"))

    def test_report_marks_unavailable_and_insufficient_items(self):
        brief = self.closed()
        text = render_weekly_report(NOW, brief, observe_only(brief, NOW))
        self.assertIn("決策日 2／5", text)
        self.assertIn("尚無 team_inputs 2.4", text)


class BenchmarkFailureTests(Fixture):
    def test_database_error_keeps_the_trial_pending_instead_of_crashing(self):
        from etf_agent.strategy_reflection.trial import open_trial  # noqa: F401

        with mock.patch("etf_agent.benchmark.build_benchmark_comparison", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(service.benchmark_excess(self.tmp, self.tmp / "db", SATURDAY), {})


class ThesisTrackingTests(unittest.TestCase):
    """買進論點追蹤：狀態只依價格事實，理由文字只在 ``text``，除權息與缺價不亂算。"""

    def run_for(self, prices, tolerance="0.10", ex_rights=False, alerts=None):
        from decimal import Decimal
        from etf_agent.strategy_reflection.thesis import build_thesis_tracking

        class Book:
            def trading_days(self, start):
                return sorted(prices)

            def vwap(self, day):
                return {"2330.TW": Decimal(prices[day])} if prices[day] is not None else {}

            def ex_rights_symbols(self, after, through):
                return {"2330.TW"} if ex_rights else set()

        entry = {"intent": {"items": [{"symbol": "2330.TW", "intent": "buy", "conviction": "medium", "rationale": "營收強",
                                       "invalidation_conditions": ["跌破六十日均線"], "unresolved_questions": []}]},
                 "decision": {"orders": [{"symbol": "2330.TW", "side": "buy", "estimated_price": "100"}],
                              "portfolio": {"positions": [{"symbol": "2330.TW"}]}},
                 "team_inputs": {}}
        later = {"intent": {"items": [{"symbol": "2330.TW", "intent": "hold"}]}, "decision": {"orders": [], "portfolio": {"positions": [{"symbol": "2330.TW"}]}},
                 "team_inputs": {"position_review": {"items": [{"symbol": "2330.TW", "flags": [{"flag": f} for f in (alerts or [])]}]}}}
        rows = build_thesis_tracking({"2026-11-02": entry, "2026-11-03": later}, Book(), Decimal(tolerance))
        return rows[0]

    def test_up_down_and_breach_states(self):
        self.assertEqual(self.run_for({"2026-11-02": "100", "2026-11-03": "103"})["status"], "on_track")
        down = self.run_for({"2026-11-02": "100", "2026-11-03": "96"})
        self.assertEqual((down["status"], down["return_since_entry"]), ("within_tolerance", "-0.040000"))
        self.assertEqual(self.run_for({"2026-11-02": "100", "2026-11-03": "88"})["status"], "needs_review")

    def test_loss_alert_forces_review_even_if_price_recovered(self):
        self.assertEqual(self.run_for({"2026-11-02": "100", "2026-11-03": "99"}, alerts=["loss_alert"])["status"], "needs_review")

    def test_max_drawdown_uses_running_peak(self):
        row = self.run_for({"2026-11-02": "100", "2026-11-03": "110", "2026-11-04": "99"})
        self.assertEqual(row["max_drawdown_since_entry"], "-0.100000")

    def test_ex_rights_and_missing_prices_are_not_guessed(self):
        self.assertEqual(self.run_for({"2026-11-02": "100", "2026-11-03": "50"}, ex_rights=True)["status"], "ex_rights")
        self.assertEqual(self.run_for({"2026-11-02": None})["status"], "unavailable")

    def test_reason_text_stays_out_of_the_numeric_view_but_in_the_report(self):
        from etf_agent.strategy_reflection.thesis import numeric_view

        row = self.run_for({"2026-11-02": "100", "2026-11-03": "96"})
        self.assertNotIn("營收強", json.dumps(numeric_view([row]), ensure_ascii=False))
        text = render_weekly_report(NOW, {"decision_days": 1, "required_decision_days": 10, "gate_open": False, "knobs": {}},
                                    {"status": "observe_only", "summary": "s"}, None, None, [], [row])
        self.assertIn("當初理由：營收強", text)
        self.assertIn("重估條件：跌破六十日均線", text)
        self.assertIn("短期回檔，在容忍範圍內", text)


class ThesisReviewTests(unittest.TestCase):
    """論點檢視：只引用該檔最新內容、逐條對應重估條件、失敗標 unknown 且不中斷。"""

    ROW = {"symbol": "2330.TW", "entry_day": "2026-10-27", "status": "within_tolerance", "days_held": 2, "return_since_entry": "-0.03", "max_drawdown_since_entry": "-0.04", "tolerance": "0.10",
           "text": {"rationale": "營收強", "invalidation_conditions": ["毛利率低於指引", "技術面轉為下跌"], "unresolved_questions": []}}
    CONTEXT = {"2330.TW": {"findings": {"fund-2330-1": {"analyst": "fundamental", "outlook": "positive", "text": "毛利率持平"}},
                           "claims": {"bear-2330-1": {"role": "bear", "text": "動能降溫"}}}}

    def briefs(self):
        from etf_agent.strategy_reflection.thesis_review import review_briefs

        return review_briefs([self.ROW], self.CONTEXT)

    def item(self, **changes):
        base = {"symbol": "2330.TW", "verdict": "intact", "summary": "毛利率持平，理由仍在", "finding_ids": ["fund-2330-1"],
                "conditions": [{"status": "not_triggered", "finding_ids": ["fund-2330-1"]}, {"status": "unknown", "finding_ids": []}]}
        return {"items": [{**base, **changes}]}

    def errors(self, output):
        from etf_agent.strategy_reflection.thesis_review import validate_review

        return validate_review(output, self.briefs())

    def test_valid_review_passes_and_price_facts_are_only_background(self):
        self.assertEqual(self.errors(self.item()), [])
        self.assertEqual(self.briefs()[0]["price_facts"]["return_since_entry"], "-0.03")

    def test_coverage_condition_count_and_ids_are_enforced(self):
        self.assertTrue(self.errors({"items": []}))
        self.assertTrue(any("逐條對應" in e for e in self.errors(self.item(conditions=[{"status": "unknown", "finding_ids": []}]))))
        self.assertTrue(any("不屬於該檔最新內容" in e for e in self.errors(self.item(finding_ids=["fund-9999-1"]))))

    def test_triggered_needs_evidence_and_verdict_must_agree(self):
        triggered = [{"status": "triggered", "finding_ids": []}, {"status": "unknown", "finding_ids": []}]
        self.assertTrue(any("必須引用" in e for e in self.errors(self.item(conditions=triggered))))
        with_id = [{"status": "triggered", "finding_ids": ["fund-2330-1"]}, {"status": "unknown", "finding_ids": []}]
        self.assertTrue(any("不能是 intact" in e for e in self.errors(self.item(conditions=with_id))))
        self.assertEqual(self.errors(self.item(verdict="broken", conditions=with_id)), [])
        self.assertTrue(any("至少要有一個被觸發" in e for e in self.errors(self.item(verdict="broken"))))
        self.assertTrue(any("必須引用" in e for e in self.errors(self.item(verdict="weakened", finding_ids=[]))))

    def run_review(self, runner):
        with tempfile.TemporaryDirectory() as temp:
            return service.review_thesis(runner, Path(temp), [self.ROW], self.CONTEXT)

    def test_valid_agent_output_is_kept(self):
        from etf_agent.automation.agent_runner import AgentCall

        class Runner:
            def run(inner, task, run_dir):
                return AgentCall(task.name, 0, ThesisReviewTests.item(self), 0.1)

        self.assertEqual(self.run_review(Runner())["2330.TW"]["verdict"], "intact")

    def test_invalid_output_runner_error_and_no_runner_all_become_unknown(self):
        from etf_agent.automation.agent_runner import AgentCall, DailyPipelineError

        class Bad:
            def run(inner, task, run_dir):
                return AgentCall(task.name, 0, ThesisReviewTests.item(self, finding_ids=["nope"]), 0.1)

        class Boom:
            def run(inner, task, run_dir):
                raise DailyPipelineError("逾時")

        for runner in (Bad(), Boom(), None):
            result = self.run_review(runner)["2330.TW"]
            self.assertEqual(result["verdict"], "unknown", runner)
            self.assertEqual([c["status"] for c in result["conditions"]], ["unknown", "unknown"])

    def test_report_shows_verdict_and_condition_status(self):
        review = {"2330.TW": {**self.item(verdict="weakened")["items"][0]}}
        text = render_weekly_report(NOW, {"decision_days": 1, "required_decision_days": 10, "gate_open": False, "knobs": {}},
                                    {"status": "observe_only", "summary": "s"}, None, None, [], [{**self.ROW, "entry_intent": "buy", "entry_price": "1",
                                    "intents_since": {}, "alerts_since": {}}], review)
        self.assertIn("理由轉弱", text)
        self.assertIn("毛利率低於指引（未觸發）", text)
        self.assertIn("只供參考，不影響交易", text)


class BenchmarkDiagnosticsTests(Fixture):
    COMPARISON = {"status": "ok", "account": {"series": [{"trade_date": "d%d" % i, "nav": str(100 + i)} for i in range(7)]}, "benchmarks": [
        {"benchmark_id": "0050_buy_and_hold", "status": "ok", "comparison": {
            "status": "ok", "aligned_days": 3, "account_cumulative_return": "0.010000", "benchmark_cumulative_return": "0.020000",
            "excess_return": "-0.010000", "relative_drawdown": "0.005000", "from_trade_date": "x"},
         "series": [{"trade_date": "d%d" % i, "nav": str(100 + 2 * i)} for i in range(7)]},
        {"benchmark_id": "universe_equal_weight", "status": "ok", "comparison": {"status": "unavailable", "reason": "r"}},
    ]}

    def test_only_ok_comparisons_with_numbers_become_diagnostics(self):
        with mock.patch.object(service, "_benchmark_comparison", return_value=self.COMPARISON):
            result = service.benchmark_diagnostics(self.tmp, self.tmp / "db", NOW.isoformat())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(list(result["benchmarks"]), ["0050_buy_and_hold"])
        self.assertEqual(result["benchmarks"]["0050_buy_and_hold"]["excess_return"], "-0.010000")
        self.assertNotIn("from_trade_date", result["benchmarks"]["0050_buy_and_hold"])
        # 最近 5 個交易日：帳戶 101→106（+4.950495%），基準 102→112（+9.803922%）
        self.assertEqual(result["benchmarks"]["0050_buy_and_hold"]["week_account_return"], "0.049505")
        self.assertEqual(result["benchmarks"]["0050_buy_and_hold"]["week_benchmark_return"], "0.098039")

    def test_unavailable_ledger_or_no_aligned_day_is_labelled_not_guessed(self):
        with mock.patch.object(service, "_benchmark_comparison", return_value=None):
            self.assertEqual(service.benchmark_diagnostics(self.tmp, self.tmp / "db", NOW.isoformat())["status"], "unavailable")
        empty = {"status": "ok", "benchmarks": [{"benchmark_id": "a", "comparison": {"status": "unavailable"}}]}
        with mock.patch.object(service, "_benchmark_comparison", return_value=empty):
            self.assertEqual(service.benchmark_diagnostics(self.tmp, self.tmp / "db", NOW.isoformat())["status"], "unavailable")

    def test_ok_benchmark_becomes_evidence_and_shows_in_report(self):
        with mock.patch.object(service, "_benchmark_comparison", return_value=self.COMPARISON):
            bench = service.benchmark_diagnostics(self.tmp, self.tmp / "db", NOW.isoformat())
        brief = build_brief(report(status="insufficient"), self.template, NOW.isoformat(), {"decision_days": 1, "benchmark": bench})
        self.assertIn("diag.benchmark.benchmarks.0050_buy_and_hold.excess_return", brief["evidence"])
        text = render_weekly_report(NOW, brief, observe_only(brief, NOW))
        self.assertIn("0050 買入持有（對齊 3 個交易日）", text)
        self.assertIn("超額 -0.010000", text)


class ApplyPendingTests(Fixture):
    def seal(self, now=FRIDAY, proposal=True):
        brief = self.brief if proposal else build_brief(report(status="insufficient"), self.template, NOW.isoformat())
        reflection = self.reflection() if proposal else observe_only(brief, now)
        reflection_id = reflection["reflection_id"]
        save_reflection(self.repository, reflection, brief)
        return reflection_id

    def pending(self, now=SATURDAY, **options):
        return apply_pending(self.tmp, self.tmp / "db", self.tmp / "decisions", now,
                             replayer=passing_replayer, clock=lambda: now, **options)

    def test_saturday_morning_applies_the_friday_proposal(self):
        reflection_id = self.seal()
        result = self.pending()
        self.assertTrue(result["applied"], result)
        self.assertEqual(result["reflection_id"], reflection_id)
        self.assertEqual(self.live()["position_sizing"]["conviction_multipliers"]["high"], "1.4")

    def test_not_applied_twice(self):
        self.seal()
        self.pending()
        self.assertIn("試用觀察期尚未結案", self.pending()["reason"])

    def test_no_reflection_or_no_proposal_applies_nothing(self):
        self.assertFalse(self.pending()["applied"])
        self.seal(proposal=False)
        self.assertIn("沒有提案", self.pending()["reason"])

    def test_stale_reflection_is_not_applied(self):
        self.seal()
        stale = self.pending(SATURDAY + timedelta(days=9))
        self.assertIn("已過期", stale["reason"])
        self.assertEqual(self.live()["position_sizing"]["conviction_multipliers"]["high"], "1.5")

    def test_apply_window_still_protects_the_evening_decisions(self):
        self.seal()
        result = self.pending(SATURDAY_LATE)
        self.assertFalse(result["applied"])
        self.assertEqual(self.live()["position_sizing"]["conviction_multipliers"]["high"], "1.5")

    def test_failed_replay_gate_blocks_the_pending_apply(self):
        self.seal()
        result = apply_pending(self.tmp, self.tmp / "db", self.tmp / "decisions", SATURDAY,
                               replayer=lambda path, replay_id: replay_body("90") if replay_id.endswith("candidate") else replay_body(),
                               clock=lambda: SATURDAY)
        self.assertFalse(result["applied"])
        self.assertIn("影子回放未通過", result["reason"])


if __name__ == "__main__":
    unittest.main()
