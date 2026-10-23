"""每日決策鏈的風險階段：配置前現金姿態與每次修正的風險審查。"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.decision import (
    DecisionToolError, MomentumEngine, RiskReviewValidator, artifact_content_sha256, liquidity_exposure, sector_exposure,
)
from etf_agent.decision.contracts import DecisionContext
from etf_agent.decision.drawdown import stance_meets_floor
from etf_agent.decision.market_scope import market_sentiment_channel
from etf_agent.decision.sizing import validate_cash_stance

from .agent_runner import AgentTask, DailyPipelineError
from .agent_schemas import CASH_STANCE_SCHEMA, REVIEW_SCHEMA, _SHARED_RULES


class RiskStagesMixin:
    """由 ``DailyDecisionPipeline`` 組合；依賴 ``PipelineBase`` 的 save_*、agent_step 與 ``root``。"""

    def run_cash_stance(
        self,
        bundle: Mapping[str, object],
        momentum: Mapping[str, object],
        analyst_reports: Mapping[str, Mapping[str, object]],
        trade_decision: Mapping[str, object],
        drawdown_control: Optional[Mapping[str, object]] = None,
    ) -> Dict[str, object]:
        """風險 Agent 依市場層級摘要給現金姿態；分布與計數由程式統計，回撤下限由程式強制。"""

        def counts(values: Sequence[object]) -> Dict[str, int]:
            result: Dict[str, int] = {}
            for value in values:
                result[str(value)] = result.get(str(value), 0) + 1
            return dict(sorted(result.items()))

        price_evidence = sorted(
            {str(row["source_evidence_id"]) for row in bundle["snapshot"]["latest_prices"] if row.get("source_evidence_id")}
        )
        brief_path = self.save_brief(
            "brief_cash_stance",
            {
                "regime_assessment": momentum.get("regime_assessment"),
                "analyst_outlooks": {
                    name: counts([item.get("outlook") for item in report.get("items", [])])
                    for name, report in analyst_reports.items()
                },
                "trade_intents": counts([item.get("intent") for item in trade_decision.get("items", [])]),
                "buy_add_convictions": counts(
                    [item.get("conviction") for item in trade_decision.get("items", []) if item.get("intent") in {"buy", "add"}]
                ),
                "tradability": counts(
                    [item.get("state") for item in (bundle.get("tradability_assessment") or {}).get("symbols", [])]
                ),
                "company_events": [
                    {"symbol": item["symbol"], "event_outlook": item.get("event_outlook"),
                     "events": item.get("events", []), "findings": item.get("findings", []),
                     "data_gaps": item.get("data_gaps", [])}
                    for item in analyst_reports["event"].get("items", [])
                ],
                "account": {key: bundle["account_snapshot"].get(key) for key in ("cash", "nav", "positions")},
                "rules": {key: bundle["rules"].get(key) for key in ("cash_weight_must_be_below", "min_positions", "max_positions")},
                # 策略文件：風險 Agent 看整體市場情緒；與事件分析師讀同一份由程式重建的全市場通道。
                "market_sentiment": market_sentiment_channel(bundle),
                "drawdown_control": dict(drawdown_control) if drawdown_control else None,
                "citable_evidence_ids": price_evidence,
            },
        )
        floor = (drawdown_control or {}).get("stance_floor")
        context = DecisionContext(bundle)

        def validate(stance: Mapping[str, object]) -> List[str]:
            errors: List[str] = []
            validate_cash_stance(context, stance, "cash_stance", errors)
            if isinstance(stance, Mapping) and not stance_meets_floor(stance.get("level"), floor):
                errors.append("帳戶回撤 %s 已達門檻，現金姿態不得低於 %s" % (drawdown_control.get("drawdown"), floor))
            return errors

        task = AgentTask(
            "cash_stance",
            "你是 $portfolio-risk-review 風險 Agent 的市場風險評估。先讀 %s（「配置前現金姿態」一節），再讀市場層級摘要 %s。"
            "依 regime、分析師看法分布、交易決策、交易狀態與重大事件風險，給整體現金姿態 aggressive／neutral／defensive。"
            "競賽規定現金必須低於 NAV 25%%，姿態對應的現金比例由 Policy 決定，你不得輸出百分比。"
            "market_sentiment 是整體台股情緒（全市場新聞，不可用時為恐懼貪婪指標），status 非 available 時不得當成情緒結論；"
            "drawdown_control 是程式從帳本計算的峰值回撤；stance_floor 不是 aggressive 時，姿態不得比它更積極。"
            "evidence_ids 從 citable_evidence_ids 或公司事件 findings 的 evidence_ids 中選取。%s"
            % (self.root / "tradeagent/risk/portfolio-risk-review/SKILL.md", brief_path, _SHARED_RULES),
            CASH_STANCE_SCHEMA,
        )
        stance = self.agent_step(task, lambda output: dict(output), validate)
        self.save_artifact("cash_stance", stance)
        return stance

    # ------------------------------------------------------------------ review
    def _review(
        self, bundle: Mapping[str, object], policy: Mapping[str, object],
        proposal: Mapping[str, object], scenario: Mapping[str, object], guard: Mapping[str, object], run_id: str,
    ) -> Dict[str, object]:
        validator = RiskReviewValidator(bundle, policy)
        revision = int(proposal["revision_count"])

        def build(output: Mapping[str, object]) -> Dict[str, object]:
            decision = output["decision"]
            review: Dict[str, object] = {
                "schema_version": "1.0",
                "review_id": "risk-review:%s:r%d" % (run_id, revision),
                "bundle_id": bundle["bundle_id"],
                "snapshot_id": bundle["snapshot_id"],
                "decision_cutoff": bundle["decision_cutoff"],
                "bundle_hash": bundle["bundle_sha256"],
                "proposal_id": proposal["proposal_id"],
                "scenario_id": scenario["scenario_id"],
                "guard_id": guard["guard_id"],
                "revision_index": revision + 1 if decision == "revise" else revision,
                "decision": decision,
                "rationale": output["rationale"],
                "evidence_ids": output["evidence_ids"],
                "risk_flags": output["risk_flags"],
                "revision_actions": output["revision_actions"] if decision == "revise" else [],
                "unresolved_questions": output["unresolved_questions"],
                "status": "completed",
                "errors": [],
            }
            review["content_sha256"] = artifact_content_sha256(review)
            return review

        def validate(review: Mapping[str, object]) -> List[str]:
            return validator.validate(proposal, scenario, guard, review)

        if guard.get("passed") is not True:
            # 契約規定硬性規則失敗時只能 reject，確定性產生，不交給 Agent 判斷。
            failed = [str(item.get("rule_id")) for item in guard.get("checks", []) if not item.get("passed")]
            review = build(
                {
                    "decision": "reject",
                    "rationale": "CompetitionGuard 硬性規則未通過（%s），依契約自動拒絕。" % "、".join(failed),
                    "evidence_ids": [str(bundle["account_snapshot"]["source_evidence_id"])],
                    "risk_flags": ["GUARD_FAILED:%s" % rule for rule in failed],
                    "unresolved_questions": [],
                    "revision_actions": [],
                }
            )
            errors = validate(review)
            if errors:
                raise DailyPipelineError("Guard 失敗的拒絕審查驗證失敗：" + "；".join(errors))
            return review
        exposure = sector_exposure(proposal, policy)
        liquidity = liquidity_exposure(proposal, MomentumEngine(bundle).run())
        # 審查 Agent 只讀提案／情境／Guard，沒有共同輸入；不提供可引用清單時它會編造 artifact ID
        # 而被 Validator 拒絕，正式流程曾要靠第二次重試才通過。
        held = {str(item["symbol"]).upper() for item in proposal["allocation_proposal"]["positions"]}
        citable = sorted(
            str(row["source_evidence_id"]) for row in bundle["snapshot"]["latest_prices"]
            if row.get("source_evidence_id") and str(row["symbol"]).upper() in held
        )
        citable_path = self.save_artifact("review_citable_evidence_r%d" % revision, {"citable_evidence_ids": citable})
        names = {
            name: self.save_artifact("%s_review_input_r%d" % (name, revision), payload)
            for name, payload in (("proposal", proposal), ("scenario", scenario), ("guard", guard),
                                  ("sector_exposure", exposure), ("liquidity_exposure", liquidity))
        }
        task = AgentTask(
            "review_r%d" % revision,
            "你是 $portfolio-risk-review 風險審查子 Agent。先讀 %s，再讀提案 %s、情境 %s、Guard %s、程式計算的產業實際權重 %s"
            "與每筆委託佔 20 日均量的比例 %s（flagged 表示超過警示比例或缺均量，可考慮 remove_candidate 或降低周轉）；可引用的 evidence ID 清單在 %s。"
            "輸出 approve、revise 或 reject。revision_actions 每一項都必須含 type、symbol、value、reason 四個字串欄位；"
            "remove_candidate 的 symbol 為本提案買進股票且 value 為空字串；其他類型的 symbol 為空字串。revise 只能使用 remove_candidate、"
            "increase_cash_buffer（value 不得低於目前現金緩衝）、reduce_max_stock_weight、reduce_turnover_limit（value 不得高於目前值），"
            "value 為 0～1 的小數字串；已是第 %d 次修正、上限 %d 次。evidence_ids 只能從可引用清單中選取，不得引用 proposal／scenario／guard 等 artifact ID。%s"
            % (
                self.root / "tradeagent/risk/portfolio-risk-review/SKILL.md",
                names["proposal"], names["scenario"], names["guard"], names["sector_exposure"], names["liquidity_exposure"], citable_path,
                revision, int(policy["max_revisions"]), _SHARED_RULES,
            ),
            REVIEW_SCHEMA,
        )
        return self.agent_step(task, build, validate)
