# 回測 Agent 計畫 V1

> 開發順序：投資組合買賣決策與風控多子 Agent 通過單元與整合測試後實作；回測及前向驗證通過後，才接自動化排程與報告 Agent。

## 定位

回測 Agent 負責用歷史時鐘重播 Data、Research、Momentum／Portfolio／Risk 的相同契約，執行策略比較、成交模擬、帳戶更新與績效分析。它回答「Agent 是否正確使用當時可得資料」以及「研究與策略是否在扣除成本後產生穩定增益」。

回測 Agent 不修改正式資料、不調整線上帳戶、不下單，也不能使用回測日期之後的資訊。第一版由 Codex 或 Claude 工作階段載入規劃中的 `strategy-backtest` Skill，選擇核准的實驗與診斷工具；歷史時鐘、特徵、成交、帳務、費稅與績效一律由確定性 Python 物件執行。

詳細統計方法、資料切分、成交假設與比較策略以[回測與驗證方法規格](backtest_plan_v1.md)為準；本文件只定義 Agent 架構與交付流程。

## 輸入與輸出

輸入：

- `BacktestRequest`：期間、策略版本、初始資金、資料版本與實驗預算。
- 可按歷史 `decision_cutoff` 重建的 `ResearchSnapshot`。
- 事件研究、動能、配置、訂單與風控的固定版本契約。
- 競賽規則、交易成本、成交價格、公司行動及基準版本。
- 人工事件測試集與允許比較的基準策略。

輸出：

| 契約 | 內容 |
| --- | --- |
| `BacktestRun` | `backtest_id`、期間、版本、狀態、每個交易日與錯誤 |
| `ReplayResult` | 每日 Snapshot、ResearchResult、DecisionResult 與 GuardResult 引用 |
| `ExecutionResult` | 模擬成交、未成交、費稅、滑價、現金與持倉變化 |
| `PerformanceResult` | 淨值、報酬、最大回撤、換手率、成本及個股／產業貢獻 |
| `AgentEvaluationResult` | 引用正確率、工具成功率、格式通過率、重跑差異與耗用資源 |
| `BacktestReport` | 策略比較、限制、資料偏差、失敗原因與是否可進入前向驗證 |

## 執行流程

```text
接收 BacktestRequest 並鎖定所有版本
  → HistoricalClock 逐日推進 decision_cutoff
  → PointInTimeDataProvider 建立當時可用 Snapshot
  → 重播事件研究 Agent
  → 重播投資組合買賣決策與風控多子 Agent
  → ExecutionSimulator 模擬成交、費稅與未成交
  → AccountLedger 更新現金、持股與收盤估值
  → 保存每日 ReplayResult
  → PerformanceAnalyzer 計算研究品質與投資績效
  → 與基準策略比較並產生 BacktestReport
  → 通過門檻才進入固定版本前向驗證
```

## Agent 工具

| 工具 | 責任 |
| --- | --- |
| `create_backtest_run` | 驗證請求並鎖定資料、Skill、模型、策略與規則版本 |
| `inspect_historical_coverage` | 檢查價格、事件、財報、交易池與公司行動的時間涵蓋 |
| `run_historical_replay` | 依歷史時鐘執行指定期間，不允許直接瀏覽今日網頁 |
| `simulate_execution` | 根據核准口徑模擬成交、未成交、費稅、滑價與整股限制 |
| `analyze_performance` | 計算報酬、回撤、換手、成本、勝率及貢獻 |
| `evaluate_agent_behavior` | 評估引用、工具使用、格式、不確定性與重跑穩定度 |
| `compare_strategies` | 在相同期間、資料、成本與風控下比較核准策略 |
| `build_backtest_report` | 產生有版本引用的結果與限制說明 |

LLM 只能選擇已核准的實驗、要求診斷與解釋確定性結果，不能挑選最好的一次重跑、刪除失敗日、修改淨值、績效或統計區間。

## 物件架構

| 物件 | 責任 |
| --- | --- |
| `BacktestAgentService` | 對外入口、預算、狀態與停止條件 |
| `HistoricalClock` | 產生交易日與當日 decision cutoff |
| `PointInTimeDataProvider` | 只提供當時可得的資料版本 |
| `ReplayOrchestrator` | 使用與每日流程相同的研究、決策與風控介面 |
| `ExecutionSimulator` | 模擬成交、未成交、費稅、滑價及公司行動 |
| `AccountLedger` | 更新現金、持股、成本與每日估值 |
| `PerformanceAnalyzer` | 計算研究品質、績效、風險與策略差異 |
| `BacktestRepository` | 保存 run、每日輸入輸出、交易、淨值與 artifact |

## 必要比較組

所有策略使用相同期間、價格、成本與風控：

1. 固定 25 檔等權基準。
2. 純動能策略。
3. 規則事件＋動能。
4. LLM 固定資料事件＋動能。
5. Agent 工具研究事件＋動能。
6. Agent 工具研究＋動能＋防守配置。

不得只回測最終版本。每次比較要保存差異設定，避免同時改變資料、模型、風控與策略後無法判斷增益來源。

## Fail-closed 與防偏差規則

- 無法證明 `published_at`／`available_at` 的歷史資料不得進入正式回測。
- 每個工具都必須強制套用歷史 cutoff；歷史模式不得直接搜尋現在的網頁答案。
- 使用目前 150 檔名單重播過去時必須標記存活者／交易池選擇偏差。
- 缺少成交價格、公司行動、基準或費稅資料時停止正式結果，不得自行補值。
- 訓練、驗證與保留測試必須按時間分開；看過保留測試後修改策略，需要新的未見資料。
- LLM 多次重跑要全部保存並報告分散程度，不得只挑最好結果。
- 失敗日、風控拒絕與候選不足日必須保留在完整時間軸。
- 回測結果不能直接授權實盤或競賽送件，必須再通過固定版本前向驗證。

## 開發里程碑

1. **B0 契約與歷史時鐘**：建立 `BacktestRequest`、`BacktestRun`、交易日曆與 cutoff 測試。
2. **B1 時間點資料**：完成價格、事件、財報、交易池及公司行動的無未來資料 fixture。
3. **B2 成交與帳務**：完成可手算核對的成交、未成交、費稅、現金、持股與公司行動測試。
4. **B3 策略比較**：實作必要比較組、績效、回撤、成本、貢獻與不確定性分析。
5. **B4 Agent 評估**：加入事件測試集、工具行為、引用正確性、重跑穩定度與資源統計。
6. **B5 Agent 工具循環**：建立 `strategy-backtest` Skill、結構化工具與 Codex／Claude 一致性驗證。
7. **B6 前向驗證**：凍結通過版本，在未見資料上連續執行並保存完整失敗紀錄。

完成條件：相同保存輸入可重播出相同成交與績效；所有每日結果可追到版本與證據；沒有未來資料；至少完成必要比較組與敏感度測試；保留測試及前向驗證未顯示不可接受的風險後，才交給[自動化排程與報告 Agent](automation_reporting_agent_plan.md)。
