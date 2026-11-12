# 反彈參與閘門 v2：只算事實、由風險 Agent 單獨決定

狀態（2026-11-10）：**已於 01:37 開啟**（人決定；`policy_id` strategy-2026-11-10-rebound-v2-1）。正式設定的 `rebound_participation` 換成 v2 欄位（`version`、`min_coverage`、`by_state`，值沿用 v1，仍只有 confirmed 實質放寬）；回滾＝把 `config/policy_versions/2026-11-10-rebound-v1.json` 的該區塊換回去。v2 沒有重放驗證，開啟是人承擔風險的決定，不代表驗證通過。

## 為什麼要有 v2

v1 的 `turn_state` 用一組固定數字（池報酬 ≥ 3%、站回 MA5 ≥ 30%、第 2～3 日確認…）替風險 Agent 先判斷「這是不是真反彈」。這些數字是以只有極少數大漲日的歷史（n ≤ 3）擬合出來的，過擬合風險高（見 [rebound_participation_plan.md](rebound_participation_plan.md)）。專案原則是「確定性歸程式、不確定性歸 LLM」：是否轉向是判斷，不該由死數字鎖住。v2 把判斷還給風險 Agent，程式只守資料品質與硬上限。

## 設計

- 程式只整理事實：最近 5 日的池等權報酬、站回 MA5 比例、20 日新低比例、regime、覆蓋率與 `data_status`（沒有 `state`、沒有轉向門檻）。風險 Agent 本來就看得到 `turn_facts` 的多日事實，v2 的 `recent_path` 補上逐日路徑。
- 有效狀態 `effective_state` 只由風險 Agent 的 `turn_assessment` 對映：`no_turn_evidence`→none、`turn_unconfirmed`→candidate、`turn_confirmed`→confirmed。
- 程式只做兩件壓回 none 的事（缺資料不放行）：`data_status` 為 unavailable（當日覆蓋或報酬樣本覆蓋 `return_coverage`（當日與前一交易日都有收盤者占交易池）< `min_coverage`、行情缺漏、regime 算不出、bar 晚於最近交易日），或 `drawdown_control.stance_floor` 為 defensive。
- 對映列 `by_state`、現金放寬、換手上限、換股的上限（換手、檔數、現金）全部沿用 v1，程式硬上限不變。

## 與 v1 的行為差異（開啟前要知道）

1. **regime 不是 bear 不再擋。** v1 只在 regime 為 bear 時才可能升級；v2 只看資料是否可用。Agent 在非空頭 regime 填 `turn_confirmed` 也會放寬。
2. **沒有「兩邊都同意」的第二把鑰匙。** v1 程式不同意就不放寬；v2 只剩 Agent 一把。判斷錯時，保護只剩 `by_state` 的放寬幅度與換手、現金、檔數硬上限。
3. **沒有候選過期與退回的記憶。** v1 的 candidate 第 4 日未確認即回 none；v2 每天都由 Agent 依當日事實重判。

## 已知缺口（使用者 2026-11-10 決定先不處理）

`turn_assessment` 成為唯一鑰匙後，驗證仍只核列舉值域，不要求引用證據、不驗文字品質。若之後要補：`turn_assessment` 非 `no_turn_evidence` 時必須引用證據，這會改風險 Agent 契約，需另開閘門版本。

## 實作範圍

`decision/rebound.py` 依 `version` 分派（v1 函式本體不動）；`team_inputs` 仍為 2.8、欄位名仍是 `turn_state`（v2 的 `schema_version` 為 2.0、無 `state`）；風險 Agent 簡報與 Skill 參考檔（cash-stance.md）有 v2 專用說明。測試在 `tests/decision/test_rebound_participation_v2.py`；v1 的測試不變（只有「version 只允許」那一條把不合法的範例由 v2 改成 v3）。

## 證據等級

v2 沒有重放驗證，也不比 v1 更有證據：它只是把判斷移到 Agent。有沒有用只有比賽期間的即時紀錄能回答。

## 後續：曝險讓位配對（2026-11-10，已實作、尚未開啟）

`agent_swap.exposure`（見 [agent_led_exposure_plan.md](agent_led_exposure_plan.md)）讓風險 Agent 的 `exposure_view` 在 `agent_swap` v2 下決定取用幾對曝險配對；本檔的 `turn_assessment` 放寬與它互不取代，是否退場沿用「先觀察滿 10 個決策日」。
