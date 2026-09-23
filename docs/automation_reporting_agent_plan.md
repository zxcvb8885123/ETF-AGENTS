# 自動化排程與報告 Agent 計畫 V2

> 更新日期：2026-09-22。開發順序：可先實作離線契約、報告與 fixture 驗收；正式每日排程須待資料、研究、決策／風控、正式回測及固定版本前向驗證通過後啟用。

> 現況：Research Report V0 與 A0／A1 離線 DailyReport／FailureReport 已完成；RPT0～RPT4 第一版及一件真實事件研究續跑已通過驗收。正式 Decision／Risk 輸入與每日排程尚未接通；詳見[真實研究續跑與決策報告驗收](report_workflow_acceptance_plan.md)。

## 定位

本 Agent 負責在固定時間啟動已驗證的 Data、Research、Portfolio／Risk 流程，保存每個階段的結果，組裝 D-Plan 與人類可讀報告，並在失敗時通知使用者。它不能修改研究結論、重算權重、放寬風控、直接下單或自動送件。

架構不設「主辦平台送件／交易執行 Agent」。本 Agent 的交付終點是報告、已驗證候選檔與人工檢視資訊；平台送件與交易屬系統外的人工操作，也不列入本計畫的後續開發階段。人工確認不會觸發任何外部送件或交易。

排程本身使用一般 scheduler 執行確定性工作；只有需要摘要、解釋失敗或整理報告文字時，才由 Codex／Claude 工作階段載入 `daily-report` Skill。第一版不需要模型 API、LangChain、LangGraph 或 CLIProxyAPI。

## 每日流程

```text
排程觸發並建立 pipeline_run_id
  → Data Agent 更新資料並建立 ResearchSnapshot
  → 事件研究 Agent 產生 ResearchResult
  → 選配市場認知結果及其版本化 PerceptionDataBundle（缺少時明確降級）
  → 投資組合買賣決策與風控多子 Agent 產生 DecisionResult
  → 驗證所有 run_id、snapshot_id、cutoff 與版本一致
  → DPlanBuilder 組裝官方 D-Plan 4.0
  → DPlanValidator 執行 JSON Schema＋語意驗證
      ├─ 失敗 → FailureReport＋通知，停止送件候選
      └─ 通過 → D-Plan.json＋DailyReport
  → 交付人工檢視，系統流程結束
```

任何前置階段失敗時不得跳過或沿用未知版本結果。是否允許使用最近一次已知良好結果必須由明確規則決定，並在報告中標記資料日期；預設 fail closed。

## 輸入與輸出

輸入：

- `DataAgentResult`／`ResearchSnapshot`
- `ResearchResult`
- 選配的 `MarketPerceptionResult`／`PerceptionDataBundle`，必須成對且通過驗證
- `DecisionResult`／`GuardResult`
- `BacktestReport`／前向驗證狀態
- 官方 `D-Plan.schema.json`、語意規則及競賽設定版本
- 排程、截止時間、通知政策與執行模式（fixture／正式）

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
| `ReviewHandoff` | 列出檔案、雜湊、驗證結果及待人工檢視事項；可保存檢視紀錄，不執行送件或交易 |

D-Plan Builder／Validator 是確定性程式，不是另一個會自行推論的 LLM Agent。報告 Agent 可以整理文字，但所有數字、引用、決策與訂單必須直接來自已驗證契約。

一般 scheduler 只啟動確定性 CLI。需要 Agent 推論但尚無已配置的 Codex／Claude 工作階段時，流程保存 `waiting_for_agent` 狀態與輸入 artifact；收到通過驗證的輸出後才能續跑，不宣稱單靠 scheduler 已完成無人值守推論。

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

Research Report V0 不代表正式 DailyReport；A0／A1 已完成 fixture／離線版，A2 以後仍為規劃。

| 階段 | 具體工作與交付 | 驗收條件 |
| --- | --- | --- |
| A0 執行契約與離線編排 | **已完成：fixture／離線版。** `PipelineRun`、`RunManifest`、不可變 artifact 儲存、上游 adapter 與最小 CLI；先讀取已保存結果 | 驗證 Snapshot、含時區 cutoff、版本及內容雜湊；上游失敗停止下游；可重建、可驗證、可續跑 |
| A1 每日／失敗報告 | **已完成：fixture／離線版。** DailyReport 同源 JSON／Markdown、FailureReport、重建 validator；整合研究、決策與風控的已驗證結果 | 修改任一數字、引用或風控結果均被拒絕；研究資料不足時輸出降級 DailyReport；風控拒絕、資料不符或執行鍵衝突時只封存 FailureReport |
| A2 D-Plan 候選檔 | 待官方規格。取得並封存官方 schema 與語意規則的來源／版本／雜湊；建立 Builder、Validator、ReviewHandoff | 完整引用鏈且可重算；缺官方規格時標記 blocked，不以自建 fixture 宣稱通過官方驗證；失敗不發布候選檔 |
| A3 排程與恢復 | 本機 scheduler adapter、交易日／截止時間設定、互斥鎖、防重複、有限重試、續跑與通知紀錄 | 重複觸發不重複發布；程序中斷可恢復；逾時停止；通知失敗不使既有通過產物失效，且可獨立重試 |
| A4 Daily-report Skill 整合 | **部分完成：RPT0～RPT4 已接入**；工作流可在 Research Report 後呼叫既有 `AutomationReportingApplicationService`，產生 DailyReport 或 FailureReport | `$daily-report` 明確出現在 default_prompt；研究與決策 CLI 支援執行、狀態、驗證與續跑；缺 Decision／Risk 時維持 `waiting_for_decision`，文字不可改寫已驗證事實／決策 |
| A5 端到端演練與正式啟用驗收 | fixture 故障注入、真實資料 dry-run、固定版本前向驗證證據與正式啟用清單 | fixture 與正式產物明確區隔；正式資料、帳戶、交易狀態、競賽規則與回測／前向驗證均通過，才可啟用正式排程 |

核心邏輯放在 `src/etf_agent/automation/` 與 `src/etf_agent/runtime/`，報告 Builder／Validator 延伸 `src/etf_agent/reporting/`；`cli/` 提供人工、排程與 Skill 共用的命令入口，Skill 只定義 Agent 工作流程。A0 起提供最小離線 CLI，各階段同步加入測試，A4 再整合完整操作介面。

### 已完成的離線入口

`cli/daily_report.py` 已提供 A0／A1 的三個確定性操作：

- `run`：驗證封存的 Decision run、對齊同一份 Snapshot 與 ResearchResult，成功時產生 DailyReport，失敗時只封存 FailureReport；`report_workflow.py` 可在研究報告完成後續接這個入口。
- `validate`：以原始輸入完整重建 DailyReport。
- `verify-run`：驗證已封存 pipeline run 的 manifest 與每個檔案雜湊。

建立前必須先以投資組合決策 CLI 保存完整、已驗證的 Decision run。例如：

```bash
PYTHONPATH=src python3 cli/daily_report.py run \
  --execution-mode fixture \
  --generated-at 2026-09-20T01:05:00+00:00 \
  --pipeline-run-id fixture-20260920-1 \
  --repository artifacts/pipeline_runs \
  --snapshot /path/to/research_snapshot.json \
  --research /path/to/research_result.json \
  --decision-repository /path/to/portfolio_decisions \
  --decision-run-id decision-run-id
```

`fixture` 只用於契約與故障分支驗收，並非真實資料或正式日常決策。相同執行日、cutoff、模式與策略規格若有相同輸入，會回傳既有封存結果；若輸入內容不同，則封存 FailureReport 並拒絕重複發布。

## 執行契約與驗收案例

- `PipelineRun` 保存 run ID、執行模式、策略／程式／設定版本、含時區 cutoff、階段輸入輸出雜湊、開始／結束時間、重試次數與錯誤碼。`RunManifest` 封存所有必要輸入引用，禁止悄悄換成最新版本。
- 階段狀態採 `pending → running → succeeded`；可轉入 `waiting_for_agent`、`failed` 或 `blocked`。等待輸出不可標為成功；截止前仍缺必要 artifact 時停止該 run。
- 防重複鍵包含業務日期、cutoff、執行模式與設定／策略版本；同鍵相同輸入回傳既有結果，同鍵不同輸入拒絕覆寫。重試保存獨立 attempt，已封存產物保持不可變。
- 報告與候選檔先在暫存區完整驗證，再一次發布；重啟不得把半成品視為成功。D-Plan 失敗可保留內部診斷，但只交付 FailureReport，不交付可用候選檔。
- 測試涵蓋 cutoff 後資料、缺時區、Snapshot／版本不符、引用不存在、產物遭修改、缺必要資料、風控拒絕、schema 不符、重複觸發、中斷恢復、超時與通知故障。
- 市場認知缺少時允許明確降級；若有提供，必須成對驗證 bundle／result、授權與時間一致性。必要決策、風控或正式啟用證據缺少時停止。
- 每次修改 Python、契約、CLI 或 Skill，依 `AGENTS.md` 執行完整 unittest、compileall 與 diff check；新增 Skill 再執行 quick_validate。

完成條件：每日流程可重跑且不重複發布；失敗停在正確階段；報告與候選檔可回溯到同一組輸入版本；D-Plan 通過已封存的官方規格驗證；整個流程以交付人工檢視結束，不具備外部送件或交易執行能力。

A0／A1 與 RPT0～RPT4 第一版已完成，沿用既有 Builder，補上資料時間、Agent 交接、續跑、固定格式交付與 Decision／Risk 接線。下一步是以實際來源完成一次研究續跑演練，再接 A2 官方候選檔、A3 正式排程及 A5 正式啟用；各階段仍需原有前置驗收，不因研究報告可用而提前啟用。
