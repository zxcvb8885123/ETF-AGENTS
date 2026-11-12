# 曝險換股參數精簡（`exposure_rotation` v2）：設計計劃

狀態（2026-11-09）：**v2 程式與測試已實作（`decision/exposure.py`、`decision/allocation.py`、`tests/decision/test_exposure_rotation_v2.py`），`config/decision_policy.json` 已於 2026-11-09 17:50 切到 v2**（見 [status.md](../status.md#版本閘門啟用狀態)；切換前的 v1 樣板存於 `config/policy_versions/2026-11-09-exposure-rotation-v1.json`）。本檔的數字全部來自 `replay_reference_only` 的重算（固定封存的 Agent 意圖與 11-09 的真實持股重跑確定性程式），不是對 LLM 判斷的前瞻驗證；只有比賽期間的即時紀錄算證據。

## 1. 要解決的問題

人的疑問：曝險換股要設的數字太多（`max_swaps_per_run`、`max_rotation_fraction`、`release_margin`、`min_holding_days`、`min_new_conviction`、`beta_floor_by_stance`），是否多數根本不需要設，甚至讓 Agent 自己決定。

## 2. 調查結果（推論；模擬為 `replay_reference_only`，限制見第 3 節）

調查由一個唯讀子 Agent 做，腳本在暫存目錄（`sim2.py`、`sim3.py`，不在專案內）；**本檔作者未獨立重跑 `sim3.py` 的全部敏感度表**，只核對了下列行號。

起點：11-09 真實持股（26 檔、現金 18.6%、防守），價格固定，Agent 轉積極（`aggressive`／`lower`／`balanced`，滿檔 26）。

| 參數改動 | 10 日後含現金 beta | 判定 |
| --- | --- | --- |
| 基準（2 檔／0.20／0.10／5 日／下限 1.00） | 0.857，第 10 天 `no_eligible_victim` 停下 | — |
| `max_swaps_per_run` 1 | 0.632（仍在爬） | 綁定，近似線性 |
| `max_swaps_per_run` 3／4 | 0.816／0.817 | 不單調，比 2 檔差（第 4 節） |
| `max_rotation_fraction` 0.20（現值） | 同基準，全程沒出現 `rotation_fraction` 旗標 | **不綁定** |
| `max_rotation_fraction` 0.03／0.05 | 0.359／0.685 | 低值才綁定 |
| `release_margin` 0.02／0.05 | 0.857／0.857（與基準逐位相同） | 「換入者 beta ≥ 組合 beta + margin」不綁定 |
| `release_margin` 0.20／0.30 | 0.826／0.715 | 只透過「下限 − margin」（停止線）起作用 |
| 下限 aggressive 0.80 | 0.707，第 7 天達標 | 綁定 |
| 下限 aggressive 1.20 | 0.857（與 1.00 相同） | 高於可達 beta 就沒差 |
| `min_holding_days` 0 | 0.909（把剛換入的中 beta 股再換掉） | 弱綁定；設太大（20／60）則零換股 |
| `min_new_conviction` low | Policy 驗證拒絕 | 值域實際只剩 medium／high |
| `conviction_gap_swap` 開／關 | 0.858／0.858，gap 換股 0 次 | 被共用的 `max_swaps` 餓死 |
| 候選新股只有真實的 12 檔 | 0.616，第 5 天起 `no_eligible_fill` | 實務上最先綁定的是交易 Agent 的 buy 名單 |

另：現金一天內從 18.6% 降到 3～6%，不是曝險換股造成，是姿態對應的現金格點（`config/decision_policy.json` 的 `cash_ratio_range.grid`）；曝險換股只動名額。

## 3. 限制

- 價格固定；beta 由 11-09 Snapshot 的歷史算，不隨模擬日更新。
- 候選新股從 `no_trade` 人為補到 30 檔（依代號順序，沒有挑 beta）。
- 持股的 hold 信心全給 `medium`；換入後的股票在模擬裡沒有改標 hold（改標後結果差 < 0.01，見調查報告）。
- 回放工具 `evaluation/replay.py` 略過此閘門（notes 標 `exposure_rotation: ignored_in_replay`），模擬是自行修補函式後跑的，不是正式回放。
- 沒有模擬轉空：防守姿態的下限是 `null`，曝險換股不動作，轉空只靠現金格點與 Agent 自己的減碼。

## 4. 為什麼「調大檔數」沒用（事實＋推論）

- **`no_eligible_victim` 的來源**（事實，`src/etf_agent/decision/exposure.py:623-626`、`allocation.py:961-971`）：換出者必須是交易 Agent 標 `hold`、信心非 `high`、可交易、持有天數夠，且 beta 低於組合 beta。基準第 10 天，26 檔中 17 檔是模擬換入、4 檔 `add`、1 檔 `trim`，只剩 3 檔 `hold`，它們的 beta（0.89／1.35／1.67）都高於組合 beta 0.857，候選池為空。
- **結構限制**（推論）：標 `add`／`buy`／`trim` 的持股永遠不是換出者（設計如此，見 [exposure_rotation_plan.md](exposure_rotation_plan.md) 第 6 節）。4 檔低 beta 的 `add` 持股佔 NAV 約 26～31%，加上約 3% 現金，把可達的含現金 beta 封在約 0.87。所以積極下限 1.00（觸發線 0.90）在這個持股結構下達不到，參數怎麼調都一樣。
- **3、4 檔比 2 檔差**（推論）：兩種設定 10 天內的 17 對換出／換入名單完全相同；差在第 1 天。4 對配對的賣加買約 30% NAV，撞到 `max_turnover_rate`（0.3）與現金檢查，`allocation.py` 的復原迴圈（約 430 行）逐一取消沒拿到買單的換入者，最後只剩 1 對，釋出的現金依信心權重灌進低 beta 的 `add` 持股，這些之後再也換不出去。是三個閘門（曝險換股、換手限制、信心權重）交互的路徑相依，不是任何單一參數的單調函數。`max_rotation_fraction` 0.10 加 4 檔反而達標（0.904，第 7 天），就是因為它把第 1 天壓回 2 對。

## 5. 方案與推薦

### 5.1 精簡參數（推薦）

| 欄位 | 做法 | 理由 |
| --- | --- | --- |
| `max_rotation_fraction` | 刪除 | 現值 0.20 在 `max_swaps_per_run`＝2、單股上限 10%（`config/decision_rules.json`）下數學上不會觸發；換手由 `turnover_fit` 把關 |
| `min_new_conviction` | 刪除，直接沿用 `rotation.min_new_conviction` | 驗證器已強制它不低於 `rotation` 的值（`exposure.py:134-142`），`_conviction_targets` 另用 `rotation` 的門檻擋 `ROTATION_CONVICTION`（`allocation.py:1208` 附近），兩處本來就必須一致 |
| `release_margin` | 併進停止線：改為 `beta_target_by_stance`（aggressive `0.90`、neutral `0.55`、defensive `null`），觸發與停止都是 beta ≥ 目標 | 現行只有「下限 − margin」起作用，文件也承認容易誤讀；v1 的觸發線就是 `下限 − margin`，數字上與 v2 完全對應 |
| 換入者 beta 門檻（`組合 beta + release_margin`） | v2 改為「換入者 beta 必須高於組合 beta」，不另設數字 | margin 0.02～0.05 的結果與 0.10 逐位相同；**門檻改為 0 的結果沒有試過**，實作前要用同一腳本驗證 |

v2 欄位：`version`（`v2`）、`beta_window_days`、`beta_target_by_stance`、`max_swaps_per_run`、`min_holding_days`、`hold_conviction`，選配 `conviction_gap_swap`，另有流程綁入的 `held_since_by_symbol`。人設的數字從 8 個減為 5 個：視窗、兩個停止線、步幅、天數。

### 5.2 保留不動

- `max_swaps_per_run`、`min_holding_days`：人設閘門。`max_swaps_per_run` 另被 `rotation.max_swaps_per_run`（上線值 5）封頂。
- `beta_window_days`：統計視窗，不是策略旋鈕。
- `conviction_gap_swap`：本計劃不改。已知它與曝險換股共用 `max_swaps_per_run`，曝險先跑，所以常被餓死；要不要給它獨立上限是另一個設計決定。

### 5.3 讓 Agent 自己決定（不建議現在做）

- **直接給數字**：違反「Agent 不輸出任何數字」總則，數字無法由 Finalizer 重算，否決。
- **風險 Agent 多給列舉 `exposure_appetite`（none／low／high），程式對映成停止線與步幅**：可行，做法與 `cash_grade → grid` 相同。但它只替換「姿態」這一維的對映，姿態本來就是列舉對映數字（`beta_floor_by_stance`），人設數字沒有變少；還要擴充風險 Agent 的證據規則、Finalizer 重算、回放標 ignored。
- **交易 Agent 給每檔 `rotation_candidate`**：不建議。交易 Agent 分批、看不到整體持股與別批新股，會讓看不到全局的角色決定整體曝險。
- 結論：沒有 ≥ 10 個決策日（`calibration.MIN_DECISION_DAYS`）的實跑證據顯示姿態對映不夠用之前，不加列舉。

### 5.4 結構規則（不是本計劃範圍，只記錄）

真正限制進攻可達性的是結構：`add` 持股不能被換出、交易 Agent 的 buy 名單太短。是否允許 `add`（信心 medium）持股當換出者、是否讓信心差換股有獨立上限，都會改變行為，須另立計劃與新版本。

## 6. 相容策略

- 所有 v1 欄位都在 `GATE_REQUIRED_FIELDS`（`exposure.py:33`），缺欄位即拒（`:111-113`）。直接刪欄位會讓 11-09 之後封存的 Policy 重建失敗。
- 推薦：**新增 `version: v2`，v1 路徑逐位元保留**（版本閘門總則：沒有該版本的舊 Policy／舊 run 維持原重建行為）。`config/policy_versions/2026-11-09-before-review.json` 保留不動。
- 替代：把待刪欄位改成選填、缺少時走 v2 語意。程式較少，但同一個 `version: v1` 會有兩種語意，不推薦。
- 策略反思的禁區前綴 `exposure_rotation`（`strategy_reflection/contracts.py:163` 附近）不用改，v2 仍由人設定、反思不得改。
- 回放仍略過此閘門，不受影響。

## 7. 影響位置（實作時才動；行號為 2026-11-09 工作區）

| 類別 | 位置 |
| --- | --- |
| 驗證與白名單 | `src/etf_agent/decision/exposure.py`：`GATE_REQUIRED_FIELDS`（33 行）、形狀驗證（103 行起，含 `release_margin`／`max_rotation_fraction`／`min_new_conviction` 各段與「下限必須大於 margin」檢查） |
| 換股配對 | `exposure.py` 的 `exposure_rotation_plan`（537 行起：`cap`、`margin`、`min_tier`、`target`、`fill_pool` 條件）；`allocation.py` 的 `_exposure_rotation`（910 行起，`Fill` 的信心仍要保留） |
| 設定 | `config/decision_policy.json`（`position_sizing.exposure_rotation`，開啟 v2 才改，見第 9 節） |
| 文件 | `docs/decision-contracts.md`（曝險換股欄位與規則段）、`docs/status.md`（版本閘門啟用狀態）、`docs/glossary.md`（`exposure_rotation` 列）、`docs/plans/exposure_rotation_plan.md`（加註 v2 取代說明）、`README.md`、`AGENTS.md`（曝險換股段，目前寫 v1） |
| 測試 | `tests/decision/test_exposure_rotation.py`、`tests/decision/test_exposure_rotation_e2e.py`（v1 測試全保留，另加 v2 版本） |

## 8. 測試（`unittest.TestCase` 風格）

- v2 欄位白名單：缺欄位、多出 v1 欄位（`release_margin` 等）、`beta_target_by_stance.defensive` 非 null 都拒絕；目標必須 aggressive ≥ neutral。
- v1 的 Policy、舊 run 重建逐位元不變（雜湊相同）。
- 等價測試：用 v1（下限 1.00／0.65、margin 0.10）與 v2（目標 0.90／0.55）對同一份輸入，換股結果相同（換入者 beta 門檻除外，見下一條）。
- 換入者 beta 門檻改為「高於組合 beta」：覆蓋剛好相等、略高、略低三種邊界；並用 11-09 輸入比對與 margin 0.10 的差異，確認不是沉默改變行為。
- `min_new_conviction` 沿用 `rotation` 的值：`rotation` 為 high 時，medium 新股不會被換入也不會只賣不買。
- 既有的 fail-closed 分支照舊：缺 hold 信心、缺帳本歷史、`held_since_by_symbol` 與持股不符一律拒絕。

## 9. 驗證與回滾

1. 實作後用 `sim2.py`／`sim3.py` 的同一組情境重跑 v1 與 v2，確認基準軌跡相同（beta 0.11→0.86 的路徑），差異只出現在第 5.1 節標明的「門檻改為 0」。
2. `./start.sh` 排程不動；是否把 `config/decision_policy.json` 切到 v2 由人決定。切換前先保留 v1 快照於 `config/policy_versions/`。
3. 切換後至少觀察 10 個決策日（`calibration.MIN_DECISION_DAYS`）；明顯變差由人把該欄位換回 v1 即回滾。
4. 不自動套用、不自動上傳；本計劃不改變任何競賽限制、費稅或硬性規則。

## 10. 需要人決定

1. 是否接受以 `v2` 刪欄位、保留 v1 路徑（推薦），還是改為選填欄位。
2. 停止線直接寫 0.90／0.55（推薦，一個欄位），還是維持「下限 − margin」兩個欄位。
3. 換入者 beta 門檻改為「高於組合 beta」是否接受（實作前先驗證）。
4. 是否另開計劃討論結構規則：`add` 持股能否當換出者、信心差換股的獨立上限。
5. 是否要風險 Agent 的 `exposure_appetite` 列舉：建議不要，除非先有 ≥ 10 個決策日的實跑證據。

## 11. 實作紀錄與已知行為差異（2026-11-09）

- 實作：`exposure.py` 新增 `EXPOSURE_ROTATION_V2`、`GATE_FIELDS_V2`、`gate_is_v2`、`_validate_beta_targets`；`exposure_rotation_plan` 依版本分支（v2 沒有 `cap`、`margin` 為 0、換入者 beta 嚴格大於組合 beta），並新增 `min_new_conviction` 參數，由 `allocation.py` 的 `_exposure_rotation` 傳入 `rotation.min_new_conviction`。v1 程式路徑未改動。
- v1 測試只改一處：原本拿 `v2` 當「不合法版本」的案例改成 `v3`。
- 與調查報告不同之處（測試發現）：v1 的 `max_rotation_fraction` 也限制**信心差換股**。測試世界（單股上限 100%）裡，v1 擋下 1101（約 50% NAV）的信心差換股，v2 不擋（`test_gap_swap_is_no_longer_bounded_by_a_rotation_fraction`）。正式設定的單股上限是 10%、`max_swaps_per_run` 是 2，所以兩對加起來最多約 20% NAV，和 v1 的 0.20 上限等價；但**這個等價取決於 `max_swaps_per_run × 單股上限`**：調高換股檔數、或持股因漲跌超過單股上限時，v2 的賣出量不再有獨立的比例上限，只剩 `turnover_fit`（`max_turnover_rate` 0.3）。
- 刪除欄位的同時，`release_margin` 的「換入者 beta ≥ 組合 beta + margin」改為「高於組合 beta」。調查中 margin 0.02～0.10 的結果逐位相同，但**門檻改為 0 沒有用 11-09 真實輸入驗證過**（第 5.1 節要求的驗證尚未做）；單元測試只證明「換入者 beta 都 ≥ 組合 beta + 0.10 時 v1 與 v2 結果相同」。
- 已驗證（`replay_reference_only`）：用 11-09 真實持股、積極／`lower`／`balanced`、候選新股 30 檔、10 天的同一組情境（`sim4.py`）跑 v1 與 v2，每天的含現金 beta（0.1057→0.8572）、現金比例、訂單數、換出的股票名單逐日完全相同，連第 10 天 `no_eligible_victim` 都一樣。所以「換入者門檻改為高於組合 beta」在這組情境沒有改變行為；仍只是一組情境，不代表所有輸入。
- 切換（2026-11-09 17:50，人決定）：`policy_id` 改為 `strategy-2026-11-09-exposure-v2-1`、`available_at` 為真實切換時間（不回填）；觸發與停止線數值不變（進攻 0.90、中性 0.55）。回放仍略過此閘門。
- 回滾：把 `exposure_rotation` 區塊換回 `config/policy_versions/2026-11-09-exposure-rotation-v1.json` 的內容並更新 `policy_id`／`available_at`；v1 程式路徑仍在。

## 12. v3：防守姿態的 beta 停止線

動機（2026-11-09 實況）：大漲日交易池等權 +5.51%，我們的 NAV 只有 +0.40%，持股 beta 只有 0.13。v2 的 `beta_target_by_stance.defensive` 只能是 null，防守姿態不觸發曝險換股，所以低 beta 持股一檔都沒換。是否在防守姿態也補 beta 是策略取捨，由人決定；程式只提供能力。

- 實作：`exposure.py` 新增 `EXPOSURE_ROTATION_V3`、`gate_is_v3`、`gate_has_targets`（v2／v3 共用欄位集合與配對規則）；`_validate_beta_targets` 在 v3 允許 `defensive` 填數字（值域與 aggressive／neutral 相同：大於 0 且不超過 2），並要求 進攻 ≥ 中性 ≥ 防守（有填的比較）。`exposure_rotation_plan` 對 v3 走 v2 的全部分支，`defensive` 為 null 時 `skipped.exposure = stance_not_triggered`，與 v2 逐位相同。
- 不變：欄位白名單（沿用 v2，不加回已刪的欄位）、`max_swaps_per_run`、換入者 beta 嚴格高於組合 beta、`rotation.min_new_conviction`、與 `drawdown_derisk` 互斥（減碼日略過）、不只賣不買、賣單由程式標 `exit` 且不改寫交易 Agent 意圖、`turn_assessment` 等列舉與姿態不設對應。`team_inputs` 結構沒有變，維持 2.7（`exposure_review.policy.version` 只回寫閘門版本字串）；v1／v2 的 Policy 重建路徑逐位元不變。
- 策略反思：整個 `exposure_rotation` 已在 `FORBIDDEN_PREFIXES`，v3 欄位同樣不可調。
- 啟用：由人把正式設定的 `exposure_rotation.version` 改成 `v3` 並把 `beta_target_by_stance.defensive` 填成數字；尚未開啟，未用歷史輸入驗證防守目標值（例如 0.35）的實際效果。

## 後續：曝險讓位配對（2026-11-10，已實作、尚未開啟）

`agent_swap` v2 下 `exposure_rotation` 的換股步驟本來就停用；`agent_swap.exposure` 的曝險配對由換股提名走，不經本檔的換股規則。見 [agent_led_exposure_plan.md](agent_led_exposure_plan.md)。
