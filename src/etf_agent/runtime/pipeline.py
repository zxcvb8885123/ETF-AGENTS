"""Small, provider-neutral building blocks for immutable pipeline run records."""

from __future__ import annotations

from typing import Dict, Mapping, Optional, Sequence

from etf_agent.core import content_sha256


class PipelineRuntimeError(ValueError):
    """Raised when a runtime record cannot retain its execution identity."""


def build_pipeline_run(
    *,
    schema_version: str,
    pipeline_run_id: str,
    execution_mode: str,
    status: str,
    started_at: str,
    ended_at: str,
    snapshot_id: Optional[str],
    decision_cutoff: Optional[str],
    business_date: Optional[str],
    idempotency_key: Optional[str],
    input_fingerprint: Optional[str],
    stages: Sequence[Mapping[str, object]],
    errors: Sequence[str],
) -> Dict[str, object]:
    """Create a hash-sealed pipeline record without making any market decision."""
    for field, value in (
        ("schema_version", schema_version),
        ("pipeline_run_id", pipeline_run_id),
        ("execution_mode", execution_mode),
        ("status", status),
        ("started_at", started_at),
        ("ended_at", ended_at),
    ):
        if not isinstance(value, str) or not value.strip():
            raise PipelineRuntimeError("%s 必須是非空字串" % field)
    if any(not isinstance(stage, Mapping) for stage in stages):
        raise PipelineRuntimeError("stages 必須是物件陣列")
    if any(not isinstance(error, str) or not error.strip() for error in errors):
        raise PipelineRuntimeError("errors 必須是非空字串陣列")
    result: Dict[str, object] = {
        "schema_version": schema_version,
        "pipeline_run_id": pipeline_run_id,
        "execution_mode": execution_mode,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "snapshot_id": snapshot_id,
        "decision_cutoff": decision_cutoff,
        "business_date": business_date,
        "idempotency_key": idempotency_key,
        "input_fingerprint": input_fingerprint,
        "stages": [dict(stage) for stage in stages],
        "errors": list(errors),
    }
    result["content_sha256"] = content_sha256(result)
    return result
