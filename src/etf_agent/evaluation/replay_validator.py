"""回放報告的重算驗證：封存雜湊、content_sha256，再以報告記錄的輸入整段重跑並逐欄比對。"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Mapping, Optional

from etf_agent.core import canonical_sha256

from .contracts import ReplayError
from .replay import REPLAY_ROOT, ReplayRepository, build_replay, read_replay, replay_body_sha256
from .replay_tools import file_sha256

# 重算必須完全一致的欄位；provenance 的程式版本與 tradeagent 雜湊只警告（程式變了但結果相同仍算通過）。
REBUILT_FIELDS = ("schema_version", "status", "inputs", "start_account", "days", "not_replayed_run_ids", "nav_series", "metrics", "limitations")
REBUILT_PROVENANCE = ("policy_template", "runs", "execution_price_rule", "settlement_calendar_basis")


def verify_replay(
    root: Path, replay_id: str, *, policy_template_path: Optional[Path] = None, decision_root: Optional[Path] = None,
    database: Optional[Path] = None, log=lambda _: None,
) -> Dict[str, object]:
    """回傳 {valid, errors, warnings}。任何重算差異都視為竄改或輸入改變，不猜原因。"""
    errors: List[str] = []
    warnings: List[str] = []
    try:
        ReplayRepository(Path(root) / REPLAY_ROOT).verify(replay_id)
    except ReplayError as error:
        return {"valid": False, "errors": ["回放封存驗證失敗：%s" % error], "warnings": warnings}
    report, _ = read_replay(root, replay_id)
    if report.get("content_sha256") != replay_body_sha256(report):
        return {"valid": False, "errors": ["回放報告 content_sha256 與內容不一致"], "warnings": warnings}
    inputs = report["inputs"]
    template_path = Path(policy_template_path or inputs["policy_template_path"])
    try:
        rebuilt, _ = build_replay(
            root, template_path, decision_root=Path(decision_root or inputs["decision_root"]),
            database=Path(database or inputs["database"]), as_of=str(inputs["as_of"]), run_ids=list(inputs["run_ids"]),
            start=inputs.get("start"), end=inputs.get("end"), allow_gaps=bool(inputs.get("allow_gaps")),
            initial_cash=inputs.get("initial_cash"), log=log,
        )
    except ReplayError as error:
        return {"valid": False, "errors": ["重算失敗：%s" % error], "warnings": warnings}
    # 重算時的路徑可能不同（驗證者指定複本），只比較內容欄位。
    for field in REBUILT_FIELDS:
        left, right = report.get(field), rebuilt.get(field)
        if field == "inputs":
            left = {k: v for k, v in dict(left).items() if k not in {"policy_template_path", "decision_root", "database"}}
            right = {k: v for k, v in dict(right).items() if k not in {"policy_template_path", "decision_root", "database"}}
        if canonical_sha256(left) != canonical_sha256(right):
            errors.append("回放報告的 %s 與重算不一致" % field)
    for field in REBUILT_PROVENANCE:
        if canonical_sha256(report["provenance"].get(field)) != canonical_sha256(rebuilt["provenance"].get(field)):
            errors.append("回放報告的 provenance.%s 與重算不一致" % field)
    for field in ("code_version", "tradeagent"):
        if canonical_sha256(report["provenance"].get(field)) != canonical_sha256(rebuilt["provenance"].get(field)):
            warnings.append("provenance.%s 已改變（程式或 Skill 檔案與回放當時不同），重算結果仍一致" % field
                            if not errors else "provenance.%s 已改變" % field)
    if policy_template_path is not None:
        warnings.append("使用指定的策略樣板重算：%s（sha256 %s）" % (template_path, file_sha256(template_path)))
    return {"valid": not errors, "errors": errors, "warnings": warnings}
