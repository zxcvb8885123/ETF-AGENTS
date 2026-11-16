# 每日決策 token／美元成本降低計畫（草案，未實作）

狀態：草案，2026-11-17 撰寫；**只有計畫，沒有改任何程式、契約、設定或 Skill**。所有「預估省多少」都是估算區間，不是實測；
凡是用封存 run 重算的品質數字一律是 `replay_reference_only`，不是對 LLM 判斷的前瞻驗證。

與既有文件的關係：

- [`context_slimming_plan.md`](context_slimming_plan.md)（2026-11-13）已逐欄盤點各 brief 的瘦身項目。本計畫**不重做**那份盤點，只在第 4.7 節更新它的現況與剩餘項目。
- [`../token_usage.md`](../token_usage.md) 的驗收關卡（雙側基準、逐階段 A/B、全量覆蓋、逐條核對）仍然有效，本計畫的品質守門沿用它。
- 本計畫新增的是：輸出 token、跨 run 沿用、新聞增量標註、失敗與重試、快取可控範圍、模型分級，以及文獻依據。

名詞：「寫入」＝ `cache_creation_input_tokens`，「讀取」＝ `cached_input_tokens`，「輸出」＝ `output_tokens`（含 thinking）。

## 1. 結論先講

1. **成本只有兩個大項**：寫入約 56%、輸出約 42%、讀取約 2%（第 2.2 節）。讀取幾乎不用錢，所以「讓 Agent 少翻幾次檔」省不到錢；要省就是少寫入（少送、或讓第二個人讀到快取）與少輸出。
2. **最大宗是重複判斷同一份輸入**。事件分析師每晚重判約 1,600 則事件，其中 97～99% 與前一次是同一則；
   以「逐檔事件與新聞完全相同」計，晚間 run 有 38～64% 的事件量可以直接沿用前一次已驗證的結果（第 4.1 節）。這一項單獨就值 $3～5.5／晚間 run，但屬於跨交易日沿用，須使用者決定。
3. **同一份輸入重跑，結論本來就會變**：封存 run 中有 2,405 檔次是「輸入逐檔相同卻被判了兩次」，整檔看法一致率 90.3%；逐則重大性一致率 89.6%，其中 high 只有 68.5%（第 2.5 節）。
   這是所有 A/B 的雜訊底線：任何縮減方案只要差異不高於這個底線，就無法用單次對照證明它變差或沒變差。
4. **全市場新聞標註從 11/16 起變貴**（60 則 → 429 則，$0.22 → $1.56～1.82），而連續兩次 run 有 418／429 則相同。逐則標籤只跟該則內容有關，可以確定性沿用（第 4.2 節），風險低。
5. **多頭與空頭的 brief 除了 `role` 與 `packet_envelope` 之外逐位元相同**（已驗證 3 批）。理論上第二個人可以讀第一個人寫的快取，但現行「用 Read 工具讀檔」的流程做不到；
   可行的路只有把共同輸入放進 system prompt，而這條路的快取行為官方文件沒有寫明，要先做一次小實驗（第 3 節、第 4.5 節）。
6. **批次大小、批次排序、TTL、並行度都不是省錢槓桿**（第 4.9 節）：每次呼叫的固定開銷只有約 $0.03，批次專屬資料沒有共同前綴，5 分鐘 TTL 已是正確設定。
7. 全部做完的粗估：晚間完整 run 由約 $19 降到 $9～13；其中不需要使用者做原則決定、風險低的部分約 $3～5。

## 2. 成本模型（實測）

### 2.1 單價

以 `agent_calls` 的 token 數乘下列單價，四個 run 的加總與 `agent_estimated_usage_usd` 完全相同（誤差 $0.00），所以單價是對的：

| 模型別名 | 輸入 | 寫入（5 分鐘） | 讀取 | 輸出 |
| --- | --- | --- | --- | --- |
| `sonnet`（分析師、多空、新聞標註） | $2 | $2.50 | $0.10 | $10 |
| `opus`（交易、排序、提名、現金姿態、風險審查） | $4 | $5 | $0.20 | $20 |

每百萬 token。與官方定價頁的 Claude Sonnet 5.5、Claude Opus 5.5 一致（<https://platform.claude.com/docs/en/about-claude/pricing>）；
別名實際解析到哪個模型本計畫沒有直接證據，只是價格吻合。同頁列出 Claude Haiku 5.5 為 $0.10／$0.125／$0.01／$0.50，**但 prompt 超過 10 萬 token 時為 5 倍**（$0.50／$0.625／$0.05／$2.50）。

`claude -p` 用訂閱登入時，這些美元是 Claude Code 在本機依公開定價算的估計值，不是帳單（<https://code.claude.com/docs/en/costs>）；實際消耗的是訂閱額度。
過去兩週有 3 個 run 因 5 小時額度用完而失敗，所以 token 降下來同時也降低撞額度的機率。

### 2.2 各階段花費

| 階段（模型） | 11/15 22:01 完整 | 11/16 22:20 失敗 | 11/16 23:01 重跑 | 11/13 07:11 早上 |
| --- | --- | --- | --- | --- |
| 事件分析師（sonnet） | $8.57（27 次） | $8.19（25 次） | 全部沿用，$0 | $7.85（29 次） |
| 交易 Agent（opus，3 批） | $2.77 | $2.64 | $3.38（含重試 $0.75） | $2.97 |
| 空頭（sonnet） | $1.60 | $2.08（含重試 $0.49） | $1.77 | $1.90 |
| 多頭（sonnet） | $1.54 | $1.53 | $1.54 | $1.62 |
| 技術分析師（sonnet） | $1.41 | $1.05 | $1.37 | $1.35 |
| 基本面分析師（sonnet） | $1.33 | $1.32 | $1.24 | $1.28 |
| 全市場新聞標註（sonnet） | $0.22（60 則） | $1.82（448 則） | $1.56（429 則，含重試 $0.50） | $0.31 |
| 排序、提名、釋出順序、現金姿態、風險審查、優質股裁決（opus） | $1.43 | $0.68（未跑完） | $1.16 | $1.32 |
| **合計** | **$18.86** | **$19.31** | **$12.01** | **$18.61** |
| 其中寫入／讀取／輸出 | $10.56／$0.32／$7.98 | $10.37／$0.58／$8.37 | $6.34／$0.35／$5.32 | $10.20／$0.36／$8.04 |

（來源：`artifacts/daily/<日期>/<run>/pipeline.json` 的 `agent_calls`。）

### 2.3 成本結構的四個事實

1. **寫入量大約就是 brief 被讀進去一次的量**。以三個 run 的首次呼叫回歸：事件每字元 0.79 token、新聞 0.83、多空 0.51～0.53、交易 0.49、技術 0.46、基本面 0.33，
   另加每次呼叫 1～3.5 萬 token 的固定部分（Skill 與 references、提示、Schema、前幾輪的輸出）。中文多的 brief 每字元較貴。
2. **輸出裡 thinking 只占一小部分**。依 `var/logs/agent_trace/` 的 `thinking_tokens` 事件粗估（估計值、解析方式是本計畫自己寫的）：事件 27～30%、多空 20～32%、交易 17～22%、技術與基本面 6～12%。
   其餘是結構化輸出本身。所以壓 thinking 的上限約是總成本的 10%，更大的是輸出內容的結構。
3. **事件輸出有約三分之一是重複的鍵名與空陣列**。11/16 的 1,632 則事件中 low 1,118 則、medium 491 則、high 23 則；每則固定輸出 7 個鍵，
   low 事件的 `counter_evidence`、`pending_confirmations`、`corroborated_by` 依提示一律是空陣列。
4. **交易 Agent 的輸出有 47% 花在 `no_trade`**（198 個項目中 141 個、50,713／107,732 字元，含一次重試），而且用的是最貴的模型。

### 2.4 失敗與重試

- 11 月 44 個 run 合計 $680.2，其中**未完成的 run 花了 $52.0（7.6%）**：11/03 $9.01 與 11/08 $14.29（額度用完）、11/12 08:23 $9.38（事件一批驗證兩次都失敗）、11/16 22:20 $19.31（「新聞來源回應重複」，是程式重建錯誤，不是 Agent 錯誤）。
- 11/10 之後 `attempt > 1` 的呼叫花了 $18.0／$295.8（6.1%）。現行重試是整批重做：整份 brief 重新寫入、整批重新輸出。
- 續跑（`--resume`）只在同一個 run 目錄內沿用已驗證輸出。11/16 23:01 是新 run，所以只有事件沿用（跨 run、同一交易日資料夾）生效，技術、基本面、多空、交易、新聞全部重做，$12.01。

### 2.5 重跑變異（品質對照的雜訊底線）

從封存 run 取「連續兩次 run、事件與新聞輸入逐檔完全相同、且兩次都由 Agent 重新判斷」的股票（共 23 組 run、2,405 檔次、18,625 則事件）：

| 項目 | 一致率 |
| --- | --- |
| 整檔 `outlook`／`event_outlook` | 90.3%（2,171／2,405） |
| 逐則 `materiality` | 89.6%（16,681／18,625） |
| 其中前次為 low | 94.3%（11,191／11,873） |
| 其中前次為 medium | 83.0%（4,986／6,009） |
| 其中前次為 high | **68.5%（503／734）** |
| 逐則 `direction`（有此欄位的 run） | 92.5%（4,907／5,302） |

變動幾乎都在相鄰等級之間（medium→low 802、low→medium 674、high→medium 231、medium→high 219，low→high 只有 1）。
技術分析師在輸入逐檔相同時 119／119 一致；基本面 90／97（92.8%）。樣本來自不同日期的 run，模型版本與提示在期間內改過，所以這是上界偏寬的估計。

### 2.6 前 5 大可省項目

| 排名 | 項目 | 預估省／run | 品質風險 | 節 |
| --- | --- | --- | --- | --- |
| 1 | 事件分析沿用擴大（跨交易日逐檔沿用、同日比對放寬） | 晚間 $3.1～5.5（無新事件日最多 $8.2）；早上 $0.7～4 | 中，須使用者決定 | 4.1 |
| 2 | 輸出側：驗證失敗只重做失敗股票、事件空陣列選填、effort 降級 | $1.2～2.7 | 前兩項低、effort 中 | 4.3、4.4 |
| 3 | brief 瘦身剩餘項目（事件財報脈絡、證據種類、low 事件索引、技術與基本面欄位表、交易 dip） | $1.0～1.6 | 低～中 | 4.7 |
| 4 | 全市場新聞增量標註 | $0.9～1.2 | 低 | 4.2 |
| 5 | 多空共同輸入與 Skill 放進可快取前綴 | $0.8～1.3 | 品質風險低；**可行性未驗證** | 4.5 |
| 另計 | 失敗前移檢查、同日重跑沿用 | 只在失敗日省 $4～19 | 低 | 4.6 |

各項不能直接相加（例如事件沿用後，事件 brief 瘦身能省的也跟著變少）。

## 3. `claude -p` 能控制快取到什麼程度

現行指令在 `src/etf_agent/automation/agent_runner.py` 的 `ClaudeAgentRunner.run`，環境變數 `CLAUDE_CODE_PROMPT_CACHE_TTL=5m` 在 `_run_claude_streaming`。

| 問題 | 查到的事實 | 來源 |
| --- | --- | --- |
| `--exclude-dynamic-system-prompt-sections` 存在嗎 | 存在。本機 2.1.325 的 `--help`：把 cwd、環境資訊、memory 路徑、git status 從 system prompt 移到第一則 user 訊息，「Improves cross-user prompt-cache reuse」，只在預設 system prompt 下有效 | `claude --help`；<https://code.claude.com/docs/en/cli-reference> |
| 對我們有用嗎 | 官方說明的目的是跨使用者、跨機器。同一目錄平行啟動的 session 本來就建立相同前綴；先後啟動的 session 只有在啟動時的 git status 快照相同時才共用。我們同機、同目錄，預期幫助很小 | <https://code.claude.com/docs/en/prompt-caching> |
| TTL 可控嗎 | `CLAUDE_CODE_PROMPT_CACHE_TTL` 只接受 `5m`、`1h`。訂閱方案內主對話預設 1 小時，所以現行改成 5 分鐘是有作用的 | <https://code.claude.com/docs/en/env-vars>、prompt-caching 頁 |
| 快取分幾層 | system prompt（核心指示與工具定義）→ 專案脈絡 → 對話。system 層任何變動會讓後面全部失效；「載入的工具定義集合改變」也會 | prompt-caching 頁 |
| `--json-schema` 怎麼送出 | Claude Code 把它變成一個名為 `StructuredOutput` 的工具加進 session。工具定義屬於 system 層，所以**逐批不同的 Schema 推論上會讓 system 層起就不命中**；官方沒有直接寫 | <https://code.claude.com/docs/en/agent-sdk/structured-outputs> |
| 能附加 system prompt 嗎 | `--append-system-prompt`、`--append-system-prompt-file`、`--system-prompt`、`--system-prompt-file` 在 `-p` 模式都可用（後兩個 file 版本不在 `--help` 列表，解析器接受） | cli-reference、<https://code.claude.com/docs/en/headless> |
| 附加的文字會被快取嗎 | 依層級規則屬於 system 層，逐位元相同就應該命中。**官方沒有寫 `--append-system-prompt` 的文字相對於快取斷點的位置**，這是推論 | <https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts> |
| 同時啟動的呼叫能互相讀快取嗎 | API 層：快取項目要等第一個回應開始串流後才可讀，N 個同時送出、前綴相同的請求全部付寫入價。Claude Code 文件只對 workflow 的扇出寫了延遲啟動（`CLAUDE_CODE_WORKFLOW_PREFIX_STAGGER_MS`），沒有寫獨立 `-p` | <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>；env-vars 頁 |
| 能從預熱的 session 分岔嗎 | `--resume` 加 `--fork-session` 存在，恢復時會讀仍在有效期內、未變動的前綴。但官方沒有寫「多個 `-p` 平行分岔共用前綴」；且恢復後的 `total_cost_usd` 含先前花費，直接加總會重複計算 | prompt-caching 頁、<https://code.claude.com/docs/en/agent-sdk/cost-tracking> |
| thinking 能關嗎 | Opus 5.5、Sonnet 5.5、Haiku 5.5 不能關；`MAX_THINKING_TOKENS` 對 adaptive 模型無效，只能用 `--effort`（low／medium／high／xhigh／max）。Claude Code 對 Opus 5.5、Sonnet 5.5、Haiku 5.5 的預設是 `medium` | costs 頁、<https://code.claude.com/docs/en/model-config> |
| Read 單次上限 | 預設 25,000 token（`CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS` 可調），整檔超過 256 KB 會被拒；2.1.296 起 Read 有 `allow_large` 參數可一次讀完 | <https://code.claude.com/docs/en/tools-reference> |
| 可以不用 Read、改 stdin 嗎 | 可以，`cat 檔 \| claude -p '…'`，上限 10 MB，內容進 user 訊息。官方沒有比較兩種做法的 token 或快取差異 | headless 頁 |
| `--bare` | 少載入 hooks、auto memory、CLAUDE.md 探索等，但**只接受 API key，不讀訂閱登入**，所以現況不能用 | headless 頁 |

對本專案的含意：

- **現況能跨呼叫共用的只有 Claude Code 自己的固定開頭**。每次呼叫的第一則 user 訊息就含批次專屬的路徑與股票清單，之後的內容（Skill、brief）都排在它後面，不可能命中別的呼叫寫的快取。
- **「批次排序」與「固定內容放前面」在現行流程沒有作用點**：brief 是對話中途用 Read 讀進來的，JSON 內鍵的順序不影響跨呼叫命中。
- **唯一能讓不同呼叫共用大段內容的位置是 system prompt**（第 4.5 節）。它是否真的命中，要用 `OTEL_LOG_RAW_API_BODIES=file:<目錄>` 記下實際請求（modifying-system-prompts 頁「See what Claude received」）加上 `usage` 的讀取數字確認。
- 事件分析師的 Schema 目前逐批不同（`event_analyst_schema` 帶入 `_batch_corroboration_ids`）。若實驗證實它讓 system 層不命中，要把這個列舉移出 Schema、改由 Validator 檢查（Validator 本來就檢查）。

## 4. 候選措施

每項的「驗證」都分兩層：**離線**（不呼叫 Agent、用封存 run 重算，可證明覆蓋與可重建，不能證明判斷品質）與 **A/B**（呼叫 Agent，對照第 2.5 節的雜訊底線）。

### 4.1 事件分析沿用擴大（最大宗）

現況：`automation/event_reuse.py` 只在**同一交易日資料夾**內、逐檔簡報列雜湊完全相同時沿用，並用本次 bundle 與 `AnalystReportValidator` 重驗。

量測（完整簡報的 run，11/02～11/11）：

| 方向 | 逐則事件與前次相同 | 「事件與新聞逐檔相同」的股票 | 這些股票占的事件量 |
| --- | --- | --- | --- |
| 早上 → 當晚（跨交易日） | 97～99% | 61～91／128～146 檔 | 38～64%（452／894 ～ 773／1,214） |
| 晚上 → 隔天早上（同日，現行已沿用） | 接近 100% | 現行整列相同 87～118 檔；只比事件與新聞則為 104～141 檔 | — |
| 11/15 22:01 → 11/16 22:20 | 100% | 149／149 檔 | 100%，但因跨日沒有沿用，花了 $8.19 |

三個子項，風險遞增：

**(a) 同日比對放寬**：比對時不看 `data_availability` 的筆數與時間（現行已排除 `status`），並把 `financial_context` 也移出比對。

- 預估省：早上 run 多沿用 11～28 檔（約 $0.7～1.6）；財報季更多（11/12 早上未沿用的 72 檔中有 44 檔事件與新聞相同，約 $4）。
- 品質風險：`data_availability` 部分很低（不是判斷依據）。`financial_context` 部分低～中：沿用的判斷是用舊一季的財務規模做的；引用的財報編號若已不在本次 bundle，重驗會失敗而整批不沿用（fail-closed）。
- 驗證：離線重算每組相鄰 run 的可沿用檔數；沿用項目全數通過本次 bundle 的 Validator。A/B 不需要另跑——比對本來就會重判的那些股票即可（見第 7 節的免費對照）。

**(b) 跨交易日逐檔沿用**：事件與新聞逐檔相同時，沿用前一個已完成 run 的結果，不限同一交易日。

- 預估省：晚間 run $3.1～5.5；沒有新事件的日子最多 $8.2。
- 品質風險：中。輸入相同時重判的期望品質並不比沿用高（第 2.5 節），但沿用會把一次抽樣的結果固定下來：一則被誤判成 low 的事件會連續多天維持 low，不再有重抽的機會。建議的緩解：
  1. 沿用天數上限（例如同一判斷最多沿用 3 個交易日）。
  2. 持股與前次含 high 事件的股票一律重判（high 的重跑一致率只有 68.5%，最不該固定）。
  3. 每次隨機重判約 10% 的可沿用股票，把「沿用值 vs 重判值」的差異率寫進 `pipeline.json`，當作長期監測。
- 時間一致性：沿用的是「更早的截止時間、對同一批事實」做的判斷，不會帶入未來資訊；報告信封仍用本次 bundle，並經本次 Validator 重驗。
- 這違反現行「只在同一交易日沿用」的做法，**是原則變更，須使用者決定**（`context_slimming_plan.md` 4.6 已提過同一問題）。

**(c) 逐則沿用**（同一則事件不論同檔有沒有新事件都沿用）：可再多省，但要改每批的即時驗證、Agent 要看得到舊事件摘要才能寫整檔 `outlook`，等於改契約。**本計畫不建議在比賽期間做**。

共同事項：

- 契約與版本閘門：`analyst_event.json` 的形狀不變；沿用來源照現行另存 `event_reuse.json`，不進報告本體，不升版。沒有任何 Validator 檢查項目是誰產生的，所以舊 run 重建不受影響。
  開關比照現行 `event_reuse`（CLI 參數、記入 `pipeline.json`），預設維持現行行為；(b) 若使用者認為屬於策略行為，改走 Policy 版本閘門。
- 測試：比對放寬後「任何事件或新聞欄位不同仍不沿用」、沿用項目引用不存在即整批不沿用、超過天數上限不沿用、來源 run 未完成或毀損不沿用、開關關閉時逐位元同現行。
- 檔案：`src/etf_agent/automation/event_reuse.py`、`team_stages.py` 的 `_reusable_event_items`、`tests/` 對應測試、`docs/token_usage.md`。

### 4.2 全市場新聞增量標註（已實作，未 commit）

現況：`automation/market_news_stage.py` 每次把視窗內全部項目交給 Agent 逐則標註。11/16 加入 Yahoo 來源、視窗改 4 天、合併多次抓取後，項目由 60 則變成 429～448 則；11/16 22:20 → 23:01 有 418／429 則相同。

做法：

1. 逐則標籤（`relevance`、`stance`、`reason`）只描述該則內容。前一個已完成 run 的 `market_news_bundle.json` 中，`item_id` 相同且通過當時驗證的標籤由程式沿用。
2. Agent 只標新項目；簡報另附「已沿用且 `relevance=market`」項目的標題與既有標籤，供寫 `synthesis` 引用。非 market 的沿用項目不必給 Agent 看。
3. 程式合併新舊標籤後，照現行 `evaluate(build_market_news_pack(...))` 驗證全量覆蓋。
4. 另一個小項：給 Agent 看短代號（`n1`…），由程式對回 `item_id`，輸出約少 10%。現行 `_restore_content_ids` 已是同一種做法。

- 預估省：$0.9～1.2／run（輸出 $0.8 中約七成、寫入 $0.6～0.8 中約五到七成）。相鄰 run 的重疊率目前只有一組新口徑的資料（97%），晚上到早上的重疊率是推估，沒有量到。
- 品質風險：低。資訊沒有減少；風險是沿用的標籤若當初標錯會留到該則離開視窗（最多 4 天）。`synthesis` 每次仍由 Agent 重寫。
- 「情緒標籤必須覆蓋查詢視窗內全部項目」仍成立（沿用加新標）。
- 驗證：離線以封存的 bundle 重建，確認合併後的標籤全量覆蓋、`evaluate` 通過；A/B 比較 `outlook` 與 `market_unique_count`。第一次驗證失敗率（11/16 23:01 第一次交件就因「引用不存在或重複」被退，$1.05）應下降。
- 契約：資料包形狀不變，沿用來源另存 `market_news_label_reuse.json`，不升版。開關比照 `event_reuse`。
- 檔案：`src/etf_agent/automation/market_news_stage.py`、對應測試。

### 4.3 驗證失敗只重做失敗的股票

現況：`pipeline_base.py` 的 `agent_step` 驗證失敗後帶回饋整批重做。最常見的失敗是 `claim_kind`／`finding_kind` 與證據種類不對映、引用到別檔股票，通常只涉及 1～2 檔。

做法：驗證錯誤能歸到特定股票時（錯誤訊息已有 `items[n]`），第二次只要求重交那幾檔，其餘沿用第一次已通過逐檔驗證的項目，由程式合併後再整批驗證一次。不能歸到單一股票的錯誤（例如 `finding_id` 跨股票重複）照舊整批重做。

- 預估省：$0.5～0.9／run（重試成本約占 6%，可省其中大半）。另外降低整個 run 因單批兩次失敗而報廢的機率（11/12 08:23，$9.38）。
- 品質風險：低。未出錯的股票不再因重試而被改寫，`revision_diff` 要稽核的「因驗證回饋變積極」反而變少。
- 契約：重試次數上限、fail-closed 都不變；每次原始輸出照舊另存。交易 Agent「每個 claim 剛好處理一次」是逐檔可驗的，適用；`batch` 層級的欄位若有則整批重做。
- 測試：部分重交的合併結果等於逐檔聯集、重交項目含非指定股票即拒絕、第二次仍失敗即停止。
- 檔案：`src/etf_agent/automation/pipeline_base.py`（`agent_step`）、`batching.py`、`team_stages.py` 各 Validator 的建立處。

### 4.4 輸出 token 控制

| 項目 | 做法 | 預估省／run | 風險 |
| --- | --- | --- | --- |
| 事件空陣列選填 | 交件 Schema 中 `counter_evidence`、`pending_confirmations`、`corroborated_by` 改為選填，缺欄位由程式在封存前補 `[]`；封存的報告形狀不變 | $0.25～0.3 | 低：medium／high 可能因此少寫反證，要比較這兩欄的非空比例 |
| effort 降一級 | 分析師與新聞標註加 `--effort low`；先用 `OTEL_LOG_RAW_API_BODIES` 確認現在實際送出的是哪一級（Claude Code 對 5.5 系列預設 `medium`） | $0.5～1.5，很不確定 | 中：官方對 `low` 的描述是「Significant token savings with some capability reduction」 |
| 交易 Agent 的 `no_trade` 精簡 | 提示加長度指引（例如理由 60 字內、未解問題最多 1 條） | $0.15～0.3 | 低～中：要先確認 `prior_decision_context` 與論點檢視是否讀 `no_trade` 的理由 |
| 事件分析師不寫 financial 發現 | 見 `context_slimming_plan.md` 4.2 | 約 $0.1 加下游輸入 | 中：改提示會動到判斷 |

- effort 一定要 A/B，不能只看驗證通過率。交易、現金姿態、風險審查**不降**。
- 檔案：`src/etf_agent/automation/agent_schemas.py`、`agent_runner.py`（新增 effort 參數、記入 `agent_calls`）、`team_stages.py` 的提示、`tradeagent/` 對應 Skill。

### 4.5 多空共同輸入與 Skill 放進可快取前綴

事實：`brief_bull_b{0,1,2}.json` 與 `brief_bear_b{0,1,2}.json` 只有 `role` 與 `packet_envelope` 不同（共同輸入本來就是設計，`shared_input_sha256`）。每批約 18.5 萬字元、寫入約 11 萬 token，兩方各付一次。

做法（**要先通過階段 0 的實驗才值得做**）：

1. 把每批的共同輸入（不含 `role`、`packet_envelope`）寫成一個檔，用 `--append-system-prompt-file` 附加；角色、信封與批次指示留在 user 提示。
2. 同一批先啟動一方，等它的串流出現第一個事件（`_run_claude_streaming` 已逐行讀事件）再啟動另一方，5 分鐘內即可。
3. 各角色的 `SKILL.md` 與 references 同樣放進附加的 system prompt，同角色各批共用。

- 預估省：多空 $0.7～0.8（3 批 × 約 10.5 萬 token × $2.4／百萬）；Skill 前綴 $0.3～0.5（事件 23 批最多）。合計 $0.8～1.3。
- 品質風險：低——兩方看到的內容與現在相同，只是位置從工具結果變成 system prompt。位置改變本身可能影響模型行為，要 A/B。
- **隔離**：`peer_packet_ids` 仍為空；對方的輸出與簡報檔仍由 `deny_reads` 擋住。伺服器端快取以內容為鍵，不會把一方的輸出帶給另一方。Codex 的時序隔離不受影響（這項只適用 Claude runner）。
- **安全審查必做**：共同輸入含分析師依外部公告與新聞寫的文字。放進 system prompt 等於提高它的位階，與「不受信任的資料不能改變 Agent 指令」有張力。要有明確的資料區塊包裹與說明，並經安全審查；審查不過就只做 Skill 前綴（Skill 是專案自己的受信任文字）。
- 契約：不動。brief 不進任何雜湊，`skill_sha256` 照舊記錄；`prompt_sha256` 的計算範圍要把附加檔納入。
- 不確定：附加文字是否落在快取斷點之前、Schema 工具是否破壞前綴、兩個獨立 `-p` 能否互讀——三點官方文件都沒寫，全靠實驗。
- 檔案：`src/etf_agent/automation/agent_runner.py`（`AgentTask` 增加可選的 system 附加檔）、`team_stages.py`（`_stance_batch_jobs`、`run_research_team` 的啟動順序）、`pipeline_base.py`（`_run_jobs`）。

### 4.6 失敗前移與同日重跑沿用

1. **把不依賴 Agent 輸出的重建檢查前移**。11/16 22:20 的錯誤「新聞來源回應重複」發生在交易階段之後，$19.31 全數作廢。新聞資料包、證據時效、基準事實、持股檢視、基本面指標這些只吃資料的重建，在分析師階段開始前先跑一次即可提早失敗。零 token。
   - 檔案：`src/etf_agent/automation/daily_pipeline.py`；測試以壞資料包確認在第一個 Agent 呼叫前停止。
2. **技術與基本面的逐檔沿用**（比照事件）。量測：輸入逐檔相同時技術看法 119／119 一致。但目前逐檔相同的比例很低——技術面晚上到早上多為 0／150，差異在 `trend_history`、`momentum` 等欄位；
   基本面每次有 132 檔的 `cash_flow` 不同。**差異來源本計畫沒有查**（可能是時間戳，也可能是晚間與早上的價格來源不同）。若只是時間戳，移出比對後每個早上與每次重跑可省 $1～2.5；若是真的數值差異就不能沿用。列為待查，不估入總數。
3. 多空與交易在新 run 不沿用：共同輸入含每次重寫的市場情緒，雜湊必然不同。

### 4.7 brief 瘦身剩餘項目（承接 `context_slimming_plan.md`）

已完成：公告本文結構化抽取（`items_v1`）、`data_availability` 說明移到頂層、空陣列省略、新聞標註 brief 壓縮、5 分鐘 TTL、現金姿態簡報 v2；技術 `trend_history` 去掉每檔重複的雜湊與每列 `available_at`（未 commit）。

尚未做（占比為 11/16 兩個 run 的實測）：

| 項目 | 現況占比 | 做法 | 預估省／run | 風險 | 檔案 |
| --- | --- | --- | --- | --- | --- |
| 事件 `financial_context` | 事件 brief 的 11.9% | 每份財報一行，幣別與單位頂層說明一次，與預設不同才逐檔寫 | 約 $0.4 | 低，須可還原 | `decision/analysts.py`、`team_stages.py` 的 `_slim_event_batch` |
| 多空 `evidence_kinds` | 多空 brief 的 20.3% | 事件編號不列（由所在欄位可知種類），其餘依種類分組 | 約 $0.2 | 低；看 `claim_kind` 退回率 | `decision/stance.py`（`build_stance_brief`） |
| 多空的 low 事件 | 21.5%（1,118 則） | 主簡報只留索引，全文放同批附錄檔 | 約 $0.45 | 中：每 run 約 2 個只靠 low 事件的空頭論點可能消失 | 同上 |
| 技術 `trend_history` 列 | 技術 brief 的 66.7% | 表頭一次、每列只有值 | 約 $0.1～0.2 | 低～中 | `decision/analysts.py` |
| 基本面 `metrics`、`cash_flow`、`fundamental_flags` | 62.5% | 固定說明放頂層、空清單省略 | 約 $0.1～0.2 | 低 | 同上 |
| 交易 `dip` | 交易 brief 的 37.7% | 空清單與 false 省略、`candidate=none` 只留必要欄、與 `position_review` 重複的報酬欄只留一份 | 約 $0.15～0.3 | 中：最後整合關、最貴的模型 | `decision/` 的 `dip_background_brief`、`team_stages.py` |
| 事件 brief 頂層 `market_sentiment` | 2.9%（23 批各一份） | 逐批只需要判讀公司事件，可只留 `outlook` | 約 $0.1 | 低～中 | `team_stages.py` |

原則不變：去重必須可由程式還原成原簡報；Validator 與 Finalizer 不讀簡報，舊 run 重建不受影響；會改變 Agent 可見範圍的（low 事件索引、交易 dip）走 Policy 版本閘門並預設不開。

### 4.8 模型分級

| 階段 | 現行 | 建議 | 理由與風險 |
| --- | --- | --- | --- |
| 交易、排序、換股提名、釋出順序、現金姿態、風險審查 | opus | **不降** | 最後整合與風控；一次錯誤直接變成委託 |
| 多頭、空頭 | sonnet | **不降** | claims 是交易 Agent 唯一能採納的依據；也不升（實測 opus $7.3 對 sonnet $2.9） |
| 事件分析師 | sonnet | 不直接換；可做影子比對 | 重大性判斷是整條鏈的入口，high 的重跑一致率已經偏低 |
| 基本面分析師 | sonnet | 不降 | 要解讀比率與資料缺口 |
| 技術分析師 | sonnet | 可評估 haiku（影子） | 指標由程式算好，輸入相同時 119／119 一致，任務接近讀表。注意 brief 約 10 萬 token，超過 Haiku 5.5 的長 prompt 門檻會變 5 倍價，要先瘦身或切 4 批 |
| 全市場新聞逐則標註 | sonnet | 可評估 haiku（影子） | 四選一分類加一句理由，有程式全量驗證；`synthesis` 仍可留給 sonnet。風險在全市場情緒會限制現金姿態（負面時不得 `aggressive`） |

- 影子比對的成本很低（Haiku 5.5 單價是 sonnet 的二十分之一），可以每天多跑一份、不進決策，累積 10 個決策日再看一致率是否不低於 sonnet 自己的重跑一致率。
- `haiku` 別名在本機解析到哪個模型沒有確認；官方建議批次作業直接寫完整模型 ID（model-config 頁）。
- 文獻提醒：串接與路由的省錢效果高度依任務而定，且需要同分佈的標註樣本（第 5.5 節）。本專案沒有「正確答案」可訓練路由器，只能用一致率當代理指標。

### 4.9 評估後不建議當成省錢手段的項目

| 項目 | 結論 | 依據 |
| --- | --- | --- |
| 事件批次放大 | 不做 | 每次呼叫的固定開銷約 1.2 萬 token 寫入加 1.8 萬讀取，約 $0.03；23 批併成 12 批只省約 $0.3，而 50 檔一批時曾出現只做前 12 檔的失敗（`agent_schemas.py` 註解）。長輸入也有準確度風險（第 5.1 節） |
| 事件批次縮小 | 不做 | 每多一批多約 $0.03，無品質證據 |
| 批次排序、JSON 鍵順序 | 無作用點 | 見第 3 節 |
| 改回 1 小時 TTL | 不做 | 寫入單價 2 倍；批次專屬內容不會在 5 分鐘後再被讀。若第 4.5 節上線，同批兩方相隔數秒啟動，5 分鐘足夠 |
| 降低並行度 | 現況不需要 | 各批沒有共同前綴可搶。第 4.5 節上線後只需「同前綴的第一個先跑」，不必整體降並行 |
| 改用 stdin 或 `allow_large` 取代分頁 Read | 可做但不省錢 | 讀取只占 2%。好處是少幾輪往返與讀檔失敗（事件每次呼叫約 2.6 次讀簡報），屬於延遲與穩定性改善 |
| `--bare` | 不能用 | 只接受 API key |
| Message Batches API（五折） | 不做 | 要直接串 API，專案規則禁止；且為非同步，趕不上交件時間 |
| LLMLingua 類壓縮 | 不做 | 見第 5.2 節 |
| 合併多空為一次呼叫 | 不做 | 破壞隔離 |

## 5. 文獻依據

查證等級：**全文**＝讀過論文全文、數字出自原表；**摘要**＝只讀 arXiv 摘要頁；**轉述**＝網頁經摘要工具轉述，引用原句前要再對一次。
這些研究的模型多半較舊、任務也不是金融判讀，只能當方向性的依據，不能當成本專案的品質證明。

### 5.1 長輸入的準確度衰退

- Liu 等，*Lost in the Middle*（2023，全文）：多文件問答呈 U 形，相關文件在中段時 GPT-3.5-Turbo 的表現「can drop by more than 20%」，最差時低於不給文件（56.1%）。<https://arxiv.org/abs/2307.03172>
- Levy 等，*Same Task, More Tokens*（ACL 2024，全文）：同一題只加 padding，輸入到 3,000 token 時平均準確度由 0.92 掉到 0.68；與任務無關的 padding 傷害較大。<https://arxiv.org/abs/2402.14848>
- Modarressi 等，*NoLiMa*（ICML 2025，全文）：13 個模型中 11 個在 32K 時掉到基準的一半以下；Claude 3.5 Sonnet 由 87.5 降到 29.8。題目刻意設計成沒有字面重疊。<https://arxiv.org/abs/2502.05167>
- Hong 等，*Context Rot*（Chroma 2025，廠商報告，轉述）：18 個模型輸入越長越不可靠；但此報告說目標位置沒有明顯影響，與上面第一篇不一致。<https://www.trychroma.com/research/context-rot>

對本計畫的用處：支持「不放大批次」與「刪掉與判斷無關的內容不會傷品質、可能有幫助」。**不能**用來證明某個具體欄位可以刪——那要靠本專案自己的引用紀錄與 A/B。

### 5.2 Prompt 壓縮

- LLMLingua（EMNLP 2023，全文）：GSM8K 壓 20 倍幾乎不掉分（78.85 → 77.33），但那是壓縮重複性很高的示範題；BBH 壓 5 倍就由 70.07 掉到 61.60。<https://arxiv.org/abs/2310.05736>
- LongLLMLingua（ACL 2024，全文）：4 倍壓縮下反而改善位置效應，但必須先知道問題才能壓。<https://arxiv.org/abs/2310.06839>
- LLMLingua-2（2024，全文）：純抽取式，MeetingBank 問答 3.1 倍壓縮 87.75 → 86.92。<https://arxiv.org/abs/2403.12968>

三篇都**沒有量測識別碼與數字的保留率**；LLMLingua-2 附錄的範例把「1 + 1 + 1 + 1 + 1 + 1 = 6」壓成「1 + 1 = 6」。
本專案每個主張都要追溯到 `evidence_id`、數字不得改寫，所以這類有損壓縮不適用；能做的只有程式可還原的去重（第 4.7 節）。

### 5.3 結構化資料的表示方式

- Sui 等，*Table Meets LLM*（2023，全文）：不同序列化格式的準確度差 1～5 個百分點，沒有一種格式每個任務都贏；論文沒有報告 token 數。<https://arxiv.org/abs/2305.13062>
- He 等（2024，預印本，全文）：同一任務換 prompt 格式，GPT-3.5 與 GPT-4 的偏好相反。<https://arxiv.org/abs/2411.10541>
- TOON 專案 README（專案自測，轉述）：精簡 JSON 與 TOON、YAML、XML 的準確度差距都在誤差範圍內；純表格時 CSV 比 TOON 更小。<https://github.com/toon-format/toon>
- improvingagents 部落格（單一小模型，轉述）：CSV 最省 token 但準確度次差（44.3%，JSON 52.3%）。<https://www.improvingagents.com/blog/best-input-data-format-for-llms>

結論：同儕審查文獻只支持「格式會影響準確度、且因模型而異」。「欄位表更省又不掉分」目前只有互相矛盾的非正式基準。
所以第 4.7 節的技術與基本面欄位表**必須 A/B**，不能假設安全；現行每檔一行的精簡 JSON 已拿到大部分的 token 好處。

### 5.4 多 Agent 的成本

- Anthropic 工程部落格（2025，轉述）：多 Agent 系統用掉的 token 約為聊天的 15 倍、一般 Agent 約 4 倍。<https://www.anthropic.com/engineering/multi-agent-research-system>
- Claude Code 成本文件：Agent teams 的 token 用量「approximately 7x」於一般 session。<https://code.claude.com/docs/en/costs>
- Cemri 等，*Why Do Multi-Agent LLM Systems Fail?*（NeurIPS 2025）：7 個框架失敗率 41～86.7%；研究的是失敗原因，沒有量成本。<https://arxiv.org/abs/2503.13657>
- Tran 與 Kiela（2026，預印本，摘要）：推理 token 預算相同時，單 Agent 在多跳推理上不輸多 Agent。<https://arxiv.org/abs/2604.02460>

對本計畫的用處：多 Agent 天生較貴是預期內的事；本專案的分工（分析、多空隔離、交易、風險）是契約與稽核的要求，不在本計畫調整範圍。省錢要從「同一份輸入不要重複付費」下手。

### 5.5 模型分級與串接

- FrugalGPT（2023，全文）：金融新聞標題資料集上，串接便宜模型可用約 2% 的成本達到 GPT-4 的準確度；需要同分佈的標註樣本訓練評分器。<https://arxiv.org/abs/2305.05176>
- RouteLLM（ICLR 2025，全文）：節省幅度依任務差很多（MT Bench 3.66 倍、MMLU 1.41 倍且品質降到 92%）。<https://arxiv.org/abs/2406.18665>

### 5.6 重跑變異與金融文本分類

- Wang 與 Wang（2025，摘要）：金融與會計任務每個輸入跑 50 次，二元分類與情緒分析接近可重現，複雜任務變異較大；較新的模型不一定較穩定；3～5 次重跑聚合可大幅提升一致性。<https://arxiv.org/abs/2503.16974>
- Atil 等（2024，摘要）：理應確定性的設定下，重跑間準確度差距最高 15%。<https://arxiv.org/abs/2408.04667>
- Li 等（EMNLP 2023 Industry，全文）：黃金新聞標題分類，微調 BERT 的 F1 95.36 高於 GPT-4 五樣本的 86.00。<https://arxiv.org/abs/2305.05862>
- Vamvourellis 與 Mehta（2025，預印本，轉述）：金融情緒分類上推理越多與人工標籤越不一致（GPT-4o 不用逐步推理 0.895、o3-mini 0.797）。<https://arxiv.org/html/2506.04574v1>

對本計畫的用處：與第 2.5 節的實測一致——重跑變異是真實存在的雜訊。新聞逐則標註這類短文本分類，較小的模型或較低的 effort 未必較差，值得做影子比對。

### 5.7 thinking 與 effort

- Anthropic effort 文件：effort 影響文字、工具呼叫與 thinking 的全部 token；官方只有定性描述，沒有各等級的準確度或 token 數字，並要求在自己的評測上做 sweep。<https://platform.claude.com/docs/en/build-with-claude/effort>
- thinking 以輸出 token 計費，不論是否顯示。<https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost>

## 6. 建議的實作順序

| 階段 | 內容 | 驗收標準 | 回滾 |
| --- | --- | --- | --- |
| 0 量測（不改行為） | (1) `scripts/measure_daily_briefs.py` 加上本計畫第 2 節的重算（各階段寫入／讀取／輸出、重試成本、相鄰 run 的可沿用率、重判一致率）；(2) 以 `OTEL_LOG_RAW_API_BODIES` 記一次真實請求，確認 effort 等級、Schema 工具位置、system prompt 是否逐批相同；(3) 兩次小呼叫測 `--append-system-prompt-file` 的第二次是否讀到快取（預估花費 $0.5 以內） | 對 11/15、11/16 的 run 算出的數字與本文件一致；三個快取問題有明確答案並寫回本文件 | 無（只讀與本機實驗） |
| 1 零判斷風險 | 4.6-1 失敗前移；4.3 部分重試；4.2 新聞增量標註；4.1(a) 的 `data_availability` 部分 | 全部測試通過；開關關閉時簡報與輸出逐位元同現行；離線重建全量覆蓋；上線後 3 個 run 的首次驗證失敗率不上升 | 關 CLI 開關 |
| 2 可還原的去重 | 4.7 的事件財報脈絡、證據種類分組、基本面去重；4.4 事件空陣列選填 | 新舊簡報可由程式互轉且逐值相等；封存 run 的 finding／claim 引用的 `evidence_id` 100% 仍在；A/B 差異不高於第 2.5 節底線 | 關開關 |
| 3 需要 A/B 才能上 | 4.4 effort；4.7 的欄位表、low 事件索引、交易 dip；4.5 前綴快取（階段 0 證實可行且安全審查通過才做）；4.8 的 haiku 影子比對 | 第 7 節的 A/B 通過；haiku 累積 10 個決策日 | Policy 閘門刪欄位或關開關 |
| 4 原則決定 | 4.1(a) 的財報脈絡部分與 4.1(b) 跨交易日沿用 | 使用者同意後才開始；先以「只記錄、不沿用」跑 5 個交易日，確認沿用值與重判值的差異率不高於 2.5 節底線 | 關開關，回到同日沿用 |

每一步都照 AGENTS.md 跑完整 pytest、`compileall` 與 `git diff --check`；改到 Agent 輸入契約或 Skill 時同步更新 `docs/token_usage.md`、`docs/architecture.md` 與 Skill 的 `references/`。

## 7. 品質守門

### 7.1 能用封存 run 做的事

| 檢查 | 做法 | 能證明 | 不能證明 |
| --- | --- | --- | --- |
| 可重建 | 舊 run 以現行程式重跑 Finalizer 與各 Validator | 改動沒有破壞舊 run 的逐位元重建 | 任何關於判斷品質的事 |
| 引用保留率 | 用新程式對封存的 bundle 重建簡報，檢查歷史上被 finding／claim 引用過的每個 `evidence_id` 是否仍在簡報裡、屬於同一檔 | 縮減沒有拿掉「過去曾被用到」的證據 | 沒被引用過的內容是否曾影響判斷（背景欄位沒有編號，無從量起） |
| 可還原 | 新簡報由程式轉回舊簡報，逐值比對 | 去重沒有丟資訊 | 換了呈現方式後模型是否讀得一樣好 |
| 沿用的免費對照 | 對歷史上「輸入逐檔相同、卻被重判」的股票，比較「假如沿用前次」與「實際重判」的結果 | 沿用造成的差異率，以及它是否高於重跑變異（就是第 2.5 節） | 哪一個結果比較對 |
| 配置回放 | 固定封存的 Agent 意圖，重算配置 | 程式部分不受影響 | **LLM 會不會因為輸入改變而給出不同意圖**——回放正是把這個固定住了 |

### 7.2 需要呼叫 Agent 的 A/B

沿用 [`../token_usage.md`](../token_usage.md) 的驗收關卡，可用既有的 `cli/agent_eval.py`（`run --suite … --repeats 2 --stop-after analysts`）：

1. 同一份封存輸入跑三臂：現行、現行再跑一次（量當天的重跑變異）、新方案。同 Snapshot、截止時間、Policy、模型與執行器。
2. 比較：驗證退回率與 `revision_diff`、`outlook`／`materiality`／`direction` 一致率、多空 `strength` 與 claim 數、交易 `intent`／`conviction` 一致率、新增的 `unknown` 數、實際寫入與輸出 token。
3. 通過條件：新方案對現行的不一致率不高於兩次現行之間的不一致率；涉及 high 的每一則變動與每一個意圖改變逐條人工核對；不新增 `unknown`；退回率不上升；實際 token 降幅達估算的七成。
4. 至少兩個不同交易日；事件分析師抽 6 批即可。結果另存、不寫進既有 Decision run，標 `evidence_grade: replay_reference_only`。

### 7.3 誠實的限制

- 上面全部加起來，能證明的是「新方案與現行的差異不大於現行自己重跑的差異」。**不能證明決策品質沒有下降**，更不能證明報酬不受影響。
- high 事件的重跑一致率只有 68.5%，樣本又少（每個 run 約 20 多則），所以最重要的那一類事件，反而最難用 A/B 看出差別。這也是 4.1(b) 建議 high 一律重判的原因。
- 真正的證據只有比賽期間的即時紀錄。比賽 11/27 結束，剩下的決策日不足以累積 `calibration.MIN_DECISION_DAYS`（10 個完整決策日）的樣本去評斷任何一項改動對報酬的影響。
- 因此建議：比賽結束前只上階段 0～1（不改變 Agent 看到的判斷材料、或只沿用同一則內容的既有判斷），其餘在賽後或有餘裕時做。

## 8. 需要使用者決定

1. 是否接受事件分析跨交易日沿用（4.1b）；若接受，天數上限、是否排除持股與 high、抽樣重判比例。
2. 同日沿用比對是否把 `financial_context` 移出（4.1a 後半）。
3. 新聞增量標註是否上線（4.2）；沿用的標籤最多會留 4 天。
4. 是否允許把共同輸入放進 system prompt（4.5）——涉及安全審查；或只放 Skill。
5. 是否做 haiku 影子比對（每天多花約 $0.1～0.5），以及通過後是否真的切換。
6. effort 是否值得花 A/B 預算（估 $10～20）去試。
7. 階段 2 之後的項目要在比賽期間做，還是留到賽後。

## 9. 不確定與查不到的地方

- **訂閱額度的實際消耗方式**與本文的美元估計是否成比例：官方只說美元是依公開定價算的估計值，沒有說明額度怎麼扣。
- `sonnet`、`opus`、`haiku` 別名在本機解析到哪個模型：只由單價吻合推得，沒有直接證據。
- `claude -p` 多輪工具呼叫時，`usage` 的讀取數為何多數呼叫只有約 1.8 萬（看起來只讀了一次固定開頭）：本計畫沒有查明機制，只確認了成本加總正確。
- `--append-system-prompt` 的文字相對於快取斷點的位置、`StructuredOutput` 工具的 Schema 是否破壞前綴、兩個獨立 `-p` 能否互讀快取：官方文件都沒寫，待階段 0 實驗。
- `--exclude-dynamic-system-prompt-sections`：本機 `--help` 說會搬移 cwd、環境資訊、memory 路徑與 git status，官方文件只提 auto memory 位置等每使用者脈絡，兩者範圍不一致；對同機同目錄是否有效沒有文件依據。
- thinking 占輸出的比例是由 trace 的 `thinking_tokens` 事件粗估的，沒有用 `usage.output_tokens_details.thinking_tokens` 驗證（`agent_calls` 目前沒有存這個欄位）。
- 新聞標註晚上到隔天早上的重疊率：新口徑下只有 22:20 → 23:01 一組資料。
- 技術與基本面簡報列為何每次都不同（4.6-2）：沒有查。
- 交易 Agent 的 `no_trade` 理由是否被 `prior_decision_context` 或論點檢視讀取：沒有查。
- 文獻方面：沒有找到針對「公告重大性判定」或中文／台股文本的 LLM 一致性研究；沒有找到同儕審查的 JSON／CSV／精簡格式「token 數加準確度」比較；
  壓縮方法是否保留識別碼與數字沒有任何論文量測；官方沒有 effort 各等級的定量曲線。第 5 節標「摘要」「轉述」的來源，數字未經逐字核對。
- 所有「預估省」都假設各階段的呼叫次數與輸出量不變；實際會隨事件量與重試次數浮動，11/16 的 brief 口徑也才上線一天。

## 附錄：數字怎麼重算

- 各階段 token 與花費：讀 `artifacts/daily/*/*/pipeline.json` 的 `agent_calls`，依名稱去掉 `_b<批次>` 與雜湊尾碼分組，乘第 2.1 節單價。
- brief 欄位占比：對 `brief_*.json` 逐鍵以精簡 JSON 的字元數加總。
- 可沿用率：相鄰兩個 run 的 `brief_event_b*.json` 逐檔比較 `events`、`news`、`financial_context`、`data_availability`。有沿用的 run 只存了未沿用股票的簡報，樣本有偏，所以表中只用完整簡報的 run。
- 重判一致率：同上條件下排除 `event_reuse.json` 列出的股票，比較兩個 run 的 `analyst_event.json`。
- thinking 粗估：`var/logs/agent_trace/<run>/*.jsonl` 中 `thinking_tokens` 事件的 `estimated_tokens`，以每段最大值加總。
