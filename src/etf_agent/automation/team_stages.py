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
    render_trader_report,
    seal_report,
    seal_stance_packet,
    seal_trade_decision,
    trade_decision_envelope,
)
from etf_agent.decision.analysts import LLM_ANALYSTS, no_event_item, seal_event_market_report
from etf_agent.decision.prior_decision import NO_SESSION_REASON
from etf_agent.decision.stance import STANCE_ROLES

from .agent_runner import AgentTask, DailyPipelineError
from .agent_schemas import (
    ANALYST_SCHEMA,
    EVENT_ANALYST_SCHEMA,
    EVENT_BATCH_MAX_EVENTS,
    EVENT_BATCH_SIZE,
    STANCE_SCHEMA,
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
                program_items = [no_event_item(row["symbol"], row["data_availability"])
                                 for row in brief["symbols"] if not row["events"]]
                skipped = {item["symbol"] for item in program_items}
                self.log("  [事件] %d 檔無重大訊息，由程式輸出 unknown，其餘 %d 檔交給 Agent" % (
                    len(skipped), len(brief["symbols"]) - len(skipped)))
                analyst_batches = weighted_batches(
                    [row["symbol"] for row in brief["symbols"] if row["symbol"] not in skipped],
                    {row["symbol"]: len(row["events"]) for row in brief["symbols"]},
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
    ) -> Dict[str, object]:
        """交易 Agent 逐批整合多空研究，說明論點取捨後決定意圖與 buy／add 信心。

        ``prior_context`` 是 ``load_prior_decision_context`` 的結果：只放進 brief 當背景，Validator 禁止引用。"""
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
                    **({"prior_decision_background": background} if background is not None else {}),
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
                        }
                        for symbol in batch["symbols"]
                    ],
                },
            )
            validator = TradeDecisionValidator(bundle, momentum, debate, symbols=batch["symbols"], prior_context=prior_context)
            task = AgentTask(
                name,
                "你是 $trader 交易 Agent，負責整合多頭與空頭兩位研究員的結果。先讀 %s 及其中連結的 references/data-rules.md，"
                "再讀本批輸入 %s。對本批 %d 檔股票（%s），先比較兩方的依據與分歧，說明每個論點採納或否決的理由，"
                "再依持股與交易條件形成單一交易意圖；不要按強度標籤或論點數量投票。"
                "rationale 只寫整合後的判斷，不逐條重貼多空報告或論點全文；論點識別碼保留在採納／否決陣列供程式核對。"
                "理由用簡短繁體中文分段：先寫主導決定的理由，再寫主要風險為何改變或沒有改變行動；"
                "不要在 rationale、未解問題或重估條件中寫 bull-/bear- 識別碼或英文等級，不反覆敘述逐個 claim 的處理過程。"
                "買進或加碼須給信心等級，其他行動不給買進信心。每檔剛好交付一次，不新增事實或 claim；欄位與限制依參考文件。%s%s"
                % (
                    self.root / "tradeagent/trading/trader/SKILL.md", brief_path,
                    len(batch["symbols"]), "、".join(batch["symbols"]),
                    _PRIOR_RULES if background is not None else "", _SHARED_RULES,
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
        errors = TradeDecisionValidator(bundle, momentum, debate, prior_context=prior_context).validate(decision)
        if errors:
            raise DailyPipelineError("TradeDecision 合併後驗證失敗：" + "；".join(errors[:10]))
        self.save_artifact("trade_decision", decision)
        (self.run_dir / "trader_report.md").write_text(
            render_trader_report(bundle, momentum, debate, decision), encoding="utf-8"
        )
        return decision
