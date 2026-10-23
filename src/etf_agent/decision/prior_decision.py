"""前次決策背景（PriorDecisionContext）：讓交易 Agent 看到上一份已封存決策對目前持股的意圖與重估條件。

這是記憶／反思的最小版本，只給交易 Agent、只當**不可引用的背景**：

- 來源只能是 DecisionRepository 內通過 manifest 驗證、狀態 approved／no_trade 的 Decision run；
  run_id、decision／intent 內容雜湊與 manifest 雜湊全部記錄，Finalizer 可重算本 artifact 的雜湊。
- 前次 cutoff 必須早於本次 cutoff（含時區）；snapshot 不同時明確標為 ``cross_snapshot_exception``，
  因為前次結論不是本次 Snapshot 的事實，所以不配 evidence_id、``citable`` 永遠為 false。
- 只整理目前持股的 ``intent``／``conviction``／``invalidation_conditions``；不含數字、權重或訂單。
- 找不到前次 run 時輸出 ``status=unavailable``，不是錯誤；內容由 ``PriorDecisionContextValidator`` 重建驗證。
- 交易 Agent 的文字欄位若出現本背景的識別碼，``TradeDecisionValidator`` 以 ``prior_reference_tokens`` 拒絕；
  行動的改變或延續仍必須由本次多空 claim 支持（既有 buy／add 與 trim／exit 規則）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core.run_layout import list_run_dirs

from .fields import (
    INTENTS,
    DecisionToolError,
    artifact_content_sha256,
    canonical_sha256,
    parse_time,
    reject_unknown_fields,
    required_string,
    string_list,
    validate_forbidden_keys,
)
from .sizing import CONVICTION_LEVELS


PRIOR_DECISION_VERSION = "1.0"
PRIOR_STATUSES = ("available", "unavailable")
USABLE_DECISION_STATUSES = ("approved", "no_trade")
NO_PRIOR_REASON = "NO_PRIOR_DECISION_RUN"
CONTEXT_FIELDS = {
    "schema_version", "context_id", "status", "reason", "account_id", "snapshot_id", "decision_cutoff",
    "source", "same_snapshot", "cross_snapshot_exception", "items", "uncovered_symbols", "citable", "content_sha256",
}
SOURCE_FIELDS = {
    "run_id", "decision_id", "decision_status", "snapshot_id", "decision_cutoff", "target_session_start", "selection",
    "intent_id", "intent_sha256", "decision_sha256", "manifest_sha256",
}
SELECTIONS = ("ledger_applied", "latest_cutoff")
NO_SESSION_REASON = "NO_TARGET_SESSION"
ITEM_FIELDS = {"symbol", "intent", "conviction", "invalidation_conditions"}
_SIZED_INTENTS = {"buy", "add"}
_RUN_FILES = ("decision_input", "decision", "intent", "manifest")


def _held_symbols(bundle: Mapping[str, object]) -> List[str]:
    return sorted({str(item.get("symbol", "")).upper() for item in bundle["account_snapshot"].get("positions", [])})


def _account_id(bundle: Mapping[str, object]) -> object:
    return (bundle.get("account_snapshot") or {}).get("account_id")


def _read_run_json(run_dir: Path, name: str) -> Optional[Mapping[str, object]]:
    try:
        payload = json.loads((run_dir / ("%s.json" % name)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _session_start_text(bundle: Mapping[str, object]) -> Optional[str]:
    session = (bundle.get("trading_status_bundle") or {}).get("target_session") or {}
    start = session.get("start") if isinstance(session, Mapping) else None
    return start if isinstance(start, str) and start.strip() else None


def _session_start(bundle: Mapping[str, object], field: str) -> datetime:
    start = _session_start_text(bundle)
    if start is None:
        raise DecisionToolError("%s 缺少 trading_status_bundle.target_session.start，無法比較交易時段" % field)
    return parse_time(start, field + ".target_session.start")


def find_prior_decision_run(
    repository_root: Path, bundle: Mapping[str, object], applied_run_ids: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, str]]:
    """前一個**交易時段**的封存 Decision run；找不到回 None，找到回 ``{"run_id", "selection"}``。

    候選：同帳戶、狀態 approved／no_trade、目標交易時段（``target_session.start``）嚴格早於本次、cutoff 早於本次。
    同一天 21:00 備案與隔天 07:00 重做是同一時段的兩個 run，不能把備案當成前次決策，所以用時段而不是 cutoff 排序。
    最新的前一時段有多個 run 時：帳本實際結算過的（``applied_run_ids``，來自 close 狀態的 decision_run_id）剛好一個就取它
    （``selection=ledger_applied``）；否則取該時段 cutoff 最新者（``selection=latest_cutoff``）；cutoff 也相同才拒絕。
    只讀 ``decision`` 與 ``decision_input`` 的身分欄位；內容完整性由 ``read_decision_run`` 以 manifest 驗證。
    """
    cutoff = parse_time(bundle.get("decision_cutoff"), "decision_cutoff")
    session_start = _session_start(bundle, "本次 bundle")
    account_id = _account_id(bundle)
    candidates: List[Tuple[datetime, datetime, str]] = []
    for run_dir in list_run_dirs(Path(repository_root)):
        # 先讀小的 decision.json 過濾狀態與時間，只對候選 run 讀含整份 Snapshot 的 decision_input.json 核對帳戶與時段。
        decision = _read_run_json(run_dir, "decision")
        if decision is None or decision.get("status") not in USABLE_DECISION_STATUSES:
            continue
        try:
            prior_cutoff = parse_time(decision.get("decision_cutoff"), "decision_cutoff")
        except DecisionToolError:
            continue
        if prior_cutoff >= cutoff:
            continue
        prior_bundle = _read_run_json(run_dir, "decision_input")
        if prior_bundle is None or _account_id(prior_bundle) != account_id:
            continue
        if prior_bundle.get("decision_cutoff") != decision.get("decision_cutoff"):
            continue  # 身分不一致的 run 不當候選；read_decision_run 的 manifest 驗證會再把關。
        try:
            prior_session = _session_start(prior_bundle, "前次 bundle")
        except DecisionToolError:
            continue  # 沒有目標時段的封存無法排序，不當候選。
        if prior_session < session_start:
            candidates.append((prior_session, prior_cutoff, run_dir.name))
    if not candidates:
        return None
    latest_session = max(session for session, _, _ in candidates)
    same_session = [(moment, run_id) for session, moment, run_id in candidates if session == latest_session]
    applied = [run_id for _, run_id in same_session if run_id in set(applied_run_ids or ())]
    if len(applied) == 1:
        return {"run_id": applied[0], "selection": "ledger_applied"}
    if len(applied) > 1:
        raise DecisionToolError("帳本記錄同一交易時段結算了多個 Decision run，無法判定前次決策：%s" % "、".join(sorted(applied)))
    latest_cutoff = max(moment for moment, _ in same_session)
    matches = sorted(run_id for moment, run_id in same_session if moment == latest_cutoff)
    if len(matches) > 1:
        raise DecisionToolError("前一交易時段有多個 cutoff 相同的 Decision run，無法判定前次決策：%s" % "、".join(matches))
    return {"run_id": matches[0], "selection": "latest_cutoff"}


def read_decision_run(repository_root: Path, run_id: str) -> Dict[str, object]:
    """讀取前次 Decision run 的身分檔；先以 manifest 驗證整個 run，任何不一致都拒絕。"""
    from .finalization import DecisionRepository  # 延後匯入：finalization 也匯入本模組做重建驗證。

    run_dir = DecisionRepository(Path(repository_root)).verify(run_id)
    prior: Dict[str, object] = {"run_id": run_id}
    for name in _RUN_FILES:
        payload = _read_run_json(run_dir, name)
        if payload is None:
            raise DecisionToolError("前次 Decision run 缺少或無法讀取 %s：%s" % (name, run_id))
        prior[name] = payload
    return prior


def _verify_prior(bundle: Mapping[str, object], prior: Mapping[str, object], selection: str) -> Dict[str, object]:
    """核對前次 run 的帳戶、交易時段與 cutoff 順序、狀態與內容雜湊，回傳要記錄的來源欄位。"""
    if selection not in SELECTIONS:
        raise DecisionToolError("selection 必須是 %s" % "／".join(SELECTIONS))
    run_id = required_string(prior, "run_id")
    for name in _RUN_FILES:
        if not isinstance(prior.get(name), Mapping):
            raise DecisionToolError("前次 Decision run %s 缺少 %s" % (run_id, name))
    prior_bundle, decision, intent, manifest = (prior[name] for name in _RUN_FILES)
    if _account_id(prior_bundle) != _account_id(bundle):
        raise DecisionToolError("前次 Decision run %s 的帳戶與本次不同，不得當作前次決策" % run_id)
    prior_cutoff = parse_time(prior_bundle.get("decision_cutoff"), "前次 decision_cutoff")
    if prior_cutoff >= parse_time(bundle.get("decision_cutoff"), "decision_cutoff"):
        raise DecisionToolError("前次 Decision run %s 的 cutoff 必須早於本次 cutoff" % run_id)
    # 同一時段的 21:00 備案與 07:00 重做只差幾小時 cutoff；前次決策必須是更早的交易時段。
    if _session_start(prior_bundle, "前次 bundle") >= _session_start(bundle, "本次 bundle"):
        raise DecisionToolError("前次 Decision run %s 的目標交易時段必須早於本次，不得用同一時段的備案" % run_id)
    if decision.get("status") not in USABLE_DECISION_STATUSES:
        raise DecisionToolError("前次 Decision run %s 的狀態 %s 不是 approved／no_trade" % (run_id, decision.get("status")))
    for name, artifact in (("decision", decision), ("intent", intent)):
        if artifact.get("content_sha256") != artifact_content_sha256(artifact):
            raise DecisionToolError("前次 Decision run %s 的 %s 內容雜湊不一致" % (run_id, name))
    if decision.get("trade_intent_result_id") != intent.get("decision_id"):
        raise DecisionToolError("前次 Decision run %s 的 decision 與 intent 不是同一組" % run_id)
    snapshot_id = prior_bundle.get("snapshot_id")
    if decision.get("snapshot_id") != snapshot_id or intent.get("snapshot_id") != snapshot_id:
        raise DecisionToolError("前次 Decision run %s 的 snapshot_id 不一致" % run_id)
    if intent.get("decision_cutoff") != prior_bundle.get("decision_cutoff"):
        raise DecisionToolError("前次 Decision run %s 的 intent cutoff 與其輸入不一致" % run_id)
    if manifest.get("run_id") != run_id:
        raise DecisionToolError("前次 Decision run %s 的 manifest run_id 不一致" % run_id)
    return {
        "run_id": run_id,
        "decision_id": required_string(decision, "decision_id"),
        "decision_status": str(decision["status"]),
        "snapshot_id": required_string(prior_bundle, "snapshot_id"),
        "decision_cutoff": required_string(prior_bundle, "decision_cutoff"),
        "target_session_start": str(_session_start_text(prior_bundle)),
        "selection": selection,
        "intent_id": required_string(intent, "decision_id"),
        "intent_sha256": required_string(intent, "content_sha256"),
        "decision_sha256": required_string(decision, "content_sha256"),
        "manifest_sha256": required_string(manifest, "manifest_sha256"),
    }


def seal_prior_decision_context(body: Mapping[str, object]) -> Dict[str, object]:
    payload = {key: value for key, value in body.items() if key not in ("context_id", "content_sha256")}
    payload["context_id"] = "prior-decision:%s" % canonical_sha256(payload)[:16]
    payload["content_sha256"] = artifact_content_sha256(payload)
    return payload


def build_prior_decision_context(
    bundle: Mapping[str, object],
    prior: Optional[Mapping[str, object]],
    reason: Optional[str] = None,
    selection: str = "latest_cutoff",
) -> Dict[str, object]:
    """由本次 bundle 與（選配的）前次 run 建立 PriorDecisionContext；``prior`` 為 None 時為 unavailable。

    ``selection`` 記錄 ``find_prior_decision_run`` 怎麼挑到這個 run（帳本結算記錄或該時段 cutoff 最新），供稽核。"""
    held = _held_symbols(bundle)
    base: Dict[str, object] = {
        "schema_version": PRIOR_DECISION_VERSION,
        "account_id": _account_id(bundle),
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "citable": False,
    }
    if prior is None:
        return seal_prior_decision_context({
            **base, "status": "unavailable", "reason": reason or NO_PRIOR_REASON, "source": None,
            "same_snapshot": None, "cross_snapshot_exception": False, "items": [], "uncovered_symbols": held,
        })
    source = _verify_prior(bundle, prior, selection)
    by_symbol = {
        str(item.get("symbol", "")).upper(): item
        for item in prior["intent"].get("items", []) if isinstance(item, Mapping)
    }
    items = [
        {
            "symbol": symbol,
            "intent": by_symbol[symbol].get("intent"),
            "conviction": by_symbol[symbol].get("conviction"),
            "invalidation_conditions": list(by_symbol[symbol].get("invalidation_conditions", [])),
        }
        for symbol in held if symbol in by_symbol
    ]
    same = source["snapshot_id"] == bundle["snapshot_id"]
    return seal_prior_decision_context({
        **base, "status": "available", "reason": None, "source": source,
        "same_snapshot": same, "cross_snapshot_exception": not same, "items": items,
        "uncovered_symbols": [symbol for symbol in held if symbol not in by_symbol],
    })


def prior_reference_tokens(context: Optional[Mapping[str, object]]) -> List[str]:
    """交易 Agent 文字欄位不得出現的識別碼：背景不是證據，不能被引用。"""
    if not isinstance(context, Mapping):
        return []
    tokens = ["prior_decision", context.get("context_id")]
    source = context.get("source")
    if isinstance(source, Mapping):
        tokens.extend(source.get(key) for key in ("run_id", "decision_id", "intent_id"))
    return [token for token in tokens if isinstance(token, str) and token.strip()]


def prior_decision_brief(context: Mapping[str, object]) -> Tuple[Dict[str, object], Dict[str, Dict[str, object]]]:
    """交易 brief 用的背景摘要與逐檔對照；不帶雜湊與識別碼，少給 Agent 可抄寫的內容。"""
    source = context.get("source")
    background: Dict[str, object] = {
        "status": context.get("status"),
        "reason": context.get("reason"),
        "citable": False,
        "same_snapshot": context.get("same_snapshot"),
        "cross_snapshot_exception": context.get("cross_snapshot_exception"),
        "source": (
            {key: source.get(key) for key in ("decision_cutoff", "snapshot_id", "decision_status")}
            if isinstance(source, Mapping) else None
        ),
        "note": (
            "前次決策只供檢查本次判斷是否與前次一致、前次寫的重估條件是否已出現；它不是本次 Snapshot 的事實或證據，"
            "不得寫進 rationale、unresolved_questions 或 invalidation_conditions，延續或改變行動都必須由本次多空 claim 支持。"
        ),
    }
    by_symbol = {
        str(item["symbol"]).upper(): {key: item.get(key) for key in ("intent", "conviction", "invalidation_conditions")}
        for item in context.get("items", []) if isinstance(item, Mapping)
    }
    return background, by_symbol


class PriorDecisionContextValidator:
    """重建驗證 PriorDecisionContext：欄位白名單、雜湊、時間順序、持股對應與不可引用旗標。"""

    def __init__(self, bundle: Mapping[str, object]):
        self.bundle = dict(bundle)
        self.held = _held_symbols(bundle)
        self.cutoff = parse_time(bundle.get("decision_cutoff"), "decision_cutoff")

    def validate(self, context: object) -> List[str]:
        if not isinstance(context, Mapping):
            return ["PriorDecisionContext 必須是物件"]
        errors: List[str] = []
        prefix = "PriorDecisionContext"
        validate_forbidden_keys(context, prefix, errors)
        reject_unknown_fields(context, CONTEXT_FIELDS, prefix, errors)
        if context.get("schema_version") != PRIOR_DECISION_VERSION:
            errors.append("%s.schema_version 必須為 %s" % (prefix, PRIOR_DECISION_VERSION))
        if context.get("account_id") != _account_id(self.bundle):
            errors.append("%s.account_id 與 AccountSnapshot 不一致" % prefix)
        for field in ("snapshot_id", "decision_cutoff"):
            if context.get(field) != self.bundle.get(field):
                errors.append("%s.%s 與本次 bundle 不一致" % (prefix, field))
        if context.get("citable") is not False:
            errors.append("%s.citable 必須為 false：前次決策不是證據" % prefix)
        if context.get("content_sha256") != artifact_content_sha256(context):
            errors.append("%s.content_sha256 與內容不一致" % prefix)
        if seal_prior_decision_context(context).get("context_id") != context.get("context_id"):
            errors.append("%s.context_id 與內容不一致" % prefix)
        status = context.get("status")
        if status not in PRIOR_STATUSES:
            return errors + ["%s.status 必須是 %s" % (prefix, "／".join(PRIOR_STATUSES))]
        items = context.get("items")
        uncovered = context.get("uncovered_symbols")
        if not isinstance(items, list) or not isinstance(uncovered, list):
            return errors + ["%s.items 與 uncovered_symbols 必須是陣列" % prefix]
        if status == "unavailable":
            self._validate_unavailable(context, items, uncovered, prefix, errors)
            return errors
        if context.get("reason") is not None:
            errors.append("%s.reason 在 available 時必須為 null" % prefix)
        source = context.get("source")
        if not isinstance(source, Mapping):
            return errors + ["%s.source 必須是物件" % prefix]
        self._validate_source(source, prefix + ".source", errors)
        same = source.get("snapshot_id") == self.bundle.get("snapshot_id")
        if context.get("same_snapshot") is not same or context.get("cross_snapshot_exception") is not (not same):
            errors.append("%s 的 same_snapshot／cross_snapshot_exception 與來源 snapshot 不一致" % prefix)
        seen: List[str] = []
        for index, item in enumerate(items):
            item_prefix = "%s.items[%d]" % (prefix, index)
            if not isinstance(item, Mapping):
                errors.append("%s 必須是物件" % item_prefix)
                continue
            reject_unknown_fields(item, ITEM_FIELDS, item_prefix, errors)
            symbol = str(item.get("symbol", "")).upper()
            seen.append(symbol)
            if symbol not in self.held:
                errors.append("%s 不是目前持股：%s" % (item_prefix, symbol))
            intent = item.get("intent")
            if intent not in INTENTS:
                errors.append("%s.intent 不是合法意圖" % item_prefix)
            conviction = item.get("conviction")
            if intent in _SIZED_INTENTS and conviction not in CONVICTION_LEVELS:
                errors.append("%s buy／add 必須有信心等級" % item_prefix)
            if intent not in _SIZED_INTENTS and conviction is not None:
                errors.append("%s 只有 buy／add 可以有信心等級" % item_prefix)
            try:
                string_list(item, "invalidation_conditions")
            except DecisionToolError as error:
                errors.append("%s.%s" % (item_prefix, error))
        if len(seen) != len(set(seen)):
            errors.append("%s.items 股票不得重複" % prefix)
        if sorted(set(seen) | {str(symbol).upper() for symbol in uncovered}) != self.held or set(seen) & set(uncovered):
            errors.append("%s 的 items 與 uncovered_symbols 必須剛好涵蓋目前持股" % prefix)
        return errors

    def _validate_unavailable(
        self, context: Mapping[str, object], items: List[object], uncovered: List[object], prefix: str, errors: List[str]
    ) -> None:
        try:
            required_string(context, "reason")
        except DecisionToolError:
            errors.append("%s unavailable 時必須說明 reason" % prefix)
        if context.get("source") is not None or context.get("same_snapshot") is not None:
            errors.append("%s unavailable 時不得有來源或 snapshot 比對" % prefix)
        if context.get("cross_snapshot_exception") is not False or items:
            errors.append("%s unavailable 時不得有例外旗標或 items" % prefix)
        if sorted(str(symbol).upper() for symbol in uncovered) != self.held:
            errors.append("%s unavailable 時 uncovered_symbols 必須等於目前持股" % prefix)

    def _validate_source(self, source: Mapping[str, object], prefix: str, errors: List[str]) -> None:
        reject_unknown_fields(source, SOURCE_FIELDS, prefix, errors)
        for field in sorted(SOURCE_FIELDS):
            try:
                required_string(source, field)
            except DecisionToolError as error:
                errors.append("%s.%s" % (prefix, error))
        if source.get("decision_status") not in USABLE_DECISION_STATUSES:
            errors.append("%s.decision_status 必須是 approved／no_trade" % prefix)
        if source.get("selection") not in SELECTIONS:
            errors.append("%s.selection 必須是 %s" % (prefix, "／".join(SELECTIONS)))
        try:
            if parse_time(source.get("decision_cutoff"), prefix + ".decision_cutoff") >= self.cutoff:
                errors.append("%s.decision_cutoff 必須早於本次 cutoff" % prefix)
        except DecisionToolError as error:
            errors.append(str(error))
        try:
            prior_session = parse_time(source.get("target_session_start"), prefix + ".target_session_start")
            if prior_session >= _session_start(self.bundle, "本次 bundle"):
                errors.append("%s.target_session_start 必須早於本次目標交易時段" % prefix)
        except DecisionToolError as error:
            errors.append(str(error))
