# 投資組合買賣決策與風控多子 Agent 計畫 V2

> 開發順序：Research Report V0 之後的下一個決策層；完成後交給回測 Agent，通過前向驗證後才接正式 DailyReport 與 D-Plan。

> 實作狀態（2026-09-21）：P0～P6 fixture 驗收已完成，包括規則與必備基準綁定、帳戶對帳、整張配置／費稅、依成交重建情境、完整修正鏈與磁碟封存驗證。真實帳戶／交易狀態／正式規則接入與 P7 回測尚未完成。

## 定位與範圍

本系統把同一時間點的 `ResearchSnapshot`、已驗證研究結果、目前帳戶、競賽規則與基準版本，轉成可重算的 `DecisionResult`。第一版採用「一個主控 Agent、五個職責隔離的子 Agent、確定性配置與風控工具」：子 Agent 負責研究解讀、買賣意圖、反方風險審查與裁決；技術指標、權重、股數、費稅、現金、情境計算及競賽限制全部由 Python 工具計算。

第一版仍由 Codex 或 Claude 工作階段載入 Skills，不在專案內串接 LLM API、LangChain 或 LangGraph。輸出只是可供回測、報告及人工檢查的交易提案，不自動下單、不送件，也不能覆寫 Data、Event Research 或 Market Perception 的已驗證結果。

## 多子 Agent 架構

```text
Portfolio Decision 主控 Agent
  ├─ Momentum & Regime Agent ───── 市場狀態與確定性動能解讀
  ├─ Buy Candidate Agent ───────── 買進／加碼／觀察／排除
  ├─ Sell & Exit Agent ─────────── 續抱／減碼／退出／強制退出
  ├─ Trade Adjudicator Agent ───── 合併獨立買賣意見為交易意圖
  └─ Portfolio Risk Agent ──────── 配置後風險挑戰與修正要求
                         │
                         ▼
  Allocation／Order／Fee／Scenario／CompetitionGuard 確定性工具
                         │
                approved／rejected／no_trade
```

主控 Agent 只負責固定執行順序、傳遞已驗證契約、限制修正次數及保存結果。它不能自行新增候選、修改工具數字，或把硬性風控失敗降級為警告。

P0～P2 的 Buy 與 Sell 由主控分別建立不含 peer packet 的 role input artifact，並在分開的子 Agent 環境執行；packet 以 `role_input_sha256` 綁定輸入。DecisionInputBundle、歷史行情序列、Buy／Sell packets、DebateBundle 與 TradeIntentResult 都使用可重算內容雜湊，決策層自有 schema 採嚴格欄位白名單。

## 子 Agent 職責

### 1. Momentum & Regime Agent

- 只讀 `DecisionInputBundle` 與 `MomentumEngine` 結果。
- 解讀相對報酬、MA20／MA60、量能、ATR、下行波動、流動性及市場寬度。
- 輸出 `RegimeAssessment` 與 `MomentumResult`，市場狀態限定為 `bull`、`neutral` 或 `bear`。
- 不自行計算或改寫指標，不輸出權重、股數或訂單。

### 2. Buy Candidate Agent

- 讀取相同 Snapshot 的事件研究、市場認知、動能結果及目前持倉。
- 每檔只能提出 `buy`、`add`、`watch` 或 `exclude`。
- 保存催化因素、證據、事件期間、失效條件、追價風險與缺漏資料。
- 不得讀取 Sell Agent 輸出，不得用單一情緒或分析師目標價直接形成買進結論。

### 3. Sell & Exit Agent

- 專門檢查目前持股及其原始 thesis，不負責尋找新買進標的。
- 每檔只能提出 `hold`、`trim`、`exit` 或 `forced_exit`。
- 檢查 thesis 失效、事件結束、動能反轉、波動惡化、不可交易、規則超限及換手成本。
- 不得讀取 Buy Agent 輸出；硬性不可交易或法規／競賽限制只能標記 `forced_exit` 或交由 Guard 拒絕。

### 4. Trade Adjudicator Agent

- 讀取同一 `DecisionInputBundle`、Momentum 結果及彼此獨立的 Buy／Sell packets。
- 對衝突標的裁決 `buy`、`add`、`hold`、`trim`、`exit`、`exclude` 或 `no_trade`。
- 輸出 `TradeIntentResult`，保留採納、否決、未解問題、證據與退出條件。
- 不新增上游沒有的事實，不產生目標權重、股數、費稅或合規結論。

### 5. Portfolio Risk Agent

- 只在確定性 `AllocationProposal`、`OrderProposal` 與 `ScenarioResult` 完成後執行。
- 挑戰個股／產業集中、事件與來源集中、流動性、換手、回撤、成交價及現金壓力。
- 輸出 `approve`、`revise` 或 `reject`；`revise` 只能使用 allowlist 內的結構化修正要求。
- 不直接手寫新權重或股數，也不能覆寫 `CompetitionGuard` 的硬性失敗。

## 輸入契約

`DecisionInputBundle` 至少包含：

- `run_id`、`decision_cutoff`、`snapshot_id`、`snapshot_hash` 及 schema 版本。
- 已通過品質閘門的 `ResearchSnapshot` 與 cutoff 前行情。
- 零或多份已驗證 `ResearchResult`；Market Perception 若提供，`MarketPerceptionResult` 與 `PerceptionDataBundle` 必須成對且版本一致。
- `AccountSnapshot`：結算日期、現金、NAV、持股、成本、超限起始日及來源版本。
- 官方 150 檔交易池、可交易狀態、競賽規則及全部指定 ETF 基準版本。
- 手續費、交易稅、一張 1,000 股的固定交易單位、滑價、成交價、收盤價及壓力情境設定。

市場認知資料為選配；合法資料不存在時保留 `unavailable`，不能阻擋純事件／動能基線。帳戶、必要價格、交易池、規則或基準缺失則必須 fail closed。

## 輸出契約

| 契約 | 內容 |
| --- | --- |
| `RegimeAssessment` | 市場狀態、依據指標、資料日期與不確定性 |
| `MomentumResult` | 全交易池動能、波動、量能、流動性與市場寬度 |
| `BuyIntentPacket` | 新買／加碼候選、證據、催化因素、失效條件與排除原因 |
| `SellIntentPacket` | 續抱／減碼／退出建議、證據、失效原因與強制退出旗標 |
| `TradeDebateBundle` | 相同輸入雜湊、獨立買賣 packets 及依賴證明 |
| `TradeIntentResult` | 裁決後每檔交易意圖、優先序、理由及未解問題 |
| `AllocationProposal` | 確定性目標權重、現金目標及使用的策略設定 |
| `OrderProposal` | 目前持股到目標組合的整張買賣量、換算股數、費稅與預估現金 |
| `ScenarioResult` | 成交、收盤、波動及流動性壓力情境結果 |
| `RiskReview` | `approve`／`revise`／`reject`、結構化修正及風險證據 |
| `GuardResult` | 每條硬性規則的通過、拒絕、警告與重算數值 |
| `DecisionResult` | `approved`／`rejected`／`no_trade`、最終組合、訂單、引用與版本雜湊 |

`no_trade` 只表示沒有需要執行的交易；目前持股仍必須通過資料、情境及競賽風控，不能用零交易跳過檢查。

## 確定性工具與物件

| 工具／物件 | 責任 |
| --- | --- |
| `DecisionInputValidator` | 驗證 Snapshot、cutoff、研究、帳戶、規則與基準版本 |
| `MomentumEngine` | 以固定公式計算行情特徵與市場狀態輸入 |
| `CandidateMerger` | 合併交易意圖、既有持股與排除原因，不增加新事實 |
| `PortfolioAllocator` | 依核准策略設定計算目標權重與現金目標 |
| `OrderPlanner` | 以一張 1,000 股計算買賣量，禁止零股單、超賣、放空及雙向重複訂單 |
| `FeeTaxCalculator` | 使用十進位規則計算手續費、交易稅與預留現金 |
| `ScenarioSimulator` | 分開使用預估成交價與收盤價進行壓力情境 |
| `CompetitionGuard` | 驗證交易池、可交易性、持股數、現金、個股上限及全部 ETF Active Share |
| `DecisionValidator` | 由原始輸入重建配置、訂單、費稅、情境及最終狀態 |
| `DecisionRepository` | 保存輸入、Agent packets、修正歷程、結果與內容雜湊 |

既有 `EventDrivenStrategy` 與 `CompetitionGuard` 只作為原型基礎。實作時必須拆開候選評分、買賣裁決、配置、訂單、情境與硬性風控，避免單一物件同時決定全部結果。

## 執行與修正流程

```text
驗證 DecisionInputBundle
  → MomentumEngine 計算特徵
  → Momentum & Regime Agent 解讀市場狀態
  → Buy Candidate 與 Sell & Exit 使用相同輸入獨立執行
  → Trade Adjudicator 驗證 DebateBundle 並產生 TradeIntentResult
  → PortfolioAllocator／OrderPlanner／FeeTaxCalculator
  → ScenarioSimulator
  → Portfolio Risk Agent
      ├─ reject  → rejected DecisionResult
      ├─ revise  → 依 allowlist 重新計算，最多 3 次
      └─ approve → CompetitionGuard
                       ├─ 失敗 → rejected DecisionResult
                       └─ 通過 → approved 或 no_trade DecisionResult
  → DecisionValidator 完整重建後保存
```

修正動作第一版只允許降低風險預算、提高現金緩衝、移除／替換候選、降低單一或產業曝險及減少換手。每次修正都要保存原因、前後輸出及工具版本；三次仍未通過即拒絕，不得無限重算。

## Fail-closed 規則

- Snapshot 不可用、缺少時區、cutoff 或版本／內容雜湊不一致時停止。
- 研究引用不存在、股票誤配或 Agent packet 依賴不獨立時拒絕。
- 決策輸入夾帶未知欄位、evidence ID 跨來源碰撞、內容雜湊不一致或 Market Perception cutoff 不完全相同時拒絕。
- 缺少帳戶、必要價格、競賽規則、交易池、可交易狀態或任一指定 ETF 基準時拒絕。
- 交易池外、不可交易、超賣、放空、持股數、現金、個股權重或 Active Share 違規時拒絕。
- 費稅、整張單位、現金或情境結果無法重算時拒絕。
- Risk Agent 的文字結論與工具結果衝突時，以硬性工具為準並拒絕結果。
- `rejected` 不得輸出可送件訂單；`approved` 也只代表可交給回測與人工檢查。

## Skill 規劃

| Skill | 角色 |
| --- | --- |
| `skills/portfolio-decision/` | 主控流程、修正上限、結果保存及完整驗證 |
| `skills/momentum-regime/` | 市場狀態與動能結果解讀 |
| `skills/buy-candidate/` | 獨立買進／加碼研究 |
| `skills/sell-exit/` | 獨立續抱／減碼／退出研究 |
| `skills/trade-adjudication/` | 驗證買賣獨立性並裁決交易意圖 |
| `skills/portfolio-risk-review/` | 配置後風險挑戰及結構化修正要求 |

## 開發里程碑

1. **P0 契約與邊界（已完成）**：建立 `DecisionInputBundle`、買賣 packets、DebateBundle、結果契約、列舉值及 validators。
2. **P1 動能與市場狀態（已完成）**：完成無未來資料的 MomentumEngine、時間隔離、缺值政策及市場狀態 fixture。
3. **P2 獨立買賣裁決（已完成）**：建立 Momentum、Buy、Sell、Adjudicator Skills 與獨立依賴驗證；不產生權重。
4. **P3 配置與訂單（已完成 MVP）**：完成目標配置、整張（1,000 股）、費稅、現金、換手及受限重算的確定性工具。
5. **P4 情境與風控（已完成 MVP）**：建立 Risk Skill、價格／流動性壓力情境、全部輸入基準 Active Share、硬性規則及三次修正上限。
6. **P5 主控與保存（已完成 MVP）**：擴充 `portfolio-decision` Skill、CLI、DecisionRepository、內容雜湊及完整重建 validator。
7. **P6 測試與稽核（fixture 驗收已完成）**：涵蓋手算費稅／現金、整張部分成交、賣單未成交而買單成交的資金缺口、規則與基準缺漏、NAV 對帳、完整修正鏈及磁碟竄改／路徑越界；正式 Provider 驗收另行處理。
8. **P7 回測交接**：固定資料、策略、Agent、工具及規則版本，交給[回測 Agent](backtest_agent_plan.md)與[回測方法規格](backtest_plan_v1.md)驗證。

P0～P6 fixture 驗收已完成；下一批進入 P7 回測，正式 Provider／規則版本仍需獨立確認。

## P3～P6 實作紀錄（2026-09-21，fixture 驗收已完成）

目標：把已驗證的 `TradeIntentResult` 轉成可重算的配置、交易提案與最終風控結果。現有 CLI 負責確定性運算與驗證；Buy、Sell、裁決與風險論證仍由載入 Skills 的工作階段產生，目前沒有單一指令自動呼叫全部模型的執行器。

### 批次一：P3 配置、股數與現金

1. **補齊版本化輸入**：設計配置層獨立設定契約，保存策略、費稅、交易單位、價格假設、產業分類、可交易狀態、基準及來源版本。沿用 P0～P2 嚴格白名單，不直接塞入未知欄位；需要改版時明列 schema 遷移與相容性測試。
2. **固定第一版配置政策**：採可重現的等額新增資金配置基線，現金緩衝、單股／產業上限、減碼比例及換手上限由版本化策略設定提供。`buy/add` 只授權增加曝險；`hold/no_trade` 保留股數；`trim` 依固定政策減少；`exit/forced_exit` 目標歸零。`exclude` 不授權買進或隱含賣出既有持股。不可行時回報限制原因，不擅自改變裁決方向。這是工程基線，不宣稱為最優策略。
3. **建立 PortfolioAllocator**：先套用退出／減碼，再依可用資金配置買進／加碼候選；同順位按股票代號排序。上限裁切與剩餘資金重新分配採固定順序，保存每項裁切原因。保留既有持股造成硬性超限時拒絕，不偷偷增加賣出意圖。
4. **建立 OrderPlanner 與 FeeTaxCalculator**：以 Decimal 計算價格、張數、換算股數、手續費、稅與現金；第一版固定一張 1,000 股，最低費用及進位方式由設定提供。整張化後重新計算實際權重、費稅、換手及餘額；禁止零股單、超賣、負股數與同檔雙向訂單。帳戶若已有零股則 fail closed；賣出資金是否可支應買進由結算／購買力規則決定，不預設立即可用。
5. **產出與驗證**：提供 `AllocationProposal`、`OrderProposal` 與可重算 validators；新增配置、訂單及驗證 CLI。每份結果綁定輸入、裁決、策略及工具版本雜湊。

驗收：覆蓋新買、加碼、續抱、減碼、退出、零股／交易單位殘餘、費用不足、資金不足、同順位及不可行配置；相同輸入產出相同結果。P3 輸出仍為中間提案，不能標記最終 `approved`。

### 批次二：P4 壓力情境與 Portfolio Risk

1. **ScenarioSimulator**：依版本化設定模擬成交滑價、收盤估值、價格下跌、流動性不足及部分／無法成交。分開保存參考價、假設成交價與情境收盤價；所有假設標記為情境，不冒充 cutoff 後已知行情。缺少必要行情或分類時明列失敗。
2. **完整 CompetitionGuard**：驗證全部指定 ETF 基準，以及交易池、可交易性、持股檔數、現金、個股曝險、超限期限與必要限制。逐條保存規則 ID、門檻、重算值及證據；分別檢查取整後組合與必要情境。規則、費稅與 Active Share 計算口徑必須以有來源的有效版本確認，不把現有原型設定視為已完成官方核實。
3. **新增 portfolio-risk-review Skill**：讀取固定的配置、訂單、情境與 Guard 結果，審查集中度、來源／事件重疊、流動性、現金及換手風險。輸出 `RiskReview`，包含 `approve/revise/reject`、論點、證據與結構化修正要求。
4. **有限修正**：首次提案後最多允許三次修正重算。第一版只允許移除買進候選、降低設定內的風險預算／曝險上限、提高現金緩衝或減少換手；不能新增未裁決標的、提高買進強度或任意填入目標權重。每次變更由程式驗證合法性，再重跑配置、訂單、情境與 Guard；Risk 必須審查新版本。硬性失敗直接拒絕，策略修正只適用於尚未違反硬性規則的風險疑慮。

驗收：覆蓋單一／多個基準、基準缺漏、無法成交的強制退出、集中與現金壓力、非法修正、舊版本審查重用、第四次修正及 Risk 同意但 Guard 拒絕。`forced_exit` 表示退出要求，不能作為已成交證明。

### 批次三：P5 主控保存與最終結果

1. **DecisionResult**：保存 `approved/rejected/no_trade`、引用的配置／訂單／情境／審查／Guard、未解風險、版本與雜湊。只有 Risk 同意、硬性規則通過且完整驗證成功，才可輸出 `approved`；零訂單仍須完成相同檢查才能輸出 `no_trade`。
2. **DecisionValidator**：從原始輸入與已保存 Agent artifacts 重算配置、股數、費稅、現金、情境及 Guard，再核對最終狀態與修正鏈；不重新呼叫模型，也不以內容雜湊代替數值驗證。
3. **DecisionRepository**：按 run ID 保存不可變輸入、每次提案、Agent 輸出、修正原因、驗證結果與 manifest。採原子保存與內容比對；相同 run ID 重跑不得覆蓋不同內容，中斷後不得留下可誤認為成功的結果。
4. **主控 Skill 與 CLI 擴充**：提供配置、情境、風險審查驗證、有限修正、最終封存與重播驗證指令。Skill 主控負責呼叫子 Agent，Python 負責執行狀態與修正次數，不引入模型 API。
5. **交付邊界**：`rejected` 保留診斷與歷次提案供稽核，但最終可用訂單清單為空；`approved` 只代表通過本版驗證的模擬交易提案。Research Report V0 保持研究用途；正式 DailyReport／D-Plan 與下單不納入本批。

驗收：完整測試買進、退出、無交易、修正後通過、拒絕與中斷恢復；竄改任何中間數值、來源、版本或狀態都必須被拒絕。

### 檔案與交付安排

- 核心程式放在 `src/etf_agent/decision/`，按配置、訂單／費稅、情境、風控與保存分模組；既有 `guard.py` 的原型呼叫端需保留相容測試。
- 新增 `skills/portfolio-risk-review/`，擴充 `skills/portfolio-decision/` 的 CLI 與契約參考；同步更新 README、架構與回測交接文件。
- 每批完成均執行全專案 unittest、compileall、`git diff --check`；新增／大幅修改的 Skills 另跑結構驗證。P6 的稽核測試併入每一批，不留到所有程式完成後才補。
- 先以清楚標示的 fixtures 驗證完整流程，再接真實帳戶、可交易狀態、有效規則與全部基準；未接妥的 Provider 明確標記缺漏，不能宣稱正式資料端到端完成。
- P5 已完成 MVP；補齊 P6 邊界 fixture、正式規則與 Provider 後建立固定版本的回測交接包，進入 P7。

## 完成條件

P0～P5 MVP 後的具體補強已依 [P6 決策驗收計畫](decision_acceptance_plan.md) 完成 fixture 驗收；部分成交帳務與完整修正鏈均可重播，但仍不能把 fixture 驗收宣稱為正式資料或實盤驗收。

- 同一組輸入、設定與工具版本可重建相同數值結果。
- Buy 與 Sell packets 使用相同輸入且互相隔離，Adjudicator 不新增事實。
- 每筆權重、股數、費稅、現金及 Active Share 都可由原始輸入重算。
- 正常買進、退出、無交易、可修正與不可修正案例都有 fixture 及 fail-closed 測試。
- 必要規則違反時必定輸出 `rejected`，不生成可送件交易書。
- 完成後只交給回測 Agent；未通過歷史與前向驗證前，不宣稱可正式交易。
