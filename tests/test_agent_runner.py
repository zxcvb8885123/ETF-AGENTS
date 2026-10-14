import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from etf_agent.automation.agent_runner import AgentTask, CodexAgentRunner, DailyPipelineError


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


if __name__ == "__main__":
    unittest.main()
