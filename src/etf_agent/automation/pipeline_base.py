"""每日決策鏈的執行基礎：結果型別、輸入摘要序列化，以及 Agent 步驟（重試、續跑、平行任務）。"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, TypeVar

from .agent_runner import AgentRunner, AgentTask, DailyPipelineError


def degraded_research_result(snapshot: Mapping[str, object], run_id: str) -> Dict[str, object]:
    """舊版 fixture／獨立研究的降級結果；新每日鏈不使用此相容工具。"""
    return {
        "schema_version": "2.1",
        "run_id": run_id,
        "snapshot_id": snapshot["snapshot_id"],
        "decision_cutoff": snapshot["decision_cutoff"],
        "skill_version": "2.1.0",
        "status": "degraded",
        "items": [],
        "errors": ["EVENT_RESEARCH_NOT_RUN：每日腳本尚未自動執行事件研究子 Agent，事件未經研究，不代表沒有事件。"],
    }


def compact_brief_json(payload: Mapping[str, object]) -> str:
    """頂層每個欄位一行；值是物件陣列（如 symbols）時每個元素一行，其餘不縮排。"""
    def dump(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    lines = []
    for key, value in payload.items():
        if isinstance(value, list) and value and all(isinstance(item, Mapping) for item in value):
            body = "[\n" + ",\n".join(dump(item) for item in value) + "\n]"
        else:
            body = dump(value)
        lines.append("%s:%s" % (dump(str(key)), body))
    return "{\n" + ",\n".join(lines) + "\n}\n"


@dataclass
class PipelineResult:
    status: str
    decision_status: Optional[str] = None
    decision_run_id: Optional[str] = None
    orders: List[Dict[str, object]] = field(default_factory=list)
    cash_stance: Optional[str] = None
    research_result_path: Optional[Path] = None
    agent_calls: List[Dict[str, object]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    stage_durations: Dict[str, float] = field(default_factory=dict)


ANALYST_TASK_PREFIXES = frozenset({"technical", "fundamental", "event"})
MAX_PARALLEL_WORKERS = 8
_Job = TypeVar("_Job")


class PipelineBase:
    """由 Snapshot、帳戶快照與交易狀態包跑完一次可封存的 Portfolio Decision。"""

    def __init__(
        self,
        project_root: Path,
        run_dir: Path,
        runner: AgentRunner,
        decision_repository: Path,
        max_attempts: int = 2,
        log: Callable[[str], None] = print,
        resume: bool = False,
        analyst_runner: Optional[AgentRunner] = None,
        technical_gate: bool = False,
        max_workers: int = 1,
    ):
        """``analyst_runner`` 只負責技術／基本面／事件分析師批次，未提供時全部沿用 ``runner``。
        ``resume=True`` 時，同一 run 目錄中先前已通過驗證的 Agent 輸出會以目前的
        Validator 重驗後沿用，不再呼叫 Agent；輸入改變時 envelope 對不上，自然重跑。
        ``max_workers`` 是分析師與多空研究階段同時執行的 Agent 呼叫數上限；預設 1 為串行。"""
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= MAX_PARALLEL_WORKERS:
            raise ValueError("max_workers 必須介於 1 與 %d" % MAX_PARALLEL_WORKERS)
        self.root = project_root
        self.run_dir = run_dir
        self.runner = runner
        self.analyst_runner = analyst_runner or runner
        self.technical_gate = technical_gate
        self.decision_repository = decision_repository
        self.max_attempts = max_attempts
        self.log = log
        self.resume = resume
        self.max_workers = max_workers
        self.calls: List[Dict[str, object]] = []
        self.stage_durations: Dict[str, float] = {}
        self._calls_lock = threading.Lock()
    # ------------------------------------------------------------------ helpers
    @contextmanager
    def _timed(self, stage: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.stage_durations[stage] = round(time.monotonic() - started, 1)

    def _run_jobs(self, jobs: Sequence[Tuple[str, Callable[[], _Job]]]) -> List[_Job]:
        """依序回傳各任務結果。``max_workers`` 大於 1 時同時執行，任一失敗即取消尚未開始的任務並拋出錯誤；
        ``self.calls`` 依提交順序整理，結果不受完成先後影響。"""
        if self.max_workers == 1 or len(jobs) <= 1:
            return [job() for _, job in jobs]
        first_call = len(self.calls)
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(jobs))) as pool:
            futures = [pool.submit(job) for _, job in jobs]
            wait(futures, return_when=FIRST_EXCEPTION)
            for future in futures:
                future.cancel()
        position = {name: index for index, (name, _) in enumerate(jobs)}
        with self._calls_lock:
            self.calls[first_call:] = sorted(self.calls[first_call:], key=lambda call: position.get(str(call["name"]), len(jobs)))
        for future in futures:
            if not future.cancelled() and future.exception() is not None:
                raise future.exception()
        return [future.result() for future in futures]

    def _path(self, name: str) -> Path:
        return self.run_dir / ("%s.json" % name)

    def save_artifact(self, name: str, payload: Mapping[str, object]) -> Path:
        path = self._path(name)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def save_brief(self, name: str, payload: Mapping[str, object]) -> Path:
        """Agent 讀取的輸入摘要：不縮排，symbols 每檔一行，仍是合法 JSON，可分段讀取且少約四成字元。"""
        path = self._path(name)
        path.write_text(compact_brief_json(payload), encoding="utf-8")
        return path

    def agent_step(
        self,
        task: AgentTask,
        build: Callable[[Mapping[str, object]], Dict[str, object]],
        validate: Callable[[Mapping[str, object]], List[str]],
    ) -> Dict[str, object]:
        """執行 Agent → 補 envelope → 驗證；失敗回饋錯誤重試，仍失敗即停止。"""
        previous = sorted(
            self.run_dir.glob("%s_raw_*.json" % task.name),
            key=lambda path: int(path.stem.rsplit("_", 1)[1]),
        )
        if self.resume:
            for path in reversed(previous):
                artifact = build(json.loads(path.read_text(encoding="utf-8")))
                if not validate(artifact):
                    self.log("  [agent] %s 沿用已驗證輸出 %s" % (task.name, path.name))
                    return artifact
        prompt = task.prompt
        errors: List[str] = []
        # 原始輸出只新增、不覆寫，保留每次嘗試供稽核。
        offset = len(previous)
        for attempt in range(1, self.max_attempts + 1):
            self.log("  [agent] %s 第 %d 次" % (task.name, attempt))
            runner = self.analyst_runner if task.name.split("_", 1)[0] in ANALYST_TASK_PREFIXES else self.runner
            started = time.monotonic()
            call = runner.run(AgentTask(task.name, prompt, task.schema), self.run_dir)
            item = {
                "name": task.name, "attempt": attempt, "estimated_usage_usd": call.estimated_usage_usd,
                "duration_seconds": round(time.monotonic() - started, 1),
            }
            model = getattr(runner, "model", None)
            if model:
                item["model"] = model
            if call.usage_tokens is not None:
                item["usage_tokens"] = call.usage_tokens
            with self._calls_lock:
                self.calls.append(item)
            self.save_artifact("%s_raw_%d" % (task.name, offset + attempt), call.output)
            artifact = build(call.output)
            errors = validate(artifact)
            if not errors:
                return artifact
            # 每次驗證失敗的錯誤數與前幾項留在執行紀錄，評測時可看出 Agent 在哪裡出錯。
            with self._calls_lock:
                item["validation_errors"] = len(errors)
                item["first_validation_errors"] = [str(error)[:200] for error in errors[:3]]
            prompt = (
                task.prompt
                + "\n\n上一次輸出未通過確定性驗證，錯誤如下；請修正後重新輸出完整結果：\n- "
                + "\n- ".join(errors[:40])
            )
        raise DailyPipelineError("%s Agent 輸出驗證失敗：%s" % (task.name, "；".join(errors[:10])))
