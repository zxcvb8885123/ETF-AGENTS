"""姿態內現金子等級（``position_sizing.cash_ratio_range``，版本閘門）的前次現金比例 anchor。

單日上限只限制現金拉高：今日可選格點 ≤ max(該姿態 lower, anchor + max_daily_raise)。anchor 由每日 pipeline 綁進 Policy：

- ``prior_decision``：前次決策 run（與前次決策背景同一個 run：同帳戶、approved／no_trade、目標交易時段嚴格早於本次，
  同時段優先帳本結算記錄）最終 ``proposal.effective_policy.cash_buffer_rate``（已含修正循環的 increase_cash_buffer）。
  先以 manifest 驗證整個 run，再核對 proposal 雜湊與 decision 綁定的 proposal_id。
- ``account_cash``：沒有前次 run（比賽首日、帳戶重開、本次沒有目標時段）時，用本次起始現金比例 cash／NAV。

anchor 不經 ``prior_decision_context``（那份背景只給交易 Agent）；它只給風險 Agent 當背景：``citable=false``、沒有 evidence_id，
簡報不放 run_id。重建時 Finalizer 以決策庫重讀前次 run 重算 anchor 比對；``prior_decision`` 而呼叫端沒給決策庫一律拒絕；
有閘門卻沒有前次決策背景（team_inputs 2.2 以上）也拒絕，anchor 的 run_id 與 selection 都必須與背景一致。
「為什麼挑這個 run」由 ``PriorDecisionContextValidator`` 在 ``cash_ratio_range.selection_check`` 閘門下重跑挑選核對。
"""

from __future__ import annotations

import copy
import json
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from .contracts import DecisionToolError, artifact_content_sha256, decimal_value
from .prior_decision import _verify_prior, read_decision_run
from .sizing import cash_ratio_gate

ANCHOR_QUANTUM = Decimal("0.00000001")


def _ratio_text(value: Decimal) -> str:
    return str(value.quantize(ANCHOR_QUANTUM, rounding=ROUND_DOWN))


def account_cash_ratio(bundle: Mapping[str, object]) -> str:
    """本次起始現金比例 cash／NAV，無條件捨去到八位小數；缺值或 NAV 非正數即拒絕。"""
    account = bundle.get("account_snapshot")
    if not isinstance(account, Mapping):
        raise DecisionToolError("cash_ratio_range 需要 account_snapshot 才能計算起始現金比例")
    cash = decimal_value(account.get("cash"), "account_snapshot.cash")
    nav = decimal_value(account.get("nav"), "account_snapshot.nav")
    if nav <= 0 or cash < 0:
        raise DecisionToolError("cash_ratio_range 起始現金比例需要 NAV > 0 且現金 ≥ 0")
    return _ratio_text(min(cash / nav, Decimal("1")))


def _prior_anchor(bundle: Mapping[str, object], repository_root: Path, run_id: str, selection: str) -> Dict[str, object]:
    prior = read_decision_run(repository_root, run_id)  # manifest 驗證整個 run
    source = _verify_prior(bundle, prior, selection)
    from .finalization import DecisionRepository  # 延後匯入：finalization 也匯入本模組做重建驗證。

    run_dir = DecisionRepository(Path(repository_root)).verify(run_id)
    try:
        proposal = json.loads((run_dir / "proposal.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DecisionToolError("前次 Decision run %s 缺少或無法讀取 proposal：%s" % (run_id, error)) from error
    if not isinstance(proposal, Mapping) or proposal.get("content_sha256") != artifact_content_sha256(proposal):
        raise DecisionToolError("前次 Decision run %s 的 proposal 內容雜湊不一致" % run_id)
    if prior["decision"].get("proposal_id") != proposal.get("proposal_id"):  # type: ignore[union-attr]
        raise DecisionToolError("前次 Decision run %s 的 decision 與 proposal 不是同一組" % run_id)
    effective = proposal.get("effective_policy")
    if not isinstance(effective, Mapping):
        raise DecisionToolError("前次 Decision run %s 的 proposal 缺少 effective_policy" % run_id)
    ratio = decimal_value(effective.get("cash_buffer_rate"), "前次 effective_policy.cash_buffer_rate")
    if ratio < 0 or ratio > 1:
        raise DecisionToolError("前次 Decision run %s 的 cash_buffer_rate 必須介於 0 與 1" % run_id)
    return {
        "status": "prior_decision",
        "ratio": _ratio_text(ratio),
        "source": {
            "run_id": run_id,
            "selection": source["selection"],
            "target_session_start": source["target_session_start"],
            "decision_sha256": source["decision_sha256"],
            "proposal_id": str(proposal.get("proposal_id")),
            "proposal_sha256": str(proposal["content_sha256"]),
        },
    }


def cash_ratio_anchor(
    bundle: Mapping[str, object], repository_root: Optional[Path], prior_context: Optional[Mapping[str, object]],
) -> Dict[str, object]:
    """依前次決策背景挑到的 run 計算 anchor；背景 unavailable 或沒有背景時用本次起始現金比例。"""
    available = isinstance(prior_context, Mapping) and prior_context.get("status") == "available"
    if not available:
        return {"status": "account_cash", "ratio": account_cash_ratio(bundle), "source": None}
    if repository_root is None:
        raise DecisionToolError("cash_ratio_range 的前次現金比例需要決策庫，但呼叫端未提供")
    source = prior_context.get("source")  # type: ignore[union-attr]
    if not isinstance(source, Mapping):
        raise DecisionToolError("前次決策背景缺少 source，無法取得前次現金比例")
    return _prior_anchor(bundle, Path(repository_root), str(source.get("run_id")), str(source.get("selection")))


def bind_cash_ratio_anchor(
    policy: Mapping[str, object],
    bundle: Mapping[str, object],
    repository_root: Optional[Path],
    prior_context: Optional[Mapping[str, object]],
) -> Dict[str, object]:
    """樣板啟用 ``cash_ratio_range`` 時把 anchor 綁進 Policy；沒有閘門原樣回傳。呼叫端須重算 content_sha256。

    ``prior_context`` 必須是本次交給交易 Agent 的同一份前次決策背景，確保兩者指向同一個前次 run。"""
    bound = copy.deepcopy(dict(policy))
    gate = cash_ratio_gate(bound.get("position_sizing") or {})
    if gate is None:
        return bound
    if "anchor" in gate:
        raise DecisionToolError("策略樣板的 cash_ratio_range 不得預先填 anchor（由 pipeline 綁定）")
    gate["anchor"] = cash_ratio_anchor(bundle, repository_root, prior_context)  # type: ignore[index]
    return bound


def validate_cash_ratio_anchor(
    policy: Mapping[str, object],
    bundle: Mapping[str, object],
    repository_root: Optional[Path],
    prior_context: Optional[Mapping[str, object]],
) -> List[str]:
    """重建驗證 anchor：與前次決策背景指向同一個 run，並以決策庫或本次帳戶重算比對；fail-closed。"""
    gate = cash_ratio_gate(policy.get("position_sizing") or {})
    if gate is None:
        return []
    prefix = "DecisionPolicy.position_sizing.cash_ratio_range.anchor"
    anchor = gate.get("anchor")
    if not isinstance(anchor, Mapping):
        return ["%s 缺少或不是物件" % prefix]
    status = anchor.get("status")
    # 沒有前次決策背景就無法核對「同一個 run」：cash_ratio_range 的 run 一定有 team_inputs 2.2 以上的背景，缺少即拒絕。
    if not isinstance(prior_context, Mapping):
        return ["%s 需要 team_inputs 2.2 以上的前次決策背景才能核對是否指向同一個前次 run，但呼叫端未提供" % prefix]
    available = prior_context.get("status") == "available"
    errors: List[str] = []
    if available and status != "prior_decision":
        errors.append("%s 必須來自前次決策（前次決策背景為 available），不得改用本次帳戶現金" % prefix)
    if not available and status != "account_cash":
        errors.append("%s 在前次決策背景 unavailable 時必須是 account_cash" % prefix)
    source = anchor.get("source")
    prior_source = prior_context.get("source")
    if available and isinstance(source, Mapping) and isinstance(prior_source, Mapping):
        if source.get("run_id") != prior_source.get("run_id"):
            errors.append("%s.source.run_id 與前次決策背景的 run_id 不一致" % prefix)
        if source.get("selection") != prior_source.get("selection"):
            errors.append("%s.source.selection 與前次決策背景的 selection 不一致" % prefix)
    if errors:
        return errors
    if status == "prior_decision":
        if repository_root is None:
            return ["%s 需要決策庫重讀前次 run，但呼叫端未提供決策庫" % prefix]
        source = anchor.get("source")
        if not isinstance(source, Mapping):
            return ["%s.source 必須是物件" % prefix]
        try:
            expected = _prior_anchor(bundle, Path(repository_root), str(source.get("run_id")), str(source.get("selection")))
        except DecisionToolError as error:
            return ["%s 無法由前次 run 重算：%s" % (prefix, error)]
    elif status == "account_cash":
        try:
            expected = {"status": "account_cash", "ratio": account_cash_ratio(bundle), "source": None}
        except DecisionToolError as error:
            return ["%s 無法由本次帳戶重算：%s" % (prefix, error)]
    else:
        return ["%s.status 無效" % prefix]
    if dict(anchor) != expected:
        errors.append("%s 與重算結果不一致" % prefix)
    return errors


__all__ = ["account_cash_ratio", "bind_cash_ratio_anchor", "cash_ratio_anchor", "validate_cash_ratio_anchor"]
