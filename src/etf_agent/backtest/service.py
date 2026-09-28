"""Deterministic multi-day fixture replay service."""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Mapping, Sequence

from etf_agent.core import canonical_sha256, content_sha256
from etf_agent.decision.finalization import DecisionResultValidator
from etf_agent.ledger import AccountLedger, ExecutionSimulator

from .contracts import BACKTEST_SCHEMA_VERSION, BacktestRequestValidator, BacktestToolError, HistoricalClock, decimal_value, parse_time
from .engine import FixturePointInTimeDataProvider


class BacktestService:
    def inspect_fixture(self, request: Mapping[str, object], daily_inputs: Mapping[str, Mapping[str, object]]) -> Dict[str, object]:
        errors = BacktestRequestValidator().validate(request)
        if errors:
            raise BacktestToolError("BacktestRequest 驗證失敗：" + "；".join(errors))
        coverage = []
        for session in HistoricalClock(request):
            trade_date = session["trade_date"]
            item = daily_inputs.get(trade_date)
            if not isinstance(item, Mapping):
                coverage.append({"trade_date": trade_date, "available": False, "reason": "MISSING_DAILY_INPUT"})
                continue
            try:
                research = FixturePointInTimeDataProvider(
                    item.get("research_versions", []),
                    request["data_manifest"]["mode"] == "historical_verified",
                ).at(session["decision_cutoff"])
                market = item.get("execution_market", {})
                close_market = item.get("close_market", {})
                available = (
                    isinstance(market, Mapping)
                    and market.get("price_basis") == "unadjusted"
                    and isinstance(market.get("available_at"), str)
                    and isinstance(close_market, Mapping)
                    and close_market.get("price_basis") == "unadjusted"
                    and isinstance(close_market.get("available_at"), str)
                    and isinstance(close_market.get("quotes"), Mapping)
                )
                if available:
                    parse_time(market["available_at"], "ExecutionMarketData.available_at")
                    if parse_time(market["available_at"], "ExecutionMarketData.available_at") > parse_time(session["execution_at"], "execution_at"):
                        available = False
                    parse_time(close_market["available_at"], "CloseMarketData.available_at")
                    if parse_time(close_market["available_at"], "CloseMarketData.available_at") < parse_time(session["close_at"], "close_at"):
                        available = False
                coverage.append({"trade_date": trade_date, "available": available, "research_artifact_id": research["artifact_id"], "reason": None if available else "MISSING_UNADJUSTED_EXECUTION_OR_CLOSE"})
            except BacktestToolError as error:
                coverage.append({"trade_date": trade_date, "available": False, "reason": str(error)})
        return {"valid": all(item["available"] for item in coverage), "coverage": coverage}

    def run_fixture(self, request: Mapping[str, object], daily_inputs: Mapping[str, Mapping[str, object]]) -> Dict[str, object]:
        errors = BacktestRequestValidator().validate(request)
        if errors:
            raise BacktestToolError("BacktestRequest 驗證失敗：" + "；".join(errors))
        ledger = AccountLedger(request["initial_account"])
        simulator = ExecutionSimulator(request["execution_assumptions"])
        days = []
        for session in HistoricalClock(request):
            trade_date = session["trade_date"]
            item = daily_inputs.get(trade_date)
            if not isinstance(item, Mapping):
                raise BacktestToolError("缺少交易日輸入：%s" % trade_date)
            provider = FixturePointInTimeDataProvider(
                item.get("research_versions", []),
                request["data_manifest"]["mode"] == "historical_verified",
            )
            research = provider.at(session["decision_cutoff"])
            decision = item.get("decision")
            if not isinstance(decision, Mapping):
                raise BacktestToolError("%s 缺少 DecisionResult" % trade_date)
            if request["data_manifest"]["mode"] == "historical_verified":
                decision_run = item.get("decision_run")
                required = {"bundle", "policy", "momentum", "debate", "intent", "proposal", "scenario", "guard", "risk_review", "history"}
                if not isinstance(decision_run, Mapping) or required - set(decision_run):
                    raise BacktestToolError("正式歷史模式必須提供完整 DecisionResult 重建輸入")
                errors = DecisionResultValidator(
                    decision_run["bundle"], decision_run["policy"], decision_run["momentum"],
                    decision_run["debate"], decision_run["intent"], decision_run.get("team_inputs"),
                ).validate(
                    decision_run["proposal"], decision_run["scenario"], decision_run["guard"],
                    decision_run["risk_review"], decision_run["history"], decision,
                )
                if errors:
                    raise BacktestToolError("%s DecisionResult 重建失敗：%s" % (trade_date, "；".join(errors)))
            ledger.settle(trade_date)
            ledger.apply_actions(item.get("corporate_actions", []))
            execution = simulator.run(
                decision,
                item.get("execution_market", {}),
                ledger.buying_power(bool(request["execution_assumptions"]["reuse_sell_proceeds"])),
                session["execution_at"],
            )
            ledger.apply_fills(
                execution,
                session["settlement_date"],
                bool(request["execution_assumptions"]["reuse_sell_proceeds"]),
            )
            close_market = item.get("close_market")
            if not isinstance(close_market, Mapping) or close_market.get("price_basis") != "unadjusted" or not isinstance(close_market.get("available_at"), str) or not isinstance(close_market.get("quotes"), Mapping):
                raise BacktestToolError("CloseMarketData 必須有未還原價格、available_at 與 quotes")
            if parse_time(close_market["available_at"], "CloseMarketData.available_at") < parse_time(session["close_at"], "close_at"):
                raise BacktestToolError("CloseMarketData.available_at 早於 close_at")
            snapshot = ledger.snapshot(close_market["quotes"], trade_date)
            days.append({"trade_date": trade_date, "research_artifact_id": research["artifact_id"], "decision_id": decision.get("decision_id"), "decision_status": decision.get("status"), "execution": execution, "ledger": snapshot})
        body = {"schema_version": BACKTEST_SCHEMA_VERSION, "backtest_id": "", "request_id": request["request_id"], "request_hash": request["content_sha256"], "status": "completed", "days": days}
        body["backtest_id"] = "backtest:" + canonical_sha256(body)[:20]
        body["content_sha256"] = content_sha256(body)
        return body

    def validate_fixture(self, request: Mapping[str, object], daily_inputs: Mapping[str, Mapping[str, object]], result: Mapping[str, object]):
        expected = self.run_fixture(request, daily_inputs)
        return [] if dict(result) == expected else ["BacktestRun 與確定性重播結果不一致"]

    def build_report(self, request: Mapping[str, object], result: Mapping[str, object]) -> Dict[str, object]:
        if result.get("content_sha256") != content_sha256(result):
            raise BacktestToolError("BacktestRun.content_sha256 與內容不一致")
        if result.get("request_id") != request.get("request_id") or result.get("request_hash") != request.get("content_sha256"):
            raise BacktestToolError("BacktestRun 與 BacktestRequest 不一致")
        days = result.get("days")
        if not isinstance(days, list) or not days:
            raise BacktestToolError("BacktestRun 缺少每日結果")
        total_commission = sum(decimal_value(fill["commission"], "commission") for day in days for fill in day["execution"]["fills"])
        total_tax = sum(decimal_value(fill["tax"], "tax") for day in days for fill in day["execution"]["fills"])
        statuses = {status: 0 for status in ("approved", "no_trade", "rejected")}
        for day in days:
            statuses[day["decision_status"]] = statuses.get(day["decision_status"], 0) + 1
        body = {"schema_version": BACKTEST_SCHEMA_VERSION, "report_id": "", "backtest_id": result["backtest_id"], "request_id": request["request_id"], "data_mode": request["data_manifest"]["mode"], "days": len(days), "first_end_nav": days[0]["ledger"]["nav"], "last_nav": days[-1]["ledger"]["nav"], "total_commission": str(total_commission.quantize(Decimal("1"))), "total_tax": str(total_tax.quantize(Decimal("1"))), "decision_status_counts": statuses, "limitations": ["本報告只呈現 B0～B2 fixture 帳務驗收，不代表策略有效或可交易。"]}
        body["report_id"] = "backtest-report:" + canonical_sha256(body)[:20]
        body["content_sha256"] = content_sha256(body)
        return body

    @staticmethod
    def render_report_markdown(report: Mapping[str, object]) -> str:
        return "\n".join(["# 回測帳務驗收報告", "", "- Backtest ID：%s" % report["backtest_id"], "- 資料模式：%s" % report["data_mode"], "- 交易日：%s" % report["days"], "- 首日收盤 NAV：%s" % report["first_end_nav"], "- 最終 NAV：%s" % report["last_nav"], "- 手續費：%s" % report["total_commission"], "- 交易稅：%s" % report["total_tax"], "", "限制：%s" % report["limitations"][0], ""])
