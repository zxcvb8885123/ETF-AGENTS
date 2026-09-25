"""Event Research Agent 的對外應用服務。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from .contracts import ResearchToolError
from .tools import SnapshotResearchTools


class EventResearchApplicationService:
    """Stable facade used by the Skill CLI and future runtimes."""

    def __init__(self, tools: SnapshotResearchTools):
        self.tools = tools

    @classmethod
    def from_paths(cls, snapshot_path: Path, database_path: Path):
        return cls(SnapshotResearchTools.from_paths(snapshot_path, database_path))

    def validate_file(self, input_path: Path, output_path: Optional[Path] = None) -> Dict[str, object]:
        try:
            payload = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ResearchToolError("無法讀取 ResearchResult：%s" % error) from error
        errors = self.tools.validate_result(payload)
        response: Dict[str, object] = {"valid": not errors, "errors": errors}
        if not errors and output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            response["output"] = str(output_path)
        return response

    def validate_debate_file(
        self, input_path: Path, output_path: Optional[Path] = None
    ) -> Dict[str, object]:
        try:
            payload = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ResearchToolError("無法讀取 DebateBundle：%s" % error) from error
        errors = self.tools.validate_debate_bundle(payload)
        response: Dict[str, object] = {"valid": not errors, "errors": errors}
        if not errors and output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            response["output"] = str(output_path)
        return response
