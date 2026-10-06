# 技術分析的整段歷史輸入

2026-10-02 已接入 `build_analyst_brief(..., "technical")`。技術分析師閱讀完整封存行情視窗，沿時間判讀價格與均線結構，再用當期 momentum 核對目前位置。只提供上升／下跌／盤整的市場狀態解讀，不執行買賣、排名或配置。

## 確定性資料契約

每檔技術輸入新增 `trend_history`，由 `TechnicalTrendHistoryTools` 直接從已驗證 `DecisionInputBundle.price_series` 建立，不經模型補值。既有 MomentumResult 與 AnalystReport 契約未變，其他三位分析師不收到此欄位。既有封存行情與報告不改寫。

| 欄位 | 意義 |
| --- | --- |
| `schema_version` | 歷史輸入版本 `1.0` |
| `symbol`、`snapshot_id`、`decision_cutoff`、`bundle_hash` | 股票、快照、含時區截止時間與輸入版本 |
| `source_series_sha256`、`evidence_ids` | 原始行情序列雜湊與該股票的已驗證來源證據 |
| `start_date`、`end_date`、`observation_count` | 此封存視窗的首末日期與筆數 |
| `rows` | 全部已提供觀測，不另外截斷或選樣 |
| `rows[].trade_date`、`available_at` | 行情日期與當時可得時間 |
| `rows[].close`、`high`、`low`、`volume` | 來源價格與成交量 |
| `rows[].ma20`、`ma60` | 僅使用該日及之前觀測，由 Python 計算的均線；保留六位小數，暖機不足為 `null` |
| `content_sha256` | 整份歷史輸入的 canonical hash |

「完整」指當前 bundle 收錄的整段歷史視窗，不代表股票全部上市歷史。均線窗口以已收錄行情觀測數計算；不合成缺漏交易日。來源價格沿用 DecisionInputBuilder 已選定的分析價格版本，不另混入未調整價格。歷史 available_at 若來自重建，仍須揭露重建限制。

`TechnicalTrendHistoryValidator` 以原始 bundle 重建全部內容。自行重簽 hash 仍不能通過改值、改日期、股票錯配或少列資料的驗證。cutoff 後行情、時區缺失及原始序列 hash 不一致由 DecisionContext 拒絕。

## 模型分工與限制

技術分析師沿時間解讀主趨勢如何形成、回檔或反彈有沒有破壞結構；單日動能及一次穿越均線不能取代歷史判讀。模型可以解讀形態，不自行產生均線斜率、持續天數、報酬率等數字，也不強制保持昨日模型標籤。主要尺度無方向時可盤整，資料不足標 `unknown`。

歷史路徑是事實，趨勢分類及是否延續是推論。加入歷史輸入不代表分類已獲得客觀真值，也不保證未來股價方向或交易績效。每日流程使用正式 builder，測試只執行技術分析；後續交易與風險 Agent 不在此次測試範圍。

## 驗證

新增單元測試涵蓋逐日均線的暖機與歷史邊界、完整行情覆蓋、短視窗、技術獨有輸入、重新簽 hash 後的竄改、股票錯配、cutoff 後資料、時區缺失與來源版本衝突。歷史模型試跑位於 `artifacts/technical_prompt_trials/full-path-20261002/`，與先前當日摘要版分開封存。
