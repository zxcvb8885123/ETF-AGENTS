# 動能／配置／風控 Agent 計畫 V1

> 開發順序：事件研究 Agent 通過驗收後第二個實作；完成後交給回測 Agent，最後才接自動化排程與報告 Agent。

## 定位

本 Agent 把 `ResearchSnapshot`、`ResearchResult`、目前帳戶與競賽規則轉成可稽核的 `DecisionResult`。Agent 只負責選擇核准工具、處理缺漏與說明結果；動能、波動、權重、股數、費稅、現金與競賽限制全部由確定性 Python 物件計算。

第一版仍由 Codex 或 Claude 工作階段載入規劃中的 `portfolio-risk` Skill，不在專案內串接 LLM API，也不使用 LangChain／LangGraph。它不自動下單、不送件，也不能覆寫 Data Agent 或事件研究結果。

## 輸入與輸出

輸入：

- 已通過品質閘門的 `ResearchSnapshot` 與 `snapshot_id`。
- 已通過引用驗證的 `ResearchResult`。
- 目前現金、持股、成本、可交易狀態與決策截止時間。
- 官方 150 檔交易池、競賽限制、基準 ETF 成分與權重版本。
- 手續費、交易稅、整股單位、滑價與成交假設。

輸出：

| 契約 | 內容 |
| --- | --- |
| `MomentumResult` | 相對報酬、均線、量能、ATR、波動、流動性、市場寬度與資料日期 |
| `AllocationProposal` | 進攻／防守角色、目標權重、現金目標、加入與退出原因 |
| `OrderProposal` | 從目前持倉到目標組合所需的確定性買賣股數與成本估算 |
| `GuardResult` | 每條競賽限制的通過、拒絕、警告及證據 |
| `DecisionResult` | 合併上述版本與引用，供回測、D-Plan 與報告使用 |

## 執行流程

```text
讀取 Snapshot＋ResearchResult＋帳戶狀態
  → 驗證版本、cutoff、交易池與必要資料
  → MomentumEngine 計算全交易池特徵
  → CandidateMerger 合併事件候選與動能／防守候選
  → PortfolioAllocator 產生目標權重
  → OrderPlanner 換算整股、費稅與現金
  → CompetitionGuard 執行 fail-closed 規則
      ├─ 不通過 → rejected DecisionResult
      └─ 通過   → approved DecisionResult
  → 保存完整輸入版本、結果與拒絕原因
```

## Agent 工具

| 工具 | 責任 |
| --- | --- |
| `get_decision_inputs` | 取得同一 cutoff 的 Snapshot、研究結果、帳戶與規則版本 |
| `compute_momentum_features` | 以固定公式計算動能、量能、ATR、波動與市場寬度 |
| `build_candidate_set` | 合併事件、動能與防守候選，保留來源與排除原因 |
| `propose_allocation` | 根據市場狀態、上限及候選數建立目標權重 |
| `plan_orders` | 以價格、整股、費稅與現金限制計算訂單草案 |
| `validate_competition_rules` | 檢查持股數、個股上限、現金、交易池、Active Share 與可交易性 |
| `save_decision_result` | 只保存 schema 合格且引用完整的結果 |

LLM 不得自行計算技術指標、權重、股數、費稅或 Active Share，也不能修改工具回傳值。Agent 若要改變工具參數，必須使用設定允許的範圍並留下執行紀錄。

## 物件架構

| 物件 | 責任 |
| --- | --- |
| `PortfolioRiskAgentService` | 對外入口與執行生命週期 |
| `MomentumEngine` | 計算可重播的市場與個股特徵 |
| `CandidateMerger` | 合併事件、動能與防守候選 |
| `PortfolioAllocator` | 依市場狀態與上限產生目標權重 |
| `OrderPlanner` | 將目標權重轉成買賣股數及費稅 |
| `CompetitionGuard` | 執行不可繞過的競賽與資料品質規則 |
| `DecisionRepository` | 保存輸入版本、提案、拒絕理由與最終結果 |

既有 `EventDrivenStrategy` 與 `CompetitionGuard` 作為第一版基礎，但需要拆開「候選評分」「配置」「訂單」與「風控」契約，避免單一物件同時決定全部結果。

## Fail-closed 規則

- Snapshot 不可用、版本不一致或研究引用不存在時停止。
- 缺少必要價格、帳戶、競賽規則或基準權重時不得假設數值。
- 交易池外標的、不可交易標的、持股數或個股權重違規時拒絕。
- 現金不得為負，且必須符合競賽上限與預留交易成本。
- 無法通過 Active Share 或其他必要條件時輸出 `rejected`，不能由 Agent 改寫為警告。
- 任何 `rejected` 結果都不得形成可送件訂單。

## 開發里程碑

1. **P0 契約**：建立 `MomentumResult`、`AllocationProposal`、`OrderProposal`、`GuardResult` 與 `DecisionResult` schema。
2. **P1 動能引擎**：完成時間隔離、無未來資料的行情特徵與市場狀態測試。
3. **P2 配置與訂單**：完成目標權重、整股、費稅、現金與再平衡測試。
4. **P3 風控閘門**：補齊所有競賽規則、拒絕分支與手算 fixture。
5. **P4 Agent 工具循環**：建立 `portfolio-risk` Skill、工具預算、結構化結果與 Codex／Claude 一致性測試。
6. **P5 回測交接**：固定輸入、輸出與策略版本，交給[回測 Agent](backtest_agent_plan.md)，並依[回測方法規格](backtest_plan_v1.md)比較等權、純動能、規則事件與 Agent 事件版本。

完成條件：同一輸入得到相同數值結果；所有訂單可由帳戶、價格、權重與費稅重算；必要規則違反時必定拒絕；達成後交給回測 Agent，不直接跳到自動化排程。
