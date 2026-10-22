"""Deterministically project a verified decision run into D-Plan context."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Mapping, Sequence

from etf_agent.core import canonical_sha256

from .dplan import DPlanError


_REGIME = {"bull": "risk_on", "bear": "risk_off", "neutral": "neutral"}
_STANCE = {"aggressive", "neutral", "defensive"}


def _number(value: object, label: str) -> float:
    try:
        result = float(Decimal(str(value)))
    except Exception as error:  # noqa: BLE001 - turn data errors into a domain error.
        raise DPlanError("%s 必須是有限數值" % label) from error
    if result != result or result in (float("inf"), float("-inf")):
        raise DPlanError("%s 必須是有限數值" % label)
    return result


def _as_taipei(value: object, label: str) -> str:
    """Render a timestamp with the competition's required +08:00 offset."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise DPlanError("%s 無法解析為含時區時間" % label) from error
    if parsed.tzinfo is None:
        raise DPlanError("%s 缺少時區" % label)
    return parsed.astimezone(timezone(timedelta(hours=8))).isoformat()


class DPlanContextBuilder:
    """Build only from sealed decision artifacts and an approved static profile.

    This builder never asks an LLM to fill a D-Plan field.  It maps stored
    price evidence, deterministic momentum values, the sealed TradeDecision,
    and the one-time user-approved competition profile.
    """

    def build(
        self,
        artifacts: Mapping[str, Mapping[str, object]],
        profile: Mapping[str, object],
        daily_run: Mapping[str, object],
        code_version: str,
    ) -> Dict[str, object]:
        bundle = self._mapping(artifacts, "decision_input")
        proposal = self._mapping(artifacts, "proposal")
        momentum = self._mapping(artifacts, "momentum")
        intent = self._mapping(artifacts, "intent")
        team = self._mapping(artifacts, "team_inputs")
        self._profile(profile)
        agent = self._mapping(profile, "agent")
        started = _as_taipei(daily_run.get("started_at"), "daily_run.started_at")
        completed = _as_taipei(daily_run.get("finished_at"), "daily_run.finished_at")
        if completed < started:
            raise DPlanError("daily_run.finished_at 不得早於 started_at")
        if not isinstance(code_version, str) or not code_version:
            raise DPlanError("code_version 不得為空")

        account = self._mapping(bundle, "account_snapshot")
        snapshot = self._mapping(bundle, "snapshot")
        selected = self._selected_symbols(account, proposal)
        evidence = self._price_evidence(snapshot, selected)
        sources, source_by_evidence = self._sources(evidence)
        momentum_by_symbol = {
            str(row.get("symbol")): row for row in momentum.get("items", [])
            if isinstance(row, Mapping)
        }
        intent_by_symbol = {
            str(row.get("symbol")): row for row in intent.get("items", [])
            if isinstance(row, Mapping)
        }
        observations, observation_by_symbol = self._observations(
            momentum, momentum_by_symbol, selected, source_by_evidence
        )
        inferences, inference_refs = self._inferences(selected, intent_by_symbol, observation_by_symbol)
        cash_stance = self._mapping(team, "cash_stance")
        market_view = self._market_view(momentum, cash_stance, proposal, observations[0]["obs_id"])
        eligible = self._eligible_tickers(snapshot)
        strategy = self._strategy(profile)
        return {
            "sources": sources,
            "observations": observations,
            "market_view": market_view,
            "inferences": inferences,
            "inference_refs_by_ticker": inference_refs,
            "agent_metadata": {
                "model_provider": agent["model_provider"],
                "model_version": agent["model_version"],
                "run_started_at": started,
                "run_completed_at": completed,
                "code_version": code_version,
            },
            "eligible_tickers": eligible,
            "strategy_statement": strategy,
            "provenance": {
                "decision_bundle_id": bundle.get("bundle_id"),
                "decision_cutoff": bundle.get("decision_cutoff"),
                "profile_sha256": canonical_sha256(profile),
                "daily_run_id": daily_run.get("run_id"),
                "builder": "dplan-context-v1",
            },
        }

    @staticmethod
    def _mapping(container: Mapping[str, object], name: str) -> Mapping[str, object]:
        value = container.get(name)
        if not isinstance(value, Mapping):
            raise DPlanError("缺少或無效的 %s" % name)
        return value

    def _profile(self, profile: Mapping[str, object]) -> None:
        if profile.get("schema_version") != "1.0":
            raise DPlanError("competition_profile.schema_version 必須為 1.0")
        if profile.get("strategy_status") != "approved":
            raise DPlanError("competition_profile 策略尚未確認；填妥策略欄位並設為 approved 後才能產生 D-Plan")
        team_id = profile.get("team_id")
        if not isinstance(team_id, str) or not team_id:
            raise DPlanError("competition_profile.team_id 不得為空")
        agent = self._mapping(profile, "agent")
        if not isinstance(agent.get("model_provider"), str) or not agent.get("model_provider"):
            raise DPlanError("competition_profile.agent.model_provider 不得為空")
        if not isinstance(agent.get("model_version"), str) or not agent.get("model_version"):
            raise DPlanError("competition_profile.agent.model_version 不得為空")

    def _strategy(self, profile: Mapping[str, object]) -> Dict[str, str]:
        raw = self._mapping(profile, "strategy")
        name, theme, philosophy = raw.get("etf_name"), raw.get("investment_theme"), raw.get("investment_philosophy")
        if not all(isinstance(value, str) and value.strip() for value in (name, theme, philosophy)):
            raise DPlanError("competition_profile.strategy 的 ETF 名稱、主題與理念均不得為空")
        approved_at = raw.get("approved_at")
        approved = _as_taipei(approved_at, "competition_profile.strategy.approved_at")
        body = {"name": name, "theme": theme, "philosophy": philosophy, "approved_at": approved}
        return {**body, "sha256": canonical_sha256(body)}

    @staticmethod
    def _selected_symbols(account: Mapping[str, object], proposal: Mapping[str, object]) -> Sequence[str]:
        held = {str(row.get("symbol")) for row in account.get("positions", []) if isinstance(row, Mapping)}
        target = {
            str(row.get("symbol"))
            for row in DPlanContextBuilder._mapping(proposal, "allocation_proposal").get("positions", [])
            if isinstance(row, Mapping)
        }
        symbols = sorted((held | target) - {""})
        if not 1 <= len(symbols) <= 60:
            raise DPlanError("D-Plan context 無法從持倉與目標配置取得 1–60 檔股票")
        return symbols

    @staticmethod
    def _price_evidence(snapshot: Mapping[str, object], symbols: Sequence[str]) -> Dict[str, Mapping[str, object]]:
        price_ids = {
            str(row.get("symbol")): str(row.get("source_evidence_id"))
            for row in snapshot.get("latest_prices", []) if isinstance(row, Mapping)
        }
        by_id = {
            str(row.get("evidence_id")): row for row in snapshot.get("source_evidence", [])
            if isinstance(row, Mapping)
        }
        selected = {}
        for symbol in symbols:
            item = by_id.get(price_ids.get(symbol, ""))
            if item is None:
                raise DPlanError("%s 缺少可引用的封存價格來源" % symbol)
            selected[symbol] = item
        return selected

    @staticmethod
    def _sources(evidence: Mapping[str, Mapping[str, object]]):
        groups: Dict[tuple, list] = {}
        for item in evidence.values():
            authority = str(item.get("authority"))
            source = str(item.get("source"))
            if authority not in {"twse", "tpex", "taifex", "mops", "fininst", "media", "vendor", "other"}:
                raise DPlanError("價格來源 authority 不符合 D-Plan：%s" % authority)
            groups.setdefault((authority, source), []).append(item)
        sources, ref_by_evidence = [], {}
        for index, ((authority, source), rows) in enumerate(sorted(groups.items()), 1):
            first = sorted(rows, key=lambda row: str(row.get("url", "")))[0]
            if not isinstance(first.get("url"), str) or not first["url"].startswith(("http://", "https://")):
                raise DPlanError("封存價格來源缺少 http(s) URL")
            record = {
                "source_id": "S%d" % index,
                "authority": authority,
                "name": "%s 封存日行情（自動整理）" % source,
                "url": first["url"],
                "content_as_of": _as_taipei(first.get("content_as_of"), "price.content_as_of"),
            }
            if first.get("fetched_at") is not None:
                record["fetched_at"] = _as_taipei(first["fetched_at"], "price.fetched_at")
            sources.append(record)
            for row in rows:
                ref_by_evidence[str(row["evidence_id"])] = record["source_id"]
        return sources, ref_by_evidence

    def _observations(self, momentum, by_symbol, symbols, source_by_evidence):
        regime = self._mapping(momentum, "regime_assessment")
        if regime.get("status") != "available" or regime.get("regime") not in _REGIME:
            raise DPlanError("動能市場狀態 unavailable，不能建立 D-Plan market_view")
        source_refs = sorted(set(source_by_evidence.values()))
        observations = [{
            "obs_id": "O1", "source_ref": source_refs,
            "topic": "market_breadth",
            "statement": "交易池 MA20 與 MA60 之上比例由封存行情確定性計算，用於市場狀態分類。",
            "values": {
                "coverage_ratio": _number(regime["coverage_ratio"], "coverage_ratio"),
                "breadth_above_ma20": _number(regime["breadth_above_ma20"], "breadth_above_ma20"),
                "breadth_above_ma60": _number(regime["breadth_above_ma60"], "breadth_above_ma60"),
            },
        }]
        result = {}
        for symbol in symbols:
            item = by_symbol.get(symbol)
            if not isinstance(item, Mapping) or item.get("status") != "available":
                raise DPlanError("%s 缺少可用動能資料，不能自動建立引用鏈" % symbol)
            evidence_ids = item.get("evidence_ids")
            if not isinstance(evidence_ids, list) or not evidence_ids or str(evidence_ids[0]) not in source_by_evidence:
                raise DPlanError("%s 動能資料缺少封存來源引用" % symbol)
            obs_id = "O%d" % (len(observations) + 1)
            ticker = symbol[:4]
            observations.append({
                "obs_id": obs_id,
                "source_ref": [source_by_evidence[str(evidence_ids[0])]],
                "topic": "price_momentum",
                "statement": "%s 的封存收盤價、5／20／60 日報酬與均線位置由日行情確定性計算。" % ticker,
                "values": {
                    "close": _number(item["close"], "%s.close" % symbol),
                    "return_5d": _number(item["return_5d"], "%s.return_5d" % symbol),
                    "return_20d": _number(item["return_20d"], "%s.return_20d" % symbol),
                    "return_60d": _number(item["return_60d"], "%s.return_60d" % symbol),
                    "ma20": _number(item["ma20"], "%s.ma20" % symbol),
                    "ma60": _number(item["ma60"], "%s.ma60" % symbol),
                    "atr_14_pct": _number(item["atr_14_pct"], "%s.atr_14_pct" % symbol),
                },
            })
            result[symbol] = obs_id
        return observations, result

    @staticmethod
    def _inferences(symbols, intent_by_symbol, observation_by_symbol):
        inferences, refs = [], {}
        for symbol in symbols:
            item = intent_by_symbol.get(symbol)
            if not isinstance(item, Mapping) or not isinstance(item.get("rationale"), str):
                raise DPlanError("%s 缺少已驗證 TradeDecision rationale" % symbol)
            rationale = item["rationale"].strip()
            if len(rationale) < 20:
                raise DPlanError("%s TradeDecision rationale 過短" % symbol)
            ticker = symbol[:4]
            inf_id = "I%d" % (len(inferences) + 1)
            counter = "；".join(str(value) for value in item.get("unresolved_questions", []) if value) or None
            conclusion = "TradeDecision 對 %s 的意圖為 %s。" % (ticker, item.get("intent"))
            inferences.append({
                "inf_id": inf_id,
                "premise_refs": [observation_by_symbol[symbol]],
                "logic": rationale,
                "conclusion": conclusion,
                "counter_evidence": counter,
            })
            refs[ticker] = [inf_id]
        return inferences, refs

    def _market_view(self, momentum, cash_stance, proposal, market_obs_id):
        regime = self._mapping(momentum, "regime_assessment")
        raw_regime = str(regime["regime"])
        stance = cash_stance.get("level")
        if stance not in _STANCE:
            raise DPlanError("cash_stance.level 不合法，不能建立 D-Plan market_view")
        allocation = self._mapping(proposal, "allocation_proposal")
        cash_weight = _number(allocation.get("cash_weight"), "allocation_proposal.cash_weight")
        low, high = max(0.0, cash_weight - 0.01), min(0.25, cash_weight + 0.01)
        orders = self._mapping(proposal, "order_proposal").get("orders", [])
        buy = sum(
            Decimal(str(order.get("gross_amount", "0")))
            for order in orders if isinstance(order, Mapping) and order.get("side") == "buy"
        )
        sell = sum(
            Decimal(str(order.get("gross_amount", "0")))
            for order in orders if isinstance(order, Mapping) and order.get("side") == "sell"
        )
        nav = Decimal(str(allocation.get("nav")))
        net = buy - sell
        exposure = "hold" if abs(net) <= nav * Decimal("0.02") else "increase" if net > 0 else "reduce"
        return {
            "basis_refs": [market_obs_id],
            "logic": "交易池寬度依封存價格資料計算為 %s；現金姿態為 %s，委託淨方向依已核准配置確定性推導。" % (raw_regime, stance),
            "regime": _REGIME[raw_regime],
            "stance": stance,
            "posture": {"net_exposure_intent": exposure, "target_cash_pct_range": [low, high]},
            "counter_evidence": "市場狀態僅使用交易池價格寬度；若後續封存資料覆蓋不足，必須停止輸出。",
        }

    @staticmethod
    def _eligible_tickers(snapshot: Mapping[str, object]):
        symbols = [str(row.get("symbol")) for row in snapshot.get("latest_prices", []) if isinstance(row, Mapping)]
        tickers = [symbol[:4] for symbol in symbols if len(symbol) >= 4]
        if snapshot.get("universe_size") != 150 or len(tickers) != 150 or len(set(tickers)) != 150:
            raise DPlanError("ResearchSnapshot 未提供可核實的 150 檔官方交易池")
        return sorted(tickers)
