# AI CUP 2026 Agent 開發架構

## 整體流程

```text
資料庫
  → 歷史／當日 ResearchSnapshot
  → 策略
      ├─ 事件策略（目前開發）
      └─ 動能策略（下一階段）
  → 進攻股／防守股
  → 風控與買賣
      ├─ 實盤決策 → 每日報告
      └─ 模擬成交 → 回測績效報告
```

實盤與回測共用策略、權重配置及風控規則。回測不得另外實作一套選股邏輯，只能以歷史快照取代當日快照，並由模擬成交器取代實際下單。

## 模組

| 模組 | 輸入 | 責任 | 輸出 |
| --- | --- | --- | --- |
| 資料庫 | 官方交易池、行情、事件、帳戶與 ETF 基準 | 保存原始資料、時間、來源及固定版本快照 | `ResearchSnapshot` |
| 策略 | `ResearchSnapshot` | 整合事件與動能，分出進攻股及防守股 | `StrategyProposal` |
| 風控與買賣 | `StrategyProposal`、目前持股與現金 | 配置權重、計算交易、費稅及檢查競賽限制 | `ApprovedDecision` 或 `RejectedDecision` |
| 回測 | 歷史 `ResearchSnapshot`、`ApprovedDecision`、行情與交易成本設定 | 依交易日重播決策、模擬成交與出場、保存資產曲線及交易紀錄 | `BacktestResult` |
| 報告 | `ApprovedDecision` 或 `RejectedDecision` | 產生候選、持股、交易、理由與風控結果 | 每日報告與交易書 |

## 資料流

```text
                         ┌→ DailyReport
ResearchSnapshot
  → StrategyProposal
  → ApprovedDecision / RejectedDecision
                         └→ SimulatedExecution
                              → BacktestResult
```

所有物件共用 `run_id`、資料日期與建立時間。報告只讀取同一次決策結果，確保報告內容與實際交易一致。

回測依交易日建立獨立 `run_id`，並以 `backtest_id` 串連整段期間。每次決策只可讀取當日 `decision_cutoff` 前已發布且已取得的資料；成交使用下一個可交易時點的價格並計入手續費、交易稅與滑價，避免前視偏誤。`BacktestResult` 至少包含累積報酬、最大回撤、勝率、換手率、現金比率、交易紀錄及相對等權重基準績效。

## 回測流程

1. 固定回測期間、交易池版本、策略設定、初始資金與交易成本。
2. 逐日重建 08:55 前可取得的 `ResearchSnapshot`。
3. 呼叫正式策略與風控模組產生決策，不允許讀取未來事件或價格。
4. 以後續第一個可交易價格模擬成交，套用停損、確認失敗、時間停損與最長持有期限。
5. 更新持股、現金、資產曲線與交易紀錄，再進入下一交易日。
6. 輸出 `BacktestResult`，並保存輸入快照與設定，確保結果可重播。

## 開發順序

1. 資料庫：官方 150 檔與兩年行情已完成。
2. 事件策略：目前開發。
3. 動能策略及進攻／防守分類。
4. 風控與買賣。
5. 回測重播、模擬成交與績效報告。
6. 每日報告。

事件規則與評分細節見 [事件策略 V1](event_strategy_v1.md)。
