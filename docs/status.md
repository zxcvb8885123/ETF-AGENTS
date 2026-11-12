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
| 產業比較資料（t187ap14 上市＋上櫃） | 部分（只收集與稽核） | 2026-11-10 起每日資料階段收集，與官方損益表逐檔核對、建立產業代碼↔名稱表；**不進決策**，沒有產業中位數，分析摘要仍寫「未附產業比較標準」。`INDUSTRY_PEER=0` 關閉；見[計畫](fundamental_data_gaps_plan.md#47-c1-實作狀態2026-11-10)與[來源核對](source_audit/2026-11-10_industry_peer_sources.md) |
| FinMind 現金流量表與單季財報 | 已接入 | 現金流量表要與官方損益表相符才用；單季財報經官方金額核對後，可補去年同期累計財報（`cli/fundamental_repair.py`；2026-11-10 起每日資料階段以 `--scheduled` 自動補，`PRIOR_REPAIR=0` 可關閉） |
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

這是閘門啟用狀態與目前數值的**唯一出處**（其他文件只連結這裡）。2026-11-11 依 `config/decision_policy.json`（`policy_id: strategy-2026-11-12-turnover-skip-1`，`available_at: 2026-11-12T12:23:00+08:00`；本表 `position_review` 以外的數值以此核對）、`config/decision_rules.json`（`decision-rules-2026-11-03b`）與 `config/strategy_reflection.json` 核對；改設定時請同步更新本表。各閘門的行為與條件見[決策鏈契約](decision-contracts.md)，名稱說明見[名稱對照](glossary.md)。

2026-11-09 17:50 把 `exposure_rotation` 由 v1 切到 v2（只換欄位寫法，觸發線與停止線數值不變：進攻 0.90、中性 0.55；刪 `max_rotation_fraction`、`min_new_conviction`、`release_margin`），切換前的樣板存為 `config/policy_versions/2026-11-09-exposure-rotation-v1.json`，其餘門檻與閘門開關不變；先前的策略識別與可得時間更新（`strategy-2026-11-09-turnfacts-v1`）沿用同一段說明。審查前樣板原樣保存在 `config/policy_versions/2026-11-09-before-review.json`；該檔是當日審查前的內容快照，**不是**其中舊 `available_at` 日期當時的策略還原。封存 run 仍讀自己保存的 Policy，不改寫歷史。用新版策略重跑較早 cutoff 僅能是明確標示的反事實評測。

已開啟：

| 閘門 | 目前值 |
| --- | --- |
| `position_review` | `version v2`、`loss_alert 0.12`、`retrace_alert_20d 0.20`、`gain_alert 0.20`、`trail_alert` arm_gain 0.10／giveback 0.08（2026-11-12 `strategy-2026-11-12-liquidity-v2-1` 由 0.10、0.05／0.05 放寬：現行門檻小於換入股一到兩天的正常波動；回滾樣板 `config/policy_versions/2026-11-12-before-liquidity-v2-alerts.json`）、`relative_loss_alert` loss 0.08／lag 0.04、`stale_alert null`、`context v1`（2026-11-11 `strategy-2026-11-11-alerts-v2-1`；使用者決定硬停損不開、只保留警示讓交易 Agent 判斷；`position_sizing.stop_exit` 已實作、尚未開啟，正式設定沒有此欄位） |
| `position_sizing.method` | `conviction_volatility_v2`，`volatility_exponent 0`，乘數 high 1.5／medium 0.6／low 0.3，`volatility_floor 0.01` |
| `selection_ranking` | `v1`（`selection_tiebreak` 未設定，被名次取代） |
| `limit_scaled_conviction` | `{version v1, alpha 0.6}` |
| `positions_by_breadth` | concentrated 22／balanced 26／diversified 30 |
| `held_add_priority`＋`rotation` | `v1`；`max_swaps_per_run 7`（2026-11-12 10:01 由 5 改 7）、`min_new_conviction medium` |
| `rotation.forced_swap` | **已於 2026-11-10 20:10 移除**（與 `agent_swap` v2 互斥；回滾樣板 `config/policy_versions/2026-11-10-before-agent-swap-v2.json`） |
| `cash_buffer_floor` | `v1` |
| `cash_ratio_range` | `version v1`；`grid` aggressive 3／5／8%、neutral 8／10／13%、defensive 15／18／21%；`max_daily_raise 0.03`；`anchor` 由 pipeline 每次綁定 |
| `exposure_rotation` | `version v3`（現行；2026-11-09 開啟 v1、同日 17:50 切到 v2、23:45 切到 v3，v3 與 v2 行為相同，見[計劃](exposure_rotation_plan.md)與[v2 計劃](exposure_rotation_v2_plan.md)）；`beta_window_days 60`；`beta_target_by_stance` aggressive 0.90／neutral 0.55／defensive null（觸發與停止線，與 v1 的「下限 − margin」數值相同）；`max_swaps_per_run 2`；`min_holding_days 5`；換入者最低信心沿用 `rotation.min_new_conviction`；`hold_conviction v1`；`conviction_gap_swap` 已於 2026-11-10 20:10 隨 `agent_swap` v2 移除（現行設定沒有）；`held_since_by_symbol` 由 pipeline 依帳本綁定；`team_inputs` 2.7 |
| `exposure_rotation` v3 | **2026-11-09 23:45 已開啟**（為了 `rebound_participation`，`defensive` 目標仍是 null，行為與 v2 相同）：欄位同 v2，`beta_target_by_stance.defensive` 可填數字（例如 0.35）；見[v2 計劃第 12 節](exposure_rotation_v2_plan.md#12-v3防守姿態的-beta-停止線) |
| `cash_stance_recency` | `{version v1}`（2026-11-09 開啟；風險 Agent 簡報首鍵附證據時間表，另填 `recency_reconciliation`） |
| `cash_buffer_by_stance` | aggressive 3%／neutral 10%／defensive 20%（`cash_ratio_range` 開啟時不影響配置） |
| `cash_fit_order` | `v2`（2026-11-10 17:58 起；已無 `cash_fit_min_fill_ratio`） |
| `high_conviction_cash_priority` | `v2`（2026-11-10 17:58 起） |
| `held_weight_budget` | `v2`（2026-11-10 17:58 起） |
| `rebound_participation` | v1 **2026-11-09 23:45 已開啟（2026-11-10 已切到 v2，以下 v1 描述保留供舊 run 重建）**，只開 confirmed 一檔（candidate 的 `defensive_target` 設 0.01、換股 2 檔、持有 5 天＝實質不動；confirmed 為 0.55、4 檔、持有 0 天；**沒有**填選填的 `defensive_cash_buffer` 與 `confirmed.max_turnover_rate`，所以不放寬現金與換手；之前樣板存於 `config/policy_versions/2026-11-09-rebound-before.json`，回滾＝刪除此欄位並把 `exposure_rotation.version` 改回 v2）：程式狀態機 `turn_state` 與風險 Agent 的 `turn_assessment` 取較保守者，放寬防守姿態下的曝險換股（選填：防守現金比例條件式放寬、confirmed 換手上限，皆放寬風控、只由人設定）；前提 `exposure_rotation` v3（正式設定已是 v3）、`cash_stance_turn_facts`、`drawdown_derisk` v4、`typed_evidence_v1`；`team_inputs` 2.8；計劃與重放結果見 [rebound_participation_plan.md](rebound_participation_plan.md)；**v2 已於 2026-11-10 01:37 開啟（正式設定已切到 v2，`by_state` 與 `min_coverage` 沿用 v1 的值；**2026-11-10 起 candidate 的 `defensive_target` 由 0.01 改為 0.30**，因 0.01 低於持股 beta 而恆為 `beta_at_target`、候選狀態實質不動，仍不放寬現金與換手；v1 樣板存於 `config/policy_versions/2026-11-10-rebound-v1.json`，回滾＝換回該檔的區塊）**：拿掉固定門檻，程式只整理事實與資料品質，`turn_assessment` 單獨決定、缺資料恆 none，與 v1 差異（regime 不是 bear 不再擋）見[v2 計劃](rebound_participation_v2_plan.md) |
| `slot_recycle` | v1 **已於 2026-11-10 23:25 開啟（使用者決定；回滾樣板 `config/policy_versions/2026-11-10-before-slot-recycle.json`）**：LOT_SIZE／預算為零的新股釋出新增名額給後面的候選 |
| `agent_swap` | **2026-11-11 12:36 現況：v2，`max_swaps_per_run 5`、`min_holding_days 5`，`exposure.max_pairs_by_view` 當時為 `start_rebuild 3`／`rebuild 5`（現行為 `probe 2`／`start_rebuild 4`／`rebuild 5`，2026-11-11 14:40 由 offense 開啟時調整，見下方 `offense` 列；使用者決定，`policy_id strategy-2026-11-11-offense-ladder-1`，回滾樣板 `config/policy_versions/2026-11-11-before-offense-ladder.json`、`…-before-offense-stage0b.json`）。** 以下為 v1 影子期原始描述，保留供舊 run 重建：`{version v1, max_swaps_per_run 2, min_holding_days 5}`，**2026-11-10 15:31 已開啟，影子期**：交易 Agent 在入選排序之後多一次第二輪「換股提名」（5c，`not_needed` 時由程式封存、不呼叫 Agent），提名與依據封存後由 Finalizer 重建驗證，**配置完全不讀提名**，訂單與旗標與沒有此欄位時相同；`team_inputs` 因此升為 2.9（多存 `swap_nomination`）；回放略過此閘門。提名驗證用盡或呼叫失敗只封存 `exhausted`／`unavailable`、不影響其餘決策（硬規則仍整份拒絕）。回滾＝刪掉 `position_sizing.agent_swap`。設計見[換股提名計畫](agent_nominated_rotation_plan.md)，統計工具 `cli/calibration.py intent-landing`（意圖落地率，階段 0）已實作 |
| `agent_swap.derisk_day_view` | v1 **已開啟（2026-11-11 00:35，使用者決定）**；`policy_id strategy-2026-11-11-derisk-day-view-1`：減碼日不再整個略過換股提名，各配對照自己的條件處理（`keep_defensive` 取消 exposure 配對、quality_dip 看回撤下限與轉向條件）；硬規則與可行性試算不變；回放隨 `agent_swap` 一起略過。回滾＝刪掉 `position_sizing.agent_swap.derisk_day_view`，樣板 `config/policy_versions/2026-11-11-before-derisk-day-view.json`。起因：2026-11-11 run 現金只差 NAV 的 0.044% 就整天不能換股 |
| `agent_swap.proceeds_to_add` | v1 **已開啟（2026-11-11，使用者決定，`policy_id strategy-2026-11-11-proceeds-to-add-1`）**：換股成立時賣款可流向高信心加碼持股；細節見 [decision-contracts](decision-contracts.md)。回滾＝刪掉 `position_sizing.agent_swap.proceeds_to_add`，樣板 `config/policy_versions/2026-11-11-before-proceeds-to-add.json` |
| `agent_swap.entry_reserve` | v1 **已開啟（2026-11-11，使用者決定，`policy_id strategy-2026-11-11-entry-reserve-1`，`max_entry_weight` 0.06、`max_nominations` 8；回滾＝刪掉 `position_sizing.agent_swap.entry_reserve`，樣板 `config/policy_versions/2026-11-11-before-entry-reserve.json`）**（階段 3，設計見 [agent_led_offense_plan.md](agent_led_offense_plan.md) 4.6）：每組提名必填列舉 `size`，份量格對買進權重上限（`large`＝人填 `max_entry_weight`、`medium` 2/3、`small` 1/3），換入買進股數取份量格權重、該組換出者賣款加分到的格點以上現金、單股與產業餘裕的最小值（只縮不放），換入者先保留預算、不再參與信心預算，縮到不足一張或累計換手超過上限即整組取消、換出者不賣，有 `size` 的組不適用 `proceeds_to_add`；`max_nominations` 為提名清單上限（可大於成交上限）。前提 `agent_swap` v2 與 `agent_swap.exposure`，`quality_dip` 並存時只能 v3／v4。`team_inputs` 不升版、沒有新鍵 |
| `quality_dip_facts` | v1 **已開啟**（2026-11-10 使用者決定；樣板快照 `config/policy_versions/2026-11-10-before-quality-dip.json`）：優質股被大盤拖累的第一階段事實（紀錄用）——程式算全交易池的視窗前 60 日 beta 殘差與名次、價格位置、財報三個清單（新壞事／疑慮／不知道）與候選標示（`market_dragged`／`oversold`，超跌固定 `cause: unknown`），以背景給技術／基本面分析師與交易 Agent（不給多空研究員）；不新增 Agent 輸出欄位、不影響名額／權重／現金／Guard；`team_inputs` 另存選配鍵 `dip_facts`（不升版本，需 2.6 以上），Finalizer 整份重算；開啟方式＝在 `position_sizing` 加 `{"quality_dip_facts": {"version": "v1"}}`，回滾＝刪除該欄位；見[計畫](quality_dip_plan.md) |
| `quality_dip_assessment` | v1 **已開啟**（2026-11-10 使用者決定，與 `quality_dip_facts` 一起）：優質股第二階段（紀錄用），前提 `quality_dip_facts`——交易 Agent 對每個 buy／add 多填列舉 `dip_assessment`（`not_a_dip`／`market_driven`／`oversold_cause_unclear`／`stock_specific`／`undetermined`），價格事實不可用時只能 `undetermined`；與意圖、信心不設對應、不影響配置；開啟＝在 `position_sizing` 加 `{"quality_dip_assessment": {"version": "v1"}}`，回滾＝刪除該欄位；見[計畫](quality_dip_plan.md#10-第二階段估值-v1-實作紀錄與第三階段修訂2026-11-10第六版) |
| `event_direction` | v1 **已開啟**（2026-11-10 使用者決定，與 `quality_dip_facts` 一起）：優質股第二階段（紀錄用），前提 `quality_dip_facts`——事件分析師每則事件多填 `direction`（`positive`／`negative`／`neutral`／`unknown`），`quality_dip_facts` 的新壞事事件面改為「視窗內新可得、high 且該則方向為 negative」（缺方向列不知道）；不新增報告版本、不影響配置；開啟＝加 `{"event_direction": {"version": "v1"}}`，回滾＝刪除該欄位 |
| `exposure_view` | v1 **已開啟（2026-11-10，使用者決定）**；純紀錄、不影響配置；回滾＝刪掉 `position_sizing.exposure_view`：Agent 主導曝險的階段 1（事實與紀錄）——風險 Agent 簡報多附組合曝險事實（組合 beta、持股 beta、低 beta 占比與檔數、現金權重、持股產業權重〔金融保險另列〕、最近一個交易池上漲日的池報酬與持股依起始權重的當日貢獻；背景、不可引用），輸出多一個必填列舉 `exposure_view`（`keep_defensive`／`start_rebuild`／`rebuild`，非 `keep_defensive` 須引用市場層級證據），**純紀錄、不接進任何配置**；`agent_swap` 也開啟時換股提名簡報另附轉向、加權指數、組合曝險、上漲日報酬與候選新股 beta（背景，不改提名驗證）。前提 `exposure_rotation`、`cash_stance_turn_facts`、`market_index_facts` 與 `typed_evidence_v1`；`team_inputs` 另存選配鍵 `exposure_facts`（不升版、須 2.7 以上），Finalizer 整份重算。見 [agent_led_exposure_plan.md](agent_led_exposure_plan.md) |
| `cash_ceiling_fix`、`stock_limit_headroom`、`buy_gap_reserve`（`buy_gap_reserve_rate 0.03`）、`sector_fit`、`turnover_fit` | 皆 `v1`；`insufficient_funds_policy void_batch` |
| `reentry_cooldown` | `{version v3, days 3}`（2026-11-10 17:40 由 v1 切到 v2：冷卻名單只計交易 Agent 自己的出場；23:25 切到 v3，見下方 `reentry_cooldown` v2／v3 列） |
| `drawdown_derisk` | `{version v5, max_derisk_fraction 0.25}`（v4 於 2026-11-08 開啟，2026-11-10 17:40 切到 v5：減碼順序由交易 Agent 給） |
| `drawdown_control` | `neutral_at 0.05`、`defensive_at 0.15`、`release_neutral_below 0.03`、`release_defensive_below 0.11`（2026-11-11 12:36 強制防守線由 0.10／0.07 放寬到 0.15／0.11，使用者決定；回滾樣板 `config/policy_versions/2026-11-11-before-offense-ladder.json`）；`history_ref` 由 pipeline 綁定 |
| `stock_weight_fit` | `v1`（單股上限減碼不依此欄位開關，一律由程式執行） |
| rules | `max_turnover_rate 0.3`（policy）、`max_sector_weight 0.6`、`reuse_sell_proceeds true`、`minimum_active_share 0.15`、現金 `< 0.25`、持股 20～30 檔、單股 10%（2330 為 25%） |
| bundle | `evidence_policy: typed_evidence_v1`、`fundamental_period_policy: per_symbol_latest_available_v1`（新 bundle 預設），`team_inputs` 2.9（`exposure_rotation` 開啟為 2.7、`rebound_participation` 再開啟為 2.8、`agent_swap` 再開啟為 2.9，目前三者皆開） |
| `config/strategy_reflection.json` | `enabled true`、`auto_apply false` |

未開啟：`high_conviction_cash_floor`。

已開啟（紀錄用）：`cash_stance_turn_facts` v1（2026-11-09 開啟，轉向事實與 `turn_assessment` 列舉，不是規則，與姿態不設對應，見[計劃](turn_facts_plan.md)）。
已開啟（2026-11-10，紀錄用）：`market_index_facts` v1（風險 Agent 簡報附證交所加權指數收盤、當日漲跌、近 5／20 日報酬與離高點回檔，背景事實、不可引用；補上晚間決策看不到隔夜行情的缺口；不新增輸出欄位、不影響配置；設定在 `config/decision_policy.json` 的 `position_sizing.market_index_facts`，回滾＝刪除該欄位，見[計畫](market_index_facts_plan.md)）。
已開啟（2026-11-10，紀錄用，使用者決定）：`exposure_view` v1（風險 Agent 簡報附組合曝險事實並多填列舉 `exposure_view`，`agent_swap` 開啟時提名簡報附轉向與曝險事實；背景、不可引用、純紀錄、不影響配置；設定在 `config/decision_policy.json` 的 `position_sizing.exposure_view`，回滾＝刪掉該欄位，見[計畫](agent_led_exposure_plan.md)）。

**v2 已於 2026-11-10 23:20 開啟（使用者決定；`max_pairs_by_view` `start_rebuild 1`／`rebuild 2`；回滾樣板 `config/policy_versions/2026-11-10-before-exposure-v2.json`）**；v1 影子曾於 2026-11-10 19:45 開啟（`policy_id strategy-2026-11-10-exposure-shadow-1`，回滾樣板 `config/policy_versions/2026-11-10-before-exposure-shadow.json`）、v2 已於 23:20 開啟：`agent_swap.exposure`（Agent 主導曝險階段 2、3；v1 影子、v2 依風險 Agent 的 `exposure_view` 取用 `purpose: exposure` 的換股配對；前提 `agent_swap`、`exposure_view`；`max_pairs_by_view` 不得高於 `max_swaps_per_run`；`team_inputs` 不升版；回滾＝刪掉 `position_sizing.agent_swap.exposure`，見[計畫](agent_led_exposure_plan.md)）。

以下表格列「尚未開啟」與「分階段開啟的閘門明細」；**以每列的第二欄標示為準**（標「已開啟」的就是現行設定，標「尚未開啟」的才不是）：

| 閘門 | 說明 |
| --- | --- |
| `position_sizing.high_conviction_cash_floor` | 與 `cash_ratio_range` 互斥，目前正式設定沒有這個欄位；行為見[決策鏈契約](decision-contracts.md#高信心現金下限high_conviction_cash_floor)，只在人改設定時才生效 |
| `position_sizing.agent_swap.quality_dip`（優質股第三階段） | **v1 影子於 2026-11-10 23:05 開啟，23:25 切到 v2 正式（使用者決定）**（回滾樣板 `config/policy_versions/2026-11-10-before-quality-dip-v1.json`）：換股提名的 `purpose` 多一個 `quality_dip`（與曝險階段 2 合用列舉，前提 `agent_swap.exposure`、`quality_dip_facts`、`quality_dip_assessment`、`cash_stance_turn_facts`）；v1 影子只做 5c 結構檢查與封存、配置略過；v2（須 `agent_swap` v2）配置時另限每次對數、每檔買進權重、買進金額不超過換出賣款、需轉向跡象、回撤強制防守與減碼日略過；全部數字由人填、程式不給預設。回滾＝刪掉該欄位。見[優質股計畫](quality_dip_plan.md#104-第三階段實作紀錄2026-11-10第七版) |
| `position_sizing.agent_swap.quality_dip` **v3**（組數與份量由 Agent 提、風險 Agent 裁決） | **2026-11-11 階段 0b 已開啟（使用者決定），同日 12:36 再升為 v4（見下一列），目前正式設定是 v4**；以下為 v3 的描述（v4 是它的超集）：前提 `agent_swap` v2：前提 `agent_swap` v2；提名多 `size` 份量格與 `quality_dip_decline`，配置前風險 Agent 裁決 `approve`／`downsize`／`reject`（只能縮小），裁決失敗取消全部 quality_dip 配對；組數另計不占 `agent_swap.max_swaps_per_run`；`team_inputs` 2.10。硬上限數值待人填。見 [`quality_dip_agent_sizing_plan.md`](quality_dip_agent_sizing_plan.md)。 |
| `position_sizing.agent_swap.quality_dip` **v4**（連跌後逢低布局，v3 的超集） | **2026-11-11 12:36 已開啟（使用者決定；`policy_id strategy-2026-11-11-offense-ladder-1`；回滾樣板 `config/policy_versions/2026-11-11-before-offense-ladder.json`、`…-before-offense-stage0b.json`）**：正式設定 `max_swaps_per_run 2`、`max_entry_weight 0.04`、`oversold {max_swaps_per_run 1, max_entry_weight 0.02}`、`min_turn_assessment no_turn_evidence`（使用者決定：連跌無轉向跡象時也能抄底，回檔深度由 `ladder` 把關；有轉向跡象時不受深度門檻限制），`ladder` 為 `min_pool_drawdown 0.08`（2026-11-11 使用者由 0.10 改 0.08，早一點開放）、`max_dip_exposure 0.12`、`max_tranches 8`、`min_days_between_tranches 2`、`pause_after_further_drop 0.05`。前提 `agent_swap` v2、v3 的全部前提；新增人填的 `ladder`（`min_pool_drawdown`、`max_dip_exposure`、`max_tranches`、`min_days_between_tranches`、`pause_after_further_drop`，全部必填、程式不給預設）；有 `ladder` 時 `min_turn_assessment` 才可填 `no_turn_evidence`（沒有轉向跡象也能抄底，但交易池離 60 日高點回檔須達 `min_pool_drawdown`；有轉向跡象時不受該門檻限制）；總抄底曝險、批數、批次間隔、上一批之後再跌的暫停線由程式依帳本持有起點算出、綁成 `ladder_state`，缺資料一律關閉；`team_inputs` 維持 2.10、多選配鍵 `decline_depth_facts`（與 v4 雙向檢查、Finalizer 整份重算）。見 [`agent_led_offense_plan.md`](agent_led_offense_plan.md) 4.13。**`ladder.exposure_basis`（選配列舉，C1，已開啟，現值 `quality_dip_fills`，與 offense 兩個子欄位同批 2026-11-11 開啟）**：缺席＝總抄底曝險與批數照舊計所有新建倉（逐位元不變）；填 `quality_dip_fills` 只計 purpose=quality_dip 的成交（修正現狀：thesis／exposure／一般買進也算抄底曝險，兩組 medium 就 `exposure_full`、任何新建倉隔天都 `tranche_interval`）。成交清單 `quality_dip.ladder_entries`（`[{symbol, entry_date, run_id}]`）由 pipeline 比照 `bind_reentry_cooldown` 從決策庫確定性取得並綁進 Policy（來源：帳本已套用的前次 run，Policy 的 `nominated_pairs` 為 quality_dip、proposal 有買單與 `AGENT_SWAP_FILL`，只留目前仍持有且帳本持有起點日期不早於該 run 截止日者；樣板預填即拒絕、讀不到綁 null），`build_ladder_state` 只讀清單（公式不變），讀不到或與持股、帳本不一致即以新原因 `entries_unavailable` 關閉；Finalizer 以綁定清單重算並核對每筆在帳本驗證過的 `held_since_by_symbol` 內且日期相容、不重掃決策庫（信任層級同 `reentry_cooldown`）。沒有新的人填數字，開啟由人改 config。 |
| `position_sizing.offense` v1（轉向後進攻，階段 2） | **2026-11-11 14:40 已開啟（使用者決定；`policy_id strategy-2026-11-11-offense-1`；回滾樣板 `config/policy_versions/2026-11-11-before-offense.json`；`max_pairs_by_view` 為 probe 2／start_rebuild 4／rebuild 5，已刪除 `rebound_participation.by_state.*.defensive_cash_buffer`）**：正式數值 probe 0.10／2／medium／0.40、start_rebuild 0.08／4／large／0.55、rebuild 0.08／5／large／0.70、`offense_off_drawdown` 0.12。前提 `agent_swap` v2、`agent_swap.exposure` v2（`max_pairs_by_view` 多 `probe`，probe ≤ start_rebuild）、`agent_swap.entry_reserve`、`exposure_view` v2、`cash_ratio_range`；與 `rebound_participation.by_state.*.defensive_cash_buffer` 互斥。形狀 `{version, by_view{probe,start_rebuild,rebuild}{defensive_cash_buffer,max_exposure_pairs,max_size,beta_stop}, offense_off_drawdown}`，全部必填、無預設。`defensive_cash_buffer` 不得低於中性姿態最低格點（現行 0.08），故計畫草案的 rebuild 0.05 會被拒絕。`team_inputs` 不升版、選配鍵 `offense_facts`（須 2.9 以上）。見 [`agent_led_offense_plan.md`](agent_led_offense_plan.md)。 |
| `position_sizing.offense.thesis_size_cap`／`offense.cash_glide`（選配子欄位，各 `{"version":"v1"}`） | **已開啟（2026-11-11，`policy_id strategy-2026-11-11-offense-ext-1`，回滾樣板 `config/policy_versions/2026-11-11-before-offense-extensions.json`；審查 A4／A2）**。`thesis_size_cap`：放寬防守姿態現金目標那天 thesis 配對份量格也套該級 `max_size`（只縮不取消）；`cash_glide`：offense 放寬後現金回升受 `cash_ratio_range.max_daily_raise` 約束（只在前次放寬過、今日防守、回撤條件成立時）。沒有欄位逐位元不變；回滾＝刪欄位；同批另開 `ladder.exposure_basis quality_dip_fills`；見計畫第 11 節 |
| `position_sizing.exposure_view` **v2**（簡報說真話） | **2026-11-11 12:36 已開啟（使用者決定；正式設定 `{"version": "v2"}`）**：只改風險 Agent 簡報與提示文字，使其與現行行為一致（`exposure_view` 決定曝險配對是否取用、`turn_assessment` 影響現金目標與 quality_dip 門檻），並列追高與錯過兩種錯誤成本；配置、Schema、`team_inputs` 不變；前提 `agent_swap` v2 與 `agent_swap.exposure` v2；回滾＝版本改回 v1。 |
| `position_sizing.trader_background_record`（交易背景封存） | **已實作、尚未開啟**（2026-11-11；正式設定沒有這個欄位，只由人設定、策略反思禁區）：策略教訓與論點檢視來源檔的完整內容在交易階段前封存進 run 目錄與 `team_inputs.trader_background`（選配鍵、不升版、與閘門雙向檢查），簡報改由封存產生；不影響配置。限制：Finalizer 只能驗證封存自洽、時間合法與 `verdicts_ref` 一致，無法證明來源檔當時沒有別的版本。開啟方式：`position_sizing` 加 `"trader_background_record": {"version": "v1"}`；回滾＝刪掉該欄位。見 [`docs/audit_fix_plan.md`](audit_fix_plan.md) 項目 C。 |
| `position_sizing.valuation_facts`（估值 V2） | **已實作、尚未開啟**（2026-11-10；正式設定沒有這個欄位）：PER／PBR 在自身歷史區間的位置（近 250／750 個資料列與全部可得歷史並列）以背景給基本面分析師與交易 Agent，`team_inputs.valuation_facts` 選配鍵、Finalizer 整份重算；不影響配置。**V1 收集的紀錄授權範圍是「只收集與稽核」，即使開啟也全部 `LICENSE_SCOPE_NOT_APPROVED`**，要先由人核准新的用途範圍並改 V1 契約（只改收集器常數會讓既有 run 重建失敗）。見[優質股計畫](quality_dip_plan.md#105-估值-v2-實作紀錄2026-11-10第七版) |
| `position_sizing.revision_bounds` v1（風險修正數值界線） | **已開啟**（2026-11-11 18:33，使用者決定；`policy_id strategy-2026-11-11-revision-bounds-1`；`min_max_stock_weight 0.06`、`min_max_turnover_rate 0.15`、`step 0.01`；回滾樣板 `config/policy_versions/2026-11-11-before-revision-bounds.json`）：風險審查修正動作 `reduce_max_stock_weight`／`reduce_turnover_limit` 的 `value` 須不低於人填下限、且為人填 `step` 的整數倍；下限與步距全部由人填；沒有閘門時行為不變；不影響配置與 `team_inputs`。回滾＝刪掉該欄位。見[決策契約](decision-contracts.md)與[設計審查修正計畫](audit_fix_plan.md) B |
| `agent_swap.entry_reserve.swap_priority` v1（換股優先順序） | **已開啟**（2026-11-11 22:40，使用者決定；`policy_id strategy-2026-11-11-swap-priority-1`；回滾樣板 `config/policy_versions/2026-11-11-before-swap-priority.json`）：（1）換股試算的名額保護只計分到至少一張的新股（`slot_recycle` 以權重 0 保留、買不到一張的新股不再擋換股）；（2）`turnover_fit` 縮買單時，已保留預算的換股換入者排在其他買單之後才縮。只有版本欄位、沒有人填數字；不改寫交易 Agent 意圖、不影響 `team_inputs`；沒有該欄位時行為逐位元不變。起因：2026-11-12 的 run 6 組提名只成交 1 組，同輸入重跑開啟後 5 組成交、Guard 通過、換手 29.9%（`replay_reference_only`）。回滾＝刪掉該欄位 |
| `agent_swap.entry_reserve.turnover_skip` v1（換手超過只取消該組） | **2026-11-12 12:23 已開啟**（使用者決定；`policy_id strategy-2026-11-12-turnover-skip-1`；回滾樣板 `config/policy_versions/2026-11-12-before-turnover-skip.json`）：某一組讓累計換手超過上限時只取消該組、繼續試後面的組；沒有它時第一個超過的組之後全部取消（2026-11-12 決策的 2408←3293 單獨放得進 0.33，卻被前一組連帶取消）。只有版本欄位、沒有人填數字；不改寫交易 Agent 意圖、不影響 `team_inputs`；沒有該欄位時行為逐位元不變。回滾＝刪掉 `position_sizing.agent_swap.entry_reserve.turnover_skip`。注意：它救不了本身就超過上限的大組（如遠傳←1303），也可能讓清單後面較不想換的組先成交 |
| `position_sizing.liquidity_fit` v1／**v2**（買單流動性縮減） | **已開啟**（2026-11-11 23:05，使用者決定；`policy_id strategy-2026-11-11-liquidity-fit-1`；`max_participation 0.10`，與風險審查的流動性警示線同值；回滾樣板 `config/policy_versions/2026-11-11-before-liquidity-fit.json`）：配置引擎在其他縮減之後，把買單股數縮到「`max_participation` × 20 日均量」以內（向下取整張，不足一張取消該筆），旗標 `LIQUIDITY_LIMIT`；缺均量不縮並留 `LIQUIDITY_UNAVAILABLE`。只縮買單、不改寫交易 Agent 意圖、不影響 `team_inputs`；沒有該欄位時行為逐位元不變。起因：2026-11-12 的 8415.TWO 買單是 20 日均量的 1.64 倍，風險 Agent 只能整筆移除；同輸入重跑（不排除）縮為 54 張、Guard 通過（`replay_reference_only`）。回滾＝刪掉該欄位 |
| `liquidity_fit` **v2**（上限提前到預算階段）＋ `position_review` 放寬 ＋ `quality_dip.max_entry_weight 0.06` | **2026-11-12 00:05 已開啟**（使用者決定；`policy_id strategy-2026-11-12-liquidity-v2-1`；回滾樣板 `config/policy_versions/2026-11-12-before-liquidity-v2-alerts.json`）：（1）`liquidity_fit` v1→v2：同一條 `max_participation 0.10` 上限提前到預算階段，換入者保留與信心預算先封頂、釋出的預算重新分配，v1 的最後縮減留作保險；v1 的孤兒現金問題（縮掉的錢不再分配，現金高於目標）因此消除；同輸入重跑 11-12（不排除 8415）：換手 29.9%、現金 12.9%、Guard 通過（`replay_reference_only`）。（2）警示放寬見上方 `position_review` 列。（3）`agent_swap.quality_dip.max_entry_weight` 0.04→0.06（試單 1.33%→2%，與 `entry_reserve` 格距相同；`oversold` 0.02 與 `ladder` 不動；總量仍受 `ladder.max_dip_exposure 0.12` 與風險 Agent 裁決限制）。回滾＝依回滾樣板改回 |
| `offense.by_view.probe.beta_stop` 0.45→0.55、`agent_swap.max_swaps_per_run` 與 `rotation.max_swaps_per_run` 5→7 | **2026-11-12 10:01 已開啟**（使用者決定；`policy_id strategy-2026-11-12-swap-cap-7-1`；回滾樣板 `config/policy_versions/2026-11-12-before-swap-cap-7.json`）：11-12 決策三組曝險換股（含換出遠傳、中信金）被 `swap_cap`／`offense_beta_stop`／換手上限擋下，Agent 的提名落空。兩個組數上限須同調，否則提名簡報的 `fill_cap` 與配置不一致；`max_nominations 8` 不動。**加風險方向**：probe 停止線 0.55 與 `start_rebuild` 相同；單日換更多檔，交易成本與追高風險上升。回滾＝`beta_stop` 改回 `"0.45"`、兩個 `max_swaps_per_run` 改回 5 |
| `max_turnover_rate` 0.29→0.33 | **2026-11-12 08:13 已開啟**（使用者決定「直接改」；`policy_id strategy-2026-11-12-turnover-033-1`；回滾樣板 `config/policy_versions/2026-11-12-before-turnover-033.json`）：今日 11-12 決策被 29% 擋下 2 組換股與 2330 加碼縮減。**超過 30% 的風險**：專案內三份規則檔沒有換手上限，但主辦方官方規則未查證；程式用前一日收盤算換手，開盤跳空可能更高。先前 opus 分析（放寬到 0.32）現金反升到 21.7%。回滾＝改回 0.29 |
| `max_turnover_rate` 0.30→0.29 | **2026-11-12 00:40 已開啟**（使用者決定；`policy_id strategy-2026-11-12-turnover-029-1`；回滾樣板 `config/policy_versions/2026-11-12-before-turnover-029.json`）：程式用前一日收盤算換手、主辦方用當日成交均價結算，換手貼 29.9%～30.0% 時開盤跳空可能超過 30%；降風險方向，只留 1 個百分點跳空餘裕（現金另有 `buy_gap_reserve_rate 0.03`）。同輸入重跑代價：現金約多 1%、2330 加碼少約 4 張（fable 估計）。30% 是否為主辦方硬規則未查證。回滾＝改回 0.3 |
| `offense.by_view.probe.beta_stop` 0.40→0.45 | **2026-11-12 01:05 已開啟**（使用者決定；`policy_id strategy-2026-11-12-probe-beta-045-1`；回滾樣板 `config/policy_versions/2026-11-12-before-probe-beta-045.json`）：只改 probe 一級的預估組合 beta 停止線；加風險方向。opus 重跑分析（`replay_reference_only`）：probe 第一天只有換手上限有用，第二天起 `beta_stop` 才開始擋曝險配對；壓力估計從目前回撤 2.74% 起算，重演 10-28→11-06 約由 8.7% 升到約 10.4%、重演 11-03→11-06 約由 6.3% 升到約 7.3%，都碰不到 `offense_off_drawdown 0.12` 與 `defensive_at 0.15`。回滾＝改回 0.40 |
| `offense.by_view.probe.max_exposure_pairs`／`agent_swap.exposure.max_pairs_by_view.probe`（小量先試的曝險配對數） | **2026-11-11 23:30 由 2 改 3（使用者決定 B；`policy_id strategy-2026-11-11-probe-3-1`；回滾樣板 `config/policy_versions/2026-11-11-before-probe-3.json`）**：只改兩個人填數字，`beta_stop 0.40`、`max_size medium`、`offense_off_drawdown 0.12` 與強制防守線不變；多一組曝險配對約多 2%～4% NAV 的高 beta（fable 審查估計）。回滾＝兩處改回 2 |
| `agent_swap` v2（換股改由提名配置） | **已於 2026-11-10 20:10 開啟**（使用者決定、未等影子期；同時移除 `rotation.forced_swap` 與 `exposure_rotation.conviction_gap_swap`；`policy_id strategy-2026-11-10-agent-swap-v2-1`）：配置依交易 Agent 的有序提名換股，換出者由程式標 `exit`＋`AGENT_SWAP`、換入者 `AGENT_SWAP_FILL`；提名可行性取消只取消該對並留 `AGENT_SWAP_CANCELLED:<原因>`，換出者不賣；與 `rotation.forced_swap`、`exposure_rotation.conviction_gap_swap` 互斥，`exposure_rotation` 的換股步驟停用（`EXPOSURE_ROTATION_SKIPPED:agent_swap`）；pipeline 綁 `nominated_pairs`／`nomination_ref`，`portfolio_slots` 升 1.1。（v2 已開啟；原先「影子期後再決定」的說明見[計畫](agent_nominated_rotation_plan.md#6-需人決定事項)；現行 `max_swaps_per_run` 為 5，非此列寫的 2） |

已開啟（2026-11-10，目前值見上表；以下為行為說明）：

| 閘門 | 說明 |
| --- | --- |
| `drawdown_derisk` v5 | **已開啟（2026-11-10 17:40）**：回撤減碼的數字全同 v4，只把「先賣誰」由 ATR 順序改成交易 Agent 的釋出順序（`release_order`，`team_inputs` 選配鍵、不升版本；pipeline 綁 `release_symbols`／`release_ref`）；順序驗證用盡或呼叫失敗時退回 v4 順序並留 `DERISK_ORDER_SKIPPED:order_*`。見[自主權計畫](agent_autonomy_plan.md) |
| `reentry_cooldown` v2／v3 | **v3 已於 2026-11-10 23:25 開啟（回滾樣板 `config/policy_versions/2026-11-10-before-reentry-v3.json`；v2 於 17:40 開）**：v2 冷卻名單只計交易 Agent 自己的 `exit`／`forced_exit`（程式賣出不計）；v3 名單同 v2 但不再擋買單，改成交易 Agent 簡報的 `cooldown:<代號>` 提示，買回須在 `adopted_claim_ids` 回應，沒回應的買單由配置取消（`RISK_REENTRY_COOLDOWN`） |
| `cash_fit_order` v2、`high_conviction_cash_priority` v2、`held_weight_budget` v2 | **皆已於 2026-11-10 17:58 開啟**（回滾樣板 `config/policy_versions/2026-11-10-before-cash-v2.json`）：`cash_fit_order` v2 新股只買得到零頭時不再整筆取消（不允許 `cash_fit_min_fill_ratio`）；`high_conviction_cash_priority` v2 保留現金先給 high、移除「high 被縮減後擋掉後面中低信心新股」；`held_weight_budget` v2 預算算法同 v1，只多附交易 Agent 簡報的持股權重事實（`team_inputs.held_weight_facts`，選配鍵、不升版本，需 `selection_ranking`） |

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
| 意圖落地率與換股影子報告 | 部分 | `cli/calibration.py intent-landing`（buy／add 落地率與被程式賣出的 hold／add）已可用；`shadow-swap`（`agent_swap` v1 影子提名統計與若開 v2 的離線估算）已實作、尚未累積影子資料，估算是上限、只算 `replay_reference_only`；皆唯讀、不屬於決策鏈，見 `src/etf_agent/calibration/README.md` |
| 自動下單或送件 | 尚未，也不打算做 | 一律由人檢視後上傳 |

## 已知限制

- 虛擬帳本使用官方價量模擬成交，不等於主辦方實際成交。
- 回放與回測只重算已封存的 Agent 意圖，不是對 AI 判斷的前瞻驗證；只有比賽期間的即時紀錄算證據。
- 台指期夜盤要台北 05:00 後才有，平日 21:00 的決策拿不到；21:00 是備案，正式交件的 07:00 run 讀得到隔夜美股與夜盤（2026-11-12 第二份審查更正）。
- 持股很少、已滿檔又要強制換股時，防守減碼可能那一輪沒做：換出一檔再買一檔的成交量超過換手上限（`max_turnover_rate`），`turnover_fit` 把替補買單砍光，現金升到 100% 而觸發 `RISK_CASH_CEILING`，程式便留住被換出的持股，該輪也不再減碼，現金停在緩衝之下。結果仍通過 Guard、重建一致，不違反硬性規則；目前持股 20～30 檔，很少遇到。若發現防守姿態下現金長期補不到緩衝，先查這個互動（`forced_swap`、`turnover_fit`、`cash_ceiling_fix`、`drawdown_derisk` 四方）。測試見 `tests/decision/test_gate_prerequisites.py`。
