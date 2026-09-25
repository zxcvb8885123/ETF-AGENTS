"""由 FundamentalDataBundle 確定性計算並重算 FundamentalMetrics。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core import canonical_json, content_sha256, decimal_string
from .contracts import (
    FORMULA_VERSION,
    METRICS_SCHEMA_VERSION,
    FundamentalToolError,
    _decimal,
    _parse_time,
    _required_string,
    _same_time,
    _with_content_sha256,
)
from .tools import FundamentalSnapshotTools


class FundamentalMetricsCalculator:
    """從已驗證的 FundamentalDataBundle 重算所有數值。"""

    def __init__(self, tools: FundamentalSnapshotTools, bundle: Mapping[str, object]):
        self.tools = tools
        self.bundle = dict(bundle)
        errors = tools.validate_bundle(self.bundle)
        if errors:
            raise FundamentalToolError("FundamentalDataBundle 無效：%s" % "; ".join(errors))
        self.bundle_id = _required_string(self.bundle, "bundle_id")
        self.bundle_sha256 = _required_string(self.bundle, "content_sha256")
        self.request = dict(self.bundle["request"])
        self.cutoff = _parse_time(self.bundle["decision_cutoff"], "decision_cutoff")

    def compute(
        self,
        *,
        metrics_id: Optional[str] = None,
        computed_at: Optional[str] = None,
    ) -> Dict[str, object]:
        metrics_id = metrics_id or str(uuid.uuid4())
        if not isinstance(metrics_id, str) or not metrics_id.strip():
            raise FundamentalToolError("metrics_id 不得為空")
        computed_at = computed_at or datetime.now(timezone.utc).isoformat()
        if _parse_time(computed_at, "computed_at") < self.cutoff:
            raise FundamentalToolError("computed_at 不得早於 decision_cutoff")
        items = [self._compute_company(dict(company)) for company in self.bundle["companies"]]
        statuses = {str(item["status"]) for item in items}
        status = (
            "completed"
            if statuses == {"completed"}
            else "unavailable"
            if statuses == {"unavailable"}
            else "degraded"
        )
        payload: Dict[str, object] = {
            "schema_version": METRICS_SCHEMA_VERSION,
            "metrics_id": metrics_id.strip(),
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.bundle_sha256,
            "snapshot_id": self.bundle["snapshot_id"],
            "decision_cutoff": self.bundle["decision_cutoff"],
            "computed_at": computed_at,
            "formula_version": FORMULA_VERSION,
            "status": status,
            "items": items,
        }
        return _with_content_sha256(payload)

    def validate(self, payload: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        try:
            if payload.get("schema_version") != METRICS_SCHEMA_VERSION:
                raise FundamentalToolError("FundamentalMetrics schema_version 必須為 1.0")
            if payload.get("bundle_id") != self.bundle_id:
                raise FundamentalToolError("FundamentalMetrics.bundle_id 不一致")
            if payload.get("bundle_sha256") != self.bundle_sha256:
                raise FundamentalToolError("FundamentalMetrics.bundle_sha256 不一致")
            if payload.get("snapshot_id") != self.bundle.get("snapshot_id"):
                raise FundamentalToolError("FundamentalMetrics.snapshot_id 不一致")
            if not _same_time(
                payload.get("decision_cutoff"), self.bundle.get("decision_cutoff"), "decision_cutoff"
            ):
                raise FundamentalToolError("FundamentalMetrics.decision_cutoff 不一致")
            if payload.get("formula_version") != FORMULA_VERSION:
                raise FundamentalToolError("FundamentalMetrics.formula_version 不支援")
            expected = self.compute(
                metrics_id=_required_string(payload, "metrics_id"),
                computed_at=_required_string(payload, "computed_at"),
            )
            if canonical_json(dict(payload)) != canonical_json(expected):
                errors.append("FundamentalMetrics 未通過確定性重算")
            if payload.get("content_sha256") != content_sha256(payload):
                errors.append("FundamentalMetrics.content_sha256 不一致")
        except FundamentalToolError as error:
            errors.append(str(error))
        return errors

    def _compute_company(self, company: Mapping[str, object]) -> Dict[str, object]:
        symbol = _required_string(company, "symbol")
        statements = company.get("statements")
        if not isinstance(statements, list):
            raise FundamentalToolError("Bundle 公司 statements 必須是陣列")
        statement_map: Dict[Tuple[str, str], Dict[str, object]] = {}
        for statement in statements:
            if not isinstance(statement, Mapping):
                raise FundamentalToolError("Bundle statements 必須是物件")
            key = (str(statement.get("scope")), str(statement.get("statement_type")))
            if key in statement_map:
                raise FundamentalToolError("Bundle 同公司同範圍財報重複")
            statement_map[key] = dict(statement)
        current_income = statement_map.get(("current", "income_statement"))
        current_balance = statement_map.get(("current", "balance_sheet"))
        prior_income = statement_map.get(("prior_year", "income_statement"))
        metric_items = [
            self._ratio_metric(
                symbol,
                "operating_margin_pct",
                current_income,
                "operating_profit",
                "revenue",
                "percent",
                company,
            ),
            self._ratio_metric(
                symbol,
                "liabilities_to_assets_pct",
                current_balance,
                "total_liabilities",
                "total_assets",
                "percent",
                company,
            ),
            self._yoy_metric(symbol, "revenue_yoy_pct", current_income, prior_income, "revenue", company),
            self._yoy_metric(symbol, "net_income_yoy_pct", current_income, prior_income, "net_income", company),
            self._margin_pp_yoy(symbol, current_income, prior_income, company),
        ]
        required = set(self.request["metric_keys"])
        metric_items = [item for item in metric_items if item["metric_key"] in required]
        available_count = sum(1 for item in metric_items if item["status"] == "available")
        unavailable_count = len(metric_items) - available_count
        if available_count == 0:
            status = "unavailable"
        elif company["status"] == "completed" and unavailable_count == 0:
            status = "completed"
        else:
            status = "degraded"
        return {
            "symbol": symbol,
            "status": status,
            "period": company["period"],
            "comparison_period": company["comparison_period"],
            "metrics": metric_items,
            "missing_data": list(company["missing_data"]),
        }

    def _ratio_metric(
        self,
        symbol: str,
        metric_key: str,
        statement: Optional[Mapping[str, object]],
        numerator_key: str,
        denominator_key: str,
        unit: str,
        company: Mapping[str, object],
    ) -> Dict[str, object]:
        numerator = self._fact(statement, numerator_key)
        denominator = self._fact(statement, denominator_key)
        if numerator is None or denominator is None:
            return self._unavailable_metric(
                symbol, metric_key, unit, company, "REQUIRED_FACT_NOT_REPORTED"
            )
        if numerator["currency"] != denominator["currency"]:
            return self._unavailable_metric(symbol, metric_key, unit, company, "CURRENCY_MISMATCH")
        denominator_value = denominator["absolute_value"]
        if denominator_value <= 0:
            return self._unavailable_metric(symbol, metric_key, unit, company, "NONPOSITIVE_DENOMINATOR")
        result = numerator["absolute_value"] / denominator_value * Decimal("100")
        return self._available_metric(
            symbol,
            metric_key,
            unit,
            company,
            result,
            [numerator, denominator],
            method="ratio_v1",
        )

    def _yoy_metric(
        self,
        symbol: str,
        metric_key: str,
        current: Optional[Mapping[str, object]],
        prior: Optional[Mapping[str, object]],
        fact_key: str,
        company: Mapping[str, object],
    ) -> Dict[str, object]:
        current_fact = self._fact(current, fact_key)
        prior_fact = self._fact(prior, fact_key)
        if current_fact is None or prior_fact is None:
            return self._unavailable_metric(
                symbol, metric_key, "percent", company, "COMPARABLE_FACT_NOT_REPORTED"
            )
        if current_fact["currency"] != prior_fact["currency"]:
            return self._unavailable_metric(symbol, metric_key, "percent", company, "CURRENCY_MISMATCH")
        if prior_fact["absolute_value"] <= 0:
            return self._unavailable_metric(
                symbol, metric_key, "percent", company, "NONPOSITIVE_COMPARISON_BASE"
            )
        result = (
            (current_fact["absolute_value"] - prior_fact["absolute_value"])
            / prior_fact["absolute_value"]
            * Decimal("100")
        )
        return self._available_metric(
            symbol,
            metric_key,
            "percent",
            company,
            result,
            [current_fact, prior_fact],
            method="same_quarter_yoy_v1",
        )

    def _margin_pp_yoy(
        self,
        symbol: str,
        current: Optional[Mapping[str, object]],
        prior: Optional[Mapping[str, object]],
        company: Mapping[str, object],
    ) -> Dict[str, object]:
        current_profit = self._fact(current, "operating_profit")
        current_revenue = self._fact(current, "revenue")
        prior_profit = self._fact(prior, "operating_profit")
        prior_revenue = self._fact(prior, "revenue")
        facts = [current_profit, current_revenue, prior_profit, prior_revenue]
        if any(item is None for item in facts):
            return self._unavailable_metric(
                symbol,
                "operating_margin_pp_yoy",
                "percentage_points",
                company,
                "COMPARABLE_FACT_NOT_REPORTED",
            )
        typed_facts = [item for item in facts if item is not None]
        currencies = {str(item["currency"]) for item in typed_facts}
        if len(currencies) != 1:
            return self._unavailable_metric(
                symbol, "operating_margin_pp_yoy", "percentage_points", company, "CURRENCY_MISMATCH"
            )
        if current_revenue["absolute_value"] <= 0 or prior_revenue["absolute_value"] <= 0:
            return self._unavailable_metric(
                symbol,
                "operating_margin_pp_yoy",
                "percentage_points",
                company,
                "NONPOSITIVE_DENOMINATOR",
            )
        current_margin = current_profit["absolute_value"] / current_revenue["absolute_value"] * Decimal("100")
        prior_margin = prior_profit["absolute_value"] / prior_revenue["absolute_value"] * Decimal("100")
        return self._available_metric(
            symbol,
            "operating_margin_pp_yoy",
            "percentage_points",
            company,
            current_margin - prior_margin,
            typed_facts,
            method="operating_margin_percentage_point_change_v1",
        )

    @staticmethod
    def _fact(
        statement: Optional[Mapping[str, object]], fact_key: str
    ) -> Optional[Dict[str, object]]:
        if statement is None:
            return None
        facts = statement.get("facts")
        if not isinstance(facts, Mapping):
            raise FundamentalToolError("財報 facts 必須是物件")
        fact = facts.get(fact_key)
        if not isinstance(fact, Mapping) or fact.get("value_status") != "provided":
            return None
        value = _decimal(fact.get("value"), "%s.value" % fact_key)
        multiplier = fact.get("unit_multiplier")
        if not isinstance(multiplier, int) or multiplier <= 0:
            raise FundamentalToolError("%s.unit_multiplier 無效" % fact_key)
        document_id = statement.get("document_id")
        if not isinstance(document_id, int) or document_id <= 0:
            raise FundamentalToolError("財報 document_id 必須是正整數")
        return {
            "fact_id": "document:%d:fact:%s" % (document_id, fact_key),
            "document_id": document_id,
            "scope": statement["scope"],
            "statement_type": statement["statement_type"],
            "fact_key": fact_key,
            "currency": _required_string(fact, "currency"),
            "unit_multiplier": multiplier,
            "value": decimal_string(value),
            "absolute_value": value * Decimal(multiplier),
            "source_evidence_id": _required_string(statement, "source_evidence_id"),
        }

    def _available_metric(
        self,
        symbol: str,
        metric_key: str,
        unit: str,
        company: Mapping[str, object],
        value: Decimal,
        facts: Sequence[Mapping[str, object]],
        *,
        method: str,
    ) -> Dict[str, object]:
        dependencies = [
            {
                "fact_id": fact["fact_id"],
                "document_id": fact["document_id"],
                "scope": fact["scope"],
                "statement_type": fact["statement_type"],
                "fact_key": fact["fact_key"],
                "value": fact["value"],
                "currency": fact["currency"],
                "unit_multiplier": fact["unit_multiplier"],
                "source_evidence_id": fact["source_evidence_id"],
            }
            for fact in facts
        ]
        return {
            "metric_id": "%s:%s:%s" % (symbol, metric_key, company["period"]),
            "symbol": symbol,
            "metric_key": metric_key,
            "status": "available",
            "value": decimal_string(value),
            "unit": unit,
            "period": company["period"],
            "comparison_period": company["comparison_period"],
            "methodology": method,
            "dependencies": dependencies,
            "reason_code": None,
        }

    def _unavailable_metric(
        self,
        symbol: str,
        metric_key: str,
        unit: str,
        company: Mapping[str, object],
        reason_code: str,
    ) -> Dict[str, object]:
        return {
            "metric_id": "%s:%s:%s" % (symbol, metric_key, company["period"]),
            "symbol": symbol,
            "metric_key": metric_key,
            "status": "unavailable",
            "value": None,
            "unit": unit,
            "period": company["period"],
            "comparison_period": company["comparison_period"],
            "methodology": None,
            "dependencies": [],
            "reason_code": reason_code,
        }
