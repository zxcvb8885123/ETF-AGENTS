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

    def build_result(self, symbols, labels, run_id: str) -> Dict[str, object]:
        """全池聚合已標註資料；不使用模型補值或建立交易方向。"""
        if not isinstance(labels, list):
            raise PerceptionToolError("labels 必須是陣列")
        universe = sorted(set(str(symbol).upper() for symbol in symbols))
        if not universe or any(str(row.get("symbol", "")).upper() not in universe for row in labels):
            raise PerceptionToolError("標籤股票必須屬於指定交易池")
        items = []
        for symbol in universe:
            selected = [row for row in labels if str(row.get("symbol", "")).upper() == symbol]
            sentiment = self.tools.aggregate_sentiment(symbol, selected)
            available = sentiment["status"] == "available"
            evidence = set(sentiment["evidence_ids"])
            items.append({"symbol": symbol, "event_result_ids": [], "sentiment_labels": selected,
                          "sentiment": sentiment,
                          "priced_in_assessment": "unknown", "rationale": "由已授權資料確定性聚合；未判斷市場定價。",
                          "evidence_ids": sorted(evidence), "risk_flags": sentiment["manipulation_flags"],
                          "research_status": "usable_secondary" if available else "unavailable",
                          "status_reason": "僅作次級研究輸入。" if available else "來源不存在或覆蓋不足。"})
        result = {"schema_version": "1.0", "run_id": run_id, "snapshot_id": self.tools.snapshot_id,
                  "decision_cutoff": self.tools.decision_cutoff, "skill_version": "1.0.0",
                  "status": "completed" if all(row["research_status"] == "usable_secondary" for row in items) else "degraded",
                  "items": items, "errors": []}
        errors = MarketPerceptionResultValidator(self.tools).validate(result)
        if errors:
            raise PerceptionToolError("；".join(errors))
        return result

    def validate_file(
        self,
        input_path: Path,
        output_path: Optional[Path] = None,
    ) -> Dict[str, object]:
        payload = self.read_json(input_path, " MarketPerceptionResult")
        errors = MarketPerceptionResultValidator(self.tools).validate(payload)
        response: Dict[str, object] = {"valid": not errors, "errors": errors}
        if not errors and output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            response["output"] = str(output_path)
        return response
