"""每日決策子 Agent 的共用型別與 CLI runner。"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Protocol, Sequence


class DailyPipelineError(RuntimeError):
    """每日決策鏈無法安全繼續。"""


class TransientAgentError(DailyPipelineError):
    """子 Agent CLI 的暫時性失敗：逾時、無法啟動、非零結束、輸出不是 JSON 或沒有結構化輸出。

    ``PipelineBase.agent_step`` 只對此類錯誤在時間預算內再試一次；秘密外洩、使用未核准工具
    等安全拒絕與確定性驗證失敗仍是一般 ``DailyPipelineError``，不重試。
    """


def _strings(min_items: int = 0) -> Dict[str, object]:
    return {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": min_items}


def _object(properties: Mapping[str, object], required: Sequence[str]) -> Dict[str, object]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_TEXT = {"type": "string", "minLength": 1}

# 子 Agent 一律不得讀取的檔案樣式；Claude 與 OpenCode 的拒讀規則都由此產生，避免兩邊不一致。
SENSITIVE_READ_GLOBS = (".env", ".env.*", ".envrc", "*.pem", "*.key", "*credential*", "*secret*")
_SECRET_FILES = (".env", ".envrc")
_MIN_SECRET_LENGTH = 8


def claude_read_deny_settings() -> str:
    """``claude --settings`` 用的拒讀規則；``**/`` 前綴同時涵蓋專案根目錄與子目錄。"""
    rules = ["Read(**/%s)" % pattern for pattern in SENSITIVE_READ_GLOBS]
    return json.dumps({"permissions": {"deny": rules}}, ensure_ascii=False)


def _opencode_read_permission() -> Dict[str, str]:
    permission = {"*": "allow"}
    permission.update({pattern if pattern.startswith("*") else "*" + pattern: "deny" for pattern in SENSITIVE_READ_GLOBS})
    return permission


def _secret_values(project_root: Path) -> list[str]:
    values = []
    for name in _SECRET_FILES:
        try:
            lines = (project_root / name).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            value = line.split("=", 1)[1].strip().strip("'\"")
            if len(value) >= _MIN_SECRET_LENGTH:
                values.append(value)
    return values


def scrub_secrets(text: str, project_root: Path) -> str:
    """把專案 ``.env``／``.envrc`` 的值從 CLI 錯誤文字遮蔽掉，錯誤才能安全進日誌與執行紀錄。"""
    for value in _secret_values(project_root):
        text = text.replace(value, "[已遮蔽]")
    return text


def reject_secret_leak(name: str, output: Mapping[str, object], project_root: Path) -> None:
    """Agent 輸出含專案 ``.env``／``.envrc`` 的值時拒絕；錯誤訊息不帶出該值。

    Codex 的唯讀沙箱不限制讀檔，這是所有 runner 共用的最後一道檢查。
    """
    text = json.dumps(output, ensure_ascii=False)
    if any(value in text for value in _secret_values(project_root)):
        raise DailyPipelineError("%s Agent 輸出包含本機秘密設定的值，已拒絕" % name)

@dataclass(frozen=True)
class AgentTask:
    name: str
    prompt: str
    schema: Mapping[str, object]


@dataclass
class AgentCall:
    name: str
    attempt: int
    output: Dict[str, object]
    # claude -p 回報的 total_cost_usd：以 API 價格估算的用量；claude.ai 訂閱登入時不另計費。
    estimated_usage_usd: float = 0.0
    usage_tokens: Optional[Dict[str, int]] = None


class AgentRunner(Protocol):
    def run(self, task: AgentTask, run_dir: Path) -> AgentCall:
        ...


class ClaudeAgentRunner:
    """以 ``claude -p`` 執行單次隔離工作階段；只開放 Read 工具與結構化輸出。

    ``max_budget_usd`` 對應 ``--max-budget-usd``，依估算用量限制單次 Agent 防止失控；
    claude.ai 訂閱登入時用量計入訂閱額度，不是實際扣款上限。
    """

    def __init__(
        self,
        project_root: Path,
        executable: str = "claude",
        model: Optional[str] = None,
        max_budget_usd: float = 3.0,
        timeout_seconds: int = 1800,
    ):
        self.project_root = project_root
        self.executable = executable
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.timeout_seconds = timeout_seconds

    def run(self, task: AgentTask, run_dir: Path) -> AgentCall:
        command = [
            self.executable, "-p", task.prompt,
            "--output-format", "json",
            "--json-schema", json.dumps(task.schema, ensure_ascii=False),
            "--tools", "Read",
            "--strict-mcp-config",  # 不載入使用者的 MCP 外掛；每次呼叫約少 4 萬 token 的固定開銷
            "--settings", claude_read_deny_settings(),
            "--add-dir", str(run_dir),
            "--max-budget-usd", str(self.max_budget_usd),
        ]
        if self.model:
            command += ["--model", self.model]
        try:
            completed = subprocess.run(
                command, cwd=self.project_root, capture_output=True, text=True,
                timeout=self.timeout_seconds, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TransientAgentError("%s Agent 執行失敗：%s" % (task.name, error)) from error
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise TransientAgentError(
                "%s Agent 輸出不是 JSON（exit %d）：%s" % (task.name, completed.returncode, scrub_secrets(completed.stderr[-500:], self.project_root))
            ) from error
        if not isinstance(payload, dict):
            raise TransientAgentError("%s Agent 輸出不是 JSON 物件" % task.name)
        output = payload.get("structured_output")
        if completed.returncode != 0 or payload.get("is_error") or not isinstance(output, dict):
            raise TransientAgentError("%s Agent 未產生結構化輸出（exit %d）：%s" % (
                task.name, completed.returncode, scrub_secrets(str(payload.get("result"))[:500], self.project_root)))
        reject_secret_leak(task.name, output, self.project_root)
        return AgentCall(task.name, 0, output, float(payload.get("total_cost_usd") or 0.0), _claude_usage_tokens(payload))


def _claude_usage_tokens(payload: Dict[str, object]) -> Optional[Dict[str, int]]:
    """claude -p 的 usage 欄位換成與 Codex 相同的 token 欄位；缺欄位或型別不對時不猜測，回 None。"""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    fields = {
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in fields.values() if value is not None):
        return None
    return {name: value for name, value in fields.items() if value is not None} or None


class CodexAgentRunner:
    """以唯讀 ``codex exec`` 取得符合 JSON Schema 的單次研究輸出。"""

    def __init__(
        self,
        project_root: Path,
        executable: str = "codex",
        model: Optional[str] = None,
        timeout_seconds: int = 1800,
    ):
        self.project_root = project_root
        self.executable = executable
        self.model = model
        self.timeout_seconds = timeout_seconds

    def run(self, task: AgentTask, run_dir: Path) -> AgentCall:
        # 每次呼叫使用獨立暫存目錄，避免續跑或重試讀到前一次的輸出。
        with tempfile.TemporaryDirectory(prefix="codex-agent-", dir=run_dir) as temporary:
            schema_path = Path(temporary) / "schema.json"
            output_path = Path(temporary) / "output.json"
            schema_path.write_text(json.dumps(task.schema, ensure_ascii=False), encoding="utf-8")
            command = [
                self.executable, "exec", "--sandbox", "read-only",
                "--output-schema", str(schema_path),
                "--output-last-message", str(output_path),
            ]
            if self.model:
                command += ["--model", self.model]
            command += ["-"]
            try:
                completed = subprocess.run(
                    command, input=task.prompt, cwd=self.project_root,
                    capture_output=True, text=True, timeout=self.timeout_seconds, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise TransientAgentError("%s Codex Agent 執行失敗：%s" % (task.name, error)) from error
            if completed.returncode != 0:
                raise TransientAgentError(
                    "%s Codex Agent 失敗（exit %d）：%s" %
                    (task.name, completed.returncode, scrub_secrets(completed.stderr[-500:], self.project_root))
                )
            try:
                output = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise TransientAgentError("%s Codex Agent 未產生有效 JSON：%s" % (task.name, error)) from error
            if not isinstance(output, dict):
                raise TransientAgentError("%s Codex Agent 輸出不是物件" % task.name)
            reject_secret_leak(task.name, output, self.project_root)
            return AgentCall(task.name, 0, output)


class OpenCodeAgentRunner:
    """隔離的 OpenCode 非互動工作階段；只准讀檔，JSONL 最後一則訊息須是 JSON。"""

    def __init__(
        self,
        project_root: Path,
        executable: str = "opencode",
        model: Optional[str] = None,
        timeout_seconds: int = 1800,
    ):
        self.project_root = project_root
        self.executable = executable
        self.model = model
        self.timeout_seconds = timeout_seconds

    def run(self, task: AgentTask, run_dir: Path) -> AgentCall:
        # 不沿用使用者／專案的寫入工具設定或外部插件；每次任務都是獨立會話。
        config = {
            "agent": {
                "etf-read-only": {
                    "mode": "primary",
                    "permission": {
                        "*": "deny",
                        "read": _opencode_read_permission(),
                        "external_directory": "deny",
                    },
                }
            }
        }
        env = dict(os.environ, OPENCODE_DISABLE_PROJECT_CONFIG="1",
                   OPENCODE_CONFIG_CONTENT=json.dumps(config, ensure_ascii=False))
        command = [self.executable, "run", "--pure", "--agent", "etf-read-only", "--format", "json"]
        if self.model:
            command += ["--model", self.model]
        command += [
            "只讀本次提供的 Skill 與 brief；不得修改檔案、執行命令、讀取 .env 或自行新增來源。"
            "只輸出符合下列 JSON Schema 的單一 JSON 物件，不加 Markdown 或前後說明。\n"
            + json.dumps(task.schema, ensure_ascii=False)
            + "\n\n" + task.prompt
        ]
        try:
            completed = subprocess.run(command, cwd=self.project_root, env=env,
                                       capture_output=True, text=True, timeout=self.timeout_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TransientAgentError("%s OpenCode Agent 執行失敗：%s" % (task.name, error)) from error
        if completed.returncode != 0:
            raise TransientAgentError("%s OpenCode Agent 失敗（exit %d）：%s" %
                                      (task.name, completed.returncode, scrub_secrets(completed.stderr[-500:], self.project_root)))
        messages: Dict[str, list[str]] = {}
        final_message = None
        usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
        cost = 0.0
        try:
            for line in completed.stdout.splitlines():
                event = json.loads(line)
                kind = event.get("type")
                part = event.get("part", {})
                if not isinstance(part, dict):
                    raise ValueError("事件 part 格式錯誤")
                if kind == "error":
                    raise ValueError("Agent 回報錯誤")
                if kind == "tool_use" and part.get("tool") != "read":
                    # 安全拒絕：不是暫時性失敗，不重試。
                    raise DailyPipelineError("%s OpenCode Agent 使用未核准工具：%s" % (task.name, part.get("tool")))
                if kind == "tool_use" and part.get("state", {}).get("status") != "completed":
                    raise ValueError("Agent 讀取失敗")
                if kind == "text":
                    message_id = part.get("messageID")
                    if not isinstance(message_id, str) or not isinstance(part.get("text"), str):
                        raise ValueError("文字事件缺少訊息識別或內容")
                    messages.setdefault(message_id, []).append(part["text"])
                if kind == "step_finish":
                    tokens = part.get("tokens")
                    if not isinstance(tokens, dict) or not isinstance(tokens.get("cache"), dict):
                        raise ValueError("缺少可核對的 token 用量")
                    fields = {"input_tokens": tokens.get("input"), "cached_input_tokens": tokens["cache"].get("read"),
                              "output_tokens": tokens.get("output"), "reasoning_output_tokens": tokens.get("reasoning")}
                    for field, value in fields.items():
                        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                            raise ValueError("token 用量欄位無效：%s" % field)
                        usage[field] += value
                    amount = float(part.get("cost", 0))
                    if not math.isfinite(amount) or amount < 0:
                        raise ValueError("估算費用無效")
                    cost += amount
                    final_message = part.get("messageID") if part.get("reason") == "stop" else None
        except (ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as error:
            raise TransientAgentError("%s OpenCode Agent JSONL 無效：%s" % (task.name, error)) from error
        if not isinstance(final_message, str) or final_message not in messages:
            raise TransientAgentError("%s OpenCode Agent 未完成最終 JSON 回答" % task.name)
        try:
            output = json.loads("".join(messages[final_message]))
        except json.JSONDecodeError as error:
            raise TransientAgentError("%s OpenCode Agent 最終輸出不是 JSON" % task.name) from error
        if not isinstance(output, dict):
            raise TransientAgentError("%s OpenCode Agent 輸出不是物件" % task.name)
        reject_secret_leak(task.name, output, self.project_root)
        return AgentCall(task.name, 0, output, cost, usage)
