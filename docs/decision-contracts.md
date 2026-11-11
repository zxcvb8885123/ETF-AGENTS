# 每日決策鏈契約與版本閘門細節

這份文件收錄每日決策鏈各角色的輸入輸出契約、版本閘門的完整條件與目前設定值，以及子 Agent 執行器與策略反思旁觀者的邊界細節。內容原本寫在根目錄 `AGENTS.md`，2026-11-08 搬到這裡；`AGENTS.md` 只留原則、邊界與連結，細節一律以本文為準。本文是契約對照用，正文直接使用程式名稱。

- 流程的白話說明見[每日決策架構](architecture.md)，「為什麼這樣做」見[決策流程細節](decision-details.md)。
- 欄位與驗證以 `src/etf_agent/decision/` 的 schema 與 Validator 為準；策略反思流程見 [`src/etf_agent/strategy_reflection/README.md`](../src/etf_agent/strategy_reflection/README.md)。

## 總則

- **確定性歸程式，不確定性歸 LLM**：子 Agent 只輸出市場狀態解讀、多空論點、買賣意圖與信心分級、現金姿態與持股廣度（`cash_ratio_range` 啟用時另含姿態內現金子等級 `cash_grade`，仍是列舉）或結構化風險修正；技術指標、權重、股數、費稅、現金、情境與競賽限制由確定性 Python 程式計算，Agent 不輸出任何數字（已知例外見下方「風險修正動作的數值界線」）。
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
| 2.9 | 2.8 再加交易 Agent 第二輪的換股提名 `swap_nomination`；只在 `position_sizing.agent_swap` 啟用時存在（雙向檢查，缺一即拒絕，v1 影子與 v2 欄位集合相同），Finalizer 以 `SwapNominationValidator` 整份重建驗證；須與 2.8 的前提同時存在（目前正式） |
| 2.10 | 2.9 再加 `quality_dip_ruling`；只在 `agent_swap.quality_dip` version v3 時存在（雙向檢查，缺一即拒絕）；封存含 `status`（`completed`／`not_required`／`skipped_by_program`／`exhausted`／`unavailable`）、提名與現金姿態雜湊與逐組裁決，Finalizer 以 `QualityDipRulingValidator` 重算前置檢查與整份內容；Policy 的 `ruling_ref`／`rulings` 須與封存一致 |
| 2.8 | 2.7 再加程式計算的反彈參與狀態 `turn_state`；只在 `position_sizing.rebound_participation` 啟用時存在（雙向檢查，缺一即拒絕），Finalizer 整份重算比對 |
| 2.7 | 2.6 再加程式計算的曝險檢視 `exposure_review`；只在 `position_sizing.exposure_rotation` 啟用時存在，兩者缺一即拒絕 |
| 2.6 | 2.5 再加程式計算的基本面指標 `fundamental_metrics`；只在 bundle 有 `fundamental_period_policy` 時，Finalizer 以 `validate_fundamental_metrics` 整份重算比對 |
| 2.5 | 2.4 再加交易 Agent 第二輪的入選排序 `selection_ranking` |
| 2.4 | 2.3 再加程式算好的持股檢視 `position_review` |
| 2.3 | 三份分析報告、現金姿態、前次決策背景 `prior_decision_context` 與程式算好的名額脈絡 `portfolio_slots` |
| 2.2 | 同 2.3 但無名額脈絡 |
| 2.1（舊） | 無前次決策背景 |
| 2.0（舊） | 三／四份分析報告及事件研究，保留原始重建 |

選配鍵 `release_order`（`drawdown_derisk` v5）與 `held_weight_facts`（`held_weight_budget` v2）：皆已開啟；同 `dip_facts` 的做法，不升版本（2.9 已被換股提名占用）、只要求 2.5 以上；與閘門雙向檢查，Finalizer 整份重建，`release_order` 另比對 Policy 綁定的 `release_symbols`／`release_ref`。

選配鍵 `dip_facts`（`position_sizing.quality_dip_facts` v1，已開啟）：不升版本、不屬於上表的線性鏈，只要求 2.6 以上；與閘門雙向檢查（閘門在而鍵缺、或鍵在而閘門缺都拒絕），Finalizer 以 `validate_dip_facts` 整份重算。沒有閘門的舊 run 欄位白名單不變。

選配鍵 `valuation_facts`（`position_sizing.valuation_facts` v1，估值 V2，已實作、尚未開啟）：同 `dip_facts` 的做法，不升版本、不要求特定版本；與閘門雙向檢查，Finalizer 以 `validate_valuation_facts` 用同一個估值資料夾整份重算。沒有閘門的舊 run 欄位白名單不變。

選配鍵 `trader_background`（`position_sizing.trader_background_record` v1，交易背景封存，已實作、尚未開啟）：交易 Agent 簡報的策略教訓（`config/strategy_lessons.json`）與論點檢視（`var/strategy_reflection/thesis_verdicts.json`）原本每次 run 當場讀檔、事後無從還原；開閘門後 pipeline 在交易階段前把兩份來源檔的**完整內容**與 `content_sha256` 存成 run 目錄的 `trader_background.json`（可用與否的判斷與現行讀檔路徑一致，不可用記 `null`），簡報改由這份紀錄產生（形狀不變），續跑時驗證後沿用、不重讀來源檔。同 `valuation_facts` 的做法：不升版本、不要求特定版本，與閘門雙向檢查；Finalizer 以 `validate_trader_background` 驗證欄位白名單、內容雜湊、兩份檔各自的格式與雜湊、建立時間不晚於 cutoff、結論檔未超過有效天數，且 Policy 綁定的 `agent_swap.verdicts_ref` 存在時紀錄的結論雜湊必須一致。**限制**：Finalizer 驗證的是封存內容自洽、時間合法與綁定一致，無法證明來源檔當時沒有別的版本。回放略過此閘門。沒有閘門的舊 run 欄位白名單與簡報不變。

選配鍵 `exposure_facts`（`position_sizing.exposure_view` v1，2026-11-10 已開啟）：同 `dip_facts` 的做法，不升版本、只要求 2.7 以上；與閘門雙向檢查，Finalizer 以 `validate_exposure_facts` 整份重算。沒有閘門的舊 run 欄位白名單不變。

`portfolio_slots` 由 `decision/rotation.py` 以帳戶、policy 與同一份 debate 計算，只讀 `apply_trade_decision` 不會改動的欄位，Finalizer 以最終 policy 重算整份比對；`citable=false`、沒有 evidence_id，交易 Agent 不得寫進文字欄位。

### 持股檢視（停損／停利）

- 由 `decision/position_review.py` 以帳戶成本、同 Snapshot 行情與 policy 的 `position_review` 門檻計算帳面報酬、近 20 日高點回落與警示（`review:<代號>:<旗標>`），Finalizer 重算整份比對；警示有 evidence_id（帳戶與該檔行情）、可引用。
- 警示不是賣出指令，由交易 Agent 判斷：必須和 claim 一樣在 `adopted_claim_ids` 剛好處理一次、不可否決；`trim`／`exit` 可採納警示取代空頭 claim；`loss_alert` 的持股要 hold／add／no_trade 須同時採納多頭 claim。
- 風險 Agent 不處理停損停利。
- 門檻（`loss_alert`、`retrace_alert_20d`、`gain_alert`）為 `null` ＝ 不設死數字：停損停利由交易 Agent 依檢視事實（報酬、近 5／20 日高點回落、選用的持股事實編號 `review:<代號>:position`）自行判斷，減碼／出場可引用該編號當依據；人另設門檻時才出現必須處理的警示。策略反思旁觀者只能在試用觀察期保護下小幅調整門檻（`version` 只由人設定，門檻為 `null` 時不可調）。
- `position_review.context: "v1"`（版本閘門）：每檔多附相對交易池的 5／20 日報酬（`relative_to_pool_*`，交易池為同 Snapshot 全部有足夠行情的股票等權平均）與最新月營收／財報的可得時間，供交易 Agent 判斷重估條件觸發是個股走弱還是大盤一起走弱；Finalizer 照樣重算比對。
- 成本在比賽開帳或重建日是當日結算收盤價，不是真實買價。
- `position_review` v2（版本閘門，2026-11-11 已開啟：`trail_alert` 0.05／0.05、`relative_loss_alert` 0.08／0.04、`stale_alert null`）：新增三個只警示的門檻 `trail_alert{arm_gain,giveback}`（自進場移動停利回吐）、`relative_loss_alert{loss,lag}`（自進場虧損且輸交易池）、`stale_alert{days}`（持有滿日數且仍虧損），全部由人填；每檔多附自進場事實（進場日、持有交易日數、自進場報酬、進場後高點報酬與回落、同期交易池報酬與相對差、`entry_gap`）。基準是進場日收盤（不是成交價、不用 `average_cost`）；持有起點取自 `exposure_rotation.held_since_by_symbol`（Finalizer 以帳本重算），算不出來時事實為 null 並留 `entry_gap`、不出警示。警示仍須剛好處理一次、不可否決，簡報另列 `alerts_to_handle`，驗證回饋列出尚未處理的警示識別碼；`team_inputs` 不升版、沒有新鍵。前提 `exposure_rotation`。
- `position_sizing.stop_exit` v1（版本閘門，已實作、尚未開啟；使用者決定硬停損不開、由交易 Agent 依警示判斷，正式設定沒有此欄位；只由人設定、列入反思禁區）：程式兜底的硬停損。自進場虧損 ≥ `loss`、同期輸交易池 ≥ `lag_to_pool`、持有 ≥ `min_holding_days` 個交易日三者同時成立才強制出場，單日至多 `max_exits_per_run` 檔（依虧損由大到小、同值依代號）。強制出場的目標股數為 0、賣單意圖標 `exit`、旗標 `RISK_STOP_EXIT`，交易 Agent 的意圖不改寫；停損賣款先計入回撤減碼的補現金需求；已被停損賣出的股票不再參與買進，也算換股換出者已出場（停損釋出的名額先照一般排序分配，提名的換入者沒搶到時該對以 `swap_cap` 取消、換出者仍因停損賣出）。不可交易、超過單日上限、被最少檔數或現金上限留住時留 `RISK_STOP_EXIT_CANCELLED`；事實算不出來時不強制並留 `STOP_EXIT_UNAVAILABLE`。停損出場的股票進再進場冷卻名單（讀前次 run 的 `proposal.json` 旗標；讀不到或不符即拒絕）。前提 `position_review` v2、`exposure_rotation`、`reentry_cooldown`；回放略過（`ignored_in_replay`）。
- policy 沒有 `position_review` 時維持 2.3 行為。

### 入選排序（`selection_ranking: "v1"`）

- 交易 Agent 分批執行、各批看不到其他批候選，所以只給 high／medium／low 信心；另由交易 Agent 在各批合併後做第二輪（Skill `trader` 的 `references/ranking.md`，不另設 Agent）一次看全部 buy／add 候選，**只在同信心等級內**排先後並附理由，不改信心、不新增事實、不輸出數字（先後即陣列順序）。
- 程式轉成 `rank_by_symbol`，配置排序鍵改為（信心、名次、代號）取代支持數與 `selection_tiebreak`；各等級都只有一檔時由程式直接封存、不呼叫 Agent。
- `SelectionRankingValidator` 要求排序剛好涵蓋每個等級的 buy／add 股票，Finalizer 重建並比對 policy 綁定的名次、id 與雜湊；`selection_ranking` 與 `rank_by_symbol` 屬版本閘門。沒有 `selection_ranking` 的 policy 維持原排序（2.4 以前）。

### 前次決策背景（`prior_decision_context`）

- 只給交易 Agent、只當背景；多頭／空頭研究員不讀它。
- 來源必須是決策庫中同帳戶、`approved`／`no_trade`、通過 manifest 驗證、**目標交易時段嚴格早於本次**的 run（同一時段的 21:00 備案不算前次；同一前次時段多個 run 時優先帳本結算記錄的，否則取 cutoff 最新者），記錄 run_id、時段、挑法與內容雜湊，snapshot 不同時標 `cross_snapshot_exception`。
- `citable=false`、沒有 evidence_id，行動仍須由本次 claim 支持；沒有前次 run 時輸出 `unavailable`，不是錯誤。程式能擋的只有兩種：文字欄位出現其識別碼字串（前次決策背景簡報本來就不含識別碼，所以實際只擋到字面 `prior_decision` 等字樣），以及 pipeline 即時驗證時（`TradeDecisionValidator(background_texts=…)`，Finalizer 與其他重建呼叫端不傳、舊 run 不受影響）`rationale`／`unresolved_questions`／`invalidation_conditions` 完整照抄簡報附的策略教訓或持股論點檢視摘要（至少 10 個字，前次決策的重估條件不列入，續抱沿用同一句是合法的）。**改寫轉述無法由程式偵測**，靠提示與「行動須由本次 claim 支持」把關。
- 例外：`cash_ratio_range` 啟用時，同一個前次 run 的最終現金比例（`proposal.effective_policy.cash_buffer_rate`）由程式綁進 Policy 的 `cash_ratio_range.anchor`，只給風險 Agent 當單日上限的背景，不經由 `prior_decision_context`（見「風險 Agent」的前次識別碼規則）。

### 優質股被大盤拖累的事實（`position_sizing.quality_dip_facts` v1，第一階段，紀錄用）

設計見 [quality_dip_plan.md](quality_dip_plan.md)；啟用狀態見 status.md（已開啟）。閘門存在時：

- `decision/dip_facts.py` 對全交易池重算，全部只用 cutoff 前可得的資料：日期格是行情交易日聯集；評估視窗＝最後 21 個日期（20 個日報酬），視窗前 beta 用基準日以前 61 個日期（60 個日報酬），兩窗在基準日相接。beta 直接用 `exposure.BetaModel`，只把行情截到基準日，`BetaModel` 與 `exposure_review` 的路徑不變。
- 殘差 `residual_20d` ＝ 個股 20 日報酬 − beta × 交易池等權**日報酬連乘**的 20 日報酬（與 beta 同一個日報酬序列）；`relative_to_pool_20d` 另用各股 20 日報酬的簡單平均（與持股檢視同一公式），兩個池報酬欄位分開。有殘差的股票少於交易池 80%（沿用 `MIN_REGIME_COVERAGE`）時整份 `data_status: unavailable`。加權指數來源未核准或日期對不上只讓 `relative_to_index_20d` 為 null 並記 `data_gaps`。
- `price_pattern` 只看符號與資料品質（殘差為 0 歸 `market_explained`），程式不設殘差或跌幅門檻；`candidate` 為 `market_dragged`／`oversold`（固定 `cause: unknown`）／`none`，有新壞事即 `none`；疑慮與不知道不改變標示。
- 三個清單不混用：`new_adverse_facts`（視窗內新可得的月營收年增由正轉負、淨利由盈轉虧〔前一期可比口徑＝基本面指標的去年同季〕、事件分析師 `event_outlook` 為 negative 且視窗內有 high 事件）；金融業（財報產業別 fh／basi／bd／ins）或 `industry_formula_unsupported` 的月營收轉負改列 `concerns`；`concerns`（營收／淨利年增為負、營業利益率年差為負、營業或自由現金流為負，附期別）；`unknown`（指標 unavailable、單位或幣別衝突、缺月營收、現金流缺或未核對）。「視窗內新可得」＝ `available_at` 的台北日期晚於基準日。
- 分析師階段事件報告尚未完成：技術分析師拿 `market_attribution`／`price_position`、基本面分析師拿不含事件一項的 `fundamental_flags`（`event_check: not_applied`），兩者都不含 `price_pattern`／`candidate`，區塊本身沒有 evidence_id，finding 引用各列附的該股既有證據。完整事實在多空研究完成後才建立與存檔（`dip_facts.json`，研究員拒讀），交易 Agent 以 `dip_background` 讀到（`citable=false`、沒有 evidence_id），文字欄位出現 `dip_background`、`dip_facts` 或事實識別碼即拒絕。多空研究員不拿，`shared_input_sha256` 不變。
- 任何 bar 晚於最近交易日、bar 或文件可得時間缺時區、bar 可得時間晚於 cutoff 一律拒絕；晚於 cutoff 的文件不計入；bundle 須有 `fundamental_period_policy`。
- 不新增 Agent 輸出欄位、不影響名額／權重／股數／現金／排序／Guard；回放不讀它（樣板帶閘門時 policy 照樣綁，配置不受影響）。

### 優質股第二階段（`quality_dip_assessment` v1、`event_direction` v1，紀錄用）

設計與實作紀錄見 [quality_dip_plan.md](quality_dip_plan.md#10-第二階段估值-v1-實作紀錄與第三階段修訂2026-11-10第六版)；啟用狀態見 status.md（兩個閘門皆已開啟，2026-11-10）。兩個閘門都只由人設定、前提 `quality_dip_facts`，沒有欄位的舊 Policy／舊報告／舊 run 逐位元不變：

- `quality_dip_assessment`：交易 Agent 的 buy／add 必填列舉 `dip_assessment`，其餘意圖填 `null`；Validator 只驗必填、值域與 fail-closed（價格事實不可用只能 `undetermined`；缺 `team_inputs.dip_facts` 或非 `typed_evidence_v1` 即拒絕），不驗與意圖、信心或 `candidate` 的對應，也不進 Policy 綁定。Finalizer、回放與每日 pipeline 以 Policy 閘門重建同一個驗證。
- `event_direction`：事件分析師每則事件的 `direction` 列舉。事件報告**不新增 `schema_version`**：`AnalystReportValidator` 對 `direction` 採「有就必須是列舉值」，每日 pipeline 產出與 Finalizer 在閘門開啟時才要求必填。閘門同時切換 `quality_dip_facts` 新壞事的事件面判準（`high_event_negative_direction`；沒有閘門仍是整檔 `event_outlook` 的 `high_event_negative_outlook`），`validate_dip_facts` 依 Policy 重算。

### 估值位置（`position_sizing.valuation_facts` v1，估值 V2，背景、紀錄用）

設計與實作紀錄見 [quality_dip_plan.md](quality_dip_plan.md#105-估值-v2-實作紀錄2026-11-10第七版)；**已實作、尚未開啟**。只由人設定、策略反思禁區，沒有 Policy 層前提。

- 事實（`decision/valuation_facts.py`）：只讀估值 V1 已不可變保存、run 時間不晚於 cutoff 的最新一個 run，先依原始回應整份重建；只用核對相符、`license_status=approved` 且授權範圍允許背景進簡報的紀錄（V1 的 `valuation_v1_collection_and_audit_only` 不允許，所以目前全部 `LICENSE_SCOPE_NOT_APPROVED`）。只算 PER／PBR；上櫃股不可用；三個寫死的視窗 `rows_250`、`rows_750`、`all` 並列，固定視窗資料列不足為 `insufficient_history`。缺資料、run 不合格或資料日晚於 Snapshot 時整份 `unavailable`、不補值、不讓決策失敗。
- 簡報：基本面分析師與交易 Agent 各附 `valuation_background`（`citable=false`、沒有 evidence_id／facts_id／run_id）；交易 Agent 文字欄位出現 `valuation_background`、`valuation_facts`、facts_id 或 run_id 即拒絕。不新增 Agent 輸出欄位、不影響配置。
- `team_inputs.valuation_facts` 選配鍵：與閘門雙向同時存在，Finalizer 以決策庫旁的 `valuation_collection` 資料夾與 `source.run_id` 整份重算；run 事後被改或讀不到資料夾即拒絕。回放略過（notes 標 `valuation_facts: ignored_in_replay`）。
- 估值 V3（可引用、新證據政策版本）未實作；待決事項見 [quality_dip_plan.md 第 10.6 節](quality_dip_plan.md#106-v3-需要人決定的事2026-11-10第七版本次未實作)。

### 組合曝險事實與風險 Agent 的回補意見（`position_sizing.exposure_view` v1，Agent 主導曝險階段 1，紀錄用）

設計見 [agent_led_exposure_plan.md](agent_led_exposure_plan.md)；啟用狀態見 status.md（已開啟（2026-11-10，使用者決定）；純紀錄、不影響配置；回滾＝刪掉 `position_sizing.exposure_view`）。前提 `exposure_rotation`、`cash_stance_turn_facts`、`market_index_facts`（GATE_PREREQUISITES）與 `typed_evidence_v1`（驗證現金姿態時檢查）。閘門存在時：

- `decision/exposure_facts.py` 只依 bundle 與 Policy 中配置前後相同的欄位（`exposure_rotation.beta_window_days`、`sector_by_symbol`）重算，不讀任何 Agent 輸出：組合曝險直接重用 `build_exposure_review`（同口徑）再加持股數與低 beta 檔數；產業權重依 Policy 綁定的 `sector_by_symbol`，金融保險（`industry:17`）另列，缺代碼歸 `unknown`；上漲日＝最近 20 個交易日內最近一個交易池等權報酬（與 `turn_facts` 同一個函式）大於 0 的日子，持股當日貢獻＝Σ（本次決策起始權重 × 當日收盤報酬），任一持股缺行情即 null（不當成 0），沒有上漲日即 `unavailable`；非持股的 beta 用同一個 `BetaModel`。
- 任何 bar 晚於最近交易日、bar 可得時間缺時區或晚於 cutoff 一律拒絕。
- 風險 Agent 簡報附 `exposure_facts`（背景、`citable=false`、沒有 facts_id 與 evidence_id；不含交易 Agent 的意圖與信心統計），輸出多一個必填列舉 `exposure_view`（`keep_defensive`／`start_rebuild`／`rebuild`）：值域外或缺少即拒絕；非 `keep_defensive` 時 `evidence_ids` 須含市場層級證據（專屬檢查）；理由寫出 `exposure_facts` 鍵名或 facts_id 即拒絕；沒有閘門時輸出帶此欄位即拒絕。三個驗證點（風險階段、`apply_trade_decision`、Finalizer）用同一個 `validate_cash_stance`。
- `agent_swap` 也開啟時，換股提名簡報多一個 `exposure_facts` 區塊（轉向、`turn_state`、加權指數、組合曝險、產業權重、上漲日與持股當日報酬、提名候選的 beta），全部是背景；階段 1 本身不改提名驗證規則與既有欄位（`purpose`、曝險事實編號與 `lower_conviction_review` 屬子閘門 `agent_swap.exposure`，見[換股提名](#換股提名agent_swap)）。
- 純紀錄：不影響名額、權重、股數、現金、換股與 Guard；回放略過閘門（notes 標 `exposure_view: ignored_in_replay`）並拿掉封存姿態的 `exposure_view`。

### 論點檢視背景與策略教訓

- 每週論點檢視的結論檔 `var/strategy_reflection/thesis_verdicts.json` 由 `decision/thesis_verdicts.py` 唯讀載入，只給交易 Agent 當持股背景 `thesis_review_background`：不是證據、不可引用、沒有識別碼，建立時間須早於決策截止時間且不超過 8 天，不影響 policy 與硬性規則。論點結論唯一影響配置之處是強制換股的 `broken_symbols`（見「強制換股」）。
- 經驗教訓 `config/strategy_lessons.json` 由 `decision/strategy_lessons.py` 唯讀載入（驗證不過視同沒有），交易 Agent 在 brief 的 `strategy_lessons` 讀到：只是背景、不是證據、不可引用、不覆蓋硬性規則。

## 風險 Agent

- 風險 Agent 在配置前對整體給 `aggressive`／`neutral`／`defensive` 現金姿態並附證據，同一次輸出另給 `concentrated`／`balanced`／`diversified` 持股廣度；`cash_ratio_range` 啟用時另給 `cash_grade`（lower／middle／upper，列舉，不輸出數字）。不得手寫權重或覆寫 CompetitionGuard。
- 現金比例由 Policy 依姿態對應（`cash_buffer_by_stance`；`cash_ratio_range` 啟用時改為姿態加子等級對應 `grid`，見「姿態內現金子等級」），競賽現金上限仍由 Guard 強制。
- 檔數由 Policy 的 `positions_by_breadth` 對應（夾在 `min_positions`～`max_positions`）；沒有 `positions_by_breadth` 的舊 policy 照舊填滿 `max_positions`。
- **前次識別碼規則**（`cash_ratio_range.anchor` 的背景）：`anchor` 同樣 `citable=false`、沒有 evidence_id，簡報不放 run_id；風險理由文字出現前次識別碼（run_id、decision_id、proposal_id 與兩者的內容雜湊，含 decision_id／proposal_id 不帶前綴的完整 20 位摘要）即拒絕。比對有邊界：前後接 ASCII 英數或底線的較長 ID 不算，也不封鎖 run_id 片段、日期或短雜湊；decision_id 取自同一份前次決策背景，pipeline 與 Finalizer 共用同一個檢查。
- **`increase_cash_buffer` 的邊際**（不論有無 `cash_ratio_range`）：配置後風險審查的修正動作 `increase_cash_buffer`，其值不得高於 `cash_weight_ceiling` 減 3 個百分點邊際（`decision/risk.py` 的 `CASH_BUFFER_CEILING_MARGIN`，比賽上限 25% 時為 22%），因整張取整會讓現金高於緩衝；否則該審查無效、退回重答。`cash_ratio_range` 開啟時另不得高於今日可選的最高格點。

- **風險修正動作的數值界線（`position_sizing.revision_bounds` v1，已實作、尚未開啟）**：修正動作的 `value` 是「Agent 不輸出數字」的已知例外：只能往降風險方向、有上下界、由 Validator 檢查。沒有此閘門時 `reduce_max_stock_weight`／`reduce_turnover_limit` 只受 0～1 與「不得高於目前值」約束（無下限，舊 Policy 與舊 run 逐位元不變）。閘門形狀 `{"version": "v1", "min_max_stock_weight": "<小數字串>", "min_max_turnover_rate": "<小數字串>", "step": "<小數字串>"}`，三個數字全部由人填、程式不設預設；兩個下限須大於 0 且不高於 Policy 的 `max_stock_weight`／`max_turnover_rate`，`step` 須大於 0 且不高於 1，缺欄位、版本不是 `v1` 或有多餘欄位即整份 Policy 拒絕。開啟後 `RiskReviewValidator` 另要求兩種 `value` 不得低於對應下限、且為 `step` 的整數倍；`increase_cash_buffer` 的檢查不變；風險審查簡報附下限與步距。只由人設定、策略反思不得改（列入禁區）；不影響配置引擎與 `team_inputs`。`special_weight_limits` 內的股票（目前為 2330）不受 `reduce_max_stock_weight` 影響（配置的單股上限直接取特殊上限）；要降低它的權重請用 `remove_candidate` 或 `increase_cash_buffer`，不改配置。回滾＝刪掉 `position_sizing.revision_bounds`。
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

### 換股提名（`agent_swap`）

- 閘門 `position_sizing.agent_swap`（只由人設定、策略反思禁區；欄位 `version`、`max_swaps_per_run`［1～10］、`min_holding_days`［0～60］，另有 pipeline 綁定的 `thesis_hint_symbols`／`verdicts_ref`，v2 另有 `nominated_pairs`／`nomination_ref`，樣板不得預填）。目前正式設定是 **v2**（2026-11-10 20:10 開啟；v1 影子期 2026-11-10 15:31 開啟，保留供舊 run 重建）。前提：`exposure_rotation` v3、`rebound_participation` v2（`team_inputs` 2.9 是 2.8 的超集）。
- 提名發生在交易 Agent 入選排序之後（5c）：輸出只有 `nominations`（`buy_symbol`、`sell_symbol`、兩邊依據識別碼、文字理由）、`thesis_hint_responses`（`nominated`／`kept`）與 `no_nomination_reason`，不得有數字；換入者須是第一輪 buy、未持有、可交易、信心達 `rotation.min_new_conviction`、不在冷卻名單；換出者須是第一輪 hold、持有天數足夠；換出依據只能是本次已採納的空頭 claim 或持股檢視事實；另做保守現金與換手下界檢查，驗證失敗走帶回饋的修正循環（最多三次）。
- 封存狀態四種：`completed`、`not_needed`（沒有需要或可提名的對，程式封存、不呼叫 Agent）、`exhausted`（驗證用盡）、`unavailable`（暫時性失敗）。**提名失敗只取消該動作**（降級，不是整天失敗），其餘決策照常；安全拒絕與硬規則失敗仍整份拒絕。Finalizer 無法證明 Agent 真的失敗，只核對狀態形狀與配置未使用提名。
- **v1 影子**：提名封存並由 Finalizer 重建，配置完全不讀提名，訂單與旗標與沒有此欄位時相同；出現 `nominated_pairs`／`nomination_ref` 即拒絕；回放略過此閘門。
- **v2（2026-11-10 20:10 已開啟，現行）**：與 `rotation.forced_swap`、`exposure_rotation.conviction_gap_swap` 互斥；pipeline 在 5c 後綁 `nominated_pairs`（有序）與 `nomination_ref`，Finalizer 以 `team_inputs` 的提名重算比對，重建配置只讀 Policy。配置在曝險換股之前依序試算：減碼日整個略過（`AGENT_SWAP_SKIPPED:derisk_day`）、提名 `exhausted`／`unavailable` 不換（`AGENT_SWAP_SKIPPED:nomination_*`）、換入者不換股即有名額者不使用該對（`AGENT_SWAP_UNNEEDED`）、其餘不可行的對只取消該對（`AGENT_SWAP_CANCELLED:<原因>`，換出者不賣、不會只賣不買）；成交的對換出者標 `exit`＋`AGENT_SWAP`、換入者 `AGENT_SWAP_FILL`，不改寫交易 Agent 意圖；`exposure_rotation` 的換股步驟停用（`EXPOSURE_ROTATION_SKIPPED:agent_swap`）；`portfolio_slots` 升 1.1。
- **`agent_swap.derisk_day_view` v1（2026-11-11 00:35 已開啟，版本閘門、只由人設定）**：減碼日（`RISK_DRAWDOWN`）不再由程式整個略過換股提名，各配對照自己的條件處理——`keep_defensive` 取消 exposure 配對、quality_dip 看回撤下限與轉向條件、thesis 配對照常，不再有 `AGENT_SWAP_SKIPPED:derisk_day`（硬規則、對數上限與可行性檢查不變）；前提 `agent_swap` v2 與 `agent_swap.exposure` v2；省略此欄位的舊 Policy／舊 run 逐位元維持原行為。起因：2026-11-11 run 現金只差 NAV 的 0.044% 就被判減碼日，整天不能換股。把它的行為當成現行行為是錯的。
- **`agent_swap.proceeds_to_add` v1（2026-11-11 已開啟，版本閘門、只由人設定）**：換入者因額度被高信心加碼持股占走而沒有買單（無任何縮減旗標），且同一份配置有高信心加碼持股真的買進時，該對不取消、換出者照賣（賣款流向加碼持股），換入者改標 `AGENT_SWAP_PROCEEDS_TO_ADD`；沒有高信心加碼持股買進時仍照舊取消（不會只賣不買）；前提 `agent_swap` v2；省略欄位的舊 Policy／舊 run 逐位元維持原行為。起因：2026-11-11 run 賣 3034／2890 的錢本來就流向 2330 加碼，卻因兩個換入者沒有買單整對取消。
- **`agent_swap.entry_reserve` v1（階段 3，2026-11-11 已開啟，`policy_id strategy-2026-11-11-entry-reserve-1`，`max_entry_weight` 0.06、`max_nominations` 8，回滾樣板 `config/policy_versions/2026-11-11-before-entry-reserve.json`，版本閘門、只由人設定、策略反思禁區）**：形狀 `{"version":"v1","max_entry_weight":"…","max_nominations":N}`（兩個數字都由人填，`max_entry_weight` 不得高於 `max_stock_weight`，`max_nominations` 為 1～20 且不得小於 `agent_swap.max_swaps_per_run`）；前提 `agent_swap` v2 與 `agent_swap.exposure`，`quality_dip` 並存時只能 v3／v4。交易 Agent 在換股提名對每一組多填列舉 `size`（`small`／`medium`／`large`，不輸出數字，缺 `size` 一律驗證失敗），Policy 的 `nominated_pairs` 每組綁同一個 `size`，Finalizer 由封存的提名重算比對；`team_inputs` 不升線性版本、沒有新鍵。配置：份量格對權重上限（`large`＝人填、`medium` 2/3、`small` 1/3，沿用 quality_dip v3 常數，`decision/entry_reserve.py`）；換入買進股數取份量格權重 × NAV、該組換出者賣款淨額加依清單先後分到的格點以上現金（已交割現金扣現金緩衝後的餘額）、單股與產業上限餘裕的最小值，只縮不放；換入者先保留預算、不再參與信心預算（`_conviction_targets(reserved=…)`）；縮到不足一張取消（`AGENT_SWAP_CANCELLED:entry_reserve_lot`），依清單先後累計換手、會超過上限的組從該組起取消（`entry_reserve_turnover`），取消的組換出者不賣；有 `size` 的組不適用 `proceeds_to_add`（換入者沒買到整組取消）；超出份量格所需的賣款（例如換出者市值大於份量格）回到一般預算、可能流向其他買單；超過 `max_swaps_per_run` 的組仍從清單尾端取消。quality_dip 配對沿用自己的硬上限、裁決後份量格與「買進不超過賣款」，不吃格點以上現金。沒有此欄位的舊 Policy／舊提名封存／舊 run 逐位元不變。
- **`agent_swap.exposure`（Agent 主導曝險階段 2、3，已開啟：v1 影子 2026-11-10 19:45、v2 23:20，現行 v2）**：`agent_swap` 內的子閘門，前提 `agent_swap` 與 `exposure_view`，只由人設定、策略反思禁區。`{"version":"v1"}` 影子：提名驗證與封存照新規則，配置略過所有 `purpose: exposure` 配對（thesis 配對照常）；`{"version":"v2","max_pairs_by_view":{"start_rebuild":n,"rebuild":m}}`（需 `agent_swap` v2、0≤n≤m≤`max_swaps_per_run`）：配置依風險 Agent 的 `exposure_view`（pipeline 綁進 `exposure.view`，Finalizer 與現金姿態比對）取用——`keep_defensive` 一對都不成交（`AGENT_SWAP_CANCELLED:exposure_view`、換出者不賣、不補挑），其餘取清單前 n／m 對、超過取消（`exposure_cap`，尾端先捨棄），再走與 thesis 配對相同的可行性檢查；減碼日、換手、現金、單股、產業、檔數等界線與降級不變。提名驗證：每對必填 `purpose`；`exposure` 配對的換出依據必須含該持股自己的 `review:<代號>:exposure`（程式由 `exposure_facts` 與該股行情 evidence_id 算出、只能用於 `exposure` 的換出依據、不得放買進依據、第一輪不可採納，不需要空頭論點，仍須持有天數與可交易）；提名 `high` 信心持股做 `exposure` 配對時，`lower_conviction_review` 須逐檔交代全部 medium／low 持股剛好一次（`nominated` 須在某對 exposure 的 `sell_symbol`、`kept` 附理由且不得被換出），且高信心配對排在所有 medium／low 的 exposure 配對之後；否則該欄必須為 `[]`；理由出現曝險區塊識別碼即拒絕。程式封存（exhausted／unavailable）帶空清單。`team_inputs` 不升版（沿用 2.9），Finalizer 以 `exposure_facts` 重算整份提名。
- **`agent_swap.quality_dip`（優質股第三階段，已開啟：v1 影子 2026-11-10 23:05、v2 23:25，現行 v4）**：`agent_swap` 內的子閘門，前提 `agent_swap`、`agent_swap.exposure`（`purpose` 的宿主）、`quality_dip_facts`、`quality_dip_assessment`、`cash_stance_turn_facts`，只由人設定、策略反思禁區；欄位 `version`（v1 影子／v2 正式，v2 須 `agent_swap` v2）、`max_swaps_per_run`、`max_entry_weight`、`min_new_conviction`、`min_holding_days`、`min_turn_assessment`（`turn_unconfirmed`／`turn_confirmed`）全部必填，選填 `oversold`（不得高於上一層），v2 由 `apply_trade_decision` 綁 `turn_assessment`、Finalizer 以現金姿態比對。5c：`purpose` 可填 `quality_dip`，Validator 另驗換入者（下跌、無新壞事、候選標示與 `dip_assessment` 相符、超跌須另開 `oversold`、信心不低於門檻、`buy_basis_ids` 有非 `price` 的已採納多頭 claim）與換出者（hold／low、持有天數；換出依據同 thesis），不符時回饋並列「改用 thesis」與「刪除該對」；簡報多 `quality_dip_nominable` 與 `dip` 背景。配置：v1 略過 quality_dip 配對；v2 在回撤下限 defensive 或歷史不可用、`turn_assessment` 不足、賣款不可再用時取消全部 quality_dip 配對（`AGENT_SWAP_CANCELLED:quality_dip_stance_floor`／`quality_dip_drawdown_unavailable`／`quality_dip_turn`／`quality_dip_proceeds`），超過對數從尾端取消（`quality_dip_cap`），換入者股數以 `max_entry_weight` 與「買進含費不超過換出賣款淨額」為上限（`RISK_QUALITY_DIP_ENTRY`；不足一張時 `AGENT_SWAP_CANCELLED:quality_dip_entry`、換出者不賣）；減碼日整個略過；現金下限、換手、單股、產業、檔數不放寬。
- **`agent_swap.quality_dip` v3（組數與份量由 Agent 提、風險 Agent 裁決，2026-11-11 階段 0b 已開啟，同日 12:36 升 v4）**：前提 `agent_swap` v2，其餘前提同 v2；欄位 `max_swaps_per_run`（0～10，**另計、不占 `agent_swap.max_swaps_per_run`**）、`max_entry_weight`（單檔買進權重硬上限）全由人填，另有 pipeline 綁定的 `turn_assessment`、`ruling_ref`、`rulings`。提名的 quality_dip 配對多 `size`（`small`／`medium`／`large`，權重上限＝硬上限×1/3、2/3、1），有可提名目標卻沒列 quality_dip 配對時須填 `quality_dip_decline`。裁決（`team_inputs.quality_dip_ruling`）：`approve` 維持、`downsize` 降一格（`small` 不能再降）、`reject` 取消；不能升級；防守看法下仍放行須附 `defensive_reconciliation`；裁決失敗取消全部 quality_dip 配對、不退回 v2。前置檢查（姿態下限、回撤、轉向）由純函式 `expected_program_status` 判斷，Finalizer 重算。v1／v2 與舊提名封存逐位元維持原行為。
- **`agent_swap.quality_dip` v4（連跌後逢低布局，2026-11-11 12:36 已開啟，現行；v3 的超集）**：份量格、裁決、組數另計與 `team_inputs` 2.10 全部同 v3；另必填人填的 `ladder`（`min_pool_drawdown`、`max_dip_exposure`、`max_tranches`、`min_days_between_tranches`、`pause_after_further_drop`，`max_dip_exposure` 不得低於 `max_entry_weight`），只有 v4 才可有 `ladder`、才可把 `min_turn_assessment` 填 `no_turn_evidence`。程式新增 `decision/decline_depth.py`：（一）連跌深度事實 `team_inputs.decline_depth_facts`（選配鍵、不升版本，與 v4 雙向檢查，Finalizer 整份重算；`citable=false`、沒有 evidence_id；連跌天數與累計跌幅、離 60 日高點回檔與高點日、20 日新低比例、對照樣本〔連跌剛滿 3 天與回檔達目前深度的日子之後 3／5 日報酬分布，前瞻視窗不得越過最近交易日，`small_sample`、`replay_reference_only`〕，覆蓋低於 80% 或歷史不足只降級為 `unavailable`）；（二）梯度狀態 `ladder_state`（pipeline 在 `apply_trade_decision` 綁進 Policy、Finalizer 重算）：關閉原因 `facts_unavailable`、`held_since_unavailable`、`depth_below_gate`（只在 `no_turn_evidence` 時要求回檔深度）、`exposure_full`、`tranches_full`、`tranche_interval`、`paused_after_drop`、`pause_unavailable`，缺資料一律關閉。本輪下跌起點＝最近 60 個交易日交易池水位最高的那天（不是連跌起始日，避免一個反彈日就重置計數）；新建倉＝帳本連續持有起點不早於起點日的持股（含所有目的的新買進）；今天的買進權重上限加上已有的新建倉曝險超過 `max_dip_exposure` 時從清單尾端取消（`AGENT_SWAP_CANCELLED:quality_dip_ladder_exposure`）；梯度關閉時取消全部 quality_dip 配對（`quality_dip_ladder_<原因>`），風險 Agent 的裁決前置檢查（`program_skip_reason`）讀同一個綁定值。提名簡報附事實與梯度今天的判定（以最嚴格的 `no_turn_evidence` 假設計算），裁決簡報只附事實、不附任何上限數字；Agent 只輸出列舉與文字。沒有 `ladder` 的 v1～v3 與舊 run 逐位元不變。**`ladder.exposure_basis`（選配列舉，C1，已實作、尚未開啟）**：缺席＝總抄底曝險與批數照舊計所有新建倉（逐位元不變）；填 `quality_dip_fills` 只計 purpose=quality_dip 的成交（修正現狀：thesis／exposure／一般買進也算抄底曝險，兩組 medium 就 `exposure_full`、任何新建倉隔天都 `tranche_interval`）。成交清單 `quality_dip.ladder_entries`（`[{symbol, entry_date, run_id}]`）由 pipeline 比照 `bind_reentry_cooldown` 從決策庫確定性取得並綁進 Policy（來源：帳本已套用的前次 run，Policy 的 `nominated_pairs` 為 quality_dip、proposal 有買單與 `AGENT_SWAP_FILL`，只留目前仍持有且帳本持有起點日期不早於該 run 截止日者；樣板預填即拒絕、讀不到綁 null），`build_ladder_state` 只讀清單（公式不變），讀不到或與持股、帳本不一致即以新原因 `entries_unavailable` 關閉；Finalizer 以綁定清單重算並核對每筆在帳本驗證過的 `held_since_by_symbol` 內且日期相容、不重掃決策庫（信任層級同 `reentry_cooldown`）。沒有新的人填數字，開啟由人改 config。
- **`position_sizing.exposure_view` v2（簡報說真話，2026-11-11 12:36 已開啟）**：與 v1 的配置、Schema、`team_inputs` 完全相同，只有風險 Agent 簡報（`risk_exposure_brief` 的 note）與提示文字改為與現行行為一致；前提 `agent_swap` v2 與 `agent_swap.exposure` v2；v1 與沒有閘門的 Policy 簡報逐位元沿用舊文字。
- 設計與實作註記見[換股提名計畫](agent_nominated_rotation_plan.md)；唯讀統計 `cli/calibration.py shadow-swap`（估算是上限、`replay_reference_only`）與 `intent-landing`。

### Agent 自主權線：各版本（`reentry_cooldown` v3、`drawdown_derisk` v5、`cash_fit_order` v2、`high_conviction_cash_priority` v2、`held_weight_budget` v2 皆已開啟）

下列版本皆只由人設定、策略反思禁區，舊版本與沒有欄位的舊 run 逐位元不變；設計與實作註記見[自主權計畫](agent_autonomy_plan.md)。**下表各版本現行正式設定都已開啟**（日期與回滾樣板見 [status.md](status.md#版本閘門啟用狀態)）；較舊的版本只供舊 run 重建。

| 閘門 | 行為 |
| --- | --- |
| `reentry_cooldown` v2 | 冷卻名單只計交易 Agent 自己的 `exit`／`forced_exit`；程式因回撤、換股、單股上限賣出的不計；Finalizer 以決策庫（前次 run 的 `intent.json`）整份重算名單，讀不到或不符即拒絕 |
| `reentry_cooldown` v3 | 名單口徑同 v2，但不再擋買單：交易 Agent 簡報附 `cooldown:<代號>` 提示，買回（buy）時須放進 `adopted_claim_ids` 回應；沒回應是由配置取消該買單（`RISK_REENTRY_COOLDOWN`），不是驗證錯誤；`agent_swap` 的換入候選仍排除冷卻名單 |
| `drawdown_derisk` v5 | 數字全同 v4，只把「先賣誰」改成交易 Agent 的釋出順序（`release_order`：涵蓋全部可減碼持股各一次、只有代號與文字）；pipeline 綁 `release_symbols`／`release_ref`；順序 `exhausted`／`unavailable` 時退回 v4 的 ATR 順序並留 `DERISK_ORDER_SKIPPED:order_*`（降級，不是整份拒絕）；前提 `selection_ranking` |
| `cash_fit_order` v2 | 買單取得現金的順序同 v1，新股只買得到零頭時不再整筆取消（無 `RISK_CASH_FIT`）；不允許 `cash_fit_min_fill_ratio` |
| `high_conviction_cash_priority` v2 | 保留現金先給 high 與 high 零頭不取消，移除「high 被縮減後擋掉後面未持有的中低信心新股」 |
| `held_weight_budget` v2 | 預算算法同 v1；交易 Agent 簡報多附持股權重事實（`team_inputs.held_weight_facts`；現有權重、單股上限、`limit_scaled_conviction` 啟用時 high 份額下限，**不含** medium／low 的份額，因為簡報時現金與廣度尚未確定）；需 `selection_ranking` |

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

- 欄位 `{version: v1}`；只能用於 `evidence_policy: typed_evidence_v1`，可與 `cash_ratio_range` 獨立開關；啟用狀態見 [status.md](status.md#版本閘門啟用狀態)。
- 簡報第一個鍵 `evidence_recency` 由程式依 bundle 重算：市場層級證據的內容時間（新聞 `published_at`、隔夜時段 `session_end`、恐懼貪婪資料日）、距截止小時數與三個分組，界線是台股最近交易日 13:30；內容時間晚於截止即拒絕，找不到內容時間的證據記入 `data_gaps`；不含新聞文字。
- 風險 Agent 另填列舉 `recency_reconciliation`；最新一組（`after_latest_close`）非空時不得填 `newest_insufficient` 且須引用其中至少一個證據，為空時只能填 `newest_insufficient`。Skill 規定：最新一組的事件與新聞預設為判斷「情況有沒有改變」的主要依據，利多利空同樣適用，不採用須說明；隔夜行情仍只是背景。這條是給 Agent 的取捨規則，程式只驗引用與列舉，不要求列舉與姿態對應，不放寬任何現金格點、上限、Guard 或回撤下限。
- `team_inputs` 不升版：閘門決定 `cash_stance` 允許的欄位（與 `cash_grade` 同做法），Finalizer 與 `apply_trade_decision` 以 bundle 重算時間表再驗；回放略過此閘門（notes 標 `cash_stance_recency: ignored_in_replay`）。
- 省略此欄位的舊 Policy／舊 run 逐位元不變。

### 現金姿態的轉向事實（`cash_stance_turn_facts`）

- 欄位 `{version: v1}`；只能用於 `evidence_policy: typed_evidence_v1`，與 `cash_stance_recency`、`cash_ratio_range` 互相獨立；目前已開啟（見 [status.md](status.md#版本閘門啟用狀態)）。這是**紀錄工具，不是規則**。
- 簡報在 `regime_assessment` 旁多一個鍵 `turn_facts`，由 `decision/turn_facts.py` 只依 `price_series` 重算：最近兩日池等權日報酬、連續上漲日數、站回 MA5／MA20 的股票比例、MA20／MA60 之上比例（呼叫 `MomentumEngine._regime`，與 regime 同一公式）、MA20 之上比例的 5 日變化、20 日新低比例、regime 連續 bear 天數（需 61 筆以上，被歷史長度截斷時標 `bear_streak_truncated`）。全部為字串或整數，沒有文字判斷；資料不足記入 `data_gaps`；任何 bar 晚於 `snapshot.latest_trade_date` 即拒絕。
- 風險 Agent 另填列舉 `turn_assessment`（`no_turn_evidence`／`turn_unconfirmed`／`turn_confirmed`）；驗證只檢查必填與值域，不驗與姿態的對應、不驗文字品質，不放寬或收緊任何現金格點、`stance_floor` 或 Guard，也不改 `_regime`。Policy 另啟用 `rebound_participation` 時，這個列舉才是雙鑰匙的一把（與程式狀態取較保守者，只放寬防守姿態下的曝險換股，見下方「反彈參與」）；沒有該閘門時它仍然只是紀錄。
- 簡報 note 與 Agent Skill 只保留一般性風險提醒，不硬寫特定日期案例、後續報酬或未由本次資料計算的交易成本；歷史重跑也不得取得 cutoff 之後的案例。
- `team_inputs` 不升版；Finalizer 與 `apply_trade_decision` 以 bundle 重算再驗；回放略過此閘門（notes 標 `cash_stance_turn_facts: ignored_in_replay`）；策略反思不得改。省略此欄位的舊 Policy／舊 run 逐位元不變。

### 曝險換股與 hold 信心（`position_sizing.exposure_rotation` v1／v2／v3）

- 欄位：`version`、`beta_window_days`、`beta_floor_by_stance`（`defensive` 只能 null）、`release_margin`、`max_swaps_per_run`、`max_rotation_fraction`、`min_holding_days`、`min_new_conviction`（不得低於 `rotation` 的）、`hold_conviction: v1`、選配 `conviction_gap_swap: v1`，另有流程綁入的 `held_since_by_symbol`（不得預填）。前提：`rotation`、`held_add_priority`、`selection_ranking`、`reentry_cooldown`、`turnover_fit`、`cash_buffer_floor` 與頂層 `position_review`。人設定、策略反思不得改；啟用狀態見 [status.md](status.md#版本閘門啟用狀態)。
- **v2**（2026-11-09 已實作，同日 17:50 正式設定切到 v2，計劃見 [exposure_rotation_v2_plan.md](exposure_rotation_v2_plan.md)）：欄位只有 `version: v2`、`beta_window_days`、`beta_target_by_stance`（停止線，`defensive` 只能 null，進攻 ≥ 中性）、`max_swaps_per_run`、`min_holding_days`、`hold_conviction: v1`、選配 `conviction_gap_swap: v1` 與流程綁入的 `held_since_by_symbol`；v1 專屬欄位（`beta_floor_by_stance`、`release_margin`、`max_rotation_fraction`、`min_new_conviction`）出現即拒絕，v1 閘門逐位元不變。差異：觸發與停止都是「含現金整體 beta ≥ 目標」（沒有釋放帶）；換入者 beta 必須高於組合 beta（v1 是 ≥ 組合 beta + `release_margin`）；沒有賣出市值比例上限，換出量只受 `max_swaps_per_run` 與單股上限間接限制（換手由 `turnover_fit` 把關）；換入者的最低信心沿用 `rotation.min_new_conviction`。
- **v3**（2026-11-09 已開啟，`beta_target_by_stance.defensive` 仍是 null；動機見 [exposure_rotation_v2_plan.md](exposure_rotation_v2_plan.md#12-v3防守姿態的-beta-停止線)）：欄位集合與全部行為都同 v2，唯一差別是 `beta_target_by_stance.defensive` 可填數字（v1／v2 只能 null）；`defensive` 為 null 時等同不觸發，有填時要求 進攻 ≥ 中性 ≥ 防守（有填的比較）。`team_inputs` 結構不變（仍為 2.7，`exposure_review.policy.version` 只回寫閘門版本）；與 `drawdown_derisk` 互斥（減碼日略過）、換股上限、不只賣不買、賣單標 `exit` 等規則與 v2 相同。v1／v2 的 Policy 重建逐位元不變。
- hold 信心：閘門開啟時交易 Agent 對 `hold` 也必填 `conviction`，程式綁入 `hold_conviction_by_symbol`（不得與 `conviction_by_symbol` 重複）；沒開時 hold 帶信心被拒。
- `exposure_review`（`team_inputs` 2.7）：程式依 `price_series` 對交易池等權基準算每檔 beta、up／down capture 與含現金的組合 beta；任何持股缺資料時組合層級為 null，不補假值。
- 持有天數由帳本持股歷史算（連續持有、台北時區日曆日），Finalizer 以帳本獨立重算，讀不到或與 Policy 不符即拒絕。
- 換股：姿態有非 null 下限且組合 beta 低於「下限 − `release_margin`」（死區，起止同一條線，沒有跨日狀態）時，把「本次 buy 被名額擋下」的高 beta 新股換掉「hold、信心非 high、持有天數足夠、beta 低於組合」的持股；`conviction_gap_swap` 另可在任何姿態以 high 新股換 low 持股。兩者共用 `max_swaps_per_run`、`max_rotation_fraction`（v2 沒有後者），換出者的賣單由程式標 `exit`（旗標 `RISK_EXPOSURE_ROTATION`／`RISK_CONVICTION_GAP_SWAP`，不觸發時留 `…_SKIPPED:<原因>`），換入者拿不到名額就捨棄該對，絕不只賣不買。首個換入候選無可負擔的配對時，依原信心／名次繼續嘗試後續候選，不讓昂貴候選擋住整條配對；失敗嘗試不預扣資金。與 `drawdown_derisk` 互斥（減碼日整個略過）；`forced_swap` 與交易 Agent 的 exit／trim 優先。
- 回放略過此閘門（notes 標 `exposure_rotation: ignored_in_replay`）；省略此欄位的舊 Policy／舊 run 逐位元不變。

### 反彈參與（`position_sizing.rebound_participation` v1 已開啟；v2 已開啟）

動機、定義、過擬合風險與重放結果見 [rebound_participation_plan.md](rebound_participation_plan.md)。版本閘門，只由人設定，策略反思不得改（`FORBIDDEN_PREFIXES`）；沒有此欄位的舊 Policy／舊 run 逐位元不變。

- 前提（缺一 fail-closed 拒絕）：`exposure_rotation.version = v3`、`cash_stance_turn_facts = {version: v1}`、`drawdown_derisk.version = v4`、bundle 為 `typed_evidence_v1`（建 `turn_state` 時檢查）。
- 欄位（全部由人設定、程式不給預設值）：`version`、`candidate_return`、`candidate_ma5_share`、`confirm_return`、`confirm_ma5_share`、`confirm_new_low_share_max`、`candidate_expiry_days`、`retreat_return`、`retreat_window_days`、`retreat_ma5_share`、`min_coverage`、`by_state`（`candidate`／`confirmed` 各有 `defensive_target`、`max_swaps_per_run`、`min_holding_days`；選填 `defensive_cash_buffer`（兩態皆可，見下「條件式現金放寬」）；`confirmed` 另可選 `neutral_target` 與 `max_turnover_rate`（見下「confirmed 換手上限」））；流程綁入、不得預填的 `effective_state` 與 `cash_loosening`。`by_state` 的檔數不得高於 `rotation.max_swaps_per_run`，confirmed 不得比 candidate 更保守（停止線不降、檔數不減、持有天數不增、現金比例不升）。小動大動分開：candidate 只建議放寬停止線（`max_swaps_per_run` 與 `min_holding_days` 與現行曝險換股相同），只有 confirmed 才放大檔數並取消持有天數限制。
- **程式狀態 `turn_state` ∈ none／candidate／confirmed**（`decision/rebound.py`）：只在 regime（`MomentumEngine._regime`）為 bear 時有意義，其餘恆 none；訊號只用 `turn_facts` 既有欄位（池等權日報酬、站回 MA5 比例、20 日新低比例），沿 `price_series` 逐日推進的無記憶重算（不跨 run 存狀態）。none→candidate：當日池報酬 ≥ `candidate_return` 且站回 MA5 比例 ≥ `candidate_ma5_share`（該日為第 1 日）。candidate→confirmed：第 2 日起至第 `candidate_expiry_days` 日，**轉向日之後**各日池報酬複利 ≥ `confirm_return`（不含轉向日當天）、站回 MA5 比例 ≥ `confirm_ma5_share`、20 日新低比例 ≤ `confirm_new_low_share_max`；第 `candidate_expiry_days` + 1 日仍未確認即回 none（期間再出現大漲不重設計數）。退回 none：近 `retreat_window_days` 日池報酬複利 ≤ `retreat_return` 且站回 MA5 比例 < `retreat_ma5_share`（單日大跌不退）；或 regime 離開 bear。任一訊號算不出、覆蓋 < `min_coverage`、regime 不可用（歷史不足 61 筆）或 bar 晚於 `snapshot.latest_trade_date`：回 none 並標 `TURN_STATE_UNAVAILABLE`（缺資料永不升級）。
- **雙鑰匙**：`effective_state = min(程式 turn_state, 風險 Agent 既有 turn_assessment 對映)`，`no_turn_evidence`→none、`turn_unconfirmed`→candidate、`turn_confirmed`→confirmed；程式單獨不能升、Agent 單獨也不能升。Agent 不輸出任何數字、不新增欄位，驗證只核列舉值域。`drawdown_control.stance_floor` 為 defensive 時恆 none（回撤下限優先）；減碼日（`drawdown_derisk`）曝險換股整個略過。`effective_state` 由 `apply_trade_decision` 綁進閘門，Finalizer 以 `team_inputs.turn_state` 與風險 Agent 列舉重算比對。
- 對映（`apply_rebound`，只放寬、不收緊）：有效狀態不是 none 時，曝險換股的防守 beta 停止線取 `max(現行, by_state.defensive_target)`（`neutral_target` 選配）、`max_swaps_per_run` 取較大、`min_holding_days` 取較小；換出者範圍不變（交易 Agent 標 `hold` 且非 `high`）、賣單仍標 `exit`、不改寫交易 Agent 意圖、不只賣不買，`turnover_fit` 仍是最終上限；不改持股廣度。留下旗標 `REBOUND_PARTICIPATION:<state>`（symbol 為 `PORTFOLIO`）。
- **條件式現金放寬（選填 `by_state.<state>.defensive_cash_buffer`，放寬風控、只由人設定）**：有效狀態為 candidate／confirmed 且風險 Agent 的姿態為 `defensive` 時，`apply_trade_decision` 把 `cash_buffer_rate` 改為 `min(grid 或 cash_buffer_by_stance 的原比例, 該態目標)`——這是程式在「雙鑰匙都同意轉向」才成立的條件式覆寫，覆寫的是風險 Agent 的 defensive 現金子等級，只放寬、永不收緊；neutral／aggressive 不動。目標不得低於中性姿態最低現金比例、confirmed ≤ candidate。覆寫前後值綁進閘門的 `cash_loosening`（`{from, to}`），`sizing.validate_position_sizing` 以 `rebound.loosened_cash_buffer` 重算 `cash_buffer_rate` 與 `cash_loosening` 比對（Finalizer 隨 Policy 驗證一併重算），配置留下旗標 `REBOUND_CASH_LOOSENED:<state>`。與其他閘門：`drawdown_control.stance_floor` 為 defensive 時有效狀態恆 none，所以回撤下限優先；`cash_ratio_range.max_daily_raise` 只限制往上拉高風險 Agent 可選的格點，放寬是往下，不受影響；放寬後的比例成為次日 `cash_ratio_range` 的 anchor（次日若轉回防守，`max(lower, anchor + max_daily_raise)` 仍保證至少 lower 格可選）；`cash_buffer_floor`、`drawdown_derisk` 都讀放寬後的現金比例（防守姿態減碼的目標隨之降低，減碼日仍整個略過曝險換股）；與 `high_conviction_cash_floor` 本來就互斥。
- **confirmed 換手上限（選填 `by_state.confirmed.max_turnover_rate`，放寬風控、只由人設定）**：`max_turnover_rate` 是 `config/decision_policy.json` 的可調 Policy 值，不是競賽硬規則（`competition_rules.json`、`competition_profile.json`、`decision_rules.json` 都沒有換手上限；只有 Guard 的 `TURNOVER` 檢查與 `turnover_fit` 讀它）。有效狀態為 confirmed 且有此欄位時，`effective_policy.max_turnover_rate` 取 `max(Policy 值, 欄位值)`（只放寬），Guard 的 `TURNOVER` 同步用放寬後的上限，留下旗標 `REBOUND_TURNOVER_RAISED:confirmed`；candidate 與 none 沿用 Policy 值。修正循環的 `reduce_turnover_limit` 仍只能往下。
- 風險 Agent 簡報附 `turn_state`（`citable=false`、無 `evidence_id`，只當背景）；簡報與 Skill 不寫特定日期案例。回放略過此閘門（notes 標 `rebound_participation: ignored_in_replay`）；用新 Policy 重跑較早 cutoff 屬反事實評測（`evaluation_mode: counterfactual`）。
- **證據等級**：門檻是以只有極少數大漲日的歷史擬合出來的（n ≤ 3），重放全部是 `replay_reference_only`，不是對 LLM 的前瞻驗證；只有比賽期間的即時紀錄算證據。

#### v2（2026-11-10 01:37 已開啟）：只算事實、由風險 Agent 單獨決定

動機與取捨見 [rebound_participation_v2_plan.md](rebound_participation_v2_plan.md)。v1 保留不動（舊 Policy／舊 run 逐位元重建）；v2 是同一欄位的另一個版本，依 `version` 分派。

- 欄位只剩 `version`、`min_coverage`（資料品質數字，不是判斷）、`by_state`（對映列，規則同 v1）與程式綁入的 `effective_state`、`cash_loosening`；v1 的轉向門檻（`candidate_*`、`confirm_*`、`retreat_*`、`candidate_expiry_days`）出現即拒絕，v1 缺它們也照舊拒絕（不混用）。前提同 v1。
- `team_inputs` 仍為 2.8、欄位名仍是 `turn_state`，但 v2 的 `turn_state`（`schema_version` 2.0）**沒有 `state` 欄位**：只有 `data_status`（available／unavailable）、`regime`、最新一日 `signals`（池報酬、站回 MA5 比例、20 日新低比例、當日覆蓋 `coverage` 與報酬樣本覆蓋 `return_coverage`）、最近 5 日 `recent_path` 與 `flags`。Finalizer 以同一輸入整份重算比對，欄位白名單與 v1 分開。
- `effective_state` 只由風險 Agent 的 `turn_assessment` 對映（`no_turn_evidence`→none、`turn_unconfirmed`→candidate、`turn_confirmed`→confirmed）；程式只在 `data_status` 為 unavailable（當日覆蓋不足、前一交易日缺很多檔使報酬樣本覆蓋 `return_coverage` 不足、行情缺漏、regime 算不出、bar 晚於最近交易日）或 `drawdown_control.stance_floor` 為 defensive 時壓回 none。**與 v1 的行為差異**：regime 不是 bear 不再擋。
- 已知缺口：`turn_assessment` 成為唯一鑰匙後，驗證仍只核列舉值域，不要求引用證據、不驗文字品質（使用者 2026-11-10 決定先不加規定，風險由 `by_state` 的放寬幅度與換手、現金上限承擔）。
- 風險 Agent 簡報附 v2 的 `turn_state`（`citable=false`），提示明講「你的 turn_assessment 是唯一決定依據」。

### 轉向後進攻（`position_sizing.offense` v1，2026-11-11 14:40 已開啟）

- 版本閘門，只由人設定、策略反思禁區（`offense` 前綴）。形狀：`{version: "v1", by_view: {probe, start_rebuild, rebuild: {defensive_cash_buffer, max_exposure_pairs, max_size, beta_stop}}, offense_off_drawdown}`，缺欄位即拒絕。驗證：等級單調（越進攻現金越低、對數／份量格／beta 越高）；`defensive_cash_buffer` 不低於中性姿態最低格點；`max_exposure_pairs` 不高於 `agent_swap.max_swaps_per_run` 且等於 `agent_swap.exposure.max_pairs_by_view` 同名鍵（有 offense 時該表鍵為 probe／start_rebuild／rebuild，probe ≤ start_rebuild）；`beta_stop` 不高於 `exposure_rotation.beta_target_by_stance.aggressive`；`offense_off_drawdown` 低於 `drawdown_control.defensive_at`；與 `rebound_participation` 的 `defensive_cash_buffer` 互斥；樣板預填 `view`／`effective_view`／`suppressed_reason`／`cash_loosening` 即拒絕。
- 風險 Agent：`exposure_view` 多 `probe`，與 `turn_assessment` 一致；非 keep_defensive 至少兩個市場層級證據且其一非新聞；probe 須填 `offense_basis`（至少兩個 available 類別）與含時間條件的 `failure_condition`；防守姿態下非 keep_defensive 須填 `defensive_reconciliation`。簡報不附交易 Agent 的意圖與信心統計。
- `offense_facts`：`team_inputs` 選配鍵（不升版、須 2.9 以上，與閘門雙向同時存在），`citable=false`、無 `evidence_id`，Finalizer 整份重算；缺資料的類別為 `unavailable`。情緒類目前沒有逐日歷史分數，5 日變化為 null 並記 data_gap。
- 綁定：`apply_trade_decision` 把 `view`、`effective_view`、`suppressed_reason`（facts_unavailable／stance_floor／drawdown_unavailable／drawdown_limit）與 `cash_loosening` 綁進 Policy，Finalizer 重算比對；旗標 `OFFENSE:<等級>`、`OFFENSE_SUPPRESSED:<原因>`、`OFFENSE_SIZE_CAPPED`。
- 配置：防守姿態現金目標為 min(格點, 該級緩衝)（只放寬）；曝險配對至多 `max_exposure_pairs` 組、份量格縮到 `max_size`、依提名先後試算組合 beta，預估達 `beta_stop` 後的曝險配對取消（`offense_beta_stop`），beta 缺值取消（`offense_beta_unavailable`）；程式不挑股、不改寫交易 Agent 意圖。回放略過 offense（`offense: ignored_in_replay`）。

- 選配子欄位（已開啟，2026-11-11，`policy_id strategy-2026-11-11-offense-ext-1`；各只有 `{"version": "v1"}`，沒有人填數字，缺席時 Policy、簡報、提示與配置逐位元不變）：
  - `offense.thesis_size_cap`：生效條件為閘門已綁定、`cash_loosening` 不是 null 且有效等級在 `by_view` 內；`purpose=thesis` 配對的份量格取 min(交易 Agent 的 `size`, 該級 `max_size`)（有縮留 `OFFENSE_SIZE_CAPPED`），beta 估算用縮後權重；`quality_dip` 配對不動，thesis 配對不占 `max_exposure_pairs`、不被 `beta_stop` 取消。不限制動用放寬的現金。Finalizer 沒有新檢查（配置由 Policy 重算）。
  - `offense.cash_glide`：需要 `cash_ratio_range`。生效條件全部成立才滑行（任一不成立照格點，方向是多留現金）：今日姿態 defensive；anchor 為 `prior_decision` 且 `prior_offense_loosened` 為 true（pipeline 讀前次 run 的 policy，其 `offense.cash_loosening` 或 `cash_raise_limited` 非 null；policy 雜湊或與 proposal 的 `policy_hash` 不符一律拒絕）；回撤下限不是 defensive、帳本回撤可用且低於 `offense_off_drawdown`。作用：現金目標＝min(offense 放寬後的目標, anchor＋`max_daily_raise`)；綁定紀錄 `offense.cash_raise_limited`（`{from, to}` 或 null，只在有此欄位的 Policy 出現），Finalizer 重算並比對，`cash_buffer_rate` 必須等於 `to`。`drawdown_derisk` v5 仍照交易 Agent 的釋出順序賣到當日現金目標。

### 輸入時間與評測帳本的前置驗證

- 日行情日期須為 `YYYY-MM-DD`，Snapshot 的逐檔最新日須等於 `latest_trade_date`，各歷史 bar 不得晚於它；日收盤內容時間以台北交易日 13:30 計，內容時間不得晚於 cutoff，`available_at` 不得早於內容時間或晚於 cutoff。重新計算雜湊不能略過這些語意檢查。
- 完整每日鏈在首次 Agent 呼叫前建立基礎 Policy、綁定回撤與持有天數並驗證前提；只跑分析師時不要求交易策略或帳本。
- 完整 Agent 評測在建考卷時沿封存 Policy 的 `history_ref.head_run_id` 讀已驗證帳本，凍結同鏈的 NAV 與持股歷史；時間、持股、Snapshot、帳戶與參照雜湊均須一致。重跑只讀這份快照、不讀今日 latest，續跑另比對快照雜湊。所有考卷的缺帳本檢查先於任何 Agent 呼叫；需要曝險換股而沒有快照時明確拒絕，不默默關閘門。舊考卷可明確選只跑分析師；完整評測須重建有可核對錨點的評測集，不猜歷史。

### 2026-11-03 起的執行面閘門

全為 `position_sizing`／rules 的版本閘門；白話說明見[決策流程細節](decision-details.md#交割與現金2026-11-03-起)。

| 閘門 | 行為 |
| --- | --- |
| 交割口徑 `reuse_sell_proceeds=true` | 主辦方淨額交割，賣款當日可用（規則版本 `decision-rules-2026-11-03b`）；產業上限 `max_sector_weight=0.6` 只擋增量、不強賣既有持股 |
| `cash_fit_order` | 現金不足時依信心、名次分錢；新股殘量不足（低於 `cash_fit_min_fill_ratio`）即整筆取消，`RISK_CASH_FIT`；持有的 add 只減量 |
| `high_conviction_cash_priority`（2026-11-06，須與 `cash_fit_order` 同時出現） | 可動用現金先全部給 high，high 新股不做零頭取消、買得到幾張買幾張；high（含持有的 add）被縮減且至少買到一張後，後面未持有的中、低信心新股降為 0 並留 `CASH_BUFFER` 旗標，持股加碼不受影響；high 連一張都買不起而整筆取消時不擋後面買單 |
| `held_weight_budget`（2026-11-08） | 已持有的加碼候選現有權重高於信心份額時，以現有權重計入預算、不再加碼、不建買單（`RISK_HELD_WEIGHT`），避免其他候選被超配後又被現金緩衝砍回 |
| `slot_recycle`（`{version: v1}`，2026-11-10 23:25 已開啟） | 排名在前的新股因預算為零或目標金額不足一張（`LOT_SIZE`）而不會成交時，不再占著新增名額：移出名額計數後重算，讓後面的候選補位（原本被記 `MAX_POSITIONS`）；移出者仍記 `LOT_SIZE`、權重為 0，入選總數仍受持股上限、廣度目標與換股名額限制。只處理 `LOT_SIZE`／預算為零，不處理現金、權重或換手等後續步驟才沒有買單的候選。省略此欄位的舊 Policy／舊 run 逐位元不變；策略反思不得改 |
| `cash_ceiling_fix` | 現金逼近 25% 上限時留住 exit，`RISK_CASH_CEILING` |
| `stock_limit_headroom` | buy／add 的單股上限留 3% 空間 |
| `buy_gap_reserve` | 買單預留跳空緩衝（`buy_gap_reserve_rate`） |
| `insufficient_funds_policy: "void_batch"` | 現金不足整批作廢，與主辦方一致 |
| `turnover_fit`＋`max_turnover_rate=0.3` | 換手超過上限時縮減買單；賣單本身超過時留住 exit |
| `reentry_cooldown` | 出場後 3 日不得再買未持有新股，`RISK_REENTRY_COOLDOWN` |
| `drawdown_derisk` | defensive 時程式按 ATR 減碼至緩衝，附 hysteresis 的 `release_*`；`version` v1／v2／v3 差異見[決策流程細節](decision-details.md#控制回撤)，v4（2026-11-08）在 v3 之上把單次減碼預算限制在「換手上限減去交易 Agent 賣單」的餘額內；現行正式設定為 v5（見「Agent 自主權線」表） |
| `forced_swap.confirmation` | 強制換股須有交易 Agent 採納的該股空頭 claim（`forced_swap` 已於 2026-11-10 20:10 隨 `agent_swap` v2 自現行設定移除，僅舊 Policy 重建使用） |
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
- **回放是樣本內比較**：影子回放用的是提案時已經看過的同一段日期，不是樣本外驗證；其中 N 天的策略樣板建立時間（`available_at`）晚於當日截止時間而被改寫成 cutoff。`replay_gate` 輸出與套用歷史的 `replay_gate` 摘要因此多 `in_sample`（固定 true）與 `available_at_overridden_days`（候選回放被改寫的天數），週報在回放提醒後多一行同樣說明；只補揭露，`passed`／`reasons`／`segments` 與通過條件不變，也不加樣本外條件。
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
