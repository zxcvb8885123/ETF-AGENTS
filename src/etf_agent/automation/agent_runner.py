"""每日決策子 Agent 的共用型別與 CLI runner。"""

from __future__ import annotations

import re
import json
import math
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Protocol, Sequence, Tuple


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
_SENSITIVE_BASE_GLOBS = (
    ".env", ".env.*", ".envrc", "*.pem", "*.key", "*credential*", "*secret*",
    ".netrc", "*.p12", "*.pfx", "id_rsa*", "*.token", ".npmrc", ".pypirc", "*.jks",
)


def _with_case_variants(patterns: Sequence[str]) -> Tuple[str, ...]:
    """每個樣式另加全小寫、全大寫與首字大寫變體：Claude／OpenCode 的樣式比對可能分大小寫，
    而 macOS 檔案系統不分，``Secrets.json``、``CREDENTIALS`` 否則會漏擋。"""
    found = []
    for pattern in patterns:
        for variant in (pattern, pattern.lower(), pattern.upper(), pattern.title()):
            if variant not in found:
                found.append(variant)
    return tuple(found)


SENSITIVE_READ_GLOBS = _with_case_variants(_SENSITIVE_BASE_GLOBS)
# 外洩掃描的檔案樣式，與 SENSITIVE_READ_GLOBS 的 .env 類一致（含 .env.local、.env.production 等）。
_SECRET_FILE_GLOBS = (".env*", ".envrc")
# 範本檔只放示意值，不是秘密；納入掃描會讓無害輸出被誤拒。
_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".tmpl")
_MIN_SECRET_LENGTH = 8
# 多空研究之後的段產物（含對方論點）。拒讀規則與 Codex 暫存的殘留偵測共用這一份，避免兩邊不一致。
LATER_STAGE_GLOBS = (
    "research_debate*", "debate*", "intent*", "trade_decision*",
    "trader_*", "brief_trader_*", "team_inputs*", "selection_ranking*",
    "dip_facts*",  # quality_dip_facts 的完整事實（含候選標示）只給交易 Agent，研究員不得讀
    "swap_nomination*", "swap_nominator*",  # 5c 換股提名（agent_swap）的輸出與嘗試紀錄，研究員不得讀
    "quality_dip_ruling*", "brief_quality_dip_ruling*", "quality_dip_ruler*",  # agent_swap.quality_dip v3 的裁決輸出、簡報與嘗試紀錄，研究員不得讀
    "exposure_facts*",  # exposure_view 的組合曝險事實只給風險 Agent 與換股提名，研究員不得讀
    "offense_facts*",  # position_sizing.offense 的進攻事實只給風險 Agent，研究員不得讀
    "decline_depth_facts*",  # quality_dip v4 的連跌深度事實只給換股提名與風險 Agent 的裁決，研究員不得讀
    "held_weight_facts*",  # held_weight_budget v2 的持股權重事實只給交易 Agent，研究員不得讀
    "release_order*", "brief_release_order*",  # 回撤減碼的釋出順序（drawdown_derisk v5）的輸出、簡報與嘗試紀錄，研究員不得讀
)


def claude_read_deny_settings(extra: Sequence[str] = ()) -> str:
    """``claude --settings`` 用的拒讀規則；``**/`` 前綴同時涵蓋專案根目錄與子目錄。

    ``extra`` 是任務專屬的拒讀檔名樣式（例如多頭任務不得讀空頭輸出）。"""
    rules = ["Read(**/%s)" % pattern for pattern in (*SENSITIVE_READ_GLOBS, *extra)]
    return json.dumps({"permissions": {"deny": rules}}, ensure_ascii=False)


def _opencode_read_permission(extra: Sequence[str] = (), run_dir: Optional[Path] = None) -> Dict[str, str]:
    permission = {"*": "allow"}
    permission.update({pattern if pattern.startswith("*") else "*" + pattern: "deny" for pattern in SENSITIVE_READ_GLOBS})
    # 任務專屬樣式（多空互相隔離）：OpenCode 的 ``*`` 會跨越 ``/``，所以不能只靠 "*/bear_*"
    # （路徑中任何名為 bear_… 的目錄都會被誤擋）。有 run_dir 時錨定在該任務目錄名稱之後，
    # 只擋直接放在任務目錄下的同儕檔案；絕對或相對路徑都能比對到，不會因路徑形式而放行。
    anchor = "*/" + Path(run_dir).resolve().name + "/" if run_dir is not None else "*/"
    permission.update({pattern if pattern.startswith("*") else anchor + pattern: "deny" for pattern in extra})
    return permission


def _secret_files(project_root: Path) -> list[Path]:
    found = {path for pattern in _SECRET_FILE_GLOBS for path in project_root.glob(pattern)
             if path.is_file() and not path.name.endswith(_TEMPLATE_SUFFIXES)}
    return sorted(found)


def _secret_values(project_root: Path) -> list[str]:
    values = []
    for path in _secret_files(project_root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            raw = line.split("=", 1)[1].strip()
            candidates = {raw.strip("'\"")}
            if raw[:1] in ("'", '"'):
                # 帶引號的值：取到對應的結尾引號，後面的行尾註解不算值。
                end = raw.find(raw[0], 1)
                if end > 0:
                    candidates.add(raw[1:end])
            else:
                # 未加引號：空白後的 # 是行尾註解。
                candidates.add(re.split(r"\s+#", raw, maxsplit=1)[0].strip())
            for value in candidates:
                if len(value) >= _MIN_SECRET_LENGTH:
                    values.append(value)
    return values


def scrub_secrets(text: str, project_root: Path) -> str:
    """把專案 ``.env*``／``.envrc`` 的值從 CLI 錯誤文字遮蔽掉，錯誤才能安全進日誌與執行紀錄。"""
    for value in _secret_values(project_root):
        text = text.replace(value, "[已遮蔽]")
    return text


def _string_values(value: object):
    """遞迴取出輸出中所有字串（含字典鍵）。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _string_values(key)
            yield from _string_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_values(item)


def reject_secret_leak(name: str, output: Mapping[str, object], project_root: Path) -> None:
    """Agent 輸出含專案 ``.env*``／``.envrc`` 的值時拒絕；錯誤訊息不帶出該值。

    Codex 的唯讀沙箱不限制讀檔，這是所有 runner 共用的最後一道檢查。
    """
    # 比對原始字串值，不比對 json.dumps 後的文字：反斜線、雙引號會被跳脫而讓秘密比對失敗。
    texts = list(_string_values(output))
    texts.append(json.dumps(output, ensure_ascii=False))
    if any(value in text for value in _secret_values(project_root) for text in texts):
        raise DailyPipelineError("%s Agent 輸出包含本機秘密設定的值，已拒絕" % name)

@dataclass(frozen=True)
class AgentTask:
    name: str
    prompt: str
    schema: Mapping[str, object]
    # 任務專屬的拒讀檔名樣式（Claude／OpenCode 的 read 權限強制；Codex 沙箱無法限制讀檔）。
    deny_reads: Sequence[str] = ()
    # 只有 PipelineBase 在研究階段輸出暫存啟用（同儕輸出在階段結束前不落地到磁碟）時才設 True；
    # 帶 deny_reads 卻沒有此旗標的任務，不強制拒讀的執行器（Codex）一律拒絕啟動。
    stage_isolated: bool = False


@dataclass
class AgentCall:
    name: str
    attempt: int
    output: Dict[str, object]
    # claude -p 回報的 total_cost_usd：以 API 價格估算的用量；claude.ai 訂閱登入時不另計費。
    estimated_usage_usd: float = 0.0
    usage_tokens: Optional[Dict[str, int]] = None


class AgentRunner(Protocol):
    # 執行器是否能以自身權限設定強制 ``AgentTask.deny_reads``（Claude、OpenCode 為 True；Codex 為 False）。
    # 沒有此屬性的執行器視為不強制（fail-closed），研究階段改用輸出暫存。
    enforces_deny_reads: bool

    def run(self, task: AgentTask, run_dir: Path) -> AgentCall:
        ...


class ClaudeAgentRunner:
    """以 ``claude -p`` 執行單次隔離工作階段；只開放 Read 工具與結構化輸出。

    ``max_budget_usd`` 對應 ``--max-budget-usd``，依估算用量限制單次 Agent 防止失控；
    claude.ai 訂閱登入時用量計入訂閱額度，不是實際扣款上限。
    """

    enforces_deny_reads = True

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
            "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(task.schema, ensure_ascii=False),
            "--tools", "Read",
            "--strict-mcp-config",  # 不載入使用者的 MCP 外掛；每次呼叫約少 4 萬 token 的固定開銷
            "--setting-sources", "project",  # 不載入使用者個人設定與外掛；實測每次呼叫約少 1.1 萬 token
            "--settings", claude_read_deny_settings(task.deny_reads),
            "--add-dir", str(run_dir),
            "--max-budget-usd", str(self.max_budget_usd),
        ]
        if self.model:
            command += ["--model", self.model]
        trace_path = self.project_root / "var" / "logs" / "agent_trace" / run_dir.name / ("%s.jsonl" % task.name)
        try:
            completed = _run_claude_streaming(
                command, self.project_root, self.timeout_seconds, trace_path, self.project_root,
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


def _trace_summary(event: Mapping[str, object], elapsed: float) -> Dict[str, object]:
    """把串流事件縮成一行：只留類型、時間與工具名稱／限流資訊，不存模型文字或讀到的內容。"""
    row: Dict[str, object] = {"t": round(elapsed, 1), "type": event.get("type")}
    if event.get("subtype"):
        row["subtype"] = event.get("subtype")
    if event.get("type") == "system" and event.get("subtype") == "thinking_tokens":
        row["estimated_tokens"] = event.get("estimated_tokens")
    if event.get("type") == "rate_limit_event":
        info = event.get("rate_limit_info")
        if isinstance(info, dict):
            row["rate_limit_info"] = {
                key: info[key] for key in ("status", "rateLimitType", "resetsAt", "overageStatus")
                if isinstance(info.get(key), (str, int, float, bool)) or info.get(key) is None and key in info
            }
    message = event.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), list):
        tools = []
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                file_path = tool_input.get("file_path")
                tools.append({"name": block.get("name"), "file": file_path if isinstance(file_path, str) else None})
        if tools:
            row["tools"] = tools
    if event.get("type") == "result":
        row["duration_api_ms"] = event.get("duration_api_ms")
        row["is_error"] = event.get("is_error")
    return row


def _run_claude_streaming(
    command: Sequence[str], cwd: Path, timeout: int, trace_path: Path, project_root: Path,
) -> "subprocess.CompletedProcess[str]":
    """以串流模式執行 ``claude -p``，邊跑邊把事件摘要寫進 ``trace_path``（本機除錯用，失敗不影響流程）。

    回傳的 ``stdout`` 是最後一個 ``result`` 事件的原文（與 ``--output-format json`` 的輸出同形），
    沒有 result 事件時為空字串，由呼叫端依原規則 fail-closed。逾時會終止子程序並拋出 ``TimeoutExpired``。
    """
    started = time.monotonic()
    trace_file = None
    try:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        # 重試時不蓋掉前一次（常是失敗那次）的紀錄：檔名已存在就加 .2、.3 …
        unique_path, attempt = trace_path, 1
        while unique_path.exists():
            attempt += 1
            unique_path = trace_path.with_name("%s.%d%s" % (trace_path.stem, attempt, trace_path.suffix))
        trace_file = unique_path.open("x", encoding="utf-8")
    except OSError:
        trace_file = None
    process = subprocess.Popen(
        list(command), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    result_line = {"text": ""}
    stderr_chunks: list[str] = []

    def read_stdout() -> None:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "result":
                result_line["text"] = line
            if trace_file is not None:
                try:
                    trace_file.write(scrub_secrets(json.dumps(_trace_summary(event, time.monotonic() - started), ensure_ascii=False), project_root) + "\n")
                    trace_file.flush()
                except (OSError, ValueError):
                    pass

    def read_stderr() -> None:
        stderr_chunks.append(process.stderr.read())

    threads = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        for thread in threads:
            thread.join(timeout=5)
        if trace_file is not None:
            trace_file.close()
    return subprocess.CompletedProcess(list(command), process.returncode, result_line["text"], "".join(stderr_chunks))


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


def _last_agent_message(stdout: str) -> str:
    """從 ``codex exec --json`` 的 JSONL 標準輸出取最後一則 agent_message 文字；找不到即拋 ValueError。"""
    message = None
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            if isinstance(item.get("text"), str):
                message = item["text"]
    if message is None:
        raise ValueError("標準輸出沒有 agent_message")
    return message


class CodexAgentRunner:
    """以唯讀 ``codex exec`` 取得符合 JSON Schema 的單次研究輸出。

    Codex 沙箱只擋寫、不擋讀，無法強制 ``deny_reads``。任務帶 ``deny_reads``（例如多空研究員互相隔離）時，
    只有呼叫端宣告 ``stage_isolated``（研究階段輸出暫存：同儕輸出在階段結束前不存在於磁碟）才啟動，否則拒絕、不重試。
    這是時序隔離、不是權限隔離：Codex 仍可讀整個專案（含舊 run 的多空輸出）。
    隔離任務的回覆只走標準輸出（``--json``），不寫 ``--output-last-message`` 檔，避免同機並行的同儕任務在系統暫存區讀到。"""

    enforces_deny_reads = False

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
        if task.deny_reads and not task.stage_isolated:
            raise DailyPipelineError("%s Codex Agent 無法強制任務拒讀規則（%s），且未由研究階段輸出暫存保證隔離，已拒絕啟動；請改用 claude 或 opencode" % (
                task.name, "、".join(task.deny_reads)))
        # 每次呼叫使用獨立暫存目錄，避免續跑或重試讀到前一次的輸出；放在系統暫存區而非 run 目錄，
        # 免得同儕的 Codex 任務在 run 目錄裡讀到這次的輸出。
        with tempfile.TemporaryDirectory(prefix="codex-agent-") as temporary:
            schema_path = Path(temporary) / "schema.json"
            output_path = Path(temporary) / "output.json"
            schema_path.write_text(json.dumps(task.schema, ensure_ascii=False), encoding="utf-8")
            command = [self.executable, "exec", "--sandbox", "read-only", "--output-schema", str(schema_path)]
            if task.deny_reads:
                # 隔離任務不把 session 紀錄寫到磁碟，避免同儕的 Codex 讀到這次的完整輸出；
                # 也不用 --output-last-message 落地，改由 --json 的標準輸出（只在記憶體）取最後一則回覆。
                command += ["--ephemeral", "--json"]
            else:
                command += ["--output-last-message", str(output_path)]
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
                if task.deny_reads:
                    output = json.loads(_last_agent_message(completed.stdout))
                else:
                    output = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise TransientAgentError("%s Codex Agent 未產生有效 JSON：%s" % (task.name, error)) from error
            if not isinstance(output, dict):
                raise TransientAgentError("%s Codex Agent 輸出不是物件" % task.name)
            reject_secret_leak(task.name, output, self.project_root)
            return AgentCall(task.name, 0, output)


class OpenCodeAgentRunner:
    """隔離的 OpenCode 非互動工作階段；只准讀檔，JSONL 最後一則訊息須是 JSON。"""

    enforces_deny_reads = True

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
                        "read": _opencode_read_permission(task.deny_reads, run_dir),
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
                # 事件型別名稱沒有實際錄製的 fixture 可核對，所以不只認 ``tool_use``：任何帶 ``tool`` 欄位（或 part 型別為 tool）
                # 的事件都當作工具呼叫檢查，換了型別名稱也不會讓非 read 工具漏網。
                is_tool_event = kind == "tool_use" or part.get("tool") is not None or part.get("type") == "tool"
                if is_tool_event and part.get("tool") != "read":
                    # 安全拒絕：不是暫時性失敗，不重試。
                    raise DailyPipelineError("%s OpenCode Agent 使用未核准工具：%s" % (task.name, part.get("tool")))
                if is_tool_event and (not isinstance(part.get("state"), dict) or part["state"].get("status") != "completed"):
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
