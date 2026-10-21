# 技術分析的整段歷史輸入

## 現況

2026-10-02 起，`build_analyst_brief(..., "technical")` 已接入整段歷史。

- 技術分析師閱讀完整的封存行情視窗，沿時間判讀價格與均線結構。
- 再用當期 `momentum` 核對目前位置。
- 只提供「上升／下跌／盤整」的市場狀態解讀，不執行買賣、排名或配置。

### 摘要版（研究試驗，未接入）

`TechnicalTrendHistoryTools.summary_for_symbol()` 的 `1.1-summary` 從完整、同 Snapshot 的 `1.0` 歷史重建：

- 固定 12 個錨點
- 收盤極值
- 最大量
- 最後 5 筆

去重後依日期排序。

- `observation_count` 仍是完整歷史的筆數，`rows` 只代表抽樣。
- `TechnicalTrendHistoryValidator.validate_summary` 可從原始行情重算摘要。
- 正式每日 brief 仍用 `for_symbol()` 的完整序列。
- 在同一個 Snapshot 的 150 檔中，有 12 檔的方向因摘要而改變，要等審查證據後才決定是否接入。
- 舊封存不改寫。

## 確定性資料契約

每檔技術輸入新增 `trend_history`。它由 `TechnicalTrendHistoryTools` 直接從已驗證的 `DecisionInputBundle.price_series` 建立，不經模型補值。

- 既有的 MomentumResult 與 AnalystReport 契約不變。
- 其他三位分析師不會收到這個欄位。
- 既有的封存行情與報告不改寫。

| 欄位 | 意義 |
| --- | --- |
| `schema_version` | 歷史輸入版本 `1.0` |
| `symbol`、`snapshot_id`、`decision_cutoff`、`bundle_hash` | 股票、快照、含時區的截止時間、輸入版本 |
| `source_series_sha256`、`evidence_ids` | 原始行情序列雜湊，以及該股票已驗證的來源證據 |
| `start_date`、`end_date`、`observation_count` | 此封存視窗的首末日期與筆數 |
| `rows` | 全部已提供的觀測，不另外截斷或選樣 |
| `rows[].trade_date`、`available_at` | 行情日期與當時可得時間 |
| `rows[].close`、`high`、`low`、`volume` | 來源價格與成交量 |
| `rows[].ma20`、`ma60` | Python 只用該日及之前的觀測計算均線，保留六位小數；暖機不足時為 `null` |
| `content_sha256` | 整份歷史輸入的 canonical hash |

### 「完整」的意思

- 指當前 bundle 收錄的整段歷史視窗，不是股票的全部上市歷史。
- 均線窗口以已收錄的行情觀測數計算，不合成缺漏的交易日。
- 價格沿用 DecisionInputBuilder 已選定的分析價格版本，不另外混入未調整價格。
- 歷史 `available_at` 若來自重建，仍須揭露重建的限制。

### Validator

`TechnicalTrendHistoryValidator` 以原始 bundle 重建全部內容。

- 就算自行重簽 hash，改值、改日期、股票錯配、少列資料，仍然過不了。
- cutoff 後的行情、缺時區、原始序列 hash 不一致，由 DecisionContext 拒絕。

## 模型分工與限制

**模型負責**

- 沿時間解讀：主趨勢如何形成，回檔或反彈有沒有破壞結構。
- 單日動能，或一次穿越均線，不能取代歷史判讀。
- 主要尺度沒有方向時，可判為盤整；資料不足時標 `unknown`。

**模型不做**

- 不自行產生均線斜率、持續天數、報酬率等數字。
- 不強制保持昨日的模型標籤。

**要記得的界線**

- 歷史路徑是事實；趨勢分類，以及趨勢是否延續，是推論。
- 加入歷史輸入，不代表分類已有客觀真值，也不保證未來股價方向或交易績效。
- 每日流程使用正式 builder。測試只執行技術分析，後續的交易與風險 Agent 不在這次測試範圍。

## 驗證

新增的單元測試涵蓋：

- 逐日均線的暖機與歷史邊界
- 完整行情覆蓋、短視窗
- 技術獨有輸入
- 重新簽 hash 後的竄改
- 股票錯配
- cutoff 後資料、時區缺失
- 來源版本衝突

歷史模型試跑位於 `artifacts/archive/experiments/technical_prompt_trials/full-path-20261002/`，與先前的「當日摘要版」分開封存。
