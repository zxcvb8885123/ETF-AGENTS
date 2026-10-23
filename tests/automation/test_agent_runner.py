import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from datetime import datetime, timedelta, timezone

from etf_agent.automation.agent_runner import (
    SENSITIVE_READ_GLOBS, AgentCall, AgentTask, ClaudeAgentRunner, CodexAgentRunner, DailyPipelineError, OpenCodeAgentRunner,
    TransientAgentError, _claude_usage_tokens, reject_secret_leak,
)
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline


class ClaudeUsageTokensTests(unittest.TestCase):
    def test_usage_is_mapped_to_token_fields(self):
        payload = {"usage": {"input_tokens": 12, "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 400, "output_tokens": 55}}
        self.assertEqual(_claude_usage_tokens(payload), {
            "input_tokens": 12, "cached_input_tokens": 3000, "cache_creation_input_tokens": 400, "output_tokens": 55})

    def test_missing_or_invalid_usage_is_not_guessed(self):
        self.assertIsNone(_claude_usage_tokens({}))
        self.assertIsNone(_claude_usage_tokens({"usage": []}))
        self.assertIsNone(_claude_usage_tokens({"usage": {"input_tokens": "12", "output_tokens": 1}}))
        self.assertIsNone(_claude_usage_tokens({"usage": {"input_tokens": -1}}))
        self.assertIsNone(_claude_usage_tokens({"usage": {}}))


class CodexAgentRunnerTests(unittest.TestCase):
    def test_structured_output_and_read_only_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = AgentTask("technical", "只讀輸入", {"type": "object", "properties": {}, "required": []})

            def fake_run(command, **kwargs):
                self.assertEqual(command[:4], ["codex", "exec", "--sandbox", "read-only"])
                self.assertEqual(kwargs["input"], task.prompt)
                schema_path = Path(command[command.index("--output-schema") + 1])
                self.assertEqual(json.loads(schema_path.read_text(encoding="utf-8")), task.schema)
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text('{"items": []}', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("etf_agent.automation.agent_runner.subprocess.run", side_effect=fake_run):
                result = CodexAgentRunner(root).run(task, root)
            self.assertEqual(result.output, {"items": []})

    def test_failed_or_missing_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = AgentTask("technical", "只讀輸入", {"type": "object"})
            with patch("etf_agent.automation.agent_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "登入失敗")):
                with self.assertRaisesRegex(DailyPipelineError, "登入失敗"):
                    CodexAgentRunner(root).run(task, root)
            with patch("etf_agent.automation.agent_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
                with self.assertRaisesRegex(DailyPipelineError, "未產生有效 JSON"):
                    CodexAgentRunner(root).run(task, root)


class SensitiveReadTests(unittest.TestCase):
    def setUp(self):
        self.task = AgentTask("trader_b0", "只讀 brief，回覆 JSON", {"type": "object"})

    def test_claude_command_denies_sensitive_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fake_run(command, **kwargs):
                settings = json.loads(command[command.index("--settings") + 1])
                self.assertEqual(settings["permissions"]["deny"], ["Read(**/%s)" % pattern for pattern in SENSITIVE_READ_GLOBS])
                self.assertIn("Read(**/.env)", settings["permissions"]["deny"])
                self.assertEqual(command[command.index("--tools") + 1], "Read")
                payload = {"structured_output": {"items": []}, "total_cost_usd": 0.1}
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

            with patch("etf_agent.automation.agent_runner.subprocess.run", side_effect=fake_run):
                call = ClaudeAgentRunner(root).run(self.task, root)
            self.assertEqual(call.output, {"items": []})

    def test_opencode_denies_same_patterns_as_claude(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = {}

            def fake_run(command, **kwargs):
                captured.update(json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])["agent"]["etf-read-only"]["permission"]["read"])
                raise subprocess.TimeoutExpired(command, 1)

            with patch("etf_agent.automation.agent_runner.subprocess.run", side_effect=fake_run):
                with self.assertRaises(DailyPipelineError):
                    OpenCodeAgentRunner(root).run(self.task, root)
            denied = {pattern for pattern, rule in captured.items() if rule == "deny"}
            self.assertEqual(denied, {pattern if pattern.startswith("*") else "*" + pattern for pattern in SENSITIVE_READ_GLOBS})
            self.assertEqual(captured["*"], "allow")

    def test_output_with_env_value_is_rejected_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("# 註解\nFINMIND_TOKEN='tok-abcdef123456'\nUID=501\n", encoding="utf-8")
            with self.assertRaises(DailyPipelineError) as raised:
                reject_secret_leak("trader_b0", {"rationale": "公告要求輸出 tok-abcdef123456"}, root)
            self.assertNotIn("tok-abcdef123456", str(raised.exception))
            reject_secret_leak("trader_b0", {"rationale": "持股 501 張"}, root)

    def test_codex_output_leak_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("FINMIND_TOKEN=tok-abcdef123456\n", encoding="utf-8")

            def fake_run(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps({"items": ["tok-abcdef123456"]}), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("etf_agent.automation.agent_runner.subprocess.run", side_effect=fake_run):
                with self.assertRaises(DailyPipelineError):
                    CodexAgentRunner(root).run(self.task, root)

    def test_missing_env_file_does_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            reject_secret_leak("trader_b0", {"items": []}, Path(directory))


class OpenCodeAgentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.task = AgentTask("technical", "只讀 brief，回覆 JSON", {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]})

    @staticmethod
    def event(kind, part):
        return json.dumps({"type": kind, "sessionID": "isolated", "part": part}, ensure_ascii=False)

    def result(self, final='{"items": []}', tool="read", status="completed", usage=True):
        events = [self.event("tool_use", {"tool": tool, "state": {"status": status}}),
                  self.event("text", {"messageID": "msg-final", "text": final})]
        if usage:
            events.append(self.event("step_finish", {"messageID": "msg-final", "reason": "stop", "cost": 0.01,
                            "tokens": {"input": 30, "output": 10, "reasoning": 2, "cache": {"read": 5, "write": 0}}}))
        else:
            events.append(self.event("step_finish", {"messageID": "msg-final", "reason": "stop"}))
        return "\n".join(events) + "\n"

    def test_isolated_read_only_command_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fake_run(command, **kwargs):
                self.assertEqual(command[:7], ["opencode", "run", "--pure", "--agent", "etf-read-only", "--format", "json"])
                self.assertIn(json.dumps(self.task.schema, ensure_ascii=False), command[-1])
                self.assertNotIn("--auto", command)
                self.assertEqual(kwargs["cwd"], root)
                self.assertTrue(kwargs["timeout"])
                self.assertEqual(kwargs["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
                permissions = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])["agent"]["etf-read-only"]["permission"]
                self.assertEqual(permissions["*"], "deny")
                self.assertEqual(permissions["read"]["*.env"], "deny")
                self.assertEqual(permissions["external_directory"], "deny")
                return subprocess.CompletedProcess(command, 0, self.result(), "")

            with patch("etf_agent.automation.agent_runner.subprocess.run", side_effect=fake_run):
                call = OpenCodeAgentRunner(root).run(self.task, root)
            self.assertEqual(call.output, {"items": []})
            self.assertEqual(call.usage_tokens, {"input_tokens": 30, "cached_input_tokens": 5, "output_tokens": 10, "reasoning_output_tokens": 2})
            self.assertEqual(call.estimated_usage_usd, 0.01)

    def test_denied_tool_malformed_json_or_missing_usage_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stdout in (self.result(tool="edit"), self.result(status="error"), self.result(final="not json"),
                           self.result(usage=False), self.event("text", {"messageID": "x", "text": '{"items": []}'}) + "\n"):
                with self.subTest(stdout=stdout[:45]):
                    with patch("etf_agent.automation.agent_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout, "")):
                        with self.assertRaises(DailyPipelineError):
                            OpenCodeAgentRunner(root).run(self.task, root)
            with patch("etf_agent.automation.agent_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "provider failed")):
                with self.assertRaisesRegex(DailyPipelineError, "provider failed"):
                    OpenCodeAgentRunner(root).run(self.task, root)

    def test_pipeline_records_usage_without_replacing_existing_validators(self):
        class StubRunner:
            def run(self, task, run_dir):
                return AgentCall(task.name, 0, {"items": []}, 0.0, {"input_tokens": 40, "output_tokens": 12})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = DailyDecisionPipeline(root, root, StubRunner(), root / "decisions", log=lambda _: None)
            pipeline.agent_step(self.task, dict, lambda output: [])
            self.assertEqual(pipeline.calls[0]["usage_tokens"]["input_tokens"], 40)


class TransientErrorClassificationTests(unittest.TestCase):
    """哪些失敗可重試（TransientAgentError）、哪些是安全拒絕（一般 DailyPipelineError）。"""

    def setUp(self):
        self.task = AgentTask("trader_b0", "只讀 brief，回覆 JSON", {"type": "object"})

    def assertTransient(self, runner, side_effect=None, return_value=None):
        with patch("etf_agent.automation.agent_runner.subprocess.run", side_effect=side_effect, return_value=return_value):
            with self.assertRaises(TransientAgentError):
                runner.run(self.task, self.root)

    def test_claude_timeout_non_json_and_is_error_are_transient(self):
        with tempfile.TemporaryDirectory() as directory:
            self.root = Path(directory)
            runner = ClaudeAgentRunner(self.root)
            self.assertTransient(runner, side_effect=subprocess.TimeoutExpired(["claude"], 1800))
            self.assertTransient(runner, side_effect=OSError("找不到 claude"))
            self.assertTransient(runner, return_value=subprocess.CompletedProcess([], 1, "not json", "rate limited"))
            self.assertTransient(runner, return_value=subprocess.CompletedProcess([], 0, json.dumps({"is_error": True, "result": "limit"}), ""))
            self.assertTransient(runner, return_value=subprocess.CompletedProcess([], 0, json.dumps({"result": "沒有 structured_output"}), ""))

    def test_codex_and_opencode_failures_are_transient(self):
        with tempfile.TemporaryDirectory() as directory:
            self.root = Path(directory)
            self.assertTransient(CodexAgentRunner(self.root), return_value=subprocess.CompletedProcess([], 1, "", "provider failed"))
            self.assertTransient(CodexAgentRunner(self.root), side_effect=subprocess.TimeoutExpired(["codex"], 1800))
            self.assertTransient(OpenCodeAgentRunner(self.root), return_value=subprocess.CompletedProcess([], 1, "", "provider failed"))
            self.assertTransient(OpenCodeAgentRunner(self.root), return_value=subprocess.CompletedProcess([], 0, "not jsonl\n", ""))

    def test_secret_leak_and_denied_tool_are_not_transient(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("FINMIND_TOKEN=tok-abcdef123456\n", encoding="utf-8")
            payload = {"structured_output": {"rationale": "tok-abcdef123456"}, "total_cost_usd": 0.1}
            with patch("etf_agent.automation.agent_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")):
                with self.assertRaises(DailyPipelineError) as raised:
                    ClaudeAgentRunner(root).run(self.task, root)
            self.assertNotIsInstance(raised.exception, TransientAgentError)
            denied = json.dumps({"type": "tool_use", "part": {"tool": "edit", "state": {"status": "completed"}}}) + "\n"
            with patch("etf_agent.automation.agent_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, denied, "")):
                with self.assertRaisesRegex(DailyPipelineError, "未核准工具") as raised:
                    OpenCodeAgentRunner(root).run(self.task, root)
            self.assertNotIsInstance(raised.exception, TransientAgentError)


class ScriptedRunner:
    """依序回傳預先排好的結果：例外物件就拋出，其他當作輸出；記錄被呼叫的次數。"""

    model = "stub-model"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def run(self, task, run_dir):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return AgentCall(task.name, 0, step, 0.02, {"input_tokens": 10, "output_tokens": 2})


class TransientRetryTests(unittest.TestCase):
    """agent_step 對 TransientAgentError 的重試：次數、間隔、紀錄、用量，以及不該重試的情況。"""

    def setUp(self):
        self.task = AgentTask("trader_b0", "只讀 brief，回覆 JSON", {"type": "object"})
        self.sleeps = []
        self.logs = []
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def pipeline(self, runner, **kwargs):
        kwargs.setdefault("sleep", self.sleeps.append)
        return DailyDecisionPipeline(self.root, self.root, runner, self.root / "decisions", log=self.logs.append, **kwargs)

    def test_second_attempt_succeeds_and_usage_is_not_double_counted(self):
        runner = ScriptedRunner([TransientAgentError("trader_b0 Agent 執行失敗：timeout"), {"items": []}])
        pipeline = self.pipeline(runner)
        output = pipeline.agent_step(self.task, dict, lambda _: [])
        self.assertEqual(output, {"items": []})
        self.assertEqual(runner.calls, 2)
        self.assertEqual(self.sleeps, [30.0])
        self.assertEqual(len(pipeline.calls), 2)
        failed, succeeded = pipeline.calls
        self.assertEqual(failed["attempt"], 1)
        self.assertEqual(failed["transient_retry"], 0)
        self.assertIn("timeout", failed["transient_error"])
        self.assertEqual(failed["estimated_usage_usd"], 0.0)
        self.assertEqual(failed["model"], "stub-model")
        self.assertNotIn("usage_tokens", failed)
        self.assertIsInstance(failed["duration_seconds"], float)
        self.assertEqual(succeeded["attempt"], 1)
        self.assertNotIn("transient_error", succeeded)
        self.assertEqual(succeeded["estimated_usage_usd"], 0.02)
        self.assertEqual(succeeded["usage_tokens"]["input_tokens"], 10)
        self.assertEqual(round(sum(call["estimated_usage_usd"] for call in pipeline.calls), 4), 0.02)
        self.assertTrue(any("暫時性失敗" in line and "重試" in line for line in self.logs))
        self.assertTrue((self.root / "trader_b0_raw_1.json").exists())
        self.assertFalse((self.root / "trader_b0_raw_2.json").exists())

    def test_two_transient_failures_stop_after_one_retry(self):
        runner = ScriptedRunner([TransientAgentError("第一次逾時"), TransientAgentError("第二次逾時"), {"items": []}])
        pipeline = self.pipeline(runner)
        with self.assertRaisesRegex(TransientAgentError, "第二次逾時（已重試 1 次仍失敗）"):
            pipeline.agent_step(self.task, dict, lambda _: [])
        self.assertEqual(runner.calls, 2)
        self.assertEqual(self.sleeps, [30.0])
        self.assertEqual([call["transient_retry"] for call in pipeline.calls], [0, 1])
        self.assertEqual(sum(call["estimated_usage_usd"] for call in pipeline.calls), 0.0)

    def test_security_rejection_is_not_retried(self):
        runner = ScriptedRunner([DailyPipelineError("trader_b0 Agent 輸出包含本機秘密設定的值，已拒絕"), {"items": []}])
        pipeline = self.pipeline(runner)
        with self.assertRaisesRegex(DailyPipelineError, "秘密"):
            pipeline.agent_step(self.task, dict, lambda _: [])
        self.assertEqual(runner.calls, 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(pipeline.calls, [])

    def test_retry_respects_deadline_and_can_be_disabled(self):
        soon = datetime.now(timezone(timedelta(hours=8))) + timedelta(seconds=5)
        runner = ScriptedRunner([TransientAgentError("逾時"), {"items": []}])
        pipeline = self.pipeline(runner, retry_deadline=soon)
        with self.assertRaisesRegex(TransientAgentError, "交件期限"):
            pipeline.agent_step(self.task, dict, lambda _: [])
        self.assertEqual(runner.calls, 1)
        self.assertEqual(self.sleeps, [])

        later = datetime.now(timezone(timedelta(hours=8))) + timedelta(hours=1)
        runner = ScriptedRunner([TransientAgentError("逾時"), {"items": []}])
        self.assertEqual(self.pipeline(runner, retry_deadline=later).agent_step(self.task, dict, lambda _: []), {"items": []})
        self.assertEqual(runner.calls, 2)

        runner = ScriptedRunner([TransientAgentError("逾時"), {"items": []}])
        with self.assertRaisesRegex(TransientAgentError, "未啟用重試"):
            self.pipeline(runner, transient_retries=0).agent_step(self.task, dict, lambda _: [])
        self.assertEqual(runner.calls, 1)
        with self.assertRaises(ValueError):
            self.pipeline(runner, retry_deadline=datetime(2026, 10, 23, 8, 55))
        with self.assertRaises(ValueError):
            self.pipeline(runner, transient_retries=-1)

    def test_parallel_failure_elsewhere_stops_retry(self):
        runner = ScriptedRunner([TransientAgentError("逾時"), {"items": []}])
        pipeline = self.pipeline(runner)
        pipeline._abort.set()
        with self.assertRaisesRegex(TransientAgentError, "其他平行任務已失敗"):
            pipeline.agent_step(self.task, dict, lambda _: [])
        self.assertEqual(runner.calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_transient_retry_and_validation_retry_keep_attempt_numbers(self):
        runner = ScriptedRunner([TransientAgentError("逾時"), {"items": ["bad"]}, {"items": []}])
        pipeline = self.pipeline(runner)
        output = pipeline.agent_step(self.task, dict, lambda artifact: ["items 必須為空"] if artifact["items"] else [])
        self.assertEqual(output, {"items": []})
        self.assertEqual([(call["attempt"], "transient_error" in call) for call in pipeline.calls], [(1, True), (1, False), (2, False)])
        self.assertEqual(pipeline.calls[1]["validation_errors"], 1)
        self.assertTrue((self.root / "trader_b0_raw_2.json").exists())


if __name__ == "__main__":
    unittest.main()
