# 名稱對照

這份表只回答兩件事：某個程式名稱「是什麼」、「在哪裡定義或設定」。它**不寫啟用狀態與目前數值**——哪些閘門已開啟、門檻是多少，一律以 `config/decision_policy.json`、`config/decision_rules.json` 與 [status.md](status.md) 為準；規則為什麼這樣設計見 [decision-details.md](decision-details.md)，每日流程見 [architecture.md](architecture.md)。

- 「位置」欄的 `policy` 指 `config/decision_policy.json`（多數在 `position_sizing` 底下），`rules` 指 `config/decision_rules.json`，`bundle` 指決策輸入 `DecisionInputBundle` 的欄位；程式路徑省略 `src/etf_agent/` 前綴。
- `position_sizing`／`rules` 底下的欄位都是「版本閘門」：只由人設定、策略反思不得改；沒有該欄位的舊 Policy／舊 run 逐位元維持原重建。這條總則不在各列重複。
- 「旗標」欄是程式在配置或結算時留下的代碼；「—」表示該項不產生旗標。

## 信心、入選與份額

| 名稱 | 白話意思 | 設定或程式位置 | 旗標 |
| --- | --- | --- | --- |
| `method`（`conviction_volatility_v1`／`conviction_volatility_v2`） | 信心等級換算成目標權重的方法；v1 固定 1／ATR14，v2 可調低波動偏好的強度 | policy；`decision/sizing.py` `conviction_weights` | — |
| `conviction_multipliers` | high／medium／low 三個等級的份額乘數 | policy；`decision/sizing.py` | — |
| `volatility_exponent`、`volatility_floor` | v2 的波動度次方（0 為完全不看波動度）與 ATR 下限 | policy；`decision/sizing.py`、`decision/allocation.py`；反思旋鈕白名單 `strategy_reflection/contracts.py` | — |
| `positions_by_breadth` | 風險 Agent 的持股廣度（concentrated／balanced／diversified）對應的持股檔數 | policy；`decision/sizing.py` `breadth_target` | — |
| `selection_tiebreak`（`atr_low`／`symbol`／`momentum_high`） | 同信心等級並列時的挑股次序；有入選排序時由名次取代 | policy；`decision/sizing.py`、`decision/allocation.py` | — |
| `selection_ranking`、`rank_by_symbol` | 交易 Agent 第二輪只在同信心等級內排先後；程式轉成逐檔名次綁進 policy，配置排序鍵為（信心、名次、代號） | policy；`decision/selection_ranking.py`（`SelectionRankingValidator`、`apply_selection_ranking`）；Skill `tradeagent/trading/trader/references/ranking.md` | — |
| `limit_scaled_conviction`（`version`、`alpha`） | 高信心份額至少為 alpha × 該股單股上限；必要份額超過預算時依名次填到用完 | policy；`decision/sizing.py` `alpha_floor_weights` | — |
| `held_add_priority` | 既有持股加碼優先於未持有的新股 | policy；`decision/allocation.py` | — |
| `held_weight_budget` | 已持股權重高於信心份額時，該檔不再分配新預算的處理 | policy；`decision/allocation.py` | `RISK_HELD_WEIGHT` |
| `stock_weight_fit`、`stock_limit_headroom`（`LIMIT_HEADROOM_BUY`） | 單股硬性上限的減碼（程式一律執行，不依 `stock_weight_fit` 開關）；有買單的股票在上限下留空間 | policy；`decision/allocation.py` `_fit_stock_weights`、`decision/sizing.py` | `RISK_STOCK_WEIGHT` |

## 滿檔換股

| 名稱 | 白話意思 | 設定或程式位置 | 旗標 |
| --- | --- | --- | --- |
| `rotation`（`version`、`max_swaps_per_run`、`min_new_conviction`） | 持股滿檔時，未持有的新股只能用本次出場釋出的名額；名額配對由程式全域計算 | policy；`decision/allocation.py`、`decision/sizing.py` | — |
| `portfolio_slots` | 程式算好的名額脈絡（持股檔數、硬上限、空名額、全池多空強度統計），只給交易 Agent 當背景，`citable=false` | `decision/rotation.py`（`build_portfolio_slots`、`validate_portfolio_slots`）；封存於 `team_inputs` | — |
| `rotation.forced_swap`（`version`、`min_new_conviction`、`max_swaps_per_run`、`confirmation`） | 滿檔又沒人出場時，程式換掉「交易 Agent 標 hold、且論點檢視判定 broken」的持股；`confirmation` 要求被換出者有本次採納的該股空頭 claim | policy；`decision/allocation.py` | `RISK_FORCED_SWAP`、`RISK_FORCED_SWAP_UNCONFIRMED` |
| `forced_swap.broken_symbols`、`forced_swap.verdicts_ref` | 每日流程把論點檢視的 broken 名單與來源指紋綁進 policy；重建只讀 policy、不讀結論檔 | `decision/thesis_verdicts.py` `bind_forced_swap`；來源 `var/strategy_reflection/thesis_verdicts.json` | — |

## 現金姿態與緩衝

| 名稱 | 白話意思 | 設定或程式位置 | 旗標 |
| --- | --- | --- | --- |
| `cash_buffer_by_stance` | aggressive／neutral／defensive 三種姿態各自對應的現金比例（沒有 `cash_ratio_range` 時使用） | policy；`decision/sizing.py`、`automation/risk_stages.py` | — |
| `cash_ratio_range`（`version`、`grid`、`max_daily_raise`、流程綁入的 `anchor`） | 姿態內再分 lower／middle／upper 三格現金比例；單日只限制現金拉高幅度；`anchor` 是前次 run 的最終緩衝（沒有前次時為本次起始現金比例） | policy；`decision/cash_ratio.py`（`bind_cash_ratio_anchor`、`validate_cash_ratio_anchor`）、`decision/sizing.py`（`cash_grade_grid`、`allowed_cash_grades`） | — |
| `cash_grade`（lower／middle／upper） | 風險 Agent 在姿態內選的現金子等級，仍是列舉、不含數字 | 風險 Agent 輸出 `cash_stance.cash_grade`（proposal 的 `position_sizing.cash_stance`）；驗證 `decision/sizing.py` `validate_cash_stance`、重算 `decision/finalization.py` | — |
| `cash_grade_grid`、`cash_grade_allowed_by_stance`、`cash_ratio_anchor` | 風險 Agent 簡報欄位：格點、今日各姿態可選的子等級、前次現金比例 | `automation/risk_stages.py` | — |
| `cash_buffer_floor` | 現金已低於姿態緩衝時，緩衝只擋買單動用原有現金，不擋用本次賣款買的替補 | policy；`decision/allocation.py` | — |
| `high_conviction_cash_floor`（`version`、`floor_ratio`）、簡報 `high_conviction_cash_floor_by_stance` | 高信心買單可動用原有現金到「姿態緩衝 × floor_ratio」；防守減碼的觸發與目標同改為這條線 | policy；`decision/sizing.py`（`high_conviction_floor_ratio`）、`decision/allocation.py`；簡報 `automation/risk_stages.py` | `RISK_CASH_SOFT_FLOOR` |
| `increase_cash_buffer`、`CASH_BUFFER_CEILING_MARGIN` | 風險 Agent 修正循環可要求提高現金緩衝；其值不得高於競賽現金上限減去固定邊際（有 `cash_ratio_range` 時也不得高於今日可選的最高格點） | `decision/risk.py`、`decision/cash_ratio.py`；輸出結構 `automation/agent_schemas.py` | — |

## 現金分配與執行面

| 名稱 | 白話意思 | 設定或程式位置 | 旗標 |
| --- | --- | --- | --- |
| `cash_fit_order`、`cash_fit_min_fill_ratio` | 現金不足時依信心、名次分錢；未持有新股殘量低於目標 × 比例就整筆取消 | policy；`decision/allocation.py` `_fit_cash` | `RISK_CASH_FIT` |
| `high_conviction_cash_priority` | 可動用現金先全給高信心（high 不取消零頭）；high 被縮減後不流向未持有的中低信心新股 | policy；`decision/allocation.py` | — |
| `cash_ceiling_fix` | 現金逼近競賽上限時留住交易 Agent 標 exit 的持股 | policy；`decision/allocation.py` `_run_min_positions` | `RISK_CASH_CEILING` |
| `buy_gap_reserve`、`buy_gap_reserve_rate` | 買單預留跳空緩衝 | policy；`decision/allocation.py`、`decision/sizing.py` | — |
| `insufficient_funds_policy: void_batch` | 虛擬帳本模擬成交時現金不足整批作廢（與主辦方口徑一致） | policy；`ledger/execution.py`、`virtual_account/service.py` | — |
| `reuse_sell_proceeds` | 賣款當日可用（淨額交割） | rules；`ledger/execution.py`、`ledger/account.py` | — |
| `sector_fit`、`max_sector_weight` | 產業上限只按整張縮減當日買進增量，不賣既有持股 | `sector_fit` 在 policy、上限在 rules；`decision/risk.py`、`decision/allocation.py` | `RISK_SECTOR_WEIGHT` |
| `turnover_fit`、`max_turnover_rate` | 換手上限：先逐筆縮減買單；賣單本身就超過時依 ATR 留住出場持股 | policy；`decision/allocation.py` | `TURNOVER_LIMIT`（逐筆縮減）、`RISK_TURNOVER`（留住出場） |
| `reentry_cooldown`（`version`、`days`、流程綁入的名單） | 出場後 N 日內不再買進未持有的新股；名單由每日流程從決策庫取得 | policy；`decision/reentry_cooldown.py`（`recent_exit_symbols`、`bind_reentry_cooldown`）、`decision/allocation.py` | `RISK_REENTRY_COOLDOWN` |
| `min_positions`、`max_positions` | 持股檔數的硬性範圍；低於下限時程式依 ATR14 留住標 exit 的持股 | rules；`decision/allocation.py` `_run_min_positions`、`decision/risk.py` | `RISK_MIN_POSITIONS` |
| `minimum_active_share` | 與必備主動型 ETF 前 15 大的 Active Share 下限；不足時取消重疊最多的買單重配 | rules；`decision/active_share.py`、`decision/allocation.py`、`decision/risk.py` | `RISK_ACTIVE_SHARE` |
| 不可交易標的 | 交易狀態包非 `allowed` 的標的不建買單、續抱不賣 | `decision/allocation.py` | `RISK_NOT_TRADABLE` |
| `prev_day_orders.outcome=voided` | 主辦方資料包判定前一日委託整批作廢，帳本結算為無成交 | `competition/datapack.py`；`ledger/execution.py`、`virtual_account/service.py` | `HOST_VOIDED` |

## 回撤與減碼

| 名稱 | 白話意思 | 設定或程式位置 | 旗標 |
| --- | --- | --- | --- |
| `drawdown_control`（`neutral_at`、`defensive_at`、`release_neutral_below`、`release_defensive_below`） | 帳本 NAV 回撤達門檻時強制現金姿態下限；解除門檻帶 hysteresis | policy；`decision/drawdown.py`（`bind_drawdown`、`stance_floor`） | — |
| `history_ref`、`ledger_nav_history_provider` | 帳本 NAV 歷史的指紋（head_run_id、length、sha256）與重建時讀帳本的注入點 | `decision/drawdown.py` `verify_history_reference`；`virtual_account/service.py` | — |
| `drawdown_derisk`（`version`、`max_derisk_fraction`） | 防守姿態下程式依 ATR 把持股減碼到緩衝，單次減碼有上限；各版本差異見 [decision-details.md](decision-details.md) | policy；`decision/allocation.py`、`decision/sizing.py` | `RISK_DRAWDOWN` |

## 交易 Agent 的背景輸入

| 名稱 | 白話意思 | 設定或程式位置 | 旗標 |
| --- | --- | --- | --- |
| `position_review`（`version`、`loss_alert`、`retrace_alert_20d`、`gain_alert`、`context`） | 持股檢視：程式算帳面報酬與近 20 日高點回落並依門檻標警示；`context` 另附相對交易池報酬與基本面可得時間 | policy；`decision/position_review.py`（`build_position_review`、`validate_position_review`） | 警示 `review:<代號>:<旗標>`、持股事實 `review:<代號>:position` |
| `prior_decision_context` | 前次決策背景：同帳戶、更早時段、通過驗證的封存 run 的持股意圖與重估條件；`citable=false` | `decision/prior_decision.py`（`find_prior_decision_run`、`PriorDecisionContextValidator`） | — |
| `thesis_review_background` | 每週論點檢視的結論，只給交易 Agent 當持股背景，不是證據 | `decision/thesis_verdicts.py`；來源 `var/strategy_reflection/thesis_verdicts.json`；放入簡報 `automation/team_stages.py` | — |
| `strategy_lessons` | 反思旁觀者整理的經驗教訓，只是背景、不可引用 | `decision/strategy_lessons.py`；`config/strategy_lessons.json` | — |
| `team_inputs` | 每個 Decision run 另存的分析團隊輸入（報告、現金姿態、前次背景、名額、持股檢視、入選排序、基本面指標），版本沿革見 [architecture.md](architecture.md#decision-run-的-team_inputs-版本) | `decision/finalization.py` `build_team_inputs` | — |

## 證據、輸入與驗證

| 名稱 | 白話意思 | 設定或程式位置 | 旗標 |
| --- | --- | --- | --- |
| `evidence_policy: typed_evidence_v1`、`claim_kind`、`finding_kind` | 證據有種類；個股 claim／finding 必填種類並至少引用一個屬於該股票且對映的證據 | bundle；`decision/input_context.py`（`CLAIM_KINDS`）、`decision/input_validator.py` | — |
| `fundamental_period_policy: per_symbol_latest_available_v1`、`fundamental_metrics` | 逐檔取截止前最新可得的一季；程式算好的基本面指標存進 `team_inputs` 並整份重算 | bundle；`decision/analysts.py`（`compute_fundamental_metrics`、`validate_fundamental_metrics`）、`decision/input_builder.py` | — |
| `shared_input_sha256`、`peer_packet_ids` | 多頭／空頭研究員共同輸入的雜湊；互讀禁止要求 `peer_packet_ids` 為空 | `decision/stance.py` | — |
| `agent_calls.revision_diff` | 驗證回饋後重試時，前後輸出在意圖／信心／強度／claims 數的差異 | `automation/pipeline_base.py` `output_revision_diff` | — |

## 快照與子 Agent 執行器

| 名稱 | 白話意思 | 設定或程式位置 | 旗標 |
| --- | --- | --- | --- |
| `PRICE_DATE_FALLBACK_<最新日>_MISSING_<代號,…>` | 最新交易日有檔缺行情時退回前一個全池齊全日的資訊旗標；快照仍可用 | `data/snapshot.py` `SnapshotQualityPolicy` | 本身即旗標 |
| `agent_runner.SENSITIVE_READ_GLOBS` | Claude 與 OpenCode 子 Agent 的拒讀檔案樣式清單 | `automation/agent_runner.py` | — |
| `stage_isolated`、`research_isolation` | 多空研究輸出暫存（Codex 的時序隔離）；`pipeline.json` 記錄本次用的隔離方式 | `automation/pipeline_base.py`、`automation/agent_runner.py`；`scripts/run_daily_pipeline.py` | — |
