# 事件與市場情緒分析師

本頁保留合併時的 2.1 格式說明。當前每日流程已使用 2.2：「全市場情緒一次＋各公司事件各一次」，參見[當前範圍與缺口](event_market_scope.md)。舊格式只按原輸入重建，不改寫成新版。

## 現況

2026-10-03 合併為一位 Agent、一份報告。交易與風險未在本次報告執行。真實新聞／情緒來源尚未接入，不能把公告方向當作市場反應。

## 契約與資料邊界

事件摘要新增 `financial_context`，從同一 Snapshot 的最新損益表與資產負債表接入原值及證據。新 finding 可以引用同檔財報作背景，但事件方向仍需事件引用。每日流程將唯讀候選盤點的狀態交給事件分析師，詳細見 [資料接線](event_data_availability.md)。

分析團隊新交付鍵為 technical、fundamental、event。event 保留既有程式識別字，schema_version 為 2.1。每檔輸出 outlook（綜合）、event_outlook（事件）、events（全量逐則分級）、findings 與 data_gaps。sentiment 為已有來源 adapter 的逐檔 outlook、findings、data_gaps，Python 從同一共同輸入重建附入，模型不得輸出或改寫。

事件與情緒皆 unknown 時，綜合 outlook 必須 unknown。沒有事件時事件看法必須 unknown。事件方向需事件引用；文字發現只能引用本檔事件或已驗證情緒證據。情緒樣本、去重、來源授權、cutoff 與數值仍由既有 Perception validator 檢查。共識水準不單獨形成方向，情緒只作次級輸入。

對外用正面／中性／負面；unknown 顯示「資料不足，無法判斷」。報告保留事件、情緒、綜合三項，不把缺資料改成中性。重大性 high 仍交既有深入研究流程。

## 歷史相容

已封存的四份 AnalystReport 2.0 保留原始重建與共享輸入雜湊，不改寫 artifact。新的三份輸入必須包含 event 2.1，舊四份不能混用 event 2.1。檢查缺報告或混搭時拒絕。

## 本次報告

選用現有 2026-09-29 截止的真實、已驗證 DecisionInputBundle，本批分析台達電與富邦金兩檔，逐則覆蓋這兩檔的全部 10 份公告。報告由本次 Codex 工作階段依新 Skill 產生，為歷史資料分析示範；情緒來源缺少時明確降級。股票範圍及輸入、輸出與驗證結果保存於不可變 run。

2026-10-09：每日決策事件研究併入分析團隊，high 事件直接交股票層級多空研究，不再另跑四子 Agent。此更新取代先前 high 必須深入辯論的流程描述；event 2.2 的逐則覆蓋、引用、情緒授權與時間驗證不變。詳見 [新鏈與舊封存契約](integrated_event_analysis.md)。
