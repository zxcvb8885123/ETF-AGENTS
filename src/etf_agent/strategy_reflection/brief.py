"""把校準報告與目前策略樣板整理成帶 evidence_id 的簡報；Agent 只能引用簡報內的統計。"""

from __future__ import annotations

from typing import Dict, Mapping

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


def build_brief(report: Mapping[str, object], template: Mapping[str, object], as_of: str) -> Dict[str, object]:
    """``report`` 是 ``calibration.evaluate`` 的輸出。只收 status=ok 的 horizon，樣本不足的 horizon 不給任何統計。"""
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
    brief["brief_sha256"] = canonical_sha256({key: value for key, value in brief.items()})
    return brief
