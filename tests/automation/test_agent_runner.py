import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from datetime import datetime, timedelta, timezone

from etf_agent.automation.agent_runner import (
    SENSITIVE_READ_GLOBS, AgentCall, AgentTask, ClaudeAgentRunner, CodexAgentRunner, DailyPipelineError, OpenCodeAgentRunner,
    TransientAgentError, _claude_usage_tokens, _trace_summary, _opencode_read_permission, claude_read_deny_settings, reject_secret_leak,
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

    def test_task_with_deny_reads_is_refused_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = AgentTask("bull_b0", "只讀 brief", {"type": "object"}, deny_reads=("bear_*",))
            with patch("etf_agent.automation.agent_runner.subprocess.run", side_effect=AssertionError("不得啟動")):
                with self.assertRaises(DailyPipelineError) as raised:
                    CodexAgentRunner(root).run(task, root)
            self.assertNotIsInstance(raised.exception, TransientAgentError)
            self.assertIn("拒讀", str(raised.exception))

    def test_stage_isolated_deny_reads_task_runs_ephemeral_outside_run_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            task = AgentTask("bull_b0", "只讀 brief", {"type": "object"}, deny_reads=("bear_*",), stage_isolated=True)
            seen = {}

            def fake_run(command, **kwargs):
                self.assertIn("--ephemeral", command)
                # 隔離任務的輸出只走標準輸出，不得有任何會落地的輸出檔（同儕並行時可能被讀到）。
                self.assertNotIn("--output-last-message", command)
                self.assertIn("--json", command)
                path = Path(command[command.index("--output-schema") + 1])
                self.assertNotIn(run_dir, path.parents)  # 暫存檔不得放在 run 目錄
                seen["schema"] = path
                events = [
                    {"type": "thread.started", "thread_id": "t"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": '{"items": [1]}'}},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": '{"items": []}'}},
                    {"type": "turn.completed"},
                ]
                return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(event) for event in events), "")

            with patch("etf_agent.automation.agent_runner.subprocess.run", side_effect=fake_run):
                result = CodexAgentRunner(root).run(task, run_dir)
            self.assertEqual(result.output, {"items": []})
            self.assertEqual(list(run_dir.iterdir()), [])
            self.assertFalse(seen["schema"].exists())

    def test_stage_isolated_task_without_agent_message_is_transient_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = AgentTask("bull_b0", "只讀 brief", {"type": "object"}, deny_reads=("bear_*",), stage_isolated=True)
            done = subprocess.CompletedProcess([], 0, '{"type": "turn.completed"}\n', "")
            with patch("etf_agent.automation.agent_runner.subprocess.run", return_value=done):
                with self.assertRaises(TransientAgentError):
                    CodexAgentRunner(root).run(task, root)

    def test_task_without_deny_reads_is_not_ephemeral(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fake_run(command, **kwargs):
                self.assertNotIn("--ephemeral", command)
                Path(command[command.index("--output-last-message") + 1]).write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("etf_agent.automation.agent_runner.subprocess.run", side_effect=fake_run):
                CodexAgentRunner(root).run(AgentTask("technical", "x", {"type": "object"}), root)

    def test_runners_declare_whether_they_enforce_deny_reads(self):
        self.assertFalse(CodexAgentRunner.enforces_deny_reads)
        self.assertTrue(ClaudeAgentRunner.enforces_deny_reads)
        self.assertTrue(OpenCodeAgentRunner.enforces_deny_reads)

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

    def test_env_template_files_are_not_treated_as_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.example").write_text("FINMIND_TOKEN=your_token_here\n", encoding="utf-8")
            reject_secret_leak("trader", {"note": "your_token_here"}, root)
            (root / ".env").write_text("FINMIND_TOKEN=real_secret_value\n", encoding="utf-8")
            with self.assertRaises(DailyPipelineError):
                reject_secret_leak("trader", {"note": "real_secret_value"}, root)

    def test_claude_command_denies_sensitive_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fake_run(command, *args, **kwargs):
                settings = json.loads(command[command.index("--settings") + 1])
                self.assertEqual(
                    settings["permissions"]["deny"],
                    ["%s(**/%s)" % (tool, pattern) for tool in ("Read", "Grep", "Glob") for pattern in SENSITIVE_READ_GLOBS],
                )
                self.assertIn("Read(**/.env)", settings["permissions"]["deny"])
                self.assertEqual(command[command.index("--tools") + 1], "Read")
                disallowed = command[command.index("--disallowedTools") + 1].split(",")
                for tool in ("Grep", "Glob", "Bash", "Write", "Edit", "WebFetch", "WebSearch", "Task", "NotebookEdit"):
                    self.assertIn(tool, disallowed)
                self.assertNotIn("Read", disallowed)
                payload = {"structured_output": {"items": []}, "total_cost_usd": 0.1}
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

            with patch("etf_agent.automation.agent_runner._run_claude_streaming", side_effect=fake_run):
                call = ClaudeAgentRunner(root).run(self.task, root)
            self.assertEqual(call.output, {"items": []})

    def test_credential_style_files_and_case_variants_are_denied(self):
        for pattern in (".netrc", "*.p12", "*.pfx", "id_rsa*", "*.token", ".npmrc", ".pypirc", "*.jks"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, SENSITIVE_READ_GLOBS)
        # macOS 檔案系統不分大小寫，樣式比對可能分：大小寫變體都要在清單裡。
        for pattern in ("*SECRET*", "*Secret*", "*CREDENTIAL*", "*Credential*", ".ENV", "*.PEM", "ID_RSA*"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, SENSITIVE_READ_GLOBS)
        self.assertEqual(len(SENSITIVE_READ_GLOBS), len(set(SENSITIVE_READ_GLOBS)))
        rules = json.loads(claude_read_deny_settings())["permissions"]["deny"]
        self.assertIn("Read(**/.netrc)", rules)
        self.assertIn("Read(**/*SECRET*)", rules)
        opencode = _opencode_read_permission()
        self.assertEqual(opencode["*id_rsa*"], "deny")
        self.assertEqual(opencode["*.JKS"], "deny")

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

    def test_env_values_with_trailing_comments_are_still_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "TOKEN_A=abcdefgh12345678 # 行尾註解\nTOKEN_B='quoted-abcdef1234' # 註解\n", encoding="utf-8")
            for leaked in ("abcdefgh12345678", "quoted-abcdef1234"):
                with self.assertRaises(DailyPipelineError):
                    reject_secret_leak("trader_b0", {"rationale": "洩漏 " + leaked}, root)

    def test_env_variants_are_scanned_for_leaks(self):
        from etf_agent.automation.agent_runner import scrub_secrets

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.local").write_text("TOKEN_A=loc-abcdef123456\n", encoding="utf-8")
            (root / ".env.production").write_text("TOKEN_P=prod-abcdef123456\n", encoding="utf-8")
            (root / ".envrc").write_text("export TOKEN_B=rc-abcdef123456\n", encoding="utf-8")
            (root / ".env.d").mkdir()
            for leaked in ("loc-abcdef123456", "prod-abcdef123456", "rc-abcdef123456"):
                with self.assertRaises(DailyPipelineError) as raised:
                    reject_secret_leak("trader_b0", {"rationale": "洩漏 " + leaked}, root)
                self.assertNotIn(leaked, str(raised.exception))
            self.assertNotIn("loc-abcdef123456", scrub_secrets("錯誤 loc-abcdef123456", root))

    def test_short_values_and_comments_in_env_variants_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.local").write_text("# TOKEN=comment-abcdef123456\nPORT=8080\n", encoding="utf-8")
            reject_secret_leak("trader_b0", {"rationale": "comment-abcdef123456 與 8080"}, root)

    def test_output_with_escaped_characters_in_secret_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text('API_TOKEN=tok\\abc"def-123456\n', encoding="utf-8")
            with self.assertRaises(DailyPipelineError):
                reject_secret_leak("trader_b0", {"rationale": 'x tok\\abc"def-123456 y'}, root)

    def test_task_deny_reads_reach_claude_and_opencode_permissions(self):
        import json as _json
        from etf_agent.automation.agent_runner import _opencode_read_permission, claude_read_deny_settings

        rules = _json.loads(claude_read_deny_settings(("bear_*",)))["permissions"]["deny"]
        self.assertIn("Read(**/bear_*)", rules)
        self.assertIn("Read(**/.env)", rules)
        self.assertEqual(_opencode_read_permission(("bear_*",))["*/bear_*"], "deny")
        anchored = _opencode_read_permission(("bear_*",), Path("artifacts/daily/run1"))
        self.assertEqual(anchored["*/run1/bear_*"], "deny")
        self.assertNotIn("*/bear_*", anchored)

    def test_bull_and_bear_tasks_cannot_read_each_others_output(self):
        from etf_agent.automation.team_stages import _peer_deny_reads

        self.assertIn("bear_*", _peer_deny_reads("bull"))
        self.assertIn("brief_bull_*", _peer_deny_reads("bear"))
        self.assertIn("research_debate*", _peer_deny_reads("bull"))
        for pattern in ("debate*", "intent*", "trade_decision*", "trader_*", "brief_trader_*", "team_inputs*"):
            self.assertIn(pattern, _peer_deny_reads("bull"))
            self.assertIn(pattern, _peer_deny_reads("bear"))
        self.assertNotIn("decision*", _peer_deny_reads("bull"))

    def test_later_stage_artifacts_are_denied_to_researchers(self):
        import fnmatch
        from etf_agent.automation.agent_runner import LATER_STAGE_GLOBS
        from etf_agent.automation.team_stages import _peer_deny_reads

        for role in ("bull", "bear"):
            for pattern in ("brief_swap_nomination*", "brief_selection_ranking*", "ranker_*"):
                self.assertIn(pattern, _peer_deny_reads(role))
        rules = json.loads(claude_read_deny_settings(_peer_deny_reads("bull")))["permissions"]["deny"]
        self.assertIn("Read(**/brief_swap_nomination*)", rules)
        later_names = (
            "research_debate", "debate", "intent", "trade_decision", "trader_b0_raw_1", "brief_trader_b0", "team_inputs",
            "selection_ranking", "brief_selection_ranking", "ranker_raw_1", "swap_nomination", "brief_swap_nomination",
            "swap_nominator_raw_1", "release_order", "brief_release_order", "quality_dip_ruling",
            "brief_quality_dip_ruling",
        )
        for name in later_names:
            with self.subTest(name=name):
                self.assertTrue(any(fnmatch.fnmatch(name + ".json", pattern) for pattern in LATER_STAGE_GLOBS), name)

    def test_research_stage_inputs_are_not_residue_globs(self):
        import fnmatch
        from etf_agent.automation.agent_runner import LATER_STAGE_GLOBS

        for name in ("decision_input.json", "brief_bull_b0.json", "brief_bear_b0.json", "pipeline.json"):
            with self.subTest(name=name):
                self.assertFalse(any(fnmatch.fnmatch(name, pattern) for pattern in LATER_STAGE_GLOBS), name)

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

    def test_claude_failure_text_is_scrubbed_of_env_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("FINMIND_TOKEN=tok-abcdef123456\n", encoding="utf-8")
            failures = [
                subprocess.CompletedProcess([], 1, "not json", "bad key tok-abcdef123456"),
                subprocess.CompletedProcess([], 1, json.dumps({"is_error": True, "result": "bad tok-abcdef123456"}), ""),
            ]
            for completed in failures:
                with patch("etf_agent.automation.agent_runner._run_claude_streaming", return_value=completed):
                    with self.assertRaises(TransientAgentError) as raised:
                        ClaudeAgentRunner(root).run(self.task, root)
                self.assertNotIn("tok-abcdef123456", str(raised.exception))

    def test_claude_nonzero_exit_with_structured_output_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = json.dumps({"structured_output": {"items": []}})
            with patch("etf_agent.automation.agent_runner._run_claude_streaming",
                       return_value=subprocess.CompletedProcess([], 1, payload, "")):
                with self.assertRaises(TransientAgentError):
                    ClaudeAgentRunner(root).run(self.task, root)

    def test_claude_uses_stream_json_and_streaming_result_is_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake_claude.py"
            script.write_text(
                "import json,sys\n"
                "print(json.dumps({'type':'system','subtype':'init'}))\n"
                "print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read','input':{'file_path':'/x/brief.json'}}]}}))\n"
                "print(json.dumps({'type':'rate_limit_event','rate_limit_info':{'status':'allowed'}}))\n"
                "print('not json')\n"
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':{'items':[]},'total_cost_usd':0.2}))\n",
                encoding="utf-8",
            )
            seen = {}
            original = subprocess.Popen

            def fake_popen(command, **kwargs):
                seen["command"] = command
                return original([sys.executable, str(script)], **kwargs)

            run_dir = root / "run-1"
            run_dir.mkdir()
            with patch("etf_agent.automation.agent_runner.subprocess.Popen", side_effect=fake_popen):
                call = ClaudeAgentRunner(root).run(self.task, run_dir)
            self.assertEqual(call.output, {"items": []})
            self.assertEqual(call.estimated_usage_usd, 0.2)
            self.assertEqual(seen["command"][seen["command"].index("--output-format") + 1], "stream-json")
            self.assertIn("--verbose", seen["command"])
            trace = (root / "var" / "logs" / "agent_trace" / "run-1" / "trader_b0.jsonl").read_text(encoding="utf-8")
            rows = [json.loads(line) for line in trace.splitlines()]
            self.assertEqual([row["type"] for row in rows], ["system", "assistant", "rate_limit_event", "result"])
            self.assertEqual(rows[1]["tools"], [{"name": "Read", "file": "/x/brief.json", "path": None}])
            self.assertEqual(rows[2]["rate_limit_info"], {"status": "allowed"})
            self.assertEqual(list(run_dir.iterdir()), [])

    def test_claude_stream_without_result_event_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake_claude.py"
            script.write_text("import json\nprint(json.dumps({'type':'system','subtype':'init'}))\n", encoding="utf-8")
            original = subprocess.Popen
            with patch("etf_agent.automation.agent_runner.subprocess.Popen",
                       side_effect=lambda command, **kwargs: original([sys.executable, str(script)], **kwargs)):
                with self.assertRaises(TransientAgentError):
                    ClaudeAgentRunner(root).run(self.task, root)

    def test_claude_stream_timeout_kills_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake_claude.py"
            script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            original = subprocess.Popen
            with patch("etf_agent.automation.agent_runner.subprocess.Popen",
                       side_effect=lambda command, **kwargs: original([sys.executable, str(script)], **kwargs)):
                with self.assertRaises(TransientAgentError):
                    ClaudeAgentRunner(root, timeout_seconds=1).run(self.task, root)

    def test_trace_does_not_store_model_text_or_secrets(self):
        row = _trace_summary({"type": "assistant", "message": {"content": [{"type": "text", "text": "機密內文"}]}}, 1.234)
        self.assertEqual(row, {"t": 1.2, "type": "assistant"})

    def test_trace_records_non_read_tool_path_but_not_pattern(self):
        event = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Grep", "input": {"path": "/proj/artifacts", "pattern": "bear-2330-1"}},
        ]}}
        row = _trace_summary(event, 0.5)
        self.assertEqual(row["tools"], [{"name": "Grep", "file": None, "path": "/proj/artifacts"}])
        self.assertNotIn("bear-2330-1", json.dumps(row))
        bad_path = _trace_summary({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Glob", "input": {"path": ["x"]}}]}}, 0.5)
        self.assertIsNone(bad_path["tools"][0]["path"])

    def test_trace_counts_tool_errors_without_content(self):
        event = {"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "denied: secret text"},
            {"type": "tool_result", "is_error": False, "content": "ok"},
        ]}}
        row = _trace_summary(event, 1.0)
        self.assertEqual(row["tool_errors"], 1)
        self.assertNotIn("secret text", json.dumps(row))
        clean = _trace_summary({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "ok"}]}}, 1.0)
        self.assertNotIn("tool_errors", clean)

    def test_deny_settings_cover_read_grep_glob_for_task_and_sensitive_patterns(self):
        rules = json.loads(claude_read_deny_settings(("bear_*",)))["permissions"]["deny"]
        for tool in ("Read", "Grep", "Glob"):
            self.assertIn("%s(**/bear_*)" % tool, rules)
            self.assertIn("%s(**/.env)" % tool, rules)

    def test_directory_deny_rules_reach_claude_and_opencode(self):
        rules = json.loads(claude_read_deny_settings((), ("artifacts/reports",)))["permissions"]["deny"]
        for tool in ("Read", "Grep", "Glob"):
            self.assertIn("%s(**/artifacts/reports/**)" % tool, rules)
        self.assertEqual(json.loads(claude_read_deny_settings())["permissions"]["deny"],
                         json.loads(claude_read_deny_settings((), ()))["permissions"]["deny"])
        permission = _opencode_read_permission((), Path("artifacts/daily/run1"), ("artifacts/reports",))
        self.assertEqual(permission["*/artifacts/reports/*"], "deny")
        anchored = _opencode_read_permission(("bear_*",), Path("artifacts/daily/run1"), ("var/logs",))
        self.assertEqual(anchored["*/run1/bear_*"], "deny")
        self.assertEqual(anchored["*/var/logs/*"], "deny")

    def test_research_deny_directories_constant_and_claude_command(self):
        from etf_agent.automation.agent_runner import RESEARCH_DENY_DIRECTORIES

        self.assertIn("artifacts/reports", RESEARCH_DENY_DIRECTORIES)
        self.assertIn("var/strategy_reflection", RESEARCH_DENY_DIRECTORIES)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fake_run(command, *args, **kwargs):
                deny = json.loads(command[command.index("--settings") + 1])["permissions"]["deny"]
                self.assertIn("Read(**/artifacts/archive/**)", deny)
                payload = {"structured_output": {"items": []}, "total_cost_usd": 0.1}
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

            task = AgentTask("bull_b0", "x", {"type": "object"}, deny_read_directories=RESEARCH_DENY_DIRECTORIES)
            with patch("etf_agent.automation.agent_runner._run_claude_streaming", side_effect=fake_run):
                self.assertEqual(ClaudeAgentRunner(root).run(task, root).output, {"items": []})

    def test_codex_refuses_directory_only_deny_task_before_launch(self):
        task = AgentTask("bull_b0", "x", {"type": "object"}, deny_read_directories=("artifacts/reports",))
        with tempfile.TemporaryDirectory() as directory:
            with patch("etf_agent.automation.agent_runner.subprocess.run") as run:
                with self.assertRaises(DailyPipelineError):
                    CodexAgentRunner(Path(directory)).run(task, Path(directory))
                run.assert_not_called()

    def test_pipeline_json_is_denied_to_researchers_but_not_residue_glob(self):
        import fnmatch
        from etf_agent.automation.agent_runner import LATER_STAGE_GLOBS
        from etf_agent.automation.team_stages import _peer_deny_reads

        for role in ("bull", "bear"):
            self.assertIn("pipeline.json", _peer_deny_reads(role))
        self.assertFalse(any(fnmatch.fnmatch("pipeline.json", pattern) for pattern in LATER_STAGE_GLOBS))

    def test_prior_run_deny_globs_are_separate_from_residue_detection(self):
        import fnmatch
        from etf_agent.automation.agent_runner import LATER_STAGE_GLOBS, RESEARCH_PRIOR_RUN_DENY_GLOBS, claude_read_deny_settings
        from etf_agent.automation.pipeline_base import RESEARCH_RESIDUE_GLOBS

        for name in ("decision.json", "proposal_r0.json", "policy.json", "prior_decision_context.json", "position_review.json",
                     "revision_history.json", "risk_review.json", "cash_stance.json"):
            self.assertTrue(any(fnmatch.fnmatch(name, pattern) for pattern in RESEARCH_PRIOR_RUN_DENY_GLOBS), name)
            # 本次 run 在研究階段前就有這些檔：進殘留偵測會讓每次續跑被判殘留。
            self.assertFalse(any(fnmatch.fnmatch(name, pattern) for pattern in RESEARCH_RESIDUE_GLOBS), name)
        self.assertEqual(set(RESEARCH_PRIOR_RUN_DENY_GLOBS) & set(LATER_STAGE_GLOBS), set())
        # 研究員自己要讀的檔不得被擋。
        for name in ("brief_bull_b0.json", "brief_bear_b1.json", "SKILL.md", "data-rules.md", "decision_input.json"):
            self.assertFalse(any(fnmatch.fnmatch(name, pattern) for pattern in RESEARCH_PRIOR_RUN_DENY_GLOBS), name)
        rules = json.loads(claude_read_deny_settings(RESEARCH_PRIOR_RUN_DENY_GLOBS))["permissions"]["deny"]
        self.assertIn("Read(**/decision.json)", rules)

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

    def test_tool_event_with_unknown_type_name_is_still_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 事件型別名稱不是 tool_use，但 part 帶 tool 欄位：非 read 工具仍是安全拒絕（不重試）。
            for kind, part in (("tool", {"tool": "bash", "state": {"status": "completed"}}),
                               ("step_tool", {"type": "tool", "tool": "edit", "state": {"status": "completed"}})):
                with self.subTest(kind=kind):
                    stdout = self.event(kind, part) + "\n" + self.result()
                    with patch("etf_agent.automation.agent_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout, "")):
                        with self.assertRaisesRegex(DailyPipelineError, "未核准工具") as raised:
                            OpenCodeAgentRunner(root).run(self.task, root)
                    self.assertNotIsInstance(raised.exception, TransientAgentError)
            # read 工具但狀態不是 completed、或缺 state，仍視為讀取失敗；正常的 read 事件照常通過。
            bad = self.event("tool", {"tool": "read"}) + "\n" + self.result()
            with patch("etf_agent.automation.agent_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, bad, "")):
                with self.assertRaises(TransientAgentError):
                    OpenCodeAgentRunner(root).run(self.task, root)
            good = self.event("tool", {"tool": "read", "state": {"status": "completed"}}) + "\n" + self.result()
            with patch("etf_agent.automation.agent_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, good, "")):
                self.assertEqual(OpenCodeAgentRunner(root).run(self.task, root).output, {"items": []})

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


    def test_sibling_run_names_add_anchored_deny_rules(self):
        permission = _opencode_read_permission(("bear_*",), Path("a/2026-11-11/run2"), sibling_run_names=("run1", "run2"))
        self.assertEqual(permission["*/run2/bear_*"], "deny")
        self.assertEqual(permission["*/run1/bear_*"], "deny")
        # 不誤擋：沒有任何未錨定的 */bear_* 規則。
        self.assertNotIn("*/bear_*", permission)
        # 不傳 sibling_run_names 時與舊行為相同。
        self.assertEqual(_opencode_read_permission(("bear_*",), Path("a/2026-11-11/run2")),
                         _opencode_read_permission(("bear_*",), Path("a/2026-11-11/run2"), sibling_run_names=()))

    def test_run_lists_other_run_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("daily/2026-11-10/runA", "daily/2026-11-11/runB", "daily/2026-11-11/runC"):
                (root / relative).mkdir(parents=True)
            task = AgentTask("bull_b0", "x", {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]},
                             deny_reads=("bear_*",))
            captured = {}

            def fake_run(command, **kwargs):
                captured.update(json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])["agent"]["etf-read-only"]["permission"]["read"])
                return subprocess.CompletedProcess(command, 0, self.result(), "")

            with patch("etf_agent.automation.agent_runner.subprocess.run", side_effect=fake_run):
                OpenCodeAgentRunner(root).run(task, root / "daily/2026-11-11/runC")
            self.assertEqual(captured["*/runA/bear_*"], "deny")
            self.assertEqual(captured["*/runB/bear_*"], "deny")
            self.assertEqual(captured["*/runC/bear_*"], "deny")
            self.assertEqual([key for key in captured if key.endswith("/bear_*")].count("*/runC/bear_*"), 1)
            self.assertNotIn("*/bear_*", captured)

    def test_missing_parent_directory_does_not_raise(self):
        from etf_agent.automation.agent_runner import _sibling_run_names
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "nope" / "2026-11-11" / "runX"
            self.assertEqual(_sibling_run_names(run_dir, 1), [])
            task = AgentTask("bull_b0", "x", {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]},
                             deny_reads=("bear_*",))
            captured = {}

            def fake_run(command, **kwargs):
                captured.update(json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])["agent"]["etf-read-only"]["permission"]["read"])
                return subprocess.CompletedProcess(command, 0, self.result(), "")

            with patch("etf_agent.automation.agent_runner.subprocess.run", side_effect=fake_run):
                OpenCodeAgentRunner(Path(directory)).run(task, run_dir)
            self.assertEqual(captured["*/runX/bear_*"], "deny")

    def test_sibling_rules_capped_to_recent_dates(self):
        from etf_agent.automation.agent_runner import _sibling_run_names
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for day in range(1, 21):
                for run in range(3):
                    (root / "daily" / ("2026-10-%02d" % day) / ("r%02d_%d" % (day, run))).mkdir(parents=True)
            own = root / "daily" / "2026-10-20" / "r20_0"
            names = _sibling_run_names(own, 30)  # 59 個 × 30 樣式 > 500 → 只取最近 14 日
            self.assertEqual(len(names), 14 * 3 - 1)
            self.assertNotIn("r01_0", names)
            self.assertNotIn("r20_0", names)
            self.assertEqual(len(_sibling_run_names(own, 1)), 59)


class TransientErrorClassificationTests(unittest.TestCase):
    """哪些失敗可重試（TransientAgentError）、哪些是安全拒絕（一般 DailyPipelineError）。"""

    def setUp(self):
        self.task = AgentTask("trader_b0", "只讀 brief，回覆 JSON", {"type": "object"})

    def assertTransient(self, runner, side_effect=None, return_value=None):
        target = "_run_claude_streaming" if isinstance(runner, ClaudeAgentRunner) else "subprocess.run"
        with patch("etf_agent.automation.agent_runner.%s" % target, side_effect=side_effect, return_value=return_value):
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
            with patch("etf_agent.automation.agent_runner._run_claude_streaming", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")):
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
