# 每日決策鏈契約與版本閘門細節

這份文件收錄每日決策鏈各角色的輸入輸出契約、版本閘門的完整條件與目前設定值，以及子 Agent 執行器與策略反思旁觀者的邊界細節。內容原本寫在根目錄 `AGENTS.md`，2026-11-08 搬到這裡；`AGENTS.md` 只留原則、邊界與連結，細節一律以本文為準。本文是契約對照用，正文直接使用程式名稱。

- 流程的白話說明見[每日決策架構](architecture.md)，「為什麼這樣做」見[決策流程細節](decision-details.md)。
- 欄位與驗證以 `src/etf_agent/decision/` 的 schema 與 Validator 為準；策略反思流程見 [`src/etf_agent/strategy_reflection/README.md`](../src/etf_agent/strategy_reflection/README.md)。

## 總則

- **確定性歸程式，不確定性歸 LLM**：子 Agent 只輸出市場狀態解讀、多空論點、買賣意圖與信心分級、現金姿態與持股廣度（`cash_ratio_range` 啟用時另含姿態內現金子等級 `cash_grade`，仍是列舉）或結構化風險修正；技術指標、權重、股數、費稅、現金、情境與競賽限制由確定性 Python 程式計算，Agent 不輸出任何數字。
- **版本閘門**：`config/decision_policy.json` 的 `position_sizing`／`position_review`／`drawdown_control.history_ref`、`config/decision_rules.json` 的規則版本，以及 bundle 的 `evidence_policy`、`fundamental_period_policy`，都是版本閘門：只由人設定、策略反思不得改；沒有該欄位的舊 Policy／舊 bundle／舊 run 逐位元維持原重建行為。以下各節不再逐條重複這句話。
- 決策 artifacts 使用嚴格欄位白名單與可重算內容雜湊；修正循環最多三次（`max_revisions`），硬性規則失敗必須拒絕。
- 舊版 Portfolio Decision 主控加 Momentum、Buy、Sell、Trade Adjudicator 與 Portfolio Risk 的 1.0 鏈已於 2026-09-30 移除，Finalizer 與 Validator 遇到 1.0 debate 一律拒絕。

## 版本閘門啟用狀態

啟用狀態與目前數值只寫在一處：[status.md 的「版本閘門啟用狀態」](status.md#版本閘門啟用狀態)（依 `config/decision_policy.json`、`config/decision_rules.json` 與 `config/strategy_reflection.json` 核對）。本文各節只寫閘門「存在時」的行為與條件，不重複狀態；唯一例外是「高信心現金下限」一節開頭註明它目前不在正式設定內，避免被當成現行行為。

## 分析團隊與市場情緒報告

- 分析團隊（technical／fundamental／event 分析師）逐批覆蓋全部交易池，只輸出 `outlook`、事件 `materiality` 與引用證據的文字發現；指標與財務比率由程式計算，情緒無核准來源時確定性 `unavailable`。
- 事件分析師只處理查詢視窗內有重大訊息或已核准個股新聞（FinMind，截止時已核准且通過媒體白名單與視窗檢查）的股票，兩者都沒有者由程式直接輸出 `unknown`（最終報告仍覆蓋全部交易池）；標為 high 的事件由程式提供同 Snapshot 的事實與脈絡，保留重大性、發現與證據，直接交給股票層級多空研究員；每日鏈不再另跑 Fact／Bull／Bear／Adjudicator。
- 第三位分析師（事件與市場情緒）的對外報告版本：2.2，資料包含已核准個股新聞時為 2.3；為「一次整體台股情緒＋逐家公司事件」。舊 2.1 及四份 2.0 封存沿用原輸入驗證。
- 全市場新聞另讀同 Snapshot／cutoff 的 `market_news_input` 成對資料包，1.1 可包含核對過的歷史原頁；今日補抓的可得時間不得回填到過去。
- 技術分析師啟用 `technical_gate` 時，分批 brief 只記錄該批股票的 `momentum_only_symbols` 與 `full_history_symbols`。
- 每次 agent 呼叫的 `duration_seconds` 寫入執行紀錄。

## 多空研究員與證據種類（`typed_evidence_v1`）

- 多頭／空頭研究員讀同一份共同輸入（`shared_input_sha256`）、互相隔離（`peer_packet_ids` 為空），都必須對全部交易池逐檔給 `strength` 與有證據的 claims；`none` 時不得有 claims。比賽只能做多，空頭論點代表不宜買進或應減碼／出場。
- bundle 帶 `evidence_policy: typed_evidence_v1`（新 bundle 預設）時：
  - claim 填 `claim_kind`、分析師 finding 填 `finding_kind`（price／financial／revenue／material_event／news）；程式依來源判定證據種類並核對對映。
  - 至少一個證據必須屬於該股票；市場層級與帳戶證據只能作補充，市場層級證據不能單獨撐起個股主張。
  - 風險 Agent 現金姿態至少引用一個市場層級證據、全市場情緒 negative 時不得 `aggressive`；風險 Agent 的 brief 不附交易 Agent 的意圖與信心統計。
  - 驗證失敗的重試回饋用中性措辭，並列「降級」與「補依據」兩種修法；重試差異記在 `agent_calls.revision_diff`。
  - 沒有此欄位的舊 bundle／舊 run 維持原驗證。
- 基本面期別逐檔（`fundamental_period_policy`）：`team_inputs` 2.6 = 2.5 加程式計算的 `fundamental_metrics`，Finalizer 以 `validate_fundamental_metrics` 整份重算比對。

## 交易 Agent

- 交易 Agent 是多頭／空頭研究的整合者：先說明兩方論點的取捨，再逐檔給 `intent` 與 buy／add 的 `conviction`（`high`／`medium`／`low`），必須把多空每個 claim 剛好採納或否決一次，不得新增事實或 claim；權重、張數與現金由程式依等級與風險 Agent 的現金姿態計算。
- 交易 Agent 的重估條件須寫之後看得到的事並含時間條件；不預設短線或長線，沒有固定持有天數，也沒有持有幾天必須賣的規則。

### `team_inputs` 版本

Decision run 必須另存 `team_inputs`；Finalizer 與所有下游重建驗證時都要傳入，缺少即拒絕。

| 版本 | 內容 |
| --- | --- |
| 2.7 | 2.6 再加程式計算的曝險檢視 `exposure_review`；只在 `position_sizing.exposure_rotation` 啟用時存在，兩者缺一即拒絕 |
| 2.6 | 2.5 再加程式計算的基本面指標 `fundamental_metrics`；只在 bundle 有 `fundamental_period_policy` 時，Finalizer 以 `validate_fundamental_metrics` 整份重算比對 |
| 2.5 | 2.4 再加交易 Agent 第二輪的入選排序 `selection_ranking` |
| 2.4 | 2.3 再加程式算好的持股檢視 `position_review` |
| 2.3 | 三份分析報告、現金姿態、前次決策背景 `prior_decision_context` 與程式算好的名額脈絡 `portfolio_slots` |
| 2.2 | 同 2.3 但無名額脈絡 |
| 2.1（舊） | 無前次決策背景 |
| 2.0（舊） | 三／四份分析報告及事件研究，保留原始重建 |

`portfolio_slots` 由 `decision/rotation.py` 以帳戶、policy 與同一份 debate 計算，只讀 `apply_trade_decision` 不會改動的欄位，Finalizer 以最終 policy 重算整份比對；`citable=false`、沒有 evidence_id，交易 Agent 不得寫進文字欄位。

### 持股檢視（停損／停利）

- 由 `decision/position_review.py` 以帳戶成本、同 Snapshot 行情與 policy 的 `position_review` 門檻計算帳面報酬、近 20 日高點回落與警示（`review:<代號>:<旗標>`），Finalizer 重算整份比對；警示有 evidence_id（帳戶與該檔行情）、可引用。
- 警示不是賣出指令，由交易 Agent 判斷：必須和 claim 一樣在 `adopted_claim_ids` 剛好處理一次、不可否決；`trim`／`exit` 可採納警示取代空頭 claim；`loss_alert` 的持股要 hold／add／no_trade 須同時採納多頭 claim。
- 風險 Agent 不處理停損停利。
- 門檻（`loss_alert`、`retrace_alert_20d`、`gain_alert`）為 `null` ＝ 不設死數字：停損停利由交易 Agent 依檢視事實（報酬、近 5／20 日高點回落、選用的持股事實編號 `review:<代號>:position`）自行判斷，減碼／出場可引用該編號當依據；人另設門檻時才出現必須處理的警示。策略反思旁觀者只能在試用觀察期保護下小幅調整門檻（`version` 只由人設定，門檻為 `null` 時不可調）。
- `position_review.context: "v1"`（版本閘門）：每檔多附相對交易池的 5／20 日報酬（`relative_to_pool_*`，交易池為同 Snapshot 全部有足夠行情的股票等權平均）與最新月營收／財報的可得時間，供交易 Agent 判斷重估條件觸發是個股走弱還是大盤一起走弱；Finalizer 照樣重算比對。
- 成本在比賽開帳或重建日是當日結算收盤價，不是真實買價。
- policy 沒有 `position_review` 時維持 2.3 行為。

### 入選排序（`selection_ranking: "v1"`）

- 交易 Agent 分批執行、各批看不到其他批候選，所以只給 high／medium／low 信心；另由交易 Agent 在各批合併後做第二輪（Skill `trader` 的 `references/ranking.md`，不另設 Agent）一次看全部 buy／add 候選，**只在同信心等級內**排先後並附理由，不改信心、不新增事實、不輸出數字（先後即陣列順序）。
- 程式轉成 `rank_by_symbol`，配置排序鍵改為（信心、名次、代號）取代支持數與 `selection_tiebreak`；各等級都只有一檔時由程式直接封存、不呼叫 Agent。
- `SelectionRankingValidator` 要求排序剛好涵蓋每個等級的 buy／add 股票，Finalizer 重建並比對 policy 綁定的名次、id 與雜湊；`selection_ranking` 與 `rank_by_symbol` 屬版本閘門。沒有 `selection_ranking` 的 policy 維持原排序（2.4 以前）。

### 前次決策背景（`prior_decision_context`）

- 只給交易 Agent、只當背景；多頭／空頭研究員不讀它。
- 來源必須是決策庫中同帳戶、`approved`／`no_trade`、通過 manifest 驗證、**目標交易時段嚴格早於本次**的 run（同一時段的 21:00 備案不算前次；同一前次時段多個 run 時優先帳本結算記錄的，否則取 cutoff 最新者），記錄 run_id、時段、挑法與內容雜湊，snapshot 不同時標 `cross_snapshot_exception`。
- `citable=false`、沒有 evidence_id，交易 Agent 文字欄位出現其識別碼即拒絕，行動仍須由本次 claim 支持；沒有前次 run 時輸出 `unavailable`，不是錯誤。
- 例外：`cash_ratio_range` 啟用時，同一個前次 run 的最終現金比例（`proposal.effective_policy.cash_buffer_rate`）由程式綁進 Policy 的 `cash_ratio_range.anchor`，只給風險 Agent 當單日上限的背景，不經由 `prior_decision_context`（見「風險 Agent」的前次識別碼規則）。

### 論點檢視背景與策略教訓

- 每週論點檢視的結論檔 `var/strategy_reflection/thesis_verdicts.json` 由 `decision/thesis_verdicts.py` 唯讀載入，只給交易 Agent 當持股背景 `thesis_review_background`：不是證據、不可引用、沒有識別碼，建立時間須早於決策截止時間且不超過 8 天，不影響 policy 與硬性規則。論點結論唯一影響配置之處是強制換股的 `broken_symbols`（見「強制換股」）。
- 經驗教訓 `config/strategy_lessons.json` 由 `decision/strategy_lessons.py` 唯讀載入（驗證不過視同沒有），交易 Agent 在 brief 的 `strategy_lessons` 讀到：只是背景、不是證據、不可引用、不覆蓋硬性規則。

## 風險 Agent

- 風險 Agent 在配置前對整體給 `aggressive`／`neutral`／`defensive` 現金姿態並附證據，同一次輸出另給 `concentrated`／`balanced`／`diversified` 持股廣度；`cash_ratio_range` 啟用時另給 `cash_grade`（lower／middle／upper，列舉，不輸出數字）。不得手寫權重或覆寫 CompetitionGuard。
- 現金比例由 Policy 依姿態對應（`cash_buffer_by_stance`；`cash_ratio_range` 啟用時改為姿態加子等級對應 `grid`，見「姿態內現金子等級」），競賽現金上限仍由 Guard 強制。
- 檔數由 Policy 的 `positions_by_breadth` 對應（夾在 `min_positions`～`max_positions`）；沒有 `positions_by_breadth` 的舊 policy 照舊填滿 `max_positions`。
- **前次識別碼規則**（`cash_ratio_range.anchor` 的背景）：`anchor` 同樣 `citable=false`、沒有 evidence_id，簡報不放 run_id；風險理由文字出現前次識別碼（run_id、decision_id、proposal_id 與兩者的內容雜湊，含 decision_id／proposal_id 不帶前綴的完整 20 位摘要）即拒絕。比對有邊界：前後接 ASCII 英數或底線的較長 ID 不算，也不封鎖 run_id 片段、日期或短雜湊；decision_id 取自同一份前次決策背景，pipeline 與 Finalizer 共用同一個檢查。
- **`increase_cash_buffer` 的邊際**（不論有無 `cash_ratio_range`）：配置後風險審查的修正動作 `increase_cash_buffer`，其值不得高於 `cash_weight_ceiling` 減 3 個百分點邊際（`decision/risk.py` 的 `CASH_BUFFER_CEILING_MARGIN`，比賽上限 25% 時為 22%），因整張取整會讓現金高於緩衝；否則該審查無效、退回重答。`cash_ratio_range` 開啟時另不得高於今日可選的最高格點。
- 風險 Agent 不處理停損停利。

### 回撤下限（`drawdown_control`、`history_ref`）

- 回撤下限由 `decision/drawdown.py` 從帳本 NAV 歷史計算並綁進 Policy 的 `drawdown_control`；歷史列有 `run_id` 時另存 `history_ref`（head_run_id、length、sha256）。每日 pipeline 在有帳本歷史時重算峰值並比對，竄改峰值會被拒絕。`history_ref` 屬版本閘門。
- 事後重建（`DecisionFinalizer`、`DecisionResultValidator`，經 `nav_history_provider` 參數注入；CLI／D-Plan／交付／虛擬帳戶結算接 `ledger_nav_history_provider`）在 `history_ref` 存在時以 `head_run_id` 讀帳本歷史獨立重算；帳本讀不到、head 不存在或歷史不符、或呼叫端未提供帳本來源一律拒絕。沒有 `history_ref` 的舊 Policy／舊 run 不讀帳本、照原樣重建。

## 程式配置與風控閘門

以下全部由程式執行，Agent 不參與；程式不改寫交易 Agent 的原意圖，只在旗標欄位留下原因，仍無法滿足硬規則時由 Guard 拒絕。

### 入選與權重

- 程式依信心 high→medium→low、支持數，再依 `position_sizing.selection_tiebreak`（`atr_low` 低波動優先／`symbol` 中性／`momentum_high` 近期強勢優先；省略即 `atr_low`）挑到廣度對應的檔數；`selection_ranking` 啟用時排序鍵改為（信心、名次、代號），見「入選排序」。
- 權重由 `conviction_volatility_v1`（固定 1／ATR14）或 `conviction_volatility_v2`（再依 `volatility_exponent` 0～1 控制低波動偏好強度，0 為完全不看波動度）依 Policy 的等級乘數確定性計算。
- 高信心份額下限 `position_sizing.limit_scaled_conviction`（`{version: v1, alpha}`，alpha 必填；須 `conviction_volatility_v2` 與 `selection_ranking`）：high 的份額至少為 max(原式, min(alpha × 單股上限, 封頂))；必要份額總和超過預算時依入選名次填到用完；`volatility_exponent` 為 0 時中／低信心不得高於上一等級的最小份額，分不完的預算留作現金；不新增信心等級、不改 Agent 輸出；省略即原式。

### 滿檔換股（`rotation`）

- 持股滿檔時未持有的新股只能用本次出場釋出的名額（`position_sizing.rotation`：`max_swaps_per_run` 上限、`min_new_conviction` 最低信心；須與 `held_add_priority` 同時存在）；名額配對由程式全域計算，交易 Agent 只收到 `portfolio_slots` 當背景、不指定誰換誰。
- 啟用 rotation 時廣度收窄只停止淨增加，不凍結既有持股的加碼。省略 `rotation` 的舊 Decision run 維持原重建行為。

### 強制換股（`rotation.forced_swap`）

- 持股滿檔又沒人出場時，信心達 `min_new_conviction` 的未持有買進候選，由程式換掉「交易 Agent 明確標為 hold、且每週論點檢視判定 `broken`」的持股（依代號），每次最多 `max_swaps_per_run` 檔。
- 名單由每日 pipeline 以 `bind_forced_swap` 綁進 Policy 的 `forced_swap.broken_symbols`＋`verdicts_ref`，重建只讀 Policy、不讀結論檔，沒有名單就不換；這是論點結論唯一影響配置之處，其餘仍只是背景。
- 換出的賣單意圖由程式標為 `exit`、留下 `RISK_FORCED_SWAP` 旗標，不改寫交易 Agent 的原意圖，也不動標為 add／trim／exit 的持股、不可交易標的與被留住的出場。
- `forced_swap.confirmation`：被換出的持股須有交易 Agent 本次採納的該股空頭 claim，否則不換（`RISK_FORCED_SWAP_UNCONFIRMED`）；換出數受 `min_positions` 與現金限制。
- 省略 `forced_swap` 的舊 Policy／舊 run 維持「只有交易 Agent 標 exit 才釋出名額」的原重建行為。

### 現金緩衝（`cash_buffer_floor: "v1"`）

現金已低於姿態緩衝時，緩衝只擋買單動用原有現金，不擋用本次賣出收入買的替補（緩衝下限取 `min(緩衝, 開始現金)`），避免出場釋出的名額因現金湊不到緩衝而流失。它本身不會賣股降曝險（主動減碼見 `drawdown_derisk`）。省略此欄位的舊 Policy／舊 run 維持緩衝一律生效的原重建行為。

### 高信心現金下限（`high_conviction_cash_floor`）

目前正式設定沒有這個欄位（見 [status.md](status.md#版本閘門啟用狀態) 的「未開啟」）；以下是人開啟時的行為，不是現行行為。

- 欄位 `{version: v1, floor_ratio}`；須 `cash_buffer_floor`、`cash_fit_order`，有 `drawdown_derisk` 時須 v3（或 v4）；不得與 `cash_ratio_range` 同時開啟。
- high 買單可動用原有現金到 min(NAV × 緩衝 × floor_ratio, 起始現金)，medium／low 不變，動用時記 `RISK_CASH_SOFT_FLOOR`。
- `drawdown_derisk` 的觸發與目標同改為這條線（帶狀防守），當日觸發減碼時不套用；份額預算不變。
- 風險 Agent 簡報在閘門存在時附 `high_conviction_cash_floor_by_stance`。
- 省略此欄位的舊 Policy／舊 run 逐位元不變。

### 姿態內現金子等級（`cash_ratio_range`）

- 欄位 `{version: v1, grid, max_daily_raise}`；不得與 `high_conviction_cash_floor` 同時開啟，有 `drawdown_derisk` 時須 v3（或 v4）。
- 風險 Agent 除了姿態另給 `cash_grade`（lower／middle／upper，列舉，不輸出數字），`cash_buffer_rate` = `grid[姿態][子等級]`（建議格點 aggressive 3／5／8%、neutral 8／10／13%、defensive 15／18／21%；目前值見 [status.md](status.md#版本閘門啟用狀態)）。
- 格點驗證：兩位小數、姿態內嚴格遞增且相鄰差距不大於 `max_daily_raise`、姿態間不重疊、每點不高於 22%，否則拒絕。
- 單日只限制現金拉高：今日可選格點 ≤ max(該姿態 lower, 前次比例 + `max_daily_raise`)（建議 0.03），往下不限；切到更防守的姿態當天只到 lower，回撤強制防守也不放寬。
- 前次比例 `anchor` 由 pipeline 綁定：前次 run 的最終緩衝（`proposal.effective_policy.cash_buffer_rate`）；沒有前次 run 時為本次起始現金比例 `account_cash`。Finalizer／`DecisionResultValidator` 以決策庫重讀前次 run 重算比對，與 `prior_decision_context` 須指向同一個 run；`prior_decision` 而呼叫端沒給決策庫一律拒絕（保存前驗證、D-Plan、交付、虛擬帳戶結算都傳決策庫）。`cli/portfolio_decision.py` 的 finalize／validate-decision 在閘門開啟時預設讀 `artifacts/decisions`，可用 `--decision-repository` 指定；沒有閘門的舊 run 不讀。
- 超出姿態或今日上限一律拒絕，走帶錯誤回饋的修正循環，用盡即停止，不夾到邊界；修正循環的 `increase_cash_buffer` 也不得高於今日可選的最高格點（22% 邊際見「風險 Agent」）。
- 閘門開啟時 `cash_buffer_by_stance` 旋鈕不影響配置，相關反思提案一律 `needs_human`。
- 省略此欄位的舊 Policy／舊 run 逐位元不變。

### 現金姿態的資料新舊（`cash_stance_recency`）

- 欄位 `{version: v1}`；只能用於 `evidence_policy: typed_evidence_v1`，可與 `cash_ratio_range` 獨立開關；目前尚未開啟（見 [status.md](status.md#版本閘門啟用狀態)）。
- 簡報第一個鍵 `evidence_recency` 由程式依 bundle 重算：市場層級證據的內容時間（新聞 `published_at`、隔夜時段 `session_end`、恐懼貪婪資料日）、距截止小時數與三個分組，界線是台股最近交易日 13:30；內容時間晚於截止即拒絕，找不到內容時間的證據記入 `data_gaps`；不含新聞文字。
- 風險 Agent 另填列舉 `recency_reconciliation`；最新一組（`after_latest_close`）非空時不得填 `newest_insufficient` 且須引用其中至少一個證據，為空時只能填 `newest_insufficient`。Skill 規定：最新一組的事件與新聞預設為判斷「情況有沒有改變」的主要依據，利多利空同樣適用，不採用須說明；隔夜行情仍只是背景。這條是給 Agent 的取捨規則，程式只驗引用與列舉，不要求列舉與姿態對應，不放寬任何現金格點、上限、Guard 或回撤下限。
- `team_inputs` 不升版：閘門決定 `cash_stance` 允許的欄位（與 `cash_grade` 同做法），Finalizer 與 `apply_trade_decision` 以 bundle 重算時間表再驗；回放略過此閘門（notes 標 `cash_stance_recency: ignored_in_replay`）。
- 省略此欄位的舊 Policy／舊 run 逐位元不變。

### 現金姿態的轉向事實（`cash_stance_turn_facts`）

- 欄位 `{version: v1}`；只能用於 `evidence_policy: typed_evidence_v1`，與 `cash_stance_recency`、`cash_ratio_range` 互相獨立；目前尚未開啟（見 [status.md](status.md#版本閘門啟用狀態)）。這是**紀錄工具，不是規則**。
- 簡報在 `regime_assessment` 旁多一個鍵 `turn_facts`，由 `decision/turn_facts.py` 只依 `price_series` 重算：最近兩日池等權日報酬、連續上漲日數、站回 MA5／MA20 的股票比例、MA20／MA60 之上比例（呼叫 `MomentumEngine._regime`，與 regime 同一公式）、MA20 之上比例的 5 日變化、20 日新低比例、regime 連續 bear 天數（需 61 筆以上，被歷史長度截斷時標 `bear_streak_truncated`）。全部為字串或整數，沒有文字判斷；資料不足記入 `data_gaps`；任何 bar 晚於 `snapshot.latest_trade_date` 即拒絕。
- 風險 Agent 另填列舉 `turn_assessment`（`no_turn_evidence`／`turn_unconfirmed`／`turn_confirmed`）；驗證只檢查必填與值域，不驗與姿態的對應、不驗文字品質，不放寬或收緊任何現金格點、`stance_floor` 或 Guard，也不改 `_regime`。
- `team_inputs` 不升版；Finalizer 與 `apply_trade_decision` 以 bundle 重算再驗；回放略過此閘門（notes 標 `cash_stance_turn_facts: ignored_in_replay`）；策略反思不得改。省略此欄位的舊 Policy／舊 run 逐位元不變。

### 曝險換股與 hold 信心（`position_sizing.exposure_rotation` v1）

- 欄位：`version`、`beta_window_days`、`beta_floor_by_stance`（`defensive` 只能 null）、`release_margin`、`max_swaps_per_run`、`max_rotation_fraction`、`min_holding_days`、`min_new_conviction`（不得低於 `rotation` 的）、`hold_conviction: v1`、選配 `conviction_gap_swap: v1`，另有流程綁入的 `held_since_by_symbol`（不得預填）。前提：`rotation`、`held_add_priority`、`selection_ranking`、`reentry_cooldown`、`turnover_fit`、`cash_buffer_floor` 與頂層 `position_review`。人設定、策略反思不得改；目前未開啟（見 [status.md](status.md#版本閘門啟用狀態)）。
- hold 信心：閘門開啟時交易 Agent 對 `hold` 也必填 `conviction`，程式綁入 `hold_conviction_by_symbol`（不得與 `conviction_by_symbol` 重複）；沒開時 hold 帶信心被拒。
- `exposure_review`（`team_inputs` 2.7）：程式依 `price_series` 對交易池等權基準算每檔 beta、up／down capture 與含現金的組合 beta；任何持股缺資料時組合層級為 null，不補假值。
- 持有天數由帳本持股歷史算（連續持有、台北時區日曆日），Finalizer 以帳本獨立重算，讀不到或與 Policy 不符即拒絕。
- 換股：姿態有非 null 下限且組合 beta 低於「下限 − `release_margin`」（死區，起止同一條線，沒有跨日狀態）時，把「本次 buy 被名額擋下」的高 beta 新股換掉「hold、信心非 high、持有天數足夠、beta 低於組合」的持股；`conviction_gap_swap` 另可在任何姿態以 high 新股換 low 持股。兩者共用 `max_swaps_per_run`、`max_rotation_fraction`，換出者的賣單由程式標 `exit`（旗標 `RISK_EXPOSURE_ROTATION`／`RISK_CONVICTION_GAP_SWAP`，不觸發時留 `…_SKIPPED:<原因>`），換入者拿不到名額就捨棄該對，絕不只賣不買。首個換入候選無可負擔的配對時，依原信心／名次繼續嘗試後續候選，不讓昂貴候選擋住整條配對；失敗嘗試不預扣資金。與 `drawdown_derisk` 互斥（減碼日整個略過）；`forced_swap` 與交易 Agent 的 exit／trim 優先。
- 回放略過此閘門（notes 標 `exposure_rotation: ignored_in_replay`）；省略此欄位的舊 Policy／舊 run 逐位元不變。

### 2026-11-03 起的執行面閘門

全為 `position_sizing`／rules 的版本閘門；白話說明見[決策流程細節](decision-details.md#交割與現金2026-11-03-起)。

| 閘門 | 行為 |
| --- | --- |
| 交割口徑 `reuse_sell_proceeds=true` | 主辦方淨額交割，賣款當日可用（規則版本 `decision-rules-2026-11-03b`）；產業上限 `max_sector_weight=0.6` 只擋增量、不強賣既有持股 |
| `cash_fit_order` | 現金不足時依信心、名次分錢；新股殘量不足（低於 `cash_fit_min_fill_ratio`）即整筆取消，`RISK_CASH_FIT`；持有的 add 只減量 |
| `high_conviction_cash_priority`（2026-11-06，須與 `cash_fit_order` 同時出現） | 可動用現金先全部給 high，high 新股不做零頭取消、買得到幾張買幾張；high（含持有的 add）被縮減且至少買到一張後，後面未持有的中、低信心新股降為 0 並留 `CASH_BUFFER` 旗標，持股加碼不受影響；high 連一張都買不起而整筆取消時不擋後面買單 |
| `held_weight_budget`（2026-11-08） | 已持有的加碼候選現有權重高於信心份額時，以現有權重計入預算、不再加碼、不建買單（`RISK_HELD_WEIGHT`），避免其他候選被超配後又被現金緩衝砍回 |
| `cash_ceiling_fix` | 現金逼近 25% 上限時留住 exit，`RISK_CASH_CEILING` |
| `stock_limit_headroom` | buy／add 的單股上限留 3% 空間 |
| `buy_gap_reserve` | 買單預留跳空緩衝（`buy_gap_reserve_rate`） |
| `insufficient_funds_policy: "void_batch"` | 現金不足整批作廢，與主辦方一致 |
| `turnover_fit`＋`max_turnover_rate=0.3` | 換手超過上限時縮減買單；賣單本身超過時留住 exit |
| `reentry_cooldown` | 出場後 3 日不得再買未持有新股，`RISK_REENTRY_COOLDOWN` |
| `drawdown_derisk` | defensive 時程式按 ATR 減碼至緩衝，附 hysteresis 的 `release_*`；`version` v1／v2／v3 差異見[決策流程細節](decision-details.md#控制回撤)，v4（2026-11-08）在 v3 之上把單次減碼預算限制在「換手上限減去交易 Agent 賣單」的餘額內 |
| `forced_swap.confirmation` | 強制換股須有交易 Agent 採納的該股空頭 claim |
| 主辦方 DataPack `prev_day_orders.outcome=voided` | 帳本結算為無成交（`HOST_VOIDED`） |

### 程式風控不改寫意圖

- 單股硬性權重上限的減碼一律由程式執行（不依 Policy 的 `stock_weight_fit` 版本欄位開關）；因風控減碼產生的賣單，意圖由程式標為 `trim`（仍有持股）或 `exit`（減至零），不改寫交易 Agent 的原意圖，並留下 `RISK_STOCK_WEIGHT` 旗標。
- 交易狀態包標為非 `allowed` 的標的由程式不建立買單、續抱不賣（`RISK_NOT_TRADABLE`）。
- 配置後持股低於 `min_positions` 時，程式依 ATR14 由低到高（同值依代號）留住交易 Agent 標為 `exit` 的持股續抱直到達標（`RISK_MIN_POSITIONS`，`forced_exit` 不留）。兩者都不改寫交易 Agent 的意圖，仍無法滿足硬規則時照舊由 Guard 拒絕。
- 配置後投資組合前 15 大與任一必備主動型 ETF 前 15 大的 Active Share 低於 `minimum_active_share`（15%）時，程式取消與最低那檔 ETF 重疊最多的 buy／add 候選後重新配置，最多 3 次（`RISK_ACTIVE_SHARE`，不賣既有持股）；同樣不改寫交易 Agent 的意圖，仍不通過時由 Guard 拒絕。

### 備援 D-Plan（`competition/fallback.py`）

決策 rejected、匯出被擋或流程失敗且比賽前提（20–30 檔、現金 <25%、單股上限、DataPack matched、未過 08:55）全滿足時，程式產生「持股全部續抱、無委託」的本機候選，標 `fallback=true`、非 LLM 產製，**絕不自動上傳**；前提不滿足不硬產，只通知。排程每次結束發本機通知並寫 `var/logs/daily_status.log`，`scripts/check_dplan_ready.py` 為 08:00 看門狗（launchd 需人安裝，見[日常操作](operations.md)）。

## 子 Agent 執行器與安全邊界

白話版見[每日決策架構的「子 Agent 執行器」](architecture.md#子-agent-執行器)。

- **拒讀清單**：Claude 與 OpenCode 的 read 權限須拒絕 `.env`、`.env.*`、`.envrc`、`.pem`、`.key`、`.netrc`、`.p12`、`.pfx`、`id_rsa*`、`.token`、`.npmrc`、`.pypirc`、`.jks` 與含 `credential`／`secret` 的檔案，並含全小寫／全大寫／首字大寫變體（兩者共用 `agent_runner.SENSITIVE_READ_GLOBS`）。
- **輸出掃描**：所有 runner 的輸出含專案 `.env*`／`.envrc` 的值時一律拒絕；`*.pem`／`*.key` 等非 KEY=VALUE 檔案目前不在逐行掃描範圍，屬已知差距。
- **Codex 的研究階段暫存**：Codex 唯讀沙箱只擋寫、不擋讀，無法強制 `deny_reads`。帶 `deny_reads` 的任務（多頭／空頭研究員互相隔離）在 `CodexAgentRunner` 預設拒絕啟動，只有 `PipelineBase` 在「研究階段輸出暫存」內才以 `stage_isolated` 放行（不宣告 `enforces_deny_reads=True` 的 runner 一律視為需要暫存）。暫存時多空原始輸出只留記憶體、兩方全部完成才一次寫入 run 目錄，任一失敗整批丟棄；續跑為全有或全無（每個研究任務都有輸出才沿用，否則或有後段產物殘留一律拒絕並要求新 run）；Codex 研究任務另加 `--ephemeral` 且暫存檔不放 run 目錄。
- **這是時序隔離、不是權限隔離**：Codex 仍可讀整個專案（含舊 run 與 `artifacts/` 的多空輸出），OpenCode 的互讀禁止也不擋舊 run，隔離弱於 Claude。
- **研究員 CLI 與續跑**：`--agent-cli codex` 時全部子 Agent（含多空研究員）預設都用 Codex 模型（未另指定模型即用 Codex 預設）；多空研究員可用 `--research-agent-cli claude|opencode|codex` 另行指定（預設與主 CLI 相同；研究員為 codex 時由輸出暫存保證隔離，預檢要求 `codex exec` 支援 `--ephemeral`，`pipeline.json` 記錄 `research_isolation`）。續跑須主 CLI 與研究員 CLI 都與原 run 相同（舊 run 沒有 `research_agent_cli` 紀錄者視為與主 CLI 相同）。
- **暫時性錯誤只重試一次**：子 Agent CLI 的逾時、非零結束、輸出不是 JSON 或沒有結構化輸出屬 `TransientAgentError`，`agent_step` 只對它在交件期限內再試一次並把失敗嘗試記入 `agent_calls`；秘密外洩、未核准工具等安全拒絕不得重試；驗證失敗不走這種暫時性重試，只走帶錯誤回饋的修正循環（最多 `max_attempts` 次，用盡即停止）。

## 策略反思旁觀者

流程、旋鈕表與檔案見 [`src/etf_agent/strategy_reflection/README.md`](../src/etf_agent/strategy_reflection/README.md)，人工放行見[日常操作](operations.md#每週的人工事項策略反思放行)。本節只列邊界與目前數值。

### 邊界

- 位於 `src/etf_agent/strategy_reflection/`，不在每日決策鏈，不得被 `decision/`、`automation/` 匯入。讀校準報告提出策略樣板旋鈕提案；Agent 只輸出提案 JSON，是否生效由確定性程式依白名單、變幅上限、樣本門檻、冷卻期、**風險方向**與影子回放決定，通過才原子改寫 `config/decision_policy.json`（可 `rollback`）。
- 硬性規則、競賽限制、費稅、滑價與壓力情境不在可調範圍。版本閘門（`held_add_priority`、`rotation.version`、`selection_ranking`、`rank_by_symbol`、`history_ref`、`forced_swap`、各現金閘門等）與 `method`、`stock_weight_fit` 同屬禁區，只由人設定。
- 可調旋鈕含 `volatility_exponent`（單次 0.10、範圍 0～1）與 `rotation.max_swaps_per_run`（單次 1、範圍 0～10）；完整白名單見 README。`cash_ratio_range` 開啟時 `cash_buffer_by_stance` 的提案一律 `needs_human`。
- **風險方向**：每個旋鈕在白名單（`contracts.KNOBS.risk_reducing`）定義「降風險方向」，只有降風險方向的提案可自動套用（`apply-pending`）；加風險或方向不明（`volatility_floor`）的提案一律只封存、歷史標 `needs_human`，需人用 `cli/strategy_reflection.py apply-pending --allow-risk-increase <reflection_id>`（或 `apply --reflection-id <id> --allow-risk-increase`）明確套用。
- **影子回放**比的是對交易池等權基準的超額而非絕對 NAV：候選須在至少兩段不重疊子區間都不劣於基準、也不劣於目前策略，成交日切不出兩段或基準不可用即不通過。
- **回放不是前瞻證據**：回放、評測重跑與校準對舊日期的重算都只固定已封存的 Agent 意圖重算確定性程式，不構成對 LLM 判斷的前瞻驗證；報告 JSON 標 `evidence_grade: "replay_reference_only"`、首段固定提醒；只有比賽期間的即時紀錄才算證據。
- `config/strategy_reflection.json` 的 `auto_apply` 維持 false，缺檔或缺欄位都視為 false。

### 每週節奏與產物

- 週五 19:30 `auto --no-apply --force`：排程固定加 `--force`，不受「距上一份不到 6 天」略過限制，確保每週都有報告；此時週五的收盤資料還沒進來，且週五沒有 21:00 決策，報告涵蓋到週四。
- 樣本門檻：校準與評測要求每個 horizon 至少 10 個完整決策日（`calibration.MIN_DECISION_DAYS`）；不足時週報首段明寫「決策日不足 10 天，不下結論」並維持 observe_only。
- 產生診斷包與每週反思報告（`var/strategy_reflection/reports/`，樣本不足也會產生），以「買進的理由現在還成立嗎」為首節：逐檔列買進日、至今報酬、期間最大回落、買進後的意圖與警示，並附當初的理由與重估條件文字給人閱讀；該文字不進反思 Agent 簡報。
- **論點檢視**：同一個反思 Agent 每週另做（Skill `strategy-reflection` 的 `references/thesis-review.md`，`thesis_review.py`，不另設 Agent），逐檔對照重估條件與最新決策日的分析師發現／多空 claim，判斷理由仍成立／轉弱／被推翻，只能引用該檔最新的 finding／claim ID，驗證不過的批次標「無法判斷」；結果進週報，另寫成 `var/strategy_reflection/thesis_verdicts.json`（交易 Agent 的讀法見「論點檢視背景與策略教訓」）。
- **經驗教訓**：同一個反思 Agent 每週回頭檢視過去買進的股票，整理最多 3 條（只在校準樣本門檻通過且持股不少於 3 檔時才呼叫，`pending.json` 帶 `sample` 且週六套用前再核對最近一份反思的門檻；Skill `strategy-reflection` 的 `references/lessons.md`，`lessons.py`）：輸入只有數值與列舉（不含理由或新聞文字），每條需引用簡報 evidence_id、不得含個股代號／年份／識別碼；週六與旋鈕一起經 `apply-pending` 寫入 `config/strategy_lessons.json`。
- 週六 10:00 `apply-pending` 才套用提案；週五反思 Agent 失敗時仍封存 observe_only 反思並寫週報（再往外回報失敗）。

### 試用觀察期

- 一次只有一件變更（旋鈕或教訓）在試用。套用後有 5 個新決策日的試用觀察期，明確變差（超過 2 倍標準誤與 0.0005 日超額下限中較大者）自動還原；教訓同樣 5 個交易日、明確變差自動還原，也可 `lessons-rollback` 人工還原。
- 連續 2 次還原（只計最近一次自動關閉之後的結案）自動關閉自動套用（`config/strategy_reflection.json`）。
- 試用不會永久卡住：超過 15 個日曆天湊不齊觀察交易日以 `timeout` 結案（變更保留）、套用紀錄與 live 檔不一致以 `orphaned` 結案、還原失敗以 `revert_failed` 結案，三者都標需人工並告警。
- 被試用還原的旋鈕，簡報會附 `hist.*` 證據、冷卻加倍且不得再往同方向提案；`history.json` 讀改寫有檔案鎖。
- 持股檢視門檻（`position_review.*`）只能在試用觀察期保護下小幅調整，門檻為 `null` 時不可調。
