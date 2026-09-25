"""Deterministic D-Plan v4.0 export and local validation.

This module implements the structural and locally reproducible checks supported
by the supplied v4.0 schema and authoring guide. It is not the organizer's
server-side verify_dplan.py semantic validator.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_FLOOR
from typing import Dict, List, Mapping, Sequence

from etf_agent.decision import DecisionInputValidator, DecisionResultValidator
from etf_agent.decision.contracts import DecisionToolError, decimal_value


class DPlanError(ValueError):
    """A D-Plan cannot be safely built or validated."""


class DPlanValidator:
    """Fail-closed subset of schema v4.0 and guide-level reference checks."""

    TOP = {
        "_NOTE", "schema_version", "doc_type", "team_id", "trade_date", "sources",
        "observations", "market_view", "inferences", "decisions",
        "no_trade_decisions", "orders", "agent_metadata",
    }

    def validate(self, plan: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        unknown = sorted(set(plan) - self.TOP)
        if unknown:
            errors.append("D-Plan 含 schema v4.0 未定義欄位：" + ", ".join(unknown))
        for field in sorted(self.TOP - {"_NOTE"}):
            if field not in plan:
                errors.append("D-Plan 缺少欄位：%s" % field)
        if plan.get("schema_version") != "4.0" or plan.get("doc_type") != "D-Plan":
            errors.append("schema_version/doc_type 必須為 4.0/D-Plan")
        team = plan.get("team_id")
        if not isinstance(team, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", team):
            errors.append("team_id 必須是主辦方配發的 1–32 位英數、底線或連字號")
        trade_date = plan.get("trade_date")
        try:
            if not isinstance(trade_date, str) or date.fromisoformat(trade_date).isoformat() != trade_date:
                raise ValueError
        except ValueError:
            errors.append("trade_date 必須是有效 YYYY-MM-DD 日期")

        sources = self._array(plan, "sources", 1, 50, errors)
        observations = self._array(plan, "observations", 1, 100, errors)
        inferences = self._array(plan, "inferences", 1, 60, errors)
        decisions = self._array(plan, "decisions", 0, 30, errors)
        no_trade = self._array(plan, "no_trade_decisions", 0, 30, errors)
        orders = self._array(plan, "orders", 0, 30, errors)
        ids: Dict[str, set] = {"S": set(), "O": set(), "I": set(), "D": set()}
        self._objects(sources, "sources", {"source_id", "authority", "url", "content_as_of", "name", "archive_url", "published_at", "fetched_at"},
                      {"source_id", "authority", "url", "content_as_of"}, errors)
        self._objects(observations, "observations", {"obs_id", "source_ref", "topic", "statement", "values"},
                      {"obs_id", "source_ref", "statement", "values"}, errors)
        market = plan.get("market_view")
        self._object(market, "market_view", {"basis_refs", "logic", "regime", "stance", "posture", "counter_evidence", "confidence"},
                     {"basis_refs", "logic", "regime", "stance", "posture", "counter_evidence"}, errors)
        posture = market.get("posture") if isinstance(market, Mapping) else None
        self._object(posture, "market_view.posture", {"net_exposure_intent", "target_cash_pct_range"},
                     {"net_exposure_intent", "target_cash_pct_range"}, errors)
        self._objects(inferences, "inferences", {"inf_id", "premise_refs", "logic", "counter_evidence", "conclusion", "confidence"},
                      {"inf_id", "premise_refs", "logic", "counter_evidence"}, errors)
        self._objects(decisions, "decisions", {"decision_id", "ticker", "action", "target_weight", "inference_refs", "risk_check", "funding_for"},
                      {"decision_id", "ticker", "action", "target_weight", "inference_refs"}, errors)
        self._objects(no_trade, "no_trade_decisions", {"ticker", "reason_refs", "reason"},
                      {"ticker", "reason_refs"}, errors)
        self._objects(orders, "orders", {"ticker", "side", "shares", "decision_ref", "derivation_detail"},
                      {"ticker", "side", "shares", "decision_ref"}, errors)

        source_ids = self._register(sources, "source_id", "S", ids, errors)
        obs_ids = self._register(observations, "obs_id", "O", ids, errors)
        inf_ids = self._register(inferences, "inf_id", "I", ids, errors)
        decision_ids = self._register(decisions, "decision_id", "D", ids, errors)
        self._check_refs(observations, "source_ref", source_ids, "observation", errors)
        self._check_refs(inferences, "premise_refs", obs_ids, "inference", errors)
        self._check_refs(decisions, "inference_refs", inf_ids, "decision", errors)
        self._check_refs(no_trade, "reason_refs", inf_ids, "no_trade_decision", errors)
        self._check_refs(orders, "decision_ref", decision_ids, "order", errors)
        if isinstance(market, Mapping):
            self._check_refs([market], "basis_refs", obs_ids, "market_view", errors)

        self._validate_metadata(plan.get("agent_metadata"), errors)
        self._validate_tickers(decisions, no_trade, orders, errors)
        self._validate_market(market, errors)
        self._validate_layers(plan, ids, errors)
        return errors

    @staticmethod
    def _array(plan: Mapping[str, object], field: str, low: int, high: int, errors: List[str]) -> List[Mapping[str, object]]:
        value = plan.get(field)
        if not isinstance(value, list) or not low <= len(value) <= high:
            errors.append("%s 必須是 %s–%s 筆陣列" % (field, low, high))
            return []
        if any(not isinstance(row, Mapping) for row in value):
            errors.append("%s 每個元素都必須是物件" % field)
        return [row for row in value if isinstance(row, Mapping)]

    @staticmethod
    def _object(value: object, field: str, allowed: set, required: set, errors: List[str]) -> None:
        if not isinstance(value, Mapping):
            errors.append("%s 必須是物件" % field)
            return
        missing, unknown = sorted(required - set(value)), sorted(set(value) - allowed)
        if missing:
            errors.append("%s 缺少欄位：%s" % (field, ", ".join(missing)))
        if unknown:
            errors.append("%s 含未允許欄位：%s" % (field, ", ".join(unknown)))

    def _objects(self, rows: Sequence[Mapping[str, object]], field: str, allowed: set, required: set, errors: List[str]) -> None:
        for i, row in enumerate(rows):
            self._object(row, "%s[%d]" % (field, i), allowed, required, errors)

    @staticmethod
    def _register(rows: Sequence[Mapping[str, object]], field: str, prefix: str, ids: Dict[str, set], errors: List[str]) -> set:
        found = set()
        for row in rows:
            value = row.get(field)
            if not isinstance(value, str) or not re.fullmatch(prefix + r"[0-9]{1,3}", value):
                errors.append("%s 格式錯誤" % field)
                continue
            if value in found:
                errors.append("%s 重複：%s" % (field, value))
            found.add(value)
        ids[prefix] = found
        expected = {prefix + str(i) for i in range(1, len(found) + 1)}
        if found and found != expected:
            errors.append("%s 必須從 1 開始連續編號" % field)
        return found

    @staticmethod
    def _check_refs(rows: Sequence[Mapping[str, object]], field: str, targets: set, prefix: str, errors: List[str]) -> None:
        for row in rows:
            refs = row.get(field)
            refs = [refs] if field == "decision_ref" and isinstance(refs, str) else refs
            if not isinstance(refs, list):
                errors.append("%s.%s 必須是引用陣列" % (prefix, field))
                continue
            for ref in refs:
                if ref not in targets:
                    errors.append("%s 引用不存在：%s" % (prefix, ref))

    @staticmethod
    def _validate_metadata(value: object, errors: List[str]) -> None:
        allowed_provider = {"anthropic", "openai", "google", "xai", "deepseek", "alibaba", "meta", "mistral", "cohere", "amazon", "microsoft", "nvidia", "taide", "other"}
        fields = {"model_provider", "model_version", "run_started_at", "run_completed_at", "code_version", "input_tokens", "output_tokens", "tool_use_count"}
        DPlanValidator._object(value, "agent_metadata", fields, {"model_provider", "model_version", "run_started_at", "run_completed_at", "code_version"}, errors)
        if isinstance(value, Mapping):
            if value.get("model_provider") not in allowed_provider:
                errors.append("agent_metadata.model_provider 不在 schema v4.0 列舉值")
            for field in ("run_started_at", "run_completed_at"):
                if not isinstance(value.get(field), str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?\+08:00", value[field]):
                    errors.append("agent_metadata.%s 必須含台北時區 +08:00" % field)

    @staticmethod
    def _validate_tickers(decisions: Sequence[Mapping[str, object]], no_trade: Sequence[Mapping[str, object]], orders: Sequence[Mapping[str, object]], errors: List[str]) -> None:
        seen = set()
        for row in list(decisions) + list(no_trade):
            ticker = row.get("ticker")
            if not isinstance(ticker, str) or not re.fullmatch(r"\d{4}", ticker):
                errors.append("ticker 必須是四位數字")
            if ticker in seen:
                errors.append("同一 ticker 不得重複出現在 decisions/no_trade_decisions：%s" % ticker)
            seen.add(ticker)
        decisions_by_id = {row.get("decision_id"): row for row in decisions}
        for row in orders:
            if row.get("side") not in {"BUY", "SELL"}:
                errors.append("order.side 必須是 BUY 或 SELL")
            shares = row.get("shares")
            if not isinstance(shares, int) or isinstance(shares, bool) or shares < 1000 or shares % 1000:
                errors.append("order.shares 必須是正的 1,000 股整數倍")
            ref = decisions_by_id.get(row.get("decision_ref"))
            if not isinstance(ref, Mapping) or ref.get("ticker") != row.get("ticker"):
                errors.append("order 必須引用相同 ticker 的 decision")

    @staticmethod
    def _validate_market(value: object, errors: List[str]) -> None:
        if not isinstance(value, Mapping):
            return
        if value.get("regime") not in {"risk_on", "neutral", "risk_off"}:
            errors.append("market_view.regime 不合法")
        if value.get("stance") not in {"aggressive", "neutral", "defensive"}:
            errors.append("market_view.stance 不合法")
        posture = value.get("posture")
        if isinstance(posture, Mapping):
            if posture.get("net_exposure_intent") not in {"increase", "hold", "reduce"}:
                errors.append("market_view.posture.net_exposure_intent 不合法")
            cash = posture.get("target_cash_pct_range")
            if not isinstance(cash, list) or len(cash) != 2:
                errors.append("target_cash_pct_range 必須是兩個數值")
            else:
                try:
                    low, high = map(lambda x: Decimal(str(x)), cash)
                    if low < 0 or high > Decimal("0.25") or low > high:
                        errors.append("target_cash_pct_range 必須滿足 0 ≤ 下限 ≤ 上限 ≤ 0.25")
                except Exception:
                    errors.append("target_cash_pct_range 必須是數值")

    @staticmethod
    def _validate_layers(plan: Mapping[str, object], ids: Dict[str, set], errors: List[str]) -> None:
        # Schema-required basic type/length/range/URI/pattern checks.
        rules = {
            "sources": ("source_id", "authority", "url", "content_as_of"),
            "observations": ("obs_id", "source_ref", "statement", "values"),
            "inferences": ("inf_id", "premise_refs", "logic", "counter_evidence"),
            "decisions": ("decision_id", "ticker", "action", "target_weight", "inference_refs"),
            "no_trade_decisions": ("ticker", "reason_refs"),
            "orders": ("ticker", "side", "shares", "decision_ref"),
        }
        authorities = {"twse", "tpex", "taifex", "mops", "fininst", "media", "vendor", "other"}
        for row in plan.get("sources", []):
            if isinstance(row, Mapping):
                if row.get("authority") not in authorities:
                    errors.append("source.authority 不合法")
                for key in ("url", "archive_url"):
                    if key in row and (not isinstance(row[key], str) or not re.match(r"^https?://", row[key])):
                        errors.append("source.%s 必須是 http(s) URI" % key)
                for key in ("content_as_of", "published_at", "fetched_at"):
                    if key in row and (not isinstance(row[key], str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?\+08:00", row[key])):
                        errors.append("source.%s 必須含台北時區 +08:00" % key)
                if "name" in row and (not isinstance(row["name"], str) or len(row["name"]) > 300):
                    errors.append("source.name 最多 300 字")
                for key in ("url", "archive_url"):
                    if key in row and isinstance(row[key], str) and len(row[key]) > 2048:
                        errors.append("source.%s URI 過長" % key)
        for row in plan.get("observations", []):
            if isinstance(row, Mapping):
                if not isinstance(row.get("statement"), str) or not 5 <= len(row["statement"]) <= 500:
                    errors.append("observation.statement 長度須為 5–500")
                refs = row.get("source_ref")
                if not isinstance(refs, list) or not 1 <= len(refs) <= 5:
                    errors.append("observation.source_ref 須為 1–5 個來源引用")
                if "topic" in row and (not isinstance(row["topic"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", row["topic"])):
                    errors.append("observation.topic 格式不合法")
                values = row.get("values")
                if not isinstance(values, Mapping) or not values or any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in values.values()):
                    errors.append("observation.values 必須是非空數值物件")
        market = plan.get("market_view")
        if isinstance(market, Mapping):
            if not isinstance(market.get("logic"), str) or not 20 <= len(market["logic"]) <= 1000:
                errors.append("market_view.logic 長度須為 20–1000")
            refs = market.get("basis_refs")
            if not isinstance(refs, list) or not 1 <= len(refs) <= 10:
                errors.append("market_view.basis_refs 須為 1–10 個事實引用")
            if not isinstance(market.get("counter_evidence"), (str, type(None))) or isinstance(market.get("counter_evidence"), str) and len(market["counter_evidence"]) > 500:
                errors.append("market_view.counter_evidence 最多 500 字或 null")
        posture = market.get("posture") if isinstance(market, Mapping) else None
        if isinstance(posture, Mapping):
            cash = posture.get("target_cash_pct_range")
            if isinstance(cash, list) and len(cash) == 2 and any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in cash):
                errors.append("target_cash_pct_range 元素必須是數值")
        for row in plan.get("inferences", []):
            if isinstance(row, Mapping):
                if not isinstance(row.get("logic"), str) or not 20 <= len(row["logic"]) <= 1000:
                    errors.append("inference.logic 長度須為 20–1000")
                if not isinstance(row.get("counter_evidence"), (str, type(None))):
                    errors.append("inference.counter_evidence 必須是字串或 null")
                if isinstance(row.get("counter_evidence"), str) and len(row["counter_evidence"]) > 500:
                    errors.append("inference.counter_evidence 最多 500 字")
                if "conclusion" in row and (not isinstance(row["conclusion"], str) or len(row["conclusion"]) > 300):
                    errors.append("inference.conclusion 最多 300 字")
                refs = row.get("premise_refs")
                if not isinstance(refs, list) or not 1 <= len(refs) <= 10:
                    errors.append("inference.premise_refs 須為 1–10 個事實引用")
        for row in plan.get("decisions", []):
            if isinstance(row, Mapping):
                if row.get("action") not in {"BUY", "ADD", "TRIM", "SELL_ALL"}:
                    errors.append("decision.action 不合法")
                refs = row.get("inference_refs")
                if not isinstance(refs, list) or not 1 <= len(refs) <= 5:
                    errors.append("decision.inference_refs 須為 1–5 個推論引用")
                try:
                    weight = Decimal(str(row.get("target_weight")))
                    if weight < 0 or weight > Decimal("0.25"):
                        errors.append("decision.target_weight 超過 schema 上限")
                except Exception:
                    errors.append("decision.target_weight 必須是數值")
        for row in plan.get("no_trade_decisions", []):
            if isinstance(row, Mapping) and (not isinstance(row.get("reason_refs"), list) or not 1 <= len(row["reason_refs"]) <= 5):
                errors.append("no_trade_decision.reason_refs 須為 1–5 個推論引用")
        for row in plan.get("orders", []):
            if isinstance(row, Mapping) and "derivation_detail" in row and (not isinstance(row["derivation_detail"], str) or len(row["derivation_detail"]) > 300):
                errors.append("order.derivation_detail 最多 300 字")
        for row in plan.get("market_view", {}).get("basis_refs", []) if isinstance(plan.get("market_view"), Mapping) else []:
            if not isinstance(row, str):
                errors.append("market_view.basis_refs 必須是字串陣列")


class DPlanExporter:
    """Map a validated decision run plus agent-authored reasoning into D-Plan."""

    def build(
        self,
        team_id: str,
        trade_date: str,
        context: Mapping[str, object],
        artifacts: Mapping[str, Mapping[str, object]],
    ) -> Dict[str, object]:
        required = {"decision_input", "momentum", "debate", "intent", "policy", "proposal", "scenario", "guard", "risk_review", "revision_history", "decision"}
        missing = sorted(required - set(artifacts))
        if missing:
            raise DPlanError("Decision run 缺少 artifacts：" + ", ".join(missing))
        bundle = artifacts["decision_input"]
        input_errors = DecisionInputValidator(bundle).validate()
        if input_errors:
            raise DPlanError("DecisionInputBundle 驗證失敗：" + "; ".join(input_errors))
        result = artifacts["decision"]
        result_errors = DecisionResultValidator(bundle, artifacts["policy"], artifacts["momentum"], artifacts["debate"], artifacts["intent"]).validate(
            artifacts["proposal"], artifacts["scenario"], artifacts["guard"], artifacts["risk_review"], artifacts["revision_history"], result
        )
        if result_errors:
            raise DPlanError("DecisionResult 驗證失敗：" + "; ".join(result_errors))
        if result.get("status") not in {"approved", "no_trade"}:
            raise DPlanError("只有 approved/no_trade DecisionResult 可匯出")
        if trade_date <= str(bundle["decision_cutoff"])[:10]:
            raise DPlanError("trade_date 必須晚於決策資料 cutoff 日期")
        required_context = {
            "sources", "observations", "market_view", "inferences", "inference_refs_by_ticker",
            "agent_metadata", "eligible_tickers", "eligible_universe_source_refs", "strategy_statement",
        }
        if required_context - set(context):
            raise DPlanError("D-Plan context 缺少：" + ", ".join(sorted(required_context - set(context))))
        cutoff = datetime.fromisoformat(str(bundle["decision_cutoff"]).replace("Z", "+00:00"))
        for source in context["sources"]:
            if not isinstance(source, Mapping):
                raise DPlanError("sources 每筆必須是物件")
            for time_field in ("content_as_of", "published_at", "fetched_at"):
                value = source.get(time_field)
                if value is None:
                    continue
                try:
                    source_time = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                except ValueError as error:
                    raise DPlanError("source.%s 無法解析" % time_field) from error
                if source_time.tzinfo is None or source_time > cutoff:
                    raise DPlanError("source.%s 缺時區或晚於 decision_cutoff" % time_field)
        positions = artifacts["proposal"].get("allocation_proposal", {}).get("positions", [])
        account = bundle["account_snapshot"]
        held = {str(row["symbol"]): int(row["shares"]) for row in account.get("positions", [])}
        latest_prices = {str(row["symbol"]): Decimal(str(row["analysis_close_price"])) for row in bundle["snapshot"].get("latest_prices", [])}
        target = {str(row["symbol"]): int(row["shares"]) for row in positions}
        if any(not re.fullmatch(r"\d{4}\.TW", symbol) for symbol in set(held) | set(target)):
            raise DPlanError("D-Plan 只支援具四位數台股代碼且可映射為 ####.TW 的標的")
        nav = Decimal(str(account["nav"]))
        refs_map = context.get("inference_refs_by_ticker")
        if not isinstance(refs_map, Mapping):
            raise DPlanError("inference_refs_by_ticker 必須是股票代碼對應推論 ID 的物件")
        eligible = context.get("eligible_tickers")
        if not isinstance(eligible, list) or any(not isinstance(item, str) or not re.fullmatch(r"\d{4}", item) for item in eligible) or len(eligible) != 150 or len(set(eligible)) != 150:
            raise DPlanError("eligible_tickers 必須是已核實的 150 檔四位數官方交易池；ETF 清單不可代替")
        universe_refs = context.get("eligible_universe_source_refs")
        if not isinstance(universe_refs, list) or not universe_refs:
            raise DPlanError("缺少 150 檔官方交易池的來源引用")
        source_ids = {str(item.get("source_id")) for item in context["sources"] if isinstance(item, Mapping)}
        if any(ref not in source_ids for ref in universe_refs):
            raise DPlanError("官方交易池引用不存在於 sources")
        self._validate_strategy_statement(context.get("strategy_statement"))
        metadata = context.get("agent_metadata")
        try:
            started = datetime.fromisoformat(str(metadata["run_started_at"]))
            completed = datetime.fromisoformat(str(metadata["run_completed_at"]))
            if started.tzinfo is None or completed.tzinfo is None or completed < started:
                raise ValueError
        except (TypeError, KeyError, ValueError) as error:
            raise DPlanError("agent_metadata 執行時間缺漏或順序錯誤") from error
        if any(symbol[:4] not in set(eligible) for symbol in set(held) | set(target)):
            raise DPlanError("持倉或目標配置含官方 150 檔交易池以外標的")
        if not 20 <= len(target) <= 30:
            raise DPlanError("官方候選需有 20–30 檔目標持股；目前為 %d 檔" % len(target))
        decisions, no_trade, order_rows = [], [], []
        decision_id_by_symbol = {}
        for symbol in sorted(set(held) | set(target)):
            ticker = symbol[:4]
            current, shares = held.get(symbol, 0), target.get(symbol, 0)
            price = latest_prices.get(symbol)
            if price is None or price <= 0:
                raise DPlanError("缺少 %s 前一交易日收盤價" % ticker)
            refs = refs_map.get(ticker)
            if not isinstance(refs, list) or not refs:
                raise DPlanError("%s 缺少人工審閱的 inference_refs_by_ticker" % ticker)
            if current == shares:
                if current:
                    no_trade.append({"ticker": ticker, "reason_refs": list(refs)})
                continue
            if shares == 0:
                action, weight = "SELL_ALL", 0.0
            else:
                action = "BUY" if current == 0 else "ADD" if shares > current else "TRIM"
                exact_weight = Decimal(shares) * price / nav
                weight = float(exact_weight)
                # JSON Schema numbers are parsed as binary floats by common
                # toolchains. Move by at most a few ULPs so exact lot boundaries
                # do not floor one lot too low after serialization.
                for _ in range(8):
                    derived = int((Decimal(str(weight)) * nav / price / Decimal(1000)).to_integral_value(rounding=ROUND_FLOOR)) * 1000
                    if derived >= shares:
                        break
                    bumped = math.nextafter(weight, math.inf)
                    if bumped > 0.25:
                        break
                    weight = bumped
            decision_id = "D%d" % (len(decisions) + 1)
            decision_id_by_symbol[symbol] = decision_id
            decisions.append({"decision_id": decision_id, "ticker": ticker, "action": action, "target_weight": weight, "inference_refs": list(refs)})
        by_decision = {row["ticker"]: row["decision_id"] for row in decisions}
        for symbol in sorted(set(held) | set(target)):
            current, shares = held.get(symbol, 0), target.get(symbol, 0)
            if current == shares:
                continue
            ticker, price = symbol[:4], latest_prices.get(symbol)
            decision_ref = by_decision.get(ticker)
            if price is None or decision_ref is None:
                raise DPlanError("缺少委託價格或決策引用：%s" % ticker)
            target_shares = int((Decimal(str(next(row["target_weight"] for row in decisions if row["ticker"] == ticker))) * nav / price / Decimal(1000)).to_integral_value(rounding=ROUND_FLOOR)) * 1000 if shares else 0
            delta = target_shares - current
            if delta:
                order_rows.append({"ticker": ticker, "side": "BUY" if delta > 0 else "SELL", "shares": abs(delta), "decision_ref": decision_ref})
        # Do not allow the official derivation to silently diverge from the approved engine.
        proposed = sorted((str(o["symbol"])[:4], str(o["side"]).upper(), int(o["shares"])) for o in result.get("orders", []))
        derived = sorted((o["ticker"], o["side"], o["shares"]) for o in order_rows)
        if proposed != derived:
            raise DPlanError("主辦方 target_weight 股數公式與已核准 Decision orders 不一致；拒絕輸出 D-Plan")
        plan = {
            "schema_version": "4.0", "doc_type": "D-Plan", "team_id": team_id, "trade_date": trade_date,
            "sources": context["sources"], "observations": context["observations"], "market_view": context["market_view"],
            "inferences": context["inferences"], "decisions": decisions, "no_trade_decisions": no_trade,
            "orders": order_rows, "agent_metadata": context["agent_metadata"],
        }
        errors = DPlanValidator().validate(plan)
        if errors:
            raise DPlanError("D-Plan 本地驗證失敗：" + "; ".join(errors))
        self._validate_official_posture(plan, account, bundle, order_rows, latest_prices, target)
        return plan

    @staticmethod
    def _validate_strategy_statement(value: object) -> None:
        fields = {"name", "theme", "philosophy", "approved_at", "sha256"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise DPlanError("strategy_statement 必須含 name/theme/philosophy/approved_at/sha256")
        name, theme, philosophy = value.get("name"), value.get("theme"), value.get("philosophy")
        if not isinstance(name, str) or not name.startswith("主動") or len(name) > 20:
            raise DPlanError("策略說明 ETF 名稱須以「主動」開頭且不超過 20 字")
        if not isinstance(theme, str) or not theme.strip() or len(theme) > 50:
            raise DPlanError("策略主題須為 1–50 字")
        if not isinstance(philosophy, str) or not 100 <= len(philosophy) <= 300:
            raise DPlanError("投資理念須為 100–300 字")
        if not isinstance(value.get("approved_at"), str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+08:00", value["approved_at"]):
            raise DPlanError("策略說明 approved_at 必須含台北時區 +08:00")
        if not isinstance(value.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
            raise DPlanError("策略說明須以 SHA-256 識別已確認版本")

    @staticmethod
    def _validate_official_posture(plan: Mapping[str, object], account: Mapping[str, object], bundle: Mapping[str, object], orders: Sequence[Mapping[str, object]], prices: Mapping[str, Decimal], target: Mapping[str, int]) -> None:
        posture = plan["market_view"]["posture"]
        buy, sell = Decimal("0"), Decimal("0")
        for row in orders:
            amount = Decimal(int(row["shares"])) * prices[row["ticker"] + ".TW"]
            if row["side"] == "BUY":
                buy += amount
            else:
                sell += amount
        nav = Decimal(str(account["nav"]))
        for symbol, shares in target.items():
            weight = Decimal(shares) * prices[symbol] / nav
            limit = Decimal("0.25") if symbol == "2330.TW" else Decimal("0.10")
            if weight > limit:
                raise DPlanError("官方單一持股上限檢查失敗：%s 超過 %.0f%%" % (symbol[:4], limit * 100))
        intent = posture["net_exposure_intent"]
        if (intent == "increase" and buy <= sell) or (intent == "reduce" and sell <= buy) or (intent == "hold" and abs(buy - sell) > nav * Decimal("0.02")):
            raise DPlanError("C12 本地檢查失敗：net_exposure_intent 與委託淨流向不一致")
        cash = Decimal(str(account["cash"]))
        rules = bundle.get("rules", {})
        commission = Decimal(str(rules.get("commission_rate", "0.001425")))
        sell_tax = Decimal(str(rules.get("sell_tax_rate", "0.003")))
        minimum_commission = Decimal(str(rules.get("minimum_commission", "0")))
        estimated_cash = cash
        for row in orders:
            gross = Decimal(int(row["shares"])) * prices[row["ticker"] + ".TW"]
            fee = max(gross * commission, minimum_commission)
            tax = gross * sell_tax if row["side"] == "SELL" else Decimal("0")
            estimated_cash += -gross - fee if row["side"] == "BUY" else gross - fee - tax
        low, high = map(lambda item: Decimal(str(item)), posture["target_cash_pct_range"])
        cash_ratio = estimated_cash / nav
        if cash_ratio < low or cash_ratio > high:
            raise DPlanError("C12 本地檢查失敗：以前一日收盤價及費稅估算的現金比不在宣告區間")


def load_decision_run(repository: str, run_id: str) -> Dict[str, Dict[str, object]]:
    """Load only artifacts covered by a verified DecisionRepository manifest."""
    from pathlib import Path
    from etf_agent.decision import DecisionRepository

    run_dir = DecisionRepository(Path(repository)).verify(run_id)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    result = {}
    for name in manifest["artifacts"]:
        path = run_dir / (name + ".json")
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DPlanError("Decision artifact 無法讀取：%s" % name) from error
        if not isinstance(item, Mapping):
            raise DPlanError("Decision artifact 必須是 JSON 物件：%s" % name)
        result[name] = dict(item)
    return result
