"""基本面研究 Agent 的對外應用服務。"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping, Optional

from etf_agent.core import canonical_json
from .contracts import FundamentalToolError, _read_json
from .metrics import FundamentalMetricsCalculator
from .tools import FundamentalSnapshotTools
from .validator import FundamentalResearchResultValidator


class FundamentalResearchApplicationService:
    """CLI 使用的檔案外觀；不讀取網路，也不寫入 Data Agent 資料庫。"""

    def __init__(self, tools: FundamentalSnapshotTools):
        self.tools = tools

    @classmethod
    def from_snapshot_path(cls, snapshot_path: Path) -> "FundamentalResearchApplicationService":
        return cls(FundamentalSnapshotTools.from_path(snapshot_path))

    def build_bundle_file(
        self,
        output: Path,
        request: Mapping[str, object],
        *,
        bundle_id: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> Dict[str, object]:
        bundle = self.tools.build_bundle(request, bundle_id=bundle_id, generated_at=generated_at)
        self._write_json(output, bundle)
        return bundle

    def compute_metrics_file(
        self,
        bundle_path: Path,
        output: Path,
        *,
        metrics_id: Optional[str] = None,
        computed_at: Optional[str] = None,
    ) -> Dict[str, object]:
        calculator = FundamentalMetricsCalculator(self.tools, _read_json(bundle_path, "FundamentalDataBundle"))
        metrics = calculator.compute(metrics_id=metrics_id, computed_at=computed_at)
        self._write_json(output, metrics)
        return metrics

    def validate_result_file(
        self,
        bundle_path: Path,
        metrics_path: Path,
        input_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        bundle = _read_json(bundle_path, "FundamentalDataBundle")
        metrics = _read_json(metrics_path, "FundamentalMetrics")
        result = _read_json(input_path, "FundamentalResearchResult")
        validator = FundamentalResearchResultValidator(self.tools, bundle, metrics)
        errors = validator.validate(result)
        normalized = validator.normalized(result) if not errors else None
        if normalized is not None and output is not None:
            self._write_json(output, normalized)
        return {"valid": not errors, "errors": errors, "result": normalized}

    def archive_result_file(
        self,
        bundle_path: Path,
        metrics_path: Path,
        input_path: Path,
        output: Path,
    ) -> Dict[str, object]:
        result = self.validate_result_file(bundle_path, metrics_path, input_path)
        if not result["valid"]:
            return result
        if output.exists():
            raise FundamentalToolError("archive 輸出已存在，拒絕覆寫：%s" % output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            handle.write(canonical_json(result["result"]))
            handle.write("\n")
        return result

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
