# 投資組合買賣決策與風控多子 Agent 計畫 V2

> 開發順序：Research Report V0 之後的下一個決策層；完成後交給回測 Agent，通過前向驗證後才接正式 DailyReport 與 D-Plan。

> 實作狀態（2026-09-21）：P0～P2 已完成，包括 DecisionInputBundle、MomentumEngine、Momentum／Buy／Sell／Trade Adjudicator Skills、CLI 及 fail-closed validators。P3 配置與訂單、P4 Portfolio Risk／完整 CompetitionGuard、P5 主控保存仍待實作。

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
- 手續費、交易稅、整股單位、滑價、成交價、收盤價及壓力情境設定。

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
| `OrderProposal` | 目前持股到目標組合的整股買賣量、費稅與預估現金 |
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
| `OrderPlanner` | 計算整股買賣量，禁止超賣、放空及雙向重複訂單 |
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
- 費稅、整股、現金或情境結果無法重算時拒絕。
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
4. **P3 配置與訂單**：完成目標權重、整股、費稅、現金、換手及再平衡的確定性工具。
5. **P4 情境與風控**：建立 Risk Skill、壓力情境、全部 ETF Active Share、硬性規則及有上限修正循環。
6. **P5 主控與保存**：建立 `portfolio-decision` Skill、CLI、DecisionRepository、內容雜湊及完整重建 validator。
7. **P6 測試與稽核**：涵蓋買進、退出、無交易、資料缺漏、不可交易、現金不足、超限、壓力情境及風控拒絕。
8. **P7 回測交接**：固定資料、策略、Agent、工具及規則版本，交給[回測 Agent](backtest_agent_plan.md)與[回測方法規格](backtest_plan_v1.md)驗證。

第一個實作批次 P0～P2 已完成；下一批從 P3 開始，實作配置、股數、費稅與現金，再進入 P4 完整競賽風控。

## 完成條件

- 同一組輸入、設定與工具版本可重建相同數值結果。
- Buy 與 Sell packets 使用相同輸入且互相隔離，Adjudicator 不新增事實。
- 每筆權重、股數、費稅、現金及 Active Share 都可由原始輸入重算。
- 正常買進、退出、無交易、可修正與不可修正案例都有 fixture 及 fail-closed 測試。
- 必要規則違反時必定輸出 `rejected`，不生成可送件交易書。
- 完成後只交給回測 Agent；未通過歷史與前向驗證前，不宣稱可正式交易。
