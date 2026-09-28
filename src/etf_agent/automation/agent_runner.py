"""以 ``claude -p`` 執行隔離子 Agent 的共用型別與 runner。"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Protocol, Sequence


class DailyPipelineError(RuntimeError):
    """每日決策鏈無法安全繼續。"""


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
            raise DailyPipelineError("%s Agent 執行失敗：%s" % (task.name, error)) from error
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise DailyPipelineError(
                "%s Agent 輸出不是 JSON（exit %d）：%s" % (task.name, completed.returncode, completed.stderr[-500:])
            ) from error
        output = payload.get("structured_output")
        if payload.get("is_error") or not isinstance(output, dict):
            raise DailyPipelineError("%s Agent 未產生結構化輸出：%s" % (task.name, str(payload.get("result"))[:500]))
        return AgentCall(task.name, 0, output, float(payload.get("total_cost_usd") or 0.0))
