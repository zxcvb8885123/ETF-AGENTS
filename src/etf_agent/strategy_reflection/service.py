"""對外應用服務：簡報 → Agent 提案 → 驗證 → 影子回放 → 原子套用／回滾。

套用只由本模組以確定性程式完成；Agent 只輸出提案 JSON。任一檢查失敗都不改設定檔。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.automation.agent_runner import AgentRunner, AgentTask, DailyPipelineError
from etf_agent.calibration import CalibrationRepository, evaluate
from etf_agent.calibration.contracts import CalibrationError
from etf_agent.calibration.prices import OfficialPriceBook
from etf_agent.calibration.service import _latest_per_entry_day
from etf_agent.core import canonical_sha256, parse_aware_time

from .brief import build_brief
from .diagnostics import build_diagnostics, load_runs
from etf_agent.decision.strategy_lessons import MAX_LESSONS, load_strategy_lessons
from etf_agent.decision.thesis_verdicts import VERDICTS_PATH, seal_verdicts

from .lessons import (
    LESSONS_ACTION, LESSONS_SCHEMA, MIN_GROUP, REVERT_ACTION, apply_lessons, holdings_count, lessons_brief_for_agent,
    lessons_from_output, load_pending, revert_lessons, save_pending,
)
from .thesis import DEFAULT_TOLERANCE, build_thesis_tracking
from .thesis_review import REVIEW_SCHEMA, batches, current_context, review_briefs, unknown_items, validate_review
from .report import render_weekly_report, write_weekly_report
from .contracts import (
    AUTO_DISABLE_REVERTS, KNOBS, MAX_PROPOSALS, NEEDS_HUMAN_ACTION, POLICY_PATH, SCHEMA_VERSION, STATUS_OBSERVE_ONLY, STATUS_PROPOSED, SWITCH_PATH,
    REPLAY_BENCHMARK_ID, REPLAY_BENCHMARK_MIN_COVERAGE, REPLAY_EVIDENCE_GRADE, REPLAY_MIN_GAIN, REPLAY_MIN_SEGMENTS, REPLAY_NOTICE,
    TRIAL_DAYS, TRIAL_MAX_CALENDAR_DAYS, VERIFICATION_OBSERVE, ReflectionError,
)
from .repository import ReflectionRepository, atomic_write_json
from .tools import (
    apply_proposals, check_proposal, gated_cash_buffer_knobs, knob_risk_directions, non_risk_reducing, validate_template,
)
from .trial import (
    consecutive_reverts, evaluate_trial, excess_series, extensions, open_trial, trial_verification,
)
from .validator import reflection_sha256, validate_reflection

TAIPEI = timezone(timedelta(hours=8))
Replayer = Callable[[Path, str], Mapping[str, object]]
# 給一串回放日期（含起點），回傳等權基準 NAV（{日期: NAV}）；基準不可用時回空字典，閘門 fail-closed。
Benchmarker = Callable[[Sequence[str]], Mapping[str, Decimal]]
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


def make_brief(root: Path, database: Path, as_of: str, decisions: Optional[Path] = None) -> Dict[str, object]:
    template, _ = load_policy_and_rules(root)
    report = evaluate(CalibrationRepository(root / "artifacts" / "calibration"), database, as_of)
    diagnostics = build_diagnostics(decisions or root / "artifacts" / "decisions", database, as_of, loss_tolerance(template))
    diagnostics["benchmark"] = benchmark_diagnostics(root, database, as_of)
    return build_brief(report, template, as_of, diagnostics, ReflectionRepository(root).history())


def loss_tolerance(template: Mapping[str, object]) -> Decimal:
    """論點追蹤的虧損容忍度：沿用 policy 的 ``position_review.loss_alert``，沒有就用預設 10%。"""
    review = template.get("position_review")
    try:
        return Decimal(str(review["loss_alert"])) if isinstance(review, Mapping) and review.get("loss_alert") else DEFAULT_TOLERANCE
    except ArithmeticError:
        return DEFAULT_TOLERANCE


def weekly_thesis(root: Path, database: Path, as_of: str, decisions: Optional[Path] = None) -> List[Dict[str, object]]:
    """週報用的完整論點追蹤（含當初的買進理由與重估條件文字，給人閱讀；不進簡報）。任何資料缺漏都回空清單。"""
    return thesis_inputs(root, database, as_of, decisions)[0]


def thesis_inputs(root: Path, database: Path, as_of: str, decisions: Optional[Path] = None):
    """(論點追蹤列, 最新決策日各股票可引用的發現與 claim)。任何資料缺漏都回 ([], {})。"""
    template, _ = load_policy_and_rules(root)
    try:
        runs = load_runs(decisions or Path(root) / "artifacts" / "decisions")
        book = OfficialPriceBook(Path(database), as_of)
        cutoff = parse_aware_time(as_of, "as_of", error=ReflectionError)
        runs = [run for run in runs if parse_aware_time(run["decision_cutoff"], "decision_cutoff", error=ReflectionError) <= cutoff]
        if not runs:
            return [], {}
        chosen = _latest_per_entry_day(runs, book)[0]
        rows = build_thesis_tracking(chosen, book, loss_tolerance(template))
        return rows, current_context(Path(chosen[max(chosen)]["run_dir"]))
    except (CalibrationError, ReflectionError, OSError, ValueError, sqlite3.Error, KeyError):
        return [], {}


def review_thesis(
    runner: Optional[AgentRunner], run_dir: Path, thesis: List[Dict[str, object]], context: Mapping[str, Mapping[str, object]],
) -> Dict[str, Dict[str, object]]:
    """逐檔論點檢視（只出報告）。沒有 runner、Agent 失敗或輸出沒過驗證的批次一律標 unknown 並寫明原因，
    不中斷反思流程；結果存成 ``thesis_review.json``，不進簡報也不進 policy；可用結論另寫 ``thesis_verdicts.json`` 給交易 Agent 當背景。"""
    briefs = review_briefs(thesis, context)
    results: List[Dict[str, object]] = []
    for index, batch in enumerate(batches(briefs)):
        if runner is None:
            results.extend(unknown_items(batch, "沒有提供 Agent，未檢視"))
            continue
        path = run_dir / ("thesis_review_b%d.json" % index)
        atomic_write_json(path, {"items": batch})
        prompt = (
            "你是 $strategy-reflection 策略反思員，這次做論點檢視（不提旋鈕）。先用 Read 讀 tradeagent/risk/strategy-reflection/SKILL.md 與 tradeagent/risk/strategy-reflection/references/thesis-review.md，再讀 %s（唯一資料來源）。"
            "對 items 裡每一檔股票，逐條對照「當初的重估條件」與該檔 latest_findings／latest_claims，判斷 verdict 與每個條件的狀態；"
            "只能引用該檔 allowed_ids；價格事實只是背景，短期波動不是理由失效；不確定就標 unknown。" % path
        )
        try:
            call = runner.run(AgentTask("thesis_review", prompt, REVIEW_SCHEMA), run_dir)
            atomic_write_json(run_dir / ("thesis_review_b%d_raw.json" % index), call.output)
            errors = validate_review(call.output, batch)
        except (DailyPipelineError, ReflectionError, KeyError, TypeError) as error:
            results.extend(unknown_items(batch, "論點檢視失敗：%s" % error))
            continue
        results.extend(call.output["items"] if not errors else unknown_items(batch, "論點檢視輸出未通過驗證：%s" % "；".join(errors[:2])))  # type: ignore[arg-type]
    atomic_write_json(run_dir / "thesis_review.json", {"items": results})
    return {str(item["symbol"]).upper(): item for item in results}


def reflect_lessons(
    runner: Optional[AgentRunner], run_dir: Path, base: Path, thesis: List[Dict[str, object]],
    review: Mapping[str, Mapping[str, object]], reflection_brief: Mapping[str, object], now: datetime, root: Path,
) -> Dict[str, object]:
    """反思 Agent 回頭看過去買進的股票，歸納最多 3 條做法建議（教訓）。只封存提案，週六由 ``apply_pending`` 套用。

    校準樣本門檻未通過（``gate_open`` 不是 True，缺欄位也算）或持股少於 ``MIN_GROUP`` 檔時不呼叫 Agent。
    失敗（沒有 Agent、逾時、輸出不合格）只回原因，不中斷反思；與目前教訓相同或沒有教訓時本週不變更。"""
    if runner is None:
        return {"status": "skipped", "reason": "沒有提供 Agent"}
    if reflection_brief.get("gate_open") is not True:
        return {"status": "skipped", "reason": "校準樣本門檻未達（gate 關閉），本週不整理教訓"}
    holdings = holdings_count(thesis)
    if holdings < MIN_GROUP:
        return {"status": "skipped", "reason": "持股 %d 檔少於 %d 檔，樣本太少，本週不整理教訓" % (holdings, MIN_GROUP)}
    evidence = reflection_brief["evidence"]
    brief = lessons_brief_for_agent(thesis, review, evidence, load_strategy_lessons(root))  # type: ignore[arg-type]
    if not any(key.startswith("thesis.") for key in brief["evidence"]):  # type: ignore[union-attr]
        return {"status": "skipped", "reason": "沒有可歸納的持股統計"}
    path = run_dir / "lessons_brief.json"
    atomic_write_json(path, brief)
    prompt = (
        "你是 $strategy-reflection 策略反思員，這次回頭檢視過去買進的股票並整理經驗教訓（不提旋鈕）。先用 Read 讀 "
        "tradeagent/risk/strategy-reflection/SKILL.md 與 tradeagent/risk/strategy-reflection/references/lessons.md，再讀 %s（唯一資料來源）。"
        "最多 %d 條一般性做法建議；每條引用簡報 evidence 內存在的 evidence_id；統計看不出方向或樣本太少就給空陣列。" % (path, MAX_LESSONS)
    )
    try:
        call = runner.run(AgentTask("lessons", prompt, LESSONS_SCHEMA), run_dir)
        atomic_write_json(run_dir / "lessons_raw.json", call.output)
        sealed = lessons_from_output(call.output, brief, now)
    except (DailyPipelineError, ReflectionError, KeyError, TypeError) as error:
        return {"status": "failed", "reason": str(error)}
    if sealed is None:
        return {"status": "unchanged", "reason": "沒有新的教訓，或與目前相同"}
    sample = {"gate_open": True, "holdings": holdings, "decision_days": int(reflection_brief["decision_days"])}  # type: ignore[call-overload]
    save_pending(base, sealed, sample)
    return {"status": "proposed", "lessons_id": sealed["lessons_id"], "holdings": holdings,
            "texts": [item["text"] for item in sealed["lessons"]]}  # type: ignore[union-attr]


def _current_lesson_texts(root: Path) -> List[str]:
    current = load_strategy_lessons(root)
    return list(current["texts"]) if current else []  # type: ignore[arg-type]


def knob_limits_text() -> str:
    """單次變幅上限由 ``KNOBS`` 生成，避免提示與程式各寫一份。"""
    parts = []
    for path, knob in sorted(KNOBS.items()):
        parts.append("%s（相鄰一階）" % path if knob.kind == "enum" else "%s ±%s" % (path, knob.step.normalize()))
    return "；".join(parts)


def agent_task(brief_path: Path) -> AgentTask:
    prompt = (
        "你是策略反思員。先用 Read 讀 tradeagent/risk/strategy-reflection/SKILL.md，再讀 %s（唯一資料來源）。\n"
        "只根據簡報 evidence 裡的統計（cal.h* 校準、diag.* 每週診斷），找出目前策略樣板旋鈕的可改進處；"
        "樣本不足、統計互相矛盾或看不出方向時，proposals 給空陣列。\n"
        "規則：每則 observations 與 proposals 都必須引用簡報內存在的 evidence_ids；knob 只能是簡報 knobs 列出的路徑；"
        "proposed 是新數值（字串；列舉旋鈕給選項名稱），單次變幅不得超過程式上限（%s；回撤門檻只准調低），"
        "且離人設定的起點最多偏離 2 個 step；最多 %d 筆提案；position_review.* 一週最多提一個；"
        "不得提出簡報以外的欄位、權重、股數或風控規則。不確定就不要提案。\n"
        "風險方向：簡報 knob_risk_directions 標出每個旋鈕往哪個方向是降風險；只有降風險方向的提案可由程式自動套用，"
        "加風險或方向不明的提案只會封存並標 needs_human、等人決定，所以提加風險方向時必須在 rationale 寫明為何值得人工審。\n"
        "證據等級：簡報裡的校準統計與影子回放都只是對舊日期的重算（%s），不是對你或交易 Agent 判斷的前瞻驗證，不要把它當成已被證明的優勢。"
        % (brief_path, knob_limits_text(), MAX_PROPOSALS, REPLAY_NOTICE)
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


def observe_only(brief: Mapping[str, object], now: datetime, reason: Optional[str] = None) -> Dict[str, object]:
    summary = reason or "決策日 %s／%s，樣本不足或沒有可用 horizon，只觀察、不提案。" % (brief["decision_days"], brief["required_decision_days"])
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


def _nav_points(body: Mapping[str, object]) -> List[Tuple[str, Decimal]]:
    return [(str(point["trade_date"]), Decimal(str(point["nav"]))) for point in body.get("nav_series") or []]


def _excess(navs: Mapping[str, Decimal], benchmark: Mapping[str, Decimal], start: str, end: str) -> Decimal:
    """區間 (start, end] 的「策略報酬 − 等權基準報酬」。"""
    return (navs[end] / navs[start] - 1) - (benchmark[end] / benchmark[start] - 1)


def replay_segments(dates: Sequence[str], count: int = REPLAY_MIN_SEGMENTS) -> List[Tuple[str, str]]:
    """把 n 個 NAV 點（n−1 個成交日）切成 ``count`` 段不重疊子區間（每段至少一個成交日）；切不出來回空清單。"""
    intervals = len(dates) - 1
    if intervals < count:
        return []
    bounds = [round(index * intervals / count) for index in range(count + 1)]
    return [(dates[bounds[index]], dates[bounds[index + 1]]) for index in range(count)]


def _rate(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001")))


def replay_gate(base: Mapping[str, object], candidate: Mapping[str, object], benchmark: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    """候選策略回放要有改善，比的是**對等權基準的超額**而不是絕對 NAV（否則自動套用等於在賭市場 beta）：

    - 兩邊都完成、至少一個成交日、Guard 拒絕日不增加；
    - 整段：候選對等權基準的超額至少比目前策略高 ``REPLAY_MIN_GAIN``（回放與提案用同一段歷史，持平不算）；
    - 切成 ``REPLAY_MIN_SEGMENTS`` 段不重疊子區間，候選在每一段都不劣於等權基準（超額 ≥ 0）、也不劣於目前策略；
      成交日切不出這麼多段、或基準缺任一回放日期的估值，一律不通過（不以單段或絕對 NAV 放行）。

    回放只固定已封存的 Agent 意圖重算配置（``REPLAY_EVIDENCE_GRADE``），是必要條件，不是獲利證明。

    揭露鍵（不影響 ``passed``／``reasons``／``segments``）：``in_sample`` 固定為 True（回放與提案用同一段已看過的歷史，
    不是樣本外驗證）；``available_at_overridden_days`` 為候選回放中策略樣板 ``available_at`` 被改寫成當日 cutoff 的天數。"""
    reasons: List[str] = []
    segments: List[Dict[str, object]] = []
    for label, body in (("目前策略", base), ("候選策略", candidate)):
        if body.get("status") != "completed":
            reasons.append("%s 回放未完成（%s）" % (label, body.get("status")))
    if not reasons:
        if int(candidate["metrics"].get("trading_days") or 0) < 1:
            reasons.append("回放沒有完成任何成交日")
        rejected = lambda body: sum(1 for day in body["days"] if day.get("decision_status") == "rejected")  # noqa: E731
        if rejected(candidate) > rejected(base):
            reasons.append("候選策略 Guard 拒絕日增加")
        base_points, candidate_points = _nav_points(base), _nav_points(candidate)
        dates = [day for day, _ in candidate_points]
        bench = {str(day): Decimal(str(value)) for day, value in (benchmark or {}).items()}
        if dates != [day for day, _ in base_points] or len(dates) != len(set(dates)):
            reasons.append("兩次回放的 NAV 日期不對齊，無法比較")
        elif not replay_segments(dates):
            reasons.append("回放只有 %d 個成交日，切不出 %d 段不重疊子區間，不以單段結果放行" % (max(len(dates) - 1, 0), REPLAY_MIN_SEGMENTS))
        elif any(day not in bench or bench[day] <= 0 for day in dates):
            reasons.append("等權基準（%s）缺少回放日期的估值：%s" % (
                REPLAY_BENCHMARK_ID, "、".join(day for day in dates if day not in bench or bench[day] <= 0)))
        else:
            base_nav, candidate_nav = dict(base_points), dict(candidate_points)
            overall_base = _excess(base_nav, bench, dates[0], dates[-1])
            overall_candidate = _excess(candidate_nav, bench, dates[0], dates[-1])
            if overall_candidate < overall_base + REPLAY_MIN_GAIN:
                reasons.append("候選策略整段對等權基準的超額 %s 沒有比目前策略 %s 高出 %s（持平或更低）" % (
                    _rate(overall_candidate), _rate(overall_base), REPLAY_MIN_GAIN))
            for index, (start, end) in enumerate(replay_segments(dates), start=1):
                candidate_excess, base_excess = _excess(candidate_nav, bench, start, end), _excess(base_nav, bench, start, end)
                segments.append({"segment": index, "from": start, "to": end, "candidate_excess": _rate(candidate_excess), "base_excess": _rate(base_excess)})
                if candidate_excess < 0:
                    reasons.append("第 %d 段（%s～%s）候選策略劣於等權基準（超額 %s）" % (index, start, end, _rate(candidate_excess)))
                if candidate_excess < base_excess:
                    reasons.append("第 %d 段（%s～%s）候選策略劣於目前策略（超額 %s < %s）" % (index, start, end, _rate(candidate_excess), _rate(base_excess)))
    return {"passed": not reasons, "reasons": reasons, "benchmark_id": REPLAY_BENCHMARK_ID, "segments": segments,
            "evidence_grade": REPLAY_EVIDENCE_GRADE, "notice": REPLAY_NOTICE,
            "in_sample": True, "available_at_overridden_days": _overridden_days(candidate)}


def _overridden_days(replay: Mapping[str, object]) -> int:
    """回放中策略樣板建立時間被改寫成當日 cutoff 的天數；沒有 ``policy`` 註記的舊回放資料算 0，不拋例外。"""
    days = replay.get("days")
    if not isinstance(days, (list, tuple)):
        return 0
    return sum(1 for day in days if isinstance(day, Mapping) and isinstance(day.get("policy"), Mapping) and day["policy"].get("available_at_overridden") is True)


def default_replayer(root: Path, database: Path, decisions: Path, as_of: str) -> Replayer:
    from etf_agent.evaluation import build_replay, save_replay

    def replay(policy_path: Path, replay_id: str) -> Mapping[str, object]:
        body, artifacts = build_replay(root, policy_path, decision_root=decisions, database=database, as_of=as_of, allow_gaps=True)
        save_replay(root, body, artifacts, replay_id)
        return body

    return replay


def equal_weight_benchmark(root: Path, database: Path, as_of: str, dates: Sequence[str]) -> Dict[str, Decimal]:
    """交易池等權（每日再平衡）基準 NAV，起點 1，只用 ``as_of`` 前抓到的官方收盤價。

    相鄰兩個日期兩天都有官方收盤的股票不足 ``REPLAY_BENCHMARK_MIN_COVERAGE`` 時視為基準不可用，回空字典（閘門不通過），不補值。
    交易池沿用 ``config/benchmarks.json`` 等權基準的 universe（沒有就用 ``data/official_universe.csv``）。"""
    from etf_agent.data.benchmarks import BENCHMARK_KIND_EQUAL_WEIGHT, BenchmarkConfigError, load_benchmarks
    from etf_agent.data.universe import UniverseLoader
    from etf_agent.virtual_account import VirtualAccountError
    from etf_agent.virtual_account.daily import OfficialCloseMarket

    if not dates:
        return {}
    try:
        universe_path = "data/official_universe.csv"
        benchmarks_path = Path(root) / "config" / "benchmarks.json"
        if benchmarks_path.exists():
            definition = next((item for item in load_benchmarks(benchmarks_path) if item.kind == BENCHMARK_KIND_EQUAL_WEIGHT), None)
            if definition is not None and definition.universe_path:
                universe_path = definition.universe_path
        symbols = sorted({item.symbol for item in UniverseLoader().load(Path(root) / universe_path)})
        market = OfficialCloseMarket(Path(database))
        closes = lambda day: {symbol: Decimal(str(row["close_price"])) for symbol, row in market.quotes(day, as_of).items() if symbol in symbols}  # noqa: E731
        nav = Decimal(1)
        series = {str(dates[0]): nav}
        previous = closes(str(dates[0]))
        for day in dates[1:]:
            current = closes(str(day))
            common = [symbol for symbol in symbols if symbol in previous and symbol in current and previous[symbol] > 0]
            if not symbols or Decimal(len(common)) < REPLAY_BENCHMARK_MIN_COVERAGE * Decimal(len(symbols)):
                return {}
            nav *= sum((current[symbol] / previous[symbol] for symbol in common), Decimal(0)) / Decimal(len(common))
            series[str(day)] = nav
            previous = current
        return series
    except (BenchmarkConfigError, VirtualAccountError, OSError, ValueError, ArithmeticError, sqlite3.Error):
        return {}


def default_benchmarker(root: Path, database: Path, as_of: str) -> Benchmarker:
    return lambda dates: equal_weight_benchmark(root, database, as_of, dates)


def _record_needs_human(repository: ReflectionRepository, reflection_id: str, blocked: Mapping[str, str], now: datetime) -> None:
    """加風險／方向不明的提案只封存並標 needs_human（同一份反思只記一次）；歷史裡其他邏輯都依 action 過濾，不受影響。"""
    if any(entry.get("action") == NEEDS_HUMAN_ACTION and entry.get("reflection_id") == reflection_id for entry in repository.history()):
        return
    repository.append_history({"action": NEEDS_HUMAN_ACTION, "reflection_id": reflection_id,
                               "at": now.astimezone(TAIPEI).isoformat(), "knobs": dict(blocked)})


def apply_reflection(
    root: Path, reflection_id: str, replayer: Replayer, now: datetime, repository: Optional[ReflectionRepository] = None,
    write_lock: Optional[Callable[[], object]] = None, clock: Optional[Callable[[], datetime]] = None, enforce_window: bool = False,
    benchmarker: Optional[Benchmarker] = None, allow_risk_increase: bool = False,
) -> Dict[str, object]:
    """驗證 → 風險方向 → 影子回放 → 原子寫入設定檔。全有或全無；失敗不改設定檔，只回傳原因。

    只有全部提案都是降風險方向才會自動套用；含加風險或方向不明的提案時只封存並標 ``needs_human``，
    要人以 ``allow_risk_increase=True``（CLI ``--allow-risk-increase``）明確放行才繼續。
    ``benchmarker`` 提供回放日期的等權基準 NAV；沒有提供或基準不可用時閘門不通過（fail-closed）。
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
    blocked = non_risk_reducing(reflection["proposals"])  # type: ignore[arg-type]
    blocked.update(gated_cash_buffer_knobs(template, reflection["proposals"]))  # type: ignore[arg-type]
    if blocked and not allow_risk_increase:
        _record_needs_human(repository, reflection_id, blocked, now)
        return {"applied": False, "needs_human": True, "risk_direction": blocked,
                "reason": "提案含加風險或方向不明的旋鈕（%s），不自動套用；已封存並標 needs_human，請看週報後用 "
                          "cli/strategy_reflection.py apply-pending --allow-risk-increase %s（或 apply --reflection-id %s --allow-risk-increase）明確放行"
                          % ("、".join("%s=%s" % item for item in sorted(blocked.items())), reflection_id, reflection_id)}
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
    benchmark = benchmarker([day for day, _ in _nav_points(candidate_replay)]) if benchmarker is not None else None
    gate = replay_gate(base_replay, candidate_replay, benchmark)
    if not gate["passed"]:
        return {"applied": False, "reason": "影子回放未通過：" + "；".join(gate["reasons"]), "replay_gate": gate}
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
            # 回放期間另一程序可能剛套用教訓（不改 policy、不是 action="apply"），鎖內重查試用狀態避免兩件變更重疊。
            if open_trial(history) is not None:
                return {"applied": False, "reason": "回放期間已有另一件變更進入試用觀察期，改天再套用"}
            live = dict(candidate)
            live["policy_id"] = "strategy-%s-r%d" % (today.date().isoformat(), 1)
            live["available_at"] = moment.astimezone(TAIPEI).isoformat()  # 實際寫入時間，不是回放開始前的 now
            parse_aware_time(live["available_at"], "available_at", error=ReflectionError)
            validate_template(live, rules)
            backup = repository.policy_backups / ("%s.json" % template["policy_id"])
            repository.policy_backups.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(root / POLICY_PATH, backup)
            # 先記歷史再換 live 檔：中途中止時留下「有紀錄」的狀態（試用、冷卻仍生效；rollback 會因雜湊不符而拒絕並要求人工處理），
            # 不會出現沒有紀錄的 live 變更。
            repository.append_history({
                "action": "apply", "applied_at": today.isoformat(), "reflection_id": reflection_id,
                "knobs": [item["knob"] for item in reflection["proposals"]], "decision_days": brief["decision_days"],  # type: ignore[union-attr]
                "policy_id_before": template["policy_id"], "policy_id_after": live["policy_id"], "backup": str(backup),
                "policy_sha256_after": canonical_sha256(live),
                # 人設定的起點（累積偏移上限的基準）與試用觀察期：套用後要看新決策日的表現，明確變差才還原。
                "origins": {item["knob"]: item["current"] for item in reflection["proposals"]},  # type: ignore[union-attr]
                # 每顆旋鈕這次的方向（from→to）：試用被還原後，提案驗證用它禁止再往同方向調。
                "changes": {item["knob"]: {"from": item["current"], "to": item["proposed"]} for item in reflection["proposals"]},  # type: ignore[union-attr]
                "trial": {"verification": trial_verification([item["knob"] for item in reflection["proposals"]])},  # type: ignore[union-attr]
                # 含加風險方向而由人明確放行時留下稽核痕跡；全部降風險的自動套用沒有這個欄位。
                **({"risk_increase_allowed_by_human": dict(blocked)} if blocked else {}),
                "replay_gate": {"benchmark_id": gate["benchmark_id"], "segments": gate["segments"], "evidence_grade": gate["evidence_grade"],
                                "in_sample": gate["in_sample"], "available_at_overridden_days": gate["available_at_overridden_days"]},
            })
            try:
                atomic_write_json(root / POLICY_PATH, live)
            except BaseException:
                repository.append_history({"action": "rollback", "rolled_back_at": today.isoformat(),
                                           "of": reflection_id, "policy_id_restored": template["policy_id"]})
                raise
    except BlockingIOError:
        return {"applied": False, "reason": "每日決策進行中（artifacts/daily/.lock 被佔用），反思已封存，之後可用 apply 再套用"}
    return {"applied": True, "policy_id": live["policy_id"], "knobs": [item["knob"] for item in reflection["proposals"]],  # type: ignore[union-attr]
            "replay_gate": {"in_sample": gate["in_sample"], "available_at_overridden_days": gate["available_at_overridden_days"]},
            **({"risk_increase_allowed_by_human": dict(blocked)} if blocked else {})}


# ------------------------------------------------------------------ 開關與試用期
def load_switch(root: Path) -> Dict[str, bool]:
    """``config/strategy_reflection.json``：{"enabled": true, "auto_apply": false}。

    缺檔視為「啟用但人工模式」；``auto_apply`` 缺欄位一律視為 false（自動套用必須明寫 true 才開）；
    ``enabled`` 缺欄位或任一欄位不是布林值 fail-closed 全關。"""
    path = Path(root) / SWITCH_PATH
    if not path.exists():
        return {"enabled": True, "auto_apply": False}
    try:
        data = _read_json(path, "反思開關")
        switch = {"enabled": data["enabled"], "auto_apply": data.get("auto_apply", False)}
    except (ReflectionError, KeyError):
        return {"enabled": False, "auto_apply": False}
    if not all(isinstance(value, bool) for value in switch.values()):
        return {"enabled": False, "auto_apply": False}
    return switch


def _benchmark_comparison(root: Path, database: Path, as_of: str) -> Optional[Mapping[str, object]]:
    """帳戶對各基準的比較（與儀表板、``cli/benchmark.py`` 同一支程式）；帳本或基準不可用時回 None。"""
    from etf_agent.benchmark import BenchmarkError, build_benchmark_comparison  # 延後匯入避免循環相依
    from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository, active_account_id

    try:
        repository = VirtualAccountRepository(Path(root) / "artifacts" / "ledger", active_account_id(Path(root)))
        return build_benchmark_comparison(
            repository, database, Path(root) / "config" / "decision_rules.json", Path(root) / "config" / "benchmarks.json",
            Path(root), as_of)
    except (BenchmarkError, VirtualAccountError, OSError, ValueError, sqlite3.Error):
        return None


def benchmark_excess(root: Path, database: Path, now: datetime) -> Dict[str, Decimal]:
    """帳戶相對等權基準的逐日超額報酬；帳本或基準不可用時回空字典（試用期維持進行中，不會因此還原）。"""
    comparison = _benchmark_comparison(root, database, now.astimezone(timezone.utc).isoformat())
    return excess_series(comparison) if comparison is not None else {}


def _week_change(comparison: Mapping[str, object], item: Mapping[str, object], days: int = 5) -> Dict[str, str]:
    """最近 ``days`` 個交易日（一週）帳戶與基準的報酬；對齊的點不足 ``days``+1 個就不給（開帳第一週前不虛構）。"""
    account = {str(p["trade_date"]): Decimal(str(p["nav"])) for p in comparison["account"]["series"]}  # type: ignore[index]
    bench = {str(p["trade_date"]): Decimal(str(p["nav"])) for p in item.get("series", []) if p.get("nav") is not None}  # type: ignore[union-attr]
    dates = sorted(set(account) & set(bench))
    if len(dates) < days + 1:
        return {}
    first, last = dates[-days - 1], dates[-1]
    a, b = account[last] / account[first] - 1, bench[last] / bench[first] - 1
    return {"week_account_return": str(a.quantize(Decimal("0.000001"))), "week_benchmark_return": str(b.quantize(Decimal("0.000001"))),
            "week_excess_return": str((a - b).quantize(Decimal("0.000001")))}


def benchmark_diagnostics(root: Path, database: Path, as_of: str) -> Dict[str, object]:
    """診斷包的「與基準比較」：帳戶開帳以來對 0050 與等權基準的累積報酬與超額（只含數值，status=ok 才可引用）。
    比的是已收盤的官方價；基準不可用或沒有對齊的交易日就標 unavailable 並寫明原因。"""
    comparison = _benchmark_comparison(root, database, as_of)
    if comparison is None or comparison.get("status") != "ok":
        return {"status": "unavailable", "reason": "帳本或基準資料不可用，無法比較"}
    entries: Dict[str, object] = {}
    for item in comparison.get("benchmarks", []):  # type: ignore[union-attr]
        verdict = item.get("comparison") if isinstance(item, Mapping) else None
        if isinstance(verdict, Mapping) and verdict.get("status") == "ok":
            entries[str(item["benchmark_id"])] = {
                key: verdict[key] for key in ("aligned_days", "account_cumulative_return", "benchmark_cumulative_return",
                                              "excess_return", "relative_drawdown")
            } | {"status": "ok"} | _week_change(comparison, item)
    if not entries:
        return {"status": "unavailable", "reason": "帳戶與基準在開帳後還沒有同一天都有估值的交易日"}
    return {"status": "ok", "benchmarks": entries}


def trial_note(history, decision_days: int, excess: Optional[Mapping[str, Decimal]] = None) -> Optional[str]:
    """試用進行中的週報／略過說明。有基準超額序列時用與結算（``evaluate_trial``）相同的交易日計數，
    沒有（基準資料不可用）就退回決策日差並寫明結算會等待、超過 ``TRIAL_MAX_CALENDAR_DAYS`` 天逾時結案。"""
    entry = open_trial(history)
    if entry is None:
        return None
    head = "試用觀察期進行中：%s 套用 %s，" % (str(entry["applied_at"])[:10], "、".join(entry["knobs"]))  # type: ignore[arg-type]
    if excess:
        evaluation = evaluate_trial(entry, excess, extensions(history, str(entry["reflection_id"])) > 0)
        progress = "新交易日 %d／%d" % (evaluation["after_days"], evaluation["needed_after_days"])  # type: ignore[index]
    else:
        progress = "新決策日 %d／%d（基準資料不可用，無法計交易日；超過 %d 天逾時結案）" % (
            max(0, decision_days - int(entry["decision_days"])), TRIAL_DAYS, TRIAL_MAX_CALENDAR_DAYS)  # type: ignore[arg-type]
    return head + progress + "；結案前不提新案。"


def _applied_mismatch(root: Path, entry: Mapping[str, object]) -> Optional[Dict[str, object]]:
    """套用紀錄與目前 live 檔是否一致。一致或舊紀錄沒有雜湊（無從核對）回 None；
    不一致回 {"reason", "never_landed"}：never_landed 表示 live 檔仍是套用前的版本（歷史先寫、換檔前就被中止）。"""
    if entry.get("action") == LESSONS_ACTION:
        expected = entry.get("sha256_after")
        if not expected:
            return None
        current = load_strategy_lessons(root)
        if current is not None and current["sha256"] == expected:
            return None
        return {"reason": "目前的教訓檔不是該次套用的結果（或已被改動、刪除）", "never_landed": False}
    expected = entry.get("policy_sha256_after")
    if not expected:
        return None
    try:
        live = _read_json(Path(root) / POLICY_PATH, "目前策略樣板")
    except ReflectionError as error:
        return {"reason": str(error), "never_landed": False}
    if canonical_sha256(live) == expected:
        return None
    return {"reason": "目前策略樣板 %s 不是該次套用的結果 %s（或內容已被手動修改）" % (live.get("policy_id"), entry.get("policy_id_after")),
            "never_landed": live.get("policy_id") == entry.get("policy_id_before")}


def settle_open_trial(
    root: Path, now: datetime, repository: ReflectionRepository, excess: Mapping[str, Decimal],
    write_lock: Optional[Callable[[], object]] = None, clock: Optional[Callable[[], datetime]] = None,
) -> Optional[Dict[str, object]]:
    """結算尚未結案的試用：沒有就回 None；觀察日不夠回 pending；明確變差才還原。

    不會永遠卡住的結案：套用紀錄與 live 檔不一致 → ``orphaned``；觀察日一直湊不齊超過 ``TRIAL_MAX_CALENDAR_DAYS`` 天 → ``timeout``
    （變更保留）；判定要還原卻還原失敗 → ``revert_failed``。這三種都是 final 並標 ``needs_manual``。

    還原走 ``rollback``（要取每日鎖、避開 20:50～09:00，且設定檔被人改過就不動），所以放在週六由 ``apply_pending`` 執行。
    連續 ``AUTO_DISABLE_REVERTS`` 次還原會自動關掉自動套用，改回人工。"""
    history = repository.history()
    entry = open_trial(history)
    if entry is None:
        return None
    reflection_id = str(entry["reflection_id"])
    moment = now.astimezone(TAIPEI).isoformat()

    def record(result: str, final: bool, evaluation: Mapping[str, object], **extra: object) -> Dict[str, object]:
        repository.append_history({"action": "trial_result", "of": reflection_id, "result": result, "final": final,
                                   "settled_at": moment, "evaluation": evaluation, **extra})
        return {"trial": result, "reflection_id": reflection_id, "evaluation": evaluation, **extra}

    # 套用到一半被中止（歷史已寫、live 檔沒換）或 live 檔事後被人改過：試用評的不是這次變更，繼續等或還原都沒有意義，
    # 直接以 orphaned 結案並要求人工處理（不改 live 檔）。live 檔仍是套用前版本時順手補上 rollback，讓堆疊與冷卻回到真實狀態。
    mismatch = _applied_mismatch(root, entry)
    if mismatch is not None:
        if mismatch["never_landed"]:
            repository.append_history({"action": "rollback", "rolled_back_at": moment, "of": reflection_id,
                                       "policy_id_restored": entry.get("policy_id_before"), "reason": "套用中途中止，live 檔未更換"})
        reason = str(mismatch["reason"])
        return record("orphaned", True, {"verdict": "orphaned", "reason": reason}, needs_manual=True,
                      alert="試用 %s 的套用紀錄與目前設定不一致，已結案、需人工檢查：%s" % (reflection_id, reason))
    extended = extensions(history, reflection_id) > 0
    evaluation = evaluate_trial(entry, excess, extended)
    verdict = str(evaluation["verdict"])
    verification = str(entry["trial"]["verification"])  # type: ignore[index]

    if verdict == "pending":
        waited = _calendar_days_since(entry, now)
        if waited is not None and waited >= TRIAL_MAX_CALENDAR_DAYS:
            # 基準資料一直不可用或交易日一直湊不齊：不能讓試用永遠不結案而凍結整個反思迴圈。變更保留、不還原，標需人工。
            return record("timeout", True, {**evaluation, "reason": "套用已 %d 天仍湊不齊觀察交易日，逾時結案" % waited}, needs_manual=True,
                          alert="試用 %s 逾時結案（套用已 %d 天，觀察交易日 %s／%s）；變更保留，請人工確認" % (
                              reflection_id, waited, evaluation.get("after_days"), evaluation.get("needed_after_days")))
        return {"trial": "pending", "reflection_id": reflection_id, "evaluation": evaluation}
    if verdict == "not_worse":
        return record("kept", True, evaluation)
    revert = verdict == "worse"
    if verdict == "inconclusive":
        if not extended:
            return record("extended", False, evaluation)
        # 回放驗得了的旋鈕已過影子回放，看不出差別就保留；驗不了的（停損／停利門檻）看不出差別就還原。
        revert = verification == VERIFICATION_OBSERVE
        if not revert:
            return record("kept_unverified", True, evaluation)
    try:
        with (write_lock or _no_lock)():
            if apply_window_blocked((clock or (lambda: now))()):
                return {"trial": "revert_deferred", "reason": "目前在 20:50～09:00，為避免決策用到不同策略，還原改天執行", "reflection_id": reflection_id}
            if entry.get("action") == LESSONS_ACTION:
                revert_lessons(root, entry, repository.append_history, now)
            else:
                rollback(root, now, repository)
    except BlockingIOError:
        return {"trial": "revert_deferred", "reason": "每日決策進行中，還原改天執行", "reflection_id": reflection_id}
    except ReflectionError as error:
        # 還原不了（備份讀不到等）：結案並標需人工，否則每週都卡在同一個失敗上。
        return {**record("revert_failed", True, evaluation, needs_manual=True, reason=str(error),
                         alert="試用 %s 判定要還原但還原失敗，已結案、需人工處理：%s" % (reflection_id, error)),
                "reason": str(error)}
    result = record("reverted", True, evaluation)
    if consecutive_reverts(repository.history()) >= AUTO_DISABLE_REVERTS:
        disable_auto_apply(root)
        repository.append_history({"action": "auto_disabled", "at": moment,
                                   "reason": "連續 %d 次試用被還原，自動關閉自動套用，改回人工" % AUTO_DISABLE_REVERTS})
        result["auto_apply_disabled"] = True
    return result


def _calendar_days_since(entry: Mapping[str, object], now: datetime) -> Optional[int]:
    """套用日到今天的日曆天；套用時間讀不出來回 None（不逾時，維持等待）。"""
    try:
        applied = date.fromisoformat(str(entry["applied_at"])[:10])
    except (KeyError, ValueError):
        return None
    return (now.astimezone(TAIPEI).date() - applied).days


def disable_auto_apply(root: Path) -> None:
    path = Path(root) / SWITCH_PATH
    switch = load_switch(root)
    atomic_write_json(path, {"enabled": switch["enabled"], "auto_apply": False})


# 每週一次：距上一份反思不到這麼多天就略過（手動補跑用 force）。比週間隔略短，避免排程晚幾分鐘就跳過一週。
MIN_INTERVAL_DAYS = 6
# 待套用的反思最多等這麼久；超過就視為過期，等下一週的新反思。
# 週五 19:30 封存、下週六 10:00 套用相隔 8 天又 14.5 小時（本週反思沒有產生時才會遇到），所以上限要大於 8 天。
PENDING_MAX_AGE_DAYS = 9


def reflection_ids(repository: ReflectionRepository) -> List[str]:
    """已封存的反思 id，由舊到新（id 內含 UTC 時間，字串排序即時間排序）。"""
    runs = repository.base / "runs"
    return sorted(path.name for path in runs.iterdir() if path.is_dir() and path.name.startswith("reflection-")) if runs.exists() else []


def last_reflection_at(repository: ReflectionRepository) -> Optional[datetime]:
    ids = reflection_ids(repository)
    if not ids:
        return None
    try:
        return datetime.strptime(ids[-1], "reflection-%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def apply_pending(
    root: Path, database: Path, decisions: Path, now: datetime, replayer: Optional[Replayer] = None,
    write_lock: Optional[Callable[[], object]] = None, clock: Optional[Callable[[], datetime]] = None,
    excess: Optional[Mapping[str, Decimal]] = None, benchmarker: Optional[Benchmarker] = None,
    allow_risk_increase_for: Optional[str] = None,
) -> Dict[str, object]:
    """週六早上套用本週五封存、尚未套用的提案。仍走 ``apply_reflection`` 的驗證、風險方向、影子回放、取鎖與時段保護。

    自動只套用降風險方向；``allow_risk_increase_for`` 是人明確指定的 reflection id（CLI ``--allow-risk-increase``），
    只有它等於最近一份待套用的反思時才放行加風險方向，且這個人工指令在 ``auto_apply=false`` 的人工模式也有效（只套旋鈕、不套教訓）。"""
    repository = ReflectionRepository(root)
    switch = load_switch(root)
    human_override = allow_risk_increase_for is not None
    if not switch["enabled"]:
        return {"applied": False, "reason": "config/strategy_reflection.json 的 enabled 為 false，反思整體停用"}
    # 先結算上一次套用的試用期（明確變差才還原）；結案前不套用新提案。
    if excess is None:  # 沒有進行中的試用就不必算基準（也避免無謂的資料庫存取）
        excess = benchmark_excess(root, database, now) if open_trial(repository.history()) is not None else {}
    trial = settle_open_trial(root, now, repository, excess, write_lock, clock)
    if trial is not None and trial["trial"] in {"pending", "extended", "revert_deferred", "revert_failed"}:
        return {"applied": False, "reason": "試用觀察期尚未結案（%s），本週不套用新提案" % trial["trial"], **trial}
    if trial is not None and trial.get("auto_apply_disabled"):
        switch = load_switch(root)  # 結算剛因連續還原關閉自動套用：以最新開關為準，本次不再套用
    if not switch["auto_apply"] and not human_override:
        return {"applied": False, "reason": "config/strategy_reflection.json 的 auto_apply 為 false（人工模式）；提案已封存，請看週報後用 apply 手動套用",
                **({"trial": trial} if trial else {})}
    result = _apply_pending_knobs(root, database, decisions, now, repository, replayer, write_lock, clock, benchmarker, allow_risk_increase_for)
    # 一次只有一件變更在試用：旋鈕這週已套用就不再套教訓（教訓留到下週，過期就作廢）；人工模式下的人工指令只套旋鈕。
    if not result.get("applied") and switch["auto_apply"]:
        result["lessons"] = _apply_pending_lessons(root, now, repository, write_lock, clock)
    return {**result, **({"trial": trial} if trial else {})}


def _apply_pending_knobs(root, database, decisions, now, repository, replayer, write_lock, clock,
                         benchmarker=None, allow_risk_increase_for=None) -> Dict[str, object]:
    applied = {str(entry.get("reflection_id")) for entry in repository.history() if entry.get("action") == "apply"}
    ids = reflection_ids(repository)
    if not ids:
        return {"applied": False, "reason": "沒有任何已封存的反思"}
    latest = ids[-1]
    reflection, brief = load_reflection(repository, latest)
    if reflection["status"] != STATUS_PROPOSED:
        return {"applied": False, "reason": "最近一份反思沒有提案", "reflection_id": latest}
    if latest in applied:
        return {"applied": False, "reason": "最近一份反思已套用過", "reflection_id": latest}
    moment = last_reflection_at(repository)
    if moment is None or now - moment > timedelta(days=PENDING_MAX_AGE_DAYS):
        return {"applied": False, "reason": "最近一份反思超過 %d 天，已過期，等下一週" % PENDING_MAX_AGE_DAYS, "reflection_id": latest}
    if allow_risk_increase_for is not None and allow_risk_increase_for != latest:
        return {"applied": False, "reason": "--allow-risk-increase 指定的 %s 不是最近一份待套用的反思（%s），不套用" % (allow_risk_increase_for, latest),
                "reflection_id": latest}
    as_of = str(brief["as_of"])
    result = apply_reflection(root, latest, replayer or default_replayer(root, database, decisions, as_of), now,
                              repository, write_lock=write_lock, clock=clock or (lambda: datetime.now(timezone.utc)), enforce_window=True,
                              benchmarker=benchmarker or default_benchmarker(root, database, as_of),
                              allow_risk_increase=allow_risk_increase_for == latest)
    return {**result, "reflection_id": latest}


def _apply_pending_lessons(root, now, repository, write_lock, clock) -> Dict[str, object]:
    """週六套用週五封存的教訓提案：沒有、已套用、過期、或被擋都只回原因，不丟錯。仍走取鎖與 20:50～09:00 保護。"""
    pending = load_pending(repository.base)
    if pending is None:
        return {"applied": False, "reason": "沒有待套用的教訓"}
    lessons = pending["lessons"]
    sample = pending["sample"]
    applied = {str(entry.get("reflection_id")) for entry in repository.history() if entry.get("action") == LESSONS_ACTION}
    if lessons["lessons_id"] in applied:
        return {"applied": False, "reason": "這份教訓已套用過"}
    try:
        created = parse_aware_time(lessons["created_at"], "created_at", error=ReflectionError)
    except ReflectionError:
        return {"applied": False, "reason": "待套用教訓的時間格式錯誤"}
    if now - created > timedelta(days=PENDING_MAX_AGE_DAYS):
        return {"applied": False, "reason": "待套用的教訓超過 %d 天，已過期，等下一週" % PENDING_MAX_AGE_DAYS}
    # 套用時再核對最近一份反思的樣本門檻，擋手植或殘留的 pending.json。
    ids = reflection_ids(repository)
    try:
        latest_brief = load_reflection(repository, ids[-1])[1] if ids else None
    except ReflectionError:
        latest_brief = None
    if latest_brief is None or latest_brief.get("gate_open") is not True:
        return {"applied": False, "reason": "最近一份反思的校準樣本門檻未達，教訓不套用"}
    try:
        with (write_lock or _no_lock)():
            if apply_window_blocked((clock or (lambda: datetime.now(timezone.utc)))()):
                return {"applied": False, "reason": "目前在 20:50～09:00，教訓改天再套用"}
            # 取鎖前的檢查可能已過期（別的程序剛套用、或另一件變更剛開始試用）：鎖內重讀歷史與待套用檔再判斷。
            history = repository.history()
            reloaded = load_pending(repository.base)
            if reloaded is None or reloaded["lessons"]["lessons_id"] != lessons["lessons_id"]:
                return {"applied": False, "reason": "待套用的教訓在等待期間已改變，改天再套用"}
            if any(entry.get("action") == LESSONS_ACTION and str(entry.get("reflection_id")) == lessons["lessons_id"] for entry in history):
                return {"applied": False, "reason": "這份教訓已套用過"}
            if open_trial(history) is not None:
                return {"applied": False, "reason": "已有另一件變更在試用觀察期，教訓改天再套用"}
            return apply_lessons(root, repository.base, lessons, repository.append_history, int(sample["decision_days"]), now.astimezone(TAIPEI))
    except BlockingIOError:
        return {"applied": False, "reason": "每日決策進行中，教訓改天再套用"}
    except ReflectionError as error:
        return {"applied": False, "reason": str(error)}


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


def rollback_lessons(root: Path, now: datetime, repository: Optional[ReflectionRepository] = None) -> Dict[str, object]:
    """人工還原最近一次尚未還原的教訓套用。"""
    repository = repository or ReflectionRepository(root)
    stack: List[Mapping[str, object]] = []
    for entry in repository.history():
        if entry.get("action") == LESSONS_ACTION:
            stack.append(entry)
        elif entry.get("action") == REVERT_ACTION and stack:
            stack.pop()
    if not stack:
        raise ReflectionError("沒有可還原的教訓套用紀錄")
    return revert_lessons(root, stack[-1], repository.append_history, now.astimezone(TAIPEI))


def run_auto(
    root: Path, database: Path, decisions: Path, runner: Optional[AgentRunner], now: datetime,
    replayer: Optional[Replayer] = None, apply: bool = True, write_lock: Optional[Callable[[], object]] = None,
    clock: Optional[Callable[[], datetime]] = None, force: bool = False, benchmarker: Optional[Benchmarker] = None,
) -> Dict[str, object]:
    """每週自動流程。樣本不足時完全不呼叫 Agent（零用量），但每週都會產生程式算好的反思報告；
    提案不過驗證或回放就整份不套用，加風險方向的提案只封存標 needs_human。距上一份反思不到 6 天時略過（``force`` 手動補跑）。"""
    repository = ReflectionRepository(root)
    previous = last_reflection_at(repository)
    if not force and previous is not None and now - previous < timedelta(days=MIN_INTERVAL_DAYS):
        return {"status": "skipped", "reason": "距上一份反思（%s）不到 %d 天；手動補跑請加 --force" % (previous.isoformat(), MIN_INTERVAL_DAYS)}
    as_of = now.astimezone(timezone.utc).isoformat()
    brief = make_brief(root, database, as_of, decisions)
    template, rules = load_policy_and_rules(root)
    switch = load_switch(root)
    history = repository.history()
    thesis, context = thesis_inputs(root, database, as_of, decisions)
    review_dir = repository.base / "agent_inputs" / now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    review = review_thesis(runner if switch["enabled"] else None, review_dir, thesis, context) if thesis else {}
    if switch["enabled"] and review:
        # 論點結論給交易 Agent 當背景（決策鏈只讀這個檔，不匯入反思套件）；全部 unknown 時不覆蓋上一份。
        sealed = seal_verdicts("thesis-verdicts-" + now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), now.astimezone(timezone.utc).isoformat(), review)
        if sealed["items"]:
            atomic_write_json(root / VERDICTS_PATH, sealed)
    excess = benchmark_excess(root, database, now) if open_trial(history) is not None else {}
    trial = trial_note(history, int(brief["decision_days"]), excess)
    skip_reason = (
        "config/strategy_reflection.json 的 enabled 為 false，反思停用，只產生報告。" if not switch["enabled"]
        else trial if trial is not None else None
    )
    # 教訓與旋鈕共用校準樣本門檻：gate 沒開（或缺欄位）時完全不呼叫教訓 Agent。
    if brief.get("gate_open") is not True or skip_reason is not None:
        lessons = {"status": "skipped", "reason": (
            "校準樣本門檻未達（gate 關閉），本週不整理教訓" if brief.get("gate_open") is not True
            else "試用觀察期進行中或反思停用，本週不整理教訓")}
    elif not thesis or not switch["auto_apply"]:
        lessons = {"status": "skipped", "reason": "自動套用已關閉或沒有持股，本週不整理教訓"}
    else:
        lessons = reflect_lessons(runner, review_dir, repository.base, thesis, review, brief, now, root)  # type: ignore[arg-type]
    if brief.get("gate_open") is not True or skip_reason is not None:
        reflection = observe_only(brief, now, skip_reason)
        save_reflection(repository, reflection, brief)
        notes = [note for note in (trial, None if switch["auto_apply"] else "自動套用已關閉（人工模式）：提案只封存，不會自動套用。") if note]
        report = write_weekly_report(root, now, render_weekly_report(now, brief, reflection, None, notes, history, thesis, review, lessons, _current_lesson_texts(root)))
        return {"status": STATUS_OBSERVE_ONLY, "reflection_id": reflection["reflection_id"], "reason": reflection["summary"],
                "report": str(report)}
    if runner is None:
        raise ReflectionError("樣本足夠但沒有提供 Agent runner")
    run_dir = repository.base / "agent_inputs" / now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    atomic_write_json(run_dir / "brief.json", brief)
    try:
        call = runner.run(agent_task(run_dir / "brief.json"), run_dir)
    except DailyPipelineError as error:
        # Agent 逾時或沒有結構化輸出：本週仍封存 observe_only 反思並寫週報（含失敗原因），之後再往外丟，讓排程看得到失敗。
        reflection = observe_only(brief, now, "反思 Agent 失敗，本週沒有提案：%s" % error)
        save_reflection(repository, reflection, brief)
        write_weekly_report(root, now, render_weekly_report(
            now, brief, reflection, None, ["反思 Agent 失敗：%s" % error], history, thesis, review, lessons, _current_lesson_texts(root)))
        raise
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
    if apply and switch["auto_apply"] and reflection["status"] == STATUS_PROPOSED:
        result["apply"] = apply_reflection(root, str(reflection["reflection_id"]),
                                           replayer or default_replayer(root, database, decisions, as_of), now, repository,
                                           write_lock=write_lock, clock=clock or (lambda: datetime.now(timezone.utc)), enforce_window=True,
                                           benchmarker=benchmarker or default_benchmarker(root, database, as_of))
    notes = [] if apply and switch["auto_apply"] else ["本次只封存、不套用；週六早上由 apply-pending 套用，或用 apply 手動套用。"]
    if not switch["auto_apply"]:
        notes.append("自動套用已關閉（人工模式）：提案只封存，請看完報告後用 apply 手動套用。")
    result["report"] = str(write_weekly_report(root, now, render_weekly_report(
        now, brief, reflection, result.get("apply"), notes, history, thesis, review, lessons, _current_lesson_texts(root))))  # type: ignore[arg-type]
    return result
