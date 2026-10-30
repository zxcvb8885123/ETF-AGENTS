"""試用觀察期：前後比較、延長、還原、自動停用與累積偏移上限。"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from etf_agent.strategy_reflection import ReflectionError, ReflectionRepository, service
from etf_agent.strategy_reflection.contracts import KNOBS
from etf_agent.strategy_reflection.tools import check_proposal, knob_origins, current_knobs
from etf_agent.strategy_reflection.trial import (
    consecutive_reverts, evaluate_trial, excess_series, open_trial, trial_verification,
)

from test_strategy_reflection import Fixture, NOW
from test_weekly_reflection import SATURDAY, passing_replayer

APPLY = {"action": "apply", "reflection_id": "r1", "applied_at": "2026-11-13T10:00:00+08:00",
         "knobs": ["trim_fraction"], "decision_days": 10, "trial": {"verification": "replay"}}


def days(start, values):
    base = datetime(2026, 11, 2)
    return {(base + timedelta(days=start + i)).date().isoformat(): Decimal(v) for i, v in enumerate(values)}


class EvaluateTests(unittest.TestCase):
    def test_pending_until_enough_new_days(self):
        excess = {**days(0, ["0.001"] * 10), **days(11, ["0.001"] * 3)}
        self.assertEqual(evaluate_trial(APPLY, excess)["verdict"], "pending")

    def test_clearly_worse_requires_exceeding_threshold(self):
        before = days(0, ["0.002", "0.001", "0.003", "0.002", "0.001", "0.002"])
        worse = {**before, **days(8, ["-0.02"] * 6)}
        self.assertEqual(evaluate_trial({**APPLY, "applied_at": "2026-11-07"}, worse)["verdict"], "worse")
        flat = {**before, **days(8, ["0.002"] * 6)}
        self.assertEqual(evaluate_trial({**APPLY, "applied_at": "2026-11-07"}, flat)["verdict"], "inconclusive")  # 看不出差別
        better = {**before, **days(8, ["0.03"] * 6)}
        self.assertEqual(evaluate_trial({**APPLY, "applied_at": "2026-11-07"}, better)["verdict"], "not_worse")

    def test_five_trading_days_after_apply_are_enough(self):
        before = days(0, ["0.002", "0.001", "0.003", "0.002", "0.001", "0.002"])  # 11/2～11/7
        five = {**before, **days(6, ["0.03"] * 5)}  # 11/8～11/12：套用日（11/7）之後正好 5 個交易日
        self.assertEqual(evaluate_trial({**APPLY, "applied_at": "2026-11-07"}, five)["verdict"], "not_worse")
        four = {**before, **days(6, ["0.03"] * 4)}
        self.assertEqual(evaluate_trial({**APPLY, "applied_at": "2026-11-07"}, four)["verdict"], "pending")

    def test_short_baseline_is_inconclusive_not_worse(self):
        short = {**days(0, ["0.001"] * 2), **days(3, ["-0.05"] * 6)}
        result = evaluate_trial({**APPLY, "applied_at": "2026-11-04"}, short)
        self.assertEqual(result["verdict"], "inconclusive")

    def test_excess_series_needs_an_approved_benchmark(self):
        self.assertEqual(excess_series({"status": "unavailable"}), {})
        comparison = {"status": "ok", "account": {"series": [{"trade_date": "d1", "nav": "100"}, {"trade_date": "d2", "nav": "110"}]},
                      "benchmarks": [{"benchmark_id": "universe_equal_weight", "status": "ok",
                                      "series": [{"trade_date": "d1", "nav": "100"}, {"trade_date": "d2", "nav": "105"}]}]}
        self.assertEqual(excess_series(comparison), {"d2": Decimal("0.05")})

    def test_benchmark_gap_is_skipped_not_treated_as_one_day(self):
        account = [{"trade_date": "d%d" % i, "nav": str(100 + i)} for i in (1, 2, 3)]
        bench = [{"trade_date": "d1", "nav": "100"}, {"trade_date": "d3", "nav": "103"}]  # d2 缺基準
        comparison = {"status": "ok", "account": {"series": account},
                      "benchmarks": [{"benchmark_id": "universe_equal_weight", "status": "ok", "series": bench}]}
        self.assertEqual(excess_series(comparison), {})


class HistoryTests(unittest.TestCase):
    def test_open_trial_closes_on_final_result_and_rollback(self):
        self.assertEqual(open_trial([APPLY]), APPLY)
        self.assertIsNone(open_trial([APPLY, {"action": "trial_result", "of": "r1", "final": True, "result": "kept"}]))
        self.assertIsNone(open_trial([APPLY, {"action": "rollback"}]))
        self.assertEqual(open_trial([APPLY, {"action": "trial_result", "of": "r1", "final": False, "result": "extended"}]), APPLY)

    def test_consecutive_reverts_reset_on_keep(self):
        revert = {"action": "trial_result", "final": True, "result": "reverted"}
        keep = {"action": "trial_result", "final": True, "result": "kept"}
        self.assertEqual(consecutive_reverts([revert, revert]), 2)
        self.assertEqual(consecutive_reverts([revert, keep, revert]), 1)

    def test_observe_knobs_force_observe_verification(self):
        self.assertEqual(trial_verification(["trim_fraction"]), "replay")
        self.assertEqual(trial_verification(["trim_fraction", "position_review.loss_alert"]), "observe")

    def test_origin_is_first_value_still_in_effect(self):
        history = [{"action": "apply", "origins": {"trim_fraction": "0.5"}}, {"action": "apply", "origins": {"trim_fraction": "0.45"}}]
        self.assertEqual(knob_origins(history), {"trim_fraction": "0.5"})
        self.assertEqual(knob_origins(history + [{"action": "rollback"}, {"action": "rollback"}]), {})


class KnobTests(Fixture):
    def test_null_knob_is_not_listed(self):
        self.assertNotIn("position_review.gain_alert", current_knobs(self.template))
        with self.assertRaises(ReflectionError):
            check_proposal("position_review.gain_alert", "0.3", self.template, self.rules)

    def test_drift_beyond_two_steps_from_origin_is_rejected(self):
        step = KNOBS["trim_fraction"].step
        origin = Decimal(str(self.template["trim_fraction"]))
        template = {**self.template, "trim_fraction": str(origin + step * 2)}  # 已離起點 2 階
        beyond = str(origin + step * 3)
        with self.assertRaises(ReflectionError):
            check_proposal("trim_fraction", beyond, template, self.rules, {"trim_fraction": str(origin)})
        self.assertEqual(check_proposal("trim_fraction", beyond, template, self.rules)["proposed"], beyond)
        back = str(origin + step)  # 往起點方向走仍可
        self.assertEqual(check_proposal("trim_fraction", back, template, self.rules, {"trim_fraction": str(origin)})["proposed"], back)

    def test_review_thresholds_are_in_bounds_and_version_is_forbidden(self):
        template = {**self.template, "position_review": {"version": "v1", "loss_alert": "0.10", "retrace_alert_20d": "0.12", "gain_alert": None}}
        self.assertEqual(check_proposal("position_review.loss_alert", "0.11", template, self.rules)["proposed"], "0.11")
        for knob, value in (("position_review.loss_alert", "0.30"), ("position_review.version", "v2"), ("position_review.gain_alert", "0.3")):
            with self.assertRaises(ReflectionError, msg=knob):
                check_proposal(knob, value, template, self.rules)

    def test_prompt_limits_come_from_knobs(self):
        text = service.knob_limits_text()
        for path in KNOBS:
            self.assertIn(path, text)


class SettleTests(Fixture):
    def settle(self, excess, history=None):
        repository = ReflectionRepository(self.tmp)
        for entry in history or []:
            repository.append_history(entry)
        return service.settle_open_trial(self.tmp, SATURDAY, repository, excess, clock=lambda: SATURDAY), repository

    def test_nothing_open_returns_none(self):
        self.assertIsNone(self.settle({})[0])

    def test_without_benchmark_the_trial_stays_pending(self):
        result, _ = self.settle({}, [APPLY])
        self.assertEqual(result["trial"], "pending")

    def test_auto_disable_during_settlement_stops_the_same_run_from_applying(self):
        from unittest.mock import patch

        def settle_and_disable(root, *args, **kwargs):
            service.disable_auto_apply(root)
            return {"trial": "reverted", "auto_apply_disabled": True}

        with patch.object(service, "settle_open_trial", side_effect=settle_and_disable), \
                patch.object(service, "_apply_pending_knobs") as knobs, patch.object(service, "_apply_pending_lessons") as taught:
            result = service.apply_pending(self.tmp, self.tmp / "m.db", self.tmp / "d", SATURDAY, excess={})
        self.assertFalse(result["applied"])
        knobs.assert_not_called()
        taught.assert_not_called()

    def test_enabled_switch_defaults_open_and_malformed_fails_closed(self):
        self.assertEqual(service.load_switch(self.tmp), {"enabled": True, "auto_apply": True})
        (self.tmp / "config" / "strategy_reflection.json").write_text("{bad", encoding="utf-8")
        self.assertEqual(service.load_switch(self.tmp), {"enabled": False, "auto_apply": False})


if __name__ == "__main__":
    unittest.main()
