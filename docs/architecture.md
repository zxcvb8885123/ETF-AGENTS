# 目前每日決策架構

本文件說明已接線的每日決策流程。

- 欄位細節以 `src/etf_agent/` 的契約與 Validator 為準。
- Agent Skill 位於 `tradeagent/`。
- 流程產出供人工檢視的封存決策與中文報告，**不自動下單或送件**。

## 流程總覽

```text
資料與 ResearchSnapshot → 技術／基本面／事件分析師
  → 同輸入且互相隔離的多頭／空頭研究員
  → 交易 Agent → 風險 Agent
  → Python 配置、情境、CompetitionGuard → Decision run 與報告
```

原則：**確定性歸程式，不確定性歸 LLM。**

| 階段 | Agent 的判斷 | Python 的確定性工作 |
| --- | --- | --- |
| 資料 | 無投資判斷 | 保存來源、原始回應、`available_at`、版本與不可變 Snapshot；依 cutoff 排除未來資料 |
| 三位分析師 | 全池逐檔輸出 outlook、事件重大性與有證據的發現；全市場情緒只輸出一次 | 計算指標與財務比率、分批與覆蓋驗證；市場情緒由程式依全市場新聞或恐懼貪婪指標重建，兩者都不可用時輸出 `unavailable` |
| 多頭／空頭 | 同一共同輸入、互相隔離，逐檔提出有證據的 claims 與強度 | 核對 `shared_input_sha256`、引用歸屬、全池覆蓋、`peer_packet_ids` 為空 |
| 交易 | 逐檔取捨每個多空 claim，給買賣意圖與 buy／add 信心等級；可看前次決策對持股的意圖與重估條件，但只當背景 | 驗證每個 claim 剛好被採納或否決一次、文字欄位沒有引用前次決策；依 Policy、ATR 與風險現金姿態計算權重與張數 |
| 風險 | 配置前給現金姿態與持股廣度（集中／平衡／分散）；配置後審查並提出受限修正 | 從帳本 NAV 歷史算峰值回撤並強制姿態下限；持股廣度依 Policy 換算成 22／26／30 檔，照信心與支持數挑股（啟用 `selection_ranking` 後改為信心、名次、代號）；提供整體市場情緒與每筆委託佔 20 日均量比例；重算費稅、現金、情境與競賽限制；硬性規則失敗即拒絕，修正循環最多 3 次 |

## 持股檔數與取捨順序

持股檔數由風險 Agent 的持股廣度決定：`concentrated`（集中）、`balanced`（平衡）、`diversified`（分散），對應 `config/decision_policy.json` 的 `positions_by_breadth`（目前 22／26／30 檔，配置時夾在 `min_positions`～`max_positions`）。集中設 22 而非 20，是留兩檔緩衝給 Active Share 或不可交易剔除。Agent 只選等級，不輸出檔數；程式把廣度綁成 Policy 的 `position_breadth`，Finalizer 依 `team_inputs.cash_stance.breadth` 重建核對。

買進候選超過該檔數時，程式依下列順序決定誰先入選，其餘標為 `MAX_POSITIONS`：

1. 信心等級：`high`、`medium`、`low`。
2. 同等級時，交易決策採納的多頭 claim 數減空頭 claim 數，越大越先（`support_by_symbol`，綁在 Policy 並由 Validator 重算）。
3. 仍相同時，依 `position_sizing.selection_tiebreak`：`atr_low`（ATR14 占價格比例低者先，省略欄位時的預設與舊行為）、`symbol`（不看波動度）、`momentum_high`（20 日報酬高者先，缺值排最後）。
4. 再相同時，依股票代號。

未持有的新股只能用賣出後空出的檔數：持股已達上限時，新股不佔名額（標 `MAX_POSITIONS`），已持有的 add 候選照常入選，不會被買不進去的新股擠掉。此行為由 `position_sizing.held_add_priority`（目前 `v1`）開啟；2026-10-28 前封存、沒有此欄位的舊 Decision run 維持新股可佔滿檔名額的原重建行為，確保仍能通過重建驗證。

強制換股由 `position_sizing.rotation.forced_swap`（目前 `v1`，版本閘門，只由人設定）開啟：持股滿檔又沒人出場時，信心達門檻（目前 `high`）的未持有新股，由程式換掉交易 Agent 標為 hold、且每週論點檢視判定「理由被推翻（broken）」的持股（名單由每日 pipeline 綁進 Policy 的 `forced_swap.broken_symbols`＋`verdicts_ref`，事後重建不讀結論檔；沒有名單就不換）（每次最多 2 檔，賣單意圖標 `exit`、旗標 `RISK_FORCED_SWAP`）。沒有此欄位的舊 run 維持原行為。

現金緩衝下限由 `position_sizing.cash_buffer_floor`（目前 `v1`，版本閘門，只由人設定）開啟：現金已低於姿態緩衝時，緩衝只擋買單動用原有現金，不擋用本次賣出收入買的替補（下限取 `min(緩衝, 開始現金)`），讓出場釋出的名額不會因現金湊不到緩衝而流失。沒有此欄位的舊 Decision run 維持緩衝一律生效的原重建行為。防守姿態主動降曝險由另一個閘門 `drawdown_derisk` 開啟（見下方「2026-11-03 晚間的風控閘門」），減碼收入不受 `cash_buffer_floor` 放行、不拿去買替補。

強制換股自 `forced_swap.confirmation: "v1"`（版本閘門，只由人設定）起要有本次證據：被換出的持股除了在 `broken_symbols` 名單內，還必須有交易 Agent 本次在 `adopted_claim_ids` 採納的該股空頭 claim（`bear-` 開頭）；沒有即不換、記 `RISK_FORCED_SWAP_UNCONFIRMED`（只記錄原因，不擋決策）。同閘門內換出數不超過「出場後持股數 − `min_positions`」與現金（含本次賣出收入，依 `reuse_sell_proceeds`）能支應一張新股的檔數；被換出者若替補因現金／換手被取消而使持股數不足或換手超限，可被 `RISK_MIN_POSITIONS`／`RISK_TURNOVER` 留住。配置程式只讀交易決策，拿不到多空 debate，所以「空頭 strength ≥ moderate」這一條沒有實作（需 Finalizer 另綁定與重建）。旗標文案改為與實作一致（依代號換出，不是 20 日報酬）；沒有 `confirmation` 的舊 Policy 文案與行為逐字不變。

### 交割口徑與 2026-11-03 的執行面閘門

主辦方 DataPack 規格 §2.4 寫「交割以淨額計，同日賣出款也計入結算」，比賽辦法以全數成交、當日結算現金為負才罰，帳本 `account_snapshot.cash`（含未交割）也已與 DataPack 現金對帳吻合，所以 `config/decision_rules.json` 自 `decision-rules-2026-11-03` 起 `reuse_sell_proceeds: true`（之前為 `false`，使 `cash_buffer_floor` 形同死碼）。rules 會封進 Decision run 的 `decision_input.json`（`config_sha256` 由 `DecisionInputBuilder` 計算），Finalizer、Validator、虛擬帳戶結算與評測回放都只讀封存的那份，不讀 live 檔，所以舊 run 照舊以 `false` 重建；live 檔的讀者只有策略反思的旋鈕範圍、基準比較與儀表板的費稅設定，不影響重建。配置用的現金仍以 `settled_cash` 加本次賣出收入為底，不先用前幾日未交割賣款（較主辦方保守）。

同日另加四個版本閘門（都在 `position_sizing`，只由人設定、策略反思禁區；沒有該欄位的舊 Policy／舊 run 逐位元維持原重建行為，`effective_policy` 也只在有欄位時才多出對應鍵）：

- `cash_fit_order: "v1"`（參數 `cash_fit_min_fill_ratio`，預設 `0.5`）：現金不足時買單改依（信心、`rank_by_symbol` 名次、代號）順序取得現金，不再依代號；未持有新股的殘量低於目標股數 × 比例就整筆取消、留 `RISK_CASH_FIT` 旗標，現金給下一檔，不留 1～4 張的塵埃持股（11/03 的 6239.TW 只買到 4,000 股＝0.10% NAV 卻占一個名額）；已持有的 add 不受影響，只減少增量（維持 `CASH_BUFFER`）。輸出的訂單與旗標仍依（賣先買後、代號）排列。
- `cash_ceiling_fix: "v1"`：配置後 base 情境期末現金比例達 `cash_weight_ceiling`（25%）時，程式依 ATR14 由低到高（同值依代號）留住交易 Agent 標為 `exit` 的持股續抱（`RISK_CASH_CEILING`，`forced_exit` 不留、不可交易者不動），一檔一檔加到低於上限或沒有可留的 exit；與 `RISK_MIN_POSITIONS` 共用同一個留住集合、同一個迴圈，每輪至少多留一檔所以必定收斂。沒有可留的 exit 時照舊由 Guard `CASH_WEIGHT` 拒絕。
- `stock_limit_headroom: "v1"`：比賽規則「因加碼導致超限當日即警告，價格波動超限才有 5 日寬限」，所以當日有 buy／add 的股票單股上限封頂改留 3%（×0.97，2330 為 25%×0.97），只續抱的仍 ×0.995；`_fit_stock_weights` 每輪重算係數，買單被減光後該股回到 0.995，不會因價格漂移被多賣。D-Plan 的 `_validate_official_posture` 用的是 10%／25% 原上限，不需配合。
- `buy_gap_reserve: "v1"`（參數 `buy_gap_reserve_rate`，預設 `0.03`）：決策用前日收盤、成交用當日均價，買單累計總額 × 比例另計入現金需求（`現金 − 買單 − 累計買單×比例 ≥ 緩衝`），避免跳空開高讓主辦方以資金不足整批作廢。

虛擬帳本另有 `insufficient_funds_policy: "void_batch"`（同屬 `position_sizing`，由結算時封存的 policy 讀出）：模擬成交時任一買單湊不到現金就整批作廢（含賣單），與主辦方相同；沒有此欄位的舊 run 維持逐張減少。

### 2026-11-03 晚間的風控閘門

同日晚間再加四個版本閘門（都在 `position_sizing`，只由人設定、策略反思禁區；沒有該欄位的舊 Policy／舊 run 逐位元維持原重建行為，6 份封存 run 10/27～11/03 重建驗證全數通過）與兩個數字：

- **產業上限 0.6**：`config/decision_rules.json` 的 `max_sector_weight` 由 1（關閉）改為 `0.6`（版本 `decision-rules-2026-11-03b`，`available_at` 2026-11-03 20:30，早於當晚 21:00 的 cutoff；舊 run 讀封存的 `decision-rules-2026-10-25`）。產業上限不是比賽硬規則、是本隊自設的風控，所以 `sector_fit: "v1"` 啟用時 `_fit_sector_weights` 只按整張減少當日 buy／add 的增量（同產業先減低信心、情境市值大者），**不賣既有持股**；減光買單仍超限的產業（價格漂移造成）由 Guard 的 `SECTOR_WEIGHT` 與三個情境以「既有已超限且本次沒有買進該產業」容忍，details 記為 `carried`；有買進的產業仍是硬性違規。沒有閘門的舊路徑維持「先減買單、再賣持股」並由 Guard 嚴格判定。目前持股最大產業約 26%，0.6 不會立即觸發。
- **換手上限 0.3**：`config/decision_policy.json` 的 `max_turnover_rate` 由 1 改為 `0.3`（換手 = 買賣 gross 合計 ÷ 決策時 NAV，Guard 的 `TURNOVER` 同口徑）。`turnover_fit: "v1"` 啟用時 `_fit_cash` 不再逐張擋換手，改由配置最後一步 `_fit_turnover` 依（信心低→高、ATR14 高→低、代號）縮減買單：已持有的 add 只減增量、未持有的新股殘量低於目標 × `cash_fit_min_fill_ratio` 就整筆取消（換股數減少），每筆記 `TURNOVER_LIMIT`；買單減光仍超過（賣單本身超過）時由 `_run_min_positions` 依 ATR14 由低到高留住 exit 持股（`RISK_TURNOVER`），不讓 Guard 整份拒絕。風險 Agent 的 `reduce_turnover_limit` 修正照樣走同一條縮減。舊路徑依取得現金的順序逐張擋換手、超過即由 Guard 拒絕，逐位不變。
- **出場冷卻** `reentry_cooldown: {"version": "v1", "days": 3}`：每日 pipeline 以 `decision/reentry_cooldown.py::bind_reentry_cooldown` 從決策庫找同帳戶、`approved`、目標交易時段早於本次且相隔不超過 `days` 個日曆天、cutoff 更早的 run，取其最終 `decision.orders` 中 `side=sell` 且訂單意圖 `exit`／`forced_exit`（含程式強制換股與風控減至零）的股票，綁成 `blocked_symbols`＋`source_run_ids`；配置時未持有的買進候選在名單內就不建買單（`RISK_REENTRY_COOLDOWN`，已持有的 add 不受影響）。這是決策層的出場紀錄（主辦方作廢整批時該股仍在冷卻內，屬保守方向）；事後重建只讀 Policy、不重掃決策庫（與 `broken_symbols` 同做法）。`prior_decision_context` 只涵蓋前一個 run 的持股、沒有出場資訊，所以不用它。沒有目標時段或找不到決策庫時綁空名單，不是錯誤。
- **回撤解除門檻與防守減碼** `drawdown_derisk: {"version": "v1", "max_derisk_fraction": "0.25"}`，與 `drawdown_control` 的 `release_neutral_below: "0.03"`、`release_defensive_below: "0.07"` 成對（只有一邊即拒絕）：(a) 姿態下限改由 `stance_floor_path` 沿帳本 NAV 歷史逐筆重放狀態機——達 10% 進 defensive、達 5% 至少 neutral；在 defensive 時回撤要低於 7% 才降回 neutral、在 neutral 時要低於 3% 才解除，避免在 5% 附近日日切換；`DecisionPolicyValidator` 有帳本歷史時整段重放比對（Finalizer 對帶 `history_ref` 的 run 一律讀帳本），沒有歷史時只能確認下限不低於無記憶門檻。(b) 現金姿態為 `defensive` 且交易 Agent 的賣單算進現金後仍低於姿態緩衝（20% NAV）時，程式依（ATR14 高→低、信心低→高、代號）把持股按整張減碼到緩衝，一次最多賣 NAV × `max_derisk_fraction`，每檔記 `RISK_DRAWDOWN`、訂單意圖標 trim／exit，不動不可交易、被留住的 exit 與交易 Agent 已標 exit／trim 的持股，持股數不低於 `min_positions`（必要時留一張）；減碼收入不受 `cash_buffer_floor` 放行、不拿去買替補。(c) 配置預算 ≤ 0（現金已低於姿態緩衝）時候選的旗標由誤導的 `LOT_SIZE` 改為 `CASH_BUFFER`。沒有閘門的舊 Policy 維持無記憶門檻與「防守只停買、不降曝險」。

啟用 `position_sizing.rotation` 時，名額改由程式全域配對（須與 `held_add_priority` 同時存在）：

- `cap = max(廣度目標, min(目前持股數, max_positions))`：廣度收窄只停止淨增加，不再凍結既有持股的加碼。
- `growth = max(0, 廣度目標 − 目前持股數)` 是目標之下的真正空名額，不設信心門檻。
- `swap = min(本次實際出場檔數, max_swaps_per_run)` 是出場釋出的名額，只給信心不低於 `min_new_conviction` 的新股，否則標 `ROTATION_CONVICTION`。
- 新股總數不超過 `max_positions − 配置後持股數`；出場與空名額都為 0 時新股數必為 0。

交易 Agent 會在 brief 與 `team_inputs` 2.3 收到程式算好的 `portfolio_slots`（持股檔數、硬上限、空名額、全池多空強度統計）當背景，知道滿檔時買新股必須有持股出場；它不指定誰換誰，配對是程式的工作，且出場必須採納該股票的空頭 claim 或持股檢視警示。

### 入選排序

交易 Agent 逐批執行，各批看不到其他批的候選，因此信心等級只有 high／medium／low。同一等級有兩檔以上候選時，交易 Agent 的第二輪（`run_selection_ranking`，Skill `trader` 的 `references/ranking.md`，不另設 Agent）一次看全部 buy／add 候選與程式算好的 20 日報酬、ATR，只在同等級內排先後並附理由；各等級只有一檔時由程式直接封存，不呼叫 Agent。`decision/selection_ranking.py` 驗證排序剛好涵蓋每個等級的候選，並把名次綁成 `position_sizing.rank_by_symbol`（含 `selection_ranking_id`／`selection_ranking_sha256`）；配置排序鍵為（信心、名次、代號）。`team_inputs` 升為 2.5（2.4 再加 `selection_ranking`），Finalizer 重建驗證；樣板沒有 `selection_ranking` 時維持原排序。

### 持股檢視（停損／停利）

`config/decision_policy.json` 的 `position_review`（`version`、`loss_alert`、`retrace_alert_20d`、`gain_alert`，不啟用的門檻寫 `null`）啟用時，程式（`decision/position_review.py`）依帳戶成本與同 Snapshot 行情算出每檔持股的帳面報酬、近 5／20 日高點回落與選用的持股事實編號（`review:<代號>:position`），門檻預設全為 `null`（停損停利由交易 Agent 自行判斷）；人另設門檻時才標出必須處理的警示（`review:<代號>:<旗標>`）。警示隨交易 Agent 的 brief 與 `team_inputs` 2.4（2.3 再加 `position_review`）封存；Finalizer 以 bundle 與 policy 重算整份比對，改寫即拒絕。

- 警示不是賣出指令，也不是固定百分比就賣：賣、減碼或續抱仍由交易 Agent 判斷，門檻只決定「何時必須正視」。
- 若有設門檻：警示與 claim 一樣必須在 `adopted_claim_ids` 剛好處理一次，不可否決；`trim`／`exit` 可採納警示取代空頭 claim；`loss_alert` 的持股要 hold／add／no_trade 必須同時採納多頭 claim。
- 比賽開帳或重建日的成本是當日結算收盤價，不是真實買價（警示內附固定 note）。
- 停損停利預設不設死門檻，由交易 Agent 依檢視事實判斷，`trim`／`exit` 可引用 `position_id` 當依據；人另設門檻時，策略反思旁觀者只能在試用觀察期保護下小幅調整 `loss_alert`／`retrace_alert_20d`（門檻為 null 時不可調，`position_review.version`、`gain_alert` 列入禁區）；沒有 `position_review` 的 policy 維持 2.3 行為。風險 Agent 不處理停損停利。

舊 Decision run 沒有 `positions_by_breadth`，仍填滿 `max_positions` 且不看 ATR；更舊的沒有 `support_by_symbol`，只依等級與代號。策略回放遇到沒有持股廣度的舊封存時，拿掉樣板的 `positions_by_breadth` 沿用舊行為並在報告註明。

## 對應參賽策略文件的四類因子

參賽策略文件（`game_docs/ETF-AGENTS_AI_Agent_選股與決策策略_參賽格式.pdf`）寫的因子，都由程式計算後交給 Agent 判讀：

| 文件寫的 | 程式提供 | 交給誰 |
| --- | --- | --- |
| 基本面：營收、獲利、負債、現金流與期間比較 | 財務比率（逐檔以該股最新可得的一季計算，見下方「基本面期別」）、月營收、`cash_flow`（營業現金流、資本支出、自由現金流、營業現金流對稅前淨利比率） | 基本面分析師 |
| 技術：5／20／60 日變化、均線、ATR 與下行波動 | `return_5d／20d／60d`、`ma20`、`ma60`、是否站上均線、`atr_14_pct`、`downside_volatility_20d` | 技術分析師 |
| 公司事件：事件影響、反證與尚待確認事項 | 重大訊息原文、同快照財報，以及截止時已核准的 FinMind 個股新聞標題（白名單媒體、截止前最後一批；新聞標 `high` 須在 `corroborated_by` 指出重大訊息或另一家媒體的佐證）；Agent 逐則輸出 `summary`、`counter_evidence`、`pending_confirmations` | 事件分析師 → 多空研究員 |
| 市場背景與風險：整體市場情緒、集中度、流動性與壓力情境 | `market_sentiment`、產業曝險、`liquidity_exposure`、壓力情境、峰值回撤 | 風險 Agent |

### 基本面期別

基本面比率由 `decision/analysts.py::compute_fundamental_metrics` 以 `fundamentals` 的 `FundamentalDataBundle`／`FundamentalMetrics` 契約確定性計算，期別的選法由決策輸入的版本閘門決定：

- **新行為**（`DecisionInputBundle.fundamental_period_policy = "per_symbol_latest_available_v1"`，`DecisionInputBuilder` 建新 bundle 時預設寫入；`DecisionInputValidator` 只接受已知值）：`select_fundamental_periods` 逐檔取該股在 Snapshot（即 cutoff 前可得）中損益表與資產負債表都齊的最新一季（`symbol_latest_complete`）；沒有任一季兩表齊全時取任一報表的最新季（`symbol_latest_any`，缺的報表照常標 `CURRENT_STATEMENT_MISSING`）；完全沒有財報的股票沿用全池最大期別（`pool_max_fallback`，結果必為 unavailable，只為覆蓋全部交易池）。比較期一律同季去年。程式依期別分組，每組各建一份 `FundamentalDataBundle` 並計算，再合併成 `fundamental_metrics`（schema 1.1：`period_policy`、`period_by_symbol`、`period_groups` 記錄每檔實際期別與每組 bundle／metrics 雜湊，`items` 逐檔）；`validate_fundamental_metrics` 以同一份決策輸入整份重算比對。基本面 brief 每檔多附 `financial_period`，告訴分析師該檔用的是自己最新可得的一季。
- **舊行為**（bundle 沒有此欄位）：全快照 `max(period)` 當所有股票的當期，輸出逐位元不變，舊 run 重建不受影響。2026-11-03 實測顯示舊做法在少數公司提早公布 Q3 時，其餘 132／150 檔都被標 `CURRENT_STATEMENT_MISSING`。
- 不以 `data/financial.py::latest_due_quarter` 的法定公告期限推測（那是財報收集的驗收分母），而以逐股實際可得性為準。

### 快照交易日退回

`SnapshotRepository.latest_trade_date_coverage` 選快照交易日：全池每檔都有定案行情的最新日；最新日 D 有任一檔缺行情時整池退回前一個齊全日 D−1（與原 `find_latest_trade_date` 相同），並由 `SnapshotQualityPolicy` 在 `quality_flags` 留下資訊性旗標 `PRICE_DATE_FALLBACK_<D>_MISSING_<代號,…>`（超過 20 檔只列前 20 個並註明餘數）。資訊性旗標不影響 `usable`；其餘旗標（`MISSING_PRICE_DATA`、`INCOMPLETE_PRICE_COVERAGE_*`、交易池驗證）仍是阻擋性。沒有改成「以 D 為最新日、缺檔逐檔標 `MISSING_PRICE_<代號>`」，因為 `DecisionInputValidator` 要求 `universe_size` 等於最新行情檔數，`SnapshotQualityPolicy` 也會標 `INCOMPLETE_PRICE_COVERAGE` 使快照不可用：單檔缺價會讓整份決策 fail-closed，而不是只擋該檔；退回 D−1 是較保守的降級。舊 Snapshot 的 `quality_flags`／`usable` 都是封存值，不重算。

### 回撤控制

- 程式沿帳本 `parent_run_id` 往回取每個狀態的 NAV，峰值回撤 = (峰值 − 目前 NAV) ÷ 峰值。
- Policy 的 `drawdown_control` 設門檻（目前回撤 ≥ 5% 時姿態至少 `neutral`、≥ 10% 時至少 `defensive`），觀察值與下限綁進 Policy，由 `DecisionPolicyValidator` 重算。自 2026-11-03 起另有解除門檻 `release_neutral_below` 3%／`release_defensive_below` 7%（與 `position_sizing.drawdown_derisk` 閘門成對），下限沿帳本歷史重放狀態機，見上方「2026-11-03 晚間的風控閘門」。
- 風險 Agent 給的姿態比下限積極時，程式退回重答；Agent 不計算回撤，也不能放寬。
- 綁定時若歷史列都有 `run_id`（帳本鏈），另存 `history_ref`（`head_run_id`、`length`、歷史 `run_id／as_of／nav` 序列的 canonical sha256）。Validator 在有帳本歷史可讀時（每日 pipeline 綁定後的檢查，`nav_history` 止於 `head_run_id`，可用 `VirtualAccountRepository.nav_history(head_run_id)` 讀）重算參照、`peak_nav`、`peak_as_of`、`history_length` 並逐項比對，把峰值竄改成目前 NAV 會被拒絕；提供歷史卻缺 `history_ref` 同樣拒絕。`history_ref` 屬版本閘門，策略反思只能調兩個門檻、不能動它。
- 事後重建已接入：`DecisionFinalizer`、`DecisionResultValidator` 接受選用的 `nav_history_provider(head_run_id)`；policy 含 `history_ref` 時以 `head_run_id` 讀帳本歷史（`ledger_nav_history_provider(帳本根目錄, bundle)`，依 `account_snapshot.account_id` 開帳本、呼叫 `nav_history(head_run_id)`）重算參照與峰值，帳本讀不到、head 不存在、歷史不符，或呼叫端沒有提供帳本來源，一律 fail-closed 拒絕。已接上的呼叫端：`cli/portfolio_decision.py`（`--ledger-repository`）、每日 pipeline（用綁定 policy 的同一份 `nav_history`）、D-Plan 匯出（`DPlanExporter.build`）、`decision_delivery`（`--ledger-repository`）與虛擬帳戶結算。沒有 `history_ref` 的舊 Policy／舊 Decision run 與沒有 `drawdown_control` 的舊 Policy 不讀帳本、照原樣重建驗證。

### 現金流

- FinMind 現金流量表由 `scripts/collect_cash_flow.py` 核對：同期累計稅前淨利必須等於官方損益表 `profit_before_tax`，才轉成 `cash_flow_statement` 文件（來源 `FINMIND_VERIFIED_CASH_FLOW`，非官方）。
- 決策時以同快照的官方稅前淨利再核對一次，數值由 `cash_flow_context` 計算；金融保險業、資料商未提供或核對不符時標為資料缺口。

### 流動性

- 比賽以當日成交均價結算，不會因成交量只成交一部分；只有整批作廢（資金不足、超賣、合規失敗）或該檔缺成交均價時才不成交。所以壓力情境不另外依成交量限制成交。
- `liquidity_exposure` 列出每筆委託股數佔 20 日平均成交股數的比例，超過 10% 或缺均量時標 `flagged`，只供風險審查判斷，不是硬性規則。

## 主辦方資料包對帳

- 每日 `run` 在建立帳戶快照與交易狀態之後、呼叫任何子 Agent 之前，讀取本隊、本目標交易日的主辦方 DataPack，與帳本快照比對持股股數（必須完全相同）與現金（容忍比率可調）。
- DataPack 是不受信任的外部檔：欄位嚴格白名單，`null` 與空陣列不互相代替；結算未產生、身分或日期不符、對不上時一律停止。
- 結果寫入 `pipeline.json` 的 `datapack`；D-Plan 只接受 `matched` 或明確 `skipped`，並檢查每檔賣出不超過 DataPack 的 `shares`（應收配股不可賣）。
- 不一致時預設停止；明確加 `--rebase-to-datapack` 才以 DataPack 重建帳本（新增 `datapack_rebase` 狀態，舊狀態保留），並強制建立新 Snapshot，使資料包產生時間早於決策截止時間，避免回填未來資訊。
- 實作：`src/etf_agent/competition/datapack.py`；比賽第一天開新帳戶與重建帳本：`src/etf_agent/competition/account_opening.py`。

## 帳本結算

帳本若有前次已封存的決策，必須先用該決策指定成交日的 TWSE／TPEx 官方價量結算，才能建立新的「決策前帳戶快照」。

主辦方結果是地面真相：`DailyAccountRunner` 給了 `datapack_dir` 與 `team_id` 時，結算前會在資料夾找「`as_of_date` 等於成交日」的本隊 DataPack（交易日 T 的資料包在 T−1 結算後產生，其 `prev_day_orders` 就是 T−1 委託的結果；同日多份取 `generated_at` 最新者），`outcome` 為 `voided` 時該決策結算為無成交（每筆訂單列入 `unfilled_orders`、原因 `HOST_VOIDED`，持股現金只做交割與估值），`executed`／`unknown`／`none` 照模擬成交；`host_outcome`（含 DataPack 雜湊、路徑、`as_of_date`）寫進 close 狀態的 `provenance` 與 `transition`，`as_of_date` 與成交日不符或欄位不合規格一律拒絕。找不到資料包或沒給參數時維持純模擬成交，狀態形狀不變。`cli/virtual_account.py daily` 與 `./start.sh daily` 目前尚未把 `var/datapack` 與 `config/competition_profile.json` 的 `team_id` 傳給 runner，所以每日流程還沒真的用到這條路徑（見 `docs/status.md`）。

歷史上櫃價量有缺口時，可用 `scripts/repair_pending_tpex_close.py` 從 TPEx 指定日期的原頁補抓：

- 抓取時間不回填成歷史可得時間。
- 原文與來源雜湊都保留。
- 仍須通過原帳本的結算驗證。

## 價格口徑

| 欄位 | 用途 |
| --- | --- |
| `execution_close_price` | 決策與帳本用價。同日官方原價，附 `execution_price_source` 與 `execution_source_evidence_id` |
| `analysis_close_price` | 調整價，只供研究與技術指標 |

- 同日沒有官方價時，`execution_*` 為空。配置、風控與 D-Plan 退回 `analysis_close_price`。
- 重放過去日期時，退回的價格含事後除權息調整，這是已知限制。
- 舊封存沒有此欄位，仍可重建驗證。
- Yahoo 來源沒有成交金額與成交筆數：`daily_prices` 的 `trade_value`／`transactions` 為 NOT NULL，只能以 0 佔位，Snapshot 的 `latest_prices` 對 `YAHOO_FINANCE` 列輸出 `null`；官方列維持整數。帳本結算與校準只讀官方列的 `trade_value`，不受影響。

## 平行執行與耗時

`--max-workers N`（1～8，CLI 預設 8；程式內 `DailyDecisionPipeline` 預設 1 即串行）讓下列階段同時執行子 Agent：

- 技術／基本面／事件三位分析師的所有批次。
- 多頭與空頭研究員的所有批次。兩方仍只讀同一份共同輸入，任務之間不共享輸出。
- 交易 Agent 的所有批次。各批只讀共同的帳戶、多空與動能輸入，不讀其他批的輸出。

現金姿態與風險審查維持串行。平行只改變執行方式，不改 brief、Validator、Policy 與 Guard：

- 結果依提交順序合併，`agent_calls` 也依提交順序整理，不受完成先後影響。
- 任一任務失敗就取消尚未開始的任務並整體失敗（fail-closed），不使用部分結果。
- `--resume` 沿用以任務名稱區分的 `*_raw_N.json`，平行與串行的續跑互通。
- `pipeline.json` 的 `stage_durations` 記錄 `analysts`、`research`、`trader`、`cash_stance`、`allocation_review` 的牆鐘秒數。
- 同時呼叫越多，越容易撞到訂閱用量或速率上限。
- 實測（2026-10-22，同一模擬帳戶與 Snapshot，各一次）：

  | 階段 | `--max-workers 3`（交易串行） | `--max-workers 6`（交易平行） |
  | --- | --- | --- |
  | 分析師 | 368.0 秒 | 248.8 秒 |
  | 多空研究 | 253.5 秒 | 176.2 秒 |
  | 交易 | 267.3 秒 | 120.8 秒 |
  | 現金姿態 | 15.8 秒 | 28.8 秒 |
  | 配置與審查 | 24.1 秒 | 26.4 秒 |
  | 合計 | 928.7 秒 | 601.0 秒 |
  | 呼叫數／重試 | 27／0 | 28／1（`event_b9` 驗證重試） |

  - 兩次都 `approved`、30 筆委託，沒有撞到用量上限。實測當時 `agent_step` 只在輸出驗證失敗時重試；用量或執行錯誤會直接失敗，所以那次重試是輸出驗證問題，不是限流。（之後新增：CLI 逾時、無法啟動、非零結束、輸出不是 JSON 或沒有結構化輸出屬 `TransientAgentError`，會間隔 30 秒再試一次，失敗嘗試記在 `agent_calls` 的 `transient_error`／`transient_retry`，用量記 0、耗時保留；秘密外洩、使用未核准工具不重試，驗證失敗不走暫時性重試（只走帶錯誤回饋的修正循環，最多 `max_attempts` 次）；平行任務有一個已失敗時不再重試；等待加重跑會超過交件期限 08:55 時也不重試。）
  - 每種設定只跑一次，LLM 本身有變異，不能當嚴格的加速比；8 尚未實測。
  - 實測當時的程式尚未包含 #69 的事件調整（當時事件分析師 10 批、分析師共 16 次呼叫）；之後無重大訊息的股票改由程式輸出，事件批次會減少，數字需重新量測。

## 降低 token 的設定

詳見[降 token 計劃](token_reduction_plan.md)。

### 技術面閘門

預設開啟 `--technical-gate`。`--no-technical-gate` 可改回「全部附完整 `trend_history`」。

- **趨勢一致**的股票，只附 `momentum` 與證據 ID。趨勢一致的定義：
  - 收盤同在 MA20／MA60 之上，且 5／20／60 日報酬皆為正；或
  - 收盤同在 MA20／MA60 之下，且 5／20／60 日報酬皆為負。
- 其他股票與資料不足者，仍附完整歷史。
- 分析師仍對全部股票給 outlook，Validator 與引用檢查不變。
- 10/20 同 Snapshot 的有／無閘門對照，樣本只有一天。

### 分析師模型

`--model`（交易、現金姿態與風險審查）在 Claude CLI 預設為 `opus`，`--analyst-model`（分析師與全市場新聞標註）與 `--research-model`（多頭／空頭）預設為 `sonnet`（`inherit` 沿用 `--model`）。

- 技術／基本面／事件批次使用它。
- 多空、交易與現金姿態維持 `--model`。
- `agent_calls` 會記錄實際使用的模型。

### 其他

- 固定錨點摘要與事件首尾摘錄，只在另存的研究試驗中使用，**沒有**取代每日決策輸入。
- 交易 brief 已把重複的完整帳戶持倉，改成帳戶現金／NAV、持股代號與檔數。本批逐股的 `held_shares` 仍保留。
- 完整 AccountSnapshot 與分析師原始報告，繼續供 Validator 重建。
- 試驗差異與後續門檻，見[降 token 計劃](token_reduction_plan.md)。

## 配置與風控

配置程式產生提案時，就依 Policy 的單股上限（含特殊上限）檢查既有持股與候選：

- 依整張數降低預計超重的部位。
- 用同一個情境引擎，檢查部分成交後的曝險。
- `constraint_flags.RISK_STOCK_WEIGHT` 記錄程式必要的硬規則減碼。交易 Agent 原本的 `hold` 等意圖不會被改寫。
- `RISK_NOT_TRADABLE`：交易狀態包中非 `allowed` 的標的，程式不建立買單、續抱不賣，避免 Guard 的 `TRADABLE` 否決整份決策。
- `RISK_MIN_POSITIONS`：配置後持股低於 `min_positions` 時，程式依 ATR14 由低到高留住標為 `exit` 的持股（`forced_exit` 不留），避免 `POSITION_COUNT` 否決整份決策而當日無單可交。
- `RISK_ACTIVE_SHARE`：配置後投資組合前 15 大與任一必備主動型 ETF 前 15 大的 Active Share 低於 15% 時，程式取消與最低那檔 ETF 重疊最多的 buy／add 候選後重新配置，最多 3 次，不賣既有持股，避免 `ACTIVE_SHARE_ALL` 否決整份決策。
- `RISK_CASH_CEILING`（`cash_ceiling_fix` 啟用時）：配置後現金比例達競賽上限，程式依 ATR14 由低到高留住標為 `exit` 的持股續抱直到低於上限，避免 `CASH_WEIGHT` 否決整份決策。
- `RISK_CASH_FIT`（`cash_fit_order` 啟用時）：現金不足使未持有新股只買得到低於目標一定比例的零頭，程式整筆取消、不留塵埃持股。
- `RISK_SECTOR_WEIGHT`（`sector_fit` 啟用時只減買單增量、不賣持股，Guard 容忍既有已超限且未買進的產業）、`TURNOVER_LIMIT`／`RISK_TURNOVER`（`turnover_fit`：依信心低→高、ATR 高→低縮減買單，賣單本身超過時留住 exit）、`RISK_REENTRY_COOLDOWN`（出場後 N 日不再買）、`RISK_DRAWDOWN`（防守姿態減碼到姿態緩衝）、`RISK_FORCED_SWAP_UNCONFIRMED`（論點被推翻但沒有本次空頭 claim，不換）：見「2026-11-03 晚間的風控閘門」。
- 風險 Agent 不手寫股數或權重。
- 交易受限、整張／現金／換手等限制無法同時滿足時，CompetitionGuard 仍然拒絕。

## Decision run 的 `team_inputs`

| 版本 | 內容 |
| --- | --- |
| 2.6（目前） | 2.5 再加程式計算的基本面指標 `fundamental_metrics`（基本面分析師看到的那份；沒有財報時為 `null`）。bundle 有 `fundamental_period_policy` 時必為 2.6，Finalizer 以 `validate_fundamental_metrics` 用同一份決策輸入整份重算比對（逐檔期別、數值、分組雜湊），改寫、缺少或降回 2.5 都拒絕；沒有該閘門的舊 bundle／舊 run 不重算、維持 2.5 以前 |
| 2.5（舊封存） | 2.4 再加交易 Agent 第二輪的入選排序 `selection_ranking` |
| 2.2（舊封存，見上方說明） | 三份分析報告、現金姿態，以及交易 Agent 看到的前次決策背景 `prior_decision_context`（available 或 unavailable 都要存） |
| 2.1（舊封存） | 三份分析報告與現金姿態 |
| 2.0（舊封存） | 依原始分析報告與事件研究輸入重建 |

2.1／2.2 的事件報告可為 2.2 或 2.3，由決策資料包決定：資料包含 `company_news_input` 時必須是 2.3，否則是 2.2；沒有新增 `team_inputs` 版本。

### 證據種類與引用內容（`evidence_policy = typed_evidence_v1`）

2026-11-03 起 `DecisionInputBuilder` 建新 bundle 時預設寫入 `evidence_policy: "typed_evidence_v1"`（版本閘門，`DecisionInputValidator` 只接受已知值；傳 `None` 不寫欄位）。沒有此欄位的舊 bundle／舊 run 維持「引用存在且屬該股」的原驗證，逐位元重建不變。閘門開啟時：

- **證據種類由程式判定**（`DecisionContext.evidence_kinds`，Agent 不填）：`latest_prices` 為 `price`；文件依 `document_type` 為 `financial`（財報、已核對現金流）、`profile`（公司基本資料）、`revenue`（月營收）、`material_event`（重大訊息）；個股新聞與情緒資料包的來源證據為 `news`；交易狀態記錄為 `status`；帳戶快照為 `account`；隔夜市場成分、全市場情緒（新聞 findings 或恐懼貪婪成分）與程式市場狀態編號 `market-regime:pool-breadth` 為 `market`（沒有 symbol，只在閘門下註冊）。
- **個股 claim／finding 不得只靠無 symbol 的證據成立**：至少一個 `evidence_id` 必須屬於該股票；市場層級（`market`）、帳戶與交易狀態證據只能作補充（隔夜市場證據仍一律不得用於個股）。
- **Agent 必須宣告種類**：多空 claim 填 `claim_kind`、分析師 finding 填 `finding_kind`，列舉為 `price`／`financial`／`revenue`／`material_event`／`news`；主要依據（屬該股票的證據）種類必須與宣告相同（`financial` 可引用 `profile`），其餘引用只能是同種類或補充種類，claim 引用的 finding 也必須同種類；依據跨兩種時拆成兩個 claim／finding。輸出 JSON Schema（`agent_schemas.analyst_schema／event_analyst_schema／stance_schema(typed)`）與 Skill 同步加欄位；舊 bundle 的 schema 不變，出現種類欄位即被 `reject_unknown_fields` 拒絕。多空 brief 每檔另附 `evidence_kinds` 對照與頂層 `evidence_policy` 規則。
- **風險 Agent 現金姿態**（`sizing.validate_cash_stance`，Finalizer、`apply_trade_decision` 與 pipeline 同一套）：`evidence_ids` 至少一個市場層級證據（brief 的 `market_level_evidence_ids`：全市場情緒／恐懼貪婪／隔夜／`regime_assessment.evidence_id`），個股行情只能作補充；一致性護欄：`market_sentiment` 為 available 且 `negative` 時 `aggressive` 一律退回（回撤下限另由 `stance_floor` 強制）。brief 不再附交易 Agent 的意圖與 buy／add 信心統計（避免群體思維），這是 brief 變更、不影響重建。
- **驗證回饋措辭中性**（`pipeline_base.VALIDATION_FEEDBACK_PREFIX` 與 trader／stance／analysts 的錯誤訊息）：並列「降級」與「補依據」兩種合法修法，不暗示補採納才是正解；重試時 `agent_calls` 多記 `revision_diff`（逐檔 intent／conviction／strength／outlook／level／breadth 與 claims／findings／採納數的前後值，並標 `more_aggressive`／`more_conservative`／`other` 計數），供事後稽核「因驗證回饋變積極」。

Finalizer 與下游驗證遇到以下情況一律拒絕：

- 缺少必要輸入
- 引用不存在
- 版本不合
- 數值被改寫

1.0 的 Buy／Sell 舊決策鏈不再接受。

### 前次決策背景（記憶的最小版本）

交易 Agent 每天為持股寫 `invalidation_conditions`，但過去沒有人讀；現在由程式把**上一份**已封存決策對目前持股的 `intent`／`conviction`／`invalidation_conditions` 整理成 `prior_decision_context`，只給交易 Agent、只當背景：

- 來源：`artifacts/decisions/` 中同帳戶、狀態 `approved`／`no_trade`、**目標交易時段（`trading_status_bundle.target_session.start`）嚴格早於本次**且 cutoff 更早的 run。排程是 21:00 做隔天時段的備案、07:00 重做同一時段，所以依時段而不是 cutoff 排序，07:00 重做不會拿到幾小時前的備案。最新的前一時段有多個 run 時，優先取帳本 close 狀態實際結算的那個（`applied_decision_run_ids`，由 `run_daily_pipeline.py` 從 `VirtualAccountRepository.applied_decision_run_ids()` 傳入；`source.selection=ledger_applied`），否則取該時段 cutoff 最新者（`latest_cutoff`）；仍相同才拒絕。整個 run 先過 manifest 驗證，再記錄 `run_id`、時段、挑法、decision／intent 內容雜湊與 manifest 雜湊。
- 時間：前次 cutoff 必須早於本次 cutoff（含時區）；snapshot 幾乎一定不同，所以明確標 `cross_snapshot_exception=true`，表示它不是本次 Snapshot 的事實。
- 不可引用：沒有 evidence_id，`citable=false`。交易 Agent 的 `rationale`／`unresolved_questions`／`invalidation_conditions` 出現其 run_id、decision_id 或 `prior_decision` 字樣即被 `TradeDecisionValidator` 拒絕；延續或改變行動仍必須由本次採納的多空 claim 支持（buy／add 需多頭 claim、trim／exit 需空頭 claim 的規則不變）。多頭／空頭研究員看不到它。
- 沒有前次 run（新帳戶、評測重跑、決策庫為空）或本次沒有目標交易時段時輸出 `status=unavailable`（`reason` 說明），不是錯誤。
- 續跑沿用本 run 已存的 `prior_decision_context.json`（仍須通過驗證），避免決策庫變動改變輸入。
- 實作：`src/etf_agent/decision/prior_decision.py`；`team_inputs` 2.2 由 Finalizer 以 `PriorDecisionContextValidator` 重建驗證，2.1／2.0 舊封存照舊。

### 決策與每日 run 的日期資料夾

`artifacts/decisions/<date>/` 與 `artifacts/daily/<date>/` 的 `<date>` 是本次**目標交易日**（`target_session.start` 換算的台北日期），跟 `artifacts/competition_d_plan/` 一致；晚上做隔天備案時，`<date>` 就是隔天。缺少 `target_session`（評測重跑或行事曆缺漏）才退回**執行當天**。資料夾日期只是佈局，不影響 `run_id`、manifest 或雜湊封印；查找既有 run 一律先用 `find_run_dir`／`list_run_dirs` 全域掃描，不受資料夾改名或日期估算影響。`artifacts/reports/` 的 `runs/`、`views/` 與 `dated/` 亦依決策封存的目標交易日歸類；缺少可用目標時段的診斷交付沿用執行日期。`dated/` 報告檔名中的日期時間保留決策執行時間（台北），供稽核辨識。

## 校準旁觀者

`src/etf_agent/calibration/` 只讀已封存的 Decision run 與官方價量，記錄各股的信心等級與多空強度，價格到齊後以官方成交均價算前瞻報酬，並審計決策用的前日收盤與當日成交均價的差距。**不在決策鏈內**：決策、自動化與風控程式都不匯入它，也不回寫任何決策 artifact。完整口徑與限制見[校準旁觀者](../src/etf_agent/calibration/README.md)。

## 策略反思旁觀者

`src/etf_agent/strategy_reflection/` 讀校準報告，每週五晚上讓 Agent 提出策略樣板旋鈕（信心乘數、現金姿態比例、持股廣度檔數、回撤門檻、波動下限、減碼比例）的調整提案，通過程式驗證與影子回放後才改寫 `config/decision_policy.json`。**不在決策鏈內**：每日決策只讀樣板，不匯入它；Agent 不輸出數字以外的任何決定，也不碰硬性規則與模擬假設。每週產生診斷包與反思報告，週六才套用，套用後有試用觀察期與自動還原。

自動套用只放行**降風險方向**（白名單每個旋鈕都定義了方向：降低信心乘數、提高現金緩衝、增加持股檔數、調低回撤門檻、提高減碼比例、提高 `volatility_exponent`、減少換股、提高換股信心門檻、調低停損／回落警示門檻；`volatility_floor` 方向不明），加風險方向的提案只封存標 `needs_human`，需人用 `apply-pending --allow-risk-increase <reflection_id>` 明確套用。影子回放比的是對交易池等權基準的超額（不是絕對 NAV），且候選在兩段不重疊子區間都不得劣於基準或目前策略。回放只固定已封存的 Agent 意圖重算配置，不是對 LLM 判斷的前瞻驗證（`evidence_grade: replay_reference_only`）。有文獻支持的設計——逆波動配置（1/ATR14 的 `conviction_volatility_v2`）、續抱優先（`held_add_priority`）、多空單輪隔離——是版本閘門而非旋鈕，`tests/strategy_reflection/test_design_guards.py` 守住。完整流程、邊界與回滾見[策略反思旁觀者](../src/etf_agent/strategy_reflection/README.md)。

## 事件如何處理

- 重大公司事件保留在事件分析師報告，直接交給股票層級的多頭／空頭研究。
- 決策截止時 FinMind 個股新聞已核准時，`build_input_file` 在資料包加入 `company_news_input`（`company-news-1.0`），事件報告升為 2.3；每則新聞須在 `events` 剛好分類一次，見[事件研究整合說明](integrated_event_analysis.md#事件報告-23已核准個股新聞)。個股新聞只進公司事件通道，不進市場情緒。
- 每日鏈不再額外執行 Fact／Bull／Bear／Adjudicator。
- 獨立事件研究與舊封存驗證仍可使用，見[事件研究整合說明](integrated_event_analysis.md)。
- 第三位分析師把全市場情緒與逐家公司事件分開，見[市場與公司事件範圍](event_market_scope.md)。

## 子 Agent 執行器

用 `--agent-cli claude|opencode|codex` 選擇，多空研究員另用 `--research-agent-cli claude|opencode|codex` 指定（預設與主執行器相同，`codex` 亦然）。輸出都進入同一套 Validator，續跑必須沿用原執行器與原研究員執行器。

| 執行器 | 驗證狀態 |
| --- | --- |
| Codex | 可跑全部子 Agent；`--agent-cli codex` 時多空研究員預設也用 Codex：唯讀沙箱無法強制互相隔離（`deny_reads`），改由研究階段輸出暫存保證（時序隔離，見下），隔離弱於 Claude／OpenCode；曾完成真實 150 檔每日鏈（當時研究員也用 Codex），**暫存隔離版尚未用真實 Codex 端到端跑過** |
| OpenCode | 已通過本機唯讀、JSONL、預檢煙霧測試。**尚未**執行真實 150 檔端到端驗收 |
| Claude | 每日鏈使用中 |

讀檔邊界（三者共通）：

- 拒讀樣式集中在 `agent_runner.SENSITIVE_READ_GLOBS`（`.env`、`.env.*`、`.envrc`、`*.pem`、`*.key`、`*credential*`、`*secret*`、`.netrc`、`*.p12`、`*.pfx`、`id_rsa*`、`*.token`、`.npmrc`、`.pypirc`、`*.jks`，每個樣式另含全小寫／全大寫／首字大寫變體，因 macOS 檔案系統不分大小寫而樣式比對可能分）。Claude 以 `--settings` 的 `permissions.deny` 套用（已用本機 `claude -p` 實測擋下根目錄與子目錄的 `.env`、`.key`），OpenCode 以 read 權限套用。
- Codex 的 `--sandbox read-only` 只禁止寫入，**不能**限制讀檔；因此帶 `deny_reads` 的任務（多頭／空頭互相隔離）在 Codex 上預設拒絕啟動，只在「研究階段輸出暫存」內放行：多頭與空頭的原始輸出只留記憶體，兩方全部完成才一次寫入 run 目錄，任一失敗整批丟棄；Codex 研究任務加 `--ephemeral`，暫存檔放系統暫存區而非 run 目錄。共同輸入 brief（兩方內容相同、不含研究員輸出）仍照常落地。
- 暫存隔離是**時序隔離、不是權限隔離**，殘留弱點：Codex 仍可讀整個專案，包含舊 run 的 `artifacts/daily/*/bull_stance.json`、`artifacts/decisions/*/debate.json`、`artifacts/reports/` 等前幾日的多空論點，以及 `var/`；`.env*` 只靠輸出外洩掃描；`codex exec` 的 `output.json` 在 Codex 結束到 Python 讀取之間有毫秒級視窗；`--ephemeral` 是否連 `~/.codex` 內的狀態資料庫也不寫入未驗證；任何把研究輸出提早落地的程式改動都會無聲破壞隔離，因此由 `tests/automation/test_research_staging.py` 守住。Claude 的拒讀樣式（`**/…`）連舊 run 也擋；OpenCode 的互讀禁止錨定本次 run 目錄名稱，**不擋舊 run**。
- Codex 研究員的續跑為全有或全無：每個研究任務都已有輸出才逐一重驗沿用；完全沒有輸出才重跑；只有一部分、後段產物殘留、沿用的輸出驗證失敗，或非續跑卻重用已有研究輸出的目錄，一律拒絕並要求建立新 run（舊檔不搬移）。研究階段失敗等於整段研究重跑，用量較高。
- 多頭／空頭的互讀禁止清單（`team_stages._peer_deny_reads`）除了對方輸出與 brief，也含 `debate*`、`intent*`、`trade_decision*`、`trader_b*`、`trader_report*`、`brief_trader_*`、`team_inputs*`、`selection_ranking*`，防止續跑時讀到後段產物。
- 所有 runner 的輸出若含專案 `.env*`（含 `.env.local` 等）／`.envrc` 中長度 8 以上的值，一律拒絕，錯誤訊息不帶出該值。`*.pem`、`*.key` 等非 KEY=VALUE 檔案不在逐行掃描範圍（已知差距）。

OpenCode 的限制：

- 使用 `run --pure --format json` 與單次隔離的唯讀 Agent。
- 只准讀檔（`.env` 與外部目錄禁止），不准執行命令或修改檔案。
- OpenCode CLI 沒有本流程可直接用的 `--output-schema`。提示會帶 JSON Schema，但仍要靠確定性 Validator 與有限重試，拒絕不合格的結果。

全市場情緒優先使用全市場新聞（`automation/market_news_stage.py`：截止前 RSS 抓取、子 Agent 逐則標註、程式重建，失敗時降級）；新聞不可用時改用[台股恐懼貪婪指標](market_fear_greed.md)（程式計算，cutoff 前抓取）；兩者都不可用時維持 `unavailable`。不得用公司新聞或交易背景補值。

[隔夜市場背景](overnight_market.md)是另一條只給風險 Agent 現金姿態與最終報告的次級背景（美股指數、台股代理 EWT／TSM、日韓指數、VIX 與台指期夜盤，程式計算，不併入 `market_sentiment`，不給個股層級 Agent）；來源已由使用者核准（未核准為 `unavailable`），夜盤只在台北 05:00 後的決策才有。
