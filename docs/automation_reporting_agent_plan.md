# 自動化排程與報告 Agent 計畫 V1

> 開發順序：事件研究 Agent、投資組合買賣決策與風控多子 Agent 及回測 Agent 完成，且固定版本前向驗證通過後最後實作。

> 現況：Research Report V0 已完成研究層 artifact 的 JSON／Markdown 整合與完整重建驗證；它不包含本文件規劃的 DecisionResult、D-Plan、正式 DailyReport、FailureReport、排程或通知。

## 定位

本 Agent 負責在固定時間啟動已驗證的 Data、Research、Portfolio／Risk 流程，保存每個階段的結果，組裝 D-Plan 與人類可讀報告，並在失敗時通知使用者。它不能修改研究結論、重算權重、放寬風控、直接下單或自動送件。

排程本身使用一般 scheduler 執行確定性工作；只有需要摘要、解釋失敗或整理報告文字時，才由 Codex／Claude 工作階段載入規劃中的 `daily-report` Skill。第一版不需要模型 API、LangChain、LangGraph 或 CLIProxyAPI。

## 每日流程

```text
排程觸發並建立 pipeline_run_id
  → Data Agent 更新資料並建立 ResearchSnapshot
  → 事件研究 Agent 產生 ResearchResult
  → 投資組合買賣決策與風控多子 Agent 產生 DecisionResult
  → 驗證所有 run_id、snapshot_id、cutoff 與版本一致
  → DPlanBuilder 組裝官方 D-Plan 4.0
  → DPlanValidator 執行 JSON Schema＋語意驗證
      ├─ 失敗 → FailureReport＋通知，停止送件候選
      └─ 通過 → D-Plan.json＋DailyReport
  → 人工確認與手動送件
```

任何前置階段失敗時不得跳過或沿用未知版本結果。是否允許使用最近一次已知良好結果必須由明確規則決定，並在報告中標記資料日期；預設 fail closed。

## 輸入與輸出

輸入：

- `DataAgentResult`／`ResearchSnapshot`
- `ResearchResult`
- `DecisionResult`／`GuardResult`
- `BacktestReport`／前向驗證狀態
- 官方 `D-Plan.schema.json`、語意規則及競賽設定版本
- 排程、截止時間、通知政策與人工確認狀態

輸出：

| 產物 | 用途 |
| --- | --- |
| `PipelineRun` | 各階段狀態、開始／結束時間、版本、重試與錯誤 |
| `D-Plan.json` | 通過官方 schema 4.0 與語意規則的送件候選 |
| `DailyReport` | 市場狀態、事件證據、配置、訂單、風控與不確定性摘要 |
| `FailureReport` | 失敗階段、錯誤碼、受影響產物與需要人工處理事項 |
| `RunManifest` | 所有 snapshot、research、decision、schema、Skill 與程式版本引用 |

## 物件與責任

| 物件 | 責任 |
| --- | --- |
| `PipelineOrchestrator` | 依相依關係啟動各階段，不執行投資判斷 |
| `SchedulePolicy` | 定義盤後、盤前、截止前與重試時間窗 |
| `RunRepository` | 保存 pipeline run、階段狀態、版本與 artifact |
| `DPlanBuilder` | 從已驗證輸出配置 `S1`／`O1` 等 ID 並建立完整引用鏈 |
| `DPlanValidator` | 執行官方 JSON Schema 4.0 與 C1／C2／C6／C9／C11／C12／C14 規則 |
| `DailyReportBuilder` | 產生人類可讀報告，不改寫來源數字或風控結果 |
| `NotificationPolicy` | 只在完成、失敗或需要人工處理時通知 |
| `ManualApprovalGate` | 在送件前要求人工確認 |

D-Plan Builder／Validator 是確定性程式，不是另一個會自行推論的 LLM Agent。報告 Agent 可以整理文字，但所有數字、引用、決策與訂單必須直接來自已驗證契約。

## 排程草案

- 盤後：更新官方行情、公司資料與新聞候選，執行來源健康檢查。
- 盤前：以明確 `decision_cutoff` 建立 Snapshot，執行研究、配置與風控。
- 截止前：產生並驗證 D-Plan 與每日報告，保留人工檢查時間。
- 失敗重試：只重試明確的暫時性錯誤；schema、資料品質、引用與風控錯誤不自動重試掩蓋。

實際時間待主辦單位提交規則與資料發布時間確認後設定，不在計畫中硬編未確認時間。

## 安全與失敗處理

- 每個階段使用唯一 `pipeline_run_id`，並保留原始錯誤與 exit code。
- 上游 `failed` 或必要結果 `usable=false` 時停止下游。
- 禁止混用不同 cutoff、交易池版本或 Snapshot 的產物。
- D-Plan 驗證失敗時只產生 FailureReport，不產生「看似可送件」檔案。
- 報告缺少來源時不得由 LLM 補寫；缺值保持 `null` 或明確標記 unavailable。
- 第一版不登入券商、不自動下單、不自動送出競賽平台。
- 排程不得自動修改 Skill、提示詞、模型、策略參數或程式碼。

## 開發里程碑

1. **A0 執行契約**：建立 `PipelineRun`、階段狀態機、RunManifest 與 artifact 目錄。
2. **A1 D-Plan**：將官方 schema 納入專案，實作 Builder、schema validator、語意 validator 與 fixture。
3. **A2 報告**：實作 DailyReport／FailureReport Builder，驗證所有數字可回溯。
4. **A3 排程**：加入本機排程、鎖定、防重複執行、有限重試與通知政策。
5. **A4 Agent 整合**：建立 `daily-report` Skill，由 Codex／Claude 產生有引用的說明文字。
6. **A5 演練**：以成功、資料缺漏、來源故障、研究失敗、風控拒絕與 schema 失敗情境做端到端測試。

完成條件：每日流程可以重跑且不重複寫入；失敗時停止在正確階段；D-Plan 通過官方驗證；報告與送件候選可回溯到同一組輸入版本；人工批准前不會發生外部送件或交易。
