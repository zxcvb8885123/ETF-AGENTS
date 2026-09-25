"""Research Report V0 的對外應用服務。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Mapping, Optional

from .builder import ResearchReportBuilder
from .contracts import ResearchReportError
from .renderer import ResearchReportMarkdownRenderer
from .validator import ResearchReportValidator


class ResearchReportApplicationService:
    """File-oriented facade for the research-report Skill CLI."""

    @staticmethod
    def read_json(path: Path, label: str) -> Mapping[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ResearchReportError("無法讀取 %s：%s" % (label, error)) from error
        if not isinstance(payload, Mapping):
            raise ResearchReportError("%s 必須是 JSON 物件" % label)
        return payload

    @classmethod
    def from_paths(
        cls,
        snapshot_path: Path,
        research_path: Path,
        perception_path: Optional[Path] = None,
        perception_bundle_path: Optional[Path] = None,
    ) -> "ResearchReportApplicationService":
        if (perception_path is None) != (perception_bundle_path is None):
            raise ResearchReportError(
                "--perception 與 --perception-bundle 必須同時提供"
            )
        snapshot = cls.read_json(snapshot_path, "ResearchSnapshot")
        research = cls.read_json(research_path, "ResearchResult")
        perception = (
            cls.read_json(perception_path, "MarketPerceptionResult")
            if perception_path is not None
            else None
        )
        bundle = (
            cls.read_json(perception_bundle_path, "PerceptionDataBundle")
            if perception_bundle_path is not None
            else None
        )
        return cls(ResearchReportBuilder(snapshot, research, perception, bundle))

    def __init__(self, builder: ResearchReportBuilder):
        self.builder = builder
        self.renderer = ResearchReportMarkdownRenderer()

    def build_files(
        self,
        json_output: Path,
        markdown_output: Path,
        report_id: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> Dict[str, object]:
        report = self.builder.build(report_id=report_id, generated_at=generated_at)
        errors = ResearchReportValidator(self.builder).validate(report)
        if errors:
            raise ResearchReportError("報告建立後驗證失敗：%s" % "; ".join(errors))
        markdown = self.renderer.render(report)
        json_output.parent.mkdir(parents=True, exist_ok=True)
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        markdown_output.write_text(markdown, encoding="utf-8")
        return {
            "valid": True,
            "report_id": report["report_id"],
            "status": report["status"],
            "json_output": str(json_output),
            "markdown_output": str(markdown_output),
        }

    def validate_file(self, input_path: Path) -> Dict[str, object]:
        report = self.read_json(input_path, "ResearchReport")
        errors = ResearchReportValidator(self.builder).validate(report)
        return {"valid": not errors, "errors": errors}
