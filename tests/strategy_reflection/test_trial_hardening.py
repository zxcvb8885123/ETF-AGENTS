"""試用期與回饋迴圈的加固：逾時／孤兒結案、還原失敗、被還原旋鈕的簡報證據與再提案限制、歷史鎖、Agent 失敗仍有週報。"""

import threading
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from etf_agent.automation.agent_runner import DailyPipelineError
from etf_agent.strategy_reflection import (
    ReflectionError, ReflectionRepository, apply_pending, apply_reflection, build_brief, from_agent_output, run_auto, save_reflection,
    service, validate_reflection,
)
from etf_agent.strategy_reflection.contracts import TRIAL_MAX_CALENDAR_DAYS
from etf_agent.strategy_reflection.trial import change_direction, consecutive_reverts, knob_outcomes, open_trial

from test_strategy_reflection import Fixture, NOW, report, replay_body
from test_trial import APPLY, days
from test_weekly_reflection import FRIDAY, SATURDAY, passing_replayer

KNOB = "conviction_multipliers.high"
RESULT = {"action": "trial_result", "of": "r1", "final": True}


def revert_history(changes=True, decision_days=0):
    apply = {"action": "apply", "reflection_id": "r1", "applied_at": "2026-11-01T10:00:00+08:00", "knobs": [KNOB],
             "decision_days": decision_days, "origins": {KNOB: "1.5"}, "trial": {"verification": "replay"}}
    if changes:
        apply["changes"] = {KNOB: {"from": "1.5", "to": "1.6"}}
    return [apply, {**RESULT, "result": "reverted"}, {"action": "rollback", "of": "r1"}]


class SettleHardeningTests(Fixture):
    def settle(self, excess, history):
        for entry in history:
            self.repository.append_history(entry)
        return service.settle_open_trial(self.tmp, SATURDAY, self.repository, excess, clock=lambda: SATURDAY)

    def test_trial_that_cannot_be_evaluated_times_out_instead_of_freezing(self):
        old = {**APPLY, "applied_at": "2026-11-01T10:00:00+08:00"}  # 到 11/21 已 20 天
        result = self.settle({}, [old])
        self.assertEqual((result["trial"], result["needs_manual"]), ("timeout", True))
        self.assertIn("逾時", result["alert"])
        final = self.repository.history()[-1]
        self.assertEqual((final["action"], final["result"], final["final"]), ("trial_result", "timeout", True))
        self.assertIsNone(open_trial(self.repository.history()))  # 迴圈解凍，變更保留（沒有 rollback 紀錄）
        self.assertNotIn("rollback", [entry["action"] for entry in self.repository.history()])

    def test_trial_inside_the_calendar_limit_keeps_waiting(self):
        young = {**APPLY, "applied_at": "2026-11-13T10:00:00+08:00"}  # 8 天
        self.assertEqual(self.settle({}, [young])["trial"], "pending")
        self.assertEqual(len(self.repository.history()), 1)

    def test_unreadable_apply_time_never_times_out(self):
        self.assertEqual(self.settle({}, [{**APPLY, "applied_at": "unknown"}])["trial"], "pending")
        self.assertEqual(TRIAL_MAX_CALENDAR_DAYS, 15)

    def test_failed_revert_is_settled_and_flagged_for_a_human(self):
        before = days(0, ["0.002", "0.001", "0.003", "0.002", "0.001", "0.002"])
        worse = {**before, **days(8, ["-0.02"] * 6)}
        with mock.patch.object(service, "rollback", side_effect=ReflectionError("備份讀不到")):
            result = self.settle(worse, [{**APPLY, "applied_at": "2026-11-07"}])
        self.assertEqual((result["trial"], result["needs_manual"]), ("revert_failed", True))
        self.assertIn("備份讀不到", result["reason"])
        self.assertTrue(self.repository.history()[-1]["final"])
        self.assertIsNone(open_trial(self.repository.history()))  # 不會每週卡在同一個失敗上


class OrphanedTrialTests(Fixture):
    def apply(self):
        reflection_id = save_reflection(self.repository, self.reflection(), self.brief)
        self.assertTrue(apply_reflection(self.tmp, reflection_id, passing_replayer, NOW, self.repository)["applied"])

    def settle(self, excess=None):
        return service.settle_open_trial(self.tmp, SATURDAY, self.repository, excess or {}, clock=lambda: SATURDAY)

    def test_a_normal_apply_matches_the_live_file_and_stays_open(self):
        self.apply()
        self.assertEqual(self.settle()["trial"], "pending")

    def test_apply_interrupted_before_the_file_was_replaced_is_closed_and_unwound(self):
        entry = {**APPLY, "policy_sha256_after": "0" * 64, "policy_id_before": self.template["policy_id"],
                 "policy_id_after": "strategy-x", "origins": {"trim_fraction": "0.5"}}
        self.repository.append_history(entry)
        result = self.settle()
        self.assertEqual((result["trial"], result["needs_manual"]), ("orphaned", True))
        history = self.repository.history()
        self.assertEqual([item["action"] for item in history], ["apply", "rollback", "trial_result"])
        self.assertIsNone(open_trial(history))

    def test_live_policy_edited_by_hand_is_closed_without_touching_it(self):
        self.apply()
        live = self.live()
        live["trim_fraction"] = "0.35"
        (self.tmp / "config" / "decision_policy.json").write_text(__import__("json").dumps(live), encoding="utf-8")
        result = self.settle()
        self.assertEqual(result["trial"], "orphaned")
        self.assertEqual(self.live()["trim_fraction"], "0.35")
        self.assertNotIn("rollback", [item["action"] for item in self.repository.history()])

    def test_lessons_trial_with_missing_file_is_orphaned(self):
        self.repository.append_history({"action": "lessons_apply", "reflection_id": "lessons-x", "applied_at": "2026-11-20T10:00:00+08:00",
                                        "knobs": ["strategy_lessons"], "decision_days": 0, "sha256_after": "a" * 64,
                                        "trial": {"verification": "observe"}})
        self.assertEqual(self.settle()["trial"], "orphaned")


class FeedbackLoopTests(Fixture):
    def reflection_for(self, brief, proposed):
        return from_agent_output(self.output(proposed=proposed), brief, self.template, self.rules, NOW)

    def validate(self, history, proposed, days=25):
        brief = build_brief(report(days=days), self.template, NOW.isoformat(), None, history)
        validate_reflection(self.reflection_for(brief, proposed), brief, self.template, self.rules, history)

    def test_reverted_knob_shows_up_as_citable_evidence(self):
        brief = build_brief(report(), self.template, NOW.isoformat(), None, revert_history())
        self.assertEqual(brief["evidence"]["hist.%s.last_result" % KNOB], "reverted")
        self.assertEqual(brief["evidence"]["hist.%s.reverted_count" % KNOB], "1")
        self.assertFalse([key for key in build_brief(report(), self.template, NOW.isoformat())["evidence"] if key.startswith("hist.")])

    def test_outcomes_distinguish_in_trial_rolled_back_and_kept(self):
        in_trial = knob_outcomes([revert_history()[0]])
        self.assertEqual(in_trial[KNOB]["last_result"], "in_trial")
        manual = knob_outcomes(revert_history()[::2])  # apply + rollback，沒有結案紀錄
        self.assertEqual(manual[KNOB]["last_result"], "rolled_back")
        kept = knob_outcomes([revert_history()[0], {**RESULT, "result": "kept"}])
        self.assertEqual((kept[KNOB]["last_result"], kept[KNOB]["reverted_entry"]), ("kept", None))

    def test_cooldown_doubles_after_a_reverted_trial(self):
        with self.assertRaisesRegex(ReflectionError, "冷卻加倍"):
            self.validate(revert_history(), "1.4", days=15)  # 一般冷卻 10 天已過，但加倍的 20 天沒到

    def test_same_direction_is_refused_after_a_revert_but_the_opposite_is_allowed(self):
        with self.assertRaisesRegex(ReflectionError, "同方向"):
            self.validate(revert_history(), "1.6")
        self.validate(revert_history(), "1.4")

    def test_legacy_history_without_direction_only_gets_the_doubled_cooldown(self):
        self.validate(revert_history(changes=False), "1.6")
        with self.assertRaisesRegex(ReflectionError, "冷卻加倍"):
            self.validate(revert_history(changes=False), "1.6", days=15)

    def test_a_later_kept_trial_lifts_the_direction_ban(self):
        later = {"action": "apply", "reflection_id": "r2", "applied_at": "2026-11-10T10:00:00+08:00", "knobs": [KNOB],
                 "decision_days": 8, "trial": {"verification": "replay"}}
        history = revert_history() + [later, {**RESULT, "of": "r2", "result": "kept"}]
        self.validate(history, "1.6", days=30)

    def test_apply_records_the_direction_of_every_change(self):
        reflection_id = save_reflection(self.repository, self.reflection(), self.brief)
        apply_reflection(self.tmp, reflection_id, passing_replayer, NOW, self.repository)
        self.assertEqual(self.repository.history()[0]["changes"], {KNOB: {"from": "1.5", "to": "1.4"}})
        self.assertEqual((change_direction(KNOB, "1.5", "1.4"), change_direction(KNOB, "1.5", "1.5"), change_direction("nope", 1, 2)), (-1, 0, 0))
        self.assertEqual(change_direction("rotation.min_new_conviction", "low", "medium"), 1)

    def test_revert_counter_restarts_after_auto_disable(self):
        revert = {**RESULT, "result": "reverted"}
        disabled = {"action": "auto_disabled"}
        self.assertEqual(consecutive_reverts([revert, revert]), 2)
        self.assertEqual(consecutive_reverts([revert, revert, disabled]), 0)
        self.assertEqual(consecutive_reverts([revert, revert, disabled, revert]), 1)

    def test_trial_note_counts_the_same_days_as_the_settlement(self):
        entry = {**APPLY, "applied_at": "2026-11-07"}
        excess = {**days(0, ["0.001"] * 6), **days(6, ["0.001"] * 3)}  # 套用日之後 3 個交易日
        self.assertIn("新交易日 3／5", service.trial_note([entry], 99, excess))
        fallback = service.trial_note([entry], 12)
        self.assertIn("新決策日 2／5", fallback)
        self.assertIn("基準資料不可用", fallback)
        self.assertIsNone(service.trial_note([], 12))


class HistoryLockTests(Fixture):
    def test_concurrent_appends_never_lose_an_entry(self):
        def worker(index):
            repository = ReflectionRepository(self.tmp)  # 各執行緒自己的實例，只靠檔案鎖協調
            for step in range(8):
                repository.append_history({"action": "note", "n": "%d-%d" % (index, step)})

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        history = self.repository.history()
        self.assertEqual(len(history), 64)
        self.assertEqual(len({entry["n"] for entry in history}), 64)
        self.assertTrue(self.repository.history_lock_path.exists())


class AgentFailureTests(Fixture):
    class Failing:
        def run(self, task, run_dir):
            raise DailyPipelineError("反思 Agent 逾時")

    def test_agent_failure_still_leaves_an_observe_only_reflection_and_a_report(self):
        before = (self.tmp / "config" / "decision_policy.json").read_text(encoding="utf-8")
        with mock.patch.object(service, "make_brief", return_value=self.brief), self.assertRaises(DailyPipelineError):
            run_auto(self.tmp, self.tmp / "db", self.tmp / "decisions", self.Failing(), FRIDAY)
        ids = service.reflection_ids(self.repository)
        self.assertEqual(len(ids), 1)
        reflection, _ = service.load_reflection(self.repository, ids[0])
        self.assertEqual((reflection["status"], reflection["proposals"]), ("observe_only", []))
        text = (self.tmp / "var" / "strategy_reflection" / "reports" / "weekly-2026-11-20.md").read_text(encoding="utf-8")
        self.assertIn("反思 Agent 失敗", text)
        self.assertIn("逾時", text)
        self.assertEqual((self.tmp / "config" / "decision_policy.json").read_text(encoding="utf-8"), before)

    def test_proposal_sealed_a_week_ago_is_still_applied_on_the_next_saturday(self):
        save_reflection(self.repository, self.reflection(), self.brief)
        later = SATURDAY + timedelta(days=7)  # 週五 20:30 封存 → 下週六 10:00：8 天又 13.5 小時
        result = apply_pending(self.tmp, self.tmp / "db", self.tmp / "decisions", later, replayer=passing_replayer, clock=lambda: later)
        self.assertTrue(result["applied"], result)


if __name__ == "__main__":
    unittest.main()
