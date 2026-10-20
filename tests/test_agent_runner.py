import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from etf_agent.automation.agent_runner import AgentCall, AgentTask, CodexAgentRunner, DailyPipelineError, OpenCodeAgentRunner
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline


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


if __name__ == "__main__":
    unittest.main()
