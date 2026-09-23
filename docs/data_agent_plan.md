# Data Agent 計畫 V1

## 1. 架構決策

第一版 Data Agent 由 **Codex 或 Claude 工作階段**擔任推理與工具選擇引擎，專案本身不串接 OpenAI、Anthropic 或其他模型 API，也不引入 LangChain／LangGraph。CLIProxyAPI 延後評估，不是第一版依賴。

```text
使用者／排程觸發
       ↓
Codex 或 Claude 專用工作階段
       ↓ 載入 event-data Skill
檢查缺漏、規劃查詢、選擇允許的工具
       ↓
Python 資料工具
       ↓
驗證、版本化、SQLite、ResearchSnapshot
       ↓
DataAgentResult + 執行軌跡
```

Data Agent 在第一版的完整定義是：

```text
Codex／Claude Agent + event-data Skill + 確定性 Python 工具
```

Skill 提供角色、工作流程、資料來源優先順序、證據要求、停止條件與工具邊界；Codex／Claude 負責依目前資料狀態決定下一步。Python 工具才是實際資料來源與安全邊界，模型產生的文字不能直接當成市場事實入庫。

此架構不需要專案保存模型 API key，也不需要自行維護 LLM client。Codex／Claude 的登入、模型執行與工具權限由各自的產品工作階段處理。

## 2. 獨立性的定義

第一版的「獨立 Data Agent」是可單獨開啟、只負責資料任務的 Codex task 或 Claude session，不是常駐 Python daemon。

它必須具備：

- 專用 `event-data` Skill。
- 明確輸入：官方交易池、資料截止時間、執行模式與查詢預算。
- 專用工具清單，不能任意執行資料刪除、交易或送件。
- 自己檢查資料狀態並決定補查順序。
- 有上限的工具呼叫、重試、時間與停止條件。
- 每次執行產生 `agent_run_id`、結構化結果與完整工具軌跡。
- 只交付資料事實、來源、證據與品質狀態，不進行事件評分或交易決策。

它不需要獨立部署成服務，但必須能在與策略 Agent 不同的工作階段中執行，並只透過 `ResearchSnapshot`／`DataAgentResult` 與下游交換資料。

## 3. 第一版範圍

### 必須完成

- 讓 Codex 與 Claude 都能載入同一份 `event-data` Skill 核心內容。
- 查詢交易池、行情、公司資料、最近成功時間與品質問題。
- 由 Agent 根據缺漏與新鮮度規則決定是否呼叫資料工具。
- 透過既有 TWSE／TPEx／Yahoo collectors 取得結構化資料。
- 允許 Agent 搜尋官方網站以發現缺漏來源或補充非結構化證據。
- 保存原始回應、來源 URL、抓取時間與內容雜湊。
- 由 Python 驗證股票代碼、日期、單位、缺值、重複與版本。
- 以 `decision_cutoff` 建立不可變的 `ResearchSnapshot`。
- 快照中的行情與文件都要帶 `source_evidence_id`，可追到來源權威分類、URL、內容截至時間、抓取時間、內容雜湊與 `raw_payload_id`。
- 快照要鎖定當次實際採用的價格版本；歷史 cutoff 不得讀到 cutoff 之後才抓回的行情。
- 快照不可用時停止，禁止交給事件策略。
- 保存 Agent 的查詢計畫、工具呼叫、collection `run_id`、品質旗標與最終結果。

### 第一版不做

- 不在專案程式內呼叫任何 LLM API。
- 不要求 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 或模型 SDK。
- 不安裝或部署 CLIProxyAPI，不把 Codex／Claude 登入憑證轉成專案 API。
- 不引入 LangChain、LangGraph 或自建多 Agent runtime。
- 不讓 LLM 直接產生、修改或推測價格、營收、比率與日期。
- 不讓任意搜尋結果直接覆蓋官方資料。
- 不做事件利多／利空評分、選股、權重、訂單或交易。
- 不自動下單或送件。
- 不提供任意 SQL、Shell、資料刪除或歷史版本覆寫能力。

季報、法說、新聞、MSCI 與總體日曆逐步接入。第一版先讓 Agent 能可靠發現缺口、呼叫既有資料工具並建立可稽核快照。

## 4. Agent 與程式分工

| Codex／Claude Data Agent | Python 工具與資料層 |
| --- | --- |
| 讀取 `DataStatus` 並判斷優先補查項目 | 查詢 SQLite、計算涵蓋率與新鮮度 |
| 根據資料類型選擇允許的工具 | 連線到設定好的 TWSE／TPEx／Yahoo 來源 |
| 為缺漏資料形成官方來源查詢 | 限制網域、參數、逾時與回應大小 |
| 閱讀非結構化公告並提出公司關聯 | 驗證公司代碼、來源、時間與證據位置 |
| 發現來源衝突並要求補查 | 保存所有版本，不靜默覆寫 |
| 說明不能確認的資料與剩餘缺口 | 將缺漏保存為 `null` 與 quality flag |
| 決定何時停止查詢 | 強制工具輪數、重試與正式快照閘門 |
| 彙整執行結果 | 建立不可變 Snapshot 與結構化 run 紀錄 |

LLM 的判斷只能影響「下一個呼叫哪個允許的工具」，不能決定資料是否通過數值、時間或交易池驗證。

### Python 物件分工

Data Agent 核心採物件組合，不以大型函式同時處理查詢、轉換、驗證與寫入：

| 物件 | 責任 |
| --- | --- |
| `DataAgentService` | 提供 CLI 與其他 Agent 使用的穩定入口 |
| `SnapshotBuilder` | 協調快照建立流程，不直接保存 SQL 細節 |
| `SnapshotRepository` | 查詢 SQLite 並保存不可變快照關聯 |
| `DataAgentStatusRepository` | 查詢資料健康、收集紀錄與交易池驗證，回傳狀態物件 |
| `SourceEvidenceBuilder` | 將採用的行情與文件轉成 `SourceEvidence` |
| `SnapshotQualityPolicy` | 計算 fail-closed 品質旗標 |
| `SnapshotPrice`／`SnapshotDocument`／`SnapshotMonthlyRevenue` | 在 Python 內傳遞有型別的資料物件 |
| `ResearchSnapshot` | 聚合快照物件，並在邊界序列化成相容 JSON |
| `DataAgentApplication` | 解析 Skill CLI 命令並呼叫上述服務物件 |

日期正規化、雜湊及 URL 轉換等沒有狀態的單純操作保留為純函式；不為了形式而建立沒有責任或狀態的 class。核心流程不得再用巢狀 dictionary 作為內部領域模型，dictionary 只出現在 SQLite row 與 JSON 輸入輸出邊界。

### 工具設計原則

參考 [stocks-scoring-agent](https://github.com/realmistic/stocks-scoring-agent) 的工具拆分方式，但不採用其 OpenAI Agents SDK、即時資料直接分析或美股專屬來源。Data Agent 工具遵守：

- 每個工具只回答一類明確問題，不建立包辦所有來源的 `get_all_data`。
- 同一個 Python 工具必須能被人工 CLI、單元測試、Codex／Claude Skill 與未來的 Agent backend 共用。
- 抓取工具與查詢工具分離；查詢既有 Snapshot 不得暗中連線或更新資料。
- 公司事實支援 `periods_ago`、`compare_to` 或等價參數，可比較本期、前期及去年同期。
- 工具只回傳結構化資料、來源 ID 與品質狀態；自然語言解讀留給研究 Agent。
- Agent 只能看見核准的細粒度工具，不能取得任意網路、SQL 或 Shell 能力。

## 5. Skill 的可攜設計

`skills/event-data/` 是版本控制中的共同來源：

```text
skills/event-data/
├── SKILL.md
├── scripts/data_agent.py
├── references/data-contract.md
└── agents/openai.yaml
```

核心 `SKILL.md`、`scripts/` 與 `references/` 保持 Codex／Claude 都能理解的 Agent Skills 格式。供應商專屬設定分開處理：

- Codex：安裝共同 Skill 後以 `$event-data` 觸發；`agents/openai.yaml` 只提供 Codex／OpenAI UI metadata。
- Claude Code：將共同 Skill 安裝或同步到 `.claude/skills/event-data/`，以 `/event-data` 觸發。
- 不在兩份 Skill 中複製流程規則；共同來源修改後以安裝／同步腳本更新目標位置。

第一版新增一個本地安裝／檢查腳本，驗證兩個目標中的 Skill 版本與來源雜湊一致。若只使用其中一個產品，可以只安裝對應目標。

## 6. Agent 執行流程

```text
接收任務、decision_cutoff 與查詢預算
  ↓
呼叫 get_data_status
  ↓
建立缺漏與新鮮度清單
  ↓
是否需要補資料？
  ├─ 否 → build_snapshot
  └─ 是
       ↓
     Agent 選擇一個允許工具
       ├─ fetch_prices
       ├─ fetch_disclosures
       ├─ fetch_company_facts
       ├─ search_official_sources
       └─ read_raw_source
       ↓
     Python 驗證並保存
       ↓
     再次呼叫 get_data_status
       ↓
     未達停止條件且仍有必要缺漏？
       ├─ 是 → 下一輪工具呼叫
       └─ 否 → build_snapshot
                    ↓
              usable=false → FAILED
              usable=true  → COMPLETED／DEGRADED
```

Agent 每一輪只能選擇一個明確工具呼叫，讀取結果後才能規劃下一輪。第一版最多 8 次資料工具呼叫、每個網路工具最多 3 次總嘗試；達到預算時立即停止補查並建立品質結果，不得無限循環。

## 7. 官方資料收集與來源探索

### 正式收集

正式數值資料只能由設定檔中的 allowlist provider 取得，經 parser 與 validator 後入庫：

| 資料 | 第一版來源 |
| --- | --- |
| 官方交易池、最新價量 | 官方名單、TWSE／TPEx 最新行情管線 |
| 歷史行情 | 目前由 Yahoo 每日增量更新兩年資料；TWSE／TPEx 官方歷史 provider 已有，正式 CLI 與覆蓋補強仍在 M1 |
| 月營收 | TWSE／TPEx 公開資訊來源 |
| 重大訊息 | TWSE／TPEx 公開資訊來源 |

### 資料來源擴充順序

來源不是越多越好，先補足正式策略與歷史重播所需的時間深度：

| 優先級 | 資料 | 可得性 | 建議來源與目的 |
| --- | --- | --- | --- |
| P0 | TPEx 最新行情 | **已接入** | TPEx 收盤行情端點；與 TWSE 共用收集流程，補齊每日上市與上櫃價格新鮮度 |
| P0 | 歷史月營收、歷史重大訊息 | 官方網站可查；穩定批次介面與完整期間待 PoC | MOPS／TWSE／TPEx；讓事件期間與兩年行情可對齊回測 |
| P0 | ETF 基準成分與權重 | 條件式；須先確認競賽提供內容、指定 ETF 與授權 | 優先使用主辦單位檔案；其次基金公司公開持股。部分指數歷史權重是付費資料，不假設可免費自動取得 |
| P1 | 季報與財務指標 | 資產負債表、損益表已有官方 OpenAPI；完整 XBRL、現金流與歷史涵蓋待 PoC | MOPS XBRL／官方財報；取得 EPS、毛利率、營益率、現金流、存貨與應收帳款 |
| P1 | 公司行動與交易狀態 | 多項已確認 TWSE／TPEx 公開介面 | 處理除權息、減資、分割、停復牌、注意／處置與可成交性 |
| P1 | 法說會日曆、簡報與公司展望 | MOPS 可查；附件格式、公司覆蓋與自動下載穩定性待 PoC | MOPS、交易所活動頁及公司 IR 網站；辨識短期催化與展望變化 |
| P2 | 指數調整與總體日曆 | 政府統計多為公開；指數資料可能受授權或付費限制 | 指數編製機構、央行及政府統計；補充跨公司事件 |
| P1 | 新聞與事件線索 | TWSE RSS 已確認；Google News RSS 可作候選發現；yfinance 覆蓋不一致 | 保存標題、發布者、時間與原文連結為 `NewsCandidate`，不直接成為正式數值或回測事實 |

每一個新來源接入前，必須先確認歷史涵蓋、發布時間、當時可得時間、授權／使用條件、穩定識別鍵及原始回應保存方式。

社群情緒與分析師目標價**不屬於 Data Agent 的來源擴充清單**。目前已由獨立的[市場情緒與分析師研究 Agent](sentiment_analyst_agent_plan.md)負責來源評估、情緒／共識變化與反證；Data Agent 不負責評分、推導目標價或把這些訊號寫成正式公司事實。該 Agent 若需要保存原始資料，仍須透過受限的資料介面保存來源、授權、時間與版本，但不因此改變角色歸屬。

2026-09-17 的第一輪實測結果、交易池缺漏與新聞來源比較見 [資料來源可行性測試](source_feasibility_2026-09-17.md)。

「網站可人工查詢」不等於「可長期自動抓取」。新來源先完成 `SourceFeasibilityReport`，至少記錄端點或下載方式、參數、可取得期間、更新頻率、使用條件、限流、是否需登入、樣本雜湊及失敗模式；通過後才加入正式 allowlist。無法合法、穩定取得的資料維持 `unavailable`，不得以搜尋摘要或 LLM 推測補值。

### LLM 協助找資料

Codex／Claude 可以利用自身允許的搜尋或瀏覽工具：

- 尋找官方頁面、API、公告原文或資料說明。
- 比對同一公告的不同官方版本。
- 找出可能影響多家公司的公告證據。
- 提出新的資料來源候選。

搜尋結果先輸出成 `SourceCandidate`，至少包含：

```json
{
  "url": "https://official.example/...",
  "publisher": "官方機構名稱",
  "data_type": "material_event",
  "observed_at": "含時區時間",
  "reason": "為何與目前缺漏相關",
  "evidence": "頁面中支持此判斷的位置",
  "status": "candidate"
}
```

`SourceCandidate` 不是已驗證市場資料。它必須通過網域 allowlist、資料契約與 parser，才能變成正式紀錄；沒有對應 parser 的候選只保存於執行 artifact，等待後續接入，不能直接寫入核心數值表。

## 8. 工具介面

| 階段 | 工具 | 責任 |
| --- | --- | --- |
| V1 | `get_data_status` | 回傳來源新鮮度、交易池涵蓋、最近成功時間及缺漏 |
| V1 | `fetch_prices` | 更新指定範圍行情，沿用既有 collectors |
| V1 | `fetch_disclosures` | 依允許來源取得重大訊息原文 |
| V1 | `fetch_monthly_revenue` | 取得月營收、原始單位、期間及版本 |
| V1 | `get_monthly_revenue` | 從截止時間合格資料比較本期、前期及去年同期，不觸發網路抓取 |
| V1 | `search_official_sources` | 以資料類型、公司、日期及官方網域搜尋候選來源 |
| V1 | `read_raw_source` | 讀取已保存原文或允許的官方 URL，不以摘要取代原文 |
| V1 | `validate_records` | 驗證格式、來源、時間、公司、數字與去重鍵 |
| V1 | `store_validated_records` | 只保存通過驗證的版本，重試不得重複入庫 |
| V1 | `build_snapshot` | 固定截止時間與資料版本，產生不可變快照 |
| V1／M1 | `fetch_financial_statements` | 更新已確認的財務彙總端點；完整 XBRL／現金流留到 M4 |
| V1／M1 | `get_financial_statements` | 只對已驗證欄位做 `periods_ago`／同比查詢，不補猜缺少項目 |
| V1／M1 | `fetch_trading_status` | 更新停復牌、變更交易、分盤、管理、注意及處置狀態 |
| V1／M2 | `fetch_news_candidates` | 從核准 RSS／新聞索引保存標題、來源、時間與連結，不預設抓取全文 |
| V1／M2 | `search_news_candidates` | 依股票、別名、日期與來源查詢候選，回傳誤配與重複旗標 |
| V1.1／M4 | `fetch_investor_materials` | 更新法說會日曆、簡報與公司正式展望 |
| V1.1／M4 | `fetch_corporate_actions` | 更新除權息、減資、分割與代號承接證據 |
| V1.1／M4 | `fetch_benchmark_holdings` | 更新 ETF／指數成分、權重、生效日與版本 |
| V1.1／M4 | `get_price_features` | 從 Snapshot 計算相對報酬、量能、ATR、波動與市場寬度，不讓 LLM 計算 |

工具使用結構化 JSON 輸入輸出及穩定 exit code，讓 Codex 與 Claude 不必解析人類介面文字。所有寫入工具必須自行驗證參數，不能只依賴 Skill 指示。

## 9. 輸入、結果與執行軌跡

### DataAgentRequest

| 欄位 | 說明 |
| --- | --- |
| `decision_cutoff` | 含時區的資料截止時間 |
| `universe_path` | 官方交易池路徑 |
| `database_path` | SQLite 路徑 |
| `mode` | `status`、`collect`、`snapshot` 或 `research` |
| `required_data` | 此次必要資料類別 |
| `max_tool_calls` | Agent 資料工具呼叫上限，正式預設 8 |
| `max_attempts` | 每個網路工具的總嘗試次數，正式預設 3 |
| `allowed_domains` | 可搜尋及讀取的官方網域 |

### DataAgentResult

| 欄位 | 說明 |
| --- | --- |
| `agent_run_id` | 本次 Agent 執行 ID |
| `agent_host` | `codex` 或 `claude` |
| `status` | `completed`、`degraded` 或 `failed` |
| `started_at`／`finished_at` | UTC 執行時間 |
| `decision_cutoff` | 本次快照截止時間 |
| `tool_calls` | 工具、參數摘要、嘗試次數、結果與時間 |
| `collection_run_ids` | 本次觸發的各來源 collection run |
| `source_health` | 各來源探測狀態、schema、資料日期、筆數與核准狀態 |
| `universe_validation` | 每檔股票的可交易、失效、缺漏或代號承接待確認狀態 |
| `source_candidates` | 尚未接入正式 parser 的官方來源候選 |
| `news_candidates` | 新聞標題、發布者、時間、連結、股票候選及誤配／重複旗標 |
| `snapshot_id` | 成功建立時的快照 ID |
| `snapshot_usable` | 下游是否可使用 |
| `quality_flags` | 資料品質與涵蓋缺口 |
| `errors` | 結構化錯誤代碼、步驟與訊息 |

模型產生的自然語言摘要只能作為說明。下游必須讀取結構化欄位、SQLite 紀錄及 `snapshot_id`，不能從 Agent 對話文字解析數字。

## 10. 資料與時間規則

詳細契約以 `skills/event-data/references/data-contract.md` 為準。

- 每筆保存來源 ID、URL、內容雜湊、`published_at`、`available_at`、`fetched_at` 與版本。
- 區分公告日期、營收／財報期間與事件日期，不互相替代。
- 月營收保留幣別、原始單位與 Decimal 值；缺值保存為 `null`，LLM 不得補猜。
- 更正公告建立新版本並連結舊版本；既有 Snapshot 不因更正而改變。
- Snapshot 只納入 `published_at` 與 `available_at` 都不晚於 `decision_cutoff` 的最新合格版本。
- 歷史補抓不能把本次取得時間冒充過去的可用時間。
- 公司不明、時間缺漏或來源衝突的資料保留原文、記錄品質問題並隔離。
- 新聞或搜尋摘要只能作為補查線索，不能覆蓋官方數字。

### D-Plan 交接邊界

Data Agent 只提供建立 D-Plan 所需的資料事實與來源證據，不直接產生完整 D-Plan，也不負責 `market_view`、`inferences`、`decisions`、`orders` 或 `no_trade`。

`ResearchSnapshot` 對下游提供：

- `latest_prices`：本次快照實際採用的行情，每筆引用 `source_evidence_id`。
- `documents`：公告、營收與後續新聞候選等資料，每筆引用 `source_evidence_id`。
- `source_evidence`：內部穩定證據 ID、D-Plan `authority` 分類、來源 URL、`content_as_of`、`published_at`、`fetched_at`、內容雜湊與 `raw_payload_id`。
- `decision_cutoff`、交易池版本與品質旗標：讓下游證明沒有使用未來資料。

未來新增的確定性 **D-Plan Builder／Validator** 才負責：選出本日實際引用的證據、依序配置 `S1`／`O1` 等送件 ID、合併研究與風控結果、建立完整引用鏈、轉成 `+08:00` 時間格式，並依官方 `D-Plan.schema.json` 與 C1／C2／C6／C9／C11／C12／C14 語意規則驗證。驗證失敗就停止產檔，不能由 LLM 補寫缺少欄位。

## 11. 失敗、重試與停止條件

### 立即失敗

- 官方交易池為空或無法載入。
- `decision_cutoff` 沒有時區或格式錯誤。
- 正式模式缺少必要行情。
- 必要官方來源查詢失敗，無法確認資料新鮮度。
- SQLite 寫入、版本關聯或 Snapshot 建立失敗。
- Snapshot 回傳 `usable=false`。
- Agent 嘗試使用未允許的來源、工具或資料寫入路徑。

### 有上限重試

- 只重試逾時、暫時性連線錯誤與明確可重試的伺服器錯誤。
- 解析、輸入、權限及資料契約錯誤不得靠重試掩蓋。
- 每次嘗試都記錄；耗盡 `max_attempts` 後停止該來源。
- Agent 達到 `max_tool_calls` 後必須停止補查並回報剩餘缺口。

### 下游閘門

只有 `status in {completed, degraded}` 且 `snapshot_usable=true` 時，才可把 `snapshot_id` 傳給事件研究。失敗結果仍保存供稽核，但不能進入策略、報告或交易流程。不得以缺資料推論沒有利空或沒有事件。

## 12. 執行方式

### Codex

在專案工作階段啟用共同 Skill 後，以 `$event-data` 要求 Data Agent 執行。例如：

```text
使用 $event-data 檢查官方交易池的資料缺漏，更新允許的官方來源，
建立截止 2026-09-17T08:30:00+08:00 的研究快照。
最多執行 8 次資料工具呼叫；快照不可用時停止並回報原因。
```

### Claude Code

將共同 Skill 同步至 `.claude/skills/event-data/` 後，以 `/event-data` 執行相同任務。Claude Code 使用專案工具與其工作階段權限執行腳本，不由本專案呼叫 Anthropic API。

### 排程限制

- 確定性的 `collect`／`snapshot` CLI 可以由 cron 或一般排程器執行。
- 需要 LLM 判斷缺漏與搜尋來源的 `research` 模式，必須由 Codex／Claude 工作階段啟動。
- 若使用 Codex Automation 或 Claude Code 非互動模式，仍視為由產品工作階段執行，不在本專案內接模型 API。
- 沒有可用的 Codex／Claude 工作階段時，流程只能執行確定性收集，不能假裝完成 LLM 資料研究。

## 13. 未來 LLM API 升級邊界

第一版只保留架構邊界，不實作 LLM API client。Data Agent 的工具、請求、結果與資料契約不得依賴特定模型供應商，未來只有在需要「無人值守且必須由模型判斷」的排程時，才評估新增 `ReasoningBackend`。

```text
DataAgentRunner
      ↓
ReasoningBackend（未來可替換）
      ├─ CodexSessionBackend      # V1 由工作階段人工／自動觸發，不在程式內實作
      ├─ ClaudeSessionBackend     # V1 由工作階段人工／自動觸發，不在程式內實作
      ├─ OfficialApiBackend       # V2 候選
      └─ OpenAICompatibleBackend  # V2 實驗候選，可指向 CLIProxyAPI
      ↓
相同的 allowlist Python tools 與 DataAgentResult
```

升級門檻必須全部成立：

- 已有需要長期、無人值守執行的 LLM `research` 任務，確定性排程無法滿足。
- Codex／Claude 工作階段或其正式非互動方式不能穩定完成該任務。
- 已完成成本、速率限制、憑證保存、失敗恢復與服務條款評估。
- 已有 fixture／evaluation，可比較新後端與既有工作階段的工具選擇及最終 Snapshot。
- 新後端故障時能 fail closed，不影響確定性收集、資料驗證及既有 Snapshot。

若進入 V2，優先實作官方模型 API adapter；CLIProxyAPI 只能作為可移除的本機實驗 adapter，不得成為核心資料層依賴。若實驗使用 CLIProxyAPI，必須只綁定 localhost、關閉遠端管理、使用本機存取金鑰、排除 OAuth token／設定／log 進版控，且不得利用多帳號輪替規避限制。

## 14. 檔案配置

```text
skills/event-data/                 # 共同 Skill 來源
├── SKILL.md
├── agents/openai.yaml             # Codex／OpenAI 專屬 metadata
├── scripts/data_agent.py          # 結構化資料工具 CLI
└── references/data-contract.md

.claude/skills/event-data/         # Claude Code 安裝目標，不複製維護規則

src/etf_agent/data/                # 抓取、解析、驗證、版本與 Snapshot
├── snapshot.py                    # Snapshot 資料物件、Builder、品質政策與 Service
├── snapshot_repository.py         # Snapshot 專用 SQLite Repository
├── status.py                      # DataAgentStatus 與狀態查詢 Repository
└── evidence.py                    # SourceEvidenceBuilder 與無狀態時間／URL 工具
src/etf_agent/contracts.py         # Request／Result／SourceCandidate／NewsCandidate／SourceFeasibilityReport 契約
scripts/install_agent_skills.py    # 安裝／同步並檢查 Skill 版本
artifacts/data-agent/{agent_run_id}/
├── request.json
├── tool_calls.jsonl
├── source_candidates.json
├── news_candidates.json
└── result.json
artifacts/source-feasibility/{source_id}.json
```

Skill 腳本只包裝 `src/etf_agent/data/` 的正式功能，不複製資料邏輯。Codex、Claude、人工 CLI 與排程器必須共用相同 parser、validator 與 Snapshot builder。

## 15. 已有基礎與待實作

### 計畫狀態（2026-09-19）

| 項目 | 狀態 | 證據／說明 |
| --- | --- | --- |
| Data Agent 基礎資料層 | 已完成 | SQLite、原始回應、collection run、文件版本與不可變 Snapshot 已有程式與測試 |
| Data Agent 物件化 | 已完成 | Service、Builder、Repository、Evidence Builder、品質政策與 Snapshot 資料物件已分離；CLI JSON 保持相容 |
| 官方交易池 | 已更新、持續每日驗證 | 2026-09-14 新版官方 PDF 共 150 檔，已將 `5371 中光電` 更新為 `3718 中光電投控`；PDF 雜湊保存在競賽設定 |
| 第一輪來源可行性測試 | 已完成 | 已測 TWSE／TPEx 行情、營收、重大訊息、財報、交易狀態及新聞候選來源 |
| 自動化來源探測 | 已完成 | `scripts/probe_data_sources.py` 產生並保存結構化 `SourceFeasibilityReport` |
| M0 來源健康與交易池閘門 | 已完成 | 150 檔逐檔狀態、`5371`／`3718` 回歸測試與 Snapshot fail-closed 已驗收 |
| M1 行情資料 | **第一段已完成** | TWSE／TPEx 最新行情 150／150；Yahoo 兩年行情已改為安全截止日的增量更新，150 檔均到 2026-09-17 |
| D-Plan 資料交接 | **Data Agent 端第一段已完成** | Snapshot 已輸出價格／文件來源證據並鎖定採用價格版本；完整 D-Plan Builder／Validator 留在報告整合階段 |
| M1 其餘官方資料 | **進行中** | 下一步是官方歷史行情 CLI、財報彙總、交易狀態及其餘細粒度工具 |
| M2～M3 | 尚未開始 | 依 M1 → M2 → M3 順序執行，不平行擴張來源 |
| M4 歷史與進階資料 | PoC／後續 | 不阻擋第一版 Data Agent，但會阻擋正式事件回測 |
| LLM API／CLIProxyAPI／LangChain／LangGraph | 不列入 V1 | Codex／Claude 工作階段搭配共用 Skill 即可 |

目前測試基線為 `PYTHONPATH=src python3 -m unittest discover -s tests -v`，共 41 個測試通過。這個數字是 2026-09-19 的基線；後續以 CI／實際測試輸出為準，不在文件中假設永久固定。

### 已完成

- SQLite、原始回應與 collection run 紀錄。
- 官方交易池、TWSE／TPEx 最新行情，以及 Yahoo 兩年行情增量更新。
- TWSE／TPEx 月營收與重大訊息。
- 文件去重、更正版本及時間隔離。
- 不可變 `ResearchSnapshot` 與品質旗標。
- Snapshot 行情／文件的 `SourceEvidence`、價格版本鎖定與抓取時間 cutoff 隔離。
- `event-data` Skill、資料契約與 `status`／`collect`／`snapshot` CLI。
- 重抓冪等、更正版本、舊快照不變、截止時間與缺值測試。
- 2026-09-17 完成第一輪官方端點與新聞來源可行性測試，結果見 [資料來源可行性測試](source_feasibility_2026-09-17.md)。

### 最新執行路線

以下順序取代「同時擴充所有來源」的做法；前一階段未通過閘門，不進入依賴它的階段。

#### M0：來源健康與交易池閘門

狀態：**已完成（2026-09-17）**。初次即時驗收為 TWSE 100／100、TPEx 49／50，成功發現舊名單中的 `5371.TWO` 已不可交易並讓正式 Snapshot fail closed。2026-09-18 收到新版官方 PDF 後，確認完整 150 檔名單唯一代號差異為 `5371 → 3718`，已更新交易池、名稱、來源日期及來源雜湊；重測結果為 TWSE 100／100、TPEx 50／50，交易池驗證 `completed` 且 `usable=true`。舊案例保留為回歸 fixture，確保未來未經官方文件確認時仍不會自動替換。

- 將本次人工探測整理成可重跑的 `scripts/probe_data_sources.py`，輸出 `SourceFeasibilityReport`。
- 新增 `UniverseValidationResult`，每日比對官方交易池、TWSE／TPEx 行情與可交易狀態。
- 對缺少行情、終止交易、代號變更或承接未確認的股票標記 `universe_mismatch`／`not_tradable`。
- 保留舊版 `5371`／`3718` 情境為第一個回歸 fixture；只有像 2026-09-14 新版官方 PDF 這類主辦文件，才能授權更新正式交易池。

完成條件：所有 150 檔都有 `tradable`、`not_tradable` 或 `universe_mismatch` 的明確狀態；必要標的異常時 Snapshot fail closed。

##### M0 已交付內容

1. 在 `src/etf_agent/contracts.py` 建立 `SourceFeasibilityReport`、`UniverseInstrumentStatus` 與 `UniverseValidationResult`，先固定 JSON schema、列舉值與時間格式。
2. 先把已核准的 TWSE／TPEx 探測端點加入 `config/data_sources.json` allowlist，再新增 `scripts/probe_data_sources.py`；輸出 HTTP 狀態、schema、資料日期、筆數、交易池覆蓋、耗時及錯誤，不在探測時產生投資結論。
3. 新增交易池驗證器，合併 TWSE 與 TPEx 最新行情結果，對每檔股票輸出 `tradable`、`not_tradable` 或 `universe_mismatch` 及來源證據。
4. 將舊版 `5371` 缺漏、`3718` 存在但不得自行替換做成固定 fixture；另驗證新版正式交易池確實使用 `3718`，並保留空交易池、來源失敗、重複代號與跨市場錯配測試。
5. 讓 `status` 顯示來源健康與交易池統計；讓正式 `snapshot` 在必要股票狀態未確認時回傳不可用及非零 exit code。
6. 完成單元測試與離線 fixture 後，再以即時端點執行一次驗收，將結果保存到 `artifacts/source-feasibility/`，不把即時網路測試當成單元測試。

M0 當時不包含 TPEx collector 正式接入、財報入庫、新聞收集或 LLM 工具循環；TPEx collector 已在後續 M1 第一段完成，其餘仍分別留在 M1、M2、M3。

#### M1：補齊確定性官方資料

狀態：**進行中**。

- [x] 接入 TPEx `tpex_mainboard_quotes` 最新行情，與 TWSE 共用 universe 篩選、raw payload、collection run、SQLite 入庫及 Snapshot 分析 view。
- [x] 新增 `scripts/collect_latest_prices.py` 與 Skill `collect-prices`，一次更新官方 150 檔的上市／上櫃最新行情。
- [x] 完成即時驗收：2026-09-17 行情寫入 TWSE 100 檔、TPEx 50 檔，`3718.TWO` 已由 `TPEX_MAINBOARD_QUOTES` 入庫；正式 Snapshot 為 150／150 且 `usable=true`。
- [x] 將 Yahoo 兩年行情改成每日增量刷新：既有標的重抓 7 天、新標的補完整兩年；截止日以 TWSE／TPEx 都已完成的最近官方交易日為準，排除盤中未完成日 K。2026-09-18 驗收時 150 檔均更新至 2026-09-17，缺漏 0 檔。
- [x] 讓 `ResearchSnapshot` 輸出行情與文件的來源證據，並以 `snapshot_prices` 鎖定採用版本；價格也依實際 `fetched_at` 執行 cutoff 隔離，供未來 D-Plan 完整引用鏈使用。
- [ ] 將 TWSE／TPEx 官方歷史行情 collector 接成正式 CLI，逐步降低 Yahoo 作為必要歷史來源的地位。
- [ ] 合併六種業別的 TWSE／TPEx 綜合損益表與資產負債表端點。
- [ ] 接入暫停／恢復、變更交易、分盤、管理、注意及處置狀態。
- [ ] 將 `fetch_prices`、`fetch_monthly_revenue`、`fetch_disclosures`、`fetch_financial_statements` 拆成可獨立測試的結構化工具。

M1 接下來依序執行：

1. 官方歷史行情 CLI：共用現有 TWSE／TPEx provider，支援增量區間、失敗重試、逐檔缺漏與來源覆蓋報告。
2. 財報彙總：先接已確認的損益表與資產負債表欄位，保留期間、產業格式、發布／取得時間及原始回應。
3. 交易狀態：接入停復牌、變更交易、分盤、管理、注意及處置狀態，加入 Snapshot 可成交性閘門。
4. 細粒度工具：把行情、月營收、重大訊息與財報更新拆成可獨立執行、測試及記錄的結構化工具。

完成條件：除已被交易池閘門隔離的標的外，當期行情與財報涵蓋率達 100%；每筆有來源、期間、抓取時間、原始回應與版本。

#### M2：新聞候選層

- 接入 TWSE 官方新聞 RSS。
- 建立 Google News RSS `NewsCandidate` PoC；yfinance 只作非必要備援。
- 新增公司正式名稱、簡稱、舊名、代號及產業詞別名表。
- 實作 URL／標題／發布者／時間去重、日期範圍、股票誤配及來源品質旗標。
- 預設只保存允許的 metadata、摘要與原文連結，不大量重製全文。

完成條件：固定 12 檔上市／上櫃 fixture 能重跑；同一文章不重複入庫；「世界」等一般詞彙不會只靠名稱自動歸因；新聞候選不能單獨改寫核心數值或形成交易候選。

#### M3：Data Agent 工具循環

- 完成 `DataAgentRequest`、`DataAgentResult`、`SourceCandidate`、`NewsCandidate` 與工具軌跡契約。
- 將 CLI 統一成結構化 JSON 輸入輸出與穩定 exit code。
- 更新 `event-data` Skill，加入缺漏判斷、最多 8 次工具呼叫、每個網路工具最多 3 次嘗試及停止條件。
- 建立 Codex／Claude Skill 安裝與一致性檢查。
- 以相同 fixture 分別由 Codex 與 Claude 執行，確認 Snapshot 與品質結果一致。

完成條件：不使用模型 API key，Codex／Claude 都能完成 `status → collect → validate → snapshot`；所有寫入仍由 Python 驗證。

#### M4：歷史與進階資料 PoC

- 驗證 MOPS 歷史月營收、重大訊息、完整 XBRL、現金流、財報附註及法說附件。
- 確認分頁、session、限流、可得期間、發布時間與使用條件後，才建立回補器。
- 向主辦單位確認指定 ETF、前十大口徑、更新方式與授權；無資料時不得假裝完成 Active Share。
- 完成時間隔離後，將 Snapshot 交給事件研究與歷史重播。

完成條件：每個正式來源都有核准的 `SourceFeasibilityReport`；歷史資料能證明 `published_at` 與 `available_at`，不能證明者標記探索資料並排除正式回測。

### 第一版剩餘工作

1. 完成 M1 官方歷史行情 CLI、財報彙總、交易狀態與細粒度結構化工具。
2. 完成 M2 `NewsCandidate`、公司別名、去重、時間與誤配檢查。
3. 補齊 `DataAgentRequest`、`DataAgentResult`、`SourceCandidate`、`NewsCandidate` 與工具軌跡契約；既有 `SourceFeasibilityReport`、`UniverseValidationResult` 不重做。
4. 將工具 CLI 統一為結構化 JSON 輸入輸出與穩定 exit code，加入 `get_data_status` 及 allowlist `search_official_sources`。
5. 新增 `agent_run_id` artifact 目錄，保存查詢計畫、工具呼叫、collection run、候選來源與最終結果。
6. 完成 Codex／Claude Skill 安裝與一致性檢查，並以相同 fixture 驗證 Snapshot 與品質結果一致。
7. 將可用 Snapshot 先交給[事件研究 Agent](event_strategy_v1.md)，再交給[動能／配置／風控 Agent](momentum_portfolio_risk_agent_plan.md)。
8. [回測 Agent](backtest_agent_plan.md)依[回測與驗證方法規格](backtest_plan_v1.md)，使用相同資料契約完成歷史重播、模擬成交、策略比較與前向驗證。
9. 回測與前向驗證通過後，交給[自動化排程／報告 Agent](automation_reporting_agent_plan.md)；其中的確定性 D-Plan Builder／Validator 負責送件格式，Data Agent 不接手市場觀點、推論、決策或訂單。

上述功能依 M0～M4 執行；第一版交付範圍以 M0～M3 為主，M4 除必要的來源可行性驗證外不阻擋第一版 Data Agent。

### 資料來源擴充待辦

1. 先為每個候選來源產生 `SourceFeasibilityReport`；未確認介面、期間及使用條件前，不承諾正式接入。
2. TPEx 官方最新行情已接入；下一步將既有 TWSE／TPEx 官方歷史行情 collector 接成正式 CLI。
3. 對 MOPS 歷史月營收與歷史重大訊息做小範圍 PoC，確認可用期間、分頁、限流與發布時間後，再建立回補器。
4. 向競賽規則／主辦資料確認指定 ETF、前十大口徑與更新方式；若官方未提供且來源需付費，標記為外部依賴並阻止正式 Active Share 驗證。
5. 先接入已確認的財務彙總 API，再驗證完整 XBRL／財報文件；只對已證實可取得的欄位建立期間比較。
6. 接入 TWSE 官方新聞 RSS，並以 Google News RSS 做 `NewsCandidate` PoC；只保存允許的 metadata／摘要與原文連結，加入公司別名、去重、時間與誤配檢查。
7. 對法說附件與公司 IR 網站做覆蓋率測試，缺附件時保留日曆事件與缺漏旗標，不以 LLM 補寫展望。
8. 接入已確認的公司行動與交易狀態端點，並每日檢查交易池代號、承接關係與可交易狀態。
9. GDELT 與 yfinance 新聞不得作必要來源；社群情緒與分析師目標價已移出 Data Agent，改列未來獨立研究 Agent。

`ReasoningBackend`、官方模型 API adapter、CLIProxyAPI adapter、LangChain 與 LangGraph 都不列入第一版待實作清單。

## 16. 驗收條件

- 專案依賴中沒有 LLM SDK，執行時不要求模型 API key。
- 第一版可在未安裝 CLIProxyAPI 的環境完成所有必要功能。
- Codex 與 Claude 都能從共同 Skill 完成狀態檢查、補查及 Snapshot 建立。
- 每次執行都有唯一 `agent_run_id`，可追到所有工具呼叫、collection run、候選來源及 Snapshot。
- LLM 不能直接寫入核心市場數值；所有正式資料都經 allowlist provider、parser 與 validator。
- 相同官方資料重跑不建立重複文件；更正內容建立新版本。
- 截止時間之後才取得的文件不會進入歷史 Snapshot。
- 截止時間之後才取得的行情不會進入歷史 Snapshot；Snapshot 會鎖定每檔實際採用的 `raw_payload_id`。
- Snapshot 中的行情與文件都能由 `source_evidence_id` 追到權威分類、URL、內容時間、抓取時間與內容雜湊。
- 缺值保持 `null`，不因 Agent 說明或搜尋摘要而改寫。
- 必要資料缺漏、來源失敗或 Snapshot 不可用時，下游不執行。
- 所有 Agent 工具輪數與網路重試都有上限。
- 每個工具可不經 LLM 獨立呼叫與測試；相同輸入及 Snapshot 產生相同結構化數值結果。
- 查詢工具不會在背景抓取新資料，抓取工具也不直接輸出投資建議。
- 新來源只有在 `SourceFeasibilityReport` 確認介面、使用條件、期間與失敗模式後，才能加入正式 allowlist。
- 付費、需登入、授權不明或無穩定批次介面的資料會明確標記外部依賴，不宣稱已可自動取得。
- 新聞候選缺少可核對原文或官方證據時，不會被提升為正式事件或核心數值資料。
- 同一 fixture 在 Codex 與 Claude 執行後，結構化資料及 Snapshot 一致；允許自然語言摘要不同。
- 既有資料收集、版本化、Snapshot 與策略測試持續通過。
