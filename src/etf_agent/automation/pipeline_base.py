"""每日決策鏈的執行基礎：結果型別、輸入摘要序列化，以及 Agent 步驟（重試、續跑、平行任務）。"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, TypeVar

from etf_agent.core import single_line

from .agent_runner import LATER_STAGE_GLOBS, AgentCall, AgentRunner, AgentTask, DailyPipelineError, TransientAgentError, reject_secret_leak
from .skill_digest import ROOT_PLACEHOLDER, RUN_DIR_PLACEHOLDER, prompt_sha256, skill_digest, skill_versions


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
    position_breadth: Optional[str] = None
    research_result_path: Optional[Path] = None
    agent_calls: List[Dict[str, object]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    stage_durations: Dict[str, float] = field(default_factory=dict)
    # 本次 run 各子 Agent 實際讀到的 Skill 檔版本（由 agent_calls 的 skill_files 彙整）；只作執行紀錄，不進決策 artifact。
    skill_versions: Dict[str, object] = field(default_factory=dict)


# market 為事件分析師的全市場新聞標註（market_news_labels_*）。
ANALYST_TASK_PREFIXES = frozenset({"technical", "fundamental", "event", "market"})
RESEARCH_TASK_PREFIXES = frozenset({"bull", "bear"})
MAX_PARALLEL_WORKERS = 8
DEFAULT_TRANSIENT_RETRIES = 1  # 暫時性失敗（逾時、非零結束、輸出不是 JSON）最多再試的次數
DEFAULT_RETRY_DELAY_SECONDS = 30.0
# 研究階段暫存啟用時，run 目錄內出現這些檔代表多空研究員（或其後段產物）已落地；
# 不含每次都會重寫的 brief_bull_*／brief_bear_*（兩方共同輸入，不含任何研究員輸出）。
RESEARCH_RESIDUE_GLOBS = ("bull_*.json", "bear_*.json") + LATER_STAGE_GLOBS
_Job = TypeVar("_Job")
# 驗證失敗的回饋採中性措辭：兩種合法修法並列，不暗示「補採納」才是正解，避免 Agent 為了通過驗證而變積極。
VALIDATION_FEEDBACK_PREFIX = (
    "\n\n上一次輸出未通過確定性驗證，錯誤列在下方。每一項都有兩種合法修法，請依你的判斷擇一："
    "（1）降級：改為較保守或不需該依據的結果，例如把 buy／add 改為 hold 或 no_trade、把 strength 改為 none、"
    "把 outlook 改為 unknown 並說明 data_gaps、撤回沒有依據的 claim 或 finding；"
    "（2）補依據：只在輸入中確實存在對應的證據、發現或論點、而且你本來就認同時，補上正確的引用或採納。"
    "不要為了通過驗證而把判斷變得更積極、採納你不認同的論點，或引用不相符的證據。請修正後重新輸出完整結果：\n- "
)


_DIFF_FIELDS = ("intent", "conviction", "strength", "outlook", "event_outlook", "level", "breadth")
_DIFF_COUNTS = ("claims", "findings", "adopted_claim_ids", "rejected_claim_ids", "events")
_ORDERS = {
    "conviction": (None, "low", "medium", "high"),  # None→有信心等級代表新增 buy／add
    "strength": ("none", "weak", "moderate", "strong"),
    "outlook": ("negative", "unknown", "neutral", "positive"),
    "event_outlook": ("negative", "unknown", "neutral", "positive"),
    "intent": ("exit", "trim", "no_trade", "hold", "add", "buy"),
    "level": ("defensive", "neutral", "aggressive"),
    "breadth": ("diversified", "balanced", "concentrated"),
}


def _direction(field: str, before: object, after: object) -> str:
    """同一欄位前後值的方向：積極、保守或無法排序；供稽核統計，不影響驗證。

    claims／findings／採納／否決等計數的增減方向不明（多一個空頭 claim 不等於更積極），一律標 other。"""
    order = _ORDERS.get(field)
    if order is None or before not in order or after not in order:
        return "other"
    delta = order.index(after) - order.index(before)
    return "more_aggressive" if delta > 0 else "more_conservative" if delta < 0 else "same"


def _row_summary(row: Mapping[str, object]) -> Dict[str, object]:
    summary: Dict[str, object] = {field: row.get(field) for field in _DIFF_FIELDS if field in row}
    for field in _DIFF_COUNTS:
        if isinstance(row.get(field), list):
            summary["%s_count" % field] = len(row[field])
    return summary


def output_revision_diff(before: Mapping[str, object], after: Mapping[str, object]) -> Dict[str, object]:
    """驗證回饋重試前後的差異摘要：逐檔（``items`` 依 symbol）或整體（現金姿態等）比較意圖、信心、強度、
    看法與 claims／findings 數，並標示每個改變是變積極、變保守或其他。只做摘要，不改變任何輸出。"""
    def rows(payload: Mapping[str, object]) -> Dict[str, Dict[str, object]]:
        items = payload.get("items")
        if isinstance(items, list):
            return {str(item.get("symbol", "")).upper(): _row_summary(item) for item in items if isinstance(item, Mapping)}
        return {"": _row_summary(payload)}

    old, new = rows(before), rows(after)
    changes: List[Dict[str, object]] = []
    counts = {"more_aggressive": 0, "more_conservative": 0, "other": 0}
    for key in sorted(set(old) | set(new)):
        previous, current = old.get(key, {}), new.get(key, {})
        for field in sorted(set(previous) | set(current)):
            if previous.get(field) == current.get(field):
                continue
            direction = _direction(field, previous.get(field), current.get(field))
            if direction == "same":
                direction = "other"
            counts[direction] += 1
            change: Dict[str, object] = {"field": field, "before": previous.get(field), "after": current.get(field), "direction": direction}
            if key:
                change["symbol"] = key
            changes.append(change)
    return {"changed_fields": len(changes), **counts, "changes": changes[:200]}


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
        research_runner: Optional[AgentRunner] = None,
        technical_gate: bool = False,
        max_workers: int = 1,
        transient_retries: int = DEFAULT_TRANSIENT_RETRIES,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
        retry_deadline: Optional[datetime] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ):
        """``analyst_runner`` 只負責技術／基本面／事件分析師批次（含全市場新聞標註），``research_runner`` 只負責
        多頭／空頭研究員批次；未提供時沿用 ``runner``（交易、現金姿態與風險審查一律用 ``runner``）。
        ``resume=True`` 時，同一 run 目錄中先前已通過驗證的 Agent 輸出會以目前的
        Validator 重驗後沿用，不再呼叫 Agent；輸入改變時 envelope 對不上，自然重跑。
        ``max_workers`` 是分析師與多空研究階段同時執行的 Agent 呼叫數上限；預設 1 為串行。
        ``transient_retries`` 是單次 Agent 呼叫遇到 ``TransientAgentError`` 時最多再試的次數，兩次之間等待
        ``retry_delay_seconds``；``retry_deadline``（含時區，通常是交件期限）存在時，等待加上失敗那次的耗時
        會超過期限就不再試。``sleep`` 供測試注入，預設等待可被平行任務的失敗提前喚醒。"""
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= MAX_PARALLEL_WORKERS:
            raise ValueError("max_workers 必須介於 1 與 %d" % MAX_PARALLEL_WORKERS)
        if isinstance(transient_retries, bool) or not isinstance(transient_retries, int) or transient_retries < 0:
            raise ValueError("transient_retries 必須是非負整數")
        if not isinstance(retry_delay_seconds, (int, float)) or isinstance(retry_delay_seconds, bool) or retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds 必須是非負數")
        if retry_deadline is not None and retry_deadline.tzinfo is None:
            raise ValueError("retry_deadline 必須含時區")
        self.root = project_root
        self.run_dir = run_dir
        self.runner = runner
        self.analyst_runner = analyst_runner or runner
        self.research_runner = research_runner or runner
        # 研究員的執行器不能強制 deny_reads（Codex；沒有此屬性者一律視為不強制）時，改由研究階段輸出暫存保證隔離：
        # 多頭與空頭的原始輸出只留記憶體，兩方全部完成才一次寫入 run 目錄（見 ``_staged_research_outputs``）。
        self.last_result: Optional[PipelineResult] = None  # 預期之外的錯誤往上拋之前，保留已花的呼叫紀錄供呼叫端使用
        self._research_staging = not getattr(self.research_runner, "enforces_deny_reads", False)
        self._staged_raw: Optional[List[Tuple[str, Dict[str, object]]]] = None
        self.technical_gate = technical_gate
        # 分析團隊階段算好的基本面指標（沒有財報時 None）；bundle 有 fundamental_period_policy 時封進 team_inputs 2.6。
        self.fundamental_metrics: Optional[Dict[str, object]] = None
        self.decision_repository = decision_repository
        self.max_attempts = max_attempts
        self.log = log
        self.resume = resume
        self.max_workers = max_workers
        self.transient_retries = transient_retries
        self.retry_delay_seconds = float(retry_delay_seconds)
        self.retry_deadline = retry_deadline
        self.calls: List[Dict[str, object]] = []
        self.stage_durations: Dict[str, float] = {}
        self._calls_lock = threading.Lock()
        # 平行任務有一個已確定失敗時設定；其他任務不再進入暫時性重試，等待中的也立即醒來。
        self._abort = threading.Event()
        self._sleep = sleep or self._abort.wait
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
            if any(not future.cancelled() and future.done() and future.exception() is not None for future in futures):
                self._abort.set()
            for future in futures:
                future.cancel()
        position = {name: index for index, (name, _) in enumerate(jobs)}
        with self._calls_lock:
            self.calls[first_call:] = sorted(self.calls[first_call:], key=lambda call: position.get(str(call["name"]), len(jobs)))
        for future in futures:
            if not future.cancelled() and future.exception() is not None:
                raise future.exception()
        return [future.result() for future in futures]

    def _research_residue(self) -> List[Path]:
        """run 目錄中已落地的多空研究員輸出與其後段產物（不含 raw 檔以外的 brief）。"""
        found = {path for pattern in RESEARCH_RESIDUE_GLOBS for path in self.run_dir.glob(pattern)}
        return sorted(found)

    @contextmanager
    def _staged_research_outputs(self, task_names: Sequence[str]) -> Iterator[None]:
        """研究階段輸出暫存：``_research_staging`` 啟用時，階段內各任務的原始輸出只留記憶體，全部成功才一次寫入。

        這是時序隔離：多頭與空頭執行期間，對方的輸出不存在於磁碟，不強制拒讀的執行器（Codex）也讀不到；
        任一任務失敗則整批丟棄，run 目錄不留任何一方的輸出。續跑規則是全有或全無：
        每個任務都有 raw 輸出才沿用（逐一重驗）；完全沒有輸出才重跑；其餘（只有一部分、或後段產物殘留）一律拒絕，
        因為對方輸出已在磁碟上，再啟動研究員就破壞隔離；不搬移舊檔，請建立新 run。
        """
        if not self._research_staging:
            yield
            return
        raw_files = {path for name in task_names for path in self.run_dir.glob("%s_raw_*.json" % name)}
        present = [name for name in task_names if any(self.run_dir.glob("%s_raw_*.json" % name))]
        residue = [path for path in self._research_residue() if path not in raw_files]
        all_present = len(present) == len(task_names) and bool(task_names)
        if self.resume and all_present:
            yield  # 全部有 raw：agent_step 逐一重驗沿用，不啟動任何研究員。
            return
        if present or residue:
            names = [path.name for path in (sorted(raw_files) + residue)][:6]
            raise DailyPipelineError(
                "研究階段輸出暫存要求多空輸出不得先落地，但 run 目錄已有 %s；%s。請建立新的 run（舊輸出保留供稽核）" % (
                    "、".join(names), "續跑只能在全部研究任務都已有輸出時沿用" if self.resume else "非續跑不得重用已有研究輸出的目錄"))
        self._staged_raw = []
        try:
            yield
            staged = self._staged_raw
        except BaseException:
            self._staged_raw = None
            raise
        self._staged_raw = None
        for name, payload in sorted(staged, key=lambda item: item[0]):
            self.save_artifact(name, payload)

    def _save_raw(self, task_name: str, number: int, payload: Mapping[str, object]) -> None:
        name = "%s_raw_%d" % (task_name, number)
        if self._staged_raw is not None and task_name.split("_", 1)[0] in RESEARCH_TASK_PREFIXES:
            with self._calls_lock:
                self._staged_raw.append((name, dict(payload)))
            return
        self.save_artifact(name, payload)

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
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(raw, Mapping):
                        raise ValueError("輸出不是 JSON 物件")
                    # 沿用的輸出也要過秘密外洩檢查：檔案可能是人為放進來的；外洩屬安全拒絕，不跳過、直接停止。
                    reject_secret_leak(task.name, raw, self.root)
                    artifact = build(raw)
                    errors = validate(artifact)
                except (KeyError, TypeError, ValueError, AttributeError, IndexError) as error:
                    # 壞掉或缺欄位的舊輸出視為不可沿用，改看更早的嘗試；都不行就照常重跑，不讓整個 run 失敗。
                    self.log("  [agent] %s 的已存輸出 %s 無法沿用（%s: %s）" % (task.name, path.name, type(error).__name__, str(error)[:100]))
                    continue
                if not errors:
                    self.log("  [agent] %s 沿用已驗證輸出 %s" % (task.name, path.name))
                    return artifact
            if previous and self._staged_raw is None and self._research_staging and task.name.split("_", 1)[0] in RESEARCH_TASK_PREFIXES:
                # 對方的輸出已在磁碟上，重跑這個研究任務會破壞隔離：拒絕，不啟動。
                raise DailyPipelineError("%s 已有的輸出全部驗證失敗，研究階段輸出暫存不允許在對方輸出已落地時重跑；請建立新的 run" % task.name)
        prompt = task.prompt
        errors: List[str] = []
        # 原始輸出只新增、不覆寫，保留每次嘗試供稽核。
        offset = len(previous)
        previous_artifact: Optional[Mapping[str, object]] = None
        for attempt in range(1, self.max_attempts + 1):
            self.log("  [agent] %s 第 %d 次" % (task.name, attempt))
            prefix = task.name.split("_", 1)[0]
            runner = (self.analyst_runner if prefix in ANALYST_TASK_PREFIXES
                      else self.research_runner if prefix in RESEARCH_TASK_PREFIXES else self.runner)
            call, item = self._call_agent(runner, task, prompt, attempt)
            if call.usage_tokens is not None:
                item["usage_tokens"] = call.usage_tokens
            with self._calls_lock:
                self.calls.append(item)
            self._save_raw(task.name, offset + attempt, call.output)
            artifact = build(call.output)
            errors = validate(artifact)
            if previous_artifact is not None:
                # 驗證回饋後的重試：記錄每檔意圖／信心／強度／看法與 claims 數的變化，供事後稽核「因驗證回饋變積極」。
                with self._calls_lock:
                    item["revision_diff"] = output_revision_diff(previous_artifact, artifact)
            previous_artifact = artifact
            if not errors:
                return artifact
            # 每次驗證失敗的錯誤數與前幾項留在執行紀錄，評測時可看出 Agent 在哪裡出錯。
            with self._calls_lock:
                item["validation_errors"] = len(errors)
                item["first_validation_errors"] = [str(error)[:200] for error in errors[:3]]
            # 錯誤可能含 Agent 輸出的外來值：逐條壓成單行並限長，避免偽造新的回饋條目或指示。
            prompt = task.prompt + VALIDATION_FEEDBACK_PREFIX + "\n- ".join(single_line(error) for error in errors[:40])
        raise DailyPipelineError("%s Agent 輸出驗證失敗：%s" % (task.name, "；".join(errors[:10])))

    def _call_agent(
        self, runner: AgentRunner, task: AgentTask, prompt: str, attempt: int,
    ) -> Tuple[AgentCall, Dict[str, object]]:
        """呼叫一次 Agent；``TransientAgentError`` 在重試次數與時間預算內再試。

        每次失敗的嘗試也記入 ``self.calls``（``transient_error``、``transient_retry``、耗時；用量無法取得記 0），
        成功那次另記一筆，所以耗時不遺失、用量不重複。只有 ``TransientAgentError`` 會重試，安全拒絕與其他
        ``DailyPipelineError`` 直接往上拋。"""
        for retry in range(self.transient_retries + 1):
            started = time.monotonic()
            if task.deny_reads and not getattr(runner, "enforces_deny_reads", False) and self._staged_raw is None:
                # 不強制拒讀的執行器只能在研究階段輸出暫存之內執行帶 deny_reads 的任務；其他情形直接拒絕，不重試。
                raise DailyPipelineError("%s 的執行器無法強制拒讀規則，且不在研究階段輸出暫存內，已拒絕啟動" % task.name)
            try:
                call = runner.run(AgentTask(
                    task.name, prompt, task.schema, task.deny_reads, stage_isolated=self._staged_raw is not None,
                ), self.run_dir)
            except TransientAgentError as error:
                duration = round(time.monotonic() - started, 1)
                failed = self._call_item(runner, task, attempt, 0.0, duration)
                failed.update({"transient_error": str(error)[:200], "transient_retry": retry})
                with self._calls_lock:
                    self.calls.append(failed)
                blocker = self._retry_blocker(retry, duration)
                if blocker is not None:
                    self.log("  [agent] %s 暫時性失敗，%s：%s" % (task.name, blocker, error))
                    raise TransientAgentError("%s（%s）" % (error, blocker)) from error
                self.log("  [agent] %s 暫時性失敗，%.0f 秒後重試（第 %d 次重試）：%s" % (
                    task.name, self.retry_delay_seconds, retry + 1, error))
                self._sleep(self.retry_delay_seconds)
                if self._abort.is_set():
                    raise TransientAgentError("%s（其他平行任務已失敗，不再重試）" % error) from error
                continue
            return call, self._call_item(runner, task, attempt, call.estimated_usage_usd, round(time.monotonic() - started, 1))
        raise AssertionError("unreachable")  # pragma: no cover

    def _call_item(self, runner: AgentRunner, task: AgentTask, attempt: int, usage_usd: float, duration: float) -> Dict[str, object]:
        item: Dict[str, object] = {
            "name": task.name, "attempt": attempt, "estimated_usage_usd": usage_usd, "duration_seconds": duration,
        }
        model = getattr(runner, "model", None)
        if model:
            item["model"] = model
        # Skill／指令版本指紋：提示詞指名的 SKILL.md（含連結的 references）逐檔雜湊，與去掉 run 目錄／專案根目錄後的
        # 基礎指令雜湊（不含驗證回饋）。只是執行紀錄，不進 Decision artifact、manifest 或內容雜湊。
        item.update(skill_digest(self.root, task.prompt))
        item["prompt_sha256"] = prompt_sha256(
            task.prompt, {str(self.run_dir): RUN_DIR_PLACEHOLDER, str(self.root): ROOT_PLACEHOLDER}
        )
        return item

    def skill_versions(self) -> Dict[str, object]:
        """本次 run 到目前為止所有 Agent 呼叫讀到的 Skill 檔版本彙整（見 ``skill_digest.skill_versions``）。"""
        with self._calls_lock:
            return skill_versions(list(self.calls))

    def _retry_blocker(self, retry: int, failed_duration: float) -> Optional[str]:
        """不該再試的原因；可以再試時回 None。"""
        if retry >= self.transient_retries:
            return "已重試 %d 次仍失敗" % self.transient_retries if self.transient_retries else "未啟用重試"
        if self._abort.is_set():
            return "其他平行任務已失敗，不再重試"
        if self.retry_deadline is not None:
            # 以失敗那次的耗時估計重試需要的時間；等待加上重跑會超過交件期限就不再試，避免拖過 08:55 才失敗。
            needed = timedelta(seconds=self.retry_delay_seconds + failed_duration)
            if datetime.now(self.retry_deadline.tzinfo) + needed >= self.retry_deadline:
                return "距交件期限 %s 不足以重試" % self.retry_deadline.strftime("%H:%M")
        return None
