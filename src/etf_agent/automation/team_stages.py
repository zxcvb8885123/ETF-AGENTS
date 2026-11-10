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
from etf_agent.decision.strategy_lessons import lessons_brief, load_strategy_lessons
from etf_agent.decision.thesis_verdicts import load_thesis_verdicts, verdicts_brief

from .agent_runner import LATER_STAGE_GLOBS, AgentTask, DailyPipelineError
from .agent_schemas import (
    EVENT_BATCH_MAX_EVENTS,
    EVENT_BATCH_SIZE,
    RANKING_SCHEMA,
    SWAP_NOMINATION_SCHEMA,
    TRADE_SCHEMA,
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


def _peer_deny_reads(role: str) -> Tuple[str, ...]:
    peer = "bear" if role == "bull" else "bull"
    # 續跑時 run 目錄可能已有後段產物（含對方論點的 debate、交易與 team_inputs），一併擋掉；不用 ``decision*``，會擋到共同輸入。
    return ("%s_*" % peer, "brief_%s_*" % peer) + LATER_STAGE_GLOBS


class TeamStagesMixin:
    """由 ``DailyDecisionPipeline`` 組合；依賴 ``PipelineBase`` 的 save_*、agent_step 與 _run_jobs。"""

    def run_analyst_team(
        self, bundle: Mapping[str, object], momentum: Mapping[str, object], database_path: Optional[Path] = None
    ) -> Dict[str, Dict[str, object]]:
        """三位分析師逐批執行；事件與市場情緒共用一位 Agent、一份報告。"""
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
        typed = evidence_policy(bundle) is not None

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
                    _SHARED_RULES + (_EVENT_RULES if analyst == "event" else "") + (_FINDING_KIND_RULES if typed else ""),
                ),
                event_analyst_schema(typed) if analyst == "event" else analyst_schema(typed),
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
    ) -> Dict[str, object]:
        """交易 Agent 逐批整合多空研究，說明論點取捨後決定意圖與 buy／add 信心。

        ``prior_context`` 是 ``load_prior_decision_context`` 的結果：只放進 brief 當背景，Validator 禁止引用。
        ``portfolio_slots`` 是 ``build_portfolio_slots`` 算好的名額脈絡（啟用 rotation 才有）：讓交易 Agent
        知道滿檔時買新股必須有持股出場。名額與配對仍由程式決定，Agent 不輸出任何數字。
        ``position_review`` 是 ``build_position_review`` 算好的持股檢視（policy 有 ``position_review`` 才有）：
        警示 ``review_id`` 必須和 claim 一樣處理一次，停損／停利的判斷仍由 Agent 做，數字由程式算。
        ``exposure_review`` 是 ``build_exposure_review`` 算好的曝險檢視（policy 有 ``exposure_rotation`` 才有）：每檔持股的 beta 與捕捉
        當背景事實附在 brief（不可引用），並讓 hold 也必填 conviction；換股由程式依帳本與 beta 決定，Agent 不輸出任何數字。"""
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
                        }
                        for symbol in batch["symbols"]
                    ],
                },
            )
            validator = TradeDecisionValidator(bundle, momentum, debate, symbols=batch["symbols"], prior_context=prior_context, position_review=position_review, hold_conviction=hold_conviction)
            task = AgentTask(
                name,
                "你是 $trader 交易 Agent，負責整合多頭與空頭兩位研究員的結果。先讀 %s 及其中連結的 references/data-rules.md，"
                "再讀本批輸入 %s。對本批 %d 檔股票（%s）逐檔整合兩方論點、記錄每個論點的採納或否決，"
                "再依持股與交易條件形成單一交易意圖；整合方式、理由寫法、欄位與限制都依參考文件。"
                "交件前核對：items 的股票數必須等於本批 %d 檔、逐檔都有裁決，漏一檔整批會被退回重做。%s%s%s%s%s"
                % (
                    self.root / "tradeagent/trading/trader/SKILL.md", brief_path,
                    len(batch["symbols"]), "、".join(batch["symbols"]), len(batch["symbols"]),
                    _PRIOR_RULES if background is not None else "",
                    _ROTATION_RULES if portfolio_slots is not None else "",
                    _REVIEW_RULES if position_review is not None else "",
                    _EXPOSURE_RULES if exposure_review is not None else "",
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
            bundle, momentum, debate, prior_context=prior_context, position_review=position_review,
            hold_conviction=hold_conviction,
        ).validate(decision)
        if errors:
            raise DailyPipelineError("TradeDecision 合併後驗證失敗：" + "；".join(errors[:10]))
        self.save_artifact("trade_decision", decision)
        (self.run_dir / "trader_report.md").write_text(
            render_trader_report(bundle, momentum, debate, decision, position_review, hold_conviction), encoding="utf-8"
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
    ) -> Dict[str, object]:
        """5c 換股提名（agent_swap 閘門）：交易 Agent 在入選排序之後，提名「用哪檔新股換掉哪檔持股」的有序清單。

        ``policy`` 是 5c 時的基礎 Policy（reentry_cooldown、持有天數與論點提示已綁入）。程式判定不需要時直接封存 ``not_needed``、
        不呼叫 Agent；驗證最多三次嘗試，用盡封存 ``exhausted``、暫時性失敗重試後仍失敗封存 ``unavailable``（空清單，其餘決策照常）；
        安全拒絕仍整個停止。v1（影子）與 v2 的簡報、提示與 Skill 完全相同，這裡不讀 ``agent_swap.version``。"""
        envelope = nomination_envelope(bundle, trade_decision, ranking, "swap-nomination:%s" % run_id)
        try:
            facts = nomination_facts(bundle, policy, trade_decision, ranking, debate, position_review, exposure_review)
        except (DecisionToolError, KeyError, TypeError, ValueError) as error:
            raise DailyPipelineError("換股提名無法由共同輸入建立事實：%s" % error) from error
        validator = SwapNominationValidator(
            bundle, policy, trade_decision, ranking, debate, position_review, exposure_review, prior_context=prior_context,
        )
        if not needs_nomination(facts):
            nomination = program_nomination(envelope, STATUS_NOT_NEEDED)
        else:
            try:
                brief = swap_nomination_brief(
                    bundle, policy, trade_decision, ranking, debate, position_review, exposure_review,
                    load_thesis_verdicts(self.root, bundle["decision_cutoff"]),
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
                % (self.root / "tradeagent/trading/trader/references/swap-nomination.md", brief_path, _SHARED_RULES),
                SWAP_NOMINATION_SCHEMA,
            )
            fingerprint = canonical_sha256({"envelope": envelope, "brief": brief})
            outcome = self.agent_step_degradable(
                task, lambda output: seal_swap_nomination(envelope, output), validator.validate, fingerprint,
            )
            if outcome.artifact is not None:
                nomination = outcome.artifact
            elif outcome.status in (STATUS_EXHAUSTED, STATUS_UNAVAILABLE):
                nomination = program_nomination(envelope, outcome.status, outcome.errors)
            else:
                raise DailyPipelineError("換股提名未預期的步驟狀態：%s" % outcome.status)
        errors = validator.validate(nomination)
        if errors:
            raise DailyPipelineError("SwapNomination 驗證失敗：" + "；".join(errors[:10]))
        self.save_artifact("swap_nomination", nomination)
        return nomination
