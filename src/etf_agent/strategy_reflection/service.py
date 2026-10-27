"""對外應用服務：簡報 → Agent 提案 → 驗證 → 影子回放 → 原子套用／回滾。

套用只由本模組以確定性程式完成；Agent 只輸出提案 JSON。任一檢查失敗都不改設定檔。
"""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

from etf_agent.automation.agent_runner import AgentRunner, AgentTask
from etf_agent.calibration import CalibrationRepository, evaluate
from etf_agent.core import canonical_sha256, parse_aware_time

from .brief import build_brief
from .contracts import (
    MAX_PROPOSALS, POLICY_PATH, SCHEMA_VERSION, STATUS_OBSERVE_ONLY, STATUS_PROPOSED, ReflectionError,
)
from .repository import ReflectionRepository, atomic_write_json
from .tools import apply_proposals, check_proposal, validate_template
from .validator import reflection_sha256, validate_reflection

TAIPEI = timezone(timedelta(hours=8))
Replayer = Callable[[Path, str], Mapping[str, object]]
# 自動套用不得落在 20:50～隔天 09:00：21:00 備案與 07:00 重做要讀到同一份策略樣板。
APPLY_BLOCKED_FROM = 20 * 60 + 50
APPLY_BLOCKED_UNTIL = 9 * 60


@contextmanager
def _no_lock():
    yield


def apply_window_blocked(moment: datetime) -> bool:
    minutes = moment.astimezone(TAIPEI).hour * 60 + moment.astimezone(TAIPEI).minute
    return minutes >= APPLY_BLOCKED_FROM or minutes < APPLY_BLOCKED_UNTIL

_TEXT = {"type": "string", "minLength": 1}
_IDS = {"type": "array", "items": _TEXT, "minItems": 1}
REFLECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "observations", "proposals"],
    "properties": {
        "summary": _TEXT,
        "observations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "evidence_ids"],
            "properties": {"text": _TEXT, "evidence_ids": _IDS}}},
        "proposals": {"type": "array", "maxItems": MAX_PROPOSALS, "items": {
            "type": "object", "additionalProperties": False, "required": ["knob", "proposed", "rationale", "evidence_ids"],
            "properties": {"knob": _TEXT, "proposed": {"type": ["string", "number"]}, "rationale": _TEXT, "evidence_ids": _IDS}}},
    },
}


def _read_json(path: Path, label: str) -> Dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReflectionError("%s 無法讀取：%s" % (label, error)) from error
    if not isinstance(data, dict):
        raise ReflectionError("%s 必須是 JSON 物件" % label)
    return data


def load_policy_and_rules(root: Path):
    return _read_json(root / POLICY_PATH, "策略樣板"), _read_json(root / "config" / "decision_rules.json", "決策規則")


def make_brief(root: Path, database: Path, as_of: str) -> Dict[str, object]:
    template, _ = load_policy_and_rules(root)
    report = evaluate(CalibrationRepository(root / "artifacts" / "calibration"), database, as_of)
    return build_brief(report, template, as_of)


def agent_task(brief_path: Path) -> AgentTask:
    prompt = (
        "你是策略反思員。先用 Read 讀 tradeagent/risk/strategy-reflection/SKILL.md，再讀 %s（唯一資料來源）。\n"
        "只根據簡報 evidence 裡的統計，找出目前策略樣板旋鈕的可改進處；樣本不足、統計互相矛盾或看不出方向時，proposals 給空陣列。\n"
        "規則：每則 observations 與 proposals 都必須引用簡報內存在的 evidence_ids；knob 只能是簡報 knobs 列出的路徑；"
        "proposed 是新數值（字串），單次變幅不得超過程式上限（乘數 0.25、現金 0.03、檔數 2、回撤門檻 0.01 且只准調低、"
        "volatility_floor 0.005、trim_fraction 0.1）；最多 %d 筆提案；不得提出簡報以外的欄位、權重、股數或風控規則。"
        "不確定就不要提案。" % (brief_path, MAX_PROPOSALS)
    )
    return AgentTask("strategy_reflection", prompt, REFLECTION_SCHEMA)


def _base(brief: Mapping[str, object], status: str, now: datetime, summary: str, observations: List, proposals: List) -> Dict[str, object]:
    reflection: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "reflection_id": "reflection-%s" % now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "as_of": brief["as_of"], "brief_sha256": brief["brief_sha256"],
        "base_policy_id": brief["base_policy_id"], "base_policy_sha256": brief["base_policy_sha256"],
        "decision_days": brief["decision_days"], "status": status, "summary": summary,
        "observations": observations, "proposals": proposals, "citable": False,
    }
    reflection["content_sha256"] = reflection_sha256(reflection)
    return reflection


def observe_only(brief: Mapping[str, object], now: datetime) -> Dict[str, object]:
    summary = "決策日 %s／%s，樣本不足或沒有可用 horizon，只觀察、不提案。" % (brief["decision_days"], brief["required_decision_days"])
    return _base(brief, STATUS_OBSERVE_ONLY, now, summary, [], [])


def from_agent_output(
    output: Mapping[str, object], brief: Mapping[str, object], template: Mapping[str, object], rules: Mapping[str, object], now: datetime,
) -> Dict[str, object]:
    """程式補上 current／proposed 的重算值與雜湊；Agent 不能設定狀態或數值格式。"""
    proposals = []
    for item in output.get("proposals", []):
        checked = check_proposal(str(item["knob"]), item["proposed"], template, rules)
        proposals.append({"knob": str(item["knob"]), "current": checked["current"], "proposed": checked["proposed"],
                          "rationale": str(item["rationale"]), "evidence_ids": list(item["evidence_ids"])})
    observations = [{"text": str(item["text"]), "evidence_ids": list(item["evidence_ids"])} for item in output.get("observations", [])]
    status = STATUS_PROPOSED if proposals else STATUS_OBSERVE_ONLY
    return _base(brief, status, now, str(output["summary"]), observations, proposals)


def save_reflection(repository: ReflectionRepository, reflection: Mapping[str, object], brief: Mapping[str, object]) -> str:
    run_id = str(reflection["reflection_id"])
    repository.save(run_id, {"brief": brief, "reflection": reflection})
    return run_id


def load_reflection(repository: ReflectionRepository, reflection_id: str):
    run_dir = repository.run_dir(reflection_id)
    if run_dir is None:
        raise ReflectionError("找不到反思 %s" % reflection_id)
    repository.verify(reflection_id)
    return _read_json(run_dir / "reflection.json", "reflection"), _read_json(run_dir / "brief.json", "brief")


def replay_gate(base: Mapping[str, object], candidate: Mapping[str, object]) -> Dict[str, object]:
    """候選策略回放不得比目前策略差：兩邊都完成、期末 NAV 不降、Guard 拒絕日不增加。回放只是必要條件，不是獲利證明。"""
    reasons: List[str] = []
    for label, body in (("目前策略", base), ("候選策略", candidate)):
        if body.get("status") != "completed":
            reasons.append("%s 回放未完成（%s）" % (label, body.get("status")))
    if not reasons:
        if int(candidate["metrics"].get("trading_days") or 0) < 1:
            reasons.append("回放沒有完成任何成交日")
        if Decimal(str(candidate["metrics"]["end_nav"])) < Decimal(str(base["metrics"]["end_nav"])):
            reasons.append("候選策略期末 NAV 低於目前策略")
        rejected = lambda body: sum(1 for day in body["days"] if day.get("decision_status") == "rejected")  # noqa: E731
        if rejected(candidate) > rejected(base):
            reasons.append("候選策略 Guard 拒絕日增加")
    return {"passed": not reasons, "reasons": reasons}


def default_replayer(root: Path, database: Path, decisions: Path, as_of: str) -> Replayer:
    from etf_agent.evaluation import build_replay, save_replay

    def replay(policy_path: Path, replay_id: str) -> Mapping[str, object]:
        body, artifacts = build_replay(root, policy_path, decision_root=decisions, database=database, as_of=as_of, allow_gaps=True)
        save_replay(root, body, artifacts, replay_id)
        return body

    return replay


def apply_reflection(
    root: Path, reflection_id: str, replayer: Replayer, now: datetime, repository: Optional[ReflectionRepository] = None,
    write_lock: Optional[Callable[[], object]] = None, clock: Optional[Callable[[], datetime]] = None, enforce_window: bool = False,
) -> Dict[str, object]:
    """驗證 → 影子回放 → 原子寫入設定檔。全有或全無；失敗不改設定檔，只回傳原因。

    回放與 Agent 可能很久，所以鎖只在最後寫入時取（避免佔住 artifacts/daily/.lock 讓 21:00 決策被略過）；
    取鎖後重讀樣板確認沒被改動，``enforce_window`` 時再確認不在決策窗口內。"""
    repository = repository or ReflectionRepository(root)
    reflection, brief = load_reflection(repository, reflection_id)
    template, rules = load_policy_and_rules(root)
    history = repository.history()
    validate_reflection(reflection, brief, template, rules, history)
    if reflection["status"] != STATUS_PROPOSED:
        return {"applied": False, "reason": "沒有提案"}
    candidate = apply_proposals(template, reflection["proposals"], rules)  # type: ignore[arg-type]
    today = now.astimezone(TAIPEI)
    if _applied_today(history, today):
        return {"applied": False, "reason": "今天已套用過一次，明天再調"}
    work = repository.base / "replay_inputs" / reflection_id
    work.mkdir(parents=True, exist_ok=True)
    base_path, candidate_path = work / "policy_base.json", work / "policy_candidate.json"
    atomic_write_json(base_path, template)
    atomic_write_json(candidate_path, candidate)  # 回放用副本保留原 available_at，live 檔才換新的
    base_replay = replayer(base_path, "%s-base" % reflection_id)
    candidate_replay = replayer(candidate_path, "%s-candidate" % reflection_id)
    gate = replay_gate(base_replay, candidate_replay)
    if not gate["passed"]:
        return {"applied": False, "reason": "影子回放未通過：" + "；".join(gate["reasons"])}
    try:
        with (write_lock or _no_lock)():
            moment = (clock or (lambda: now))()
            if enforce_window and apply_window_blocked(moment):
                return {"applied": False, "reason": "已過 20:50（或尚未到 09:00），為避免 21:00 與 07:00 決策用到不同策略，反思已封存，改天再套用"}
            current, _ = load_policy_and_rules(root)
            history = repository.history()
            if canonical_sha256(current) != reflection["base_policy_sha256"]:
                return {"applied": False, "reason": "策略樣板在回放期間已被改動，反思已過期"}
            if _applied_today(history, today):
                return {"applied": False, "reason": "今天已套用過一次，明天再調"}
            live = dict(candidate)
            live["policy_id"] = "strategy-%s-r%d" % (today.date().isoformat(), 1)
            live["available_at"] = today.isoformat()
            parse_aware_time(live["available_at"], "available_at", error=ReflectionError)
            validate_template(live, rules)
            backup = repository.policy_backups / ("%s.json" % template["policy_id"])
            repository.policy_backups.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(root / POLICY_PATH, backup)
            atomic_write_json(root / POLICY_PATH, live)
            repository.append_history({
                "action": "apply", "applied_at": today.isoformat(), "reflection_id": reflection_id,
                "knobs": [item["knob"] for item in reflection["proposals"]], "decision_days": brief["decision_days"],  # type: ignore[union-attr]
                "policy_id_before": template["policy_id"], "policy_id_after": live["policy_id"], "backup": str(backup),
                "policy_sha256_after": canonical_sha256(live),
            })
    except BlockingIOError:
        return {"applied": False, "reason": "每日決策進行中（artifacts/daily/.lock 被佔用），反思已封存，之後可用 apply 再套用"}
    return {"applied": True, "policy_id": live["policy_id"], "knobs": [item["knob"] for item in reflection["proposals"]]}  # type: ignore[union-attr]


def _applied_today(history, today: datetime) -> bool:
    return any(entry.get("action") == "apply" and str(entry.get("applied_at", "")).startswith(today.date().isoformat())
               for entry in history)


def rollback(root: Path, now: datetime, repository: Optional[ReflectionRepository] = None) -> Dict[str, object]:
    """還原最近一次尚未回滾的套用：把備份的舊設定檔原樣寫回。"""
    repository = repository or ReflectionRepository(root)
    stack: List[Mapping[str, object]] = []
    for entry in repository.history():
        if entry.get("action") == "apply":
            stack.append(entry)
        elif entry.get("action") == "rollback" and stack:
            stack.pop()
    if not stack:
        raise ReflectionError("沒有可回滾的套用紀錄")
    target = stack[-1]
    backup = Path(str(target["backup"]))
    restored = _read_json(backup, "備份策略樣板")
    live = _read_json(root / POLICY_PATH, "目前策略樣板")
    if live.get("policy_id") != target["policy_id_after"] or canonical_sha256(live) != target["policy_sha256_after"]:
        raise ReflectionError("目前設定檔 %s 不是該次套用的結果 %s（或內容已被手動修改），請人工處理" % (live.get("policy_id"), target["policy_id_after"]))
    atomic_write_json(root / POLICY_PATH, restored)
    repository.append_history({"action": "rollback", "rolled_back_at": now.astimezone(TAIPEI).isoformat(),
                               "of": target["reflection_id"], "policy_id_restored": restored.get("policy_id")})
    return {"rolled_back": target["reflection_id"], "policy_id": restored.get("policy_id")}


def run_auto(
    root: Path, database: Path, decisions: Path, runner: Optional[AgentRunner], now: datetime,
    replayer: Optional[Replayer] = None, apply: bool = True, write_lock: Optional[Callable[[], object]] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> Dict[str, object]:
    """每日自動流程。樣本不足時完全不呼叫 Agent（零用量）；提案不過驗證或回放就整份不套用。"""
    repository = ReflectionRepository(root)
    as_of = now.astimezone(timezone.utc).isoformat()
    brief = make_brief(root, database, as_of)
    template, rules = load_policy_and_rules(root)
    if not brief["gate_open"]:
        reflection = observe_only(brief, now)
        save_reflection(repository, reflection, brief)
        return {"status": STATUS_OBSERVE_ONLY, "reflection_id": reflection["reflection_id"], "reason": reflection["summary"]}
    if runner is None:
        raise ReflectionError("樣本足夠但沒有提供 Agent runner")
    run_dir = repository.base / "agent_inputs" / now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    atomic_write_json(run_dir / "brief.json", brief)
    call = runner.run(agent_task(run_dir / "brief.json"), run_dir)
    atomic_write_json(run_dir / "agent_output_raw.json", call.output)  # 先留底，之後被拒絕也查得到 Agent 原話
    try:
        reflection = from_agent_output(call.output, brief, template, rules, now)
        validate_reflection(reflection, brief, template, rules, repository.history())
    except ReflectionError as error:
        atomic_write_json(run_dir / "rejected.json", {"reason": str(error)})
        raise
    save_reflection(repository, reflection, brief)
    result: Dict[str, object] = {"status": reflection["status"], "reflection_id": reflection["reflection_id"],
                                 "proposals": len(reflection["proposals"]), "estimated_usage_usd": call.estimated_usage_usd}  # type: ignore[arg-type]
    if apply and reflection["status"] == STATUS_PROPOSED:
        result["apply"] = apply_reflection(root, str(reflection["reflection_id"]),
                                           replayer or default_replayer(root, database, decisions, as_of), now, repository,
                                           write_lock=write_lock, clock=clock or (lambda: datetime.now(timezone.utc)), enforce_window=True)
    return result
