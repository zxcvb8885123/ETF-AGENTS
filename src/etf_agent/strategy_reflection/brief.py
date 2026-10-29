"""把校準報告與目前策略樣板整理成帶 evidence_id 的簡報；Agent 只能引用簡報內的統計。"""

from __future__ import annotations

from typing import Dict, Mapping, Optional

from etf_agent.calibration.contracts import MIN_DECISION_DAYS
from etf_agent.core import canonical_sha256

from .contracts import GATE_HORIZONS, SCHEMA_VERSION
from .tools import current_knobs

_SKIP_KEYS = {"pending_days", "excluded_obs"}


def _flatten(prefix: str, value: object, out: Dict[str, str]) -> None:
    if isinstance(value, Mapping):
        for key in sorted(value):
            if key not in _SKIP_KEYS:
                _flatten("%s.%s" % (prefix, key), value[key], out)
    elif value is not None and not isinstance(value, (list, tuple)):
        out[prefix] = str(value)


def _flatten_ok(prefix: str, value: object, out: Dict[str, str]) -> None:
    """診斷包只收 status=ok 的項目；樣本不足或資料缺漏的項目不產生任何可引用的證據。"""
    if isinstance(value, Mapping):
        if "status" in value and value["status"] != "ok":
            return
        for key in sorted(value):
            if key not in _SKIP_KEYS and key not in {"status", "reason", "required_days", "required_sells", "schema_version"}:
                _flatten_ok("%s.%s" % (prefix, key), value[key], out)
    elif value is not None and not isinstance(value, (list, tuple)):
        out[prefix] = str(value)


def build_brief(
    report: Mapping[str, object], template: Mapping[str, object], as_of: str,
    diagnostics: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """``report`` 是 ``calibration.evaluate`` 的輸出。只收 status=ok 的 horizon，樣本不足的 horizon 不給任何統計。

    ``diagnostics`` 是 ``diagnostics.build_diagnostics`` 的結果（每週診斷包，只含數值與列舉）：完整內容放在簡報供閱讀，
    只有 status=ok 的指標才成為 ``diag.*`` 證據。沒有診斷包的舊簡報與舊測試不受影響。"""
    evidence: Dict[str, str] = {}
    ok_horizons = []
    for horizon, entry in sorted(report["horizons"].items()):
        if entry.get("status") == "ok":
            ok_horizons.append(horizon)
            _flatten("cal.h%s" % horizon, entry, evidence)
    knobs = current_knobs(template)
    for path, value in knobs.items():
        evidence["policy.%s" % path] = value
    evidence["cal.decision_days"] = str(report["decision_days"])
    if diagnostics is not None:
        _flatten_ok("diag", diagnostics, evidence)
    brief: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of,
        "decision_days": int(report["decision_days"]),
        "required_decision_days": MIN_DECISION_DAYS,
        "ok_horizons": ok_horizons,
        "gate_open": any(horizon in ok_horizons for horizon in GATE_HORIZONS),
        "base_policy_id": template.get("policy_id"),
        "base_policy_sha256": canonical_sha256(template),
        "knobs": knobs,
        "evidence": evidence,
    }
    if diagnostics is not None:
        brief["diagnostics"] = dict(diagnostics)
    brief["brief_sha256"] = canonical_sha256({key: value for key, value in brief.items()})
    return brief
