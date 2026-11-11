"""每日決策鏈的團隊階段：分析師、多頭／空頭研究員與交易 Agent（逐批、可平行）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.decision import (
    AnalystReportValidator,
    DecisionToolError,
    PriorDecisionContextValidator,
    ResearchDebateBundleValidator,
    StancePacketValidator,
    TradeDecisionValidator,
    build_analyst_brief,
    build_prior_decision_context,
    build_research_debate,
    build_stance_brief,
    canonical_sha256,
    compute_fundamental_metrics,
    find_prior_decision_run,
    prior_decision_brief,
    read_decision_run,
    position_review_brief,
    exposure_review_brief,
    analyst_dip_view,
    dip_background_brief,
    program_ranking,
    ranking_envelope,
    ranking_needs_agent,
    render_trader_report,
    seal_report,
    seal_selection_ranking,
    selection_ranking_brief,
    SelectionRankingValidator,
    seal_stance_packet,
    seal_trade_decision,
    trade_decision_envelope,
)
from etf_agent.decision.analysts import LLM_ANALYSTS, no_event_item, seal_event_market_report
from etf_agent.decision.prior_decision import NO_SESSION_REASON
from etf_agent.decision.exposure_facts import EXPOSURE_FACTS_KEY, nomination_exposure_brief
from etf_agent.decision.exposure_swap import exposure_swap_gate
from etf_agent.decision.quality_dip_swap import quality_dip_swap_gate
from etf_agent.decision.valuation_facts import valuation_background_brief
from etf_agent.decision.held_weight_facts import held_weight_brief
from etf_agent.decision.sizing import reentry_hint_id
from etf_agent.decision.market_index import market_index_facts
from etf_agent.decision.turn_facts import turn_facts
from etf_agent.decision.stance import STANCE_ROLES
from etf_agent.decision.swap_nomination import (
    STATUS_EXHAUSTED,
    STATUS_NOT_NEEDED,
    STATUS_UNAVAILABLE,
    SwapNominationValidator,
    nomination_envelope,
    nomination_facts,
    needs_nomination,
    program_nomination,
    seal_swap_nomination,
    swap_nomination_brief,
)
from etf_agent.decision.release_order import (
    STATUS_EXHAUSTED as RELEASE_EXHAUSTED,
    STATUS_NOT_NEEDED as RELEASE_NOT_NEEDED,
    STATUS_UNAVAILABLE as RELEASE_UNAVAILABLE,
    ReleaseOrderValidator,
    needs_release_order,
    program_release_order,
    release_envelope,
    release_facts,
    release_order_brief,
    seal_release_order,
)
from etf_agent.decision.strategy_lessons import lessons_brief, load_strategy_lessons
from etf_agent.decision.thesis_verdicts import load_thesis_verdicts, verdicts_brief

from .agent_runner import LATER_STAGE_GLOBS, AgentTask, DailyPipelineError
from .agent_schemas import (
    EVENT_BATCH_MAX_EVENTS,
    EVENT_BATCH_SIZE,
    RANKING_SCHEMA,
    RELEASE_ORDER_SCHEMA,
    SWAP_NOMINATION_EXPOSURE_SCHEMA,
    SWAP_NOMINATION_QUALITY_DIP_SCHEMA,
    SWAP_NOMINATION_SCHEMA,
    TRADE_SCHEMA,
    trade_schema_with_dip_assessment,
    _CLAIM_KIND_RULES,
    _EVENT_RULES,
    _FINDING_KIND_RULES,
    _SHARED_RULES,
    analyst_schema,
    event_analyst_schema,
    stance_schema,
)
from etf_agent.decision import evidence_policy
from .batching import merge_batch_items, universe_batches, weighted_batches
from .event_reuse import reusable_event_items


# 本次 brief 含哪些背景區塊，提示只用一句指向 data-rules.md 的對應小節；規則本身只寫在那裡（與 Validator 同步）。
_PRIOR_RULES = (
    "本批輸入含 prior_decision_background 與各股 prior_decision（前次決策背景，只作一致性自我檢查、沒有 evidence_id），"
    "依 data-rules.md「前次決策背景」處理。"
)
_ROTATION_RULES = (
    "本批輸入含 portfolio_slots（程式算好的名額狀態，只是背景、沒有 evidence_id），依 data-rules.md「名額狀態」處理。"
)
_REVIEW_RULES = (
    "本批輸入含 position_review_background 與各持股的 position_review（程式算好的持股檢視，數字不可改寫；"
    "flags 內的 review_id 放進 adopted_claim_ids 剛好一次），依 data-rules.md「持股檢視」處理。"
)

_EXPOSURE_RULES = (
    "本批輸入含 exposure_background 與各持股的 exposure（程式算好的 beta 與上行／下行捕捉，只是背景、沒有 evidence_id、不可引用）；"
    "hold 的 conviction 必填，依 data-rules.md「hold 信心」處理。"
)


_HELD_WEIGHT_RULES = (
    "本批輸入含 held_weight_background 與各持股的 held_weight（程式算的現有權重、單股上限與 high 份額下限，只是背景、沒有 evidence_id、"
    "不可引用，也不得寫進理由）：add 的目標權重是 max(現有權重, 信心份額)，現有權重已不低於份額時加碼不會成交；"
    "份額在你交件時還算不出來，這些事實只幫你判斷 add 是否有實質效果，不改變任何驗證規則。"
)
_COOLDOWN_RULES = (
    "本批部分未持有的股票附有 reentry_cooldown 提示（近期才由交易 Agent 自己出場，是程式事實、不是禁止）：若你仍決定 buy 這檔，"
    "必須把該提示的 hint_id 放進這檔的 adopted_claim_ids 一次，並在 rationale 說明為何與先前出場時的判斷不同；"
    "沒有放進去的 buy 會被程式取消。不 buy 就不要放，也不可放進 rejected_claim_ids。"
)


_DIP_ANALYST_RULES = (
    "本批輸入含 dip_facts_background 與各股的 %s（程式算的事實，不是訊號，數字不可改寫；區塊本身沒有 evidence_id，"
    "引用時用各列附的該股既有證據編號），依 SKILL.md「優質股被大盤拖累的事實」節處理。"
)
_EVENT_DIRECTION_RULES = (
    "每則事件另填列舉 direction（positive／negative／neutral／unknown）：只描述這一則事件對公司的方向，不是整檔 event_outlook，"
    "也不是你對股價的預測；看不出來就填 unknown，依 SKILL.md「事件方向」節處理。"
)
_DIP_TRADER_RULES = (
    "本批輸入含 dip_background 與各股的 dip（程式算的價格事實、財報三個清單與候選標示，只是背景、沒有 evidence_id、不可引用，"
    "也不得寫進理由），依 data-rules.md「優質股被大盤拖累的事實」處理。"
)
_DIP_ASSESSMENT_RULES = (
    "每個 buy／add 另填列舉 dip_assessment（其餘意圖填 null）：not_a_dip＝這檔不是在大盤下跌中被拖累或超跌；market_driven＝跌幅主要可由大盤解釋；"
    "oversold_cause_unclear＝比大盤多跌、原因不明、你仍判斷值得買；stock_specific＝多跌有個股原因；undetermined＝無法判斷。"
    "該股 dip.price_pattern 為 unavailable 時只能填 undetermined。這只是紀錄你怎麼看，與意圖、信心不設對應，不影響名額與權重；"
    "依 data-rules.md「dip_assessment」處理。"
)


def _peer_deny_reads(role: str) -> Tuple[str, ...]:
    peer = "bear" if role == "bull" else "bull"
    # 續跑時 run 目錄可能已有後段產物（含對方論點的 debate、交易與 team_inputs），一併擋掉；不用 ``decision*``，會擋到共同輸入。
    return ("%s_*" % peer, "brief_%s_*" % peer) + LATER_STAGE_GLOBS


class TeamStagesMixin:
    """由 ``DailyDecisionPipeline`` 組合；依賴 ``PipelineBase`` 的 save_*、agent_step 與 _run_jobs。"""

    def run_analyst_team(
        self, bundle: Mapping[str, object], momentum: Mapping[str, object], database_path: Optional[Path] = None,
        dip_gate: Optional[Mapping[str, object]] = None, event_direction: bool = False,
        valuation_facts: Optional[Mapping[str, object]] = None,
    ) -> Dict[str, Dict[str, object]]:
        """三位分析師逐批執行；事件與市場情緒共用一位 Agent、一份報告。

        ``dip_gate`` 是 position_sizing.quality_dip_facts 閘門物件：有時技術分析師多拿逐檔 ``market_attribution``／``price_position``、
        基本面分析師多拿 ``fundamental_flags``（不含 price_pattern／candidate）；沒有閘門時簡報與舊版逐鍵相同。
        ``event_direction``（position_sizing.event_direction 閘門）為 True 時事件分析師每則事件另填 direction 列舉；False 時 Schema 與驗證不變。
        ``valuation_facts``（position_sizing.valuation_facts，估值 V2）有時基本面分析師簡報多附 ``valuation_background``（citable=false、
        沒有識別碼）；沒有時簡報逐鍵不變。"""
        metrics = compute_fundamental_metrics(bundle)
        # bundle 有 fundamental_period_policy 時，指標另進 team_inputs 2.6 供 Finalizer 重算比對（沒有財報時為 None）。
        self.fundamental_metrics = metrics
        if metrics is not None:
            self.save_artifact("fundamental_metrics", metrics)
        batches = universe_batches([row["symbol"] for row in bundle["snapshot"]["latest_prices"]])
        reports: Dict[str, Dict[str, object]] = {}
        availability = {}
        if database_path is not None:
            from etf_agent.data.analyst_availability import analyst_availability
            inventory = analyst_availability(database_path, [row["symbol"] for row in bundle["snapshot"]["latest_prices"]], str(bundle["decision_cutoff"]))
            self.save_artifact("analyst_data_availability", inventory)
            availability = {row["symbol"]: [{key: value for key, value in channel.items() if key != "candidate_references"}
                                          for channel in row["channels"]] for row in inventory["symbols"]}
        plans: Dict[str, Tuple[Dict[str, object], List[Dict[str, object]], List[Dict[str, object]]]] = {}
        jobs: List[Tuple[str, Callable[[], Dict[str, object]]]] = []
        dip_view = analyst_dip_view(bundle, dip_gate, metrics) if dip_gate is not None else None
        for analyst in LLM_ANALYSTS:
            brief = build_analyst_brief(
                bundle, momentum, analyst, metrics, event_market_scope=analyst == "event",
                technical_gate=self.technical_gate and analyst == "technical",
                compact_technical=analyst == "technical",
            )
            if dip_view is not None and analyst in ("technical", "fundamental"):
                brief = _with_dip_view(brief, dip_view, analyst)
            if valuation_facts is not None and analyst == "fundamental":
                brief = _with_valuation_view(brief, valuation_facts)
            if "technical_gate" in brief:
                self.log("  [技術閘門] 趨勢一致 %d 檔只附 momentum，其餘 %d 檔附完整歷史" % (
                    len(brief["technical_gate"]["momentum_only_symbols"]), brief["technical_gate"]["full_history_symbols"]))
            if analyst == "event":
                for row in brief["symbols"]:
                    row["data_availability"] = availability.get(row["symbol"], [{"status": "本次未查資料庫候選狀態，不能宣稱來源沒有資料"}])
            analyst_batches = batches
            program_items: List[Dict[str, object]] = []
            if analyst == "event":
                # 查詢視窗內沒有重大訊息的股票，結果只可能是 unknown（Validator 強制），由程式直接產出，不送 Agent。
                # 有核准個股新聞時，只有新聞的股票也交給 Agent；新聞與重大訊息一起計入批次則數。
                news_checked = "company_news_input" in bundle
                program_items = [no_event_item(row["symbol"], row["data_availability"], news_checked)
                                 for row in brief["symbols"] if not row["events"] and not row.get("news")]
                skipped = {item["symbol"] for item in program_items}
                self.log("  [事件] %d 檔無重大訊息%s，由程式輸出 unknown，其餘 %d 檔交給 Agent（個股新聞 %d 則）" % (
                    len(skipped), "或新聞" if news_checked else "", len(brief["symbols"]) - len(skipped),
                    sum(len(row.get("news", [])) for row in brief["symbols"])))
                agent_symbols = [row["symbol"] for row in brief["symbols"] if row["symbol"] not in skipped]
                reused = self._reusable_event_items(bundle, brief, agent_symbols, event_direction) if self.event_reuse else []
                if reused:
                    reused_symbols = {item["symbol"] for item in reused}
                    program_items = [*program_items, *reused]  # 沿用的項目與程式產出的 unknown 一起當作不送 Agent 的偽批次
                    skipped = skipped | reused_symbols
                analyst_batches = weighted_batches(
                    [row["symbol"] for row in brief["symbols"] if row["symbol"] not in skipped],
                    {row["symbol"]: len(row["events"]) + len(row.get("news", [])) for row in brief["symbols"]},
                    EVENT_BATCH_SIZE, EVENT_BATCH_MAX_EVENTS)
            plans[analyst] = (brief, analyst_batches, program_items)
            jobs.extend(self._analyst_batch_jobs(bundle, analyst, brief, analyst_batches, event_direction))
        partials = self._run_jobs(jobs)
        cursor = 0
        for analyst in LLM_ANALYSTS:
            brief, analyst_batches, program_items = plans[analyst]
            outputs = [partial["items"] for partial in partials[cursor:cursor + len(analyst_batches)]]
            cursor += len(analyst_batches)
            if program_items:
                analyst_batches = [*analyst_batches, {"batch_index": len(analyst_batches),
                                                      "symbols": [item["symbol"] for item in program_items]}]
                outputs.append(program_items)
            combined = merge_batch_items(analyst_batches, outputs, analyst)
            report = (seal_event_market_report(bundle, brief["report_envelope"], combined)
                      if analyst == "event" else seal_report(brief["report_envelope"], combined))
            errors = AnalystReportValidator(bundle, analyst, require_event_direction=event_direction and analyst == "event").validate(report)
            if errors:
                raise DailyPipelineError("%s 分析報告合併後驗證失敗：%s" % (analyst, "；".join(errors[:10])))
            reports[analyst] = report
            self.save_artifact("analyst_%s" % analyst, report)
        return reports

    def _reusable_event_items(
        self, bundle: Mapping[str, object], brief: Mapping[str, object], agent_symbols: Sequence[str], event_direction: bool,
    ) -> List[Dict[str, object]]:
        """同一交易日前一次 run 中輸入逐檔完全相同的事件分析項目；以本次 bundle 與 Validator 重驗，驗不過整批不重用。"""
        source, items = reusable_event_items(self.run_dir, {row["symbol"]: row for row in brief["symbols"]}, agent_symbols)
        if not items:
            return []
        report = seal_event_market_report(bundle, brief["report_envelope"], items)
        errors = AnalystReportValidator(
            bundle, "event", symbols=sorted(item["symbol"] for item in items), require_event_direction=event_direction,
        ).validate(report)
        if errors:
            self.log("  [事件重用] 前一次 %s 的結果在本次資料上驗證失敗（%s），全部重新交給 Agent" % (source, "；".join(errors[:2])))
            return []
        self.log("  [事件重用] 沿用 %s 的事件分析 %d 檔（輸入逐檔完全相同、已用本次資料重驗），其餘交給 Agent" % (source, len(items)))
        self.save_artifact("event_reuse", {"source_run": source, "symbols": sorted(item["symbol"] for item in items)})
        return items

    def _analyst_batch_jobs(
        self, bundle: Mapping[str, object], analyst: str, brief: Dict[str, object],
        analyst_batches: Sequence[Mapping[str, object]], event_direction: bool = False,
    ) -> List[Tuple[str, Callable[[], Dict[str, object]]]]:
        """每批一個任務；任務各自綁定本批的 brief、Validator 與提示，可安全同時執行。"""
        typed = evidence_policy(bundle) is not None
        dip_rules = (
            _DIP_ANALYST_RULES % ("market_attribution 與 price_position" if analyst == "technical" else "fundamental_flags")
            if "dip_facts_background" in brief else ""
        ) + (_VALUATION_ANALYST_RULES if "valuation_background" in brief else "")

        def make_job(batch: Mapping[str, object]) -> Tuple[str, Callable[[], Dict[str, object]]]:
            members = set(batch["symbols"])
            batch_brief = {
                **brief,
                "batch": {key: batch[key] for key in ("batch_index", "batch_count", "symbols")},
                "symbols": [entry for entry in brief["symbols"] if entry["symbol"] in members],
            }
            if "valuation_background" in brief:
                # 估值 V2：背景只附本批股票的位置（其餘欄位不變）。
                batch_brief["valuation_background"] = {
                    **brief["valuation_background"],
                    "by_symbol": {key: value for key, value in brief["valuation_background"]["by_symbol"].items() if key in members},
                }
            if "technical_gate" in brief:
                gated = [symbol for symbol in brief["technical_gate"]["momentum_only_symbols"] if symbol in members]
                batch_brief["technical_gate"] = {
                    **brief["technical_gate"],
                    "momentum_only_symbols": gated,
                    "full_history_symbols": len(members) - len(gated),
                }
            name = "%s_b%d" % (analyst, batch["batch_index"])
            brief_path = self.save_brief("brief_%s" % name, batch_brief)
            validator = AnalystReportValidator(
                bundle, analyst, symbols=batch["symbols"], require_event_direction=event_direction and analyst == "event",
            )
            task = AgentTask(
                name,
                "你是 $%s-analyst。先讀 %s，再讀本批輸入摘要 %s。只輸出本批 %d 檔股票（%s），每一檔都必須出現一次。%s"
                % (
                    analyst, self.root / ("tradeagent/analysis/%s/%s-analyst/SKILL.md" % (analyst, analyst)), brief_path,
                    len(batch["symbols"]), "、".join(batch["symbols"]),
                    _SHARED_RULES + (_EVENT_RULES if analyst == "event" else "") + (_FINDING_KIND_RULES if typed else "")
                    + dip_rules + (_EVENT_DIRECTION_RULES if event_direction and analyst == "event" else ""),
                ),
                event_analyst_schema(typed, event_direction) if analyst == "event" else analyst_schema(typed),
            )
            return name, lambda: self.agent_step(
                task,
                lambda output: seal_event_market_report(bundle, brief["report_envelope"], output["items"])
                if analyst == "event" else seal_report(brief["report_envelope"], output["items"]),
                validator.validate,
            )

        return [make_job(batch) for batch in analyst_batches]

    # ------------------------------------------------------------------ research team
    def run_research_team(
        self,
        bundle: Mapping[str, object],
        momentum: Mapping[str, object],
        analyst_reports: Mapping[str, Mapping[str, object]],
        research_result: Mapping[str, object],
        run_id: str,
    ) -> Dict[str, object]:
        """多頭與空頭研究員各自逐批對全部股票表態；兩方讀同一輸入、互相隔離。"""
        batches = universe_batches([row["symbol"] for row in bundle["snapshot"]["latest_prices"]])
        briefs = {role: build_stance_brief(bundle, momentum, analyst_reports, research_result, role) for role in STANCE_ROLES}
        jobs: List[Tuple[str, Callable[[], Dict[str, object]]]] = []
        for role in STANCE_ROLES:
            jobs.extend(self._stance_batch_jobs(bundle, momentum, analyst_reports, research_result, role, briefs[role], batches))
        # 執行器不能強制拒讀（Codex）時，多空原始輸出暫存到兩方全部完成才落地（時序隔離）；Claude／OpenCode 行為不變。
        with self._staged_research_outputs([name for name, _ in jobs]):
            partials = self._run_jobs(jobs)
        packets: Dict[str, Dict[str, object]] = {}
        for index, role in enumerate(STANCE_ROLES):
            outputs = [partial["items"] for partial in partials[index * len(batches):(index + 1) * len(batches)]]
            packet = seal_stance_packet(briefs[role]["packet_envelope"], merge_batch_items(batches, outputs, role))
            packets[role] = packet
            self.save_artifact("%s_stance" % role, packet)
        debate = build_research_debate(bundle, packets["bull"], packets["bear"], "research-debate:%s" % run_id)
        errors = ResearchDebateBundleValidator(bundle, momentum, analyst_reports, research_result).validate(debate)
        if errors:
            raise DailyPipelineError("ResearchDebateBundle 驗證失敗：" + "；".join(errors[:10]))
        self.save_artifact("research_debate", debate)
        return debate

    def _stance_batch_jobs(
        self, bundle: Mapping[str, object], momentum: Mapping[str, object],
        analyst_reports: Mapping[str, Mapping[str, object]], research_result: Mapping[str, object],
        role: str, brief: Dict[str, object], batches: Sequence[Mapping[str, object]],
    ) -> List[Tuple[str, Callable[[], Dict[str, object]]]]:
        """多頭或空頭每批一個任務；兩方只讀同一份共同輸入，任務之間不共享輸出。"""
        typed = evidence_policy(bundle) is not None

        def make_job(batch: Mapping[str, object]) -> Tuple[str, Callable[[], Dict[str, object]]]:
            members = set(batch["symbols"])
            name = "%s_b%d" % (role, batch["batch_index"])
            brief_path = self.save_brief(
                "brief_%s" % name,
                {**brief, "batch": {key: batch[key] for key in ("batch_index", "batch_count", "symbols")},
                 "symbols": [entry for entry in brief["symbols"] if entry["symbol"] in members]},
            )
            validator = StancePacketValidator(
                bundle, momentum, analyst_reports, research_result, role, symbols=batch["symbols"]
            )
            task = AgentTask(
                name,
                "你是 $%s-researcher。先讀 %s 及其中連結的 references/data-rules.md，再讀本批輸入摘要 %s。"
                "公司事件在逐檔資料中，全市場情緒在摘要頂層只提供一次；不能用市場氣氛替代個股依據。"
                "對本批 %d 檔股票（%s）逐檔提出%s，說明支持依據、論點強度與需要重新評估的情況。"
                "每一檔剛好交付一次，找不到充分理由時明示沒有充分論點；交付欄位與引用依參考文件。"
                "不得讀取或推測另一方研究員的輸出。%s"
                % (
                    role, self.root / ("tradeagent/research/stock-research/%s-researcher/SKILL.md" % role), brief_path,
                    len(batch["symbols"]), "、".join(batch["symbols"]),
                    "最強的做多（值得持有或買進）論點" if role == "bull" else "最強的反對（不宜持有或應避開）論點",
                    _SHARED_RULES + (_CLAIM_KIND_RULES if typed else ""),
                ),
                stance_schema(typed),
                # 多空互相隔離由讀檔權限強制：不得讀另一方的輸出、原始輸出與輸入摘要。
                deny_reads=_peer_deny_reads(role),
            )
            return name, lambda: self.agent_step(
                task, lambda output: seal_stance_packet(brief["packet_envelope"], output["items"]), validator.validate
            )

        return [make_job(batch) for batch in batches]

    # ------------------------------------------------------------------ trader
    def load_prior_decision_context(
        self, bundle: Mapping[str, object], applied_run_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, object]:
        """前次決策背景：從決策庫找同帳戶、**目標交易時段更早**、approved／no_trade 的封存 run（同一時段有多個時優先
        帳本結算記錄的 ``applied_run_ids``，否則取該時段 cutoff 最新者），整理目前持股的前次意圖與重估條件；找不到或本次
        沒有目標時段時 unavailable。續跑時沿用本 run 已存且仍通過驗證的版本，避免決策庫變動改變輸入。"""
        path = self._path("prior_decision_context")
        validator = PriorDecisionContextValidator(bundle, self.decision_repository)
        if self.resume and path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not validator.validate(saved):
                self.log("  [前次決策] 沿用已驗證的 prior_decision_context.json")
                return saved
        if not ((bundle.get("trading_status_bundle") or {}).get("target_session") or {}).get("start"):
            found, prior, reason = None, None, NO_SESSION_REASON
        else:
            found = find_prior_decision_run(self.decision_repository, bundle, applied_run_ids)
            prior = read_decision_run(self.decision_repository, found["run_id"]) if found else None
            reason = None
        context = build_prior_decision_context(
            bundle, prior, reason=reason, selection=found["selection"] if found else "latest_cutoff")
        errors = validator.validate(context)
        if errors:
            raise DailyPipelineError("PriorDecisionContext 驗證失敗：" + "；".join(errors[:10]))
        self.save_artifact("prior_decision_context", context)
        if context["status"] == "available":
            self.log("  [前次決策] 來源 %s（時段 %s，cutoff %s，%s，挑法 %s），持股 %d 檔有前次意圖、%d 檔沒有；只作背景、不可引用" % (
                found["run_id"], context["source"]["target_session_start"], context["source"]["decision_cutoff"],
                "跨 Snapshot 例外" if context["cross_snapshot_exception"] else "同 Snapshot", found["selection"],
                len(context["items"]), len(context["uncovered_symbols"])))
        else:
            self.log("  [前次決策] 無可用的前次 Decision run（%s），背景為 unavailable" % context["reason"])
        return context

    def run_trader(
        self,
        bundle: Mapping[str, object],
        momentum: Mapping[str, object],
        analyst_reports: Mapping[str, Mapping[str, object]],
        debate: Mapping[str, object],
        run_id: str,
        prior_context: Optional[Mapping[str, object]] = None,
        portfolio_slots: Optional[Mapping[str, object]] = None,
        position_review: Optional[Mapping[str, object]] = None,
        exposure_review: Optional[Mapping[str, object]] = None,
        dip_facts: Optional[Mapping[str, object]] = None,
        held_weight_facts: Optional[Mapping[str, object]] = None,
        reentry_hint_symbols: Optional[Sequence[str]] = None,
        dip_assessment: bool = False,
        valuation_facts: Optional[Mapping[str, object]] = None,
    ) -> Dict[str, object]:
        """交易 Agent 逐批整合多空研究，說明論點取捨後決定意圖與 buy／add 信心。

        ``prior_context`` 是 ``load_prior_decision_context`` 的結果：只放進 brief 當背景，Validator 禁止引用。
        ``portfolio_slots`` 是 ``build_portfolio_slots`` 算好的名額脈絡（啟用 rotation 才有）：讓交易 Agent
        知道滿檔時買新股必須有持股出場。名額與配對仍由程式決定，Agent 不輸出任何數字。
        ``position_review`` 是 ``build_position_review`` 算好的持股檢視（policy 有 ``position_review`` 才有）：
        警示 ``review_id`` 必須和 claim 一樣處理一次，停損／停利的判斷仍由 Agent 做，數字由程式算。
        ``exposure_review`` 是 ``build_exposure_review`` 算好的曝險檢視（policy 有 ``exposure_rotation`` 才有）：每檔持股的 beta 與捕捉
        當背景事實附在 brief（不可引用），並讓 hold 也必填 conviction；換股由程式依帳本與 beta 決定，Agent 不輸出任何數字。
        ``dip_assessment``（position_sizing.quality_dip_assessment 閘門，需 ``dip_facts``）為 True 時，buy／add 另填列舉 dip_assessment
        （紀錄用）；False 時 Schema 與驗證逐位元不變。
        ``valuation_facts``（position_sizing.valuation_facts，估值 V2）有時每批簡報多附本批股票的 ``valuation_background``（citable=false），
        文字欄位出現其識別碼即拒絕；沒有時簡報與驗證逐鍵不變。"""
        envelope = trade_decision_envelope(bundle, debate, "trade-decision:%s" % run_id)
        background, prior_by_symbol = prior_decision_brief(prior_context) if prior_context is not None else (None, {})
        held = {str(item["symbol"]).upper(): item.get("shares") for item in bundle["account_snapshot"].get("positions", [])}
        momentum_status = {str(item.get("symbol", "")).upper(): item.get("status") for item in momentum.get("items", [])}
        tradability = {
            str(item.get("symbol", "")).upper(): item.get("state")
            for item in (bundle.get("tradability_assessment") or {}).get("symbols", [])
        }
        stances = {
            packet["role"]: {str(item["symbol"]).upper(): item for item in packet["items"]}
            for packet in debate["packets"]
        }
        outlooks = {
            name: {str(item["symbol"]).upper(): item.get("outlook") for item in report.get("items", [])}
            for name, report in analyst_reports.items()
        }
        review_brief = position_review_brief(position_review) if position_review is not None else {}
        exposure_brief = exposure_review_brief(exposure_review) if exposure_review is not None else {}
        # quality_dip_facts：整份事實以 dip_background 附上（citable=false、沒有 evidence_id）；沒有閘門時簡報逐鍵不變。
        dip_brief = dip_background_brief(dip_facts) if dip_facts is not None else {}
        # held_weight_budget v2：持股權重事實附在簡報（citable=false）；沒有閘門時簡報逐鍵不變。
        weight_brief = held_weight_brief(held_weight_facts) if held_weight_facts is not None else {}
        # reentry_cooldown v3：未持有且近期出場的股票各附一個 hint_id 提示（買回須回應）；沒有 v3 時簡報逐鍵不變。
        hint_symbols = sorted({str(s).upper() for s in (reentry_hint_symbols or ())} - set(held))
        hold_conviction = exposure_review is not None
        lessons_background = lessons_brief(load_strategy_lessons(self.root, bundle["decision_cutoff"]))  # 反思整理的做法建議；只是背景，沒有可引用的 ID
        thesis_verdicts = load_thesis_verdicts(self.root, bundle["decision_cutoff"])  # 每週論點檢視；只是背景，沒有可引用的 ID
        batches = universe_batches([row["symbol"] for row in bundle["snapshot"]["latest_prices"]])
        def make_job(batch: Mapping[str, object]) -> Tuple[str, Callable[[], Dict[str, object]]]:
            name = "trader_b%d" % batch["batch_index"]
            thesis_background = verdicts_brief(thesis_verdicts, set(held) & set(batch["symbols"]))
            brief_path = self.save_brief(
                "brief_%s" % name,
                {
                    "regime_assessment": momentum.get("regime_assessment"),
                    "account": {
                        "cash": bundle["account_snapshot"].get("cash"),
                        "nav": bundle["account_snapshot"].get("nav"),
                        "held_symbols": sorted(held),
                        "position_count": len(held),
                    },
                    "rules": {key: bundle["rules"].get(key) for key in ("min_positions", "max_positions", "max_stock_weight", "cash_weight_must_be_below")},
                    **({"portfolio_slots": portfolio_slots} if portfolio_slots is not None else {}),
                    **({"position_review_background": {key: review_brief[key] for key in ("policy", "note")}}
                       if position_review is not None else {}),
                    **({"exposure_background": exposure_brief["background"]} if exposure_review is not None else {}),
                    **({"dip_background": dip_brief["background"]} if dip_facts is not None else {}),
                    **({"held_weight_background": weight_brief["background"]} if held_weight_facts is not None else {}),
                    **({"valuation_background": valuation_background_brief(valuation_facts, batch["symbols"])}
                       if valuation_facts is not None else {}),
                    **({"prior_decision_background": background} if background is not None else {}),
                    **({"strategy_lessons": lessons_background} if lessons_background is not None else {}),
                    **({"thesis_review_background": thesis_background} if thesis_background is not None else {}),
                    "batch": {key: batch[key] for key in ("batch_index", "batch_count", "symbols")},
                    "symbols": [
                        {
                            "symbol": symbol,
                            "held_shares": held.get(symbol, 0),
                            "momentum_status": momentum_status.get(symbol),
                            "tradability_state": tradability.get(symbol),
                            "analyst_outlooks": {analyst: values.get(symbol) for analyst, values in outlooks.items()},
                            "bull": {key: stances["bull"][symbol][key] for key in ("strength", "claims", "invalidation_conditions")},
                            "bear": {key: stances["bear"][symbol][key] for key in ("strength", "claims", "invalidation_conditions")},
                            **({"prior_decision": prior_by_symbol.get(symbol)} if background is not None else {}),
                            **({"position_review": review_brief["by_symbol"][symbol]}
                               if symbol in review_brief.get("by_symbol", {}) else {}),
                            **({"exposure": exposure_brief["by_symbol"][symbol]}
                               if symbol in exposure_brief.get("by_symbol", {}) else {}),
                            **({"dip": dip_brief["by_symbol"][symbol]}
                               if symbol in dip_brief.get("by_symbol", {}) else {}),
                            **({"held_weight": weight_brief["by_symbol"][symbol]}
                               if symbol in weight_brief.get("by_symbol", {}) else {}),
                            **({"reentry_cooldown": {"hint_id": reentry_hint_id(symbol), "citable": False}}
                               if symbol in hint_symbols else {}),
                        }
                        for symbol in batch["symbols"]
                    ],
                },
            )
            validator = TradeDecisionValidator(bundle, momentum, debate, symbols=batch["symbols"], prior_context=prior_context, position_review=position_review, hold_conviction=hold_conviction, dip_facts=dip_facts, reentry_hint_symbols=reentry_hint_symbols, dip_assessment=dip_assessment, valuation_facts=valuation_facts)
            task = AgentTask(
                name,
                "你是 $trader 交易 Agent，負責整合多頭與空頭兩位研究員的結果。先讀 %s 及其中連結的 references/data-rules.md，"
                "再讀本批輸入 %s。對本批 %d 檔股票（%s）逐檔整合兩方論點、記錄每個論點的採納或否決，"
                "再依持股與交易條件形成單一交易意圖；整合方式、理由寫法、欄位與限制都依參考文件。"
                "交件前核對：items 的股票數必須等於本批 %d 檔、逐檔都有裁決，漏一檔整批會被退回重做。%s%s%s%s%s%s%s%s%s%s"
                % (
                    self.root / "tradeagent/trading/trader/SKILL.md", brief_path,
                    len(batch["symbols"]), "、".join(batch["symbols"]), len(batch["symbols"]),
                    _PRIOR_RULES if background is not None else "",
                    _ROTATION_RULES if portfolio_slots is not None else "",
                    _REVIEW_RULES if position_review is not None else "",
                    _EXPOSURE_RULES if exposure_review is not None else "",
                    _DIP_TRADER_RULES if dip_facts is not None else "",
                    _DIP_ASSESSMENT_RULES if dip_assessment else "",
                    _HELD_WEIGHT_RULES if held_weight_facts is not None else "",
                    _COOLDOWN_RULES if set(hint_symbols) & set(batch["symbols"]) else "",
                    _VALUATION_TRADER_RULES if valuation_facts is not None else "",
                    _SHARED_RULES,
                ),
                trade_schema_with_dip_assessment() if dip_assessment else TRADE_SCHEMA,
            )
            return name, lambda: self.agent_step(
                task, lambda output: seal_trade_decision(envelope, output["items"]), validator.validate
            )

        # 各批只讀共同的帳戶、多空與動能輸入，不讀其他批的輸出，可同時執行。
        partials = self._run_jobs([make_job(batch) for batch in batches])
        outputs: List[List[Dict[str, object]]] = [partial["items"] for partial in partials]
        decision = seal_trade_decision(envelope, merge_batch_items(batches, outputs, "trader"))
        errors = TradeDecisionValidator(
            bundle, momentum, debate, prior_context=prior_context, position_review=position_review,
            hold_conviction=hold_conviction, dip_facts=dip_facts, reentry_hint_symbols=reentry_hint_symbols,
            dip_assessment=dip_assessment, valuation_facts=valuation_facts,
        ).validate(decision)
        if errors:
            raise DailyPipelineError("TradeDecision 合併後驗證失敗：" + "；".join(errors[:10]))
        self.save_artifact("trade_decision", decision)
        (self.run_dir / "trader_report.md").write_text(
            render_trader_report(
                bundle, momentum, debate, decision, position_review, hold_conviction, reentry_hint_symbols=reentry_hint_symbols,
                dip_facts=dip_facts, dip_assessment=dip_assessment,
            ), encoding="utf-8"
        )
        return decision

    def run_selection_ranking(
        self,
        bundle: Mapping[str, object],
        momentum: Mapping[str, object],
        analyst_reports: Mapping[str, Mapping[str, object]],
        debate: Mapping[str, object],
        trade_decision: Mapping[str, object],
        run_id: str,
    ) -> Dict[str, object]:
        """排序 Agent 一次看全部 buy／add 候選，只在同信心等級內排先後並附理由；不改信心、不新增事實。

        沒有任何等級有兩檔以上候選時沒有先後可排，由程式直接封存，不呼叫 Agent。"""
        envelope = ranking_envelope(bundle, trade_decision, "selection-ranking:%s" % run_id)
        if not ranking_needs_agent(trade_decision):
            ranking = program_ranking(envelope, trade_decision)
        else:
            brief_path = self.save_brief(
                "brief_selection_ranking",
                {
                    "regime_assessment": momentum.get("regime_assessment"),
                    "candidates": selection_ranking_brief(trade_decision, debate, analyst_reports, momentum),
                    "account": {"held_symbols": sorted(
                        str(item["symbol"]).upper() for item in bundle["account_snapshot"].get("positions", [])
                    )},
                },
            )
            validator = SelectionRankingValidator(bundle, trade_decision)
            task = AgentTask(
                "ranker",
                "你是 $trader 交易 Agent，現在做**第二輪入選排序**。先讀 %s，再讀全部 buy／add 候選 %s。"
                "第一輪已給每檔信心等級；你只在**同一信心等級內**決定先後，把你最想優先入選的放在陣列最前面，"
                "每一檔都附一句白話理由；每個信心等級必須剛好列出該等級的全部候選，不多不少。%s"
                % (self.root / "tradeagent/trading/trader/references/ranking.md", brief_path, _SHARED_RULES),
                RANKING_SCHEMA,
            )
            ranking = self.agent_step(
                task, lambda output: seal_selection_ranking(envelope, output["tiers"]), validator.validate
            )
        errors = SelectionRankingValidator(bundle, trade_decision).validate(ranking)
        if errors:
            raise DailyPipelineError("SelectionRanking 驗證失敗：" + "；".join(errors[:10]))
        self.save_artifact("selection_ranking", ranking)
        return ranking

    def run_swap_nomination(
        self,
        bundle: Mapping[str, object],
        policy: Mapping[str, object],
        trade_decision: Mapping[str, object],
        ranking: Mapping[str, object],
        debate: Mapping[str, object],
        position_review: Mapping[str, object],
        exposure_review: Mapping[str, object],
        prior_context: Optional[Mapping[str, object]],
        run_id: str,
        exposure_facts: Optional[Mapping[str, object]] = None,
        turn_state: Optional[Mapping[str, object]] = None,
        dip_facts: Optional[Mapping[str, object]] = None,
    ) -> Dict[str, object]:
        """5c 換股提名（agent_swap 閘門）：交易 Agent 在入選排序之後，提名「用哪檔新股換掉哪檔持股」的有序清單。

        ``policy`` 是 5c 時的基礎 Policy（reentry_cooldown、持有天數與論點提示已綁入）。程式判定不需要時直接封存 ``not_needed``、
        不呼叫 Agent；驗證最多三次嘗試，用盡封存 ``exhausted``、暫時性失敗重試後仍失敗封存 ``unavailable``（空清單，其餘決策照常）；
        安全拒絕仍整個停止。v1（影子）與 v2 的簡報、提示與 Skill 完全相同，這裡不讀 ``agent_swap.version``。"""
        envelope = nomination_envelope(bundle, trade_decision, ranking, "swap-nomination:%s" % run_id)
        # agent_swap.exposure（版本閘門，Agent 主導曝險階段 2、3）：每對另有 purpose、輸出另有 lower_conviction_review；
        # 簡報與驗證另需 exposure_facts。沒有閘門時 Schema、簡報與封存逐位元不變。
        exposure_swap = exposure_swap_gate(policy.get("position_sizing")) is not None
        if exposure_swap and exposure_facts is None:
            raise DailyPipelineError("policy 啟用 agent_swap.exposure 時必須有 exposure_facts（position_sizing.exposure_view）")
        # agent_swap.quality_dip（優質股第三階段，版本閘門）：purpose 多 quality_dip，簡報與驗證另需 dip_facts；沒有閘門時完全不讀。
        quality_swap = quality_dip_swap_gate(policy.get("position_sizing")) is not None
        if quality_swap and dip_facts is None:
            raise DailyPipelineError("policy 啟用 agent_swap.quality_dip 時必須有 dip_facts（position_sizing.quality_dip_facts）")
        quality_dip = dip_facts if quality_swap else None
        try:
            facts = nomination_facts(
                bundle, policy, trade_decision, ranking, debate, position_review, exposure_review, exposure_facts, quality_dip)
        except (DecisionToolError, KeyError, TypeError, ValueError) as error:
            raise DailyPipelineError("換股提名無法由共同輸入建立事實：%s" % error) from error
        validator = SwapNominationValidator(
            bundle, policy, trade_decision, ranking, debate, position_review, exposure_review, prior_context=prior_context,
            exposure_facts=exposure_facts, dip_facts=quality_dip,
        )
        if not needs_nomination(facts):
            nomination = program_nomination(envelope, STATUS_NOT_NEEDED, exposure=exposure_swap)
        else:
            try:
                brief = swap_nomination_brief(
                    bundle, policy, trade_decision, ranking, debate, position_review, exposure_review,
                    load_thesis_verdicts(self.root, bundle["decision_cutoff"]), exposure_facts if exposure_swap else None,
                    quality_dip,
                )
                if exposure_facts is not None:
                    # exposure_view（版本閘門，Agent 主導曝險階段 1）：附轉向、加權指數、組合曝險、上漲日報酬與新股 beta 事實
                    # （背景、不可引用）；不改提名的驗證規則。沒有閘門時簡報與提示逐鍵不變。
                    brief = dict(brief)
                    brief[EXPOSURE_FACTS_KEY] = nomination_exposure_brief(
                        exposure_facts, [str(row["symbol"]) for row in brief["candidates"]],
                        turn_facts(bundle), market_index_facts(bundle), turn_state, exposure_swap,
                    )
            except (DecisionToolError, KeyError, TypeError, ValueError) as error:
                raise DailyPipelineError("換股提名簡報建立失敗：%s" % error) from error
            brief_path = self.save_brief("brief_swap_nomination", brief)
            task = AgentTask(
                "swap_nominator",
                "你是 $trader 交易 Agent，現在做**第三步：換股提名**。先讀 %s，再讀 %s。"
                "第一輪與入選排序已完成；你只決定「若需要名額，依序用哪檔新股換掉哪檔持股」，輸出有先後的配對清單，"
                "每一對引用簡報列出的依據識別碼並附白話理由；沒有合適的配對就輸出空清單並說明原因。"
                "簡報的 thesis_hints 每檔都必須在 thesis_hint_responses 回應一次。%s"
                % (self.root / "tradeagent/trading/trader/references/swap-nomination.md", brief_path, _SHARED_RULES)
                + (NOMINATION_EXPOSURE_PROMPT if exposure_facts is not None else "")
                + (NOMINATION_PURPOSE_PROMPT if exposure_swap else "")
                + (NOMINATION_QUALITY_DIP_PROMPT if quality_swap else ""),
                SWAP_NOMINATION_QUALITY_DIP_SCHEMA if quality_swap else (
                    SWAP_NOMINATION_EXPOSURE_SCHEMA if exposure_swap else SWAP_NOMINATION_SCHEMA),
            )
            fingerprint = canonical_sha256({"envelope": envelope, "brief": brief})
            outcome = self.agent_step_degradable(
                task, lambda output: seal_swap_nomination(envelope, output, exposure_swap), validator.validate, fingerprint,
            )
            if outcome.artifact is not None:
                nomination = outcome.artifact
            elif outcome.status in (STATUS_EXHAUSTED, STATUS_UNAVAILABLE):
                nomination = program_nomination(envelope, outcome.status, outcome.errors, exposure_swap)
            else:
                raise DailyPipelineError("換股提名未預期的步驟狀態：%s" % outcome.status)
        errors = validator.validate(nomination)
        if errors:
            raise DailyPipelineError("SwapNomination 驗證失敗：" + "；".join(errors[:10]))
        self.save_artifact("swap_nomination", nomination)
        return nomination

    def run_release_order(
        self,
        bundle: Mapping[str, object],
        momentum: Mapping[str, object],
        trade_decision: Mapping[str, object],
        ranking: Mapping[str, object],
        debate: Mapping[str, object],
        position_review: Mapping[str, object],
        prior_context: Optional[Mapping[str, object]],
        run_id: str,
    ) -> Dict[str, object]:
        """5d 回撤減碼釋出順序（drawdown_derisk v5）：交易 Agent 在入選排序之後，對可減碼持股給「若需要降低持股，先放掉誰」的順序。

        可減碼持股少於兩檔沒有先後可排，程式直接封存 ``not_needed``、不呼叫 Agent；驗證最多三次嘗試，用盡封存 ``exhausted``、
        暫時性失敗重試後仍失敗封存 ``unavailable``（空順序，配置退回 v4 的 ATR 順序並留旗標，其餘決策照常）；安全拒絕仍整個停止。
        簡報不含風險 Agent 的姿態與現金缺口（此時尚未產生）。"""
        envelope = release_envelope(bundle, trade_decision, ranking, "release-order:%s" % run_id)
        try:
            facts = release_facts(bundle, trade_decision, debate, position_review)
        except (DecisionToolError, KeyError, TypeError, ValueError) as error:
            raise DailyPipelineError("釋出順序無法由共同輸入建立事實：%s" % error) from error
        validator = ReleaseOrderValidator(bundle, trade_decision, ranking, debate, position_review, prior_context=prior_context)
        if not needs_release_order(facts):
            release = program_release_order(envelope, RELEASE_NOT_NEEDED)
        else:
            try:
                brief = release_order_brief(bundle, trade_decision, debate, position_review, momentum)
            except (DecisionToolError, KeyError, TypeError, ValueError) as error:
                raise DailyPipelineError("釋出順序簡報建立失敗：%s" % error) from error
            brief_path = self.save_brief("brief_release_order", brief)
            task = AgentTask(
                "release_order",
                "你是 $trader 交易 Agent，現在做**第四步：回撤減碼的釋出順序**。先讀 %s，再讀 %s。"
                "第一輪與入選排序已完成；你只決定「若之後需要降低持股，先放掉誰」，輸出涵蓋簡報全部持股、每檔恰好一次的先後順序，"
                "最先願意放掉的放最前面；每檔引用簡報列出的該股自己的依據識別碼並附白話理由。%s"
                % (self.root / "tradeagent/trading/trader/references/release-order.md", brief_path, _SHARED_RULES),
                RELEASE_ORDER_SCHEMA,
            )
            fingerprint = canonical_sha256({"envelope": envelope, "brief": brief})
            outcome = self.agent_step_degradable(
                task, lambda output: seal_release_order(envelope, output), validator.validate, fingerprint,
            )
            if outcome.artifact is not None:
                release = outcome.artifact
            elif outcome.status in (RELEASE_EXHAUSTED, RELEASE_UNAVAILABLE):
                release = program_release_order(envelope, outcome.status, outcome.errors)
            else:
                raise DailyPipelineError("釋出順序未預期的步驟狀態：%s" % outcome.status)
        errors = validator.validate(release)
        if errors:
            raise DailyPipelineError("ReleaseOrder 驗證失敗：" + "；".join(errors[:10]))
        self.save_artifact("release_order", release)
        return release


NOMINATION_EXPOSURE_PROMPT = (
    "簡報含 exposure_facts（程式算的轉向、加權指數、組合曝險、產業權重、上漲日報酬與新股 beta 事實）：全部是背景、沒有 evidence_id、"
    "不可放進依據，也不改變上面的提名規則，用法見 swap-nomination.md「exposure_facts」節。"
)


NOMINATION_PURPOSE_PROMPT = (
    "每一對另填列舉 purpose：thesis＝以換出股自己已採納的空頭論點或持股檢視事實讓位；exposure＝為了提高市場參與度讓位，換出依據必須含簡報該持股"
    "exposure_basis 的曝險事實編號。另填 lower_conviction_review：沒有提名第一輪信心為 high 的持股做 exposure 配對時填空陣列，否則依簡報 "
    "exposure_swap 的規則逐檔交代 medium／low 持股；用法見 swap-nomination.md「purpose 與 lower_conviction_review」節。"
)


NOMINATION_QUALITY_DIP_PROMPT = (
    "purpose 另可填 quality_dip：換入者是簡報 quality_dip_nominable=true 的新股（被大盤拖累候選；超跌候選只有 quality_dip_swap.oversold_enabled "
    "為 true 時才可），buy_basis_ids 至少一個是 claim_kind 不是 price 的已採納多頭論點，換出者必須是 quality_dip_nominable=true 的持股，"
    "換出依據規則與 thesis 相同。候選標示與 dip_assessment 只是背景、不是依據；不符合時改用 thesis 或刪除該對。"
    "用法見 swap-nomination.md「purpose=quality_dip」節。"
)


_VALUATION_ANALYST_RULES = (
    "本批輸入含 valuation_background（程式算的 PER／PBR 在自身歷史區間的位置，三個視窗並列；citable=false、沒有 evidence_id，"
    "不可放進 evidence_ids，也不得寫便宜、低估或貴）。提到時必須寫明指標與視窗，且說明位置低可能來自盈餘下修、PER 下降可能來自盈餘上修；"
    "status 為 unavailable 只代表不知道。用法見 fundamental-analyst 的「估值位置」節。"
)
_VALUATION_TRADER_RULES = (
    "本批輸入含 valuation_background（程式算的 PER／PBR 在自身歷史區間的位置；citable=false、沒有 evidence_id）：只是背景、不是依據，"
    "不得寫便宜、低估或貴，文字欄位不得寫 valuation_background 或其識別碼；行動仍須由本次 claim 支持。用法見 data-rules.md「估值位置」節。"
)


def _with_valuation_view(brief: Mapping[str, object], facts: Mapping[str, object]) -> Dict[str, object]:
    """把估值位置接進基本面分析師簡報：頂層 ``valuation_background`` 放在 symbols 之前（批次切分時整份保留，只是背景）。"""
    output = {key: value for key, value in brief.items() if key != "symbols"}
    output["valuation_background"] = valuation_background_brief(facts, [row["symbol"] for row in brief["symbols"]])
    output["symbols"] = brief["symbols"]
    return output


def _with_dip_view(brief: Mapping[str, object], view: Mapping[str, object], analyst: str) -> Dict[str, object]:
    """把分析師階段的事實接進簡報：頂層 ``dip_facts_background`` 放在 symbols 之前，逐檔加該分析師的區塊。"""
    per_symbol = view[analyst]
    output = {key: value for key, value in brief.items() if key != "symbols"}
    output["dip_facts_background"] = view["background"]
    output["symbols"] = [{**row, **per_symbol.get(row["symbol"], {})} for row in brief["symbols"]]
    return output
