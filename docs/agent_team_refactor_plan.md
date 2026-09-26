# 決策層 Agent 團隊重構計畫

日期：2026-09-26。狀態：**已確認，實作中（R1、R2 已完成）**。本計畫把決策層改為「分析團隊 → 多空研究 → 交易 → 風險」的分工，參考 [TradingAgents 的團隊分工](https://github.com/TauricResearch/TradingAgents#tradingagents-framework)；安全層（數值由 Python 計算、每一步 Validator、時間點檢查、不可變封存、不自動下單）完全保留。

## 0. 設計原則：確定性歸程式，不確定性歸 LLM

**有唯一正確答案、可以重算驗證的，一律由程式計算；需要權衡、解讀或判斷的，才交給 LLM。** LLM 的輸出只能是有限選項的判斷（等級、立場、意圖）加上引用證據的理由，不得產生或改寫任何數字；程式負責驗證 LLM 輸出的格式、引用與一致性，但不替 LLM 做判斷，驗證失敗時停止而不是補預設值。

| 工作 | 性質 | 歸屬 |
| --- | --- | --- |
| 抓取、解析、版本化資料；時間點（cutoff）檢查 | 確定性 | 程式 |
| 技術指標、財務比率、動能、ATR、市場廣度與 regime 門檻 | 確定性 | 程式 |
| 分批、合併、覆蓋檢查、ID 與內容雜湊 | 確定性 | 程式 |
| 權重、張數、費稅、現金、委託單、壓力情境 | 確定性 | 程式 |
| 競賽規則（現金 <25%、20–30 檔、單檔上限）、交易狀態閘門 | 確定性 | 程式（Guard） |
| 解讀指標與財報代表什麼、事件重大程度、資料缺口的影響 | 不確定 | LLM（分析師） |
| 每檔最強的做多／做空論點 | 不確定 | LLM（多頭／空頭研究員） |
| 權衡多空後的買賣意圖與信心等級 | 不確定 | LLM（交易 Agent） |
| 市場風險高低 → 現金姿態；提案是否需要修正 | 不確定 | LLM（風險 Agent） |
| 等級與姿態換算成權重與現金比例 | 確定性 | 程式（依 Policy 對照表） |

判斷時的檢查方式：**同樣的輸入，兩個人做會不會得到同一個答案？** 會的，交給程式；不會、需要取捨的，交給 LLM，並要求它引用證據、只能在程式定義的選項中選擇。

## 1. 為什麼要改

目前每日決策鏈（`automation/daily_pipeline.py`）是「買方 Agent（只提新標的）＋賣方 Agent（只看持股）→ 裁決 → 風控分級 → 風控審查」，有三個結構問題：

1. **新買進標的沒有反方論點。** 賣方只覆蓋持股，買方提名的新股票在裁決時只有支持 claim，裁決者聽不到任何反對理由。
2. **分析層沒有接進決策。** 基本面研究、事件研究都已實作，但每日流程只給降級的 ResearchResult；買方只看得到動能與月營收。
3. **多空辯論在錯的層級。** 多空只存在於單一事件研究（這則公告利多或利空），沒有針對「這檔股票整體該不該持有」辯論。

## 2. 目標架構

```text
Data Agent（既有）：行情、月營收、重大訊息、財報 → 不可變 Snapshot
        ↓
Python 分批（確定性）：官方 150 檔全部納入，依代號固定切成每批最多 50 檔
        ↓
分析團隊（每位分析師逐批執行，Python 合併並驗證覆蓋全部 150 檔）
  技術分析師｜基本面分析師｜事件／新聞分析師｜情緒分析師（無合法資料時 unavailable）
  事件分析師標為重大（materiality=high）的事件 → 另跑完整 Fact／Bull／Bear／Adjudicator 事件研究
        ↓ AnalystReport × 4 ＋ 重大事件 ResearchResult
研究團隊：多頭研究員 vs 空頭研究員
  讀同一份分析報告與摘要、彼此隔離；兩方都必須覆蓋 150 檔每一檔（逐批）
        ↓ BullPacket ＋ BearPacket
交易 Agent：逐檔權衡多空 → buy／add／hold／trim／exit／no_trade 與信心等級
        ↓ TradeDecision（Python 依等級與 ATR 算權重、張數、委託單）
風險 Agent：市場風險 → 現金姿態；提案／情境／Guard 審查 → approve／revise／reject
        ↓ CompetitionGuard（競賽硬性規則）→ Finalize → 封存 → DailyReport
```

## 3. 各角色輸入、輸出與禁止事項

| 角色 | 執行者 | 讀取 | 輸出 | 禁止 |
| --- | --- | --- | --- | --- |
| 分批 | Python | Snapshot 交易池 | `UniverseBatches`：150 檔依代號固定切批（每批 ≤50），批次雜湊可重算；持股與非持股不分開 | 以 LLM 挑股或排除股票 |
| 技術分析師 | Agent | shortlist 的動能特徵（Python 已計算）、市場 regime | `AnalystReport(technical)` | 自行計算或改寫指標 |
| 基本面分析師 | Agent | 基本面確定性指標（`fundamentals` 工具）、月營收 | `AnalystReport(fundamental)` | 把成長率當市場共識；補寫缺漏財報 |
| 事件／新聞分析師 | Agent | Snapshot 中 cutoff 前的重大訊息、月營收公告 | `AnalystReport(event)`，逐則事件標 `materiality`（high／medium／low／unknown） | 使用網路或模型記憶補事實 |
| 重大事件研究 | 既有 Fact／Bull／Bear／Adjudicator | 被標為 high 的事件 | 已驗證 `ResearchResult`，併入多空與交易輸入 | 略過任何被標 high 的事件 |
| 情緒分析師 | Agent 或確定性 | 已核准授權的 `PerceptionDataBundle` | `AnalystReport(sentiment)`；無來源時確定性輸出全部 `unavailable` | 以搜尋摘要或記憶冒充情緒資料 |
| 多頭研究員 | Agent | 四份 AnalystReport、角色摘要 | `BullPacket`：每檔最強的做多論點 | 讀取空頭 packet；輸出數字 |
| 空頭研究員 | Agent | 同上（相同 role input） | `BearPacket`：每檔最強的反對論點 | 讀取多頭 packet；輸出數字 |
| 交易 Agent | Agent | Bull／Bear packet、分析報告、持股 | `TradeDecision`：逐檔意圖、對 buy／add 的信心等級、採納／否決的 claim | 新增事實或 claim；輸出權重、張數 |
| 風險 Agent | Agent | 分析報告、TradeDecision；之後讀提案／情境／Guard | `cash_stance`；`RiskReview`（approve／revise／reject 與 allowlist 修正） | 手寫權重；覆寫 Guard；放寬規則 |
| 配置／Guard／封存 | Python | 以上已驗證 artifacts | Proposal、Scenario、Guard、Decision run | — |

### AnalystReport（新）

逐檔覆蓋交易池全部 150 檔（逐批產生後合併）。每檔：`outlook`（positive／negative／neutral／unknown）、`findings`（唯一 `finding_id`、文字、屬於該股票的 `evidence_ids`）、`data_gaps`。資料不足時必須標 `unknown` 並寫明缺口，不得省略股票。Validator 檢查覆蓋、引用歸屬、欄位白名單與內容雜湊。

### BullPacket／BearPacket（取代 Buy／Sell packet）

兩方由同一份 role input 產生、`peer_packet_ids` 必須為空、互相看不到。每檔：`strength`（strong／moderate／weak／none）、`claims`（唯一 `claim_id`、文字、`evidence_ids`、可引用的 `finding_ids`）、`invalidation_conditions`。`none` 代表該方找不到有證據的論點，仍須列出該股票。

### TradeDecision（取代 TradeIntentResult ＋ SizingPlan 的個股部分）

逐檔：`intent`、buy／add 時的 `conviction`（high／medium／low）、`adopted_claim_ids`／`rejected_claim_ids`（多空兩方每個 claim 剛好出現一次）、理由與未解問題。沿用現有規則：buy／add／trim／exit 必須採納同方向 claim；已持股不得 buy，未持股不得 add／hold／trim／exit。

### 風險 Agent

- 配置前：`cash_stance`（aggressive／neutral／defensive），沿用現行 `position_sizing.cash_buffer_by_stance` 對應現金比例。
- 配置後：沿用現行 `RiskReview` 與四種 allowlist 修正，Guard 失敗時確定性 reject。
- 比賽只能做多 150 檔股票，沒有放空、期貨或反向 ETF；避險手段限於提高現金（<25%）、減碼／出場、分散產業與避開高波動或事件風險標的。

## 4. 保留不動的部分

- `DecisionPolicy`（含 `position_sizing` 等級乘數 ÷ ATR 配置與現金姿態）、`AllocationOrderEngine`、`ScenarioEngine`、`CompetitionGuardV2`、`RevisionHistory`、`DecisionFinalizer`、`DecisionRepository`。
- `ClaudeAgentRunner`：Read 工具、`--json-schema`、錯誤回饋重試一次、仍失敗即停止。
- 確定性分支：空倉時空頭仍須評估候選（與舊賣方不同），但沒有股票時不呼叫 Agent；Guard 失敗時確定性 reject。

## 5. 契約升版與相容

- 新增 `AnalystReport`、`UniverseShortlist`、`BullPacket`／`BearPacket`（`ResearchDebateBundle`）、`TradeDecision`，決策鏈 schema 升為 2.0。
- `DecisionFinalizer`、`DecisionResultValidator`、`DecisionRepository` 的 artifact 集合改為新鏈；舊 1.0（Buy／Sell）鏈保留至既有 fixture 測試與已封存 run 可驗證為止，不刪除舊封存。
- 下游（虛擬帳本 `apply-decision`、DailyReport、D-Plan）讀取 Decision run 的欄位需逐一核對並補測試；若只讀 `DecisionResult.orders` 與 bundle，則不需改動。

## 6. 每日 Agent 呼叫次數

使用者決定不初篩、全部 150 檔進入分析。為避免單次輸出過大而截斷或驗證失敗，逐檔輸出的角色都分 3 批（每批 ≤50 檔）：分析 9 次（技術、基本面、事件各 3 批；情緒無資料時確定性）＋多空 6 次＋交易 3 次＋風險 1～2 次，另加每個重大事件 4 次。一般日約 20 次。每批只讀自己那批股票的資料與共同市場背景（regime），摘要約 50KB、一次可讀完；每批先單獨驗證、失敗只重跑該批，Python 合併後再驗證全體覆蓋與 ID 唯一性。claude.ai 訂閱登入時計入訂閱額度，不另計費。

## 7. 前置資料缺口

- **財報未入庫**：`financial_statements` 目前 0 筆，`start.sh daily` 未執行 `scripts/collect_financial_statements.py`。使用者決定每日收集（未更新內容自動去重）；入庫前基本面分析師只能用月營收，並須標示 `data_gaps`。
- **情緒資料**：無合法、歷史化來源，維持 `unavailable`。
- **交易狀態**：仍無核准來源（見 TS0），分析與多空照常覆蓋 `unknown` 股票，Guard 仍 fail-closed。

## 8. 開發階段

| 階段 | 交付 | 驗收 |
| --- | --- | --- |
| R1 資料與分批 | **已完成（2026-09-27）**：`start.sh daily` 以 `--latest-due` 每日收集財報（副本實測 2026 Q2 298／300，3718.TWO 缺兩張）；`automation/batching` 分批與合併 | 財報進入 Snapshot；分批可重算；合併後缺漏或重複即拒絕 |
| R2 分析團隊 | **R2a 已完成（2026-09-27）**：`decision/analysts`（AnalystReport 2.0、Validator、確定性摘要、基本面指標由 `fundamentals` 工具計算）、`technical-analyst`／`fundamental-analyst`／`event-analyst` Skill、`DailyDecisionPipeline.run_analyst_team`（逐批執行、單批重跑、合併驗證）、情緒確定性 unavailable；另修正 DecisionInputValidator 未允許 Snapshot 財報文件欄位。**R2b 已完成（2026-09-27）**：`automation/event_research_runner` 對 high 事件依序跑 Fact → Bull／Bear（只讀 FactPacket、互相隔離）→ Adjudicator，程式建立事件脈絡、價格特徵與 packet envelope 並組裝 ResearchResult 2.1；單一事件失敗時結果降級並記錄。ResearchResult 為獨立 artifact（不回寫已封存的 DecisionInputBundle），供多空、交易與報告使用 | 覆蓋全部 150 檔；引用歸屬正確；缺資料標 unknown；high 事件都有已驗證 ResearchResult |
| R3 多空研究 | Bull／Bear 契約、隔離 role input、Validator、Skill | 兩方覆蓋全部股票；互相隔離；claim 唯一 |
| R4 交易 Agent | `TradeDecision` 契約與 Validator，轉接既有配置 | 每個 claim 剛好採納或否決一次；等級可綁入 policy |
| R5 風險與鏈接 | 現金姿態與審查接新鏈；Finalizer／Repository 升版；下游核對 | 新鏈 fixture 端到端 approved／rejected 均可重建 |
| R6 每日腳本 | `daily_pipeline` 改用新鏈，真實資料演練一次 | 真實 Snapshot 跑完並產出 DailyReport；舊鏈測試仍通過 |

每階段完成後執行完整 unittest、compileall、`git diff --check` 與新增 Skill 的 quick_validate，並同步 README、AGENTS.md 與相關 docs。

## 9. 使用者決定（2026-09-26）

1. **不初篩**：官方 150 檔全部進入分析、多空與交易；以分批執行控制單次輸出大小。
2. **財報每日收集**：併入 `start.sh daily`。
3. **事件研究**：事件分析師處理全部公告並標示重大程度；`materiality=high` 的事件另跑既有 Fact／Bull／Bear／Adjudicator 四子 Agent。
