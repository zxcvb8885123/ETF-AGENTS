# 風險 Agent 現金姿態：讓「越新的資料」分量更重

狀態（2026-11-09）：**閘門 `position_sizing.cash_stance_recency` v1 已實作、測試通過，正式設定尚未開啟**（`config/decision_policy.json` 沒有此欄位）。契約見 [decision-contracts.md](decision-contracts.md#現金姿態的資料新舊cash_stance_recency)，啟用狀態見 [status.md](status.md#版本閘門啟用狀態)。

## 起因（事實）

2026-11-09 07:00 的 run：交易 Agent 想買 16 檔，只成交 1 買 1 賣。風險 Agent 選「防守／middle」，理由主要是走勢類證據（跌破月線季線、恐懼貪婪偏恐懼）；國安基金護盤與隔夜美股強勢被寫成「只當背景」。簡報沒有任何資料新舊的標示，Skill 也沒有取捨規則。

## 初版計劃的錯誤（已更正）

初版想依 `available_at` 分新舊，不可行：全市場新聞與隔夜資料的 `available_at` 是抓取時間，全部落在截止前幾秒。要分的是**內容時間**（新聞 `published_at`、時段 `session_end`、資料日），這些資料包裡本來就有。

## 已實作的內容

- `decision/recency.py`：`evidence_recency` 依 bundle 重算證據時間表，分 `after_latest_close`／`latest_session`／`accumulated` 三組，界線為台股最近交易日 13:30（與隔夜背景同一條線）；不含任何新聞文字。
- 風險 Agent 簡報首鍵附該時間表；輸出另填列舉 `recency_reconciliation`；最新一組非空時須引用其中一個證據（程式驗證）。
- 不升 `team_inputs` 版本；Finalizer／`apply_trade_decision` 以 bundle 重算再驗；回放略過此閘門。
- 不改任何現金格點、上限、Guard、回撤下限，也不讓訊號決定現金姿態。
- 以 2026-11-09 封存資料重算：最新一組含 9 則新聞（0.7～21.6 小時）與 8 個隔夜標的（50.0～64.7 小時；日經／KOSPI 是台北 14:30 收盤，晚於台股收盤 1 小時，歸最新一組）。

## Skill 取捨規則（2026-11-09 人決定採強版，只限事件與新聞）

最近收盤後才出現的事件與新聞，走勢還沒反映，判斷「情況有沒有改變」時預設以它們為主，利多利空同樣適用，不採用須說明理由。隔夜行情仍只是背景；累積走勢不因較舊而作廢。風險：護盤失敗、續跌時可能多買（上界估計：11-09 若改選 `defensive/lower`，可動用現金約 NAV 的 3.6%，與 Agent 目前已有的權限相同）。沒有足夠歷史可驗證，只能看開啟後的即時紀錄。

## 尚未決定（需人）

1. **何時開啟**：在 `config/decision_policy.json` 的 `position_sizing` 加 `"cash_stance_recency": {"version": "v1"}`；開啟後至少觀察 10 個決策日（`calibration.MIN_DECISION_DAYS`），每週檢視列舉值與姿態；明顯變差由人刪掉該欄位即回滾。

## 為什麼不做程式偵測「護盤事件」

關鍵字偵測等於讓新聞文字決定現金下限，違反「現金下限不由訊號決定」。且歷史上「護盤／禁空」只出現在 2 個決策日，遠低於 10 個決策日的樣本門檻，無法離線驗證。`docs/cross_market_study.md` 也只支持「隔夜訊號預測開盤跳空」，不支持整天方向。真正的證據只能來自開啟後的即時紀錄。

## 驗證

`tests/decision/test_cash_stance_recency.py`：閘門形狀、時間表計算（cutoff 後資料拒絕、缺時區、缺內容時間、界線）、列舉與引用驗證、無閘門時欄位被拒、Schema／簡報／提示詞、修正循環、`apply_trade_decision`、Finalizer 接線與回放；另以 11-09 封存資料核對（封存不在時略過）。
