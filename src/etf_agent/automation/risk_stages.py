"""每日決策鏈的風險階段：配置前現金姿態與每次修正的風險審查。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.decision import (
    MARKET_REGIME_EVIDENCE_ID, DecisionToolError, MomentumEngine, RiskReviewValidator, artifact_content_sha256,
    liquidity_exposure, sector_exposure,
)
from etf_agent.decision.contracts import DecisionContext
from etf_agent.decision.drawdown import stance_meets_floor
from etf_agent.decision.recency import evidence_recency
from etf_agent.decision.market_index import market_index_facts
from etf_agent.decision.turn_facts import turn_facts
from etf_agent.decision.risk import CASH_BUFFER_CEILING_MARGIN
from etf_agent.decision.market_scope import market_sentiment_channel, overnight_market_channel
from etf_agent.decision.sizing import (
    CASH_STANCES, POSITION_BREADTHS, allowed_cash_grades, cash_grade_grid, validate_cash_stance,
)

from .agent_runner import AgentTask, DailyPipelineError
from .agent_schemas import (
    CASH_GRADE_STANCE_SCHEMA, CASH_STANCE_SCHEMA, REVIEW_SCHEMA, _SHARED_RULES, cash_stance_recency_schema,
    cash_stance_turn_facts_schema,
)

# 閘門段落只說「本次簡報含哪個欄位」並指向 cash-stance.md 的對應節；規則本身只寫在那裡（與 validate_cash_stance 同步）。
OVERNIGHT_PROMPT = (
    "overnight_market 是隔夜市場背景，只作現金姿態背景之一，用法見 cash-stance.md「overnight_market」節。"
)


TYPED_STANCE_PROMPT = (
    "摘要含 market_level_evidence_ids：evidence_ids 至少一個市場層級證據，全市場情緒 negative 時姿態為 neutral 或 defensive，"
    "見 cash-stance.md「market_level_evidence_ids」節。"
)


SOFT_FLOOR_PROMPT = (
    "摘要含 high_conviction_cash_floor_by_stance（各姿態的高信心現金下限），把該姿態的實際現金落點寫進 rationale，"
    "見 cash-stance.md 對應節。"
)


def _soft_floors(
    cash_buffers: Optional[Mapping[str, object]], gate: Optional[Mapping[str, object]]
) -> Optional[Dict[str, str]]:
    """high_conviction_cash_floor 啟用時，各姿態的高信心現金下限（緩衝 × floor_ratio）；沒有閘門或缺緩衝回 None。"""
    if not cash_buffers or not isinstance(gate, Mapping) or "floor_ratio" not in gate:
        return None
    try:
        ratio = Decimal(str(gate["floor_ratio"]))
        return {stance: str(Decimal(str(value)) * ratio) for stance, value in cash_buffers.items()}
    except InvalidOperation:
        raise DailyPipelineError("position_sizing.high_conviction_cash_floor.floor_ratio 無法解析")


RECENCY_PROMPT = (
    "摘要開頭含 evidence_recency（程式依內容時間分組的證據時間表）：另輸出 recency_reconciliation（列舉），"
    "並在最新一組有證據時至少引用其中一個，規則見 cash-stance.md「evidence_recency」節。"
)


MARKET_INDEX_PROMPT = (
    "摘要含 market_index_facts（程式依證交所加權指數算的當日收盤、漲跌、近 5／20 日報酬與離高點回檔，是背景事實、"
    "沒有 evidence_id、不可引用）：判斷現金姿態時一併參考，但單日漲跌不代表趨勢，不要只憑它改變姿態。規則見 cash-stance.md「market_index_facts」節。"
)

TURN_FACTS_PROMPT = (
    "摘要含 turn_facts（程式依行情算的交易池轉向事實，是事實不是訊號）：另輸出 turn_assessment"
    "（no_turn_evidence／turn_unconfirmed／turn_confirmed），只是紀錄你怎麼看，不要求與現金姿態一致，不影響現金格點；"
    "若摘要另附程式狀態機結果，你的列舉才會與程式狀態取較保守者、決定曝險換股是否放寬。規則見 cash-stance.md「turn_facts」節。"
)


TURN_STATE_PROMPT = (
    "摘要另含 turn_state（程式狀態機的結果，只是背景，沒有證據編號、不可引用）：你的 turn_assessment 與它取較保守者，"
    "才決定防守姿態下的曝險換股是否放寬；你認為尚未轉向就填 no_turn_evidence 或 turn_unconfirmed，不要為了配合它而升級，"
    "規則見 cash-stance.md「turn_state」節。"
)


CASH_GRADE_PROMPT = (
    "Policy 啟用 cash_ratio_range：摘要含 cash_grade_grid 與 cash_grade_allowed_by_stance，除了姿態另給 cash_grade"
    "（lower／middle／upper），只能選該姿態今日可選的子等級，你不得輸出百分比，規則見 cash-stance.md「cash_grade_allowed_by_stance」節。"
)


def _grade_brief(gate: Mapping[str, object]) -> Dict[str, object]:
    """cash_ratio_range 啟用時給風險 Agent 的格點、今日可選子等級與前次比例（不含前次 run 的識別碼）。"""
    try:
        cash_grade_grid(gate)
        allowed = allowed_cash_grades(gate)
    except DecisionToolError as error:
        raise DailyPipelineError("position_sizing.cash_ratio_range 無法計算今日可選子等級：%s" % error)
    anchor = gate["anchor"]
    source = anchor.get("source") if isinstance(anchor, Mapping) else None
    return {
        "cash_grade_grid": {level: dict(gate["grid"][level]) for level in CASH_STANCES},  # type: ignore[index]
        "cash_grade_allowed_by_stance": {
            level: {grade: gate["grid"][level][grade] for grade in allowed[level]}  # type: ignore[index]
            for level in CASH_STANCES
        },
        "cash_ratio_anchor": {
            "status": anchor["status"], "ratio": anchor["ratio"],  # type: ignore[index]
            "as_of": source.get("target_session_start") if isinstance(source, Mapping) else None,
            "citable": False,
        },
        "max_daily_raise": gate["max_daily_raise"],
    }


def _cash_ratio(account: Mapping[str, object]) -> Optional[str]:
    """帳戶現金占 NAV 的比例（字串）；缺值或無法解析時回 None，不猜。"""
    try:
        cash, nav = Decimal(str(account["cash"])), Decimal(str(account["nav"]))
    except (KeyError, InvalidOperation):
        return None
    return str((cash / nav).quantize(Decimal("0.0001"))) if nav > 0 else None


class RiskStagesMixin:
    """由 ``DailyDecisionPipeline`` 組合；依賴 ``PipelineBase`` 的 save_*、agent_step 與 ``root``。"""

    def run_cash_stance(
        self,
        bundle: Mapping[str, object],
        momentum: Mapping[str, object],
        analyst_reports: Mapping[str, Mapping[str, object]],
        drawdown_control: Optional[Mapping[str, object]] = None,
        breadth_targets: Optional[Mapping[str, object]] = None,
        cash_buffers: Optional[Mapping[str, object]] = None,
        high_conviction_cash_floor: Optional[Mapping[str, object]] = None,
        cash_ratio_range: Optional[Mapping[str, object]] = None,
        prior_context: Optional[Mapping[str, object]] = None,
        cash_stance_recency: Optional[Mapping[str, object]] = None,
        cash_stance_turn_facts: Optional[Mapping[str, object]] = None,
        turn_state: Optional[Mapping[str, object]] = None,
        market_index_facts_gate: Optional[Mapping[str, object]] = None,
    ) -> Dict[str, object]:
        """風險 Agent 依市場層級摘要給現金姿態與持股廣度；分布與計數由程式統計，回撤下限由程式強制。

        brief 不再附交易 Agent 的意圖與 buy／add 信心統計（避免群體思維）：現金姿態只看市場層級背景、
        分析師看法分布、交易狀態、公司事件風險、帳戶與回撤。"""

        def counts(values: Sequence[object]) -> Dict[str, int]:
            result: Dict[str, int] = {}
            for value in values:
                result[str(value)] = result.get(str(value), 0) + 1
            return dict(sorted(result.items()))

        price_evidence = sorted(
            {str(row["source_evidence_id"]) for row in bundle["snapshot"]["latest_prices"] if row.get("source_evidence_id")}
        )
        # 隔夜市場背景只在決策輸入有附時才進 brief 與提示詞，沒有附的舊流程維持原樣。
        overnight = overnight_market_channel(bundle)
        context = DecisionContext(bundle)
        typed = context.evidence_policy is not None
        regime = momentum.get("regime_assessment")
        if typed and isinstance(regime, Mapping):
            # typed_evidence_v1：市場狀態有市場層級證據編號，現金姿態可引用它當市場層級依據。
            regime = {**regime, "evidence_id": MARKET_REGIME_EVIDENCE_ID}
        soft_floors = _soft_floors(cash_buffers, high_conviction_cash_floor)
        # cash_ratio_range（版本閘門，已綁定 anchor）：簡報改附格點與今日可選子等級，不再附三段階梯，避免當成錨點。
        graded = cash_ratio_range is not None
        if graded and "anchor" not in cash_ratio_range:  # type: ignore[operator]
            raise DailyPipelineError("position_sizing.cash_ratio_range 尚未綁定 anchor（前次現金比例）")
        grade_brief = _grade_brief(cash_ratio_range) if graded else {}  # type: ignore[arg-type]
        # cash_stance_recency（版本閘門）：簡報第一個鍵附程式算的證據時間表；沒有閘門時簡報與舊版逐鍵相同。
        recency_brief: Dict[str, object] = {}
        if cash_stance_recency is not None:
            if not typed:
                raise DailyPipelineError("position_sizing.cash_stance_recency 只能用於 evidence_policy=typed_evidence_v1")
            recency_brief = {"evidence_recency": evidence_recency(bundle, context.market_evidence_ids)}
        # cash_stance_turn_facts（版本閘門）：regime_assessment 旁附程式算的轉向事實；沒有閘門時簡報與舊版逐鍵相同。
        turn_brief: Dict[str, object] = {}
        if cash_stance_turn_facts is not None:
            if not typed:
                raise DailyPipelineError("position_sizing.cash_stance_turn_facts 只能用於 evidence_policy=typed_evidence_v1")
            turn_brief = {"turn_facts": turn_facts(bundle)}
            if turn_state is not None:
                # rebound_participation（版本閘門）：程式狀態機的結果只當背景（沒有 evidence_id，不可引用）。
                turn_brief["turn_state"] = {"citable": False, **dict(turn_state)}
        # market_index_facts（版本閘門）：附程式算的加權指數事實（背景、不可引用）；沒有閘門時簡報與舊版逐鍵相同。
        index_brief: Dict[str, object] = (
            {"market_index_facts": market_index_facts(bundle)} if market_index_facts_gate is not None else {}
        )
        brief_path = self.save_brief(
            "brief_cash_stance",
            {
                **recency_brief,
                "regime_assessment": regime,
                **turn_brief,
                **index_brief,
                "analyst_outlooks": {
                    name: counts([item.get("outlook") for item in report.get("items", [])])
                    for name, report in analyst_reports.items()
                },
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
                # 持股廣度對應的目標檔數由 Policy 決定；風險 Agent 只選等級，程式依信心順序挑到該檔數。
                "positions_by_breadth": dict(breadth_targets) if breadth_targets else None,
                # 姿態的代價：各姿態對應的現金緩衝與目前現金比例，讓風險 Agent 知道選哪個姿態會不會讓買單被緩衝擋掉。
                **({"cash_buffer_by_stance": dict(cash_buffers) if cash_buffers else None} if not graded else grade_brief),
                "cash_ratio_now": _cash_ratio(bundle["account_snapshot"]),
                # high_conviction_cash_floor（版本閘門）啟用時才附：各姿態下高信心買單可動用到、程式減碼也只補到的現金下限。
                **({"high_conviction_cash_floor_by_stance": soft_floors} if soft_floors else {}),
                "citable_evidence_ids": price_evidence,
                **({"overnight_market": overnight, "citable_evidence_ids": sorted(
                    set(price_evidence) | {row["evidence_id"] for row in overnight["components"] if row["evidence_id"]})}
                   if overnight is not None else {}),
                # typed_evidence_v1：市場層級證據另列一份，至少要引用其中一個；個股行情只能作補充。
                **({"market_level_evidence_ids": sorted(context.market_evidence_ids),
                    "citable_evidence_ids": sorted(set(price_evidence) | context.market_evidence_ids)} if typed else {}),
            },
        )
        floor = (drawdown_control or {}).get("stance_floor")

        def validate(stance: Mapping[str, object]) -> List[str]:
            errors: List[str] = []
            validate_cash_stance(
                context, stance, "cash_stance", errors, cash_ratio_range=cash_ratio_range, prior_context=prior_context,
                recency_gate=cash_stance_recency, turn_facts_gate=cash_stance_turn_facts,
            )
            if breadth_targets and isinstance(stance, Mapping) and "breadth" not in stance:
                errors.append("cash_stance.breadth 必須是 %s" % "／".join(POSITION_BREADTHS))
            if isinstance(stance, Mapping) and not stance_meets_floor(stance.get("level"), floor):
                errors.append("帳戶回撤 %s 已達門檻，現金姿態不得低於 %s" % (drawdown_control.get("drawdown"), floor))
            return errors

        task = AgentTask(
            "cash_stance",
            "你是 $portfolio-risk-review 風險 Agent 的市場風險評估。先讀 %s（「配置前現金姿態」一節）及其連結的 %s，"
            "再讀市場層級摘要 %s。給整體現金姿態 aggressive／neutral／defensive 與持股廣度 breadth"
            "（concentrated／balanced／diversified），rationale 同時說明兩者的理由；"
            "摘要刻意不附交易 Agent 的意圖與信心統計，請獨立判斷市場風險。競賽規定現金必須低於 NAV 25%%，%s"
            "%s%s%s判斷依據、證據選法與各欄位的用法依 cash-stance.md。%s"
            % (self.root / "tradeagent/risk/portfolio-risk-review/SKILL.md",
               self.root / "tradeagent/risk/portfolio-risk-review/references/cash-stance.md", brief_path,
               CASH_GRADE_PROMPT if graded else "姿態對應的現金比例由 Policy 決定，你不得輸出百分比。",
               OVERNIGHT_PROMPT if overnight is not None else "",
               SOFT_FLOOR_PROMPT if soft_floors else "",
               TYPED_STANCE_PROMPT if typed else "", _SHARED_RULES)
            + (RECENCY_PROMPT if cash_stance_recency is not None else "")
            + (TURN_FACTS_PROMPT if cash_stance_turn_facts is not None else "")
            + (TURN_STATE_PROMPT if turn_state is not None else "")
            + (MARKET_INDEX_PROMPT if market_index_facts_gate is not None else ""),
            cash_stance_turn_facts_schema(graded, cash_stance_recency is not None) if cash_stance_turn_facts is not None
            else cash_stance_recency_schema(graded) if cash_stance_recency is not None
            else (CASH_GRADE_STANCE_SCHEMA if graded else CASH_STANCE_SCHEMA),
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

        max_revisions = int(policy["max_revisions"])

        def build(output: Mapping[str, object]) -> Dict[str, object]:
            decision = output["decision"]
            rationale = output["rationale"]
            risk_flags = list(output["risk_flags"])
            if decision == "revise" and revision >= max_revisions:
                # 已用完修正次數：確定性降級為 reject，不靠下一次被 Validator 拒絕後整條鏈失敗。
                decision = "reject"
                rationale = "已達修正上限（%d 次）仍要求修正，依契約降級為拒絕：%s" % (max_revisions, rationale)
                risk_flags.append("REVISION_LIMIT_REACHED")
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
                "rationale": rationale,
                "evidence_ids": output["evidence_ids"],
                "risk_flags": risk_flags,
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
            "你是 $portfolio-risk-review 風險審查子 Agent。先讀 %s 及其連結的 %s，再讀提案 %s、情境 %s、Guard %s、"
            "程式計算的產業實際權重 %s 與每筆委託佔 20 日均量的比例 %s（flagged 表示超過警示比例或缺均量，可考慮 remove_candidate 或降低周轉）；"
            "可引用的 evidence ID 清單在 %s（evidence_ids 只能從它選取）。輸出 approve、revise 或 reject；revision_actions 的欄位格式與限制依 risk-review-output.md，"
            "本次 increase_cash_buffer 的 value 不得高於 %s（競賽現金上限 %s 減邊際 %s）；已是第 %d 次修正、上限 %d 次"
            "（已達上限時不得再 revise，只能 approve 或 reject，要求修正會由程式降級為 reject）。%s"
            % (
                self.root / "tradeagent/risk/portfolio-risk-review/SKILL.md",
                self.root / "tradeagent/risk/portfolio-risk-review/references/risk-review-output.md",
                names["proposal"], names["scenario"], names["guard"], names["sector_exposure"], names["liquidity_exposure"], citable_path,
                Decimal(str(policy["cash_weight_ceiling"])) - CASH_BUFFER_CEILING_MARGIN,
                policy["cash_weight_ceiling"], CASH_BUFFER_CEILING_MARGIN, revision, int(policy["max_revisions"]), _SHARED_RULES,
            ),
            REVIEW_SCHEMA,
        )
        return self.agent_step(task, build, validate)
