"""經驗教訓：數值整理、文字規則、套用／試用／還原，以及交易 brief 只拿到背景文字。"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from etf_agent.automation.agent_runner import AgentCall, DailyPipelineError
from etf_agent.decision.strategy_lessons import (
    LESSONS_PATH, lesson_text_errors, lessons_brief, load_strategy_lessons, seal_lessons, validate_lessons_file,
)
from etf_agent.strategy_reflection import ReflectionError, ReflectionRepository, service
from etf_agent.strategy_reflection.lessons import (
    LESSONS_ACTION, apply_lessons, lessons_brief_for_agent, lessons_from_output, load_pending, revert_lessons, save_pending,
    thesis_aggregates,
)
from etf_agent.strategy_reflection.trial import open_trial

from test_strategy_reflection import Fixture, NOW
from test_weekly_reflection import SATURDAY, SATURDAY_LATE

GOOD = "高信心買進後多半回吐，買進前先確認回檔再進場"


def lessons(text=GOOD, evidence=("thesis.mean_return",), lessons_id="lessons-1"):
    return seal_lessons(lessons_id, NOW.isoformat(), [{"text": text, "evidence_ids": list(evidence)}])


def row(symbol, conviction, ret, days=5, status="within_tolerance"):
    return {"symbol": symbol, "entry_conviction": conviction, "entry_intent": "buy", "days_held": days, "return_since_entry": ret,
            "max_drawdown_since_entry": "-0.02", "status": status}


class LoadCutoffTests(unittest.TestCase):
    def test_lessons_created_after_decision_cutoff_are_not_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / LESSONS_PATH).write_text(json.dumps(lessons()), encoding="utf-8")
            self.assertIsNotNone(load_strategy_lessons(root))
            self.assertIsNotNone(load_strategy_lessons(root, (NOW + timedelta(days=1)).isoformat()))
            self.assertIsNone(load_strategy_lessons(root, (NOW - timedelta(days=1)).isoformat()))
            self.assertIsNone(load_strategy_lessons(root, "沒有時區"))


class TextRuleTests(unittest.TestCase):
    def test_general_advice_passes(self):
        self.assertEqual(lesson_text_errors(GOOD), [])

    def test_codes_years_ids_urls_and_length_are_rejected(self):
        for bad in ("2330 買進後多半回吐要小心追高進場", "3653健策買進後回吐要小心追高進場", "2026 年的做法要更保守小心追高",
                    "依 review:2330.TW:position 的事實要更保守", "參考 https://example.com 的做法更保守進場", "太短", "字" * 121):
            self.assertTrue(lesson_text_errors(bad), bad)

    def test_file_validation_catches_tampering_and_too_many(self):
        data = lessons()
        self.assertEqual(validate_lessons_file(data), [])
        self.assertTrue(validate_lessons_file({**data, "lessons": [{"text": GOOD + "改", "evidence_ids": ["x"]}]}))
        many = seal_lessons("l", NOW.isoformat(), [{"text": GOOD, "evidence_ids": ["x"]}] * 4)
        self.assertTrue(validate_lessons_file(many))


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, data):
        (self.root / LESSONS_PATH).parent.mkdir(parents=True, exist_ok=True)
        (self.root / LESSONS_PATH).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_missing_tampered_or_invalid_file_means_no_lessons(self):
        self.assertIsNone(load_strategy_lessons(self.root))
        self.write({**lessons(), "lessons_id": "改過"})
        self.assertIsNone(load_strategy_lessons(self.root))
        (self.root / LESSONS_PATH).write_text("{bad", encoding="utf-8")
        self.assertIsNone(load_strategy_lessons(self.root))

    def test_brief_gives_text_and_rules_but_no_identifiers(self):
        self.write(lessons())
        brief = lessons_brief(load_strategy_lessons(self.root))
        self.assertEqual(brief["lessons"], [GOOD])
        self.assertNotIn("lessons-1", json.dumps(brief, ensure_ascii=False))
        self.assertIn("不是證據", brief["note"])
        self.assertIsNone(lessons_brief(None))


class AggregateTests(unittest.TestCase):
    def test_groups_need_three_holdings_and_no_free_text_leaks(self):
        thesis = [row("a", "high", "0.10"), row("b", "high", "-0.02"), row("c", "high", "0.04"), row("d", "low", "-0.05"),
                  {"symbol": "e", "status": "unavailable"}]
        evidence = thesis_aggregates(thesis, {"A": {"verdict": "weakened", "conditions": [{"status": "triggered"}]}})
        self.assertEqual(evidence["thesis.holdings"], "4")
        self.assertEqual(evidence["thesis.conviction.high.count"], "3")
        self.assertEqual(evidence["thesis.conviction.high.mean_return"], "0.040000")
        self.assertNotIn("thesis.conviction.low.mean_return", evidence)
        self.assertEqual(evidence["thesis.triggered_conditions"], "1")
        self.assertEqual(thesis_aggregates([], {}), {})

    def test_agent_brief_has_enums_and_numbers_only(self):
        brief = lessons_brief_for_agent([row("2330.TW", "high", "0.1")], {}, {"diag.x": "1", "knobs.y": "2"}, None)
        self.assertIn("diag.x", brief["evidence"])
        self.assertNotIn("knobs.y", brief["evidence"])
        self.assertNotIn("symbol", brief["holdings"][0])
        self.assertEqual(brief["current_lessons"], [])


class ReportNumberingTests(unittest.TestCase):
    def test_section_numbers_are_unique_and_in_order(self):
        from etf_agent.strategy_reflection import render_weekly_report

        brief = {"decision_days": 1, "required_decision_days": 10, "gate_open": False, "knobs": {"a": "1"}}
        text = render_weekly_report(NOW, brief, {"status": "observe_only", "summary": "s"}, None, None, [], [{**row("2330.TW", "high", "0.1"), "entry_day": "2026-10-27", "entry_price": "1", "tolerance": "0.10", "intents_since": {}, "alerts_since": {}}], {},
                                    {"status": "unchanged", "reason": "r"}, [GOOD])
        headings = [line.split("、")[0] for line in text.splitlines() if line.startswith("## ")]
        self.assertEqual(len(headings), len(set(headings)), headings)
        self.assertIn("目前生效：" + GOOD, text)


class OutputTests(unittest.TestCase):
    BRIEF = {"evidence": {"thesis.mean_return": "0.01"}, "current_lessons": [GOOD]}

    def test_unknown_evidence_and_bad_text_are_rejected(self):
        with self.assertRaises(ReflectionError):
            lessons_from_output({"lessons": [{"text": "高信心買進後多半回吐，買進前先確認回檔", "evidence_ids": ["nope"]}]}, self.BRIEF, NOW)
        with self.assertRaises(ReflectionError):
            lessons_from_output({"lessons": [{"text": "2330 買進後多半回吐，之後要更保守進場", "evidence_ids": ["thesis.mean_return"]}]}, self.BRIEF, NOW)

    def test_empty_or_unchanged_means_no_change(self):
        self.assertIsNone(lessons_from_output({"lessons": []}, self.BRIEF, NOW))
        self.assertIsNone(lessons_from_output({"lessons": [{"text": GOOD, "evidence_ids": ["thesis.mean_return"]}]}, self.BRIEF, NOW))

    def test_new_lesson_is_sealed(self):
        text = "持有很久仍未兌現的股票偏多，重估條件的時間要寫得更緊"
        sealed = lessons_from_output({"lessons": [{"text": text, "evidence_ids": ["thesis.mean_return"]}]}, self.BRIEF, NOW)
        self.assertEqual(validate_lessons_file(sealed), [])


class ReflectLessonsTests(Fixture):
    ROWS = [row("2330.TW", "high", "0.1")]

    class Runner:
        def __init__(self, output=None, error=None):
            self.output, self.error, self.calls = output, error, 0

        def run(self, task, run_dir):
            self.calls += 1
            if self.error:
                raise self.error
            return AgentCall(task.name, 0, self.output, 0.1)

    def reflect(self, runner, rows=None):
        run_dir = self.tmp / "run"
        run_dir.mkdir(exist_ok=True)
        return service.reflect_lessons(runner, run_dir, self.tmp / "var" / "strategy_reflection", self.ROWS if rows is None else rows, {}, {}, NOW, self.tmp)

    def test_valid_output_is_saved_as_pending_not_applied(self):
        runner = self.Runner({"lessons": [{"text": GOOD, "evidence_ids": ["thesis.mean_return"]}]})
        result = self.reflect(runner)
        self.assertEqual(result["status"], "proposed")
        self.assertIsNotNone(load_pending(self.tmp / "var" / "strategy_reflection"))
        self.assertFalse((self.tmp / LESSONS_PATH).exists())

    def test_failures_never_raise_and_skip_without_data(self):
        self.assertEqual(self.reflect(self.Runner(error=DailyPipelineError("逾時")))["status"], "failed")
        self.assertEqual(self.reflect(self.Runner({"lessons": [{"text": "2330 太貴不要買進這種股票了", "evidence_ids": ["x"]}]}))["status"], "failed")
        self.assertEqual(self.reflect(None)["status"], "skipped")
        runner = self.Runner({"lessons": []})
        self.assertEqual(self.reflect(runner, rows=[])["status"], "skipped")
        self.assertEqual(runner.calls, 0)


class ApplyAndRevertTests(Fixture):
    def setUp(self):
        super().setUp()
        self.repository = ReflectionRepository(self.tmp)
        self.base = self.repository.base

    def pending(self, now=SATURDAY, **options):
        return service._apply_pending_lessons(self.tmp, now, self.repository, None, lambda: now)

    def test_saturday_applies_pending_and_opens_a_trial(self):
        save_pending(self.base, lessons(lessons_id="lessons-20261120T123000Z"))
        # created_at 用 NOW（週五）→ 週六在 8 天內
        result = self.pending()
        self.assertTrue(result["applied"], result)
        self.assertEqual(load_strategy_lessons(self.tmp)["texts"], [GOOD])
        trial = open_trial(self.repository.history())
        self.assertEqual(trial["action"], LESSONS_ACTION)
        self.assertEqual(trial["trial"]["verification"], "observe")
        self.assertIn("已套用", self.pending()["reason"])

    def test_stale_late_evening_and_empty_cases_do_not_apply(self):
        self.assertFalse(self.pending()["applied"])
        save_pending(self.base, lessons())
        self.assertIn("過期", self.pending(NOW + timedelta(days=9))["reason"])
        self.assertIn("20:50", self.pending(SATURDAY_LATE)["reason"])
        self.assertFalse((self.tmp / LESSONS_PATH).exists())

    def test_applied_lessons_take_effect_at_apply_time_not_proposal_time(self):
        history = []
        apply_lessons(self.tmp, self.base, lessons(lessons_id="l-1"), history.append, 3, SATURDAY)
        # 提案建立於 NOW（週五），週六才生效：週五晚間的 Snapshot 不得讀到。
        self.assertIsNone(load_strategy_lessons(self.tmp, NOW.isoformat()))
        self.assertIsNotNone(load_strategy_lessons(self.tmp, SATURDAY.isoformat()))

    def test_history_is_recorded_before_live_file_and_failed_write_is_reverted(self):
        from unittest import mock

        history = []
        with mock.patch("etf_agent.strategy_reflection.lessons.atomic_write_json", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                apply_lessons(self.tmp, self.base, lessons(lessons_id="l-1"), history.append, 3, SATURDAY)
        self.assertEqual([entry["action"] for entry in history], [LESSONS_ACTION, "lessons_revert"])
        self.assertFalse((self.tmp / LESSONS_PATH).exists())

    def test_revert_restores_previous_version_or_deletes_and_refuses_hand_edits(self):
        history = []
        apply_lessons(self.tmp, self.base, lessons(lessons_id="l-1"), history.append, 3, SATURDAY)
        first = history[-1]
        self.assertFalse(first["had_previous"])
        revert_lessons(self.tmp, first, history.append, SATURDAY)
        self.assertFalse((self.tmp / LESSONS_PATH).exists())

        apply_lessons(self.tmp, self.base, lessons(lessons_id="l-1"), history.append, 3, SATURDAY)
        second_text = "持有很久仍未兌現的股票偏多，重估條件的時間要寫得更緊"
        apply_lessons(self.tmp, self.base, lessons(text=second_text, lessons_id="l-2"), history.append, 8, SATURDAY)
        revert_lessons(self.tmp, history[-1], history.append, SATURDAY)
        self.assertEqual(load_strategy_lessons(self.tmp)["texts"], [GOOD])

        (self.tmp / LESSONS_PATH).write_text((self.tmp / LESSONS_PATH).read_text(encoding="utf-8").replace("回吐", "回落"), encoding="utf-8")
        with self.assertRaises(ReflectionError):
            revert_lessons(self.tmp, history[-2], history.append, SATURDAY)

    def test_open_trial_blocks_knob_proposals_and_a_worse_trial_reverts_lessons(self):
        apply_lessons(self.tmp, self.base, lessons(lessons_id="l-1"), self.repository.append_history, 0, SATURDAY)
        self.assertIsNotNone(open_trial(self.repository.history()))
        days = {(datetime(2026, 11, 2) + timedelta(days=i)).date().isoformat(): Decimal(v)
                for i, v in enumerate(["0.002", "0.001", "0.003", "0.002", "0.001", "0.002"] + ["-0.03"] * 6)}
        worse = {**days}
        entry = open_trial(self.repository.history())
        # 讓套用日落在第 6 天之後：改寫 applied_at 對應的基準期
        history = self.repository.history()
        history[-1] = {**entry, "applied_at": "2026-11-07T10:00:00+08:00"}
        self.repository.history_path.write_text(json.dumps(history), encoding="utf-8")
        result = service.settle_open_trial(self.tmp, SATURDAY, self.repository, worse, clock=lambda: SATURDAY)
        self.assertEqual(result["trial"], "reverted")
        self.assertIsNone(load_strategy_lessons(self.tmp))
        self.assertIsNone(open_trial(self.repository.history()))

    def test_manual_rollback_command_path(self):
        apply_lessons(self.tmp, self.base, lessons(lessons_id="l-1"), self.repository.append_history, 0, SATURDAY)
        service.rollback_lessons(self.tmp, SATURDAY, self.repository)
        self.assertIsNone(load_strategy_lessons(self.tmp))
        with self.assertRaises(ReflectionError):
            service.rollback_lessons(self.tmp, SATURDAY, self.repository)


if __name__ == "__main__":
    unittest.main()
