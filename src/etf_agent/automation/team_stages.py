"""每日決策鏈的團隊階段：分析師、多頭／空頭研究員與交易 Agent（逐批、可平行）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.decision import (
    AnalystReportValidator,
    PriorDecisionContextValidator,
    ResearchDebateBundleValidator,
    StancePacketValidator,
    TradeDecisionValidator,
    build_analyst_brief,
    build_prior_decision_context,
    build_research_debate,
    build_stance_brief,
    compute_fundamental_metrics,
    find_prior_decision_run,
    prior_decision_brief,
    read_decision_run,
    position_review_brief,
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
from etf_agent.decision.stance import STANCE_ROLES
from etf_agent.decision.strategy_lessons import lessons_brief, load_strategy_lessons

from .agent_runner import AgentTask, DailyPipelineError
from .agent_schemas import (
    ANALYST_SCHEMA,
    EVENT_ANALYST_SCHEMA,
    EVENT_BATCH_MAX_EVENTS,
    EVENT_BATCH_SIZE,
    STANCE_SCHEMA,
    RANKING_SCHEMA,
    TRADE_SCHEMA,
    _EVENT_RULES,
    _SHARED_RULES,
)
from .batching import merge_batch_items, universe_batches, weighted_batches


# 前次決策背景只給交易 Agent 核對一致性；它不是本次證據，Validator 會拒絕文字欄位出現其識別碼。
_PRIOR_RULES = (
    "輸入的 prior_decision_background 與各股 prior_decision 是前次已封存決策對目前持股的意圖、信心與重估條件，"
    "只供你檢查這次判斷是否與前次一致、前次寫的重估條件是否已由本次多空論點證實；它不是本次事實或證據，"
    "不得寫進 rationale、unresolved_questions 或 invalidation_conditions，也不得只因前次曾買進就續抱或加碼；"
    "延續或改變行動都必須由本次採納的 claim 支持。沒有附前次決策的股票，照一般方式判斷。"
)
_ROTATION_RULES = (
    "輸入的 portfolio_slots 是程式算好的名額狀態：held_positions 是目前持股檔數，max_positions 是硬上限，"
    "free_slots 是真正的空名額，held_by_bear_strength 與 unheld_by_bull_strength 是全池統計。"
    "free_slots 為 0（已滿檔）時，未持有股票的 buy 只有在全池有持股被標為 exit 或 trim 時才可能成交，"
    "由程式依信心等級配對，名額不足者一律不建立買單；你看不到其他批的候選，也不需要指定誰換誰。"
    "因此持股若空頭 claim 已經成立、多頭理由不再成立，應該給 exit 或 trim，而不是慣性 hold；"
    "但不得只為了騰出名額而出場：exit 與 trim 一律必須採納至少一個該股票的空頭 claim，"
    "也不得為了填滿名額而買進沒有多頭依據的股票。名額、權重、股數與現金全部由程式計算，"
    "portfolio_slots 沒有 evidence_id，不可寫進 rationale、unresolved_questions 或 invalidation_conditions。"
)
_REVIEW_RULES = (
    "輸入的 position_review_background 與各持股的 position_review 是程式算好的持股檢視（帳面報酬、近 5／20 日高點回落、警示，以及可能附上的相對交易池報酬 relative_to_pool_* 與最新月營收／財報可得時間 latest_*_available_at；這些是程式算的事實，不是證據，不可引用為 evidence_id），"
    "數字不可改寫。flags 內的 review_id 是程式警示：你必須把它放進 adopted_claim_ids 剛好一次，不可放進 rejected_claim_ids，"
    "也不得新增或改寫任何數字。警示只代表需要正視，不是賣出指令：請結合多空論點判斷是 hold、trim 還是 exit。"
    "loss_alert 的持股若要 hold、add 或 no_trade，必須同時採納至少一個多頭 claim，否則請 trim 或 exit；"
    "retrace_alert 代表從近期高點回落，可作為停利依據。trim 與 exit 可以採納警示取代空頭 claim。"
    "賺賠自帳戶成本起算，開帳日成本是當日結算收盤價，請參考 note；review_id 可寫進 adopted_claim_ids，"
    "但說明理由時請用白話描述虧損或回落，不要貼出識別碼。"
)


class TeamStagesMixin:
    """由 ``DailyDecisionPipeline`` 組合；依賴 ``PipelineBase`` 的 save_*、agent_step 與 _run_jobs。"""

    def run_analyst_team(
        self, bundle: Mapping[str, object], momentum: Mapping[str, object], database_path: Optional[Path] = None
    ) -> Dict[str, Dict[str, object]]:
        """三位分析師逐批執行；事件與市場情緒共用一位 Agent、一份報告。"""
        metrics = compute_fundamental_metrics(bundle)
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
        for analyst in LLM_ANALYSTS:
            brief = build_analyst_brief(
                bundle, momentum, analyst, metrics, event_market_scope=analyst == "event",
                technical_gate=self.technical_gate and analyst == "technical",
                compact_technical=analyst == "technical",
            )
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
                analyst_batches = weighted_batches(
                    [row["symbol"] for row in brief["symbols"] if row["symbol"] not in skipped],
                    {row["symbol"]: len(row["events"]) + len(row.get("news", [])) for row in brief["symbols"]},
                    EVENT_BATCH_SIZE, EVENT_BATCH_MAX_EVENTS)
            plans[analyst] = (brief, analyst_batches, program_items)
            jobs.extend(self._analyst_batch_jobs(bundle, analyst, brief, analyst_batches))
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
            errors = AnalystReportValidator(bundle, analyst).validate(report)
            if errors:
                raise DailyPipelineError("%s 分析報告合併後驗證失敗：%s" % (analyst, "；".join(errors[:10])))
            reports[analyst] = report
            self.save_artifact("analyst_%s" % analyst, report)
        return reports

    def _analyst_batch_jobs(
        self, bundle: Mapping[str, object], analyst: str, brief: Dict[str, object],
        analyst_batches: Sequence[Mapping[str, object]],
    ) -> List[Tuple[str, Callable[[], Dict[str, object]]]]:
        """每批一個任務；任務各自綁定本批的 brief、Validator 與提示，可安全同時執行。"""

        def make_job(batch: Mapping[str, object]) -> Tuple[str, Callable[[], Dict[str, object]]]:
            members = set(batch["symbols"])
            batch_brief = {
                **brief,
                "batch": {key: batch[key] for key in ("batch_index", "batch_count", "symbols")},
                "symbols": [entry for entry in brief["symbols"] if entry["symbol"] in members],
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
            validator = AnalystReportValidator(bundle, analyst, symbols=batch["symbols"])
            task = AgentTask(
                name,
                "你是 $%s-analyst。先讀 %s，再讀本批輸入摘要 %s。只輸出本批 %d 檔股票（%s），每一檔都必須出現一次。%s"
                % (
                    analyst, self.root / ("tradeagent/analysis/%s/%s-analyst/SKILL.md" % (analyst, analyst)), brief_path,
                    len(batch["symbols"]), "、".join(batch["symbols"]),
                    _SHARED_RULES + (_EVENT_RULES if analyst == "event" else ""),
                ),
                EVENT_ANALYST_SCHEMA if analyst == "event" else ANALYST_SCHEMA,
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
                    _SHARED_RULES,
                ),
                STANCE_SCHEMA,
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
        validator = PriorDecisionContextValidator(bundle)
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
    ) -> Dict[str, object]:
        """交易 Agent 逐批整合多空研究，說明論點取捨後決定意圖與 buy／add 信心。

        ``prior_context`` 是 ``load_prior_decision_context`` 的結果：只放進 brief 當背景，Validator 禁止引用。
        ``portfolio_slots`` 是 ``build_portfolio_slots`` 算好的名額脈絡（啟用 rotation 才有）：讓交易 Agent
        知道滿檔時買新股必須有持股出場。名額與配對仍由程式決定，Agent 不輸出任何數字。
        ``position_review`` 是 ``build_position_review`` 算好的持股檢視（policy 有 ``position_review`` 才有）：
        警示 ``review_id`` 必須和 claim 一樣處理一次，停損／停利的判斷仍由 Agent 做，數字由程式算。"""
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
        lessons_background = lessons_brief(load_strategy_lessons(self.root))  # 反思整理的做法建議；只是背景，沒有可引用的 ID
        batches = universe_batches([row["symbol"] for row in bundle["snapshot"]["latest_prices"]])
        def make_job(batch: Mapping[str, object]) -> Tuple[str, Callable[[], Dict[str, object]]]:
            name = "trader_b%d" % batch["batch_index"]
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
                    **({"prior_decision_background": background} if background is not None else {}),
                    **({"strategy_lessons": lessons_background} if lessons_background is not None else {}),
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
                        }
                        for symbol in batch["symbols"]
                    ],
                },
            )
            validator = TradeDecisionValidator(bundle, momentum, debate, symbols=batch["symbols"], prior_context=prior_context, position_review=position_review)
            task = AgentTask(
                name,
                "你是 $trader 交易 Agent，負責整合多頭與空頭兩位研究員的結果。先讀 %s 及其中連結的 references/data-rules.md，"
                "再讀本批輸入 %s。對本批 %d 檔股票（%s），先比較兩方的依據與分歧，說明每個論點採納或否決的理由，"
                "再依持股與交易條件形成單一交易意圖；不要按強度標籤或論點數量投票。"
                "rationale 只寫整合後的判斷，不逐條重貼多空報告或論點全文；論點識別碼保留在採納／否決陣列供程式核對。"
                "理由用簡短繁體中文分段：先寫主導決定的理由，再寫主要風險為何改變或沒有改變行動；"
                "不要在 rationale、未解問題或重估條件中寫 bull-/bear- 識別碼或英文等級，不反覆敘述逐個 claim 的處理過程。"
                "買進或加碼須給信心等級，其他行動不給買進信心。每檔剛好交付一次，不新增事實或 claim；欄位與限制依參考文件。%s%s%s%s"
                % (
                    self.root / "tradeagent/trading/trader/SKILL.md", brief_path,
                    len(batch["symbols"]), "、".join(batch["symbols"]),
                    _PRIOR_RULES if background is not None else "",
                    _ROTATION_RULES if portfolio_slots is not None else "",
                    _REVIEW_RULES if position_review is not None else "",
                    _SHARED_RULES,
                ),
                TRADE_SCHEMA,
            )
            return name, lambda: self.agent_step(
                task, lambda output: seal_trade_decision(envelope, output["items"]), validator.validate
            )

        # 各批只讀共同的帳戶、多空與動能輸入，不讀其他批的輸出，可同時執行。
        partials = self._run_jobs([make_job(batch) for batch in batches])
        outputs: List[List[Dict[str, object]]] = [partial["items"] for partial in partials]
        decision = seal_trade_decision(envelope, merge_batch_items(batches, outputs, "trader"))
        errors = TradeDecisionValidator(
            bundle, momentum, debate, prior_context=prior_context, position_review=position_review
        ).validate(decision)
        if errors:
            raise DailyPipelineError("TradeDecision 合併後驗證失敗：" + "；".join(errors[:10]))
        self.save_artifact("trade_decision", decision)
        (self.run_dir / "trader_report.md").write_text(
            render_trader_report(bundle, momentum, debate, decision, position_review), encoding="utf-8"
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
                "你是 $trader 交易 Agent，現在做**第二輪入選排序**。先讀 %s 與 %s，再讀全部 buy／add 候選 %s。"
                "第一輪已給每檔信心等級；你只在**同一信心等級內**決定先後，把你最想優先入選的放在陣列最前面，"
                "每一檔都附一句白話理由。不得改信心等級，不得新增事實或論點，不得輸出任何數字或權重；"
                "每個信心等級必須剛好列出該等級的全部候選，不多不少。%s"
                % (self.root / "tradeagent/trading/trader/SKILL.md", self.root / "tradeagent/trading/trader/references/ranking.md", brief_path, _SHARED_RULES),
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
