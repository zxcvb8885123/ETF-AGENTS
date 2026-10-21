# 事件研究併入分析團隊（2026-10-09）

## 變更重點

每日決策鏈不再對 high 事件呼叫 Fact／Bull／Bear／Adjudicator。新流程如下：

1. 事件事實由程式從同 Snapshot 的文件建立。
2. 事件分析師交付 `materiality`、`findings`、反向材料與 `data_gaps`。
3. 股票層級的多頭／空頭研究員讀相同的共同輸入，只做一輪多空研究。
4. 交易 Agent 逐一採納或否決論點。
5. 風險 Agent 給現金姿態，並審查程式提案。

## 新契約與歷史相容

### `team_inputs`

| 版本 | 規則 |
| --- | --- |
| 2.1（新） | 嚴格只接受 `schema_version`、`analyst_reports`、`cash_stance`。三份報告的鍵必須是 technical／fundamental／event，event 使用 2.2。不得注入 `research_result` |
| 2.0（舊） | 沿用原白名單、三／四份分析報告與已驗證的 ResearchResult。多空共同輸入雜湊、DecisionResult ID 與封存內容都不改寫 |

### 共同輸入雜湊

- 新鏈的共用雜湊使用空的事件研究輸入 `{}`。這只是既有函式的相容參數。
- 它不建立 ResearchResult，也不宣稱「沒有事件」或「completed」。
- StancePacket／ResearchDebateBundle 維持 2.0。其 `shared_input_sha256` 綁定實際的分析報告與相容參數。

### 驗證

- Finalizer 與 Validator 依 `team_inputs` 版本，重建同一份多空輸入。
- 缺輸入、版本混搭、多餘欄位、內容被改，一律拒絕。

### 多空與風險的輸入

- 新的多空 brief **不含** `event_research`、`research_status`。
- high 事件保存在 event 分析報告，並交給多空兩方。
- 風險摘要直接包含逐檔的 `company_events`、`findings`、`data_gaps`。

### 每日摘要與續跑

- 每日摘要使用 `event_analysis_mode=integrated_analyst`，不再讀取不存在的 `event_research_result`。
- 新 run 保存 `decision_chain` 2.1 標記。
- 有舊 raw 輸出、卻缺少這個標記的 run，**不能跨版本續跑**，須另建新 run。這是為了避免把舊模型判斷重新包裝成新的共同輸入。
- 新鏈同版本仍可 resume。

### 保留與不放寬

獨立事件研究的 CLI、Validator 與工具仍保留，供舊封存與單獨研究使用，但不在目前每日決策路徑內。

下列規則**不放寬**：來源、`available_at`、數字重算、授權、Guard、最多 3 次風險修正。

## 多空研究指令與市場背景（2026-10-09）

**Skill 文件**

- 多空 Skill 的主文件只描述：研究目的、如何從證據形成論點、強弱，以及何時需要重估。
- 欄位、ID 與引用規則，移到各自的 `references/data-rules.md`。
- 執行 prompt 要求讀取該參考文件。

**市場背景**

- 多空 brief 在頂層帶入 event 2.2 已驗證的 `market_sentiment`。兩方收到相同資料，且只出現一次。
- 個股事件仍在 `symbols[].analysts.event`。
- 缺少市場資料時維持 `unavailable`，不推成中性。市場背景不能代替個股論點的證據。
- 共同輸入雜湊原本就包含完整分析報告，所以不變更歷史雜湊或既有 JSON 輸出契約。

## 交易整合閱讀版（2026-10-09）

新增 `decision/trader_report.py`。

- 驗證 TradeDecision 後，只照原值呈現逐檔整合結果。不重貼多空報告，也不逐條重貼論點。
- 每日 `run_trader` 會自動保存 `trader_report.md`。
- 正式 JSON 仍保留 `adopted_claim_ids`／`rejected_claim_ids`。原始多空封存與契約不變。
- 主 prompt 明確要求：`rationale` 是整合後的判斷，不羅列多空全文。

### 可讀性補強

- 輸出先顯示結論與信心。
- 理由依完整句分段。
- 移除括號中的論點索引。索引當主詞時，保留多頭／風險分析的歸屬。
- 英文等級轉成中文。
- prompt 同步要求：短句、分段，對外不寫內部 ID。

這只是呈現轉換：不摘要、不改數值或決定。原始 JSON 與其雜湊都保留。
