"""以原始輸入重建整份 ResearchReport 並拒絕任何改寫。"""

from __future__ import annotations

from typing import List, Mapping, Optional

from .builder import ResearchReportBuilder
from .contracts import FORBIDDEN_REPORT_FIELDS, REPORT_STATUSES, ResearchReportError


class ResearchReportValidator:
    """Rebuild the report and reject any altered narrative or number."""

    def __init__(self, builder: ResearchReportBuilder):
        self.builder = builder

    def validate(self, report: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        if report.get("schema_version") != "1.0":
            errors.append("schema_version 必須為 1.0")
        if report.get("status") not in REPORT_STATUSES:
            errors.append("status 必須是 completed 或 degraded")
        for field in ("report_id", "generated_at", "snapshot_id", "decision_cutoff"):
            if not isinstance(report.get(field), str) or not str(report.get(field)).strip():
                errors.append("缺少必要字串欄位：%s" % field)
        forbidden = FORBIDDEN_REPORT_FIELDS.intersection(report.keys())
        if forbidden:
            errors.append("ResearchReport 不得包含交易欄位：%s" % ", ".join(sorted(forbidden)))
        try:
            expected = self.builder.build(
                report_id=str(report.get("report_id", "")),
                generated_at=str(report.get("generated_at", "")),
            )
            difference = _first_difference(expected, report)
            if difference is not None:
                errors.append("ResearchReport 與確定性重建結果不一致：%s" % difference)
        except ResearchReportError as error:
            errors.append(str(error))
        return errors


def _first_difference(expected: object, actual: object, path: str = "$") -> Optional[str]:
    if type(expected) is not type(actual):
        return "%s 型別不同" % path
    if isinstance(expected, Mapping):
        expected_keys = set(expected)
        actual_keys = set(actual)
        if expected_keys != actual_keys:
            return "%s 欄位不同" % path
        for key in sorted(expected_keys):
            difference = _first_difference(expected[key], actual[key], "%s.%s" % (path, key))
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return "%s 長度不同" % path
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
            difference = _first_difference(
                expected_item, actual_item, "%s[%d]" % (path, index)
            )
            if difference is not None:
                return difference
        return None
    if expected != actual:
        return "%s 值不同" % path
    return None
