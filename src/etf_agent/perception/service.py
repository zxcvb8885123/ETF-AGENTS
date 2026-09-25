"""市場情緒與分析師 Agent 的對外應用服務。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Mapping, Optional

from .contracts import PerceptionToolError
from .tools import PerceptionDataTools
from .validator import MarketPerceptionResultValidator


class MarketPerceptionApplicationService:
    """Facade used by the Skill CLI and future runtimes."""

    def __init__(self, tools: PerceptionDataTools):
        self.tools = tools

    @classmethod
    def from_path(cls, bundle_path: Path) -> "MarketPerceptionApplicationService":
        return cls(PerceptionDataTools.from_path(bundle_path))

    @staticmethod
    def read_json(path: Path, label: str) -> Mapping[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PerceptionToolError("無法讀取%s：%s" % (label, error)) from error
        if not isinstance(payload, Mapping):
            raise PerceptionToolError("%s必須是 JSON 物件" % label)
        return payload

    def validate_file(
        self,
        input_path: Path,
        output_path: Optional[Path] = None,
        event_result_path: Optional[Path] = None,
    ) -> Dict[str, object]:
        payload = self.read_json(input_path, " MarketPerceptionResult")
        event_result = (
            self.read_json(event_result_path, " ResearchResult")
            if event_result_path is not None
            else None
        )
        errors = MarketPerceptionResultValidator(self.tools, event_result).validate(payload)
        response: Dict[str, object] = {"valid": not errors, "errors": errors}
        if not errors and output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            response["output"] = str(output_path)
        return response

    def compare_event_file(
        self,
        event_result_path: Path,
        event_id: str,
        fact_name: str,
        metric: str,
        forecast_period: str,
        threshold_pct: float = 2.0,
    ) -> Dict[str, object]:
        event_result = self.read_json(event_result_path, " ResearchResult")
        return self.tools.compare_event_expectations(
            event_result,
            event_id,
            fact_name,
            metric,
            forecast_period,
            threshold_pct,
        )
