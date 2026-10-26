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
| 風險 | 配置前給現金姿態與持股廣度（集中／平衡／分散）；配置後審查並提出受限修正 | 從帳本 NAV 歷史算峰值回撤並強制姿態下限；持股廣度依 Policy 換算成 22／26／30 檔，照信心、支持數、ATR14 挑股；提供整體市場情緒與每筆委託佔 20 日均量比例；重算費稅、現金、情境與競賽限制；硬性規則失敗即拒絕，修正循環最多 3 次 |

## 持股檔數與取捨順序

持股檔數由風險 Agent 的持股廣度決定：`concentrated`（集中）、`balanced`（平衡）、`diversified`（分散），對應 `config/decision_policy.json` 的 `positions_by_breadth`（目前 22／26／30 檔，配置時夾在 `min_positions`～`max_positions`）。集中設 22 而非 20，是留兩檔緩衝給 Active Share 或不可交易剔除。Agent 只選等級，不輸出檔數；程式把廣度綁成 Policy 的 `position_breadth`，Finalizer 依 `team_inputs.cash_stance.breadth` 重建核對。

買進候選超過該檔數時，程式依下列順序決定誰先入選，其餘標為 `MAX_POSITIONS`：

1. 信心等級：`high`、`medium`、`low`。
2. 同等級時，交易決策採納的多頭 claim 數減空頭 claim 數，越大越先（`support_by_symbol`，綁在 Policy 並由 Validator 重算）。
3. 仍相同時，ATR14 占價格比例低者先（波動小的先入選）。
4. 再相同時，依股票代號。

舊 Decision run 沒有 `positions_by_breadth`，仍填滿 `max_positions` 且不看 ATR；更舊的沒有 `support_by_symbol`，只依等級與代號。策略回放遇到沒有持股廣度的舊封存時，拿掉樣板的 `positions_by_breadth` 沿用舊行為並在報告註明。

## 對應參賽策略文件的四類因子

參賽策略文件（`game_docs/ETF-AGENTS_AI_Agent_選股與決策策略_參賽格式.pdf`）寫的因子，都由程式計算後交給 Agent 判讀：

| 文件寫的 | 程式提供 | 交給誰 |
| --- | --- | --- |
| 基本面：營收、獲利、負債、現金流與期間比較 | 財務比率、月營收、`cash_flow`（營業現金流、資本支出、自由現金流、營業現金流對稅前淨利比率） | 基本面分析師 |
| 技術：5／20／60 日變化、均線、ATR 與下行波動 | `return_5d／20d／60d`、`ma20`、`ma60`、是否站上均線、`atr_14_pct`、`downside_volatility_20d` | 技術分析師 |
| 公司事件：事件影響、反證與尚待確認事項 | 重大訊息原文、同快照財報，以及截止時已核准的 FinMind 個股新聞標題（白名單媒體、截止前最後一批）；Agent 逐則輸出 `summary`、`counter_evidence`、`pending_confirmations` | 事件分析師 → 多空研究員 |
| 市場背景與風險：整體市場情緒、集中度、流動性與壓力情境 | `market_sentiment`、產業曝險、`liquidity_exposure`、壓力情境、峰值回撤 | 風險 Agent |

### 回撤控制

- 程式沿帳本 `parent_run_id` 往回取每個狀態的 NAV，峰值回撤 = (峰值 − 目前 NAV) ÷ 峰值。
- Policy 的 `drawdown_control` 設門檻（目前回撤 ≥ 5% 時姿態至少 `neutral`、≥ 10% 時至少 `defensive`），觀察值與下限綁進 Policy，由 `DecisionPolicyValidator` 重算。
- 風險 Agent 給的姿態比下限積極時，程式退回重答；Agent 不計算回撤，也不能放寬。
- 舊 Decision run 的 Policy 沒有 `drawdown_control`，照原樣重建驗證。

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

  - 兩次都 `approved`、30 筆委託，沒有撞到用量上限。實測當時 `agent_step` 只在輸出驗證失敗時重試；用量或執行錯誤會直接失敗，所以那次重試是輸出驗證問題，不是限流。（之後新增：CLI 逾時、無法啟動、非零結束、輸出不是 JSON 或沒有結構化輸出屬 `TransientAgentError`，會間隔 30 秒再試一次，失敗嘗試記在 `agent_calls` 的 `transient_error`／`transient_retry`，用量記 0、耗時保留；秘密外洩、使用未核准工具與驗證失敗不重試；平行任務有一個已失敗時不再重試；等待加重跑會超過交件期限 08:55 時也不重試。）
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
- 風險 Agent 不手寫股數或權重。
- 交易受限、整張／現金／換手等限制無法同時滿足時，CompetitionGuard 仍然拒絕。

## Decision run 的 `team_inputs`

| 版本 | 內容 |
| --- | --- |
| 2.2（目前） | 三份分析報告、現金姿態，以及交易 Agent 看到的前次決策背景 `prior_decision_context`（available 或 unavailable 都要存） |
| 2.1（舊封存） | 三份分析報告與現金姿態 |
| 2.0（舊封存） | 依原始分析報告與事件研究輸入重建 |

2.1／2.2 的事件報告可為 2.2 或 2.3，由決策資料包決定：資料包含 `company_news_input` 時必須是 2.3，否則是 2.2；沒有新增 `team_inputs` 版本。

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

## 校準旁觀者

`src/etf_agent/calibration/` 只讀已封存的 Decision run 與官方價量，記錄各股的信心等級與多空強度，價格到齊後以官方成交均價算前瞻報酬，並審計決策用的前日收盤與當日成交均價的差距。**不在決策鏈內**：決策、自動化與風控程式都不匯入它，也不回寫任何決策 artifact。完整口徑與限制見[校準旁觀者](../src/etf_agent/calibration/README.md)。

## 事件如何處理

- 重大公司事件保留在事件分析師報告，直接交給股票層級的多頭／空頭研究。
- 決策截止時 FinMind 個股新聞已核准時，`build_input_file` 在資料包加入 `company_news_input`（`company-news-1.0`），事件報告升為 2.3；每則新聞須在 `events` 剛好分類一次，見[事件研究整合說明](integrated_event_analysis.md#事件報告-23已核准個股新聞)。個股新聞只進公司事件通道，不進市場情緒。
- 每日鏈不再額外執行 Fact／Bull／Bear／Adjudicator。
- 獨立事件研究與舊封存驗證仍可使用，見[事件研究整合說明](integrated_event_analysis.md)。
- 第三位分析師把全市場情緒與逐家公司事件分開，見[市場與公司事件範圍](event_market_scope.md)。

## 子 Agent 執行器

用 `--agent-cli claude|codex|opencode` 選擇。三者的輸出都進入同一套 Validator，續跑必須沿用原執行器。

| 執行器 | 驗證狀態 |
| --- | --- |
| Codex | 已完成真實 150 檔每日鏈 |
| OpenCode | 已通過本機唯讀、JSONL、預檢煙霧測試。**尚未**執行真實 150 檔端到端驗收 |
| Claude | 每日鏈使用中 |

讀檔邊界（三者共通）：

- 拒讀樣式集中在 `agent_runner.SENSITIVE_READ_GLOBS`（`.env`、`.env.*`、`.envrc`、`*.pem`、`*.key`、`*credential*`、`*secret*`）。Claude 以 `--settings` 的 `permissions.deny` 套用（已用本機 `claude -p` 實測擋下根目錄與子目錄的 `.env`、`.key`），OpenCode 以 read 權限套用。
- Codex 的 `--sandbox read-only` 只禁止寫入，**不能**限制讀檔；這條路徑只靠下一條的輸出檢查。
- 所有 runner 的輸出若含專案 `.env`／`.envrc` 中長度 8 以上的值，一律拒絕，錯誤訊息不帶出該值。

OpenCode 的限制：

- 使用 `run --pure --format json` 與單次隔離的唯讀 Agent。
- 只准讀檔（`.env` 與外部目錄禁止），不准執行命令或修改檔案。
- OpenCode CLI 沒有本流程可直接用的 `--output-schema`。提示會帶 JSON Schema，但仍要靠確定性 Validator 與有限重試，拒絕不合格的結果。

全市場情緒優先使用全市場新聞（`automation/market_news_stage.py`：截止前 RSS 抓取、子 Agent 逐則標註、程式重建，失敗時降級）；新聞不可用時改用[台股恐懼貪婪指標](market_fear_greed.md)（程式計算，cutoff 前抓取）；兩者都不可用時維持 `unavailable`。不得用公司新聞或交易背景補值。
