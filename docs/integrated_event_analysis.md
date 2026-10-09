# 事件研究併入分析團隊（2026-10-09）

每日決策鏈不再對 high 事件呼叫 Fact／Bull／Bear／Adjudicator。事件事實由程式從同 Snapshot 的文件建立，事件分析師交付 materiality、findings、反向材料與 data_gaps；股票層級多頭／空頭研究員讀相同共同輸入，完成唯一一輪多空研究。交易 Agent 逐一採納或否決論點，風險 Agent 給現金姿態並審查程式提案。

## 新契約與歷史相容

- 新 `team_inputs.schema_version=2.1` 嚴格只接受 `schema_version`、`analyst_reports`、`cash_stance`。三份報告鍵必須是 technical／fundamental／event，event 使用 2.2；不得注入 research_result。
- 舊 2.0 沿用原白名單、三／四份分析報告及已驗證 ResearchResult；多空共同輸入雜湊、DecisionResult ID 及封存內容不改寫。
- 新鏈的共用雜湊使用空事件研究輸入 `{}`，僅作既有函式的相容參數，不建立 ResearchResult，不宣稱沒有事件或 completed。
- Finalizer／Validator 按 team_inputs 版本重建同一份多空輸入；缺輸入、版本混搭、額外欄位或更改內容均拒絕。StancePacket／ResearchDebateBundle 維持 2.0，其 shared_input_sha256 綁定實際分析報告與相容參數。
- 新多空 brief 不含 event_research／research_status；high 事件保存在 event 分析報告並交給兩方。風險摘要直接包含逐檔 company_events、findings、data_gaps。
- 每日摘要使用 event_analysis_mode=integrated_analyst，不再讀取不存在的 event_research_result。新 run 保存 decision_chain 2.1 標記；有舊 raw 輸出卻缺此標記的 run 不可跨版本續跑，須另建新 run，避免把舊模型判斷重新包裝成新共同輸入。新鏈同版本仍可 resume。

獨立事件研究 CLI、Validator 與工具保留供舊封存與單獨研究；它們不在目前每日決策路徑。來源、available_at、數字重算、授權、Guard 與最多三次風險修正不放寬。
