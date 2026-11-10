"""PipelineBase.agent_step_degradable：驗證用盡與暫時性失敗回傳確定性降級結果，安全拒絕仍拋錯，續跑可重現。

既有 ``agent_step``（預設 2 次、用盡讓整份失敗）的行為由 test_agent_runner.TransientRetryTests 維持，這裡只補對照案例。
"""

import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.agent_runner import AgentTask, DailyPipelineError, TransientAgentError
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.automation.pipeline_base import (
    DEGRADE_MAX_ATTEMPTS, STEP_COMPLETED, STEP_EXHAUSTED, STEP_UNAVAILABLE, AgentStepOutcome,
)

from test_agent_runner import ScriptedRunner

BAD = {"items": ["bad"]}
GOOD = {"items": []}


def validate(artifact):
    return ["items 必須為空"] if artifact["items"] else []


class DegradableStepTests(unittest.TestCase):
    def setUp(self):
        self.task = AgentTask("swap_nomination", "只讀 brief，回覆 JSON", {"type": "object"})
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.sleeps = []

    def tearDown(self):
        self.directory.cleanup()

    def pipeline(self, runner, **kwargs):
        kwargs.setdefault("sleep", self.sleeps.append)
        return DailyDecisionPipeline(self.root, self.root, runner, self.root / "decisions", log=lambda _: None, **kwargs)

    def step(self, pipeline, fingerprint="fp-1", **kwargs):
        return pipeline.agent_step_degradable(self.task, dict, validate, fingerprint, **kwargs)

    def marker(self):
        return json.loads((self.root / "swap_nomination_degraded.json").read_text(encoding="utf-8"))

    def test_default_is_three_attempts_and_exhausted_after_three_invalid_outputs(self):
        self.assertEqual(DEGRADE_MAX_ATTEMPTS, 3)
        runner = ScriptedRunner([BAD, BAD, BAD, GOOD])
        pipeline = self.pipeline(runner, max_attempts=2)  # 不沿用 PipelineBase.max_attempts
        outcome = self.step(pipeline)
        self.assertEqual((outcome.status, outcome.artifact, outcome.attempts), (STEP_EXHAUSTED, None, 3))
        self.assertIn("items 必須為空", outcome.errors[0])
        self.assertEqual(runner.calls, 3)
        for number in (1, 2, 3):
            self.assertTrue((self.root / ("swap_nomination_raw_%d.json" % number)).exists())
        self.assertEqual(self.marker()["status"], STEP_EXHAUSTED)
        self.assertEqual(self.marker()["fingerprint"], "fp-1")
        self.assertEqual(pipeline.calls[1]["validation_errors"], 1)
        self.assertIn("revision_diff", pipeline.calls[1])

    def test_success_on_second_attempt_is_completed_and_leaves_no_marker(self):
        runner = ScriptedRunner([BAD, GOOD])
        outcome = self.step(self.pipeline(runner))
        self.assertEqual((outcome.status, outcome.artifact, outcome.attempts), (STEP_COMPLETED, GOOD, 2))
        self.assertFalse((self.root / "swap_nomination_degraded.json").exists())

    def test_attempt_limit_is_caller_specified(self):
        runner = ScriptedRunner([BAD, BAD, BAD])
        outcome = self.step(self.pipeline(runner), max_attempts=1)
        self.assertEqual((outcome.status, outcome.attempts, runner.calls), (STEP_EXHAUSTED, 1, 1))

    def test_transient_failure_after_retry_is_unavailable(self):
        runner = ScriptedRunner([TransientAgentError("第一次逾時"), TransientAgentError("第二次逾時"), GOOD])
        pipeline = self.pipeline(runner)
        outcome = self.step(pipeline)
        self.assertEqual((outcome.status, outcome.artifact), (STEP_UNAVAILABLE, None))
        self.assertIn("第二次逾時", outcome.errors[0])
        self.assertEqual(runner.calls, 2)
        self.assertEqual(self.marker()["status"], STEP_UNAVAILABLE)
        self.assertEqual([call["transient_retry"] for call in pipeline.calls], [0, 1])

    def test_transient_failure_followed_by_success_is_completed(self):
        runner = ScriptedRunner([TransientAgentError("逾時"), GOOD])
        outcome = self.step(self.pipeline(runner))
        self.assertEqual(outcome.status, STEP_COMPLETED)
        self.assertFalse((self.root / "swap_nomination_degraded.json").exists())

    def test_security_rejection_still_raises_and_leaves_no_marker(self):
        runner = ScriptedRunner([DailyPipelineError("swap_nomination Agent 輸出包含本機秘密設定的值，已拒絕"), GOOD])
        with self.assertRaisesRegex(DailyPipelineError, "秘密") as raised:
            self.step(self.pipeline(runner))
        self.assertNotIsInstance(raised.exception, TransientAgentError)
        self.assertFalse((self.root / "swap_nomination_degraded.json").exists())
        self.assertEqual(runner.calls, 1)

    def test_parallel_abort_is_not_degraded(self):
        pipeline = self.pipeline(ScriptedRunner([TransientAgentError("逾時"), GOOD]))
        pipeline._abort.set()
        with self.assertRaises(TransientAgentError):
            self.step(pipeline)
        self.assertFalse((self.root / "swap_nomination_degraded.json").exists())

    def test_unparseable_output_counts_as_a_validation_failure_not_a_crash(self):
        runner = ScriptedRunner([{"nope": 1}, GOOD])
        pipeline = self.pipeline(runner)
        outcome = pipeline.agent_step_degradable(
            self.task, lambda raw: {"items": raw["items"]}, validate, "fp-1")
        self.assertEqual((outcome.status, outcome.artifact, outcome.attempts), (STEP_COMPLETED, GOOD, 2))
        self.assertIn("輸出無法解析", pipeline.calls[0]["first_validation_errors"][0])

    def test_resume_reproduces_exhausted_without_calling_the_agent(self):
        first = self.step(self.pipeline(ScriptedRunner([BAD, BAD, BAD])))
        runner = ScriptedRunner([GOOD])
        again = self.step(self.pipeline(runner, resume=True))
        self.assertEqual(again, first)
        self.assertEqual(runner.calls, 0)

    def test_resume_reproduces_unavailable_without_calling_the_agent(self):
        first = self.step(self.pipeline(ScriptedRunner([TransientAgentError("a"), TransientAgentError("b")])))
        runner = ScriptedRunner([GOOD])
        again = self.step(self.pipeline(runner, resume=True))
        self.assertEqual((again.status, again.errors), (STEP_UNAVAILABLE, first.errors))
        self.assertEqual(runner.calls, 0)

    def test_resume_with_a_different_fingerprint_ignores_the_marker_and_reruns(self):
        self.step(self.pipeline(ScriptedRunner([BAD, BAD, BAD])), fingerprint="fp-1")
        runner = ScriptedRunner([GOOD])
        outcome = self.step(self.pipeline(runner, resume=True), fingerprint="fp-2")
        self.assertEqual(outcome.status, STEP_COMPLETED)
        self.assertEqual(runner.calls, 1)

    def test_resume_reuses_a_validated_raw_output_before_any_marker(self):
        self.step(self.pipeline(ScriptedRunner([BAD, GOOD])))
        runner = ScriptedRunner([BAD])
        outcome = self.step(self.pipeline(runner, resume=True))
        self.assertEqual((outcome.status, outcome.artifact), (STEP_COMPLETED, GOOD))
        self.assertEqual(runner.calls, 0)

    def test_corrupt_or_foreign_marker_is_ignored(self):
        (self.root / "swap_nomination_degraded.json").write_text('{"status": "exhausted"}', encoding="utf-8")
        runner = ScriptedRunner([GOOD])
        self.assertEqual(self.step(self.pipeline(runner, resume=True)).status, STEP_COMPLETED)
        self.assertEqual(runner.calls, 1)

    def test_marker_is_only_read_on_resume(self):
        self.step(self.pipeline(ScriptedRunner([BAD, BAD, BAD])))
        runner = ScriptedRunner([GOOD])
        self.assertEqual(self.step(self.pipeline(runner)).status, STEP_COMPLETED)
        self.assertEqual(runner.calls, 1)

    def test_argument_validation(self):
        pipeline = self.pipeline(ScriptedRunner([]))
        for bad in (0, 11, True, "3"):
            with self.assertRaises(ValueError):
                self.step(pipeline, max_attempts=bad)
        for bad in ("", None):
            with self.assertRaises(ValueError):
                pipeline.agent_step_degradable(self.task, dict, validate, bad)

    def test_existing_agent_step_still_defaults_to_two_attempts_and_fails_the_run(self):
        runner = ScriptedRunner([BAD, BAD, GOOD])
        pipeline = self.pipeline(runner)
        with self.assertRaisesRegex(DailyPipelineError, "Agent 輸出驗證失敗"):
            pipeline.agent_step(self.task, dict, validate)
        self.assertEqual(runner.calls, 2)
        self.assertFalse((self.root / "swap_nomination_degraded.json").exists())
        self.assertIsInstance(AgentStepOutcome(STEP_COMPLETED), AgentStepOutcome)


if __name__ == "__main__":
    unittest.main()
