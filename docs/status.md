# 目前狀態與限制

這一頁回答一件事：哪些功能現在真的在跑，哪些只做了一半，哪些還沒接。功能或資料來源的狀態改變時，請更新這一頁。

三種狀態的意思：

- **已接入**：程式已實作，且每日流程會用到。
- **部分**：有些做了、有些沒做，或做好了但還沒在真實排程中驗證，備註欄會寫缺什麼。
- **尚未**：沒有做，或只有設計想法。

資料來源要經過使用者核准才會進正式計算；沒核准或抓不到時，該項輸出「不可用」，不會用猜的補上。

## 資料

| 功能 | 現況 | 備註 |
| --- | --- | --- |
| 150 檔交易池行情（官方價量） | 已接入 | 帳本與決策用 TWSE／TPEx 官方原價；研究指標用調整價。缺官方價時退回調整價 |
| 月營收、重大訊息、財報（官方） | 已接入 | 見[資料來源清單](data_sources_inventory.md) |
| FinMind 現金流量表與單季財報 | 已接入 | 現金流量表要與官方損益表相符才用；單季財報經官方金額核對後，可補去年同期累計財報（`cli/fundamental_repair.py`） |
| 基本面期別逐檔判斷 | 已接入 | 每檔用「截止前最新可得的一季」。舊做法在 11/03 實測有 132／150 檔被誤判缺財報；舊 run 維持舊算法，見[架構](architecture.md#基本面期別) |
| 全市場新聞（中央社、自由時報） | 已接入 | 2026-10-23 核准。至少 5 則、2 家媒體才算可用，否則改用恐懼貪婪指標；新聞失敗不會讓整天失敗 |
| 台股恐懼貪婪指標 | 已接入 | 期交所 VIX、FinMind 融資 5 日變化與融資水位（已不再用 Put/Call 比），見[說明](market_fear_greed.md)。VIX 依使用者決定以非商業研究用途核准 |
| 融資水位（恐懼貪婪第 4 項） | 部分 | 程式與測試已完成，核准已寫入設定，尚待實跑抓取驗證，見[說明](margin_level_signal_plan.md) |
| FinMind 個股新聞 | 已接入 | 只進公司事件，不代表全市場情緒；只用標題、只用白名單媒體。2026-10-27 使用者核准（自行判斷，非媒體書面同意），見[來源核對](source_audit/2026-10-25_finmind_stock_news.md) |
| 隔夜市場背景 | 部分 | Yahoo 美股與日韓指數、VIX 已核准；台指期夜盤的日期時間格式尚待實抓驗證，不符時夜盤維持不可用。只給風險 Agent 與報告當背景，見[說明](overnight_market.md) |
| 價格日退回旗標 | 已接入 | 最新交易日有檔缺行情時，退回前一個全池齊全日並留下資訊旗標，不影響可用性，見[架構](architecture.md#快照交易日退回) |
| 主辦方資料包對帳 | 已接入 | 每日跑 Agent 前先核對帳本持股與現金，對不上就停止，見[日常操作](operations.md) |

## 決策與風控

| 功能 | 現況 | 備註 |
| --- | --- | --- |
| 每日決策鏈（分析團隊、多空、交易、風險） | 已接入 | 見[每日決策架構](architecture.md) |
| 中文決策報告 | 已接入 | 可由封存重建並驗證 |
| 同日賣款可買替補、現金分配、單股上限空間、跳空預留 | 已接入 | 2026-11-03 起的規則，2026-11-04 起的正式 run 已帶這些閘門（例如 11-06 run 出現現金分配取消旗標）；舊 run 不受影響，見[決策細節](decision-details.md) |
| 持股滿檔換股、強制換股、產業與換手上限、再買冷卻、防守減碼 | 已接入 | 同上；個別分支（換手縮減、強制換股）是否觸發見各 run 的旗標，再買冷卻名單與防守減碼已在 11-04 起的 run 實際綁定／觸發 |
| 主辦方結果當結算依據 | 已接入，尚未實跑 | 資料包標示當日委託作廢時，帳本結算為無成交；找不到本隊資料包時維持純模擬 |
| 配置與輪動 v2 | 部分 | 低波動偏好強度與輪動已有程式；設計文件見[輪動 v2](sizing_rotation_v2_design.md) |
| 基本面成長訊號 | 部分 | 已有訊號品質檢查；完整可比性設計見[成長訊號](fundamental_growth_signal_plan.md) |

### 版本閘門啟用狀態

這是閘門啟用狀態與目前數值的**唯一出處**（其他文件只連結這裡）。2026-11-09 依 `config/decision_policy.json`（`policy_id: strategy-2026-11-09-turnfacts-v1`，`available_at: 2026-11-09T16:10:00+08:00`）、`config/decision_rules.json`（`decision-rules-2026-11-03b`）與 `config/strategy_reflection.json` 核對；改設定時請同步更新本表。各閘門的行為與條件見[決策鏈契約](decision-contracts.md)，名稱說明見[名稱對照](glossary.md)。

本次只更新策略識別與真實可得時間，不變更任何策略門檻或閘門開關。審查前樣板原樣保存在 `config/policy_versions/2026-11-09-before-review.json`；該檔是當日審查前的內容快照，**不是**其中舊 `available_at` 日期當時的策略還原。封存 run 仍讀自己保存的 Policy，不改寫歷史。用新版策略重跑較早 cutoff 僅能是明確標示的反事實評測。

已開啟：

| 閘門 | 目前值 |
| --- | --- |
| `position_review` | `version v1`、`loss_alert 0.15`、`retrace_alert_20d null`、`gain_alert null`、`context v1` |
| `position_sizing.method` | `conviction_volatility_v2`，`volatility_exponent 0`，乘數 high 1.5／medium 0.6／low 0.3，`volatility_floor 0.01` |
| `selection_ranking` | `v1`（`selection_tiebreak` 未設定，被名次取代） |
| `limit_scaled_conviction` | `{version v1, alpha 0.6}` |
| `positions_by_breadth` | concentrated 22／balanced 26／diversified 30 |
| `held_add_priority`＋`rotation` | `v1`；`max_swaps_per_run 5`、`min_new_conviction medium` |
| `rotation.forced_swap` | `version v1`、`min_new_conviction high`、`max_swaps_per_run 2`、`confirmation v1` |
| `cash_buffer_floor` | `v1` |
| `cash_ratio_range` | `version v1`；`grid` aggressive 3／5／8%、neutral 8／10／13%、defensive 15／18／21%；`max_daily_raise 0.03`；`anchor` 由 pipeline 每次綁定 |
| `exposure_rotation` | `version v1`（2026-11-09 開啟，見[計劃](exposure_rotation_plan.md)）；`beta_window_days 60`；`beta_floor_by_stance` aggressive 1.00／neutral 0.65／defensive null（實際觸發線＝下限 − `release_margin`：進攻 0.90、中性 0.55）；`release_margin 0.10`；`max_swaps_per_run 2`；`max_rotation_fraction 0.20`；`min_holding_days 5`；`min_new_conviction medium`；`hold_conviction v1`；`conviction_gap_swap v1`；`held_since_by_symbol` 由 pipeline 依帳本綁定；`team_inputs` 2.7 |
| `cash_stance_recency` | `{version v1}`（2026-11-09 開啟；風險 Agent 簡報首鍵附證據時間表，另填 `recency_reconciliation`） |
| `cash_buffer_by_stance` | aggressive 3%／neutral 10%／defensive 20%（`cash_ratio_range` 開啟時不影響配置） |
| `cash_fit_order` | `v1`，`cash_fit_min_fill_ratio 0.5` |
| `high_conviction_cash_priority` | `v1`（2026-11-06 新增） |
| `held_weight_budget` | `v1`（2026-11-08 新增） |
| `cash_ceiling_fix`、`stock_limit_headroom`、`buy_gap_reserve`（`buy_gap_reserve_rate 0.03`）、`sector_fit`、`turnover_fit` | 皆 `v1`；`insufficient_funds_policy void_batch` |
| `reentry_cooldown` | `{version v1, days 3}` |
| `drawdown_derisk` | `{version v4, max_derisk_fraction 0.25}`（v4 於 2026-11-08 開啟） |
| `drawdown_control` | `neutral_at 0.05`、`defensive_at 0.10`、`release_neutral_below 0.03`、`release_defensive_below 0.07`；`history_ref` 由 pipeline 綁定 |
| `stock_weight_fit` | `v1`（單股上限減碼不依此欄位開關，一律由程式執行） |
| rules | `max_turnover_rate 0.3`（policy）、`max_sector_weight 0.6`、`reuse_sell_proceeds true`、`minimum_active_share 0.15`、現金 `< 0.25`、持股 20～30 檔、單股 10%（2330 為 25%） |
| bundle | `evidence_policy: typed_evidence_v1`、`fundamental_period_policy: per_symbol_latest_available_v1`（新 bundle 預設），`team_inputs` 2.7（`exposure_rotation` 開啟時） |
| `config/strategy_reflection.json` | `enabled true`、`auto_apply false` |

未開啟：`high_conviction_cash_floor`。

已開啟（紀錄用）：`cash_stance_turn_facts` v1（2026-11-09 開啟，轉向事實與 `turn_assessment` 列舉，不是規則，與姿態不設對應，見[計劃](turn_facts_plan.md)）。

未開啟：

| 閘門 | 說明 |
| --- | --- |
| `position_sizing.high_conviction_cash_floor` | 與 `cash_ratio_range` 互斥，目前正式設定沒有這個欄位；行為見[決策鏈契約](decision-contracts.md#高信心現金下限high_conviction_cash_floor)，只在人改設定時才生效 |

## 帳本、D-Plan 與營運

| 功能 | 現況 | 備註 |
| --- | --- | --- |
| 虛擬帳本與績效儀表板 | 已接入 | 含對 0050、150 檔等權的基準比較 |
| D-Plan v4.3 候選 | 已接入，仍需人工上傳 | 決策核准、競賽設定確認、帳本與資料包對帳都通過才產生；主辦方伺服器驗證與上傳由人執行 |
| 備援 D-Plan | 已接入，尚未在真實排程觸發 | 決策被拒、D-Plan 被擋或流程失敗時，在前提都滿足下產生「全部續抱、無委託」的本機候選，絕不自動上傳 |
| 排程通知與狀態紀錄 | 已接入 | 本機通知加 `var/logs/daily_status.log`；不傳手機或 email |
| 08:00 看門狗 | 完成 | 腳本與排程樣板都有，已於 2026-11-05 安裝到本機 launchd（週一至週五 08:00），安裝方式見[日常操作](operations.md) |
| 策略反思（每週） | 已接入 | 只自動套用降風險的調整，其餘等人放行；`auto_apply` 維持關閉。回放只是參考，不算對 AI 判斷的驗證 |
| Agent 評測與多日回放 | 已接入 | 不屬於決策鏈，見 `src/etf_agent/evaluation/README.md` |
| 自動下單或送件 | 尚未，也不打算做 | 一律由人檢視後上傳 |

## 已知限制

- 虛擬帳本使用官方價量模擬成交，不等於主辦方實際成交。
- 回放與回測只重算已封存的 Agent 意圖，不是對 AI 判斷的前瞻驗證；只有比賽期間的即時紀錄算證據。
- 台指期夜盤要台北 05:00 後才有，平日 21:00 的決策拿不到。
- 持股很少、已滿檔又要強制換股時，防守減碼可能那一輪沒做：換出一檔再買一檔的成交量超過換手上限（`max_turnover_rate`），`turnover_fit` 把替補買單砍光，現金升到 100% 而觸發 `RISK_CASH_CEILING`，程式便留住被換出的持股，該輪也不再減碼，現金停在緩衝之下。結果仍通過 Guard、重建一致，不違反硬性規則；目前持股 20～30 檔，很少遇到。若發現防守姿態下現金長期補不到緩衝，先查這個互動（`forced_swap`、`turnover_fit`、`cash_ceiling_fix`、`drawdown_derisk` 四方）。測試見 `tests/decision/test_gate_prerequisites.py`。
