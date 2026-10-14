"""以原始封存重建 JSON 及 Markdown。"""

from .builder import build_report
from .renderer import render_report
from .contracts import DeliveryError


def validate_report(artifacts, run_id, report, markdown):
    expected = build_report(artifacts, run_id)
    if report != expected or markdown != render_report(expected):
        raise DeliveryError("最終報告 JSON／Markdown 與完整重建不一致")
