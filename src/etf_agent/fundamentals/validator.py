"""FundamentalResearchResult 的欄位白名單、引用與重算驗證。"""

from __future__ import annotations

from typing import Dict, List, Mapping

from etf_agent.core import content_sha256
from .contracts import (
    FORBIDDEN_RESULT_FIELDS,
    RESULT_SCHEMA_VERSION,
    FundamentalToolError,
    _optional_string_list,
    _required_string,
    _same_time,
    _string_list,
    _with_content_sha256,
)
from .metrics import FundamentalMetricsCalculator
from .tools import FundamentalSnapshotTools


class FundamentalResearchResultValidator:
    """驗證 Agent 解讀只引用同一資料包與可重算指標。"""

    def __init__(
        self,
        tools: FundamentalSnapshotTools,
        bundle: Mapping[str, object],
        metrics: Mapping[str, object],
    ):
        self.tools = tools
        self.bundle = dict(bundle)
        self.metrics = dict(metrics)
        self.calculator = FundamentalMetricsCalculator(tools, bundle)
        metric_errors = self.calculator.validate(metrics)
        if metric_errors:
            raise FundamentalToolError("FundamentalMetrics 無效：%s" % "; ".join(metric_errors))
        self.companies = {
            str(item["symbol"]): dict(item) for item in self.bundle["companies"]
        }
        self.metric_companies = {
            str(item["symbol"]): dict(item) for item in self.metrics["items"]
        }

    def validate(self, payload: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        try:
            self._validate_payload(payload)
        except FundamentalToolError as error:
            errors.append(str(error))
        return errors

    def normalized(self, payload: Mapping[str, object]) -> Dict[str, object]:
        errors = self.validate(payload)
        if errors:
            raise FundamentalToolError("FundamentalResearchResult 無效：%s" % "; ".join(errors))
        normalized = dict(payload)
        normalized.pop("content_sha256", None)
        return _with_content_sha256(normalized)

    def _validate_payload(self, payload: Mapping[str, object]) -> None:
        if not isinstance(payload, Mapping):
            raise FundamentalToolError("FundamentalResearchResult 必須是物件")
        self._validate_forbidden_fields(payload)
        allowed = {
            "schema_version",
            "run_id",
            "snapshot_id",
            "decision_cutoff",
            "bundle_id",
            "bundle_sha256",
            "metrics_id",
            "metrics_sha256",
            "skill_version",
            "status",
            "items",
            "errors",
            "content_sha256",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise FundamentalToolError("FundamentalResearchResult 有未允許欄位：%s" % ", ".join(sorted(unknown)))
        if payload.get("schema_version") != RESULT_SCHEMA_VERSION:
            raise FundamentalToolError("FundamentalResearchResult schema_version 必須為 1.0")
        _required_string(payload, "run_id")
        if payload.get("snapshot_id") != self.bundle.get("snapshot_id"):
            raise FundamentalToolError("FundamentalResearchResult.snapshot_id 不一致")
        if not _same_time(
            payload.get("decision_cutoff"), self.bundle.get("decision_cutoff"), "decision_cutoff"
        ):
            raise FundamentalToolError("FundamentalResearchResult.decision_cutoff 不一致")
        for field, expected in (
            ("bundle_id", self.bundle.get("bundle_id")),
            ("bundle_sha256", self.bundle.get("content_sha256")),
            ("metrics_id", self.metrics.get("metrics_id")),
            ("metrics_sha256", self.metrics.get("content_sha256")),
        ):
            if payload.get(field) != expected:
                raise FundamentalToolError("FundamentalResearchResult.%s 不一致" % field)
        if payload.get("skill_version") != "1.0.0":
            raise FundamentalToolError("FundamentalResearchResult.skill_version 不支援")
        items = payload.get("items")
        if not isinstance(items, list):
            raise FundamentalToolError("FundamentalResearchResult.items 必須是陣列")
        expected_symbols = list(self.bundle["request"]["symbols"])
        received_symbols = [_required_string(item, "symbol").upper() if isinstance(item, Mapping) else "" for item in items]
        if received_symbols != expected_symbols:
            raise FundamentalToolError("FundamentalResearchResult.items 必須完整且依 request.symbols 排序")
        statuses: List[str] = []
        for item in items:
            statuses.append(self._validate_item(dict(item)))
        expected_status = (
            "completed"
            if statuses and set(statuses) == {"completed"}
            else "unavailable"
            if statuses and set(statuses) == {"unavailable"}
            else "degraded"
        )
        if payload.get("status") != expected_status:
            raise FundamentalToolError(
                "FundamentalResearchResult.status 應為 %s" % expected_status
            )
        errors = payload.get("errors")
        if not isinstance(errors, list) or any(
            not isinstance(item, str) for item in errors
        ):
            raise FundamentalToolError("FundamentalResearchResult.errors 必須是字串陣列")
        supplied_hash = payload.get("content_sha256")
        if supplied_hash is not None and supplied_hash != content_sha256(payload):
            raise FundamentalToolError("FundamentalResearchResult.content_sha256 不一致")

    def _validate_item(self, item: Mapping[str, object]) -> str:
        allowed = {
            "symbol",
            "research_status",
            "status_reason",
            "observations",
            "assumptions",
            "invalidation_conditions",
            "limitations",
            "evidence_ids",
            "metric_ids",
        }
        unknown = set(item) - allowed
        if unknown:
            raise FundamentalToolError("基本面 item 有未允許欄位：%s" % ", ".join(sorted(unknown)))
        symbol = _required_string(item, "symbol").upper()
        company = self.companies.get(symbol)
        metric_company = self.metric_companies.get(symbol)
        if company is None or metric_company is None:
            raise FundamentalToolError("基本面 item 引用不存在的股票：%s" % symbol)
        expected = str(metric_company["status"])
        if item.get("research_status") != expected:
            raise FundamentalToolError("%s.research_status 應為 %s" % (symbol, expected))
        _required_string(item, "status_reason")
        observations = item.get("observations")
        if not isinstance(observations, list):
            raise FundamentalToolError("%s.observations 必須是陣列" % symbol)
        assumptions = _optional_string_list(item, "assumptions")
        invalidations = _optional_string_list(item, "invalidation_conditions")
        limitations = _optional_string_list(item, "limitations")
        if expected == "unavailable" and not limitations:
            raise FundamentalToolError("%s unavailable 結果必須列出 limitations" % symbol)

        available_metrics = {
            str(metric["metric_id"]): metric
            for metric in metric_company["metrics"]
            if metric["status"] == "available"
        }
        allowed_evidence = {
            str(statement["source_evidence_id"]) for statement in company["statements"]
        }
        all_observation_evidence: List[str] = []
        all_observation_metrics: List[str] = []
        for index, observation in enumerate(observations):
            if not isinstance(observation, Mapping):
                raise FundamentalToolError("%s.observations[%d] 必須是物件" % (symbol, index))
            observation_allowed = {"text", "evidence_ids", "metric_ids"}
            if set(observation) - observation_allowed:
                raise FundamentalToolError("%s.observations[%d] 有未允許欄位" % (symbol, index))
            text = _required_string(observation, "text")
            if len(text) > 2000:
                raise FundamentalToolError("%s.observations[%d].text 過長" % (symbol, index))
            evidence_ids = _string_list(observation, "evidence_ids")
            metric_ids = _optional_string_list(observation, "metric_ids")
            if not set(evidence_ids).issubset(allowed_evidence):
                raise FundamentalToolError("%s.observations[%d] 引用不存在或跨股票證據" % (symbol, index))
            if not set(metric_ids).issubset(available_metrics):
                raise FundamentalToolError("%s.observations[%d] 引用不可用或跨股票指標" % (symbol, index))
            all_observation_evidence.extend(evidence_ids)
            all_observation_metrics.extend(metric_ids)
        if expected != "unavailable" and not observations:
            raise FundamentalToolError("%s 可用結果至少要有一則 observation" % symbol)
        evidence_ids = _optional_string_list(item, "evidence_ids")
        metric_ids = _optional_string_list(item, "metric_ids")
        if evidence_ids != sorted(set(all_observation_evidence)):
            raise FundamentalToolError("%s.evidence_ids 必須等於 observations 的去重引用" % symbol)
        if metric_ids != sorted(set(all_observation_metrics)):
            raise FundamentalToolError("%s.metric_ids 必須等於 observations 的去重引用" % symbol)
        return expected

    def _validate_forbidden_fields(self, value: object, path: str = "$") -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if isinstance(key, str) and key in FORBIDDEN_RESULT_FIELDS:
                    raise FundamentalToolError("結果不得含交易欄位：%s.%s" % (path, key))
                self._validate_forbidden_fields(child, "%s.%s" % (path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self._validate_forbidden_fields(child, "%s[%d]" % (path, index))
