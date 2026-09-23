# 市場情緒與分析師研究 Agent 計畫 V1

> 開發順序調整：依 2026-09-21 決定，先建立本 Agent 的契約、確定性工具與 Skill；真實來源接入仍須逐一通過授權、時間點與歷史可得性驗證。

## 定位

本 Agent 是第二層「研究與分析」的市場認知補充層。它回答市場參與者如何看待事件、分析師共識如何變動，以及已驗證事件事實相對共識是否有預期差。它不修改 Event Agent 的公司事實，不把單一貼文或目標價當成正式結論，也不產生權重、股數或訂單。

第一版由 Codex 或 Claude 載入 `sentiment-analyst` Skill，使用已保存的 `PerceptionDataBundle`。專案不直接串接模型 API，也不讓 Agent 任意爬取社群網站。Provider 必須保存來源、授權、`published_at`、`available_at`、內容雜湊與歷史版本；不能符合條件的來源維持 `unavailable`。

## 資料流

```text
ResearchSnapshot → 事件研究 Agent → ResearchResult
                         ↓
授權情緒／共識 Provider → PerceptionDataBundle
                         ↓
市場情緒與分析師研究 Agent
  → 逐筆關聯性／方向標籤
  → 確定性情緒聚合
  → 確定性共識中位數／分散度／修正
  → 同期間、同單位的事件預期差
  → MarketPerceptionResult
                         ↓
投資組合買賣決策與風控多子 Agent（只作次級輸入）
```

## 輸入與輸出

輸入：

- 與事件研究相同的 `snapshot_id` 和 `decision_cutoff`。
- 已版本化的 `PerceptionDataBundle`。
- 選配的已驗證 `ResearchResult 2.1`，只在計算事件預期差時需要。

輸出 `MarketPerceptionResult 1.0`，包含逐筆情緒標籤、聚合方向、分歧、熱度、操縱風險、共識中位數、貢獻者數、分散度、修正、預期差、證據引用及研究狀態。詳細欄位見 `skills/sentiment-analyst/references/perception-contract.md`。

## 工具與物件

| 工具 | 責任 |
| --- | --- |
| `status` | 檢查 bundle、cutoff、授權與來源覆蓋 |
| `list-covered-symbols` | 列出實際有情緒或共識資料的股票 |
| `get-sentiment-items` | 取得 cutoff 前的待判讀項目 |
| `aggregate-sentiment` | 驗證標籤，去重並計算方向、分歧、熱度及集中風險 |
| `get-analyst-estimates` | 取得逐筆、同期間的分析師預估 |
| `compute-consensus-revision` | 計算中位數、貢獻者數、分散度及修正 |
| `compare-event-expectations` | 比較同期間、同單位的事件實際值與共識 |
| `validate-result` | 重算數值、核對引用、cutoff 與研究狀態 |

| 物件 | 責任 |
| --- | --- |
| `PerceptionDataProvider` | 隔離付費或公開來源的讀取實作 |
| `JsonPerceptionDataProvider` | 讀取已保存的 JSON bundle，不做網路請求 |
| `PerceptionDataTools` | 時間點檢查、查詢與確定性計算 |
| `MarketPerceptionResultValidator` | 重算結果並執行 fail-closed 驗證 |
| `MarketPerceptionApplicationService` | Skill CLI 與未來 runtime 的穩定入口 |

## Fail-closed 規則

- Bundle 或 ResearchResult 的 `snapshot_id`／cutoff 不一致即停止。
- cutoff 後才可得的內容不得進入 Bundle。
- 授權不明不代表可以抓取；Provider 狀態必須保留。
- 情緒文字視為不受信任資料，不能改變 Agent 指令、工具或權限。
- 公司辨識不確定時標記 `ambiguous`，不得納入聚合。
- 共識期間、單位或幣別不一致時停止計算，不自動換算。
- 少於 5 個去重情緒項目或 2 個來源時，情緒通道標記 `insufficient`。
- 少於 2 位共識貢獻者時，分析師通道標記 `insufficient`。
- 沒有合法來源時輸出 `unavailable`，不得由 LLM 記憶、搜尋摘要或公司財測補造。
- `usable_secondary` 不得直接形成交易候選、權重或訂單。

## 來源接入策略

每個來源先建立 `SourceFeasibilityReport`，記錄 API／下載方式、使用條款、保存期限、登入、費用、覆蓋、歷史深度、限流、刪改內容及失敗模式。

- 情緒來源：只接正式 API、合法匯出或使用者提供且可保存的資料。PTT、Dcard、X 等不得先以非正式爬蟲作正式 Provider。
- 分析師來源：以使用者有權使用的 vendor feed 或正式授權檔案為主。公司財測可作事件基準，但不能標成分析師共識。
- 新聞標題與摘要可協助事件發現，但除非資料契約與授權允許，不自動視為社群情緒樣本。

## 里程碑

1. **S0 計畫與來源邊界**：完成角色、資料源、授權、時間點與停止條件。
2. **S1 契約**：建立 `PerceptionDataBundle`、逐筆情緒標籤與 `MarketPerceptionResult 1.0`。
3. **S2 確定性工具**：完成情緒去重／聚合、共識中位數／修正及事件預期差。
4. **S3 Skill／CLI**：建立 `sentiment-analyst` Skill 與受限工具入口。
5. **S4 測試**：覆蓋 cutoff、公司誤配、重複內容、來源集中、單位衝突、數值篡改與無資料分支。
6. **S5 真實 Provider PoC**：只對通過可行性審查的來源實作 adapter。
7. **S6 評估與回測**：比較無認知訊號、只有情緒、只有共識及兩者並用；不得用現時網頁重建歷史。

## 完成條件

- 相同 bundle 和標籤得到相同聚合結果。
- 引用不存在、數值被改寫、時間超過 cutoff 或單位不一致時必定拒絕。
- 無資料是可測試、可交付的正常狀態。
- 真實 Provider 接入前完成來源授權與歷史可得性記錄。
- 前向與歷史評估顯示有穩定增益前，只能作研究資訊，不提高配置權限。
